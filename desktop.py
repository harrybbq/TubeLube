"""TubeLube desktop launcher.

Runs the same Flask app as the web version, but inside a native application
window (via pywebview / the system WebView2 runtime) instead of a browser tab.

    python desktop.py        # native window
    python app.py            # web version in your browser (unchanged)

The desktop app runs its OWN server on a free port (independent of the web
version on 5001), so the two never conflict and a stale process can't block it.
A boot log is written to %TEMP%\tubelube_desktop.log for diagnosis.
"""

from __future__ import annotations

import os
import socket
import sys
import tempfile
import threading
import time
import traceback
import urllib.request

# Under pythonw.exe (no console) stdout/stderr are None; libs that write to them
# would crash. Redirect to a null sink before importing anything that logs.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

_LOG = os.path.join(tempfile.gettempdir(), "tubelube_desktop.log")


def _log(msg: str) -> None:
    try:
        with open(_LOG, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')}  {msg}\n")
    except OSError:
        pass


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _http_ok(url: str, timeout: float) -> bool:
    end = time.time() + timeout
    last = None
    while time.time() < end:
        try:
            with urllib.request.urlopen(url, timeout=1) as r:
                if r.status == 200:
                    return True
        except Exception as e:
            last = e
            time.sleep(0.2)
    _log(f"http_ok timed out; last error: {last!r}")
    return False


def main() -> None:
    _log("=== boot ===")
    import app as tubelube
    _log("imported app")

    host = tubelube.APP_HOST
    port = _free_port()
    tubelube.APP_PORT = port
    url = f"http://{host}:{port}/"
    _log(f"chose url {url}")

    def _serve():
        try:
            tubelube.run_server()
        except Exception:
            _log("run_server crashed:\n" + traceback.format_exc())

    threading.Thread(target=_serve, daemon=True).start()
    _log("server thread started")

    if not _http_ok(url, timeout=25):
        raise SystemExit("server did not become ready")
    _log("server is ready (HTTP 200)")

    try:
        import webview
    except ImportError as e:
        raise SystemExit("pywebview not installed: pip install -r requirements.txt") from e
    _log(f"pywebview imported ({getattr(webview, '__version__', '?')})")

    storage = os.path.join(tempfile.gettempdir(), "tubelube_webview")
    os.makedirs(storage, exist_ok=True)

    webview.create_window("TubeLube", url, width=1280, height=820, min_size=(960, 640))
    _log("window created; starting GUI loop")
    webview.start(storage_path=storage)
    _log("GUI loop ended (window closed)")


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        _log(f"SystemExit: {e}")
        raise
    except Exception:
        _log("FATAL:\n" + traceback.format_exc())
        raise
