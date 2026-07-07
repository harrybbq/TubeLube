"""TubeLube — local Flask app for YouTube/local audio-video speed control.

Routes:
  GET  /                     UI (or install-prompt page if deps missing)
  POST /pick_folder          native folder picker, returns chosen path
  POST /upload               accepts a dropped/picked local file, returns temp path
  POST /convert              starts a background job, returns job_id
  GET  /status/<job_id>      progress + status + result path
  POST /open_folder          opens the OS file manager at a given folder
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import uuid
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from flask import Flask, jsonify, render_template, request, send_from_directory

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


def _default_output_dir() -> str:
    """User's Downloads folder, or home dir as a fallback."""
    candidate = Path.home() / "Downloads"
    if candidate.is_dir():
        return str(candidate)
    return str(Path.home())


@app.route("/")
def index():
    missing = _check_dependencies()
    if missing:
        return render_template("install_prompt.html", missing=missing), 200
    return render_template("index.html", default_output_dir=_default_output_dir())


@app.route("/pick_folder", methods=["POST"])
def pick_folder():
    """Open a native folder picker on the user's machine."""
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


@app.route("/preview_youtube", methods=["POST"])
def preview_youtube():
    data = request.get_json(force=True, silent=True) or {}
    url = (data.get("url") or "").strip()
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
    data = request.get_json(force=True, silent=True) or {}
    name = (data.get("name") or "").strip()
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
    data = request.get_json(force=True, silent=True) or {}
    url = (data.get("url") or "").strip()
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

    title = (data.get("title") or "").strip()
    if not title:
        # Best-effort title lookup; falls back to the URL inside get_title.
        title = downloader.get_title(url)

    entry = playset.add_song(pid, url, title, speed, preserve_pitch, output_format, quality)
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
    data = request.get_json(force=True, silent=True) or {}
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
        preserve_pitch = bool(params.get("preserve_pitch", True))
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
        elif local_path and os.path.isfile(local_path):
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
    data = request.get_json(force=True, silent=True) or {}

    # Validate speed early to give a friendly error.
    try:
        s = float(data.get("speed", 1.0))
    except (TypeError, ValueError):
        return jsonify({"error": "Speed must be a number."}), 400
    if not (converter.MIN_SPEED <= s <= converter.MAX_SPEED):
        return jsonify({"error": f"Speed must be between {converter.MIN_SPEED} and {converter.MAX_SPEED}."}), 400

    if data.get("output_format") not in ("mp3", "mp4"):
        return jsonify({"error": "Choose MP3 or MP4."}), 400

    out_dir = (data.get("output_dir") or "").strip()
    if not out_dir or not os.path.isdir(out_dir):
        return jsonify({"error": "Choose a valid output folder."}), 400

    source = (data.get("source") or "").strip()
    local_path = (data.get("local_path") or "").strip()
    if not source and not local_path:
        return jsonify({"error": "Drop a file or paste a YouTube URL."}), 400
    if source and not downloader.is_youtube_url(source) and not local_path:
        return jsonify({"error": "That URL doesn't look like a YouTube link."}), 400

    job = Job(id=uuid.uuid4().hex)
    JOBS[job.id] = job
    t = threading.Thread(target=_run_job, args=(job, data), daemon=True)
    t.start()
    return jsonify({"job_id": job.id})


@app.route("/status/<job_id>")
def status(job_id: str):
    job = JOBS.get(job_id)
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


@app.route("/open_folder", methods=["POST"])
def open_folder():
    data = request.get_json(force=True, silent=True) or {}
    path = data.get("path", "")
    if not path:
        return jsonify({"error": "No path."}), 400
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
    webbrowser.open(f"http://{APP_HOST}:{APP_PORT}/")


def run_server() -> None:
    """Run the Flask server (no browser). Used by the desktop launcher, which
    supplies its own native window. threaded=True so preview prefetch can run
    alongside the request that's playing."""
    app.run(host=APP_HOST, port=APP_PORT, debug=False, use_reloader=False, threaded=True)


def main() -> None:
    # Web mode: open the default browser shortly after the server starts.
    threading.Timer(1.0, _open_browser).start()
    run_server()


if __name__ == "__main__":
    main()
