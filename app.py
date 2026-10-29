"""TubeLube — local Flask app for YouTube/local audio-video speed control.

Routes:
  GET  /                     UI (or install-prompt page if deps missing)
  POST /pick_folder          native folder picker, returns chosen path (local only)
  POST /upload               accepts a dropped/picked local file, returns temp path
  POST /convert              starts a background job, returns job_id
  GET  /status/<job_id>      progress + status + result path
  GET  /download/<job_id>    sends a finished job's output file (for phones/remote)
  POST /open_folder          opens the OS file manager at a given folder (local only)

Run `python app.py --lan` to also accept connections from other devices on
your Wi-Fi (e.g. your phone) — it prints the address and a QR code to scan.
"""

from __future__ import annotations

import argparse
import array
import ipaddress
import math
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import uuid
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from flask import Flask, jsonify, render_template, request, send_file, send_from_directory

import converter
import downloader
import playset

APP_HOST = "127.0.0.1"
APP_PORT = 5001

# When frozen by PyInstaller, bundled data (templates/) lives under _MEIPASS;
# point Flask there. In normal runs this is just the app directory.
if getattr(sys, "frozen", False):
    _BASE = Path(getattr(sys, "_MEIPASS", os.path.dirname(__file__)))
    app = Flask(__name__,
                template_folder=str(_BASE / "templates"),
                static_folder=str(_BASE / "static"))
else:
    app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024 * 1024  # 2 GB upload cap

UPLOAD_DIR = Path(tempfile.gettempdir()) / "tubelube_uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


@dataclass
class Job:
    id: str
    status: str = "pending"          # pending | downloading | converting | done | error
    message: str = ""
    progress: float = 0.0            # 0..100
    output_path: Optional[str] = None
    error: Optional[str] = None
    lock: threading.Lock = field(default_factory=threading.Lock)


JOBS: dict[str, Job] = {}
# Guards JOBS itself (each Job has its own lock for its fields). Without it,
# pruning iterated the dict while another request inserted into it, which can
# raise "dictionary changed size during iteration" and 500 the request.
_JOBS_LOCK = threading.Lock()


def _get_job(job_id: str) -> Optional[Job]:
    with _JOBS_LOCK:
        return JOBS.get(job_id)


def _check_dependencies() -> list[str]:
    """Return list of missing dep names ('ffmpeg', 'yt-dlp')."""
    missing = []
    if not shutil.which("ffmpeg"):
        missing.append("ffmpeg")
    try:
        import yt_dlp  # noqa: F401
    except ImportError:
        missing.append("yt-dlp")
    return missing


def _request_is_local() -> bool:
    """True when the request comes from this machine (not a phone/LAN device)."""
    return request.remote_addr in ("127.0.0.1", "::1")


# ── Request hardening ─────────────────────────────────────────────────────
# Two holes this closes:
#  1. Any website open in the user's browser could POST to 127.0.0.1:5001 —
#     get_json(force=True) accepted text/plain bodies, which browsers send
#     cross-site without a CORS check. The browser always sends an Origin
#     header on such requests, so reject one that isn't this app.
#  2. DNS rebinding: an attacker's domain re-pointed at 127.0.0.1 makes requests
#     look same-origin. Their Host header still carries the attacker's domain,
#     so only accept Hosts that are an IP address or "localhost".
def _host_ok(host: str) -> bool:
    name = host[1:host.find("]")] if host.startswith("[") else host.rsplit(":", 1)[0]
    if name == "localhost":
        return True
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        return False


@app.before_request
def _guard_request():
    if not _host_ok(request.host or ""):
        return jsonify({"error": "Requests must be addressed to this computer's IP or localhost."}), 403
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        origin = request.headers.get("Origin")
        if origin and urlparse(origin).netloc != request.host:
            return jsonify({"error": "Blocked a request from another website."}), 403


def _json_body() -> dict:
    """The JSON object body, or {} — never trusts a non-JSON content type."""
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _str(data: dict, key: str) -> str:
    """A stripped string field; non-strings (numbers, lists) count as empty."""
    v = data.get(key)
    return v.strip() if isinstance(v, str) else ""


def _is_unc(path: str) -> bool:
    # \\server\share paths make Windows connect out over SMB (leaking the
    # user's NTLM hash to whoever runs that server). Never touch them.
    return path.startswith(("\\\\", "//"))


def _parse_eq(data: dict):
    """Validate the optional tone settings. Returns (eq_dict, error_message).
    Missing → flat. Each band is dB within ±EQ_LIMIT_DB; anything else is a 400."""
    raw = data.get("eq")
    if raw in (None, ""):
        return {k: 0.0 for k in converter.EQ_BANDS}, None
    if not isinstance(raw, dict):
        return None, "Tone settings must be an object like {bass, mid, treble}."
    out = {}
    for band in converter.EQ_BANDS:
        v = raw.get(band, 0)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            return None, f"{band.capitalize()} must be a number of dB."
        if abs(v) > converter.EQ_LIMIT_DB:
            return None, f"{band.capitalize()} must be between -{converter.EQ_LIMIT_DB:g} and +{converter.EQ_LIMIT_DB:g} dB."
        out[band] = round(float(v), 1)
    return out, None


MAX_CLIPS = 100
MAX_CLIP_REPEAT = 20


def _parse_clips(data: dict):
    """Validate the optional clip edit. Returns (clips | None, error_message).
    Missing / null → no edit. Otherwise a non-empty list of
    {start, end, repeat}: finite seconds, end > start, repeat 1–20, and at most
    converter.MAX_SEGMENTS pieces once repeats are expanded."""
    raw = data.get("clips")
    if raw is None:
        return None, None
    if not isinstance(raw, list) or not raw:
        return None, "The clip edit must be a non-empty list of clips."
    if len(raw) > MAX_CLIPS:
        return None, f"A clip edit can have at most {MAX_CLIPS} clips."
    out, pieces = [], 0
    for i, c in enumerate(raw, 1):
        if not isinstance(c, dict):
            return None, f"Clip {i} must be an object like {{start, end, repeat}}."
        s, e, r = c.get("start"), c.get("end"), c.get("repeat", 1)
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
               for v in (s, e)):
            return None, f"Clip {i} needs a start and end in seconds."
        if s < 0 or e - s < 0.05:
            return None, f"Clip {i} is too short or out of range."
        if isinstance(r, bool) or not isinstance(r, int) or not (1 <= r <= MAX_CLIP_REPEAT):
            return None, f"Clip {i} can repeat 1 to {MAX_CLIP_REPEAT} times."
        pieces += r
        out.append({"start": round(float(s), 3), "end": round(float(e), 3), "repeat": r})
    if pieces > converter.MAX_SEGMENTS:
        return None, (f"That edit has {pieces} pieces once repeats are counted; "
                      f"the limit is {converter.MAX_SEGMENTS}.")
    return out, None


def _inside_uploads(path: str) -> bool:
    """Local files may only come from our own upload folder — the UI only ever
    sends /upload results, and anything else let a LAN device convert (and then
    download) arbitrary media files from this PC."""
    try:
        return Path(path).resolve().is_relative_to(UPLOAD_DIR.resolve())
    except (OSError, ValueError):
        return False


def _lan_ip() -> Optional[str]:
    """Best-effort LAN IP of this machine (no traffic is actually sent)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        return None


def _default_output_dir() -> str:
    """User's Downloads folder, or home dir as a fallback."""
    candidate = Path.home() / "Downloads"
    if candidate.is_dir():
        return str(candidate)
    return str(Path.home())


@app.errorhandler(playset.PlaysetUnavailable)
def _playset_unavailable(e):
    # Better a clear "try again" than overwriting the user's playlists.
    return jsonify({"error": f"Your playlists couldn't be opened just now ({e}). "
                             "Close any other TubeLube window and try again."}), 503


@app.route("/")
def index():
    missing = _check_dependencies()
    if missing:
        return render_template("install_prompt.html", missing=missing), 200
    return render_template("index.html", default_output_dir=_default_output_dir())


@app.route("/pick_folder", methods=["POST"])
def pick_folder():
    """Open a native folder picker on the user's machine."""
    if not _request_is_local():
        return jsonify({"error": "Only available on the computer running TubeLube."}), 403
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        path = filedialog.askdirectory(title="Choose output folder")
        root.destroy()
        return jsonify({"path": path or ""})
    except Exception as e:
        return jsonify({"error": f"Folder picker unavailable: {e}"}), 500


@app.route("/upload", methods=["POST"])
def upload():
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "No file provided."}), 400
    safe_name = Path(f.filename).name  # strip any path components
    if not safe_name.lower().endswith((".mp3", ".mp4", ".m4a", ".wav", ".webm", ".mkv", ".mov")):
        return jsonify({"error": "Unsupported file type. Use MP3 or MP4."}), 400
    dest = UPLOAD_DIR / f"{uuid.uuid4().hex}_{safe_name}"
    f.save(dest)
    return jsonify({"path": str(dest), "name": safe_name, "preview_url": f"/serve/{dest.name}"})


@app.route("/serve/<path:filename>")
def serve_upload(filename: str):
    """Stream a previously-uploaded or fetched preview file back to the browser.
    send_from_directory rejects path traversal and resolves only within UPLOAD_DIR."""
    return send_from_directory(UPLOAD_DIR, filename, conditional=True)


_PEAKS_CACHE: dict = {}
PEAK_BUCKETS = 1500


@app.route("/peaks/<path:filename>")
def peaks(filename: str):
    """Waveform overview of a previewable file for the clip editor: the loudest
    sample in each of ~1500 slices, 0–1. ffmpeg decodes to 8 kHz mono so a long
    mix or a video costs a few MB here instead of gigabytes in the browser."""
    path = UPLOAD_DIR / Path(filename).name            # same rule as /serve
    if not path.is_file():
        return jsonify({"error": "File not found."}), 404
    key = (path.name, path.stat().st_mtime_ns)
    if key in _PEAKS_CACHE:
        return jsonify(_PEAKS_CACHE[key])
    r = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", "8000",
         "-f", "s16le", "-"],
        capture_output=True, timeout=120,
    )
    if r.returncode != 0 or not r.stdout:
        return jsonify({"error": "Couldn't read the audio for a waveform."}), 500
    samples = array.array("h")
    samples.frombytes(r.stdout[: len(r.stdout) // 2 * 2])
    n = len(samples)
    step = max(1, n // PEAK_BUCKETS)
    out = []
    for i in range(0, n, step):
        seg = samples[i:i + step]
        out.append(round(max(max(seg), -min(seg)) / 32768, 3))
    result = {"peaks": out, "duration": n / 8000}
    if len(_PEAKS_CACHE) > 32:
        _PEAKS_CACHE.clear()
    _PEAKS_CACHE[key] = result
    return jsonify(result)


@app.route("/preview_youtube", methods=["POST"])
def preview_youtube():
    data = _json_body()
    url = _str(data, "url")
    if not url:
        return jsonify({"error": "No URL provided."}), 400
    try:
        path = downloader.download_audio_preview(url, str(UPLOAD_DIR))
    except downloader.DownloadError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": f"Preview download failed: {e}"}), 500
    name = Path(path).name
    return jsonify({"path": path, "name": name, "preview_url": f"/serve/{name}"})


# ── Playlists: named collections of saved songs + their speed/pitch settings ──

@app.route("/playlists", methods=["GET"])
def playlists_list():
    return jsonify(playset.list_playlists())


@app.route("/playlists", methods=["POST"])
def playlists_create():
    data = _json_body()
    name = _str(data, "name")
    if not name:
        return jsonify({"error": "Playlist needs a name."}), 400
    return jsonify({"playlist": playset.create_playlist(name)})


@app.route("/playlists/<pid>", methods=["GET"])
def playlist_get(pid: str):
    pl = playset.get_playlist(pid)
    if pl is None:
        return jsonify({"error": "Playlist not found."}), 404
    playset.set_active(pid)  # remember last-viewed across launches
    return jsonify({"playlist": pl})


@app.route("/playlists/<pid>", methods=["DELETE"])
def playlist_delete(pid: str):
    if not playset.delete_playlist(pid):
        return jsonify({"error": "Can't delete (it may be your last playlist)."}), 400
    return jsonify({"ok": True})


@app.route("/playlists/<pid>/songs", methods=["POST"])
def playlist_add_song(pid: str):
    data = _json_body()
    url = _str(data, "url")
    if not downloader.is_youtube_url(url):
        return jsonify({"error": "Saving needs a valid YouTube URL."}), 400

    try:
        speed = float(data.get("speed", 1.0))
    except (TypeError, ValueError):
        return jsonify({"error": "Speed must be a number."}), 400
    if not (converter.MIN_SPEED <= speed <= converter.MAX_SPEED):
        return jsonify({"error": f"Speed must be between {converter.MIN_SPEED} and {converter.MAX_SPEED}."}), 400

    output_format = data.get("output_format")
    if output_format not in ("mp3", "mp4"):
        output_format = "mp3"
    quality = data.get("quality") or ("best" if output_format == "mp4" else "192")
    preserve_pitch = bool(data.get("preserve_pitch", False))

    title = _str(data, "title")
    if not title:
        # Best-effort title lookup; falls back to the URL inside get_title.
        title = downloader.get_title(url)

    eq, err = _parse_eq(data)
    if err:
        return jsonify({"error": err}), 400
    entry = playset.add_song(pid, url, title, speed, preserve_pitch, output_format, quality, eq)
    if entry is None:
        return jsonify({"error": "Playlist not found."}), 404
    return jsonify({"song": entry})


@app.route("/playlists/<pid>/songs/<song_id>", methods=["DELETE"])
def playlist_remove_song(pid: str, song_id: str):
    if not playset.remove_song(pid, song_id):
        return jsonify({"error": "Song not found."}), 404
    return jsonify({"ok": True})


@app.route("/playlists/<pid>/reorder", methods=["POST"])
def playlist_reorder(pid: str):
    data = _json_body()
    order = data.get("order")
    if not isinstance(order, list):
        return jsonify({"error": "order must be a list of song ids."}), 400
    songs = playset.reorder(pid, [str(x) for x in order])
    if songs is None:
        return jsonify({"error": "Playlist not found."}), 404
    return jsonify({"songs": songs})


def _run_job(job: Job, params: dict) -> None:
    """Background worker: optionally download, then convert."""
    try:
        source = params.get("source", "").strip()
        is_url = downloader.is_youtube_url(source)
        local_path = params.get("local_path") or None
        output_format = params["output_format"]
        speed = float(params["speed"])
        # Default must match the UI's default (unchecked = pitch follows speed);
        # this used to default True, silently preserving pitch if the field
        # were ever missing from the request.
        preserve_pitch = bool(params.get("preserve_pitch", False))
        output_dir = params["output_dir"]
        quality = params.get("quality", "best" if output_format == "mp4" else "192")

        if is_url:
            with job.lock:
                job.status = "downloading"
                job.message = "Downloading…"
                job.progress = 0.0

            def dl_progress(pct: float, msg: str) -> None:
                with job.lock:
                    job.progress = pct
                    job.message = f"{msg}… {pct:.0f}%"

            input_path = downloader.download(
                source, output_format, quality, str(UPLOAD_DIR), on_progress=dl_progress,
            )
        elif local_path and _inside_uploads(local_path) and os.path.isfile(local_path):
            input_path = local_path
        else:
            raise ValueError("Provide a YouTube URL or pick a local file.")

        with job.lock:
            job.status = "converting"
            job.message = "Converting…"
            job.progress = 0.0

        def conv_progress(pct: float) -> None:
            with job.lock:
                job.progress = pct
                job.message = f"Converting… {pct:.0f}%"

        out = converter.convert(
            input_path=input_path,
            output_format=output_format,
            speed=speed,
            preserve_pitch=preserve_pitch,
            output_dir=output_dir,
            # MP3: respect the user's bitrate pick (192k / 320k).
            # MP4: the quality picker selects video resolution, not audio — so
            # pin audio to 320k AAC (transparent, modest size impact next to video).
            audio_bitrate=f"{quality}k" if output_format == "mp3" else "320k",
            on_progress=conv_progress,
            start=params.get("trim_start"),
            end=params.get("trim_end"),
            loops=int(params.get("loops") or 1),
            eq=params.get("eq"),
            clips=params.get("clips"),
        )
        with job.lock:
            job.status = "done"
            job.message = "Done"
            job.progress = 100.0
            job.output_path = out
    except Exception as e:
        with job.lock:
            job.status = "error"
            job.error = str(e)
            job.message = ""


@app.route("/convert", methods=["POST"])
def start_convert():
    data = _json_body()

    # Validate speed early to give a friendly error.
    try:
        s = float(data.get("speed", 1.0))
    except (TypeError, ValueError):
        return jsonify({"error": "Speed must be a number."}), 400
    if not (converter.MIN_SPEED <= s <= converter.MAX_SPEED):
        return jsonify({"error": f"Speed must be between {converter.MIN_SPEED} and {converter.MAX_SPEED}."}), 400

    fmt = data.get("output_format")
    if fmt not in ("mp3", "mp4"):
        return jsonify({"error": "Choose MP3 or MP4."}), 400

    # Quality was never checked: "quality": null reached ffmpeg as "-b:a Nonek"
    # and only failed after the whole download.
    allowed_q = ("192", "320") if fmt == "mp3" else ("720", "1080", "best")
    q = data.get("quality")
    if q in (None, ""):
        q = allowed_q[0] if fmt == "mp3" else "best"
    q = str(q)
    if q not in allowed_q:
        return jsonify({"error": f"Quality must be one of: {', '.join(allowed_q)}."}), 400
    data["quality"] = q

    # bool("false") is True — accept only a real JSON true/false.
    pp = data.get("preserve_pitch", False)
    if not isinstance(pp, bool):
        return jsonify({"error": "preserve_pitch must be true or false."}), 400

    # Phones/LAN devices can't see this PC's folders, so their conversions
    # always land in the default folder rather than wherever the request says.
    out_dir = _str(data, "output_dir") if _request_is_local() else _default_output_dir()
    if not out_dir or _is_unc(out_dir) or not os.path.isdir(out_dir):
        return jsonify({"error": "Choose a valid output folder."}), 400
    data["output_dir"] = out_dir

    source = _str(data, "source")
    local_path = _str(data, "local_path")
    if not source and not local_path:
        return jsonify({"error": "Drop a file or paste a YouTube URL."}), 400
    if source and not downloader.is_youtube_url(source) and not local_path:
        return jsonify({"error": "That URL doesn't look like a YouTube link."}), 400
    if local_path and not source and not _inside_uploads(local_path):
        return jsonify({"error": "That file wasn't uploaded through TubeLube — drop or choose it again."}), 400
    data["source"], data["local_path"] = source, local_path

    # Trim window / repeat count (all optional).
    ts, te = data.get("trim_start"), data.get("trim_end")
    try:
        ts = None if ts in (None, "") else float(ts)
        te = None if te in (None, "") else float(te)
    except (TypeError, ValueError):
        return jsonify({"error": "Trim positions must be numbers."}), 400
    # NaN slips past every < / > comparison and reached ffmpeg as "-to nan".
    if any(v is not None and not math.isfinite(v) for v in (ts, te)):
        return jsonify({"error": "Trim positions must be real numbers."}), 400
    if ts is not None and ts < 0:
        return jsonify({"error": "Trim start can't be negative."}), 400
    if ts is not None and te is not None and te - ts < 0.05:
        return jsonify({"error": "The selected section is too short."}), 400
    try:
        loops = int(data.get("loops") or 1)
    except (TypeError, ValueError):
        return jsonify({"error": "Repeat count must be a whole number."}), 400
    if not (1 <= loops <= 100):
        return jsonify({"error": "Repeat count must be between 1 and 100."}), 400
    data["trim_start"], data["trim_end"], data["loops"] = ts, te, loops
    eq, err = _parse_eq(data)
    if err:
        return jsonify({"error": err}), 400
    data["eq"] = eq
    clips, err = _parse_clips(data)
    if err:
        return jsonify({"error": err}), 400
    data["clips"] = clips

    job = Job(id=uuid.uuid4().hex)
    with _JOBS_LOCK:
        _prune_jobs()
        JOBS[job.id] = job
    t = threading.Thread(target=_run_job, args=(job, data), daemon=True)
    t.start()
    return jsonify({"job_id": job.id})


@app.route("/status/<job_id>")
def status(job_id: str):
    job = _get_job(job_id)
    if not job:
        return jsonify({"error": "Unknown job."}), 404
    with job.lock:
        return jsonify({
            "status": job.status,
            "message": job.message,
            "progress": job.progress,
            "output_path": job.output_path,
            "error": job.error,
        })


@app.route("/download/<job_id>")
def download(job_id: str):
    """Send a finished job's output file — how phones save the result, since
    'open folder' only makes sense on the machine the server runs on."""
    job = _get_job(job_id)
    if not job:
        return jsonify({"error": "Unknown job."}), 404
    with job.lock:
        path = job.output_path if job.status == "done" else None
    if not path or not os.path.isfile(path):
        return jsonify({"error": "No finished file for this job."}), 404
    return send_file(path, as_attachment=True, download_name=Path(path).name)


@app.route("/open_folder", methods=["POST"])
def open_folder():
    if not _request_is_local():
        return jsonify({"error": "Only available on the computer running TubeLube."}), 403
    data = _json_body()
    path = _str(data, "path")
    if not path:
        return jsonify({"error": "No path."}), 400
    if _is_unc(path):
        return jsonify({"error": "Network paths can't be opened from TubeLube."}), 400
    folder = path if os.path.isdir(path) else os.path.dirname(path)
    if not os.path.isdir(folder):
        return jsonify({"error": "Folder not found."}), 404
    try:
        if sys.platform.startswith("win"):
            # Highlight the file if a file path was given, else just open the folder.
            if os.path.isfile(path):
                subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
            else:
                os.startfile(folder)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", folder])
        else:
            subprocess.Popen(["xdg-open", folder])
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _open_browser():
    # Always via loopback — APP_HOST may be 0.0.0.0 in LAN mode.
    webbrowser.open(f"http://127.0.0.1:{APP_PORT}/")


def _print_phone_banner() -> None:
    ip = _lan_ip()
    if not ip:
        print("Couldn't work out this machine's LAN IP — find it with "
              "`ipconfig` (Windows) or `ifconfig` (macOS/Linux) and open "
              f"http://<that-ip>:{APP_PORT}/ on your phone.")
        return
    url = f"http://{ip}:{APP_PORT}/"
    print()
    print("=" * 46)
    print("  On your phone (same Wi-Fi), open:")
    print(f"    {url}")
    print("=" * 46)
    try:
        import qrcode
    except ImportError:
        print("(pip install qrcode to get a scannable code here)")
    else:
        qr = qrcode.QRCode(border=1)
        qr.add_data(url)
        qr.print_ascii(tty=False, invert=True)
    print("If the phone can't connect, allow Python through your firewall")
    print("(Windows shows a prompt on first run — tick 'Private networks').")
    print()


_CACHE_LIMIT_BYTES = 2 * 1024 ** 3   # keep the preview cache under ~2 GB


def _prune_preview_cache(limit: int = _CACHE_LIMIT_BYTES) -> None:
    """Cached preview audio speeds up replays, but nothing ever deleted it, so
    UPLOAD_DIR grew without bound. Drop the oldest files once past the cap."""
    try:
        files = []
        for p in UPLOAD_DIR.glob("*"):
            if p.is_file():
                st = p.stat()
                files.append((st.st_mtime, st.st_size, p))
        total = sum(sz for _, sz, _ in files)
        if total <= limit:
            return
        files.sort()  # oldest first
        for _, size, p in files:
            if total <= limit:
                break
            try:
                p.unlink()
                total -= size
            except OSError:
                pass
    except OSError:
        pass


def _prune_jobs(keep: int = 20) -> None:
    """JOBS was never cleaned, so every conversion leaked a record for the life
    of the process. Drop the oldest finished ones."""
    excess = len(JOBS) - keep
    if excess <= 0:
        return
    for jid in [k for k, v in JOBS.items() if v.status in ("done", "error")][:excess]:
        JOBS.pop(jid, None)


_UPDATE_STAMP = Path(tempfile.gettempdir()) / "tubelube_ytdlp_update.txt"
_UPDATE_INTERVAL = 12 * 3600  # check at most twice a day


def _auto_update_ytdlp() -> None:
    """Keep yt-dlp current so YouTube's frequent changes don't break downloads.

    Runs in the background at startup and installs the latest yt-dlp into the
    *user* site-packages — the same place the app imports it from, and NO admin
    rights needed (admin is only required to touch the system Python folder,
    which we deliberately avoid). Throttled so it doesn't run pip every launch,
    and effective on the *next* launch (this process keeps the yt-dlp it already
    imported). Failures are swallowed — a stale yt-dlp still runs, and an update
    attempt must never block or crash startup.

    Disable with the env var TUBELUBE_NO_AUTOUPDATE=1.
    """
    if os.environ.get("TUBELUBE_NO_AUTOUPDATE") or getattr(sys, "frozen", False):
        return  # frozen .exe bundles yt-dlp; pip doesn't apply there
    try:
        import time
        now = time.time()
        if _UPDATE_STAMP.exists():
            try:
                if now - float(_UPDATE_STAMP.read_text().strip()) < _UPDATE_INTERVAL:
                    return
            except ValueError:
                pass
        # Claim this attempt with a short back-off first, so two copies starting
        # together don't both run pip. The full 12h stamp is only written after
        # a *successful* install — previously a failed or timed-out pip still
        # blocked any retry for 12 hours.
        _UPDATE_STAMP.write_text(str(now - _UPDATE_INTERVAL + 3600))
        cmd = [sys.executable, "-m", "pip", "install", "--upgrade", "--pre", "yt-dlp[default]"]
        if sys.prefix == sys.base_prefix:   # --user is an error inside a venv
            cmd.insert(4, "--user")
        # No timeout: killing pip mid-install could leave yt-dlp half-removed,
        # and the app would then fail to start. A slow update is harmless.
        r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if r.returncode == 0:
            _UPDATE_STAMP.write_text(str(time.time()))
    except Exception:
        pass


def run_server() -> None:
    """Run the Flask server (no browser). Used by the desktop launcher, which
    supplies its own native window. threaded=True so preview prefetch can run
    alongside the request that's playing."""
    threading.Thread(target=_auto_update_ytdlp, daemon=True).start()
    threading.Thread(target=_prune_preview_cache, daemon=True).start()
    try:
        import yt_dlp
        print(f"TubeLube — using yt-dlp {yt_dlp.version.__version__}")
    except Exception:
        pass
    app.run(host=APP_HOST, port=APP_PORT, debug=False, use_reloader=False, threaded=True)


def main() -> None:
    global APP_HOST, APP_PORT
    parser = argparse.ArgumentParser(description="TubeLube — local speed/pitch converter")
    parser.add_argument("--lan", action="store_true",
                        help="also accept connections from devices on your Wi-Fi/LAN "
                             "(e.g. your phone); prints the address + QR code to scan")
    parser.add_argument("--port", type=int, default=APP_PORT, help=f"port to listen on (default {APP_PORT})")
    args = parser.parse_args()
    APP_PORT = args.port
    if args.lan:
        APP_HOST = "0.0.0.0"
        _print_phone_banner()
    # Web mode: open the default browser shortly after the server starts.
    threading.Timer(1.0, _open_browser).start()
    run_server()


if __name__ == "__main__":
    main()
