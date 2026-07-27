"""Per-meeting phone sync — strictly opt-in, text only, never audio.

A meeting is uploaded to the user's own InsForge row (RLS: owner-only)
only after they toggle "View on phone" on that meeting. The row carries
title, times, speakers, turns (speaker/start/end/text), the speaking
stats and the summary — the WAVs never leave the Mac. Toggling off
deletes the row.

WHAT THIS DOES AND DOES NOT DO WITH THE USER'S PRIVATE WRITING
--------------------------------------------------------------
Read this before changing _payload, and do not shorten it into a promise
it does not keep.

NOT UPLOADED, and structurally so: meta["notes"] (what the user typed on a
private panel during the meeting) and meta["cues"] (points they wrote for
themselves before it). _payload builds the row from a fixed list of keys,
so those arrays are absent by construction rather than by being stripped,
and no note or cue reaches the row IN THE USER'S OWN WORDS — verified by
the key list below and by SUMMARY_UPLOAD_KEYS.

BUT THEIR SUBSTANCE DOES TRAVEL, inside the summary, BY DESIGN. This is
the part an earlier version of this docstring got wrong, and it is the
part that matters. summarize.py's NOTES_GUIDANCE instructs the model, in
so many words, that a task/decision/name/deadline the user wrote down
"MUST survive into the summary even when nobody said it out loud", and
that "EVERY uncovered cue must appear in the summary — in open_questions
if it is a question, in follow_ups if it is something to do". So for a
meeting with notes, the uploaded summary is expected to contain what the
user privately wrote, restated by the summarizer:

    note  "I said I'd send the revised deck by Friday"
      ->  action_items: [{"owner": "You", "task": "Send the revised deck",
                          "due": "Friday"}]          <- uploaded
    cue   "ask why the last hire left"
      ->  open_questions: ["Why did the last hire leave?"]  <- uploaded

That is not a leak to be patched — it IS the summary the user asked for,
and stripping it would mean shipping the phone a summary that omits the
commitments its owner cared enough to type. The honest statement of the
boundary is therefore:

    nothing the user typed leaves this Mac verbatim;
    what the summarizer made of it does, because that is the summary.

Anyone who wants the phone copy to be free of note-derived content wants
a different summary, not a different filter here — that is a product
decision, not something _payload can do without deleting content.

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


# The summary keys that go on the wire. An ALLOWLIST, not a blocklist, and
# that direction is the whole point: summarize.py owns meta["summary"] and can
# grow a key at any time, and a blocklist would upload each new one by default
# and only stop once somebody noticed. "unaddressed_cues" is what that hazard
# looks like: a key added to the summary purely to draw tick marks on the Mac,
# which a blocklist keeps off the wire only for as long as someone remembers it
# is there. Listed here means somebody chose to publish it; anything unlisted
# stays on this Mac until they do.
#
# This is today's summary shape exactly (summarize._coerce's fields, plus
# "engine" and "notes_omitted"), so it uploads byte-for-byte what it uploaded
# before, minus the one key below.
SUMMARY_UPLOAD_KEYS = (
    # summarize._coerce's fields, in the order it writes them...
    "headline", "tldr", "key_points", "decisions", "action_items",
    "follow_ups", "open_questions", "follow_up_email",
    # ...then the two the engines add afterwards.
    "engine",
    # A count of the notes that did not fit the summary prompt — a number, no
    # note text. It travels because a caveat about how a summary was built is
    # worth least where the summary is not; see summarize._mark_notes_omitted.
    "notes_omitted",
)

# Withheld on purpose, and already known about — so no warning is logged for
# it. summarize.py stores "unaddressed_cues" as the VERBATIM text of the points
# the user prepared and never got to raise ("push back on their pricing"). Cues
# are written before the meeting, for the user's own eyes, and were never said
# out loud to anybody; the field exists only to draw tick marks in the Mac
# review UI (templates/index.html matches it against meta["cues"]) and no phone
# client reads it. Excluding it keeps the user's own words off the wire and
# changes nothing anyone can see. Default-exclude, no toggle — a switch that
# offers to publish private text in exchange for nothing is not a choice worth
# putting in front of someone.
#
# NOTE what this does NOT claim: the same cue, in the summarizer's words, is
# still expected in open_questions and is still uploaded. See the module
# docstring — this closes the verbatim channel, not the summary.
SUMMARY_LOCAL_ONLY = ("unaddressed_cues",)


def _summary_for_upload(summary):
    """The summary reduced to the keys that may leave the Mac.

    A copy, never an edit: the caller's meta is the live meeting dict, and
    dropping a key from it here would delete the cue verdict from meeting.json
    the next time anything wrote that dict back.

    A key that is neither uploaded nor on the known-withheld list is a key
    summarize.py grew since this list was written. Withholding it is the safe
    default, but doing that silently is how a stale allowlist quietly stops
    syncing something the phone needs — so it is logged, once per push, with
    the name to add here if it should travel.

    Filtered in the SUMMARY's key order, not the allowlist's, so the row a
    given summary produces is byte-for-byte what it was before this filter
    existed, less the withheld keys.
    """
    if not isinstance(summary, dict):
        return summary
    unknown = [k for k in summary
               if k not in SUMMARY_UPLOAD_KEYS and k not in SUMMARY_LOCAL_ONLY]
    if unknown:
        log.warning("summary key(s) %s are not in SUMMARY_UPLOAD_KEYS and were "
                    "kept on this Mac — add them there if the phone should see "
                    "them", ", ".join(sorted(unknown)))
    return {k: v for k, v in summary.items() if k in SUMMARY_UPLOAD_KEYS}


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
        "summary": _summary_for_upload(meta.get("summary")),
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


_draining = threading.Lock()


def drain(read_meeting):
    """Retry queued pushes. read_meeting(id) -> meta dict or None.

    The _queue_lock is only held briefly to snapshot/rewrite the queue, never
    across the network pushes — so a concurrent enqueue() from a request
    handler can't block for the duration of the network calls. _draining
    ensures only one drain runs at a time.
    """
    if not _draining.acquire(blocking=False):
        return  # a drain is already in progress
    try:
        with _queue_lock:
            items = _load_queue()
        if not items:
            return
        failed = []
        for meeting_id in items:
            try:
                meta = read_meeting(meeting_id)
                if meta is None or not (meta.get("sync") or {}).get("enabled"):
                    continue  # deleted or un-synced meanwhile — drop silently
                push_meeting(meta)
            except Exception as exc:
                log.info("queued sync for %s still failing: %s", meeting_id, exc)
                failed.append(meeting_id)
        # Re-merge failures with anything enqueued while we were pushing.
        with _queue_lock:
            current = set(_load_queue())
            still_pending = (current - set(items)) | set(failed)
            _save_queue(list(still_pending))
    finally:
        _draining.release()


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
