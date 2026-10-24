"""Persistent playlists — named collections of saved YouTube songs, each song
carrying the user's preferred speed/pitch/format settings so a saved entry
replays *their* version (e.g. a 0.8x slowed edit).

Stored as a single JSON file beside the app:

    {
      "version": 2,
      "playlists": [
        {"id": "...", "name": "My Playset", "songs": [ {song}, ... ]}
      ],
      "active": "<playlist id>"
    }

Older single-list files ({"songs": [...]}) are migrated automatically into a
default playlist named "My Playset" on first read. Writes are lock-guarded and
atomic (temp file + replace) so a crash mid-write can't corrupt the store.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import paths

_STORE = paths.data_dir() / "playset.json"
_lock = threading.Lock()


class PlaysetUnavailable(Exception):
    """The store exists but couldn't be read right now (e.g. briefly locked)."""

_DEFAULT_NAME = "My Playset"


def _normalize(raw) -> dict:
    """Return a valid v2 dict, migrating the old {'songs': [...]} format and
    guaranteeing at least one playlist with a stable id. Does not write."""
    if not isinstance(raw, dict):
        raw = {}

    if "playlists" not in raw:
        # Migrate the old single-list format (or an empty/missing store).
        old_songs = raw.get("songs") if isinstance(raw.get("songs"), list) else []
        pid = uuid.uuid4().hex
        return {
            "version": 2,
            "playlists": [{"id": pid, "name": _DEFAULT_NAME, "songs": old_songs}],
            "active": pid,
        }

    pls = raw.get("playlists")
    if not isinstance(pls, list) or not pls:
        pid = uuid.uuid4().hex
        raw["playlists"] = [{"id": pid, "name": _DEFAULT_NAME, "songs": []}]
        raw["active"] = pid
    else:
        for p in pls:
            p.setdefault("id", uuid.uuid4().hex)
            p.setdefault("name", "Playlist")
            if not isinstance(p.get("songs"), list):
                p["songs"] = []

    ids = {p["id"] for p in raw["playlists"]}
    if raw.get("active") not in ids:
        raw["active"] = raw["playlists"][0]["id"]
    raw["version"] = 2
    return raw


def _read() -> dict:
    """Load + normalize the store. Persists if normalization changed the
    structure (e.g. migration / first run) so ids stay stable across reads.
    Caller must hold _lock."""
    # Any read failure used to fall back to an empty store and then *write* it,
    # silently wiping every playlist. Now the two failure kinds are separated.
    raw = None
    if _STORE.exists():
        for attempt in range(6):
            try:
                # utf-8-sig: tolerate a BOM from hand-editing in Notepad.
                with _STORE.open("r", encoding="utf-8-sig") as f:
                    raw = json.load(f)
                break
            except json.JSONDecodeError:
                # Genuinely damaged: keep it for recovery, then start fresh.
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                try:
                    _STORE.replace(_STORE.with_name(f"{_STORE.name}.corrupt-{stamp}"))
                except OSError:
                    raise PlaysetUnavailable("playset.json is damaged and couldn't be moved aside.")
                raw = None
                break
            except OSError:
                # Probably locked for a moment (antivirus, the other TubeLube
                # window). Retry, and never overwrite it if it stays locked.
                if attempt == 5:
                    raise PlaysetUnavailable("playset.json is locked by another program.")
                time.sleep(0.1)
    norm = _normalize(raw)
    if raw is not norm:   # migrated / created — persist for stable ids
        _write(norm)
    return norm


def _write(data: dict) -> None:
    # Unique temp name: a fixed one let the web app and the desktop window
    # (separate processes) clobber each other's half-written temp file.
    tmp = _STORE.with_name(f".{_STORE.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    for attempt in range(6):
        try:
            tmp.replace(_STORE)  # atomic on the same filesystem
            return
        except PermissionError:
            # Windows refuses the swap while another process has the file open.
            if attempt == 5:
                try:
                    tmp.unlink()
                except OSError:
                    pass
                raise PlaysetUnavailable("playset.json is locked by another program.")
            time.sleep(0.1)


def _flat_eq(eq: Optional[dict]) -> dict:
    """Normalise tone settings to {bass, mid, treble} floats (missing = 0)."""
    eq = eq or {}
    return {k: round(float(eq.get(k) or 0.0), 1) for k in ("bass", "mid", "treble")}


def _find(data: dict, pid: str) -> Optional[dict]:
    for p in data["playlists"]:
        if p["id"] == pid:
            return p
    return None


def _summary(p: dict) -> dict:
    return {"id": p["id"], "name": p["name"], "count": len(p["songs"])}


# ── Playlist-level operations ──────────────────────────────────────────────

def list_playlists() -> dict:
    """{'playlists': [{id, name, count}], 'active': id}."""
    with _lock:
        d = _read()
        return {"playlists": [_summary(p) for p in d["playlists"]],
                "active": d["active"]}


def get_playlist(pid: str) -> Optional[dict]:
    """Full playlist {id, name, songs} or None."""
    with _lock:
        d = _read()
        p = _find(d, pid)
        if not p:
            return None
        return {"id": p["id"], "name": p["name"], "songs": p["songs"]}


def create_playlist(name: str) -> dict:
    name = (name or "").strip() or "New playlist"
    with _lock:
        d = _read()
        p = {"id": uuid.uuid4().hex, "name": name, "songs": []}
        d["playlists"].append(p)
        d["active"] = p["id"]
        _write(d)
        return _summary(p)


def rename_playlist(pid: str, name: str) -> bool:
    name = (name or "").strip()
    if not name:
        return False
    with _lock:
        d = _read()
        p = _find(d, pid)
        if not p:
            return False
        p["name"] = name
        _write(d)
        return True


def delete_playlist(pid: str) -> bool:
    """Delete a playlist. Refuses to remove the last one (always keep ≥1)."""
    with _lock:
        d = _read()
        if len(d["playlists"]) <= 1 or not _find(d, pid):
            return False
        d["playlists"] = [p for p in d["playlists"] if p["id"] != pid]
        if d["active"] == pid:
            d["active"] = d["playlists"][0]["id"]
        _write(d)
        return True


def set_active(pid: str) -> bool:
    with _lock:
        d = _read()
        if not _find(d, pid):
            return False
        d["active"] = pid
        _write(d)
        return True


# ── Song-level operations (scoped to a playlist) ───────────────────────────

def add_song(
    pid: str,
    url: str,
    title: str,
    speed: float,
    preserve_pitch: bool,
    output_format: str,
    quality: str,
    eq: Optional[dict] = None,
) -> Optional[dict]:
    """Add a song to playlist `pid` (newest first). De-dupes on
    url+speed+pitch+tone — a different bass setting is a different "version",
    just like a different speed. Returns the entry, or None if the playlist
    doesn't exist. Songs saved before tone existed simply lack "eq" (= flat)."""
    entry = {
        "id": uuid.uuid4().hex,
        "url": url,
        "title": title,
        "speed": round(float(speed), 3),
        "preserve_pitch": bool(preserve_pitch),
        "format": output_format,
        "quality": quality,
        "eq": _flat_eq(eq),
        "added": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    with _lock:
        d = _read()
        p = _find(d, pid)
        if not p:
            return None
        for s in p["songs"]:
            if (s.get("url") == url
                    and s.get("speed") == entry["speed"]
                    and s.get("preserve_pitch") == entry["preserve_pitch"]
                    and _flat_eq(s.get("eq")) == entry["eq"]):
                return s
        p["songs"].insert(0, entry)
        _write(d)
        return entry


def remove_song(pid: str, song_id: str) -> bool:
    with _lock:
        d = _read()
        p = _find(d, pid)
        if not p:
            return False
        before = len(p["songs"])
        p["songs"] = [s for s in p["songs"] if s.get("id") != song_id]
        changed = len(p["songs"]) != before
        if changed:
            _write(d)
        return changed


def reorder(pid: str, order_ids: list[str]) -> Optional[list[dict]]:
    """Reorder playlist `pid`'s songs to match `order_ids`. Songs missing from
    the list are kept (appended) so a stale order can't drop songs. Returns the
    new ordered list, or None if the playlist doesn't exist."""
    with _lock:
        d = _read()
        p = _find(d, pid)
        if not p:
            return None
        by_id = {s.get("id"): s for s in p["songs"]}
        seen: set[str] = set()
        new_list: list[dict] = []
        for sid in order_ids:
            s = by_id.get(sid)
            if s is not None and sid not in seen:
                new_list.append(s)
                seen.add(sid)
        for s in p["songs"]:
            if s.get("id") not in seen:
                new_list.append(s)
        p["songs"] = new_list
        _write(d)
        return new_list
