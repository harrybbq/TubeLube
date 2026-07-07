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
from pathlib import Path
from typing import Callable, Optional

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
            capture_output=True, text=True, check=True,
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
            capture_output=True, text=True, check=True,
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
            capture_output=True, text=True, check=True,
        )
        _rubberband_cache = " rubberband " in out.stdout
    except Exception:
        _rubberband_cache = False
    return _rubberband_cache


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


def _build_output_name(src: str, speed: float, ext: str, out_dir: str) -> str:
    """Generate e.g. 'mytrack_0.75x.mp3' in out_dir, avoiding collisions."""
    stem = Path(src).stem
    # Trim 1.000 -> 1, 0.750 -> 0.75
    speed_str = f"{speed:g}"
    base = f"{stem}_{speed_str}x.{ext}"
    # Strip filesystem-unfriendly chars
    base = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", base)
    target = Path(out_dir) / base
    n = 2
    while target.exists():
        target = Path(out_dir) / f"{stem}_{speed_str}x_{n}.{ext}"
        n += 1
    return str(target)


_TIME_RE = re.compile(r"time=(\d+):(\d+):(\d+\.?\d*)")


def _parse_progress(line: str, total_duration: Optional[float]) -> Optional[float]:
    """Parse ffmpeg's stderr 'time=HH:MM:SS.ss' and return % of total (0-100)."""
    if not total_duration:
        return None
    m = _TIME_RE.search(line)
    if not m:
        return None
    h, mi, s = m.groups()
    elapsed = int(h) * 3600 + int(mi) * 60 + float(s)
    # Output duration shrinks by 1/speed but ffmpeg reports input-time progress
    # for filter graphs; using total_duration of the source is the right denominator.
    return max(0.0, min(100.0, (elapsed / total_duration) * 100.0))


def convert(
    input_path: str,
    output_format: str,           # "mp3" or "mp4"
    speed: float,
    preserve_pitch: bool,
    output_dir: str,
    audio_bitrate: str = "192k",  # for mp3 / mp4-audio: "192k" or "320k"
    on_progress: Optional[Callable[[float], None]] = None,
) -> str:
    """Run ffmpeg to produce the sped/slowed file. Returns the output path."""
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
    out_path = _build_output_name(input_path, speed, output_format, output_dir)

    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "info", "-i", input_path]

    if output_format == "mp3":
        cmd += [
            "-vn",
            "-filter:a", audio_filter,
            "-c:a", "libmp3lame",
            "-b:a", audio_bitrate,
            # libmp3lame compression_level controls encoder effort, NOT bitrate.
            # 0 = slowest/highest-quality psychoacoustic analysis (still fast in practice).
            "-compression_level", "0",
            out_path,
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
            out_path,
        ]

    proc = subprocess.Popen(
        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        text=True, bufsize=1, universal_newlines=True,
    )
    assert proc.stderr is not None
    try:
        for line in proc.stderr:
            if on_progress:
                pct = _parse_progress(line, duration)
                if pct is not None:
                    on_progress(pct)
    finally:
        ret = proc.wait()
    if ret != 0:
        raise ConversionError(f"ffmpeg exited with code {ret}")
    return out_path


# TODO v2: batch processing — accept a list of (input, speed) pairs.
# TODO v2: trimming (-ss / -to flags before -i).
# TODO v2: reverb/effects — chain aecho or use sox.
