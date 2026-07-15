"""Per-meeting phone sync — strictly opt-in, text only, never audio.

A meeting is uploaded to the user's own InsForge row (RLS: owner-only)
only after they toggle "View on phone" on that meeting. The row carries
title, times, speakers, turns (speaker/start/end/text) and the summary —
the WAVs never leave the Mac. Toggling off deletes the row.

Edits made after syncing (summary, tidy, renames, recluster) re-push
automatically via push_if_synced(). Failures land in an offline queue
(~/.meetingscribe/sync_queue.json) and drain on the next opportunity.
"""

import json
import logging
import threading
import time
from pathlib import Path

import insforge_client

log = logging.getLogger("meetingscribe.sync")

QUEUE_PATH = Path.home() / ".meetingscribe" / "sync_queue.json"
MAX_PAYLOAD_BYTES = 5 * 1024 * 1024  # a 2h meeting is ~400 KB; 5 MB is pathological

_queue_lock = threading.Lock()


class SyncError(RuntimeError):
    pass


def _payload(meta):
    turns = [
        {
            "speaker": t.get("speaker"),
            "start": t.get("start"),
            "end": t.get("end"),
            "text": t.get("text"),
        }
        for t in (meta.get("turns") or [])
    ]
    row = {
        "meeting_id": meta["id"],
        "title": meta.get("title") or "",
        "created": meta.get("created"),
        "duration": meta.get("duration"),
        "mode": meta.get("mode"),
        "speakers": meta.get("speakers") or {},
        "turns": turns,
        "summary": meta.get("summary"),
        "stats": meta.get("stats"),
    }
    return row


def push_meeting(meta):
    """Upsert one meeting's text to the user's row. Raises on failure."""
    state = insforge_client.state()
    if not state["signed_in"]:
        raise SyncError("Sign in first to view meetings on your phone.")
    row = _payload(meta)
    row["user_id"] = state["user_id"]
    body = json.dumps([row])
    if len(body.encode()) > MAX_PAYLOAD_BYTES:
        raise SyncError("This meeting's transcript is too large to sync.")
    status, data = insforge_client.db_request(
        "POST", "meetings?on_conflict=user_id,meeting_id",
        body=[row], prefer="resolution=merge-duplicates")
    if status not in (200, 201):
        raise SyncError(f"Sync failed ({(data or {}).get('message') or status}).")
    log.info("synced meeting %s (%d turns)", meta["id"], len(row["turns"]))


def delete_remote(meeting_id):
    """Remove a meeting's row (toggle off / local delete). Best effort."""
    try:
        status, data = insforge_client.db_request(
            "DELETE", f"meetings?meeting_id=eq.{meeting_id}")
        if status not in (200, 204):
            raise SyncError(f"Could not remove the synced copy ({status}).")
    except insforge_client.AuthError as exc:
        raise SyncError(str(exc)) from exc


# ------------------------------------------------------------ offline queue --

def _load_queue():
    try:
        return list(json.loads(QUEUE_PATH.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return []


def _save_queue(items):
    try:
        QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
        QUEUE_PATH.write_text(json.dumps(sorted(set(items))), encoding="utf-8")
    except OSError as exc:
        log.warning("could not persist sync queue: %s", exc)


def enqueue(meeting_id):
    with _queue_lock:
        items = _load_queue()
        if meeting_id not in items:
            items.append(meeting_id)
            _save_queue(items)


def drain(read_meeting):
    """Retry queued pushes. read_meeting(id) -> meta dict or None."""
    with _queue_lock:
        items = _load_queue()
        if not items:
            return
        remaining = []
        for meeting_id in items:
            try:
                meta = read_meeting(meeting_id)
                if meta is None or not (meta.get("sync") or {}).get("enabled"):
                    continue  # deleted or un-synced meanwhile — drop silently
                push_meeting(meta)
            except Exception as exc:
                log.info("queued sync for %s still failing: %s", meeting_id, exc)
                remaining.append(meeting_id)
        _save_queue(remaining)


def push_if_synced(read_meeting, write_meeting, meeting_id):
    """Background re-push after an edit; queues on failure. Never raises."""
    def run():
        try:
            meta = read_meeting(meeting_id)
            if meta is None or not (meta.get("sync") or {}).get("enabled"):
                return
            try:
                push_meeting(meta)
                meta = read_meeting(meeting_id)
                if meta is not None and (meta.get("sync") or {}).get("enabled"):
                    meta["sync"]["pushed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                    meta["sync"]["error"] = None
                    write_meeting(meta)
                drain(read_meeting)
            except Exception as exc:
                log.warning("sync push for %s failed: %s", meeting_id, exc)
                enqueue(meeting_id)
                try:
                    meta = read_meeting(meeting_id)
                    if meta is not None and (meta.get("sync") or {}).get("enabled"):
                        meta["sync"]["error"] = str(exc)
                        write_meeting(meta)
                except Exception:
                    pass
        except Exception as exc:  # absolutely never disturb the caller
            log.warning("push_if_synced(%s): %s", meeting_id, exc)

    threading.Thread(target=run, daemon=True, name=f"sync-{meeting_id}").start()
