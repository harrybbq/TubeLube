"""YouTube downloads via yt-dlp.

Returns a path to a single file on disk: either an mp3 (audio-only mode) or
the source video in its native container ready for ffmpeg speed processing.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Callable, Optional

# Imported lazily-tolerant: raising here (as before) crashed app.py at import,
# so the "yt-dlp is missing" install page could never actually be shown.
try:
    import yt_dlp
except ImportError:
    yt_dlp = None


def _require_ytdlp() -> None:
    if yt_dlp is None:
        raise DownloadError("yt-dlp is not installed. Run: pip install -r requirements.txt")


# Options shared by every yt-dlp call. noplaylist matters: a normal "Mix" link
# (watch?v=X&list=RD…) otherwise downloads the entire playlist, and the result
# isn't a single file, so conversion failed with "Input file not found".
_BASE_OPTS = {"noplaylist": True}


# Default cookie-DB locations per browser on Windows. The first one that
# exists wins. macOS/Linux paths could be added here later if needed.
# If a cookies file exists alongside the app, it's preferred over browser-cookie
# extraction. Netscape-format cookies.txt — exported via the "Get cookies.txt
# LOCALLY" browser extension. Most reliable workaround when Chrome's
# App-Bound Encryption blocks cookie-DB reads.
#
# Windows hides ".txt" by default, so users who save the file commonly end up
# with "cookies.txt.txt" — accept both names rather than silently ignoring it.
_COOKIES_CANDIDATES = [
    Path(__file__).parent / "cookies.txt",
    Path(__file__).parent / "cookies.txt.txt",
]


def _find_cookies_file() -> Optional[Path]:
    for p in _COOKIES_CANDIDATES:
        if p.exists():
            return p
    return None


_BROWSER_COOKIE_PATHS = [
    ("chrome",  r"%LOCALAPPDATA%\Google\Chrome\User Data\Default\Network\Cookies"),
    ("chrome",  r"%LOCALAPPDATA%\Google\Chrome\User Data\Default\Cookies"),
    ("edge",    r"%LOCALAPPDATA%\Microsoft\Edge\User Data\Default\Network\Cookies"),
    ("edge",    r"%LOCALAPPDATA%\Microsoft\Edge\User Data\Default\Cookies"),
    ("brave",   r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\User Data\Default\Network\Cookies"),
    ("firefox", r"%APPDATA%\Mozilla\Firefox\Profiles"),
]


def _candidate_browsers() -> list[str]:
    """Installed browsers whose cookie stores exist, in priority order.
    De-duped so a browser with multiple known paths is only tried once."""
    seen: set[str] = set()
    found: list[str] = []
    for name, raw in _BROWSER_COOKIE_PATHS:
        if name in seen:
            continue
        if os.path.exists(os.path.expandvars(raw)):
            seen.add(name)
            found.append(name)
    return found


def _is_cookie_lock_error(msg: str) -> bool:
    """True if the error is 'couldn't read the cookie DB' (browser is running /
    file locked / DPAPI couldn't decrypt), as opposed to anything else."""
    m = msg.lower()
    if "cookie" not in m:
        return False
    return any(k in m for k in (
        "could not copy", "could not read", "failed", "decrypt",
        "lock", "permission", "in use",
    ))


def _ydl_extract(opts: dict, url: str, download: bool = True) -> dict:
    """extract_info with browser-cookies first and a no-cookies fallback.

    YouTube has been rolling out a bot challenge that rejects unauthenticated
    requests. Passing the user's saved browser cookies makes the request look
    like a normal logged-in visit and bypasses the wall. Try each installed
    browser in turn; if every cookie-read fails (e.g. Chrome with App-Bound
    Encryption while running) AND Edge/Firefox aren't usable either, retry
    without cookies — that might still succeed for un-gated videos. If even
    that fails, surface a friendly error explaining how to fix it.
    """
    _require_ytdlp()
    opts = {**_BASE_OPTS, **opts}

    # Manual cookies.txt wins if present. If YouTube still bot-gates us with it,
    # it has most likely expired — fall through to browser cookies / no cookies
    # rather than failing outright, and say "re-export" if all of those fail too.
    cookies_file = _find_cookies_file()
    stale_cookiefile = False
    if cookies_file:
        try:
            return _extract_with_cookiefile(opts, url, download, cookies_file)
        except yt_dlp.utils.DownloadError as e:
            if not _is_bot_gate(str(e)):
                e._tubelube_cookiefile = True  # type: ignore[attr-defined]
                raise
            stale_cookiefile = True

    saw_cookie_lock = False
    for browser in _candidate_browsers():
        try:
            with yt_dlp.YoutubeDL({**opts, "cookiesfrombrowser": (browser,)}) as ydl:
                return ydl.extract_info(url, download=download)
        except yt_dlp.utils.DownloadError as e:
            if _is_cookie_lock_error(str(e)):
                saw_cookie_lock = True
                continue  # try the next browser
            raise
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=download)
    except yt_dlp.utils.DownloadError as e:
        # Annotate so _friendly_download_error can give the right hint.
        if saw_cookie_lock:
            e._tubelube_cookie_lock = True  # type: ignore[attr-defined]
        if stale_cookiefile:
            e._tubelube_stale_cookiefile = True  # type: ignore[attr-defined]
        raise


def _is_bot_gate(msg: str) -> bool:
    m = msg.lower()
    return "sign in to confirm" in m or "not a bot" in m or "confirm you" in m


def _extract_with_cookiefile(opts: dict, url: str, download: bool, cookies_file: Path) -> dict:
    """Run yt-dlp against a private COPY of cookies.txt. yt-dlp writes cookies
    back to `cookiefile` when it closes, so pointing it at the user's file let a
    preview and a conversion running together race to rewrite (and corrupt) it."""
    fd, tmp = tempfile.mkstemp(prefix="tubelube_cookies_", suffix=".txt")
    os.close(fd)
    try:
        shutil.copyfile(cookies_file, tmp)
        with yt_dlp.YoutubeDL({**opts, "cookiefile": tmp}) as ydl:
            return ydl.extract_info(url, download=download)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(s: str) -> str:
    """Remove terminal colour codes yt-dlp bakes into some error strings, so
    they don't show up as raw `[0;31m…` in the UI banner."""
    return _ANSI_RE.sub("", s)


def _friendly_download_error(e: Exception) -> "DownloadError":
    msg = _strip_ansi(str(e))
    lower = msg.lower()
    cookie_locked = getattr(e, "_tubelube_cookie_lock", False) or _is_cookie_lock_error(msg)
    bot_gated = ("sign in to confirm" in lower or "confirm you" in lower
                 or "not a bot" in lower)
    forbidden = "403" in lower and "forbidden" in lower
    uses_cookiefile = _find_cookies_file() is not None
    if bot_gated and (getattr(e, "_tubelube_stale_cookiefile", False) or uses_cookiefile):
        return DownloadError(
            "YouTube wants you signed in, and your cookies.txt looks expired. "
            "Re-export it: open youtube.com signed in, use the 'Get cookies.txt "
            "LOCALLY' extension, and replace the cookies.txt in the TubeLube folder."
        )
    if cookie_locked and bot_gated:
        return DownloadError(
            "YouTube wants cookies and your browser is holding them. "
            "Close Chrome / Edge completely (check the system tray, "
            "right-click → Quit), then try again."
        )
    if cookie_locked:
        return DownloadError(
            "TubeLube couldn't read your browser's cookies because the browser "
            "is running. Close Chrome / Edge completely (check the system tray) "
            "and try again."
        )
    if bot_gated:
        return DownloadError(
            "YouTube wants authentication for this video. Close your browser "
            "(Chrome / Edge) completely so TubeLube can pass your saved cookies "
            "through, then try again."
        )
    if forbidden:
        cookie_tip = (" If that doesn't help, re-export your cookies.txt."
                      if uses_cookiefile else "")
        return DownloadError(
            "YouTube refused the download (HTTP 403) even after trying several "
            "players. This usually means yt-dlp is out of date — update it with:  "
            "python -m pip install -U --pre \"yt-dlp[default]\"  — then try again."
            + cookie_tip
        )
    return DownloadError(msg)


# Player clients to try for downloading, in order — tried ONE AT A TIME so a
# 403 (or other recoverable failure) on one client's media URL moves cleanly to
# the next. This matters because passing several clients at once makes yt-dlp
# pick the single best format across all of them; if that one URL 403s it never
# falls through to another client's copy. YouTube keeps shifting which clients
# hand back downloadable URLs — `android` and `mweb` currently download reliably
# (full adaptive audio, no PO token); the rest are fallbacks for other videos.
_CLIENTS = ["android", "mweb", "tv", "web_safari", "web"]


def _is_format_unavailable(msg: str) -> bool:
    m = msg.lower()
    return ("requested format is not available" in m
            or "only images are available" in m
            or "no video formats" in m)


def _should_try_next_client(msg: str) -> bool:
    """Whether a client's failure is worth retrying with the next client rather
    than surfacing to the user: a 403 on the media URL, no usable formats, or
    YouTube's transient 'page needs to be reloaded'."""
    m = msg.lower()
    return (("403" in m and "forbidden" in m)
            or _is_format_unavailable(msg)
            or "page needs to be reloaded" in m
            or "the page needs" in m)


def _download_with_client_fallback(base_opts: dict, url: str) -> str:
    """Try each client in _CLIENTS on its own until one extracts AND downloads.
    Returns the downloaded file path. `base_opts` must NOT include
    extractor_args — it's set per client here."""
    last: Optional[Exception] = None
    for client in _CLIENTS:
        opts = {**base_opts,
                "extractor_args": {"youtube": {"player_client": [client]}}}
        try:
            info = _ydl_extract(opts, url)
            if info.get("requested_downloads"):
                return info["requested_downloads"][0]["filepath"]
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.prepare_filename(info)
        except yt_dlp.utils.DownloadError as e:
            last = e
            if _should_try_next_client(str(e)):
                continue  # this client's URL failed — try the next one
            raise _friendly_download_error(e) from e
    raise _friendly_download_error(last)


class DownloadError(Exception):
    pass


_URL_RE = re.compile(
    r"^(https?://)?(www\.)?(youtube\.com|youtu\.be|m\.youtube\.com)/",
    re.IGNORECASE,
)


def is_youtube_url(text: str) -> bool:
    return bool(_URL_RE.match((text or "").strip()))


# 11-char YouTube video id from the common URL shapes.
_VID_RE = re.compile(r"(?:v=|/shorts/|youtu\.be/|/embed/|/v/)([0-9A-Za-z_-]{11})")


def _extract_video_id(url: str) -> Optional[str]:
    m = _VID_RE.search(url or "")
    return m.group(1) if m else None


def _find_cached_preview(work_dir: Path, video_id: str) -> Optional[Path]:
    """Return a previously-downloaded, complete preview file for this video id,
    or None. Lets us skip the whole yt-dlp round trip on replays."""
    for p in work_dir.glob(f"preview_{video_id}.*"):
        if p.suffix.lower() in (".part", ".ytdl", ".tmp"):
            continue  # interrupted/partial download — ignore
        if p.is_file() and p.stat().st_size > 0:
            return p
    return None


def get_title(url: str) -> str:
    """Fetch a video's title without downloading the media. Used to give saved
    playset entries a human-readable name. Falls back to the URL on any error
    so saving never hard-fails just because the title lookup didn't work."""
    if not is_youtube_url(url):
        raise DownloadError("That doesn't look like a YouTube URL.")
    if yt_dlp is None:
        return url
    # Try the same clients the downloader uses, in order, until one resolves a
    # title. Hardcoding a client list here was a bug: when `tv`/`web` broke,
    # every lookup threw and songs got saved named after their URL.
    for client in _CLIENTS:
        opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "extractor_args": {"youtube": {"player_client": [client]}},
        }
        try:
            info = _ydl_extract(opts, url, download=False)
            title = (info or {}).get("title")
            if title:
                return title
        except Exception:
            continue
    return url


def _hook_factory(on_progress: Optional[Callable[[float, str], None]]):
    def hook(d: dict) -> None:
        if not on_progress:
            return
        status = d.get("status", "")
        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes") or 0
            pct = (downloaded / total * 100.0) if total else 0.0
            on_progress(pct, "Downloading")
        elif status == "finished":
            on_progress(100.0, "Download complete")
    return hook


def download(
    url: str,
    output_format: str,         # "mp3" or "mp4"
    quality: str,               # mp3: "192" or "320"; mp4: "720", "1080", "best"
    work_dir: str,
    on_progress: Optional[Callable[[float, str], None]] = None,
) -> str:
    """Download a YouTube URL into work_dir. Returns the resulting file path."""
    _require_ytdlp()
    if not is_youtube_url(url):
        raise DownloadError("That doesn't look like a YouTube URL.")
    if output_format not in ("mp3", "mp4"):
        raise DownloadError("Output format must be 'mp3' or 'mp4'.")

    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)
    # %(id)s keeps the filename safe; we rename based on title at convert time.
    outtmpl = str(work / "%(title).100B [%(id)s].%(ext)s")

    opts: dict = {
        "outtmpl": outtmpl,
        "noprogress": True,
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": [_hook_factory(on_progress)],
        "restrictfilenames": False,
        "windowsfilenames": True,
    }

    if output_format == "mp3":
        # Grab the raw bestaudio stream (typically opus/webm or m4a) and skip
        # yt-dlp's mp3 extraction postprocessor. converter.py applies the speed
        # filter and encodes to mp3 in a single pass — avoiding the double
        # lossy mp3 encode we'd get if yt-dlp extracted to mp3 first.
        opts["format"] = "bestaudio/best"
    else:  # mp4
        if quality == "best":
            fmt = "bv*+ba/b"
        else:
            # 720 or 1080 — cap height, prefer mp4 where possible
            h = "720" if quality == "720" else "1080"
            fmt = f"bv*[height<={h}][ext=mp4]+ba[ext=m4a]/bv*[height<={h}]+ba/b[height<={h}]"
        opts["format"] = fmt
        opts["merge_output_format"] = "mp4"

    # Tries the fast client set, broadening to more clients on a format error.
    return _download_with_client_fallback(opts, url)


def download_audio_preview(
    url: str,
    work_dir: str,
    on_progress: Optional[Callable[[float, str], None]] = None,
) -> str:
    """Fast audio-only fetch for the in-browser preview player.

    Skips ffmpeg post-processing — the raw bestaudio stream (typically webm/opus
    or m4a) plays directly in modern browsers, so we save the conversion time.
    """
    _require_ytdlp()
    if not is_youtube_url(url):
        raise DownloadError("That doesn't look like a YouTube URL.")
    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)

    vid = _extract_video_id(url)
    if not vid:
        return _fetch_preview(url, work, on_progress)
    # One fetch per video at a time. Pressing play while that same song was
    # being prefetched used to run two downloads into the same file, which
    # could corrupt the cache. The second caller now waits, then hits the cache.
    with _preview_lock(vid):
        # Cache hit: serve instantly and skip yt-dlp entirely (the slow part).
        cached = _find_cached_preview(work, vid)
        if cached:
            if on_progress:
                on_progress(100.0, "Cached")
            return str(cached)
        return _fetch_preview(url, work, on_progress)


_preview_locks: dict[str, threading.Lock] = {}
_preview_locks_guard = threading.Lock()


def _preview_lock(video_id: str) -> threading.Lock:
    with _preview_locks_guard:
        return _preview_locks.setdefault(video_id, threading.Lock())


def _fetch_preview(url: str, work: Path,
                   on_progress: Optional[Callable[[float, str], None]]) -> str:
    outtmpl = str(work / "preview_%(id)s.%(ext)s")
    opts = {
        "outtmpl": outtmpl,
        "format": "bestaudio/best",
        "noprogress": True,
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": [_hook_factory(on_progress)],
        "windowsfilenames": True,
    }
    # Fast clients first; broaden automatically if this video has no usable
    # audio on them (the cause of "Requested format is not available").
    return _download_with_client_fallback(opts, url)


# TODO v2: playlist support (batch).
# TODO v2: subtitles/auto-captions extraction.
