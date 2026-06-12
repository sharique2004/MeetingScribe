"""Today's calendar events for auto-naming recordings (macOS, fully local).

Wraps the EventKit Swift helper in tools/calendar_events.swift. The compiled
binary is cached at ~/.meetingscribe/bin/calendar_events and rebuilt whenever
the source changes — same pattern as the Apple Speech transcriber.

macOS shows a one-time calendar-permission prompt the first time the helper
runs; the prompt is attributed to whichever app launched the server
(MeetingScribe.app declares the required usage description).
"""

import json
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from config import BASE_DIR

log = logging.getLogger("meetingscribe.calendar")

_SRC = BASE_DIR / "tools" / "calendar_events.swift"
_BIN = Path.home() / ".meetingscribe" / "bin" / "calendar_events"
_CACHE_TTL_S = 60
_HELPER_TIMEOUT_S = 150  # generous: the first run waits on the permission prompt

_cache = {"at": 0.0, "events": None, "error": None}


def _ensure_binary():
    if sys.platform != "darwin" or not _SRC.exists():
        return None
    if _BIN.exists() and _BIN.stat().st_mtime >= _SRC.stat().st_mtime:
        return str(_BIN)
    swiftc = shutil.which("swiftc") or "/usr/bin/swiftc"
    if not (shutil.which("swiftc") or os.path.exists("/usr/bin/swiftc")):
        return None
    try:
        _BIN.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [swiftc, "-O", str(_SRC), "-o", str(_BIN)],
            check=True, capture_output=True, text=True, timeout=300,
        )
        return str(_BIN)
    except (subprocess.SubprocessError, OSError) as exc:
        log.warning("could not build calendar helper: %s", exc)
        return None


def todays_events(force_refresh=False, cached_only=False):
    """Return {"available": bool, "events": [...], "error": str|None}.

    Events: {title, start, end, calendar, attendees, organizer} with epoch
    seconds. Cached for a minute — EventKit queries spawn a process.
    cached_only never spawns the helper; use it on latency-sensitive paths
    (the UI's periodic poll keeps the cache warm).
    """
    now = time.time()
    if not force_refresh and _cache["events"] is not None and now - _cache["at"] < _CACHE_TTL_S:
        return {"available": True, "events": _cache["events"], "error": None}
    # Errors get a much shorter TTL than successes: a denied-then-granted
    # permission or a transient helper failure should recover quickly.
    if not force_refresh and _cache["error"] is not None and now - _cache["at"] < 10:
        return {"available": False, "events": [], "error": _cache["error"]}
    if cached_only:
        return {"available": False, "events": [], "error": "no fresh calendar data"}

    binary = _ensure_binary()
    if binary is None:
        return {"available": False, "events": [], "error": "calendar helper unavailable"}
    try:
        proc = subprocess.run(
            [binary], capture_output=True, text=True, timeout=_HELPER_TIMEOUT_S
        )
    except subprocess.SubprocessError as exc:
        _cache.update(at=now, events=None, error=str(exc))
        return {"available": False, "events": [], "error": str(exc)}
    if proc.returncode != 0:
        error = (proc.stderr or "calendar helper failed").strip().splitlines()[-1]
        _cache.update(at=now, events=None, error=error)
        return {"available": False, "events": [], "error": error}
    try:
        events = json.loads(proc.stdout or "[]")
    except ValueError as exc:
        _cache.update(at=now, events=None, error=f"bad helper output: {exc}")
        return {"available": False, "events": [], "error": str(exc)}
    _cache.update(at=now, events=events, error=None)
    return {"available": True, "events": events, "error": None}


def current_event(lead_minutes=15, cached_only=False):
    """The event happening now (or starting within lead_minutes), if any.

    When events overlap, prefers the one that started most recently — that's
    the meeting the user is actually joining.
    """
    info = todays_events(cached_only=cached_only)
    now = time.time()
    candidates = [
        e for e in info["events"]
        if e["start"] - lead_minutes * 60 <= now < e["end"]
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda e: e["start"])
