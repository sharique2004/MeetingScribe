"""MeetingScribe — local meeting recorder, transcriber and conversation coach.

Run:  venv\\Scripts\\python.exe app.py   (or double-click run.bat)
"""

import atexit
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import webbrowser
from datetime import datetime

from flask import Flask, abort, jsonify, redirect, request, send_from_directory

import ai_cli
import ask
import audio_archive
import diarization
import insforge_client
import live_captions
import local_llm
import notes as notes_store
import pipeline
import summarize
import sync
import tech_vocabulary
import tidy
import voice_profiles
from audio_recorder import MeetingRecorder
from config import (BASE_DIR, ECAPA_ONNX_PATH, MODELS_DIR, RECORDINGS_DIR,
                    ModelDownloadError, human_bytes, load_config,
                    seed_ecapa_onnx)

try:  # macOS only - raises ImportError elsewhere
    import macos_audio
except Exception:
    macos_audio = None

try:  # calendar auto-naming (macOS EventKit helper)
    import calendar_events
except Exception:
    calendar_events = None

app = Flask(__name__, static_folder=str(BASE_DIR / "static"), static_url_path="/static")
app.json.sort_keys = False  # keep speaker/stats ordering ("You" first, then Speaker 1…)

REC = MeetingRecorder()
JOBS = {}  # meeting_id -> {"state": queued|processing|done|error, "message": str}
SUMMARY_JOBS = {}  # meeting_id -> {"state": processing|done|error, "message": str}
# Recluster runs inside the request, but it rewrites analysis.npz, so it still
# needs a claim: an entry lives here only for the duration of one call.
RECLUSTER_JOBS = {}  # meeting_id -> {"state": "processing", "message": str}
# FLAC archival (audio_archive.py). An entry lives here only while one
# meeting's tracks are being compressed; it exists so delete/reprocess/
# recluster/rename stand aside for the encoder exactly as they do for each
# other — and vice versa, via the blockers in _compress_one.
COMPRESS_JOBS = {}  # meeting_id -> {"state": "processing", "message": str}
# One ask at a time per meeting. Kept deliberately small — /api/status ships
# every entry to every poller — so the live answer text lives in _ASK_LIVE
# (below, by the ask routes) and the finished exchange in the meeting's
# qa.json, never here.
ASK_JOBS = {}  # meeting_id -> {"state": processing|done|error, "message": str,
#                               "question": str, "qa_id": str}
RECORD_LOCK = threading.Lock()  # serializes start/stop transitions across requests
JOB_LOCK = threading.Lock()  # makes "check job state then register" atomic
SYNC_ALL = {}  # progress of a "sync all to phone" run
LIVE = None  # live_captions.LiveSession for the current/last recording
# The recording that stopped most recently: {"id": str, "at": monotonic float}.
# A note the panel was still holding when the user pressed Stop has to land in
# the meeting they were in, and by the time it arrives the recorder is idle and
# no longer knows which one that was. Replaced wholesale, never mutated, so a
# reader without RECORD_LOCK sees one consistent dict or the other.
LAST_RECORDING = None
# How long after a stop a note with no meeting_id is still filed under that
# meeting. Long enough for the panel to flush a field the user was typing in
# (and to retry a failed send); short enough that a note typed into a stale
# panel much later is refused and left with the user rather than filed under a
# meeting they have stopped thinking about. A client that knows the id sends it
# and is not subject to this at all — see record_note.
NOTE_GRACE_SECONDS = 600

# One wording for "a recluster owns this meeting right now", used by every
# route that has to stand aside for one. It was copied out five times and a
# sixth caller is being added below; a single name keeps them from drifting.
RECLUSTER_BUSY = "Speaker count is being changed — try again in a moment"


def _claim_job(jobs, meeting_id, message, *, blockers=(), busy="Already in progress"):
    """Atomically register a background job if none conflicts.

    Returns None on success, or an (error, status) tuple to return. `blockers`
    are (dict, label) pairs whose 'processing' state blocks this job — checked
    under the same lock as the claim so two requests can't both slip through.
    `busy` is the message when `jobs` itself already holds this meeting; it is
    only worth overriding where the route already had a better one to keep.
    """
    with JOB_LOCK:
        if jobs.get(meeting_id, {}).get("state") == "processing":
            return ({"error": busy}, 409)
        for d, label in blockers:
            if d.get(meeting_id, {}).get("state") == "processing":
                return ({"error": label}, 409)
        jobs[meeting_id] = {"state": "processing", "message": message}
    return None

try:  # meeting nudges (macOS signals; harmless to lack elsewhere)
    import nudge
    NUDGES = nudge.NudgeEngine()
except Exception as _exc:
    app.logger.warning("nudges unavailable: %s", _exc)
    NUDGES = None

MEETING_ID_RE = re.compile(r"^\d{8}-\d{6}$")
# Folders are "<title> — <id>" so meetings are spottable in Finder, or a bare
# id before a title exists. The id suffix keeps names unique and the API keyed
# by id alone.
FOLDER_ID_RE = re.compile(r"^(?:.* — )?(\d{8}-\d{6})$")
LIST_FIELDS = ("id", "title", "created", "duration", "status", "mode", "sync")

# How much of a summary a sidebar row is allowed to carry, when the summary has
# no headline. Matches what the native row rendered from its own fetch.
BRIEF_CHARS = 140


def _row_view(meta):
    """The row half of a meeting: the one line it shows and the badges it draws.

    THE POINT OF THIS FUNCTION IS THE REQUEST IT DELETES. Every visible sidebar
    row used to GET /api/meetings/<id> just to learn whether the meeting has a
    summary — which answers with the WHOLE document: every turn, every word,
    the stats and the notes. Forty rows meant forty full transcripts on the
    wire (megabytes for a library of long meetings, to render forty booleans
    and one sentence) plus forty passes through _persist_notes, which takes
    JOB_LOCK and can write meeting.json, competing with the very job the row is
    reporting on.

    None of it needed a second request. _list_meetings has already parsed this
    meeting.json to build the row, so the answer is free; it just was not being
    returned. See /api/meetings for the published contract.

    `warnings` is passed through RAW rather than pre-classified into "capture"
    and "minor". That split is a list of phrases the client matches on, and it
    belongs in ONE place; a server-side copy would be a second one, drifting
    quietly the first time either side edited its list.
    """
    summary = meta.get("summary") or {}
    brief = str(summary.get("headline") or "").strip()
    if not brief:
        brief = " ".join(str(summary.get("tldr") or "").split())[:BRIEF_CHARS]
    return {
        "brief": brief,
        "has_summary": bool(summary),
        "has_transcript": bool(meta.get("turns")),
        "has_notes": bool(meta.get("notes")),
        "warnings": list(meta.get("warnings") or []),
    }


# The server binds 127.0.0.1, but a web page in the user's browser can still
# reach it. Two guards keep a random website from driving the app:
#  - Host header must be a loopback name → defeats DNS-rebinding (a rebound
#    domain arrives with Host: evil.com).
#  - State-changing requests with a cross-origin Origin are rejected → defeats
#    plain CSRF. The native app and curl send no Origin and are unaffected.
_ALLOWED_HOSTS = None  # built lazily from the configured port


def _loopback_hosts():
    global _ALLOWED_HOSTS
    if _ALLOWED_HOSTS is None:
        port = load_config().get("port", 5005)
        _ALLOWED_HOSTS = {f"127.0.0.1:{port}", f"localhost:{port}",
                          "127.0.0.1", "localhost"}
    return _ALLOWED_HOSTS


@app.before_request
def _guard_local_only():
    host = (request.host or "").lower()
    if host not in _loopback_hosts():
        abort(403)
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("Origin")
        if origin:
            from urllib.parse import urlparse
            if (urlparse(origin).hostname or "").lower() not in ("127.0.0.1", "localhost"):
                abort(403)


# ---------------------------------------------------------------- storage ----

def _safe_folder_title(title):
    """Make a meeting title safe as a folder name (macOS + OneDrive + Windows)."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", str(title or ""))
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned[:60].strip(" .")


def _folder_name_for(meta):
    safe = _safe_folder_title(meta.get("title"))
    return f"{safe} — {meta['id']}" if safe else meta["id"]


def _dir_for(meeting_id):
    """Resolve a meeting id to its folder (no request-context validation)."""
    plain = RECORDINGS_DIR / meeting_id
    if plain.exists():
        return plain
    suffix = f" — {meeting_id}"
    if RECORDINGS_DIR.exists():
        for d in RECORDINGS_DIR.iterdir():
            if d.is_dir() and d.name.endswith(suffix):
                return d
    return plain  # canonical location for a meeting that doesn't exist yet


def _meeting_dir(meeting_id):
    if not MEETING_ID_RE.match(meeting_id):
        abort(400, "bad meeting id")
    return _dir_for(meeting_id)


def _sync_folder_name(meta):
    """Rename the folder to match the title. Skipped while the meeting is
    recording or processing (open file handles / a job holding the old path);
    those catch up at stop / job-end / next startup.

    EVERY background job that holds a resolved path counts, summaries included.
    summarize.summarize_meeting() is handed _dir_for(id) once, at job start, and
    keeps that absolute path across a model call that runs for minutes; renaming
    the directory underneath it means its final read-modify-write lands on a
    path that no longer exists. summarize._store_summary() answers that OSError
    with "Could not save the summary" — so a rename typed mid-summary threw the
    whole summary away, and where the re-read failed some other way it fell back
    to writing its own stale snapshot, taking the new title with it. Same family
    as the recluster write-back: a long job holding a snapshot of a folder that
    someone else is allowed to move. The rename is not lost, only deferred — the
    summary job applies it when it finishes (see summarize_meeting below), and
    _backfill_folder_names() sweeps up anything a crash left behind."""
    if REC.is_recording and REC.meeting_id == meta["id"]:
        return
    if JOBS.get(meta["id"], {}).get("state") == "processing":
        return
    if SUMMARY_JOBS.get(meta["id"], {}).get("state") == "processing":
        return  # a summary is holding this exact path for the length of its run
    if RECLUSTER_JOBS.get(meta["id"], {}).get("state") == "processing":
        return  # a recluster is holding this exact path open mid-write
    if COMPRESS_JOBS.get(meta["id"], {}).get("state") == "processing":
        return  # the archiver is mid-encode against this exact path
    if ASK_JOBS.get(meta["id"], {}).get("state") == "processing":
        return  # an answer is being written against this exact path
    current = _dir_for(meta["id"])
    target = current.with_name(_folder_name_for(meta))
    if not current.exists() or current == target or target.exists():
        return
    try:
        current.rename(target)
    except OSError as exc:  # e.g. OneDrive briefly locking the folder
        app.logger.warning("could not rename folder for %s: %s", meta["id"], exc)


def _read_meeting(meeting_id):
    path = _meeting_dir(meeting_id) / "meeting.json"
    if not path.exists():
        abort(404, "meeting not found")
    meta = json.loads(path.read_text(encoding="utf-8"))
    # Notes live in their own append-only file while a meeting runs and are
    # copied into meeting.json at stop. Anything that stops the app before that
    # — a crash, a force-quit, /api/shutdown's os._exit — leaves them on disk
    # and in no document, and a note that exists but is nowhere visible is the
    # same as a lost one to the person who typed it. Union them in on every
    # read, so no reader of a meeting can miss one however it was interrupted.
    # In memory only: writing them back is _persist_notes, which the paths that
    # may write meeting.json call explicitly.
    notes_store.fold(_dir_for(meeting_id), meta)
    return meta


def _write_meeting(meta):
    """Replace meeting.json in one indivisible step.

    This used to write a temp file with a FIXED name ("meeting.json.tmp") and
    lean on an in-process lock to keep two writers off it. That lock only ever
    covered this function: pipeline.py, tidy.py and summarize.py write the same
    folder from their own threads, and a second MeetingScribe process (the
    packaged .app and a source checkout share ~/.meetingscribe) takes no
    in-process lock at all. Two writers meeting on one temp name is how a
    half-written file gets published over the only copy of a transcript.

    pipeline._atomic_write_text is the one implementation of that write — a
    unique temp name in the same directory, the target's permissions carried
    over, fsync before the rename, and the temp file removed if anything
    fails — so use it rather than keeping a second, weaker copy here.
    """
    pipeline._atomic_write_text(_dir_for(meta["id"]) / "meeting.json",
                                json.dumps(meta, ensure_ascii=False, indent=1))


def _raw_meeting(meeting_id):
    """meeting.json exactly as it is on disk — nothing folded in, no abort.

    The read behind a read-modify-write. _read_meeting and _read_meeting_safe
    both merge the notes file into what they return, which is right for a
    reader but wrong for a writer that wants to know what is actually stored.
    """
    try:
        path = _dir_for(meeting_id) / "meeting.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None


def _read_meeting_safe(meeting_id):
    """Like _read_meeting but returns None instead of aborting (for
    background threads, where a Flask abort would be meaningless)."""
    meta = _raw_meeting(meeting_id)
    if meta is not None:
        notes_store.fold(_dir_for(meeting_id), meta)  # see _read_meeting
    return meta


def _persist_notes(meeting_id):
    """Copy any notes.jsonl entries missing from meeting.json into it.

    -> True if it wrote. Cheap and idempotent: notes.fold() reports whether the
    document would actually change, and nothing is written when it would not.

    Called from the paths where a note could otherwise stay invisible: opening a
    meeting, starting a reprocess, startup after a crash, and a note that
    arrives once the recording it belongs to has already stopped. Readers do not
    depend on it — every read folds the notes in anyway — it is what makes the
    stored document match, so the summary, the export and the phone copy (which
    read meeting.json directly, not through here) see them too.

    Stands aside while a background job owns the meeting. process, tidy,
    recluster and summarize all publish meeting.json from a snapshot plus the
    keys they own; a write squeezed in between one of those reads and its
    os.replace is simply dropped, and could equally drop the job's result if the
    ordering fell the other way. Nothing is lost by waiting — notes.jsonl still
    holds the note, and the next read of the meeting folds it in. The claim is
    checked under JOB_LOCK for the same reason _edit_meeting_json holds it: the
    check and the write have to be one step, or a job claiming itself in the gap
    puts us right back in the race.

    THE INVARIANT THIS DEPENDS ON, stated once here because it is easy to break
    from the other side: JOB_LOCK is held across the WHOLE read-modify-write of
    meeting.json, by every route that does one. Standing aside for a claimed job
    is not enough on its own — a route that reads, mutates and writes without the
    lock and without a claim (record_stop was exactly that: it published the
    duration, the tracks and status="processing") races this function directly,
    and the loser's changes are silently overwritten by the winner's stale
    snapshot. record_stop, reprocess, tidy, tidy/undo and _edit_meeting_json all
    hold it now. A new writer of this file must too.
    """
    with JOB_LOCK:
        for jobs in (JOBS, RECLUSTER_JOBS, SUMMARY_JOBS):
            if jobs.get(meeting_id, {}).get("state") == "processing":
                return False
        meta = _raw_meeting(meeting_id)
        if meta is None or not notes_store.fold(_dir_for(meeting_id), meta):
            return False
        try:
            _write_meeting(meta)
        except OSError as exc:  # read-only disk, meeting deleted mid-call…
            app.logger.warning("could not save notes into %s: %s", meeting_id, exc)
            return False
    return True


def _push_synced(meeting_id):
    """Re-upload a synced meeting after an edit. No-op when not synced."""
    sync.push_if_synced(_read_meeting_safe, _edit_meeting_json_safe, meeting_id)


def _write_transcript_md(meta):
    """Save a human-readable transcript.md beside the audio so the meeting can
    be read later in any editor, with no app or meeting.json needed. Best-effort
    — never let a file-write failure break processing.

    Written atomically like everything else in the folder. write_text truncates
    the target first, so the whole point of this file — being readable when the
    app is not — was lost for the duration of every rewrite, and a crash or the
    os._exit() behind /api/shutdown froze it truncated. It is rewritten after
    every rename, tidy, summary and recluster, so that window comes round often.
    """
    if not meta.get("turns"):
        return
    try:
        pipeline._atomic_write_text(_dir_for(meta["id"]) / "transcript.md",
                                    _export_markdown(meta))
    except Exception as exc:  # noqa: BLE001 — saving the .md is non-critical
        app.logger.warning("could not write transcript.md for %s: %s", meta.get("id"), exc)


# List-scan cache: folder name -> (meeting.json mtime, item, searchable text).
# Keeps GET /api/meetings O(changed files) per request even with thousands of
# meetings — only new/edited meeting.json files are re-read.
#
# The lock is the paging one: a sidebar that fetches page after page has
# several of these scans in flight at once, and the eviction sweep below walks
# a dict another request thread is inserting into ("dictionary changed size
# during iteration", which Flask would answer with an HTML 500 the sidebar can
# only read as "the list is gone"). Held across the scan, which is a few stat()
# calls once the cache is warm; the pages are served from the returned list.
_LIST_CACHE = {}
_LIST_LOCK = threading.Lock()

# GET /api/meetings paging. The default is one screenful; the maximum is what
# one request will ever serve, so a library bigger than that is READ, in pages,
# rather than silently cut off at the end of the first one.
MEETINGS_PAGE_DEFAULT = 40
MEETINGS_PAGE_MAX = 500


def _list_meetings(query=""):
    items = []
    if not RECORDINGS_DIR.exists():
        return items
    seen = set()
    query = (query or "").strip().lower()
    with _LIST_LOCK:
        for d in RECORDINGS_DIR.iterdir():
            meta_path = d / "meeting.json"
            if not d.is_dir() or not FOLDER_ID_RE.match(d.name) or not meta_path.exists():
                continue
            try:
                mtime = meta_path.stat().st_mtime
            except OSError:
                continue
            # The cache key is BOTH files this row is built from. notes.jsonl
            # is the durable record of a note and meeting.json is only caught
            # up later (see _persist_notes), so a row keyed on meeting.json
            # alone would keep saying "no notes" about a meeting the user has
            # been typing into. One extra stat() per row.
            try:
                stamp = (mtime, (d / notes_store.NOTES_FILE).stat().st_mtime)
            except OSError:
                stamp = (mtime, 0.0)
            seen.add(d.name)
            cached = _LIST_CACHE.get(d.name)
            if cached and cached[0] == stamp:
                item, haystack = cached[1], cached[2]
            else:
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                except (ValueError, OSError):
                    continue
                item = {k: meta.get(k) for k in LIST_FIELDS}
                item["speakers"] = len(meta.get("speakers") or {})
                # The brief and the badges, so no client has to ask again.
                notes_store.fold(d, meta)
                item.update(_row_view(meta))
                haystack = " ".join(
                    [str(meta.get("title") or "")] + list((meta.get("speakers") or {}).values())
                ).lower()
                _LIST_CACHE[d.name] = (stamp, item, haystack)
            if query and query not in haystack:
                continue
            items.append(item)
        for stale in set(_LIST_CACHE) - seen:  # renamed/deleted folders
            del _LIST_CACHE[stale]
    items.sort(key=lambda m: m["id"], reverse=True)
    return items


# ------------------------------------------------------------- processing ----

def _auto_summarize(meeting_id, meta):
    """Write the summary the moment there is a transcript to write it from.

    Nothing ever did this. A summary only happened if someone pressed a button,
    and the button was hidden on any meeting that had a note on it, so in
    practice most meetings never got one: 26 of the 46 on the development
    machine had a transcript and no summary. A feature you have to remember to
    ask for is a feature that mostly does not run.

    Skipped when a summary already exists, which is the reprocess case: the
    user asked for a new transcript, not for the engine to spend a model call
    replacing a summary behind their back. Re-analyse is one click away and
    says what it does.

    NEVER FATAL. This runs on the tail of a transcription that SUCCEEDED, and
    every reason a summary can refuse — no summariser signed in, Apple
    Intelligence switched off, a recluster holding the file — is a reason to
    leave the meeting transcribed and quiet, not to fail it.
    """
    try:
        if not load_config().get("auto_summarize", True):
            return
        if meta.get("summary") or not meta.get("turns"):
            return
        refused = _start_summary(meeting_id)
    except Exception as exc:  # noqa: BLE001 — a bonus, never the meeting's fate
        app.logger.warning("auto-summary failed to start for %s: %s", meeting_id, exc)
        return
    if refused:
        app.logger.info("auto-summary skipped for %s: %s",
                        meeting_id, refused[0].get("error"))


def _start_processing(meeting_id, claimed=False):
    if not claimed:  # record_stop path: register the job now
        JOBS[meeting_id] = {"state": "processing", "message": "Loading model…"}

    def update(msg):
        JOBS[meeting_id]["message"] = msg

    def run():
        transcribed = None
        try:
            pipeline.process_meeting(_dir_for(meeting_id), update)
            meta = _read_meeting(meeting_id)
            _write_transcript_md(meta)
            JOBS[meeting_id] = {"state": "done", "message": "Complete"}
            _sync_folder_name(meta)  # catch up on renames deferred mid-job
            _push_synced(meeting_id)
            transcribed = meta
        except Exception:
            err = traceback.format_exc().strip().splitlines()[-1]
            # ORDER MATTERS HERE, and it was wrong.
            #
            # This used to publish the error state to JOBS first and only then
            # read meeting.json, mutate it and write the whole snapshot back —
            # unlocked, and with the claim already dropped. Both halves of that
            # lose an edit. Dropping the claim first re-opens the meeting to
            # every route that stands aside for a running job (a note landing
            # from the panel, a rename, the reprocess the user presses the
            # moment they see the failure), and writing a whole snapshot read
            # outside JOB_LOCK overwrites whatever any of them just saved. A
            # failed run has no business deleting a note the user typed.
            #
            # So: record the failure on the document FIRST, through the same
            # read-modify-write-under-JOB_LOCK every other writer of this file
            # uses (the invariant _persist_notes documents), touching only the
            # two keys this path owns — and release the claim afterwards, so
            # the meeting is never unowned and half-written at the same time.
            def mark_failed(meta):
                meta["status"] = "error"
                meta["error"] = err

            try:
                _edit_meeting_json_safe(meeting_id, mark_failed)
            except Exception:  # the document is beyond saving; JOBS still says so
                pass
            JOBS[meeting_id] = {"state": "error", "message": err}

        # OUTSIDE the try, and last. Two reasons, both load-bearing: anything
        # this raised inside that block would land in the handler above and
        # mark a meeting that transcribed perfectly well as failed; and
        # _start_summary blocks on a "processing" entry in JOBS, so claiming
        # the summary any earlier would refuse against this very job.
        if transcribed is not None:
            _auto_summarize(meeting_id, transcribed)
            # Archive this meeting once the dust settles. Delayed so the
            # summary's claim lands first (its blocker then makes the
            # archiver wait its turn); a no-op unless the user opted in.
            threading.Timer(5.0, _compress_one, args=(meeting_id,)).start()

    threading.Thread(target=run, daemon=True, name=f"process-{meeting_id}").start()


# ------------------------------------------------------------------ routes ----

ONBOARDING_VERSION = 1


@app.get("/")
def index():
    if int(load_config().get("onboarded") or 0) < ONBOARDING_VERSION:
        return redirect("/onboarding")
    return send_from_directory(str(BASE_DIR / "templates"), "index.html")


@app.get("/onboarding")
def onboarding():
    return send_from_directory(str(BASE_DIR / "templates"), "onboarding.html")


@app.post("/api/onboarding/done")
def onboarding_done():
    from config import CONFIG_PATH
    try:
        current = json.loads(CONFIG_PATH.read_text(encoding="utf-8")) if CONFIG_PATH.exists() else {}
    except (ValueError, OSError):
        current = {}
    current["onboarded"] = ONBOARDING_VERSION
    CONFIG_PATH.write_text(json.dumps(current, indent=1), encoding="utf-8")
    return jsonify({"ok": True})


# Deep links the onboarding page can open (WKWebView can't open custom
# URL schemes itself, so the server does it).
_SETTINGS_PANES = {
    "internet-accounts": "x-apple.systempreferences:com.apple.Internet-Accounts-Settings.extension",
    "apple-intelligence": "x-apple.systempreferences:com.apple.Siri-Settings.extension",
    "notifications": "x-apple.systempreferences:com.apple.Notifications-Settings.extension",
    "microphone": "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone",
}


@app.post("/api/onboarding/open-settings")
def onboarding_open_settings():
    target = (request.get_json(force=True, silent=True) or {}).get("target")
    url = _SETTINGS_PANES.get(target)
    if not url or sys.platform != "darwin":
        return jsonify({"error": "unknown settings pane"}), 400
    subprocess.Popen(["open", url])
    return jsonify({"ok": True})


@app.get("/api/status")
def status():
    # Snapshot the dicts: a background thread can add/remove a meeting while
    # jsonify walks them ("dictionary changed size during iteration"). Same
    # JSON out, just taken atomically. recluster_jobs is additive — existing
    # clients read "jobs"/"summary_jobs" by name and ignore the rest.
    with JOB_LOCK:
        jobs, summary_jobs = dict(JOBS), dict(SUMMARY_JOBS)
        recluster_jobs, ask_jobs = dict(RECLUSTER_JOBS), dict(ASK_JOBS)
    return jsonify({"recorder": REC.status(), "jobs": jobs,
                    "summary_jobs": summary_jobs,
                    "recluster_jobs": recluster_jobs,
                    "ask_jobs": ask_jobs})


@app.get("/api/devices")
def devices():
    # auto_route_macos is a user setting, so preflight has to be told the real
    # value — otherwise the UI reports "routes automatically" to someone who
    # turned routing off and would silently record nothing but their own mic.
    cfg = load_config()
    # A copy: preflight hands back its own cached dict, and adding a key to
    # that would edit the recorder's cache rather than this response.
    info = dict(REC.preflight(auto_route=bool(cfg.get("auto_route_macos", True))))
    # Whether this Mac can record is not only a question about devices. This
    # is the probe the UI runs before offering the button, so it is where a
    # disk with no room for a meeting has to appear too.
    info["disk"] = _disk_state()
    return jsonify(info)


@app.get("/api/llm/status")
def llm_status():
    """Is the on-device model (Apple Intelligence) ready for AI features?"""
    ok, reason = local_llm.available()
    return jsonify({
        "available": ok,
        "engine": "apple-intelligence",
        "reason": reason,
        "message": None if ok else local_llm.reason_message(reason),
    })


@app.get("/api/cli-engines")
def cli_engines():
    """Which AI CLIs are installed — the single probe the Settings UI reads,
    so front ends never re-hardcode binary paths. Includes the active engine
    so the picker can show what is currently in force."""
    return jsonify({
        "engines": ai_cli.detect_all(),
        "active": summarize._pick_engine(),
    })


# ----------------------------------------------------- first-run models ----
#
# THE AMBUSH THIS EXISTS TO REMOVE
# --------------------------------
# A fresh install ships no speech models. Parakeet (~2.4 GB) is pulled from
# HuggingFace by pipeline._get_parakeet and the neural diarizer's CoreML bundle
# (~22 MB) by the fluid-diarizer helper — both LAZILY, on the first meeting the
# user records. (The ECAPA speaker embedder was the third: it is now carried
# inside the bundle instead, which is why _speaker_spec below downloads
# nothing.) On a real fresh install that meeting sat on one static
# "Loading model…" for as long as a multi-gigabyte transfer takes: "working"
# and "hung" looked identical, and a stalled connection simply never ended.
#
# pipeline.py and diarization.py now report those downloads and retry a stalled
# one, so the lazy path is legible too. This is the other half: the wait no
# longer has to happen during a meeting at all. These two routes move it to
# onboarding, where a wait is expected, and report it in bytes:
#
#   GET  /api/models/status    what this Mac has, what it still needs, how big
#                              that is, and the live progress of a run. Cheap
#                              and safe to poll; never blocks on the network
#                              (the survey that does runs on its own thread and
#                              the route reports state "checking" meanwhile).
#   POST /api/models/prefetch  start the download. 409 while one is running.
#                              Same shape as /api/sync/all: one global job, a
#                              POST to start it and a GET to watch it.
#
# NOTHING HERE IS LOAD-BEARING. Skip the step, or never call these at all, and
# the lazy path is exactly what it was: the models download on first use. This
# only moves *when*, and makes the wait legible while it happens.
#
# STATES, as reported by both routes:
#   checking     the survey has not finished (or a run is still surveying)
#   ready        every required model is already on this Mac
#   missing      something is missing and no run is in flight
#   downloading  a run is in flight
#   done         a run finished and every required model is present
#   error        a run failed; `error` says why and a retry is allowed

MODEL_JOB = {}  # the single prefetch run — one global job, like SYNC_ALL
_MODEL_JOB_LOCK = threading.Lock()
# What has to be fetched, worked out once per relevant config (see _survey_key).
_MODEL_SURVEY = {}
_MODEL_SURVEY_LOCK = threading.Lock()
_MODEL_SURVEY_THREAD = None
_MODEL_SIZE_CACHE = {}

# Used only where nothing can be asked about a size (offline, or a component
# with no download plan of its own). Measured on this machine, 2026-08-04.
# The speaker model has no entry: it is not downloaded from anywhere, so its
# spec quotes 0 rather than a guess at a transfer that never happens.
_FALLBACK_BYTES = {"asr": 2_471_596_000, "turns": 22_000_000}
# How often the sampler reads the byte counter, and how long without a single
# new byte counts as stalled. A stalled transfer is the "it never stopped" half
# of the bug report: it has to be SAYABLE, not just endured.
_MODEL_SAMPLE_S = 0.4
_MODEL_STALL_S = 45.0


def _dir_bytes(path):
    """Bytes really occupied under `path` (missing path -> 0).

    lstat, not getsize: the HuggingFace cache stores one blob per file and
    points snapshots/<sha>/<name> at it with a SYMLINK, so following links
    counts every model twice — parakeet measured 4.9 GB against the 2.3 GB
    `du` reports, and the progress bar would have finished at 50%.
    """
    total = 0
    for root, _dirs, files in os.walk(str(path)):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:  # a temp file the downloader just replaced
                pass
    return total


def _hf_size(repo, files=None):
    """Exact download size from HuggingFace, or None if it can't be asked.

    The fallback for the components with no download plan of their own
    (config.hf_download_plan is the better answer wherever it exists — it
    counts only what is still MISSING). A short timeout because this sits
    behind a UI that is waiting on it: an unreachable hub must cost a few
    seconds and then degrade, never hold the survey open.
    """
    try:
        from huggingface_hub import HfApi

        info = HfApi().model_info(repo, files_metadata=True, timeout=8)
    except Exception as exc:  # noqa: BLE001 — offline is an ordinary case here
        app.logger.info("could not size %s from HuggingFace: %s", repo, exc)
        return None
    total = sum(int(s.size or 0) for s in (info.siblings or [])
                if files is None or s.rfilename in files)
    return total or None


def _cached_file(repo, names, cache_dir):
    """Is any of `names` already in the HuggingFace cache under `cache_dir`?"""
    try:
        from huggingface_hub import try_to_load_from_cache
    except Exception:
        return False
    for name in names:
        try:
            hit = try_to_load_from_cache(repo, name, cache_dir=cache_dir)
        except Exception:  # noqa: BLE001 — a cache probe must never raise out
            hit = None
        if isinstance(hit, str) and os.path.exists(hit):
            return True
    return False


# THE FETCHERS. Each one performs exactly the download its lazy loader would
# perform later, into exactly the folder that loader reads, so a pre-fetched
# model makes the first meeting instant rather than merely warm.
#
# The one that matters is pipeline.py's own entry point:
#
#   pipeline.download_asr_model(cfg, bytes_cb)   -> bytes fetched
#       bytes_cb(done_bytes, total_bytes, label). Raises ModelDownloadError,
#       whose str() is user-facing. Idempotent and cheap once on disk, and it
#       retries a stalled transfer itself (see config.ensure_hf_files).
#   pipeline.asr_download_size(cfg)
#       -> (bytes still to fetch, bytes in total), or (None, None) offline.
#
# diarization keeps both entry points (download_speaker_model,
# embedder_download_size), but the speaker model now ships in the bundle: they
# copy from Resources rather than fetch, and both answer 0 bytes. Only the
# first is wired in below — _speaker_spec sets "plan": None, because a size
# that is always 0 is not worth a call. Everything else here is for the
# components with no entry point of
# their own — the Whisper backends a machine without Parakeet falls back to,
# and the CoreML diarizer, which is fetched by a Swift helper and has no
# Python-side counter at all.


def _fetch_snapshot(repo):
    from huggingface_hub import snapshot_download

    snapshot_download(repo)


def _fetch_faster_whisper(model):
    from faster_whisper.utils import download_model

    download_model(model, cache_dir=str(MODELS_DIR / "whisper"))


def _fetch_neural_turns():
    """The CoreML diarizer fetches AND compiles its models the first time the
    helper runs, so the only way to pre-fetch it is to run it once. Three
    seconds of quiet noise is enough; the answer is thrown away."""
    import numpy as np
    import soundfile as sf

    import diarization_neural

    fd, path = tempfile.mkstemp(suffix=".wav", prefix="ms-warmup-")
    os.close(fd)
    try:
        noise = np.random.default_rng(0).normal(0, 0.02, 16000 * 3).astype("float32")
        sf.write(path, noise, 16000)
        diarization_neural.diarize_turns(path, num_speakers=1, timeout=900)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


# A COMPONENT SPEC, once, so the three below read the same way:
#   key, label, detail, required   what the UI says about it
#   dir                            where its bytes land (the sampler watches it)
#   present()                      is it already here? LOCAL PROBES ONLY — this
#                                  runs on every status poll, so it must never
#                                  touch the network
#   plan()                         (pending, total) bytes, or (None, None); one
#                                  network call, survey-time only
#   fetch(bytes_cb)                do the download; raise on failure
#   reports_bytes                  fetch() drives bytes_cb, so progress is the
#                                  library's own count instead of a disk sample
#   fallback                       the size to quote when nothing else answers


def _asr_spec(cfg):
    """The transcription model, or None when the chosen backend has none to
    download (Apple SpeechAnalyzer's models are part of macOS)."""
    backend = pipeline.pick_backend(cfg)
    if backend == "apple":
        return None
    if backend == "parakeet":
        lang = pipeline._parakeet_lang(cfg)
        repo = (pipeline.PARAKEET_REPO_EN if lang in (None, "en")
                else pipeline.PARAKEET_REPO_MULTI)
        cache = str(MODELS_DIR / "parakeet")
        return {
            "key": "asr", "label": "Speech model",
            "detail": repo.rsplit("/", 1)[1], "required": True,
            "dir": MODELS_DIR / "parakeet", "repo": repo,
            "files": pipeline.PARAKEET_FILES,
            "fallback": _FALLBACK_BYTES["asr"], "reports_bytes": True,
            "present": lambda: _cached_file(repo, ("model.safetensors",), cache),
            "plan": lambda: pipeline.asr_download_size(cfg),
            "fetch": lambda cb: pipeline.download_asr_model(cfg, cb),
        }
    if backend == "mlx":
        model = pipeline.resolve_model(cfg, "mlx")
        repo = pipeline.MLX_REPOS.get(model, model)
        return {
            "key": "asr", "label": "Speech model", "detail": model, "required": True,
            "dir": MODELS_DIR / "hf", "repo": repo, "files": None,
            "fallback": _FALLBACK_BYTES["asr"], "reports_bytes": False,
            "present": lambda: _cached_file(
                repo, ("weights.npz", "model.safetensors"), None),
            "plan": None,
            "fetch": lambda _cb: _fetch_snapshot(repo),
        }
    model = pipeline.resolve_model(cfg, "faster")
    # faster-whisper's own name -> repo table lives behind an import of
    # ctranslate2, which is far too heavy for a status poll; the naming is
    # stable and the fetch below goes through faster_whisper itself anyway.
    repo = model if "/" in model else f"Systran/faster-whisper-{model}"
    cache = str(MODELS_DIR / "whisper")
    return {
        "key": "asr", "label": "Speech model", "detail": model, "required": True,
        "dir": MODELS_DIR / "whisper", "repo": repo, "files": None,
        "fallback": _FALLBACK_BYTES["asr"], "reports_bytes": False,
        "present": lambda: _cached_file(repo, ("model.bin",), cache),
        "plan": None,
        "fetch": lambda _cb: _fetch_faster_whisper(model),
    }


def _speaker_spec():
    """ECAPA voice embeddings — how many people spoke, and which is which.

    THE ONE COMPONENT THAT NEVER DOWNLOADS. It rides inside the app bundle and
    config.seed_ecapa_onnx() installs it at ECAPA_ONNX_PATH on startup, so by
    the time anything polls this route a packaged install is already present
    and there is nothing for a prefetch to do. It is still listed because the
    survey is the user's inventory of what this Mac has, and a required model
    missing from it must be visible — which is the source-checkout case, where
    `fetch` can only re-run the bundle copy and then say, in the words
    diarization gives it, that the model has to be generated.
    """
    path = ECAPA_ONNX_PATH
    return {
        "key": "speakers", "label": "Speaker voice model",
        "detail": "ECAPA-TDNN (ONNX, bundled)", "required": True,
        "dir": path.parent, "repo": None, "files": None,
        # No download to quote, so no fallback size to guess with: _survey
        # reports a model that is here at what it occupies on disk, and one
        # that is not at 0, because 0 is what fetching it costs.
        "fallback": 0, "reports_bytes": False,
        "present": path.is_file,
        "plan": None,
        "fetch": diarization.download_speaker_model,
    }


def _turn_spec(cfg):
    """The neural turn-placer. Optional in the strongest sense: the classic
    assignment ships as the fallback for every failure this can have, so a
    machine that never gets these models transcribes exactly as well."""
    if cfg.get("diarization_engine", "auto") == "classic":
        return None
    import diarization_neural
    import swift_helpers

    # find_binary NEVER compiles (see swift_helpers) — a poll must not sit
    # behind a `swift build`. No helper means nothing worth pre-fetching.
    if swift_helpers.find_binary("fluid-diarizer") is None:
        return None
    models = diarization_neural.MODELS_SUBDIR
    return {
        "key": "turns", "label": "Speaker turn model",
        "detail": "community-1 on the Neural Engine", "required": False,
        "dir": models, "repo": None, "files": None,
        "fallback": _FALLBACK_BYTES["turns"], "reports_bytes": False,
        "present": lambda: any(models.glob("*/Segmentation.mlmodelc/coremldata.bin")),
        "plan": None,
        "fetch": lambda _cb: _fetch_neural_turns(),
    }


def _spec_size(spec):
    """Bytes this component still has to fetch.

    The library's own plan first — it counts what is MISSING, so a half-finished
    download is quoted at what is left rather than at the whole file again.
    """
    plan = spec.get("plan")
    if plan is not None:
        try:
            pending, total = plan()
        except Exception as exc:  # noqa: BLE001 — a size is never worth failing on
            app.logger.info("could not size %s: %s", spec["key"], exc)
            pending = total = None
        if pending:
            return pending
        if pending == 0 and total:
            return total  # nothing pending: quote the size it occupies
    if spec.get("repo") is None:
        return spec["fallback"]
    key = (spec["repo"], spec.get("files"))
    if key not in _MODEL_SIZE_CACHE:
        _MODEL_SIZE_CACHE[key] = _hf_size(spec["repo"], spec.get("files")) or spec["fallback"]
    return _MODEL_SIZE_CACHE[key]


def _survey_key(cfg):
    return (cfg.get("whisper_backend"), cfg.get("language"),
            cfg.get("whisper_model"), cfg.get("diarization_engine"))


def _survey(force=False):
    """What this Mac still has to fetch, and how big it is.

    BLOCKING — it asks HuggingFace for sizes and pick_backend() may compile the
    Apple Speech helper on a source checkout. Every caller runs it on a worker
    thread; the routes report state "checking" until it lands.
    """
    cfg = load_config()
    key = _survey_key(cfg)
    with _MODEL_SURVEY_LOCK:
        if not force and _MODEL_SURVEY.get("key") == key:
            return _MODEL_SURVEY["specs"]
    specs = [s for s in (_asr_spec(cfg), _speaker_spec(), _turn_spec(cfg)) if s]
    for spec in specs:
        # A model already here is reported at its size ON DISK, not at what it
        # would have cost to download: this step must never quote a download
        # figure for something it is not going to download.
        spec["size"] = (_dir_bytes(spec["dir"]) or spec["fallback"]
                        if spec["present"]() else _spec_size(spec))
    with _MODEL_SURVEY_LOCK:
        _MODEL_SURVEY.update(key=key, specs=specs)
    return specs


def _survey_in_background():
    global _MODEL_SURVEY_THREAD
    cfg_key = _survey_key(load_config())
    with _MODEL_SURVEY_LOCK:
        if _MODEL_SURVEY.get("key") == cfg_key:
            return
        if _MODEL_SURVEY_THREAD is not None and _MODEL_SURVEY_THREAD.is_alive():
            return
        _MODEL_SURVEY_THREAD = threading.Thread(
            target=_survey_quietly, daemon=True, name="model-survey")
        _MODEL_SURVEY_THREAD.start()


def _survey_quietly():
    try:
        _survey()
    except Exception as exc:  # noqa: BLE001 — a failed survey must not wedge
        app.logger.warning("model survey failed: %s", exc)


def _model_view():
    """The body both routes return. Presence is re-read from disk on every
    call, so a model that arrived by any other route (the lazy path, a manual
    copy, a second MeetingScribe) shows up here immediately."""
    with _MODEL_JOB_LOCK:  # snapshot the top level; see status() for why
        job = dict(MODEL_JOB)
    running = job.get("state") in ("checking", "downloading")
    with _MODEL_SURVEY_LOCK:
        specs = _MODEL_SURVEY.get("specs")
    if specs is None:
        return {"state": "checking", "ready": False, "stalled": False,
                "message": "Checking what this Mac already has", "error": None,
                "downloaded_bytes": 0, "total_bytes": 0, "components": []}

    live = job.get("components") or {}
    scope = job.get("scope")
    components, downloaded, total, ready = [], 0, 0, True
    for spec in specs:
        present = spec["present"]()
        entry = live.get(spec["key"], {})
        size = int(spec["size"])
        got = size if present else min(size, int(entry.get("bytes") or 0))
        if not present and spec["required"]:
            ready = False
        # Totals cover the components THIS RUN took on (or, with no run, the
        # ones still missing) — so the bar is over the work actually being
        # waited for and never dilutes it with 2.4 GB that is already here.
        counted = spec["key"] in scope if scope is not None else not present
        if counted:
            downloaded += got
            total += size
        components.append({
            "key": spec["key"], "label": spec["label"], "detail": spec["detail"],
            "required": bool(spec["required"]), "present": present,
            "state": ("present" if present else entry.get("state") or "pending"),
            "bytes": got, "total_bytes": size,
            "error": entry.get("error"),
        })

    if running:
        state = job["state"]
    elif job.get("state") == "error":
        state = "error"
    elif ready:
        state = "done" if job.get("state") == "done" else "ready"
    else:
        state = "missing"
    # The job's own words, EXCEPT where the disk contradicts them: a finished
    # run whose models are somehow not here must not still be saying "ready".
    message = job.get("message")
    if state == "missing" or not message:
        message = ("Everything is already on this Mac" if ready
                   else f"{human_bytes(total)} to download")
    return {"state": state, "ready": ready, "stalled": bool(job.get("stalled")),
            "message": message, "error": job.get("error"),
            "downloaded_bytes": downloaded, "total_bytes": total,
            "components": components}


def _download_one(spec):
    """Fetch one component, keeping a real byte count on screen throughout.

    Two sources, in order of honesty. pipeline.download_asr_model counts the
    bytes it transfers and hands them over (`reports_bytes`), which is exact
    and survives a resume. The rest have no counter, so progress is read from
    the only place it is visible: bytes landing in the target folder (the
    speaker model's bundle copy is one of these, and lands in one go). That
    under-reports a resumed download (the part already there is the baseline)
    but still moves and still finishes at 100%, and a fresh install — the case
    this whole path exists for — is exact either way.

    One sampler either way, because it also owns the stall flag: bytes that
    stop moving is what "it never stopped" looked like from the outside, and
    saying so is the whole point.
    """
    key = spec["key"]
    entry = MODEL_JOB["components"][key]
    baseline = _dir_bytes(spec["dir"])
    size = int(spec["size"])
    reports = bool(spec.get("reports_bytes"))
    entry.update(state="downloading", bytes=0)
    stop = threading.Event()

    def line(got):
        return (f"Downloading the {spec['label'].lower()}, "
                f"{human_bytes(got)} of {human_bytes(size)}")

    def report(done, total, _label=None):
        # Count and sentence updated together, from the same event: read a tick
        # apart they disagree, and a progress display that contradicts itself
        # is not believed for the rest of the download.
        got = min(int(total or size) or size, max(0, int(done or 0)))
        entry["bytes"] = got
        MODEL_JOB["message"] = line(got)

    def sample():
        last, changed = -1, time.monotonic()
        while not stop.wait(_MODEL_SAMPLE_S):
            if reports:
                got = int(entry["bytes"])  # the library owns the number
            else:
                got = min(size, max(0, _dir_bytes(spec["dir"]) - baseline))
                entry["bytes"] = got
                MODEL_JOB["message"] = line(got)
            if got != last:
                last, changed = got, time.monotonic()
            MODEL_JOB["stalled"] = (time.monotonic() - changed) > _MODEL_STALL_S

    MODEL_JOB["message"] = line(0)
    watcher = threading.Thread(target=sample, daemon=True, name=f"model-bytes-{key}")
    watcher.start()
    try:
        spec["fetch"](report)
    finally:
        stop.set()
        MODEL_JOB["stalled"] = False
    entry.update(state="done", bytes=size)


def _landed_anyway(spec):
    """The fetch raised, but the model is on disk regardless -> count it.

    The neural turn model has no download-only entry point: it is fetched by
    RUNNING the helper, which also performs real inference, and that inference
    can legitimately refuse the synthetic warm-up audio ("noSpeechDetected")
    long after the models were downloaded and compiled for the Neural Engine.
    The disk is the authority everywhere else in this file and it is the
    authority here: a failure that left the model behind is not a failure.
    """
    if not spec["present"]():
        return False
    MODEL_JOB["components"][spec["key"]].update(
        state="done", bytes=int(spec["size"]), error=None)
    return True


def _fail_component(spec, message, detail):
    """Record one component's failure. A REQUIRED one fails the run; an
    optional one is logged and left behind, because the classic path already
    covers everything it does and half a transcript is not on the table."""
    MODEL_JOB["components"][spec["key"]].update(
        state="error" if spec["required"] else "skipped", error=detail)
    if spec["required"]:
        MODEL_JOB.update(state="error", stalled=False,
                         message=message, error=detail)
    else:
        app.logger.warning("could not pre-fetch %s: %s", spec["key"], detail)


def _run_model_prefetch():
    try:
        specs = _survey()
    except Exception:
        detail = traceback.format_exc().strip().splitlines()[-1]
        MODEL_JOB.update(state="error", stalled=False,
                         message="Could not work out what this Mac needs",
                         error=detail)
        return
    todo = [s for s in specs if not s["present"]()]
    MODEL_JOB.update(state="downloading", scope=[s["key"] for s in todo],
                     components={s["key"]: {"state": "pending", "bytes": 0}
                                 for s in todo})
    if not todo:
        MODEL_JOB.update(state="done", message="Everything is already on this Mac")
        return
    for spec in todo:
        try:
            _download_one(spec)
        except ModelDownloadError as exc:
            # str() is already user-facing copy, and it names the model, the
            # size, what failed and the fact that a retry resumes — far better
            # than anything this route could compose from a traceback.
            if not _landed_anyway(spec):
                _fail_component(spec, str(exc), str(exc))
                if spec["required"]:
                    return
        except Exception:
            if not _landed_anyway(spec):
                detail = traceback.format_exc().strip().splitlines()[-1]
                _fail_component(
                    spec, f"Could not download the {spec['label'].lower()}", detail)
                if spec["required"]:
                    return
    # "Done" is a claim about the disk, so check the disk. A fetch that returned
    # without leaving the model behind is a failure the user must be told about,
    # not a green tick over a first meeting that will download it all again.
    absent = [s for s in specs if s["required"] and not s["present"]()]
    if absent:
        MODEL_JOB.update(
            state="error", stalled=False,
            message=f"The {absent[0]['label'].lower()} is still missing",
            error="the download finished but the model is not on disk")
        return
    MODEL_JOB.update(state="done", stalled=False, message="Models ready on this Mac")


@app.get("/api/models/status")
def models_status():
    """What this Mac has, what it needs, and how a run is going. Poll freely."""
    _survey_in_background()
    return jsonify(_model_view())


@app.post("/api/models/prefetch")
def models_prefetch():
    """Download the models now instead of during the user's first meeting."""
    with _MODEL_JOB_LOCK:
        if MODEL_JOB.get("state") in ("checking", "downloading"):
            return jsonify({"error": "Already downloading"}), 409
        MODEL_JOB.clear()
        MODEL_JOB.update(state="checking", stalled=False, error=None,
                         message="Checking what this Mac already has",
                         components={}, scope=[])
    threading.Thread(target=_run_model_prefetch, daemon=True,
                     name="model-prefetch").start()
    return jsonify(_model_view())


@app.get("/api/calendar/today")
def calendar_today():
    """Today's calendar events (for title suggestions and auto-naming)."""
    if calendar_events is None:
        return jsonify({"available": False, "events": [], "error": "not supported here"})
    return jsonify(calendar_events.todays_events())


# ------------------------------------------------------------- disk space ----
#
# Recording writes uncompressed 16-bit WAV and nothing ever removes it: the
# system tap runs at 48 kHz stereo (192 KB/s) and the mic at 48 kHz mono
# (96 KB/s), so one meeting-hour costs about 1.04 GB and the folder only ever
# grows. A disk that fills is therefore not a corner case, it is the end of a
# straight line — and the worst possible moment to discover it is mid-meeting,
# where a failed write surfaces afterwards as a truncated track, if at all.
#
# Two guards, and only two. Refuse to START a recording that has nowhere to
# go, and say how much room is left while one runs.
#
# NOTHING HERE DELETES ANYTHING, and that is a decision rather than an
# omission. These recordings are the user's, they are the only copy, and an
# app that quietly reclaimed space by dropping the oldest meeting would have
# answered a disk problem with data loss. If retention is ever added it has to
# be something the user switched on, off by default, and it does not belong in
# the code path that is trying to start a meeting.
#
# COMPRESSION IS NOT RETENTION. The opt-in FLAC archival below
# (_compress_sweep / audio_archive.py) rewrites a finished meeting's audio
# into a smaller lossless container and removes the WAV only after decoding
# the copy back and comparing it sample for sample — the audio a reader gets
# afterwards is bit-identical, so no information is lost and the policy above
# stands. It is still off by default ("compress_recordings"), because it
# rewrites the only copy and that choice belongs to the user.
RECORDING_BYTES_PER_SECOND = 288 * 1024   # mic mono + system stereo @ 48 kHz
# Below this a recording is refused. ~2 hours of headroom, which is longer
# than any meeting this app has recorded, plus room for the pipeline's own
# working files. Anything much smaller and "you may start" stops meaning it.
MIN_FREE_TO_RECORD = 2 * 1024 ** 3
# Below this the UI is told to say so — while there is still time to act,
# rather than at the point where recording has already stopped being possible.
LOW_FREE_WARNING = 4 * 1024 ** 3
# /api/record/status is polled several times a second by the HUD; the free
# space it reports does not need to be re-measured that often.
_DISK_POLL_S = 15.0
_disk_seen = {"at": 0.0, "free": None}


def _free_bytes(force=False):
    """Free bytes where recordings are written, or None if unreadable.

    Cached for _DISK_POLL_S. None means the question could not be answered,
    which is never on its own a reason to stop someone recording.
    """
    now = time.monotonic()
    if not force and _disk_seen["free"] is not None \
            and now - _disk_seen["at"] < _DISK_POLL_S:
        return _disk_seen["free"]
    try:
        free = shutil.disk_usage(RECORDINGS_DIR).free
    except OSError as exc:  # unmounted volume, permissions…
        app.logger.warning("could not read free space on %s: %s", RECORDINGS_DIR, exc)
        return None
    _disk_seen.update(at=now, free=free)
    return free


def _recording_time_left(free):
    """`free` bytes as the recording time it buys: "about 40 minutes"."""
    minutes = int(free / RECORDING_BYTES_PER_SECOND / 60)
    if minutes < 90:
        return f"about {max(0, minutes)} minutes"
    hours = minutes / 60.0
    return f"about {hours:.0f} hours" if hours >= 2 else "about an hour and a half"


def _disk_state(force=False):
    """Space as the UI states it. Always present, always safe to poll:
    {"free": bytes|None, "state": ok|low|full|unknown, "message": str|None}.

    "full" is the state that refuses a recording; "low" is a warning and
    refuses nothing. Both carry the sentence to show, so every front end says
    the same thing about the same number.
    """
    free = _free_bytes(force=force)
    if free is None:
        return {"free": None, "state": "unknown", "message": None}
    if free < MIN_FREE_TO_RECORD:
        return {
            "free": free, "state": "full",
            "message": (f"Only {human_bytes(free)} of disk space is left, room for "
                        f"{_recording_time_left(free)} of recording. Free up space "
                        "before recording again."),
        }
    if free < LOW_FREE_WARNING:
        return {
            "free": free, "state": "low",
            "message": (f"Disk space is running low: {human_bytes(free)} left, room "
                        f"for {_recording_time_left(free)} of recording."),
        }
    return {"free": free, "state": "ok", "message": None}


# ------------------------------------------------------- FLAC archival ----
# The opt-in follow-through on the policy comment above: finished meetings'
# WAVs become verified FLACs (audio_archive.py owns the how; this block owns
# the WHEN — never while anything else holds the meeting).

_COMPRESS_SWEEP_LOCK = threading.Lock()
_COMPRESS_SWEEP_THREAD = None


def _compressible_tracks(meta):
    """(key, file_name) pairs still on WAV, for a meeting that is done."""
    if (meta or {}).get("status") != "done":
        return []
    return [(key, t.get("file") or f"{key}.wav")
            for key, t in (meta.get("tracks") or {}).items()
            if (t.get("file") or f"{key}.wav").endswith(".wav")]


def _reclaimable_bytes():
    """Bytes the archival sweep could still reclaim (approx: WAV size minus
    the ~4.4x-smaller FLAC it would become, counted as the whole WAV for a
    simple, conservative-enough headline)."""
    total = 0
    for meta in _list_meetings():
        d = _dir_for(meta["id"])
        for _key, name in _compressible_tracks(_read_meeting_safe(meta["id"]) or {}):
            try:
                total += (d / name).stat().st_size
            except OSError:
                pass
    return total


def _compress_one(meeting_id):
    """Archive one finished meeting's tracks, standing aside for everything.

    Silent no-op unless the config switch is on and the meeting is done and
    unowned. Per-track: encode+verify (audio_archive.compress_track), commit
    the new name to meeting.json under JOB_LOCK, and only then remove the
    WAV. A meeting that is mid-anything is simply skipped — the next sweep
    gets it.
    """
    if not load_config().get("compress_recordings"):
        return
    if REC.is_recording and REC.meeting_id == meeting_id:
        return
    meta = _read_meeting_safe(meeting_id)
    if not meta or meta.get("status") != "done":
        return
    d = _dir_for(meeting_id)
    denied = _claim_job(
        COMPRESS_JOBS, meeting_id, "Compressing audio…",
        blockers=[(JOBS, "Meeting is being processed"),
                  (SUMMARY_JOBS, "Meeting summary is being generated"),
                  (RECLUSTER_JOBS, RECLUSTER_BUSY)])
    if denied:
        return
    try:
        for key, track in (meta.get("tracks") or {}).items():
            name = track.get("file") or f"{key}.wav"
            if name.endswith(".flac"):
                # Crash window: meta committed, WAV never removed. Re-verified
                # before removal inside remove_stranded_wav.
                audio_archive.remove_stranded_wav(d, key)
                continue
            patch = audio_archive.compress_track(d, key, name)
            if patch is None:
                continue

            def commit(m, key=key, patch=patch):
                tracks = m.get("tracks") or {}
                if key not in tracks:
                    return ({"error": "track vanished"}, 409)
                tracks[key].update(patch)
                return None

            _meta, err = _edit_meeting_json(meeting_id, commit)
            if err is not None:
                app.logger.warning("compress %s/%s: meta commit refused (%s); "
                                   "keeping the WAV", meeting_id, key, err[0])
                continue
            audio_archive.remove_wav(d, key)
    except Exception as exc:
        app.logger.warning("compress %s failed: %s", meeting_id, exc)
    finally:
        with JOB_LOCK:
            COMPRESS_JOBS.pop(meeting_id, None)


def _compress_sweep():
    """Every finished meeting, oldest wreckage first. One at a time — the
    encoder is fast (~2,000x realtime) but this is a background nicety and
    must never compete with a live meeting for I/O."""
    try:
        audio_archive.sweep_orphan_temps(RECORDINGS_DIR)
        for meta in _list_meetings():
            try:
                _compress_one(meta["id"])
            except Exception as exc:  # one bad folder never blocks the rest
                app.logger.warning("compress sweep skipped %s: %s", meta.get("id"), exc)
    except Exception as exc:
        app.logger.warning("compress sweep failed: %s", exc)


def _compress_sweep_async():
    """Single-flight background sweep. Returns False when one is running."""
    global _COMPRESS_SWEEP_THREAD
    with _COMPRESS_SWEEP_LOCK:
        if _COMPRESS_SWEEP_THREAD is not None and _COMPRESS_SWEEP_THREAD.is_alive():
            return False
        _COMPRESS_SWEEP_THREAD = threading.Thread(
            target=_compress_sweep, daemon=True, name="compress-sweep")
        _COMPRESS_SWEEP_THREAD.start()
    return True


@app.get("/api/storage")
def storage_state():
    """The Settings pane's storage card: what is used, what a sweep would buy."""
    used = compressed = pending = 0
    for meta in _list_meetings():
        full = _read_meeting_safe(meta["id"]) or {}
        d = _dir_for(meta["id"])
        for key, t in (full.get("tracks") or {}).items():
            name = t.get("file") or f"{key}.wav"
            try:
                used += (d / name).stat().st_size
            except OSError:
                continue
            if name.endswith(".flac"):
                compressed += 1
            elif full.get("status") == "done":
                pending += 1
    return jsonify({
        "free": _free_bytes(), "audio_bytes": used,
        "reclaimable": _reclaimable_bytes(),
        "compressed_tracks": compressed, "pending_tracks": pending,
        "enabled": bool(load_config().get("compress_recordings")),
        "sweeping": _COMPRESS_SWEEP_THREAD is not None and _COMPRESS_SWEEP_THREAD.is_alive(),
    })


@app.post("/api/storage/compress")
def storage_compress():
    """Kick a sweep over everything already on disk (the toggle only catches
    meetings finishing after it was switched on)."""
    if not load_config().get("compress_recordings"):
        return jsonify({"error": "Turn on 'Compress finished recordings' first"}), 400
    started = _compress_sweep_async()
    return jsonify({"ok": True, "started": started,
                    "reclaimable": _reclaimable_bytes()})


@app.post("/api/record/start")
def record_start():
    data = request.get_json(force=True, silent=True) or {}
    return _do_record_start(data)


def _refused(reason, sentence, status, **extra):
    """The one shape every refusal to start a recording comes back in.

        {"error": "<a sentence to show the user>",
         "reason": "<a stable code to branch on>", …}

    WHY BOTH. `error` is the whole point: pressing Record and getting a bare
    409 tells the user nothing, and "Already recording" was as close as some of
    these came to a sentence. Every refusal below now carries something a
    person can read and act on, written here rather than assembled by whoever
    is rendering it, so the native app, a browser and curl all say the same
    thing about the same failure.

    `reason` exists because a front end sometimes has to DO something specific
    (offer Stop for already_recording, open the disk pane for disk_full), and
    matching on English prose to decide is how a copy edit breaks a button. It
    is the stable half; `error` is the human half and may be reworded freely.

    The codes, all of them:

      disk_full          no room to record. Carries "disk" (see _disk_state).
      already_recording  a recording is running right now.
      too_soon           two starts inside the same second.
      folder_failed      the meeting folder could not be created.
      audio_failed       the recorder itself refused. Carries the engine's own
                         sentence, which names the device or permission at
                         fault.

    A caller that does not know a code shows `error` and is never worse off
    than it is today.
    """
    return jsonify({"error": sentence, "reason": reason, **extra}), status


def _do_record_start(data):
    """Start a recording, or refuse in the shape _refused() documents."""
    # Before anything is created: a meeting that cannot be written is worse
    # than one that never started, because the user believes it is running.
    # Measured fresh (force=True) — a start is rare, and a cached number from
    # 15 seconds ago is not what to bet a meeting on.
    disk = _disk_state(force=True)
    if disk["state"] == "full":
        return _refused("disk_full", disk["message"], 507, disk=disk)

    expected = data.get("expected_speakers")
    try:
        expected = max(1, min(8, int(expected))) if expected else None
    except (TypeError, ValueError):
        expected = None

    # No title given? Name the recording after the calendar event happening
    # right now (cached lookup only — never delays the start of a recording).
    title = (data.get("title") or "").strip()
    event = None
    if calendar_events is not None and (not title or expected is None):
        try:
            event = calendar_events.current_event(cached_only=True)
        except Exception:
            event = None
    if not title and event:
        title = event["title"]

    # Language: per-meeting choice from the record form, else the config.
    cfg = load_config()
    language = (data.get("language") or "").strip() or None
    if language not in (None, "auto") and not re.match(r"^[a-zA-Z-]{2,10}$", language):
        language = None
    if language == "auto":
        language = None

    global LIVE
    with RECORD_LOCK:
        if REC.is_recording:
            return _refused(
                "already_recording",
                "A recording is already running. Stop it before starting "
                "another one.", 409)
        meeting_id = datetime.now().strftime("%Y%m%d-%H%M%S")
        if _dir_for(meeting_id).exists():  # two starts within the same second
            return _refused(
                "too_soon",
                "Another recording just started this second. Wait a moment "
                "and press Record again.", 409)
        meeting_dir = RECORDINGS_DIR / _folder_name_for({"id": meeting_id, "title": title})
        try:
            meeting_dir.mkdir(parents=True)
        except OSError as exc:
            # Was an uncaught 500 with an HTML body: the one refusal that
            # reached the user as no sentence at all, on exactly the machine
            # (read-only volume, missing folder, permissions) where knowing
            # WHY matters most.
            app.logger.warning("could not create %s: %s", meeting_dir, exc)
            return _refused(
                "folder_failed",
                f"The folder for this recording could not be created in "
                f"{RECORDINGS_DIR}: {exc.strerror or exc}. Check that the "
                "location exists and can be written to.", 500)

        # Live captions: bias recognition toward the user's vocabulary,
        # attendee names, and the built-in tech-term list; feed the recorder's
        # PCM straight into the streaming recognizer. Optional — recording
        # works identically without it.
        taps = None
        if cfg.get("live_captions", True):
            try:
                names = []
                if event:
                    names += (event.get("names") or [])
                    if event.get("organizer"):
                        names.append(event["organizer"])
                context = tech_vocabulary.merge_context(cfg.get("vocabulary"), names)
                if LIVE is not None:
                    LIVE.discard()
                LIVE = live_captions.LiveSession(
                    locale=pipeline.apple_locale(language or cfg.get("language")),
                    context_strings=context,
                )
                if LIVE.enabled:
                    taps = {"mic": LIVE.tap("mic"), "system": LIVE.tap("system")}
            except Exception as exc:  # captions must never block recording
                app.logger.warning("live captions unavailable: %s", exc)
                LIVE = None

        try:
            auto_route = bool(cfg.get("auto_route_macos", True))
            info = REC.start(meeting_dir, meeting_id, auto_route=auto_route, taps=taps)
        except RuntimeError as exc:
            shutil.rmtree(meeting_dir, ignore_errors=True)
            # The recorder's own sentence names the device or the permission
            # that failed, so it is passed through rather than replaced — but
            # never passed through EMPTY, which is what a bare RuntimeError()
            # would leave the user staring at.
            detail = str(exc).strip()
            return _refused(
                "audio_failed",
                detail or "The audio devices could not be opened, so the "
                          "recording did not start. Check the microphone in "
                          "System Settings and try again.", 500)
    # A client that chose a specific calendar event sends its attendee count
    # here already in the engine's arithmetic (see RecorderCenter.swift).
    try:
        hint = int(data.get("speaker_count_hint") or 0) or None
        hint = max(1, min(8, hint)) if hint else None
    except (TypeError, ValueError):
        hint = None
    if hint is None and expected is None and event and event.get("attendees"):
        # Calendar attendees excludes you. Online mode wants "other speakers";
        # in-person wants the total around the shared mic, so add yourself.
        # This is a GUESS, so it goes under its own key and the pipeline
        # applies it as a cap, never as a forced count — see
        # pipeline.SPEAKER_COUNT_HINT. expected_speakers stays what the user
        # typed, or None.
        others = int(event["attendees"])
        if data.get("mode") == "inperson":
            others += 1
        hint = max(1, min(8, others))

    # Space that is merely low does not stop a meeting, but it belongs on the
    # meeting's own record: it is the one warning that explains a track that
    # stops early, and it has to outlive the recording to be read afterwards.
    warnings = list(info["warnings"])
    if disk["state"] == "low":
        warnings.append(disk["message"])

    meta = {
        "id": meeting_id,
        "title": title or "Meeting " + datetime.now().strftime("%d %b %Y, %H:%M"),
        "created": datetime.now().isoformat(timespec="seconds"),
        "mode": "inperson" if data.get("mode") == "inperson" else "online",
        "expected_speakers": expected,
        "speaker_count_hint": hint,
        "status": "recording",
        "tracks": info["tracks"],
        "warnings": warnings,
        "routing": info.get("routing"),
    }
    if language:
        meta["language"] = language
    if event:
        meta["calendar_event"] = {
            "title": event["title"],
            "start": event["start"],
            "names": event.get("names") or [],
        }
    # Freeze the pre-meeting cues into this meeting and stamp them onto `meta`,
    # so the single _write_meeting below carries them — a second write here
    # would race the folder's other writers for nothing.
    notes_store.begin_meeting(meeting_dir, meta)
    _write_meeting(meta)
    return jsonify(meta)


@app.post("/api/record/stop")
def record_stop():
    global LAST_RECORDING
    with RECORD_LOCK:
        if not REC.is_recording:
            return jsonify({"error": "Not recording"}), 409
        meeting_id = REC.meeting_id
        result = REC.stop()
        # Remember which meeting just ended BEFORE anything can fail below: a
        # note still sitting in the panel's field is posted in the moments after
        # this, when the recorder is idle and no longer knows the id. Set inside
        # the lock so it cannot be overwritten by a start that follows.
        LAST_RECORDING = {"id": meeting_id, "at": time.monotonic()}
        if LIVE is not None:
            LIVE.stop()  # helpers finalize; captions stay readable meanwhile
    # Everything that touches meeting.json happens under JOB_LOCK, which is the
    # lock this app serialises read-modify-writes of that file on (see
    # _persist_notes and _edit_meeting_json). It has to be, because a note
    # arriving right now is the single most likely thing to collide with this:
    # the panel flushes the field the user was typing in the instant they press
    # Stop, and _persist_notes then does its own read-modify-write of the same
    # document. Unsynchronised, whichever of the two read first and wrote last
    # published a snapshot missing the other's changes — and _persist_notes
    # winning meant reverting status back to "recording" and dropping the
    # duration and track results this route had just written, leaving a meeting
    # that never processes and shows as still recording. Holding the lock across
    # read → mutate → write makes one of them simply happen after the other, and
    # the loser re-reads instead of overwriting.
    #
    # The claim on JOBS goes in the same block for the same reason: registered
    # outside it, there is a gap between this write and _start_processing during
    # which _persist_notes sees no job, and the race is back.
    with JOB_LOCK:
        meta = _read_meeting(meeting_id)  # fresh read, inside the lock
        for key, tr in result["tracks"].items():
            meta["tracks"].setdefault(key, {}).update(tr)
        meta["duration"] = result["duration"]
        meta["warnings"] = meta.get("warnings", []) + result["warnings"]
        # A long meeting can eat the space it was started with. Measured here
        # rather than trusted from the poll cache, and recorded on the meeting
        # so the state the audio was written under is readable later.
        stop_disk = _disk_state(force=True)
        if stop_disk["state"] in ("low", "full") \
                and stop_disk["message"] not in meta["warnings"]:
            meta["warnings"].append(stop_disk["message"])
        meta["status"] = "processing"
        # Fold the live notes (and the cues this meeting froze) into the meeting
        # so the review UI and the summary see them. notes.jsonl stays
        # authoritative; this rides along on the write that was happening anyway.
        notes_store.attach(_dir_for(meeting_id), meta)
        # Before the claim below: _sync_folder_name stands aside for a job that
        # owns the meeting, and claiming first would defer the rename it can do
        # safely right now (the WAVs are closed and nothing else holds the path).
        _sync_folder_name(meta)
        _write_meeting(meta)
        JOBS[meeting_id] = {"state": "processing", "message": "Loading model…"}
    _start_processing(meeting_id, claimed=True)
    return jsonify(meta)


@app.get("/api/record/status")
def record_status():
    """The recorder alone — what the floating HUD polls for the timer, the
    waveform and speech-wake.

    /api/status already carries this under "recorder", but it also walks JOBS,
    SUMMARY_JOBS and RECLUSTER_JOBS behind JOB_LOCK. The HUD polls a few times
    a second for the whole of a meeting and wants none of that, so this serves
    the recorder snapshot on its own and leaves /api/status untouched for the
    web UI.

    "elapsed" and "levels" are always present, even idle: REC.status() returns
    a bare {"recording": False} with no tracks, and a client that decodes into
    a fixed shape would fail on the missing keys rather than simply draw a
    stopped timer.

    "disk" rides along for the same reason it is cheap to: this is the one
    thing a client polls throughout a meeting, so it is where "you are running
    out of room" can be said while the recording it concerns is still going.
    Its message is null unless there is something to say, and the value is
    re-measured at most every _DISK_POLL_S however often this is called.
    """
    st = REC.status()
    st.setdefault("elapsed", 0.0)
    levels = st.setdefault("levels", {})
    levels.setdefault("mic", 0.0)
    levels.setdefault("system", 0.0)
    st["disk"] = _disk_state()
    return jsonify(st)


# ------------------------------------------------------------ notes & cues ----
# The floating note panel writes here while a meeting runs, and the cue list is
# edited before one. Storage lives in notes.py; these routes only validate.

def _recently_stopped():
    """The meeting that stopped within the grace window, or None."""
    last = LAST_RECORDING  # one read of the whole dict — see the global
    if not last or time.monotonic() - last["at"] > NOTE_GRACE_SECONDS:
        return None
    return last["id"]


@app.post("/api/record/note")
def record_note():
    """Append one timestamped note to a meeting.

    THE CONTRACT (the note panel is the only client)
    ------------------------------------------------
    POST body: {"text": str, "t": float|null, "meeting_id": str|null}

      meeting_id  The meeting the note belongs to, and the ONLY thing that
                  decides where it is filed when it is present. The note goes
                  there whatever the recorder is doing now — recording,
                  stopping, processing, or finished long ago — with no time
                  limit. A panel MUST send it: it knows the id it was recording
                  under, and by the time a note flushed at Stop lands here the
                  recorder is idle and no longer does.

                  The id is never second-guessed against the live recording,
                  and in particular a note whose meeting_id names a meeting
                  OTHER than the one recording right now is filed under the id
                  it names, not the live one. That is the whole point: the two
                  disagreeing is the normal case at a handover (the panel
                  flushes meeting A's last note while B has already started),
                  and the client is the only party that knows which meeting the
                  user was looking at when they typed. Overriding it with the
                  live id is precisely how a note lands on the wrong meeting.

                  An id we cannot honour is REFUSED, never redirected: a
                  malformed one is 400 and an unknown one is 404. Nothing is
                  written on either path, so the client still holds the text and
                  can retry (the panel restores it into the field and drafts it
                  to disk) — refusing costs a retry, guessing costs the truth
                  about which meeting the user was in.

                  When ABSENT (a legacy client): the active recording, or — if
                  nothing is recording — the one that stopped in the last
                  NOTE_GRACE_SECONDS, so a note posted in the moments around
                  Stop still lands in the meeting the user was in and not in the
                  next one they start. Outside that window there is no meeting
                  we can honestly attribute it to, so it is refused with 409
                  rather than filed somewhere plausible.
      t           Seconds into the meeting, or null/absent. The panel timestamps
                  its own notes (it knows when the user hit Return, not when the
                  request arrived). Anything notes.seconds() cannot vouch for —
                  absent, null, a string, NaN, an epoch stamp — falls back to
                  the recorder's elapsed while that meeting is live, otherwise
                  to the finished meeting's duration, which puts a note typed at
                  the end at the end. If that is unavailable too, the note is
                  stored UNTIMED (t: null), which is NOT the same as t: 0 —
                  see the "t" contract in notes.py.

    200 {"ok": true, "count": N, "meeting_id": id, "t": float|null,
         "recording": bool, "committed": bool}
        count      notes this meeting now has, the appended one included.
        t          the offset actually stored, null if untimed. Echoed so the
                   client can render its own copy the way the review UI will.
        recording  this meeting is the one recording right now. False tells a
                   panel its note was filed into a meeting that has ended —
                   correct, and worth knowing.
        committed  the note is in meeting.json as well as notes.jsonl. False
                   while the meeting is still recording (stop folds them in) or
                   if a background job owned the file just then — the note is
                   durably stored either way and the next read folds it in.
    400 {"error": "Empty note"} — nothing but whitespace.
    400 {"error": "bad meeting id"} — malformed meeting_id.
    404 {"error": "meeting not found"} — no such meeting folder.
    409 {"error": "Not recording"} — no meeting_id, nothing recording and
        nothing recently stopped. The note was not stored: the client must keep
        the text (the panel leaves it in the field and drafts it to disk).
    413 {"error": …, "limit": N, "length": M} — longer than notes.MAX_NOTE_CHARS.
        NOTHING was stored. This route used to answer 200 {"ok": true} here and
        quietly file the first 2000 characters, so a pasted note came back
        shorter than it went in with nothing anywhere saying so. The limit is now
        far above anything a person types and the answer is honest either way:
        a note that fits is stored to the last character, and one that does not
        is refused with the numbers, leaving the full text where the client can
        still act on it.
    500 {"error": "Could not save the note"} — the append failed.

    Every response is JSON, including the failures: the panel decodes the body
    to show why, and an escaping exception would hand it Flask's HTML error page
    to parse instead.

    NOTHING HERE EVER ANSWERS "SAVED" FOR TEXT IT DID NOT STORE IN FULL, and no
    failure path throws the text away on the client's behalf: every non-200 is a
    refusal that wrote nothing, which is what makes "keep it and retry" a
    correct response to all of them.

    Claims nothing and locks nothing on the recording path. The panel calls this
    while the audio threads run, so it must not queue behind RECORD_LOCK (held
    across the whole of REC.start, which opens device streams). The note append
    itself never touches meeting.json; only the after-the-stop case does, and by
    then there is no recording left to disturb.
    """
    data = request.get_json(force=True, silent=True) or {}
    text = str(data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "Empty note"}), 400

    meeting_id = str(data.get("meeting_id") or "").strip() or None
    if meeting_id and not MEETING_ID_RE.match(meeting_id):
        return jsonify({"error": "bad meeting id"}), 400

    # Read the recorder once: is_recording and meeting_id are two attributes,
    # and a stop landing between them would file the note under None.
    rec = REC.status()
    live_id = rec.get("meeting_id") if rec.get("recording") else None
    if meeting_id is None:
        meeting_id = live_id or _recently_stopped()
        if meeting_id is None:
            return jsonify({"error": "Not recording"}), 409

    meeting_dir = _dir_for(meeting_id)
    if not meeting_dir.exists():
        return jsonify({"error": "meeting not found"}), 404
    recording_now = meeting_id == live_id

    # notes.seconds() is the one judge of what counts as a usable offset, so the
    # server, the review UI and the summarizer all agree on which notes have a
    # place on the timeline. It returns None for anything it cannot vouch for;
    # each fallback below is tried in turn and the note is stored UNTIMED if
    # none of them produce a real one. Never 0.0 as a stand-in for "unknown" —
    # that would put the note against the first words of the meeting.
    t = notes_store.seconds(data.get("t"))
    if t is None and recording_now:
        t = notes_store.seconds(rec.get("elapsed"))
    if t is None and not recording_now:
        t = notes_store.seconds((_raw_meeting(meeting_id) or {}).get("duration"))

    try:
        count = notes_store.append_note(meeting_dir, text, t)
    except notes_store.NoteTooLong as exc:
        # Refused, not trimmed — the client still has every character. Say the
        # numbers so it can tell the user exactly how much to cut.
        return jsonify({"error": f"This note is {exc.length:,} characters — the "
                                 f"limit is {exc.limit:,}. It has NOT been saved; "
                                 "shorten it and try again.",
                        "limit": exc.limit, "length": exc.length}), 413
    except Exception as exc:
        # Broad on purpose. Every response this route can produce has to be the
        # JSON object the panel decodes; an escaping exception would become
        # Flask's HTML 500 page and the panel would report a parse failure
        # instead of "could not save", for a note the user just typed.
        app.logger.warning("could not save note for %s: %s", meeting_id, exc)
        return jsonify({"error": "Could not save the note"}), 500

    # The note is durable now. Getting it into meeting.json as well is what
    # makes it visible, and for a meeting that has already stopped nothing else
    # will: attach() ran at stop, before this note existed. Best effort — a
    # failure here costs visibility until the next read, never the note.
    committed = False
    if not recording_now:
        try:
            committed = _persist_notes(meeting_id)
        except Exception as exc:  # noqa: BLE001 — the note is already saved
            app.logger.warning("could not fold note into %s: %s", meeting_id, exc)
    return jsonify({"ok": True, "count": count, "meeting_id": meeting_id,
                    "t": t, "recording": recording_now, "committed": committed})


@app.get("/api/cues")
def cues_get():
    return jsonify({"cues": notes_store.read_cues()})


@app.put("/api/cues")
def cues_put():
    """Replace the pre-meeting cue list. Persists between meetings; a recording
    that starts later takes its own frozen copy, so editing this never rewrites
    a meeting that already happened."""
    data = request.get_json(force=True, silent=True) or {}
    cues = data.get("cues")
    if not isinstance(cues, list):
        return jsonify({"error": "cues must be a list"}), 400
    try:
        notes_store.write_cues(cues)
    except Exception as exc:  # never an HTML 500 — the panel parses JSON only
        app.logger.warning("could not save cues: %s", exc)
        return jsonify({"error": "Could not save the cues"}), 500
    return jsonify({"ok": True})


@app.get("/api/meetings/<meeting_id>/notes")
def meeting_notes(meeting_id):
    """Notes and cues for one meeting, read live from the meeting folder.

    Serves the review UI, and stays correct for a meeting that is recording
    right now — meeting.json only picks the notes up at stop.
    """
    # Validated here rather than through _meeting_dir(), whose abort(400) would
    # render Flask's HTML error page — this endpoint answers JSON or nothing.
    if not MEETING_ID_RE.match(meeting_id):
        return jsonify({"error": "bad meeting id"}), 400
    meeting_dir = _dir_for(meeting_id)
    if not meeting_dir.exists():
        return jsonify({"error": "meeting not found"}), 404
    return jsonify({"notes": notes_store.read_notes(meeting_dir),
                    "cues": notes_store.read_meeting_cues(meeting_dir)})


# -------------------------------------------------------- account & sync ----
# Optional: everything works signed-out. Signing in only enables the
# per-meeting "View on phone" sync (transcript + summary text, never audio).

@app.get("/api/auth/state")
def auth_state():
    state = insforge_client.state()
    state["providers"] = ["google"]  # rendered from the backend's enabled set
    return jsonify(state)


@app.post("/api/auth/signup")
def auth_signup():
    data = request.get_json(force=True, silent=True) or {}
    try:
        return jsonify(insforge_client.sign_up(
            (data.get("email") or "").strip(), data.get("password") or ""))
    except insforge_client.AuthError as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/auth/verify")
def auth_verify():
    data = request.get_json(force=True, silent=True) or {}
    try:
        return jsonify(insforge_client.verify_email(
            (data.get("email") or "").strip(), data.get("code") or ""))
    except insforge_client.AuthError as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/auth/signin")
def auth_signin():
    data = request.get_json(force=True, silent=True) or {}
    try:
        return jsonify(insforge_client.sign_in(
            (data.get("email") or "").strip(), data.get("password") or ""))
    except insforge_client.AuthError as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/auth/signout")
def auth_signout():
    return jsonify(insforge_client.sign_out())


@app.post("/api/auth/oauth/<provider>/start")
def auth_oauth_start(provider):
    """Open the provider sign-in in the user's default browser."""
    if provider not in ("google",):
        return jsonify({"error": "unsupported provider"}), 400
    url = insforge_client.oauth_start(provider, load_config().get("port", 5005))
    threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()
    return jsonify({"ok": True})


@app.get("/api/auth/callback")
def auth_callback():
    """OAuth redirect target (opens in the system browser)."""
    code = request.args.get("insforge_code") or request.args.get("code")
    if not code:
        return "<h3>Sign-in failed — missing code. Close this tab and try again.</h3>", 400
    try:
        insforge_client.oauth_finish(code, request.args.get("state"))
    except insforge_client.AuthError as exc:
        return f"<h3>Sign-in failed</h3><p>{exc}</p>", 400
    threading.Thread(
        target=sync.drain, args=(_read_meeting_safe,), daemon=True).start()
    return """<!doctype html><meta charset="utf-8">
    <body style="font-family:-apple-system;display:grid;place-items:center;height:90vh">
    <div style="text-align:center"><h2>✓ Signed in to MeetingScribe</h2>
    <p>You can close this tab and return to the app.</p></div>"""


@app.post("/api/meetings/<meeting_id>/sync")
def meeting_sync(meeting_id):
    """Toggle "View on phone" for one meeting.

    Every write of meeting.json here goes through the edit helpers, so each
    one is a read-modify-write under JOB_LOCK. The version that kept one dict
    across sync.push_meeting() and wrote it back afterwards published a
    snapshot from before a network call that runs for seconds — reverting
    whatever else was saved meanwhile — and did it without the lock every
    other writer of that file holds.
    """
    data = request.get_json(force=True, silent=True) or {}
    if not bool(data.get("enabled")):
        was_synced = []

        def disable(meta):
            was_synced.append(bool(meta.pop("sync", None)))
            return None

        meta, denied = _edit_meeting_json(meeting_id, disable)
        if denied:
            return jsonify(denied[0]), denied[1]
        if any(was_synced):
            try:
                sync.delete_remote(meeting_id)
            except (sync.SyncError, insforge_client.AuthError) as exc:
                app.logger.warning("could not delete synced copy of %s: %s",
                                   meeting_id, exc)
        return jsonify(meta)

    # Read outside the lock (it goes to the keychain), applied inside it, so
    # the two refusals keep the order they had: a meeting that is not ready is
    # told so before anyone is asked to sign in.
    signed_in = insforge_client.state()["signed_in"]

    def enable(meta):
        if meta.get("status") != "done" or not meta.get("turns"):
            return ({"error": "Wait for the transcript to finish first"}, 409)
        if not signed_in:
            return ({"error": "Sign in first to view meetings on your phone",
                     "needs_signin": True}, 401)
        meta["sync"] = {"enabled": True, "pushed_at": None, "error": None}
        return None

    meta, denied = _edit_meeting_json(meeting_id, enable)
    if denied:
        return jsonify(denied[0]), denied[1]
    # enabled:true is on disk before the push, so a failure here is a retry
    # sync.drain() will actually pick up (it skips anything meeting.json does
    # not say is syncing).
    try:
        sync.push_meeting(meta)
    except (sync.SyncError, insforge_client.AuthError) as exc:
        sync.enqueue(meeting_id)

        def note_error(m):
            if not (m.get("sync") or {}).get("enabled"):
                return "no longer syncing"
            m["sync"]["error"] = str(exc)
            return None

        return jsonify(_edit_meeting_json_safe(meeting_id, note_error) or meta)
    return jsonify(_edit_meeting_json_safe(meeting_id, sync.stamp_pushed) or meta)


@app.post("/api/sync/all")
def sync_all():
    """Push every finished meeting to the phone in one go (background)."""
    if not insforge_client.state()["signed_in"]:
        return jsonify({"error": "Sign in first to view meetings on your phone",
                        "needs_signin": True}), 401
    ids = [it["id"] for it in _list_meetings()
           if it.get("status") == "done"]

    def enable_sync(meta):
        """Turn syncing on for this meeting, or decline it."""
        if not meta.get("turns"):
            return "nothing to sync yet"  # non-None abandons the edit
        meta["sync"] = {"enabled": True, "pushed_at": None, "error": None}
        return None

    def run():
        done = err = 0
        for meeting_id in ids:
            try:
                # SAVED BEFORE THE UPLOAD, not after, and that ordering is the
                # whole retry story. sync.drain() re-reads meeting.json and
                # skips anything not marked enabled, so a flag that only ever
                # existed in this thread's dict meant every meeting enqueued
                # below was dropped on the next drain: the user was told
                # "N will retry" and none of them ever did.
                meta = _edit_meeting_json_safe(meeting_id, enable_sync)
                if meta is None:  # no transcript, deleted, or busy elsewhere
                    continue
                sync.push_meeting(meta)
                # The stamp goes onto a FRESH read (under JOB_LOCK), never onto
                # the dict that was uploaded. The push above can run for
                # seconds, and writing that pre-network snapshot back republished
                # a copy of the document from before it — reverting a rename, a
                # summary or a speaker fix that landed while it was in flight.
                _edit_meeting_json_safe(meeting_id, sync.stamp_pushed)
                done += 1
                SYNC_ALL["message"] = f"Synced {done} of {len(ids)}…"
            except Exception as exc:  # keep going; queue the failures
                err += 1
                sync.enqueue(meeting_id)
                app.logger.warning("sync-all failed for %s: %s", meeting_id, exc)
        SYNC_ALL.update(state="done", message=f"Synced {done} meeting(s)"
                        + (f", {err} will retry" if err else ""), synced=done)

    if SYNC_ALL.get("state") == "processing":
        return jsonify({"error": "Already syncing"}), 409
    SYNC_ALL.clear()
    SYNC_ALL.update(state="processing", message=f"Syncing {len(ids)} meeting(s)…", total=len(ids))
    threading.Thread(target=run, daemon=True, name="sync-all").start()
    return jsonify({"ok": True, "total": len(ids)})


@app.get("/api/sync/all")
def sync_all_status():
    return jsonify(SYNC_ALL or {"state": "idle"})


@app.get("/api/nudges")
def nudges():
    """The current meeting nudge (calendar / call detection), if any."""
    if NUDGES is None:
        return jsonify({"nudge": None})
    try:
        return jsonify({"nudge": NUDGES.evaluate(REC.is_recording)})
    except Exception as exc:  # nudges must never break the app
        app.logger.warning("nudge evaluation failed: %s", exc)
        return jsonify({"nudge": None})


@app.post("/api/nudges/test")
def nudge_test():
    """Fire a synthetic nudge (see NudgeEngine.test_fire) — the way to prove
    the notification chain end-to-end without scheduling a real meeting."""
    if NUDGES is None:
        return jsonify({"error": "nudges unavailable on this platform"}), 503
    title = (request.get_json(force=True, silent=True) or {}).get("title") or "Test meeting"
    return jsonify({"nudge": NUDGES.test_fire(title)})


@app.post("/api/nudges/<nudge_id>/accept")
def nudge_accept(nudge_id):
    """'Record now' on a nudge notification: start recording that meeting.

    A start, so it refuses in _refused()'s shape too — the user pressed Record
    and is owed the same sentence whichever button they pressed it on.
    """
    if NUDGES is None:
        return _refused(
            "nudges_unavailable",
            "Meeting reminders are not available on this Mac, so there is "
            "nothing to record from here. Press Record in the app instead.",
            404)
    nudge_info = NUDGES.take(nudge_id)
    if nudge_info is None:
        return _refused(
            "nudge_expired",
            "That reminder has already been answered or has expired. Press "
            "Record in the app to start this meeting.", 404)
    if REC.is_recording:
        return jsonify({"ok": True, "already_recording": True})
    return _do_record_start({
        "title": nudge_info.get("meeting_title") or "",
        "mode": "online",
    })


@app.post("/api/nudges/<nudge_id>/ack")
def nudge_ack(nudge_id):
    """'Not this meeting' on a nudge notification."""
    if NUDGES is None:
        return jsonify({"error": "nudges unavailable"}), 404
    return jsonify({"ok": NUDGES.ack(nudge_id)})


@app.get("/api/live")
def live():
    """Live-caption events since ?since=<seq> for the current recording."""
    if LIVE is None:
        return jsonify({"enabled": False, "turns": [], "partials": {}, "seq": 0})
    try:
        since = max(0, int(request.args.get("since", 0)))
    except (TypeError, ValueError):
        since = 0
    return jsonify(LIVE.snapshot(since=since))


@app.get("/api/meetings")
def meetings():
    """Paged meeting list: ?limit=40&offset=0&q=<search over titles/speakers>.

    THE SHAPE, and the contract a client has to keep to see every meeting:

        {"items": [...], "total": 412, "offset": 0, "limit": 40,
         "has_more": true, "next_offset": 40}

    `items` is one page, newest first. `total` is how many meetings match —
    the WHOLE library, not the page — and `has_more`/`next_offset` say whether
    another page exists and where it starts; a client pages until has_more is
    false. `limit` is the limit that was actually applied, which is not always
    the one that was asked for: anything above MEETINGS_PAGE_MAX is clamped,
    and echoing it is what stops a client that asked for 1,000 from reading a
    500-row answer as the complete list. items/total/offset are unchanged, so
    a client that only reads those still works.

    `q` is matched server-side against every meeting's title and speaker
    names BEFORE paging, so search reaches meetings no page has been fetched
    for. A client must therefore send the query here and not filter the page
    it happens to be holding.

    EACH ITEM, and this is the part that is new:

        {"id": "20260204-141500", "title": "…", "created": "…",
         "duration": 3612.0, "status": "done", "mode": "online",
         "sync": {…}|null, "speakers": 3,
         "brief": "Friday launch locked; QA owner still open",
         "has_summary": true, "has_transcript": true, "has_notes": false,
         "warnings": []}

    The last five are what a sidebar row draws, and they are here so that
    drawing a row costs NO further request. A client must not fetch
    /api/meetings/<id> to render a list row: that returns the entire meeting,
    transcript included, and doing it per visible row was megabytes of turns on
    the wire to decide whether to show one sentence and three badges.

      brief           the row's one line: the summary's headline, or the first
                      140 characters of its tldr, or "" when the meeting has no
                      summary. Already collapsed to single spaces.
      has_summary     a summary exists.
      has_transcript  at least one turn exists.
      has_notes       the user typed at least one note, counting notes.jsonl
                      entries meeting.json has not folded in yet.
      warnings        the engine's warning sentences, verbatim and unclassified
                      — the client splits them into capture warnings and minor
                      ones, because that phrase list belongs in one place.

    Every field is always present. `brief` is "" rather than absent when there
    is nothing to say, so a client never has to distinguish the two.

    /api/meetings/<id>/brief returns the same five for ONE meeting, for the
    moment a row finishes processing and needs its badges refreshed without
    refetching the page.
    """
    try:
        limit = max(1, min(MEETINGS_PAGE_MAX,
                           int(request.args.get("limit", MEETINGS_PAGE_DEFAULT))))
        offset = max(0, int(request.args.get("offset", 0)))
    except (TypeError, ValueError):
        return jsonify({"error": "limit/offset must be integers"}), 400
    all_items = _list_meetings(query=request.args.get("q", ""))
    page = all_items[offset:offset + limit]
    seen = offset + len(page)
    return jsonify({
        "items": page,
        "total": len(all_items),
        "offset": offset,
        "limit": limit,
        "has_more": seen < len(all_items),
        "next_offset": seen if seen < len(all_items) else None,
    })


@app.get("/api/meetings/<meeting_id>/brief")
def meeting_brief(meeting_id):
    """One meeting's row view: the brief and the badges, nothing else.

        {"id": "20260204-141500", "brief": "…", "has_summary": true,
         "has_transcript": true, "has_notes": false, "warnings": []}

    Field for field the same as an /api/meetings item (see that docstring),
    minus the columns the row already has. For refreshing ONE row — the tick a
    meeting finishes processing, when its brief and badges were last read while
    the transcript did not exist yet.

    Deliberately NOT /api/meetings/<id>: that returns the whole document, and
    it also runs _persist_notes, which takes JOB_LOCK and may rewrite
    meeting.json. Calling that once per visible row put a lock-taking write in
    the path of drawing a list, competing with the job the row is reporting on.
    Nothing here writes, and nothing here reads a turn.
    """
    folder = _meeting_dir(meeting_id)      # 400 on a bad id
    meta = _raw_meeting(meeting_id)
    if meta is None:
        abort(404, "meeting not found")
    notes_store.fold(folder, meta)         # read only, exactly as in the list
    return jsonify(dict(_row_view(meta), id=meeting_id))


@app.get("/api/meetings/<meeting_id>")
def meeting_detail(meeting_id):
    meta = _read_meeting(meeting_id)  # notes folded in for this response
    # …and written back, so the readers that do not come through here — the
    # summary, the export, the phone push — see them too. This is the path a
    # meeting killed mid-recording is repaired on: the user opens it, and the
    # notes it never got to fold in at stop become part of the document.
    _persist_notes(meeting_id)
    return jsonify(meta)


@app.delete("/api/meetings/<meeting_id>")
def meeting_delete(meeting_id):
    if REC.is_recording and REC.meeting_id == meeting_id:
        return jsonify({"error": "Meeting is currently recording"}), 409
    target = _meeting_dir(meeting_id)  # 400 on a bad id, before any claim
    # Delete is the one route that destroys a recording, so its guard has to be
    # a LOCK and not a look. Reading the three job dicts and then rmtree-ing
    # leaves a real window: a recluster claims itself under JOB_LOCK and then
    # spends the best part of a second rewriting analysis.npz and meeting.json,
    # and if its claim lands after this check it is writing into a folder that
    # is being deleted out from under it. Claiming JOBS does both halves at
    # once — it rejects whatever is already running AND stops anything new
    # starting, because process/tidy claim JOBS and recluster/summarize list it
    # as a blocker. The messages are the ones the route already returned.
    denied = _claim_job(JOBS, meeting_id, "Deleting…",
                        busy="Meeting is being processed",
                        blockers=[(SUMMARY_JOBS, "Meeting summary is being generated"),
                                  (RECLUSTER_JOBS, RECLUSTER_BUSY),
                                  (COMPRESS_JOBS, "Meeting audio is being compressed")])
    if denied:
        return jsonify(denied[0]), denied[1]
    try:
        if not target.exists():
            abort(404)
        was_synced = bool((_read_meeting_safe(meeting_id) or {}).get("sync", {}).get("enabled"))
        shutil.rmtree(target, ignore_errors=True)
        # Delete has to mean delete. The recording and its transcript are gone
        # above; the voice fingerprints they taught live in a separate file, so
        # without this they outlive the thing the user deleted. Best effort:
        # an unwritable profile store must not fail the delete.
        try:
            samples, profiles = voice_profiles.forget_meeting(meeting_id)
            if samples or profiles:
                app.logger.info("forgot %s voice sample(s) and %s profile(s) from %s",
                                samples, profiles, meeting_id)
        except Exception as exc:
            app.logger.warning("could not forget voice samples for %s: %s", meeting_id, exc)
    finally:
        # Releases the claim and, as before, drops the finished-job entries so
        # a deleted id stops showing up in /api/status. Must run even on the
        # 404 path, or that id is wedged as "processing" until restart.
        with JOB_LOCK:
            JOBS.pop(meeting_id, None)
            SUMMARY_JOBS.pop(meeting_id, None)
    if was_synced:  # remove the phone copy too (best effort, background)
        threading.Thread(target=lambda: _try_delete_remote(meeting_id),
                         daemon=True).start()
    return jsonify({"ok": True})


def _try_delete_remote(meeting_id):
    try:
        sync.delete_remote(meeting_id)
    except Exception as exc:
        app.logger.warning("could not delete synced copy of %s: %s", meeting_id, exc)


# /title and /speakers are read-modify-write on meeting.json, and a recluster
# republishes the WHOLE snapshot it loaded when it started — so an edit that
# lands mid-recluster is written, then silently reverted, and the user is left
# looking at the name they just replaced. These two are exactly the routes that
# collide in practice: the rename control and the speaker-count spinner sit on
# the same screen, and the spinner is up for about a second.
#
# Reading the flag is not sufficient on its own — a recluster could claim
# itself in the gap between the check and our write. It claims under JOB_LOCK,
# so holding JOB_LOCK across our own read-modify-write is what actually orders
# the two. The lock covers a small json.dumps and one os.replace; everything
# slow (the folder rename, transcript.md, the phone push) stays outside it.
def _edit_meeting_json(meeting_id, mutate):
    """Apply `mutate(meta)` to meeting.json without a recluster undoing it.

    -> (meta, None) on success, or (None, (payload, status)) to return.
    `mutate` may return an (payload, status) tuple to reject the edit.
    """
    with JOB_LOCK:
        if RECLUSTER_JOBS.get(meeting_id, {}).get("state") == "processing":
            return None, ({"error": RECLUSTER_BUSY}, 409)
        meta = _read_meeting(meeting_id)  # aborts 404 on an unknown id
        rejected = mutate(meta)
        if rejected is not None:
            return None, rejected
        _write_meeting(meta)
    return meta, None


def _edit_meeting_json_safe(meeting_id, mutate):
    """_edit_meeting_json for a background thread. -> the saved meta, or None.

    None covers every reason the edit did not happen: the meeting is gone or
    unreadable, a recluster owns it, the mutate declined (by returning
    anything but None), or the write failed.

    A worker cannot use the request-context version. _read_meeting aborts 404,
    and a Flask abort raised on a thread with no request to abort becomes an
    exception nobody is positioned to turn into a response; the (payload,
    status) pair it returns is HTTP vocabulary a background job has no way to
    answer in either. What matters is the half that IS shared: the whole
    read-modify-write happens under JOB_LOCK, which is the invariant
    _persist_notes documents and which every writer of meeting.json owes the
    others. Anything that reads this file, waits on a network, and then writes
    is exactly the shape that loses somebody's edit; see sync.push_if_synced.
    """
    with JOB_LOCK:
        if RECLUSTER_JOBS.get(meeting_id, {}).get("state") == "processing":
            return None
        meta = _read_meeting_safe(meeting_id)
        if meta is None or mutate(meta) is not None:
            return None
        try:
            _write_meeting(meta)
        except OSError as exc:  # read-only disk, meeting deleted mid-call…
            app.logger.warning("could not save %s: %s", meeting_id, exc)
            return None
    return meta


@app.post("/api/meetings/<meeting_id>/title")
def rename_meeting(meeting_id):
    data = request.get_json(force=True, silent=True) or {}
    title = (data.get("title") or "").strip()[:120]
    if not title:
        return jsonify({"error": "empty title"}), 400

    def mutate(meta):
        meta["title"] = title

    meta, denied = _edit_meeting_json(meeting_id, mutate)
    if denied:
        return jsonify(denied[0]), denied[1]
    _sync_folder_name(meta)
    _write_transcript_md(meta)
    _push_synced(meeting_id)
    return jsonify({"title": title})


@app.post("/api/meetings/<meeting_id>/speakers")
def rename_speaker(meeting_id):
    data = request.get_json(force=True, silent=True) or {}
    key, name = data.get("key"), (data.get("name") or "").strip()[:60]

    def mutate(meta):
        if not key or key not in meta.get("speakers", {}) or not name:
            return ({"error": "bad speaker key or name"}, 400)
        meta["speakers"][key] = name
        # A human has spoken, so whatever speaker_names inferred about this
        # cluster is now history, and history that still claims a quote
        # "proves" the old name. Drop it rather than leave it to be shown
        # next to a name it does not support.
        evidence = meta.get("speaker_names")
        if isinstance(evidence, dict) and evidence.pop(key, None) is not None:
            if not evidence:
                meta.pop("speaker_names", None)

    meta, denied = _edit_meeting_json(meeting_id, mutate)
    if denied:
        return jsonify(denied[0]), denied[1]
    # A rename is also a voice enrollment: save this cluster's centroid so the
    # person is recognized by name in future meetings. Best effort — a meeting
    # without a usable analysis cache simply doesn't enroll.
    #
    # The response carries the PROFILE, not a bare "it enrolled" flag. Which
    # profile a name lands on is a decision only the engine can make (the voice
    # picks it, names are allowed to collide, and the name is stripped and
    # truncated on the way in), so a client that tried to find it afterwards
    # would be re-deriving all of that and could still pick the wrong person.
    profile = None
    if load_config().get("voice_profiles", True):
        profile_id = voice_profiles.enroll_from_meeting(
            _dir_for(meeting_id), meta, key, name
        )
        if profile_id:
            profile = voice_profiles.get_profile(profile_id)
    _write_transcript_md(meta)
    _push_synced(meeting_id)
    return jsonify({"speakers": meta["speakers"], "voice_profile": profile})


@app.get("/api/voice-profiles")
def voice_profiles_list():
    return jsonify({"profiles": voice_profiles.list_profiles()})


@app.delete("/api/voice-profiles/<profile_id>")
def voice_profiles_delete(profile_id):
    if not voice_profiles.delete_profile(profile_id):
        return jsonify({"error": "unknown profile"}), 404
    return jsonify({"ok": True})


@app.post("/api/meetings/<meeting_id>/process")
def reprocess(meeting_id):
    if REC.is_recording and REC.meeting_id == meeting_id:
        return jsonify({"error": "Meeting is currently recording"}), 409
    meta = _read_meeting(meeting_id)
    has_audio = any(
        (_dir_for(meeting_id) / t["file"]).exists()
        for t in meta.get("tracks", {}).values()
    )
    if not has_audio:
        return jsonify({"error": "No audio files for this meeting"}), 400
    # Reprocess rewrites analysis.npz too, so it must not start while a
    # recluster is mid-write — same file, same corruption.
    denied = _claim_job(JOBS, meeting_id, "Loading model…",
                        blockers=[(SUMMARY_JOBS, "Meeting is being summarized — try again in a moment"),
                                  (RECLUSTER_JOBS, RECLUSTER_BUSY)])
    if denied:
        return jsonify(denied[0]), denied[1]
    # Re-read under JOB_LOCK rather than writing back the `meta` from above: the
    # claim was taken after that read, so a note could have landed in between
    # and this write would revert it out of meeting.json (it would survive in
    # notes.jsonl, but "durable" is not the same as "visible"). Same lock, same
    # reason, as record_stop — every read-modify-write of this file is ordered
    # by JOB_LOCK or it is racing _persist_notes.
    with JOB_LOCK:
        # _read_meeting folds in any notes the meeting.json on disk was missing
        # — a crashed recording's, typically, and Reprocess is exactly what the
        # user presses on one of those. This write is the one that stores them.
        # From here they are safe: "notes" is not a key the pipeline run claims,
        # so it carries them over from disk when it publishes.
        meta = _read_meeting(meeting_id)
        meta["status"] = "processing"
        _write_meeting(meta)
    _start_processing(meeting_id, claimed=True)
    return jsonify({"ok": True})


@app.post("/api/meetings/<meeting_id>/recluster")
def recluster(meeting_id):
    """Change the speaker count after processing — re-clusters the saved
    voice analysis in under a second, no re-transcription."""
    if REC.is_recording and REC.meeting_id == meeting_id:
        return jsonify({"error": "Meeting is currently recording"}), 409
    data = request.get_json(force=True, silent=True) or {}
    speakers = data.get("speakers")
    try:
        speakers = max(1, min(8, int(speakers))) if speakers else None
    except (TypeError, ValueError):
        speakers = None
    _read_meeting(meeting_id)  # 404 on unknown id
    # recluster_meeting is an analysis.npz WRITER, and np.savez_compressed
    # truncates the file and streams into it — two overlapping requests for the
    # same meeting would interleave and leave the cache permanently corrupt.
    # (It is derived from audio that may since have been compressed or deleted,
    # so that corruption is not always recoverable.) Claim the meeting for the
    # duration of the call, exactly like the background jobs do: a second
    # request gets 409 instead of racing, and Reprocess — the other writer of
    # that file — is blocked under the same lock rather than checked before it.
    denied = _claim_job(RECLUSTER_JOBS, meeting_id, "Re-clustering speakers…",
                        blockers=[(JOBS, "Meeting is being processed"),
                                  (SUMMARY_JOBS, "Meeting is being summarized — try again in a moment")])
    if denied:
        return jsonify(denied[0]), denied[1]

    def progress(msg):
        # The run is no longer always instant: under the mic-only fallback a
        # dropdown change can hit a track no earlier run cached, and then a full
        # ECAPA embedding pass happens INSIDE this request — seconds to minutes
        # on a long meeting. Report it on the channel the background jobs
        # already use: /api/status returns RECLUSTER_JOBS as `recluster_jobs`,
        # so a client polling it reads "Analyzing voices… 120/340" instead of
        # having to guess whether a mute spinner is slow or hung. (The server is
        # threaded, so those polls are served while this request works.)
        # .get, not [], because the claim is popped in the finally below and a
        # progress call must never be what fails the request.
        job = RECLUSTER_JOBS.get(meeting_id)
        if job is not None:
            job["message"] = msg

    try:
        meta = pipeline.recluster_meeting(_dir_for(meeting_id), speakers, progress)
        _write_transcript_md(meta)  # keep the .md consistent with the cache
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 409
    except Exception:  # noqa: BLE001 — a damaged cache must still answer JSON
        # A truncated analysis.npz raises BadZipFile/OSError/ValueError out of
        # np.load, and a truncated analysis.json raises JSONDecodeError. Those
        # escape as Flask's HTML 500 page, and the UI only reads `body.error`
        # from JSON — so the user gets a bare "INTERNAL SERVER ERROR" toast and
        # no idea that Reprocess would fix it. Never let that happen.
        app.logger.exception("recluster failed for %s", meeting_id)
        detail = traceback.format_exc().strip().splitlines()[-1]
        return jsonify({"error": "Could not re-cluster this meeting — its saved "
                                 "voice analysis looks damaged. Press Reprocess "
                                 f"to rebuild it. ({detail})"}), 500
    finally:  # released even on error, or the meeting stays blocked forever
        with JOB_LOCK:
            RECLUSTER_JOBS.pop(meeting_id, None)
    _push_synced(meeting_id)  # network I/O — outside the claim
    return jsonify(meta)


@app.post("/api/meetings/<meeting_id>/tidy")
def tidy_meeting(meeting_id):
    """Clean the transcript with the on-device model (Apple Intelligence)."""
    meta = _read_meeting(meeting_id)
    if not meta.get("turns"):
        return jsonify({"error": "No transcript to tidy yet"}), 400
    llm_ok, llm_reason = local_llm.available()
    if not llm_ok:
        return jsonify({"error": local_llm.reason_message(llm_reason)}), 400
    # Tidy rewrites meeting.json turn-by-turn; a recluster is rewriting the
    # same file's speaker labels right now, so the two must not interleave.
    denied = _claim_job(JOBS, meeting_id, "Tidying on this Mac…",
                        blockers=[(SUMMARY_JOBS, "Meeting is being summarized — try again in a moment"),
                                  (RECLUSTER_JOBS, RECLUSTER_BUSY)])
    if denied:
        return jsonify(denied[0]), denied[1]
    with JOB_LOCK:  # re-read inside the lock — see reprocess() for why
        meta = _read_meeting(meeting_id)
        meta["status"] = "processing"
        _write_meeting(meta)

    def run():
        try:
            tidy.tidy_meeting(
                _dir_for(meeting_id),
                lambda msg: JOBS[meeting_id].update(message=msg),
            )
            _write_transcript_md(_read_meeting(meeting_id))
            JOBS[meeting_id] = {"state": "done", "message": "Tidied"}
            _push_synced(meeting_id)
        except Exception:
            err = traceback.format_exc().strip().splitlines()[-1]
            JOBS[meeting_id] = {"state": "error", "message": err}
            try:  # the transcript is untouched on failure — stay "done"
                meta2 = _read_meeting(meeting_id)
                meta2["status"] = "done"
                _write_meeting(meta2)
            except Exception:
                pass

    threading.Thread(target=run, daemon=True, name=f"tidy-{meeting_id}").start()
    return jsonify({"ok": True})


@app.post("/api/meetings/<meeting_id>/tidy/undo")
def tidy_undo(meeting_id):
    backup = _meeting_dir(meeting_id) / "meeting.pretidy.json"
    if not backup.exists():
        return jsonify({"error": "No pre-tidy backup for this meeting"}), 404
    if JOBS.get(meeting_id, {}).get("state") == "processing":
        return jsonify({"error": "Meeting is being processed"}), 409
    # Undo overwrites meeting.json wholesale — never on top of a recluster
    # that is part-way through writing new labels into it.
    if RECLUSTER_JOBS.get(meeting_id, {}).get("state") == "processing":
        return jsonify({"error": RECLUSTER_BUSY}), 409
    meta = json.loads(backup.read_text(encoding="utf-8"))
    # The backup is a snapshot from before the tidy, and this publishes it
    # WHOLE — so a note taken after that snapshot would be reverted out of
    # meeting.json by an undo of something unrelated to it. Union the notes back
    # in first. Undo is about the transcript; it was never meant to un-write
    # what the user typed.
    #
    # Under JOB_LOCK because the fold and the write have to be one step: a note
    # committed by _persist_notes in between is in neither the backup nor this
    # fold, and the write would drop it straight back out of the document.
    with JOB_LOCK:
        notes_store.fold(_meeting_dir(meeting_id), meta)
        _write_meeting(meta)
    _write_transcript_md(meta)
    backup.unlink()
    _push_synced(meeting_id)
    return jsonify(meta)


def _on_device_unavailable(reason):
    """Why the on-device model cannot write this, plus the way out if there is
    one.

    Apple Intelligence is the shipped default (config.DEFAULTS), so this is the
    message a Mac with the feature switched off meets. Naming a CLI that IS
    installed turns a dead end into a two-click fix. Nothing switches by
    itself: sending a transcript to a cloud provider is the user's call, not a
    fallback to be taken quietly on their behalf.
    """
    message = local_llm.reason_message(reason)
    installed = [cli["label"] for cli in ai_cli.detect_all() if cli["installed"]]
    if installed:
        message += (f" {installed[0]} is installed on this Mac. Pick it in "
                    "Settings to use it instead.")
    return message


def _engine_precheck():
    """Can an AI write anything at all right now? -> (payload, status), or None.

    Summaries and Ask run on the same engine and now fail the same way, which
    is the point of writing it once: a missing cloud CLI is only fatal when the
    on-device model is missing too, because summarize.summarize_meeting() and
    ask.answer_question() both fall back to Apple Intelligence themselves.
    These two checks HAD drifted — Ask returned the CLI's setup instructions
    unconditionally — and since the default engine was a CLI most Macs do not
    have, the first question a new user asked was answered with an npm command.
    """
    engine = summarize._pick_engine()
    if engine != "apple":
        if ai_cli.find_cli(engine) is not None or local_llm.available()[0]:
            return None
        return ({"error": ai_cli.PROVIDERS[engine]["setup_help"],
                 "needs_claude": True}, 400)
    llm_ok, llm_reason = local_llm.available()
    if llm_ok:
        return None
    return ({"error": _on_device_unavailable(llm_reason)}, 400)


def _start_summary(meeting_id):
    """Claim the summary job and run it in the background.

    Returns None once it has started, or an (error, status) pair when it
    refuses. Shared by the route and by the auto-run on the tail of a
    transcription, so the two cannot drift in what they check, what they claim,
    what they block on, or what they clean up afterwards.
    """
    meta = _read_meeting(meeting_id)
    if not meta.get("turns"):
        return {"error": "No transcript to summarize yet"}, 400
    unusable = _engine_precheck()
    if unusable:
        return unusable
    # Summarize writes its result back into meeting.json, and a recluster is
    # rewriting that same file — and would summarize stale speaker labels.
    denied = _claim_job(SUMMARY_JOBS, meeting_id, "Summarizing…",
                        blockers=[(JOBS, "Meeting is being processed"),
                                  (RECLUSTER_JOBS, RECLUSTER_BUSY)])
    if denied:
        return denied

    def run():
        try:
            summarize.summarize_meeting(
                _dir_for(meeting_id),
                lambda msg: SUMMARY_JOBS[meeting_id].update(message=msg),
            )
            _write_transcript_md(_read_meeting(meeting_id))
            SUMMARY_JOBS[meeting_id] = {"state": "done", "message": "Summary ready"}
            _push_synced(meeting_id)
        except summarize.NeedsClaudeError as exc:
            SUMMARY_JOBS[meeting_id] = {"state": "error", "message": str(exc),
                                        "needs_claude": True}
        except Exception:
            err = traceback.format_exc().strip().splitlines()[-1]
            SUMMARY_JOBS[meeting_id] = {"state": "error", "message": err}
        finally:
            # Catch up on a rename _sync_folder_name deferred while this job
            # owned the folder. In `finally` and not in the success branch: a
            # summary that failed still left the folder named after the old
            # title, and the user's rename is not the thing that should be lost.
            # Every branch above has already moved SUMMARY_JOBS off "processing",
            # so this call is no longer the one being stood aside for.
            deferred = _read_meeting_safe(meeting_id)
            if deferred:
                try:
                    _sync_folder_name(deferred)
                except Exception as exc:  # noqa: BLE001 — cosmetic, never fatal
                    app.logger.warning("deferred folder rename failed for %s: %s",
                                       meeting_id, exc)

    threading.Thread(target=run, daemon=True, name=f"summarize-{meeting_id}").start()
    return None


@app.post("/api/meetings/<meeting_id>/summarize")
def summarize_meeting(meeting_id):
    """Generate a summary + action items with the user's own Claude (or the
    on-device model if configured). Runs in the background."""
    refused = _start_summary(meeting_id)
    if refused:
        return jsonify(refused[0]), refused[1]
    return jsonify({"ok": True})


# ------------------------------------------------------------------- ask ----
#
# ask.answer_question() has always been able to hand back the answer as it is
# written (its `on_delta` hook), but this route never passed one, so the whole
# streaming path was dead code and the user watched a spinner for the entire
# call — 8s for an ordinary question, 29s for "summarise everything" on a long
# meeting, measured on this machine. Streaming does not make the answer arrive
# any sooner; it makes the FIRST WORDS arrive at the CLI's fixed start-up cost
# instead of at the end of generation.
#
# THE WIRE FORMAT (opt-in — a caller that says nothing gets exactly the old
# response). Ask for it with {"stream": true} in the body, ?stream=1, or an
# `Accept: application/x-ndjson` header. Then:
#
#   * Any non-200 status means the old thing: a single JSON object with an
#     `error` key, and `needs_claude: true` where that applies. Every check
#     that can fail before generation starts — and every failure that happens
#     before the model has written one word — still comes back this way, so
#     the payload claudeSetupPrompt() keys on is untouched.
#   * 200 with Content-Type application/x-ndjson is a stream of JSON objects,
#     one per line, in order:
#         {"type": "delta", "text": "…"}      zero or more; plain answer text
#         {"type": "done",  "answer": "…", "citations": [{"t": 727, "quote": "…"}]}
#       or, if it fails after text has already been sent:
#         {"type": "error", "error": "…", "needs_claude": true}
#     Exactly one "done" or "error" ends the stream.
#
# The "done" event is the authority: it carries the same two fields as the
# non-streaming body (the deltas are a preview, and only "done" has citations
# validated against the turns the model was actually shown). A client can
# therefore ignore the deltas entirely, take the last line, and parse it the
# way it parses /ask today.
ASK_STREAM_MEDIA_TYPE = "application/x-ndjson"

_ASK_STREAM_TRUE = ("1", "true", "yes", "on")


def _wants_ask_stream(req, data):
    """Streaming is opt-in, so an existing caller cannot be broken by it."""
    flag = data.get("stream")
    if flag is None:
        flag = req.args.get("stream")
    if isinstance(flag, str):
        return flag.strip().lower() in _ASK_STREAM_TRUE
    if flag is not None:
        return bool(flag)
    return ASK_STREAM_MEDIA_TYPE in (req.headers.get("Accept") or "")


def _ask_error(meeting_id, exc):
    """-> (payload, status) for a failed ask. ONE definition, shared by the
    plain and the streaming path, so the needs_claude body cannot drift
    between them."""
    if isinstance(exc, ask.NeedsClaudeError):  # subclasses RuntimeError — first
        return {"error": str(exc), "needs_claude": True}, 400
    if isinstance(exc, RuntimeError):  # already user-facing prose
        return {"error": str(exc)}, 400
    # Anything else — ValueError out of min() when the budget squeezes every
    # turn out, JSONDecodeError on a truncated meeting.json, OSError on an
    # unreadable file. Flask would answer those with an HTML 500 page, and the
    # UI can only render that as "INTERNAL SERVER ERROR"; keep it JSON.
    app.logger.error("ask failed for %s", meeting_id,
                     exc_info=(type(exc), exc, exc.__traceback__))
    detail = "".join(traceback.format_exception_only(type(exc), exc)
                     ).strip().splitlines()[-1]
    return {"error": f"Could not answer that question ({detail})"}, 500


_ASK_ANSWER_KEY_RE = re.compile(r'"answer"\s*:\s*"')
_ASK_JSON_ESCAPES = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f",
                     "n": "\n", "r": "\r", "t": "\t"}


class _AskAnswerStream:
    """Turns the model's raw output into readable answer text, incrementally.

    The deltas ask.py yields are the model's literal output, and the prompt
    asks for a JSON object — so they read `{"answer": "The te`, then `am agreed
    to \\u2026`. Forwarding those verbatim would put braces, key names and
    escape sequences on the user's screen. This decodes the value of the
    "answer" field as it arrives and emits only the newly decoded characters.

    Two things it must not do. It must never emit a partial escape sequence: a
    trailing "\\" or a half-written "\\u00e" is held back until the rest lands,
    and a lone surrogate half is never emitted at all (encoding one to UTF-8
    raises, which would kill the response mid-answer). And it must cope with a
    reply that is not JSON — the model sometimes just answers in prose, which
    ask._salvage() accepts as a perfectly good uncited answer — so an output
    that does not open with `{` is passed straight through.

    Only a preview either way: the "done" event still carries the authoritative
    answer, parsed and with its citations validated.
    """

    def __init__(self):
        self._buf = ""
        self._mode = None   # None = undecided yet, "json" or "prose"
        self._pos = None    # index of the next undecoded char of the value
        self._closed = False  # the closing quote of "answer" has been seen

    def feed(self, text):
        """-> newly decoded answer text (possibly "")."""
        if self._closed or not text:
            return ""
        self._buf += text
        if self._mode is None:
            self._detect()
        if self._mode == "prose":
            out, self._buf = self._buf, ""
            return out
        if self._mode != "json":
            return ""
        if self._pos is None:
            match = _ASK_ANSWER_KEY_RE.search(self._buf)
            if match is None:
                return ""
            self._pos = match.end()
        return self._decode()

    def _detect(self):
        head = self._buf.lstrip()
        if not head:
            return
        if head[0] == "{":
            self._mode = "json"
        elif head[0] == "`":  # a ```json fence: decide on the line after it
            newline = head.find("\n")
            if newline < 0:
                if len(head) > 20:  # too long to still be a fence line
                    self._mode = "prose"
                return
            rest = head[newline + 1:].lstrip()
            if rest:
                self._mode = "json" if rest[0] == "{" else "prose"
        else:
            self._mode = "prose"

    def _decode(self):
        out, buf, i, n = [], self._buf, self._pos, len(self._buf)
        while i < n:
            ch = buf[i]
            if ch == '"':
                self._closed = True
                break
            if ch != "\\":
                out.append(ch)
                i += 1
                continue
            if i + 1 >= n:
                break  # an escape we have not seen the rest of yet
            esc = buf[i + 1]
            if esc != "u":
                out.append(_ASK_JSON_ESCAPES.get(esc, esc))
                i += 2
                continue
            if i + 6 > n:
                break
            code = _ask_hex(buf[i + 2:i + 6])
            if code is None:
                out.append(buf[i:i + 6])  # not really an escape — show it
            elif 0xD800 <= code <= 0xDBFF:  # high half of a surrogate pair
                if i + 12 > n:
                    break  # its low half has not arrived
                low = _ask_hex(buf[i + 8:i + 12]) if buf[i + 6:i + 8] == "\\u" else None
                if low is not None and 0xDC00 <= low <= 0xDFFF:
                    out.append(chr(0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00)))
                    i += 12
                    continue
                out.append("�")  # unpaired: never emit a bare surrogate
            elif 0xDC00 <= code <= 0xDFFF:
                out.append("�")
            else:
                out.append(chr(code))
            i += 6
        self._pos = i
        return "".join(out)


def _ask_hex(text):
    try:
        return int(text, 16)
    except ValueError:
        return None


# The store. Questions and answers used to live only in whichever client asked
# them: the server computed the answer, handed it over the wire once, and
# forgot it — navigate away mid-answer and the finished result landed in a
# queue nobody read. Now every ask runs as a background job (ASK_JOBS, same
# claim contract as SUMMARY_JOBS) and every settled exchange is appended to
# qa.json in the meeting's folder, so the conversation outlives the page, the
# window and the process. Clients hydrate from GET /api/meetings/<id>/qa and
# poll it while a job is live; a client that wants the words as they are
# written attaches to the same job's event stream.

_QA_LOCK = threading.Lock()  # one qa.json writer at a time (writes are tiny)


def _qa_path(meeting_id):
    return _dir_for(meeting_id) / "qa.json"


def _read_qa(meeting_id):
    """Every settled exchange for this meeting, oldest first. A missing or
    unreadable file is an empty history, never an error."""
    try:
        raw = json.loads(_qa_path(meeting_id).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (ValueError, OSError) as exc:
        app.logger.warning("unreadable qa.json for %s: %s", meeting_id, exc)
        return []
    exchanges = raw.get("exchanges") if isinstance(raw, dict) else None
    return exchanges if isinstance(exchanges, list) else []


def _append_qa(meeting_id, exchange):
    """Append one settled exchange. The path is re-resolved at write time so a
    title rename that happened during the minutes the answer took still lands
    the file in the meeting's current folder."""
    with _QA_LOCK:
        exchanges = _read_qa(meeting_id)
        exchanges.append(exchange)
        pipeline._atomic_write_text(
            _qa_path(meeting_id),
            json.dumps({"exchanges": exchanges}, ensure_ascii=False, indent=1))


def _qa_history(meeting_id):
    """Default `history` for a caller that sent none: the persisted
    conversation, in the shape ask.answer_question() takes. Clients that track
    their own thread keep sending it explicitly and win when they do."""
    history = []
    for e in _read_qa(meeting_id)[-6:]:
        if e.get("question"):
            history.append({"role": "user", "text": e["question"]})
        if e.get("answer"):
            history.append({"role": "assistant", "text": e["answer"]})
    return history


# The live side of an in-flight ask, kept OUT of ASK_JOBS on purpose:
# "partial" grows to the size of the whole answer while it is being written,
# and ASK_JOBS is shipped whole to every /api/status poller every couple of
# seconds. An entry exists exactly while its job is "processing".
_ASK_LIVE = {}  # meeting_id -> {"partial": str, "subs": [queue.Queue, ...]}
_ASK_LIVE_LOCK = threading.Lock()


def _ask_partial(meeting_id):
    with _ASK_LIVE_LOCK:
        live = _ASK_LIVE.get(meeting_id)
        return live["partial"] if live else ""


def _ask_publish(meeting_id, event):
    """Fan one ("delta"|"done"|"failed", x) event out to every subscriber and
    keep the replayable partial current. Terminal events retire the entry."""
    with _ASK_LIVE_LOCK:
        live = _ASK_LIVE.get(meeting_id)
        if live is None:
            return
        if event[0] == "delta":
            live["partial"] += event[1]
        subs = list(live["subs"])
        if event[0] != "delta":
            del _ASK_LIVE[meeting_id]
    for q in subs:
        q.put(event)


def _start_ask(meeting_id, question, history, subscriber=None):
    """Claim the ask job and run it in the background.

    Returns (None, qa_id) once started, or ((payload, status), None) when it
    refuses. `subscriber` is registered before the worker starts, so the
    caller that launched the job cannot miss its first events. Events are
    ("delta", text) — readable prose, already decoded by _AskAnswerStream —
    then exactly one ("done", result) or ("failed", (payload, status))."""
    denied = _claim_job(
        ASK_JOBS, meeting_id, "Reading the transcript…",
        busy="Still answering the last question — ask again when it finishes")
    if denied:
        return denied, None
    qa_id = str(int(time.time() * 1000))
    asked = time.time()
    ASK_JOBS[meeting_id].update(question=question, qa_id=qa_id)
    with _ASK_LIVE_LOCK:
        _ASK_LIVE[meeting_id] = {"partial": "",
                                 "subs": [subscriber] if subscriber else []}
    meeting_dir = _dir_for(meeting_id)
    if history is None:
        history = _qa_history(meeting_id)
    decoder = _AskAnswerStream()

    def on_delta(raw):
        text = decoder.feed(raw)
        if text:
            _ask_publish(meeting_id, ("delta", text))

    def run():
        try:
            result = ask.answer_question(
                meeting_dir, question, history,
                progress_cb=lambda msg: ASK_JOBS[meeting_id].update(message=msg),
                on_delta=on_delta)
            _append_qa(meeting_id, {
                "id": qa_id, "question": question,
                "answer": result.get("answer") or "",
                "citations": result.get("citations") or [],
                "asked": asked, "answered": time.time()})
            ASK_JOBS[meeting_id] = {"state": "done", "message": "Answer ready",
                                    "question": question, "qa_id": qa_id}
            _ask_publish(meeting_id, ("done", result))
        except BaseException as exc:  # noqa: BLE001 — reported to subscribers
            # BaseException, not Exception: a worker that swallowed one would
            # leave an attached request thread blocked on its queue forever.
            payload, status = _ask_error(meeting_id, exc)
            job = {"state": "error", "message": payload["error"],
                   "question": question, "qa_id": qa_id}
            if payload.get("needs_claude"):
                job["needs_claude"] = True
            ASK_JOBS[meeting_id] = job
            _ask_publish(meeting_id, ("failed", (payload, status)))

    threading.Thread(target=run, daemon=True, name=f"ask-{meeting_id}").start()
    return None, qa_id


def _ndjson(obj):
    # ensure_ascii like jsonify's, so a stray lone surrogate anywhere in the
    # payload can never raise while encoding the response body.
    return json.dumps(obj, ensure_ascii=True) + "\n"


@app.post("/api/meetings/<meeting_id>/ask")
def ask_meeting(meeting_id):
    """Answer a question about one meeting from its transcript text, with
    citations the player can seek to.

    Answers in one JSON object by default; streams the text as it is written
    when the caller opts in — see the wire format above. Either way the ask
    runs as a background job whose settled exchange is written to the
    meeting's qa.json, so navigating away no longer throws the answer away.
    {"background": true} returns immediately with {"ok": true, "qa_id": …};
    collect the answer from GET /api/meetings/<id>/qa."""
    try:
        meta = _read_meeting(meeting_id)
    except (ValueError, OSError) as exc:  # corrupt/unreadable meeting.json
        return jsonify({"error": f"Could not read this meeting: {exc}"}), 400
    if not meta.get("turns"):
        return jsonify({"error": "No transcript to ask about yet"}), 400
    data = request.get_json(force=True, silent=True) or {}
    # `or {}` covers null/""/[]/{} — but a body that is a bare JSON string or a
    # non-empty array is TRUTHY, so `data` stays a str/list and the .get below
    # raises AttributeError. Flask answers that with its HTML 500 page, which
    # breaks the one promise this route makes: every non-200 is a single JSON
    # object the UI can read `body.error` off. Check the shape instead.
    if not isinstance(data, dict):
        return jsonify({"error": "Request body must be a JSON object"}), 400
    question = (data.get("question") or "").strip()
    if not question:
        return jsonify({"error": "empty question"}), 400
    unusable = _engine_precheck()
    if unusable:
        return jsonify(unusable[0]), unusable[1]

    history = data.get("history")
    if data.get("background"):
        refused, qa_id = _start_ask(meeting_id, question, history)
        if refused:
            return jsonify(refused[0]), refused[1]
        return jsonify({"ok": True, "qa_id": qa_id})

    events = queue.Queue()
    refused, _qa = _start_ask(meeting_id, question, history, subscriber=events)
    if refused:
        return jsonify(refused[0]), refused[1]

    if not _wants_ask_stream(request, data):
        # The old synchronous shape: wait it out, answer in one object.
        while True:
            kind, payload = events.get()
            if kind == "done":
                return jsonify(payload)
            if kind == "failed":
                body, status = payload
                return jsonify(body), status

    # Block until there is something to say. That is the first delta (~4-5s of
    # CLI start-up) on the Claude path, and the finished answer on the Apple
    # one, which has no delta hook — no slower than not streaming at all.
    first = events.get()
    if first[0] == "failed":
        # Nothing was written, so the status code and body are still free:
        # answer exactly as the non-streaming path would.
        payload, status = first[1]
        return jsonify(payload), status

    def body(item):
        try:
            while True:
                kind, payload = item
                if kind == "delta":
                    yield _ndjson({"type": "delta", "text": payload})
                elif kind == "done":
                    yield _ndjson({"type": "done", **payload})
                    return
                else:  # it failed part-way through writing the answer
                    error, _status = payload
                    yield _ndjson({"type": "error", **error})
                    return
                item = events.get()
        except GeneratorExit:  # the client navigated away mid-answer; the
            raise               # job runs on and qa.json still gets the answer
        except Exception as exc:  # noqa: BLE001 — a stream must still end
            app.logger.exception("ask stream broke for %s", meeting_id)
            yield _ndjson({"type": "error",
                           "error": f"The answer was interrupted ({exc})"})

    return app.response_class(
        body(first), mimetype=ASK_STREAM_MEDIA_TYPE,
        headers={"Cache-Control": "no-cache, no-store, no-transform",
                 "X-Accel-Buffering": "no"})  # never buffer a live answer


@app.get("/api/meetings/<meeting_id>/qa")
def meeting_qa(meeting_id):
    """The conversation so far: every settled exchange, plus the live job and
    the partial answer text while one is being written. Poll this while
    job.state == "processing" — the partial grows as the model writes."""
    if not (_meeting_dir(meeting_id) / "meeting.json").exists():
        abort(404, "meeting not found")
    with JOB_LOCK:
        job = dict(ASK_JOBS[meeting_id]) if meeting_id in ASK_JOBS else None
    return jsonify({"exchanges": _read_qa(meeting_id), "job": job,
                    "partial": _ask_partial(meeting_id)})


@app.delete("/api/meetings/<meeting_id>/qa")
def clear_meeting_qa(meeting_id):
    """Forget the conversation. Refused while an answer is being written —
    its append would resurrect half of what this just deleted."""
    if not (_meeting_dir(meeting_id) / "meeting.json").exists():
        abort(404, "meeting not found")
    with JOB_LOCK:
        if ASK_JOBS.get(meeting_id, {}).get("state") == "processing":
            return jsonify({"error": "An answer is still being written — "
                                     "clear when it finishes"}), 409
    with _QA_LOCK:
        path = _qa_path(meeting_id)
        if path.exists():
            pipeline._atomic_write_text(
                path, json.dumps({"exchanges": []}, ensure_ascii=False, indent=1))
    return jsonify({"ok": True})


@app.post("/api/shutdown")
def shutdown():
    """Quit cleanly from the web UI (the .app launcher has no terminal)."""
    if REC.is_recording:
        return jsonify({"error": "Stop the recording before quitting"}), 409
    # RECLUSTER_JOBS belongs here too: a recluster is mid-write on
    # analysis.npz / meeting.json, and os._exit(0) 0.4s later would kill it
    # between np.savez_compressed's truncate and its final flush, leaving the
    # meeting's cache — and possibly its meeting.json — permanently corrupt.
    # COMPRESS_JOBS too: killing the process between audio_archive's meta
    # commit and its WAV removal is recoverable (the stranded-WAV sweep), but
    # killing it mid-_edit_meeting_json is the same truncation hazard as the
    # recluster case above.
    if any(j.get("state") == "processing"
           for j in list(JOBS.values()) + list(SUMMARY_JOBS.values())
           + list(RECLUSTER_JOBS.values()) + list(COMPRESS_JOBS.values())):
        return jsonify({"error": "A meeting is still being processed — quit when it finishes"}), 409
    threading.Timer(0.4, lambda: os._exit(0)).start()  # reply first, then exit
    return jsonify({"ok": True})


@app.get("/api/meetings/<meeting_id>/audio/<track>")
def audio(meeting_id, track):
    if track not in ("mic", "system"):
        abort(404)
    # The track's real filename lives in meeting.json — after archival it is
    # <track>.flac, not .wav (see audio_archive.py). The mimetype is set
    # explicitly because Python's mimetypes says "audio/x-flac", which
    # AVFoundation does not reliably accept; "audio/flac" streams fine
    # (verified against AVPlayer over this exact route shape).
    meta = _read_meeting(meeting_id)
    name = ((meta.get("tracks") or {}).get(track) or {}).get("file") or f"{track}.wav"
    mimetype = "audio/flac" if name.endswith(".flac") else "audio/wav"
    return send_from_directory(
        str(_meeting_dir(meeting_id)), name, mimetype=mimetype, conditional=True
    )


def _fmt_ts(seconds):
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _export_markdown(meta):
    speakers = meta.get("speakers", {})
    lines = [f"# {meta['title']}", ""]
    lines.append(
        f"*Recorded:* {meta.get('created', '')}  |  *Duration:* "
        f"{_fmt_ts(meta.get('duration') or 0)}  |  *Mode:* {meta.get('mode')}"
    )
    summary_md = summarize.to_markdown(meta.get("summary"))
    if summary_md:
        lines += ["", summary_md.rstrip()]
    stats = meta.get("stats", {})
    per = stats.get("per_speaker", {})
    if per:
        lines += ["", "## Speaking stats", ""]
        lines.append("| Speaker | Talk time | Share | Words | WPM | Questions | Fillers |")
        lines.append("|---|---|---|---|---|---|---|")
        for key, st in per.items():
            top = ", ".join(f"{w}×{c}" for w, c in list(st["fillers"].items())[:3]) or "—"
            lines.append(
                f"| {speakers.get(key, key)} | {_fmt_ts(st['seconds'])} "
                f"| {round(st['share'] * 100)}% | {st['words']} | {st['wpm']} "
                f"| {st['questions']} | {top} |"
            )
    lines += ["", "## Transcript", ""]
    for turn in meta.get("turns", []):
        name = speakers.get(turn["speaker"], turn["speaker"])
        lines.append(f"**{name}** `[{_fmt_ts(turn['start'])}]`  {turn['text']}")
        lines.append("")
    return "\n".join(lines)


def _export_text(meta):
    speakers = meta.get("speakers", {})
    lines = [meta["title"], "=" * len(meta["title"]), ""]
    for turn in meta.get("turns", []):
        name = speakers.get(turn["speaker"], turn["speaker"])
        lines.append(f"[{_fmt_ts(turn['start'])}] {name}: {turn['text']}")
    return "\n".join(lines)


WAVEFORM_BINS = 700


def _track_peaks(path, offset, total_s, bins):
    """Per-timeline-bin peak (0..1) for one WAV, reading block-by-block."""
    import numpy as np
    import soundfile as sf

    peaks = [0.0] * bins
    with sf.SoundFile(str(path)) as f:
        frames, sr = f.frames, f.samplerate
        if not frames or not total_s:
            return peaks
        for b in range(bins):
            t0 = b / bins * total_s - offset
            t1 = (b + 1) / bins * total_s - offset
            i0, i1 = max(0, int(t0 * sr)), min(frames, int(t1 * sr))
            if i1 <= i0:
                continue
            f.seek(i0)
            block = f.read(i1 - i0, dtype="float32", always_2d=True)
            if block.size:
                peaks[b] = float(np.abs(block).max())
    top = max(peaks) or 1.0
    return [round(p / top, 3) for p in peaks]


@app.get("/api/meetings/<meeting_id>/waveform")
def waveform(meeting_id):
    """Downsampled audio peaks for the transport bar (computed once, cached)."""
    folder = _meeting_dir(meeting_id)
    cache = folder / "waveform.json"
    if cache.exists():
        return app.response_class(cache.read_text(encoding="utf-8"), mimetype="application/json")
    meta = _read_meeting(meeting_id)
    total = float(meta.get("duration") or 0.0)
    out = {"bins": WAVEFORM_BINS, "duration": total, "tracks": {}}
    for key, tr in (meta.get("tracks") or {}).items():
        path = folder / tr.get("file", "")
        if not path.exists() or not total:
            continue
        try:
            out["tracks"][key] = _track_peaks(path, float(tr.get("start_offset") or 0.0), total, WAVEFORM_BINS)
        except Exception as exc:
            app.logger.warning("waveform failed for %s/%s: %s", meeting_id, key, exc)
    if out["tracks"]:
        mix = [max(vals) for vals in zip(*out["tracks"].values())]
        out["tracks"]["mix"] = [round(v, 3) for v in mix]
    body = json.dumps(out)
    try:
        tmp = cache.with_suffix(".json.tmp")
        tmp.write_text(body, encoding="utf-8")
        tmp.replace(cache)  # atomic — a crash mid-write can't corrupt the cache
    except OSError as exc:
        app.logger.warning("could not cache waveform for %s: %s", meeting_id, exc)
    return app.response_class(body, mimetype="application/json")


@app.post("/api/meetings/<meeting_id>/reveal")
def reveal_md(meeting_id):
    """Open the saved transcript.md in the OS file browser (Finder/Explorer)."""
    path = _meeting_dir(meeting_id) / "transcript.md"
    if not path.exists():  # write it on demand if somehow missing
        try:
            _write_transcript_md(_read_meeting(meeting_id))
        except Exception:
            pass
    if not path.exists():
        return jsonify({"error": "No transcript saved for this meeting yet"}), 404
    try:
        if sys.platform == "darwin":
            subprocess.run(["open", "-R", str(path)], check=False)
        elif sys.platform == "win32":
            subprocess.run(["explorer", f"/select,{path}"], check=False)
        else:
            subprocess.run(["xdg-open", str(path.parent)], check=False)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    return jsonify({"ok": True, "path": str(path)})


@app.get("/api/meetings/<meeting_id>/export")
def export(meeting_id):
    meta = _read_meeting(meeting_id)
    fmt = request.args.get("fmt", "md")
    if fmt == "txt":
        body, ext, mime = _export_text(meta), "txt", "text/plain"
    else:
        body, ext, mime = _export_markdown(meta), "md", "text/markdown"
    safe_title = re.sub(r"[^\w\- ]+", "", meta["title"]).strip() or meeting_id
    return app.response_class(
        body,
        mimetype=mime,
        headers={
            "Content-Disposition": f'attachment; filename="{safe_title}.{ext}"'
        },
    )


# ------------------------------------------------------------------- main ----

def _recover_interrupted():
    """Mark meetings left mid-flight by a previous crash so the UI offers Reprocess.

    And rescue what was typed into them. A recording that ends at a crash never
    reaches the stop path, so the notes the user took during it are sitting in
    notes.jsonl having never been folded into the meeting — the one part of that
    meeting that cannot be recreated from the audio, invisible in the only
    document anything reads. attach() folds in both those and the cues the
    meeting froze at start; it is a union, so a meeting that did manage part of
    the fold keeps what it had.
    """
    for item in _list_meetings():
        if item["status"] in ("recording", "processing"):
            meta = _read_meeting(item["id"])
            meta["status"] = "error"
            meta["error"] = "Interrupted — press Reprocess to transcribe the saved audio."
            notes_store.attach(_dir_for(item["id"]), meta)
            _write_meeting(meta)


def _backfill_notes():
    """Fold notes.jsonl into meeting.json wherever the two disagree.

    The startup sweep behind "a note that reached the disk always surfaces".
    _recover_interrupted covers the meeting a crash caught mid-flight; this
    covers every other way the two files can come apart — a note that arrived
    while the stop write was in flight, a meeting.json restored from an older
    copy, a folder carried over from a build that stored notes and nothing else.

    Writes only where something is genuinely missing (see _persist_notes), so
    the usual cost is one small read per meeting and no writes at all.
    """
    for item in _list_meetings():
        try:
            _persist_notes(item["id"])
        except Exception:  # noqa: BLE001 — never block startup on one meeting
            pass


def _backfill_transcripts():
    """Write transcript.md for any finished meeting that doesn't have one yet
    (e.g. transcribed before this feature existed)."""
    for item in _list_meetings():
        if item["status"] != "done":
            continue
        if (_dir_for(item["id"]) / "transcript.md").exists():
            continue
        try:
            _write_transcript_md(_read_meeting(item["id"]))
        except Exception:
            pass


def _backfill_folder_names():
    """Rename folders from earlier versions to the '<title> — <id>' form."""
    for item in _list_meetings():
        if item["status"] in ("recording", "processing"):
            continue
        try:
            _sync_folder_name(_read_meeting(item["id"]))
        except Exception:
            pass


if __name__ == "__main__":
    cfg = load_config()
    port = int(cfg.get("port", 5005))
    # Put the bundled, pre-built Speech/AI helpers in place before anything
    # tries to use them (packaged app; no-op from a source checkout).
    try:
        import swift_helpers
        swift_helpers.install_all_prebuilt()
    except Exception as _exc:  # never block startup on this
        app.logger.warning("installing pre-built helpers failed: %s", _exc)
    # And the bundled speaker model, for the same reason: diarization wants a
    # plain file under <DATA_DIR>/models, not one sealed inside the bundle.
    # Costs one 84 MB copy on the first launch after an install, nothing after.
    try:
        seed_ecapa_onnx()
    except Exception as _exc:  # a failure here is diagnosed at the point of use
        app.logger.warning("installing the bundled speaker model failed: %s", _exc)
    _recover_interrupted()
    _backfill_notes()
    _backfill_transcripts()
    _backfill_folder_names()
    if macos_audio is not None:
        # Put the sound output back if a previous run died mid-recording,
        # and make sure it is restored however this process exits.
        try:
            if macos_audio.restore_routing():
                print("  Restored the sound output left switched by an interrupted recording.")
        except Exception:
            pass
        atexit.register(lambda: macos_audio.restore_routing())
        # Driverless tap era: the "MeetingScribe Output" multi-output device
        # belonged to the retired BlackHole routing hack — clear a leftover.
        # build=False: never compile the helper on the startup path.
        try:
            import audio_recorder as _rec
            if _rec.system_source(build=False) == "tap" and macos_audio.cleanup_legacy_aggregate():
                print("  Removed the legacy 'MeetingScribe Output' multi-output device.")
        except Exception:
            pass
    if cfg.get("open_browser", True) and not os.environ.get("MEETINGSCRIBE_NO_BROWSER"):
        threading.Timer(1.2, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    # Retry any phone-sync pushes that failed while offline.
    threading.Timer(5.0, lambda: sync.drain(_read_meeting_safe)).start()
    # Archive any finished meetings the last run didn't get to (opt-in;
    # includes the orphan-temp and stranded-WAV recovery passes). Late enough
    # to never compete with first paint or the model survey.
    threading.Timer(20.0, _compress_sweep_async).start()
    print(f"\n  MeetingScribe running at http://127.0.0.1:{port}\n")
    app.run(host="127.0.0.1", port=port, threaded=True, debug=False, use_reloader=False)
