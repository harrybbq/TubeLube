"""Speed/pitch conversion via ffmpeg.

Two modes:
  - preserve_pitch=True  -> atempo chain (tempo changes, pitch unchanged)
  - preserve_pitch=False -> asetrate trick (pitch shifts with speed, "slowed/sped" aesthetic)

ffmpeg's atempo filter only accepts factors in [0.5, 2.0]. For values outside
that range we chain multiple atempo filters whose product equals the target.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from collections import deque
from pathlib import Path
from typing import Callable, Optional

# Decode every ffmpeg/ffprobe pipe as UTF-8. Without an explicit encoding,
# Windows uses cp1252, which can't decode some UTF-8 bytes that ffmpeg prints for
# non-Latin titles (e.g. Japanese "あ" = E3 81 82; 0x81 is undefined in cp1252).
# The read loop then raised mid-stream, nothing drained the pipe, ffmpeg blocked
# on a full buffer and the job hung forever on "Converting…".
_TXT = {"text": True, "encoding": "utf-8", "errors": "replace"}

MIN_SPEED = 0.25
MAX_SPEED = 4.0


class ConversionError(Exception):
    pass


def _atempo_chain(speed: float) -> list[float]:
    """Decompose `speed` into a list of atempo factors, each in [0.5, 2.0],
    whose product equals `speed`. e.g. 0.25 -> [0.5, 0.5]; 4.0 -> [2.0, 2.0]."""
    if speed <= 0:
        raise ConversionError("Speed must be positive.")
    factors: list[float] = []
    remaining = speed
    while remaining < 0.5:
        factors.append(0.5)
        remaining /= 0.5
    while remaining > 2.0:
        factors.append(2.0)
        remaining /= 2.0
    factors.append(round(remaining, 6))
    return factors


def _probe_sample_rate(path: str) -> int:
    """Return the audio sample rate of the first audio stream, or 44100 if probing fails."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=sample_rate", "-of", "json", path],
            capture_output=True, **_TXT, check=True,
        )
        data = json.loads(out.stdout)
        return int(data["streams"][0]["sample_rate"])
    except Exception:
        return 44100


def _probe_duration(path: str) -> Optional[float]:
    """Return media duration in seconds, or None if probing fails."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "json", path],
            capture_output=True, **_TXT, check=True,
        )
        return float(json.loads(out.stdout)["format"]["duration"])
    except Exception:
        return None


_rubberband_cache: Optional[bool] = None


def _has_rubberband() -> bool:
    """Cache whether this ffmpeg build includes the rubberband filter."""
    global _rubberband_cache
    if _rubberband_cache is not None:
        return _rubberband_cache
    try:
        out = subprocess.run(
            ["ffmpeg", "-hide_banner", "-filters"],
            capture_output=True, **_TXT, check=True,
        )
        _rubberband_cache = " rubberband " in out.stdout
    except Exception:
        _rubberband_cache = False
    return _rubberband_cache


# ── Tone (EQ) ──────────────────────────────────────────────────────────────
# Three bands, mirrored exactly by the browser preview (EQ_BANDS in index.html)
# so what you hear while tweaking is what gets exported:
#   bass   → low shelf @ 100 Hz   (Web Audio "lowshelf")
#   mid    → peak     @ 1 kHz, Q=1 (Web Audio "peaking")
#   treble → high shelf @ 8 kHz   (Web Audio "highshelf")
# Web Audio's shelves follow the RBJ cookbook with slope S=1 and ignore Q, so
# ffmpeg's shelves must be `t=s:w=1` — the filters' default (t=q:w=0.5) is a
# different curve and the export would sound different from the preview.
EQ_BANDS = {"bass": 100, "mid": 1000, "treble": 8000}
EQ_LIMIT_DB = 12.0


def _eq_filter(eq: Optional[dict]) -> str:
    """ffmpeg filter for the tone settings, or "" when flat (output unchanged)."""
    g = {k: float((eq or {}).get(k) or 0.0) for k in EQ_BANDS}
    if not any(g.values()):
        return ""
    chain = [
        f"bass=g={g['bass']:g}:f={EQ_BANDS['bass']}:t=s:w=1",
        f"equalizer=f={EQ_BANDS['mid']}:t=q:w=1:g={g['mid']:g}",
        f"treble=g={g['treble']:g}:f={EQ_BANDS['treble']}:t=s:w=1",
    ]
    # A boost can push peaks past full scale, which the encoder would clip
    # harshly. A transparent limiter catches only those peaks (the preview uses
    # a matching DynamicsCompressor at -1 dB).
    if any(v > 0 for v in g.values()):
        chain.append("alimiter=limit=0.98:attack=5:release=50:level=disabled")
    return ",".join(chain)


def _eq_tag(eq: Optional[dict]) -> str:
    """Filename tag for non-zero bands, e.g. 'B+6_T-3'; "" when flat."""
    parts = []
    for key, letter in (("bass", "B"), ("mid", "M"), ("treble", "T")):
        v = float((eq or {}).get(key) or 0.0)
        if v:
            parts.append(f"{letter}{v:+g}")
    return "_".join(parts)


def _audio_filter(speed: float, preserve_pitch: bool, sample_rate: int) -> str:
    """Build the -filter:a expression."""
    if preserve_pitch:
        # Prefer librubberband — it's the same time-stretch engine Audacity uses
        # and what most "slowed" YouTube edits sound like. atempo (a phase
        # vocoder) leaves audible warble/smearing at heavy slowdowns, especially
        # on sustained instruments and vocals; rubberband handles transients and
        # phase coherence much better.
        #   transients=smooth — better for slowing music (less crispening of attacks)
        #   detector=compound + phase=independent + window=long — cleaner steady-state
        #   pitchq=quality + channels=together — best pitch tracking, no stereo drift
        if _has_rubberband():
            return (
                f"rubberband=tempo={speed:.6f}:pitch=1.0"
                ":transients=smooth:detector=compound:phase=independent"
                ":window=long:pitchq=quality:channels=together"
            )
        # Fallback if this ffmpeg build doesn't include librubberband.
        return ",".join(f"atempo={f}" for f in _atempo_chain(speed))
    # asetrate trick: declare a new sample rate so the same samples are
    # spread/compressed in time, which shifts pitch along with tempo, then
    # resample back to the original rate so playback is at a standard rate.
    # (This is the "slowed and reverb" aesthetic — pitch dropping is the point,
    # so high-quality time-stretch doesn't apply here.)
    #
    # resampler=soxr at precision=33 is the maximum-quality option ffmpeg
    # offers — essentially mathematically transparent. cutoff=0.95 preserves
    # more of the upper spectrum than the default 0.91, keeping cymbals and
    # hi-hats crisp. cheby=1 enables Chebyshev passband for steeper roll-off
    # with higher-precision irrational ratio approximation.
    return (
        f"asetrate={sample_rate}*{speed},"
        f"aresample={sample_rate}:resampler=soxr:precision=33:cutoff=0.95:cheby=1"
    )


def _build_output_name(src: str, speed: float, ext: str, out_dir: str,
                       start: Optional[float] = None, end: Optional[float] = None,
                       loops: int = 1, eq: Optional[dict] = None,
                       n_clips: int = 0) -> str:
    """Generate e.g. 'mytrack_0.75x.mp3', or 'mytrack_0.75x_t12-40_x3.mp3' when
    a section was trimmed and/or repeated ('_clips4' for a clip edit). Avoids
    collisions."""
    stem = Path(src).stem
    parts = [stem, f"{speed:g}x"]          # trims 1.000 -> 1, 0.750 -> 0.75
    if n_clips:
        parts.append(f"clips{n_clips}")
    elif start is not None or end is not None:
        s = 0.0 if start is None else start
        parts.append(f"t{s:g}-{end:g}" if end is not None else f"t{s:g}")
    if loops and loops > 1:
        parts.append(f"x{int(loops)}")
    if _eq_tag(eq):
        parts.append(_eq_tag(eq))
    stem_full = "_".join(parts)
    # Strip filesystem-unfriendly chars
    stem_full = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", stem_full)
    # Reserve the name atomically ("x" = create, fail if it exists). A plain
    # exists() check let two identical jobs (double-click, or phone + desktop)
    # pick the same name and overwrite each other's output.
    target = Path(out_dir) / f"{stem_full}.{ext}"
    n = 2
    while True:
        try:
            with open(target, "x"):
                pass
            return str(target)
        except FileExistsError:
            target = Path(out_dir) / f"{stem_full}_{n}.{ext}"
            n += 1


def _extract_segment(input_path: str, start: Optional[float], end: Optional[float],
                     output_format: str) -> str:
    """Cut [start, end] out to a temp file, re-encoded for frame accuracy.

    Needed because ffmpeg's -stream_loop silently ignores input seeking: asking
    for a 10s region repeated 3x with `-stream_loop 2 -ss 5 -to 15` yields 10s,
    not 30s. Cutting first, then looping the cut file, works correctly.
    """
    suffix = ".wav" if output_format == "mp3" else ".mkv"
    fd, tmp = tempfile.mkstemp(suffix=suffix, prefix="tubelube_seg_")
    os.close(fd)
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    if start is not None:
        cmd += ["-ss", f"{start:.3f}"]
    if end is not None:
        cmd += ["-to", f"{end:.3f}"]
    cmd += ["-i", input_path]
    if output_format == "mp3":
        cmd += ["-vn", "-c:a", "pcm_s16le"]          # lossless intermediate
    else:
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                "-c:a", "pcm_s16le"]
    cmd += [tmp]
    r = subprocess.run(cmd, capture_output=True, **_TXT)
    if r.returncode != 0:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        last = (r.stderr or "").strip().splitlines()[-1:] or [""]
        raise ConversionError("Couldn't cut the selected section "
                              f"(ffmpeg exited {r.returncode}) {last[0]}".rstrip())
    return tmp


MAX_SEGMENTS = 100        # clips × repeats; each becomes one ffmpeg input
JOIN_FADE = 0.005         # seconds of fade at a real cut, so joins don't click


def _clip_segments(clips: list, duration: Optional[float]) -> tuple[list, float]:
    """Expand [{start, end, repeat}] into the source ranges to play, in order.

    Returns (segments, total_seconds). Segments that continue exactly where the
    previous one stopped (a split that was never rearranged) are merged, so they
    cost no extra input and get no join. The total is the same either way.
    """
    expanded = []
    for i, c in enumerate(clips, 1):
        s = max(0.0, float(c["start"]))
        e = float(c["end"])
        if duration:
            # For a YouTube link the export input is a fresh download, not the
            # preview file — allow it to be a hair shorter than what was edited.
            e = min(e, duration)
            if s >= duration - 0.05:
                raise ConversionError(f"Clip {i} starts after the end of the track.")
        if e - s < 0.05:
            raise ConversionError(f"Clip {i} is too short.")
        expanded += [(s, e)] * int(c.get("repeat") or 1)
    if not expanded:
        raise ConversionError("The clip edit is empty — add a clip or remove the edit.")
    if len(expanded) > MAX_SEGMENTS:
        raise ConversionError(f"That edit has {len(expanded)} pieces once repeats are "
                              f"counted; the limit is {MAX_SEGMENTS}.")
    total = sum(e - s for s, e in expanded)
    merged = [list(expanded[0])]
    for s, e in expanded[1:]:
        if abs(s - merged[-1][1]) < 0.001:
            merged[-1][1] = e
        else:
            merged.append([s, e])
    return [tuple(m) for m in merged], total


def _clip_graph(segments: list, video: bool, audio_filter: str, speed: float) -> str:
    """filter_complex that concatenates one input per segment, then applies
    the usual speed (+ tone) chain to the joined result."""
    parts, cat = [], ""
    last = len(segments) - 1
    for k, (s, e) in enumerate(segments):
        chain = "asetpts=PTS-STARTPTS"
        if k > 0:
            chain += f",afade=t=in:st=0:d={JOIN_FADE}"
        if k < last:
            chain += f",afade=t=out:st={max(0.0, e - s - JOIN_FADE):.4f}:d={JOIN_FADE}"
        parts.append(f"[{k}:a:0]{chain}[a{k}]")
        if video:
            parts.append(f"[{k}:v:0]setpts=PTS-STARTPTS[v{k}]")
            cat += f"[v{k}][a{k}]"
        else:
            cat += f"[a{k}]"
    n = len(segments)
    if video:
        parts.append(f"{cat}concat=n={n}:v=1:a=1[cv][ca]")
        parts.append(f"[cv]setpts={1.0/speed:.6f}*PTS[vout]")
    else:
        parts.append(f"{cat}concat=n={n}:v=0:a=1[ca]")
    parts.append(f"[ca]{audio_filter}[aout]")
    return ";".join(parts)


_TIME_RE = re.compile(r"time=(\d+):(\d+):(\d+\.?\d*)")
# ffmpeg prefixes log lines with internal context like "[in#0 @ 000002197b8e6fc0]".
_FF_NOISE = re.compile(r"\[[^\]]*@ [0-9a-fA-Fx]+\]\s*")


def _parse_progress(line: str, total_duration: Optional[float]) -> Optional[float]:
    """Parse ffmpeg's stderr 'time=HH:MM:SS.ss' and return % of total (0-100)."""
    if not total_duration:
        return None
    m = _TIME_RE.search(line)
    if not m:
        return None
    h, mi, s = m.groups()
    elapsed = int(h) * 3600 + int(mi) * 60 + float(s)
    # `time=` is the OUTPUT timestamp, so total_duration must be the expected
    # output length (input ÷ speed) — verified: a 30s input at 0.5× reports 60s.
    return max(0.0, min(100.0, (elapsed / total_duration) * 100.0))


def convert(
    input_path: str,
    output_format: str,           # "mp3" or "mp4"
    speed: float,
    preserve_pitch: bool,
    output_dir: str,
    audio_bitrate: str = "192k",  # for mp3 / mp4-audio: "192k" or "320k"
    on_progress: Optional[Callable[[float], None]] = None,
    start: Optional[float] = None,   # trim: section start in source seconds
    end: Optional[float] = None,     # trim: section end in source seconds
    loops: int = 1,                  # repeat the (trimmed) section N times
    eq: Optional[dict] = None,       # tone: {"bass","mid","treble"} in dB (±12)
    clips: Optional[list] = None,    # clip edit: [{start, end, repeat}] in order
) -> str:
    """Run ffmpeg to produce the sped/slowed file. Returns the output path.

    `start`/`end` trim to a section of the source; `loops` repeats that section
    (1 = play once). Trim positions are in *source* time, matching the preview
    player's timeline (which reports media time regardless of playback speed).
    `clips` (from the clip editor) replaces trim/loops: the listed source ranges
    are played in order, each `repeat` times, then sped up/slowed as a whole.
    """
    if not (MIN_SPEED <= speed <= MAX_SPEED):
        raise ConversionError(f"Speed must be between {MIN_SPEED} and {MAX_SPEED}.")
    if not os.path.isfile(input_path):
        raise ConversionError(f"Input file not found: {input_path}")
    if not os.path.isdir(output_dir):
        raise ConversionError(f"Output folder not found: {output_dir}")
    if output_format not in ("mp3", "mp4"):
        raise ConversionError("Output format must be 'mp3' or 'mp4'.")
    if not shutil.which("ffmpeg"):
        raise ConversionError("ffmpeg is not installed or not on PATH.")

    sr = _probe_sample_rate(input_path)
    duration = _probe_duration(input_path)
    audio_filter = _audio_filter(speed, preserve_pitch, sr)
    # EQ goes AFTER the speed change — the browser preview filters audio that's
    # already rate-changed (and pitch-shifted in pitch-follows mode), so this is
    # the only order where "bass" means the same frequencies in both.
    eq_chain = _eq_filter(eq)
    if eq_chain:
        audio_filter = f"{audio_filter},{eq_chain}"

    # ── Clip edit, or normalise the trim window ──
    segments = None
    loops = max(1, int(loops or 1))
    if clips:
        segments, clip_total = _clip_segments(clips, duration)
        start = end = None
        loops = 1
    elif start is not None or end is not None:
        s = 0.0 if start is None else max(0.0, float(start))
        e = float(end) if end is not None else (duration or 0.0)
        if duration:
            e = min(e, duration)
        if e - s < 0.05:
            raise ConversionError("The selected section is too short — "
                                  "drag the end handle further from the start.")
        start, end = s, e
    section = (end - start) if (start is not None and end is not None) else duration

    # Reserves the final name (empty placeholder). ffmpeg writes to a sibling
    # ".part" file that's renamed over it only on success, so a failed or killed
    # conversion never leaves a truncated song in the output folder.
    out_path = _build_output_name(input_path, speed, output_format, output_dir,
                                  start, end, loops, eq,
                                  n_clips=len(clips) if segments else 0)
    final = Path(out_path)
    part = final.with_name(f"{final.stem}.part{final.suffix}")  # ffmpeg muxes by last extension

    def _discard_outputs() -> None:
        for p in (part, final):
            try:
                p.unlink()
            except OSError:
                pass

    temp_segment: Optional[str] = None
    proc: Optional[subprocess.Popen] = None
    tail: deque = deque(maxlen=15)           # last ffmpeg lines, for a useful error
    try:
        # ── Choose the source: loops need the section cut out first (see
        #    _extract_segment for why -stream_loop can't do it in one pass) ──
        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "info"]
        graph = None
        if segments:
            # One input per piece, each seeked to its own range (input -ss is
            # accurate when re-encoding), joined by the concat filter. Inputs are
            # read in turn, so memory doesn't grow with the length of the edit.
            for s0, e0 in segments:
                cmd += ["-ss", f"{s0:.3f}", "-t", f"{e0 - s0:.3f}", "-i", input_path]
            total_input_secs = clip_total
            graph = _clip_graph(segments, output_format == "mp4", audio_filter, speed)
        elif loops > 1:
            if on_progress:
                on_progress(0.0)
            temp_segment = _extract_segment(input_path, start, end, output_format)
            cmd += ["-stream_loop", str(loops - 1), "-i", temp_segment]
            total_input_secs = (section or 0.0) * loops
        else:
            if start is not None:
                cmd += ["-ss", f"{start:.3f}"]
            if end is not None:
                cmd += ["-to", f"{end:.3f}"]
            cmd += ["-i", input_path]
            total_input_secs = section
        # ffmpeg's progress `time=` is output time, and every speed filter here
        # (atempo, asetrate, setpts) makes the output input÷speed long.
        expected_out = (total_input_secs / speed) if total_input_secs else None

        if graph:
            # -filter:a / -filter:v can't be combined with -filter_complex, so the
            # speed and tone chains live at the end of the graph instead.
            cmd += ["-filter_complex", graph]
            if output_format == "mp4":
                cmd += ["-map", "[vout]", "-map", "[aout]",
                        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
                        "-c:a", "aac", "-b:a", audio_bitrate, "-aac_coder", "twoloop"]
            else:
                cmd += ["-map", "[aout]", "-c:a", "libmp3lame", "-b:a", audio_bitrate,
                        "-compression_level", "0"]
            cmd += [str(part)]
        elif output_format == "mp3":
            cmd += [
                "-vn",
                "-filter:a", audio_filter,
                "-c:a", "libmp3lame",
                "-b:a", audio_bitrate,
                # libmp3lame compression_level controls encoder effort, NOT bitrate.
                # 0 = slowest/highest-quality psychoacoustic analysis (still fast in practice).
                "-compression_level", "0",
                str(part),
            ]
        else:  # mp4
            # setpts scales the video PTS; 1/speed because PTS divisor speeds it up
            # when speed > 1. Audio uses the same filter chain so A/V stay aligned.
            video_filter = f"setpts={1.0/speed:.6f}*PTS"
            cmd += [
                "-filter:v", video_filter,
                "-filter:a", audio_filter,
                "-c:v", "libx264", "-preset", "medium", "-crf", "20",
                "-c:a", "aac",
                "-b:a", audio_bitrate,
                # twoloop is AAC's higher-quality (slower) coder; the default uses a faster heuristic.
                "-aac_coder", "twoloop",
                str(part),
            ]

        proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, bufsize=1, **_TXT,
        )
        assert proc.stderr is not None
        for line in proc.stderr:
            tail.append(line.rstrip())
            if on_progress:
                pct = _parse_progress(line, expected_out)
                if pct is not None:
                    on_progress(pct)
        ret = proc.wait()
    except BaseException:
        # Any unexpected error mid-read must kill ffmpeg first — otherwise it
        # blocks writing to a pipe nobody drains, and wait() hangs forever.
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait()
        _discard_outputs()
        raise
    finally:
        if temp_segment:                     # always clean up the cut-out section
            try:
                os.unlink(temp_segment)
            except OSError:
                pass

    if ret != 0:
        _discard_outputs()
        # Skip ffmpeg's progress/banner noise; keep the lines that explain failure.
        detail = []
        for l in tail:
            l = _FF_NOISE.sub("", l).replace("�", "").strip()
            if l and not l.startswith(("frame=", "size=", "Press [q]")):
                detail.append(l)
        raise ConversionError("ffmpeg failed: " + " | ".join(detail[-2:]) if detail
                              else f"ffmpeg failed (exit {ret})")
    os.replace(part, final)
    return out_path


# TODO v2: batch processing — accept a list of (input, speed) pairs.
# TODO v2: reverb/effects — chain aecho or use sox.
