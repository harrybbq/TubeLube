"""Where TubeLube keeps the user's data (playset.json, cookies.txt).

Running from source: the project folder — unchanged, so an existing
playset.json and cookies.txt keep working.

Running as the one-file .exe: modules' __file__ points into PyInstaller's
temporary unpack folder (_MEIxxxx), which is deleted when the app exits. Saving
there made playlists vanish on every launch and cookies.txt impossible to find,
so the frozen build uses %APPDATA%\\TubeLube instead.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def app_dir() -> Path:
    """The folder the user sees: next to TubeLube.exe, or the source folder."""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def data_dir() -> Path:
    """Persistent per-user data folder (created if missing)."""
    if is_frozen():
        base = os.environ.get("APPDATA") or str(Path.home())
        d = Path(base) / "TubeLube"
    else:
        d = Path(__file__).resolve().parent
    d.mkdir(parents=True, exist_ok=True)
    return d
