"""YouTube downloads via yt-dlp.

Returns a path to a single file on disk: either an mp3 (audio-only mode) or
the source video in its native container ready for ffmpeg speed processing.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Callable, Optional

try:
    import yt_dlp
except ImportError as e:
    raise ImportError("yt-dlp is not installed. Run: pip install -r requirements.txt") from e


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
    # Manual cookies.txt wins if present — no browser dance, always works.
    cookies_file = _find_cookies_file()
    if cookies_file:
        with yt_dlp.YoutubeDL({**opts, "cookiefile": str(cookies_file)}) as ydl:
            return ydl.extract_info(url, download=download)

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
        raise


def _friendly_download_error(e: Exception) -> "DownloadError":
    msg = str(e)
    lower = msg.lower()
    cookie_locked = getattr(e, "_tubelube_cookie_lock", False) or _is_cookie_lock_error(msg)
    bot_gated = ("sign in to confirm" in lower or "confirm you" in lower
                 or "not a bot" in lower)
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
    return DownloadError(msg)


# Player clients to try, fast first. Most videos resolve on the lean pair
# quickly. Some videos expose no usable streams there — e.g. YouTube's DRM
# experiment locks the `tv` client's adaptive formats, and `web` formats may
# need a PO token — so we retry with a broader set. `web_safari` and `mweb`
# reliably surface the progressive 360p mp4 (itag 18) as a last-resort source
# with bundled audio, which is enough to play/convert. (web_music is omitted:
# its formats are PO-token-gated, so it only adds a wasted round trip.)
_CLIENTS_FAST = ["tv", "web"]
_CLIENTS_FULL = ["tv", "web_safari", "mweb", "web"]


def _is_format_unavailable(msg: str) -> bool:
    m = msg.lower()
    return ("requested format is not available" in m
            or "only images are available" in m
            or "no video formats" in m)


def _download_with_client_fallback(base_opts: dict, url: str) -> str:
    """Download trying _CLIENTS_FAST first; on a 'no usable formats' error,
    retry once with _CLIENTS_FULL. Returns the downloaded file path.
    `base_opts` must NOT include extractor_args — it's set here per attempt."""
    last: Optional[Exception] = None
    for clients in (_CLIENTS_FAST, _CLIENTS_FULL):
        opts = {**base_opts,
                "extractor_args": {"youtube": {"player_client": clients}}}
        try:
            info = _ydl_extract(opts, url)
            if info.get("requested_downloads"):
                return info["requested_downloads"][0]["filepath"]
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.prepare_filename(info)
        except yt_dlp.utils.DownloadError as e:
            last = e
            if clients is _CLIENTS_FAST and _is_format_unavailable(str(e)):
                continue  # broaden the client set and try once more
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
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        # Title is available without solving signature/format challenges, so a
        # lightweight client list is fine and fast.
        "extractor_args": {"youtube": {"player_client": ["tv", "web"]}},
    }
    try:
        info = _ydl_extract(opts, url, download=False)
        return info.get("title") or url
    except Exception:
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
    if not is_youtube_url(url):
        raise DownloadError("That doesn't look like a YouTube URL.")
    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)

    # Cache hit: if we've already fetched this video's audio, serve it instantly
    # and skip yt-dlp entirely (the slow part). Huge win for replays and
    # play-through, where the same songs get loaded repeatedly.
    vid = _extract_video_id(url)
    if vid:
        cached = _find_cached_preview(work, vid)
        if cached:
            if on_progress:
                on_progress(100.0, "Cached")
            return str(cached)

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
