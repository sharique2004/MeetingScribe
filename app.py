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
import threading
import traceback
import webbrowser
from datetime import datetime

from flask import Flask, abort, jsonify, redirect, request, send_from_directory

import ask
import insforge_client
import live_captions
import local_llm
import pipeline
import summarize
import sync
import tech_vocabulary
import tidy
from audio_recorder import MeetingRecorder
from config import BASE_DIR, RECORDINGS_DIR, load_config

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
RECORD_LOCK = threading.Lock()  # serializes start/stop transitions across requests
JOB_LOCK = threading.Lock()  # makes "check job state then register" atomic
SYNC_ALL = {}  # progress of a "sync all to phone" run
LIVE = None  # live_captions.LiveSession for the current/last recording

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
    return json.loads(path.read_text(encoding="utf-8"))


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


def _read_meeting_safe(meeting_id):
    """Like _read_meeting but returns None instead of aborting (for
    background threads, where a Flask abort would be meaningless)."""
    try:
        path = _dir_for(meeting_id) / "meeting.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None


def _push_synced(meeting_id):
    """Re-upload a synced meeting after an edit. No-op when not synced."""
    sync.push_if_synced(_read_meeting_safe, _write_meeting, meeting_id)


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
_LIST_CACHE = {}


def _list_meetings(query=""):
    items = []
    if not RECORDINGS_DIR.exists():
        return items
    seen = set()
    query = (query or "").strip().lower()
    for d in RECORDINGS_DIR.iterdir():
        meta_path = d / "meeting.json"
        if not d.is_dir() or not FOLDER_ID_RE.match(d.name) or not meta_path.exists():
            continue
        try:
            mtime = meta_path.stat().st_mtime
        except OSError:
            continue
        seen.add(d.name)
        cached = _LIST_CACHE.get(d.name)
        if cached and cached[0] == mtime:
            item, haystack = cached[1], cached[2]
        else:
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                continue
            item = {k: meta.get(k) for k in LIST_FIELDS}
            item["speakers"] = len(meta.get("speakers") or {})
            haystack = " ".join(
                [str(meta.get("title") or "")] + list((meta.get("speakers") or {}).values())
            ).lower()
            _LIST_CACHE[d.name] = (mtime, item, haystack)
        if query and query not in haystack:
            continue
        items.append(item)
    for stale in set(_LIST_CACHE) - seen:  # renamed/deleted folders
        del _LIST_CACHE[stale]
    items.sort(key=lambda m: m["id"], reverse=True)
    return items


# ------------------------------------------------------------- processing ----

def _start_processing(meeting_id, claimed=False):
    if not claimed:  # record_stop path: register the job now
        JOBS[meeting_id] = {"state": "processing", "message": "Loading model…"}

    def update(msg):
        JOBS[meeting_id]["message"] = msg

    def run():
        try:
            pipeline.process_meeting(_dir_for(meeting_id), update)
            meta = _read_meeting(meeting_id)
            _write_transcript_md(meta)
            JOBS[meeting_id] = {"state": "done", "message": "Complete"}
            _sync_folder_name(meta)  # catch up on renames deferred mid-job
            _push_synced(meeting_id)
        except Exception:
            err = traceback.format_exc().strip().splitlines()[-1]
            JOBS[meeting_id] = {"state": "error", "message": err}
            try:
                meta = _read_meeting(meeting_id)
                meta["status"] = "error"
                meta["error"] = err
                _write_meeting(meta)
            except Exception:
                pass

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
        recluster_jobs = dict(RECLUSTER_JOBS)
    return jsonify({"recorder": REC.status(), "jobs": jobs,
                    "summary_jobs": summary_jobs,
                    "recluster_jobs": recluster_jobs})


@app.get("/api/devices")
def devices():
    # auto_route_macos is a user setting, so preflight has to be told the real
    # value — otherwise the UI reports "routes automatically" to someone who
    # turned routing off and would silently record nothing but their own mic.
    cfg = load_config()
    return jsonify(REC.preflight(auto_route=bool(cfg.get("auto_route_macos", True))))


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


@app.get("/api/calendar/today")
def calendar_today():
    """Today's calendar events (for title suggestions and auto-naming)."""
    if calendar_events is None:
        return jsonify({"available": False, "events": [], "error": "not supported here"})
    return jsonify(calendar_events.todays_events())


@app.post("/api/record/start")
def record_start():
    data = request.get_json(force=True, silent=True) or {}
    return _do_record_start(data)


def _do_record_start(data):
    """Start a recording. Shared by the record button and nudge-accept."""
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
            return jsonify({"error": "Already recording"}), 409
        meeting_id = datetime.now().strftime("%Y%m%d-%H%M%S")
        if _dir_for(meeting_id).exists():  # two starts within the same second
            return jsonify({"error": "Just started another recording — wait a second"}), 409
        meeting_dir = RECORDINGS_DIR / _folder_name_for({"id": meeting_id, "title": title})
        meeting_dir.mkdir(parents=True)

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
            return jsonify({"error": str(exc)}), 500
    if expected is None and event and event.get("attendees"):
        # Calendar attendees excludes you. Online mode wants "other speakers";
        # in-person wants the total around the shared mic, so add yourself.
        others = int(event["attendees"])
        if data.get("mode") == "inperson":
            others += 1
        expected = max(1, min(8, others))

    meta = {
        "id": meeting_id,
        "title": title or "Meeting " + datetime.now().strftime("%d %b %Y, %H:%M"),
        "created": datetime.now().isoformat(timespec="seconds"),
        "mode": "inperson" if data.get("mode") == "inperson" else "online",
        "expected_speakers": expected,
        "status": "recording",
        "tracks": info["tracks"],
        "warnings": info["warnings"],
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
    _write_meeting(meta)
    return jsonify(meta)


@app.post("/api/record/stop")
def record_stop():
    with RECORD_LOCK:
        if not REC.is_recording:
            return jsonify({"error": "Not recording"}), 409
        meeting_id = REC.meeting_id
        result = REC.stop()
        if LIVE is not None:
            LIVE.stop()  # helpers finalize; captions stay readable meanwhile
    meta = _read_meeting(meeting_id)
    for key, tr in result["tracks"].items():
        meta["tracks"].setdefault(key, {}).update(tr)
    meta["duration"] = result["duration"]
    meta["warnings"] = meta.get("warnings", []) + result["warnings"]
    meta["status"] = "processing"
    _sync_folder_name(meta)  # folder picks up the title now the WAVs are closed
    _write_meeting(meta)
    _start_processing(meeting_id)
    return jsonify(meta)


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
    """Toggle "View on phone" for one meeting."""
    data = request.get_json(force=True, silent=True) or {}
    enabled = bool(data.get("enabled"))
    meta = _read_meeting(meeting_id)
    if enabled:
        if meta.get("status") != "done" or not meta.get("turns"):
            return jsonify({"error": "Wait for the transcript to finish first"}), 409
        if not insforge_client.state()["signed_in"]:
            return jsonify({"error": "Sign in first to view meetings on your phone",
                            "needs_signin": True}), 401
        meta["sync"] = {"enabled": True, "pushed_at": None, "error": None}
        _write_meeting(meta)
        try:
            sync.push_meeting(meta)
            meta["sync"]["pushed_at"] = datetime.now().isoformat(timespec="seconds")
        except (sync.SyncError, insforge_client.AuthError) as exc:
            meta["sync"]["error"] = str(exc)
            sync.enqueue(meeting_id)
        _write_meeting(meta)
    else:
        had_sync = bool(meta.pop("sync", None))
        _write_meeting(meta)
        if had_sync:
            try:
                sync.delete_remote(meeting_id)
            except (sync.SyncError, insforge_client.AuthError) as exc:
                app.logger.warning("could not delete synced copy of %s: %s",
                                   meeting_id, exc)
    return jsonify(meta)


@app.post("/api/sync/all")
def sync_all():
    """Push every finished meeting to the phone in one go (background)."""
    if not insforge_client.state()["signed_in"]:
        return jsonify({"error": "Sign in first to view meetings on your phone",
                        "needs_signin": True}), 401
    ids = [it["id"] for it in _list_meetings()
           if it.get("status") == "done"]

    def run():
        done = err = 0
        for meeting_id in ids:
            try:
                meta = _read_meeting_safe(meeting_id)
                if not meta or not meta.get("turns"):
                    continue
                meta["sync"] = {"enabled": True, "pushed_at": None, "error": None}
                sync.push_meeting(meta)
                meta["sync"]["pushed_at"] = datetime.now().isoformat(timespec="seconds")
                _write_meeting(meta)
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


@app.post("/api/nudges/<nudge_id>/accept")
def nudge_accept(nudge_id):
    """'Record now' on a nudge notification: start recording that meeting."""
    if NUDGES is None:
        return jsonify({"error": "nudges unavailable"}), 404
    nudge_info = NUDGES.take(nudge_id)
    if nudge_info is None:
        return jsonify({"error": "unknown or expired nudge"}), 404
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
    """Paged meeting list: ?limit=40&offset=0&q=<search over titles/speakers>."""
    try:
        limit = max(1, min(500, int(request.args.get("limit", 40))))
        offset = max(0, int(request.args.get("offset", 0)))
    except (TypeError, ValueError):
        return jsonify({"error": "limit/offset must be integers"}), 400
    all_items = _list_meetings(query=request.args.get("q", ""))
    return jsonify({
        "items": all_items[offset:offset + limit],
        "total": len(all_items),
        "offset": offset,
    })


@app.get("/api/meetings/<meeting_id>")
def meeting_detail(meeting_id):
    return jsonify(_read_meeting(meeting_id))


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
                                  (RECLUSTER_JOBS, RECLUSTER_BUSY)])
    if denied:
        return jsonify(denied[0]), denied[1]
    try:
        if not target.exists():
            abort(404)
        was_synced = bool((_read_meeting_safe(meeting_id) or {}).get("sync", {}).get("enabled"))
        shutil.rmtree(target, ignore_errors=True)
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

    meta, denied = _edit_meeting_json(meeting_id, mutate)
    if denied:
        return jsonify(denied[0]), denied[1]
    _write_transcript_md(meta)
    _push_synced(meeting_id)
    return jsonify({"speakers": meta["speakers"]})


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
    _write_meeting(meta)
    _write_transcript_md(meta)
    backup.unlink()
    _push_synced(meeting_id)
    return jsonify(meta)


@app.post("/api/meetings/<meeting_id>/summarize")
def summarize_meeting(meeting_id):
    """Generate a summary + action items with the user's own Claude (or the
    on-device model if configured). Runs in the background."""
    meta = _read_meeting(meeting_id)
    if not meta.get("turns"):
        return jsonify({"error": "No transcript to summarize yet"}), 400
    if summarize._pick_engine() == "claude":
        if summarize.find_claude() is None:
            return jsonify({"error": summarize._CLAUDE_SETUP_HELP,
                            "needs_claude": True}), 400
    else:
        llm_ok, llm_reason = local_llm.available()
        if not llm_ok:
            return jsonify({"error": local_llm.reason_message(llm_reason)}), 400
    # Summarize writes its result back into meeting.json, and a recluster is
    # rewriting that same file — and would summarize stale speaker labels.
    denied = _claim_job(SUMMARY_JOBS, meeting_id, "Summarizing…",
                        blockers=[(JOBS, "Meeting is being processed"),
                                  (RECLUSTER_JOBS, RECLUSTER_BUSY)])
    if denied:
        return jsonify(denied[0]), denied[1]

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


def _ask_events(meeting_dir, question, history):
    """Run one ask on a worker thread, yielding ("delta"|"done"|"failed", x).

    The work has to leave the request thread so this one can decide what the
    HTTP response even is from the FIRST item: a failure before any text was
    written is still a plain JSON error with its real status code, and only
    once words are flowing does the reply become a stream.
    """
    events = queue.Queue()
    answer = _AskAnswerStream()

    def on_delta(raw):
        text = answer.feed(raw)
        if text:
            events.put(("delta", text))

    def run():
        try:
            events.put(("done", ask.answer_question(meeting_dir, question,
                                                    history, on_delta=on_delta)))
        except BaseException as exc:  # noqa: BLE001 — reported to the client
            # BaseException, not Exception: a worker that swallowed one would
            # leave the request thread blocked on this queue forever.
            events.put(("failed", exc))

    threading.Thread(target=run, daemon=True, name="ask").start()
    while True:
        item = events.get()
        yield item
        if item[0] != "delta":
            return


def _ndjson(obj):
    # ensure_ascii like jsonify's, so a stray lone surrogate anywhere in the
    # payload can never raise while encoding the response body.
    return json.dumps(obj, ensure_ascii=True) + "\n"


@app.post("/api/meetings/<meeting_id>/ask")
def ask_meeting(meeting_id):
    """Answer a question about one meeting from its transcript text, with
    citations the player can seek to.

    Answers in one JSON object by default; streams the text as it is written
    when the caller opts in — see the wire format above."""
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
    if summarize._pick_engine() == "claude":
        if summarize.find_claude() is None:
            return jsonify({"error": summarize._CLAUDE_SETUP_HELP,
                            "needs_claude": True}), 400
    else:
        llm_ok, llm_reason = local_llm.available()
        if not llm_ok:
            return jsonify({"error": local_llm.reason_message(llm_reason)}), 400

    meeting_dir, history = _dir_for(meeting_id), data.get("history")
    if not _wants_ask_stream(request, data):
        try:
            result = ask.answer_question(meeting_dir, question, history)
        except Exception as exc:  # noqa: BLE001 — never break the JSON contract
            payload, status = _ask_error(meeting_id, exc)
            return jsonify(payload), status
        return jsonify(result)

    events = _ask_events(meeting_dir, question, history)
    # Block until there is something to say. That is the first delta (~4-5s of
    # CLI start-up) on the Claude path, and the finished answer on the Apple
    # one, which has no delta hook — no slower than not streaming at all.
    first = next(events)
    if first[0] == "failed":
        # Nothing was written, so the status code and body are still free:
        # answer exactly as the non-streaming path would.
        payload, status = _ask_error(meeting_id, first[1])
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
                    error, _status = _ask_error(meeting_id, payload)
                    yield _ndjson({"type": "error", **error})
                    return
                item = next(events)
        except GeneratorExit:  # the browser navigated away mid-answer
            raise
        except Exception as exc:  # noqa: BLE001 — a stream must still end
            app.logger.exception("ask stream broke for %s", meeting_id)
            yield _ndjson({"type": "error",
                           "error": f"The answer was interrupted ({exc})"})

    return app.response_class(
        body(first), mimetype=ASK_STREAM_MEDIA_TYPE,
        headers={"Cache-Control": "no-cache, no-store, no-transform",
                 "X-Accel-Buffering": "no"})  # never buffer a live answer


@app.post("/api/shutdown")
def shutdown():
    """Quit cleanly from the web UI (the .app launcher has no terminal)."""
    if REC.is_recording:
        return jsonify({"error": "Stop the recording before quitting"}), 409
    # RECLUSTER_JOBS belongs here too: a recluster is mid-write on
    # analysis.npz / meeting.json, and os._exit(0) 0.4s later would kill it
    # between np.savez_compressed's truncate and its final flush, leaving the
    # meeting's cache — and possibly its meeting.json — permanently corrupt.
    if any(j.get("state") == "processing"
           for j in list(JOBS.values()) + list(SUMMARY_JOBS.values())
           + list(RECLUSTER_JOBS.values())):
        return jsonify({"error": "A meeting is still being processed — quit when it finishes"}), 409
    threading.Timer(0.4, lambda: os._exit(0)).start()  # reply first, then exit
    return jsonify({"ok": True})


@app.get("/api/meetings/<meeting_id>/audio/<track>")
def audio(meeting_id, track):
    if track not in ("mic", "system"):
        abort(404)
    return send_from_directory(
        str(_meeting_dir(meeting_id)), f"{track}.wav", conditional=True
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
    """Mark meetings left mid-flight by a previous crash so the UI offers Reprocess."""
    for item in _list_meetings():
        if item["status"] in ("recording", "processing"):
            meta = _read_meeting(item["id"])
            meta["status"] = "error"
            meta["error"] = "Interrupted — press Reprocess to transcribe the saved audio."
            _write_meeting(meta)


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
    _recover_interrupted()
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
    if cfg.get("open_browser", True) and not os.environ.get("MEETINGSCRIBE_NO_BROWSER"):
        threading.Timer(1.2, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    # Retry any phone-sync pushes that failed while offline.
    threading.Timer(5.0, lambda: sync.drain(_read_meeting_safe)).start()
    print(f"\n  MeetingScribe running at http://127.0.0.1:{port}\n")
    app.run(host="127.0.0.1", port=port, threaded=True, debug=False, use_reloader=False)
