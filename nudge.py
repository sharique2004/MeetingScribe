"""Meeting nudges — "you seem to be in a meeting; record it?"

Two signals, all decided locally:

1. CALENDAR: a calendar event is starting (within a minute) or already
   running and MeetingScribe is not recording. One nudge per event, ever.
2. CALL DETECTION: some app has been holding the microphone for a while
   (Zoom, Teams, a Meet tab — anything). At most one nudge per hour, and
   the mic must stay busy across polls (>= CALL_PERSIST_S) so short blips
   like voice memos or Siri don't trigger it.

The native app polls GET /api/nudges and turns results into notifications;
"Record now" hits /accept (starts the recording), "Not this meeting" hits
/ack (never ask again for this one). Debounce state lives outside the
project folder in ~/.meetingscribe/nudges.json.
"""

import hashlib
import json
import logging
import subprocess
import time
from pathlib import Path

log = logging.getLogger("meetingscribe.nudge")

STATE_PATH = Path.home() / ".meetingscribe" / "nudges.json"
CALL_PERSIST_S = 10       # mic must stay busy this long before we ask
CALL_COOLDOWN_S = 3600    # at most one call nudge per hour
CAL_LEAD_MIN = 1          # nudge when an event starts within a minute
PENDING_TTL_S = 600       # un-actioned nudges expire after 10 minutes

# Meeting apps we can name in the nudge (process name -> label). Browser
# calls (Meet etc.) can't be named without an extension — they still
# trigger via the mic signal, just with generic wording.
_MEETING_APPS = {
    "zoom.us": "Zoom",
    "Microsoft Teams": "Teams",
    "MSTeams": "Teams",
    "Slack": "Slack",
    "FaceTime": "FaceTime",
    "Webex": "Webex",
}


def _default_calendar_event():
    try:
        import calendar_events
        return calendar_events.current_event(lead_minutes=CAL_LEAD_MIN)
    except Exception:
        return None


def _default_mic_in_use():
    try:
        import macos_audio
        return macos_audio.mic_in_use()
    except Exception:
        return False


def _default_meeting_app():
    for proc, label in _MEETING_APPS.items():
        try:
            if subprocess.run(["pgrep", "-x", proc], capture_output=True).returncode == 0:
                return label
        except OSError:
            return None
    return None


class NudgeEngine:
    """Evaluates the signals; remembers what was already asked.

    Signal providers are injectable so the decision table is testable
    with fake clocks and fake calendars.
    """

    def __init__(self, calendar_event=None, mic_in_use=None, meeting_app=None,
                 now=None, state_path=STATE_PATH):
        self._calendar_event = calendar_event or _default_calendar_event
        self._mic_in_use = mic_in_use or _default_mic_in_use
        self._meeting_app = meeting_app or _default_meeting_app
        self._now = now or time.time
        self._state_path = Path(state_path)
        self._state = self._load()
        self._pending = None          # the currently offered nudge dict
        self._pending_at = 0.0
        self._mic_first_seen = None

    # ------------------------------------------------------------- state --

    def _load(self):
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
            return {
                "done": dict(data.get("done") or {}),
                "last_call": float(data.get("last_call") or 0.0),
            }
        except (OSError, ValueError):
            return {"done": {}, "last_call": 0.0}

    def _save(self):
        try:
            done = self._state["done"]
            if len(done) > 200:  # keep the newest entries only
                keep = sorted(done, key=done.get, reverse=True)[:200]
                self._state["done"] = {k: done[k] for k in keep}
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(json.dumps(self._state), encoding="utf-8")
        except OSError as exc:
            log.warning("could not persist nudge state: %s", exc)

    # --------------------------------------------------------- evaluation --

    def evaluate(self, recording):
        """-> the nudge to (keep) showing, or None. Stable until actioned."""
        now = self._now()
        if recording:
            self._mic_first_seen = None
            self._pending = None
            return None
        if self._pending is not None:
            if now - self._pending_at <= PENDING_TTL_S:
                return self._pending
            self._pending = None  # expired unanswered — allow re-evaluation

        nudge = self._calendar_nudge(now) or self._call_nudge(now)
        if nudge is not None:
            self._pending = nudge
            self._pending_at = now
        return nudge

    def _calendar_nudge(self, now):
        event = self._calendar_event()
        if not event:
            return None
        key = "cal-" + hashlib.sha1(
            f"{event.get('title')}|{event.get('start')}".encode()).hexdigest()[:12]
        if key in self._state["done"]:
            return None
        return {
            "id": key,
            "kind": "calendar",
            "title": f"“{event.get('title', 'Your meeting')}” is starting",
            "body": "Join and start recording?",
            "meeting_title": event.get("title") or "",
        }

    def _call_nudge(self, now):
        if not self._mic_in_use():
            self._mic_first_seen = None
            return None
        if self._mic_first_seen is None:
            self._mic_first_seen = now
        if now - self._mic_first_seen < CALL_PERSIST_S:
            return None
        if now - self._state["last_call"] < CALL_COOLDOWN_S:
            return None
        app = self._meeting_app()
        label = f"In a {app} call?" if app else "In a call?"
        return {
            "id": f"call-{int(self._mic_first_seen)}",
            "kind": "call",
            "title": label,
            "body": "Something is using the microphone. Record this meeting?",
            "meeting_title": "",
        }

    # ------------------------------------------------------------ actions --

    def take(self, nudge_id):
        """Consume the pending nudge (accept path). -> the nudge or None."""
        nudge = self._resolve(nudge_id)
        if nudge is not None:
            self._finish(nudge)
        return nudge

    def ack(self, nudge_id):
        """Dismiss ('not this meeting'). -> True if it was known."""
        nudge = self._resolve(nudge_id)
        if nudge is not None:
            self._finish(nudge)
        return nudge is not None

    def _resolve(self, nudge_id):
        if self._pending is not None and self._pending["id"] == nudge_id:
            return self._pending
        return None

    def _finish(self, nudge):
        now = self._now()
        self._state["done"][nudge["id"]] = now
        if nudge["kind"] == "call":
            self._state["last_call"] = now
        self._save()
        self._pending = None
        self._mic_first_seen = None
