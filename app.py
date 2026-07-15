"""MeetingScribe — local meeting recorder, transcriber and conversation coach.

Run:  venv\\Scripts\\python.exe app.py   (or double-click run.bat)
"""

import atexit
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import traceback
import webbrowser
from datetime import datetime

from flask import Flask, abort, jsonify, request, send_from_directory

import local_llm
import pipeline
import summarize
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

try:  # mock-interview coach, served at /practice (its own UI + /api/practice/*)
    import practice
    app.register_blueprint(practice.bp)
except Exception as exc:  # missing claude/transcribe deps must not break recording
    app.logger.warning("practice mode unavailable: %s", exc)

REC = MeetingRecorder()
JOBS = {}  # meeting_id -> {"state": queued|processing|done|error, "message": str}
SUMMARY_JOBS = {}  # meeting_id -> {"state": processing|done|error, "message": str}
RECORD_LOCK = threading.Lock()  # serializes start/stop transitions across requests

MEETING_ID_RE = re.compile(r"^\d{8}-\d{6}$")
# Folders are "<title> — <id>" so meetings are spottable in Finder, or a bare
# id before a title exists. The id suffix keeps names unique and the API keyed
# by id alone.
FOLDER_ID_RE = re.compile(r"^(?:.* — )?(\d{8}-\d{6})$")
LIST_FIELDS = ("id", "title", "created", "duration", "status", "mode")


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
    those catch up at stop / job-end / next startup."""
    if REC.is_recording and REC.meeting_id == meta["id"]:
        return
    if JOBS.get(meta["id"], {}).get("state") == "processing":
        return
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
    path = _dir_for(meta["id"]) / "meeting.json"
    path.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")


def _write_transcript_md(meta):
    """Save a human-readable transcript.md beside the audio so the meeting can
    be read later in any editor, with no app or meeting.json needed. Best-effort
    — never let a file-write failure break processing."""
    if not meta.get("turns"):
        return
    try:
        path = _dir_for(meta["id"]) / "transcript.md"
        path.write_text(_export_markdown(meta), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001 — saving the .md is non-critical
        app.logger.warning("could not write transcript.md for %s: %s", meta.get("id"), exc)


def _list_meetings():
    items = []
    if not RECORDINGS_DIR.exists():
        return items
    for d in RECORDINGS_DIR.iterdir():
        meta_path = d / "meeting.json"
        if not d.is_dir() or not FOLDER_ID_RE.match(d.name) or not meta_path.exists():
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        item = {k: meta.get(k) for k in LIST_FIELDS}
        item["speakers"] = len(meta.get("speakers") or {})
        items.append(item)
    items.sort(key=lambda m: m["id"], reverse=True)
    return items


# ------------------------------------------------------------- processing ----

def _start_processing(meeting_id):
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

@app.get("/")
def index():
    return send_from_directory(str(BASE_DIR / "templates"), "index.html")


@app.get("/api/status")
def status():
    return jsonify({"recorder": REC.status(), "jobs": JOBS, "summary_jobs": SUMMARY_JOBS})


@app.get("/api/devices")
def devices():
    return jsonify(REC.preflight())


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

    with RECORD_LOCK:
        if REC.is_recording:
            return jsonify({"error": "Already recording"}), 409
        meeting_id = datetime.now().strftime("%Y%m%d-%H%M%S")
        if _dir_for(meeting_id).exists():  # two starts within the same second
            return jsonify({"error": "Just started another recording — wait a second"}), 409
        meeting_dir = RECORDINGS_DIR / _folder_name_for({"id": meeting_id, "title": title})
        meeting_dir.mkdir(parents=True)
        try:
            auto_route = bool(load_config().get("auto_route_macos", True))
            info = REC.start(meeting_dir, meeting_id, auto_route=auto_route)
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
    if event:
        meta["calendar_event"] = {"title": event["title"], "start": event["start"]}
    _write_meeting(meta)
    return jsonify(meta)


@app.post("/api/record/stop")
def record_stop():
    with RECORD_LOCK:
        if not REC.is_recording:
            return jsonify({"error": "Not recording"}), 409
        meeting_id = REC.meeting_id
        result = REC.stop()
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


@app.get("/api/meetings")
def meetings():
    return jsonify(_list_meetings())


@app.get("/api/meetings/<meeting_id>")
def meeting_detail(meeting_id):
    return jsonify(_read_meeting(meeting_id))


@app.delete("/api/meetings/<meeting_id>")
def meeting_delete(meeting_id):
    if REC.is_recording and REC.meeting_id == meeting_id:
        return jsonify({"error": "Meeting is currently recording"}), 409
    if JOBS.get(meeting_id, {}).get("state") == "processing":
        return jsonify({"error": "Meeting is being processed"}), 409
    if SUMMARY_JOBS.get(meeting_id, {}).get("state") == "processing":
        return jsonify({"error": "Meeting summary is being generated"}), 409
    target = _meeting_dir(meeting_id)
    if not target.exists():
        abort(404)
    shutil.rmtree(target, ignore_errors=True)
    JOBS.pop(meeting_id, None)
    SUMMARY_JOBS.pop(meeting_id, None)
    return jsonify({"ok": True})


@app.post("/api/meetings/<meeting_id>/title")
def rename_meeting(meeting_id):
    data = request.get_json(force=True, silent=True) or {}
    title = (data.get("title") or "").strip()[:120]
    if not title:
        return jsonify({"error": "empty title"}), 400
    meta = _read_meeting(meeting_id)
    meta["title"] = title
    _write_meeting(meta)
    _sync_folder_name(meta)
    _write_transcript_md(meta)
    return jsonify({"title": title})


@app.post("/api/meetings/<meeting_id>/speakers")
def rename_speaker(meeting_id):
    data = request.get_json(force=True, silent=True) or {}
    key, name = data.get("key"), (data.get("name") or "").strip()[:60]
    meta = _read_meeting(meeting_id)
    if not key or key not in meta.get("speakers", {}) or not name:
        return jsonify({"error": "bad speaker key or name"}), 400
    meta["speakers"][key] = name
    _write_meeting(meta)
    _write_transcript_md(meta)
    return jsonify({"speakers": meta["speakers"]})


@app.post("/api/meetings/<meeting_id>/process")
def reprocess(meeting_id):
    if REC.is_recording and REC.meeting_id == meeting_id:
        return jsonify({"error": "Meeting is currently recording"}), 409
    if JOBS.get(meeting_id, {}).get("state") == "processing":
        return jsonify({"error": "Already processing"}), 409
    if SUMMARY_JOBS.get(meeting_id, {}).get("state") == "processing":
        return jsonify({"error": "Meeting is being summarized — try again in a moment"}), 409
    meta = _read_meeting(meeting_id)
    has_audio = any(
        (_dir_for(meeting_id) / t["file"]).exists()
        for t in meta.get("tracks", {}).values()
    )
    if not has_audio:
        return jsonify({"error": "No audio files for this meeting"}), 400
    meta["status"] = "processing"
    _write_meeting(meta)
    _start_processing(meeting_id)
    return jsonify({"ok": True})


@app.post("/api/meetings/<meeting_id>/recluster")
def recluster(meeting_id):
    """Change the speaker count after processing — re-clusters the saved
    voice analysis in under a second, no re-transcription."""
    if REC.is_recording and REC.meeting_id == meeting_id:
        return jsonify({"error": "Meeting is currently recording"}), 409
    if JOBS.get(meeting_id, {}).get("state") == "processing":
        return jsonify({"error": "Meeting is being processed"}), 409
    data = request.get_json(force=True, silent=True) or {}
    speakers = data.get("speakers")
    try:
        speakers = max(1, min(8, int(speakers))) if speakers else None
    except (TypeError, ValueError):
        speakers = None
    _read_meeting(meeting_id)  # 404 on unknown id
    try:
        meta = pipeline.recluster_meeting(_dir_for(meeting_id), speakers)
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 409
    _write_transcript_md(meta)
    return jsonify(meta)


@app.post("/api/meetings/<meeting_id>/tidy")
def tidy_meeting(meeting_id):
    """Clean the transcript with the on-device model (Apple Intelligence)."""
    if JOBS.get(meeting_id, {}).get("state") == "processing":
        return jsonify({"error": "Meeting is being processed"}), 409
    if SUMMARY_JOBS.get(meeting_id, {}).get("state") == "processing":
        return jsonify({"error": "Meeting is being summarized — try again in a moment"}), 409
    meta = _read_meeting(meeting_id)
    if not meta.get("turns"):
        return jsonify({"error": "No transcript to tidy yet"}), 400
    llm_ok, llm_reason = local_llm.available()
    if not llm_ok:
        return jsonify({"error": local_llm.reason_message(llm_reason)}), 400
    meta["status"] = "processing"
    _write_meeting(meta)
    JOBS[meeting_id] = {"state": "processing", "message": "Tidying on this Mac…"}

    def run():
        try:
            tidy.tidy_meeting(
                _dir_for(meeting_id),
                lambda msg: JOBS[meeting_id].update(message=msg),
            )
            _write_transcript_md(_read_meeting(meeting_id))
            JOBS[meeting_id] = {"state": "done", "message": "Tidied"}
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
    meta = json.loads(backup.read_text(encoding="utf-8"))
    _write_meeting(meta)
    _write_transcript_md(meta)
    backup.unlink()
    return jsonify(meta)


@app.post("/api/meetings/<meeting_id>/summarize")
def summarize_meeting(meeting_id):
    """Generate a summary + action items with the on-device model (Apple
    Intelligence). Runs in the background; the transcript stays visible."""
    if SUMMARY_JOBS.get(meeting_id, {}).get("state") == "processing":
        return jsonify({"error": "Already summarizing"}), 409
    if JOBS.get(meeting_id, {}).get("state") == "processing":
        return jsonify({"error": "Meeting is being processed"}), 409
    meta = _read_meeting(meeting_id)
    if not meta.get("turns"):
        return jsonify({"error": "No transcript to summarize yet"}), 400
    llm_ok, llm_reason = local_llm.available()
    if not llm_ok:
        return jsonify({"error": local_llm.reason_message(llm_reason)}), 400
    SUMMARY_JOBS[meeting_id] = {"state": "processing", "message": "Summarizing on this Mac…"}

    def run():
        try:
            summarize.summarize_meeting(
                _dir_for(meeting_id),
                lambda msg: SUMMARY_JOBS[meeting_id].update(message=msg),
            )
            _write_transcript_md(_read_meeting(meeting_id))
            SUMMARY_JOBS[meeting_id] = {"state": "done", "message": "Summary ready"}
        except Exception:
            err = traceback.format_exc().strip().splitlines()[-1]
            SUMMARY_JOBS[meeting_id] = {"state": "error", "message": err}

    threading.Thread(target=run, daemon=True, name=f"summarize-{meeting_id}").start()
    return jsonify({"ok": True})


@app.post("/api/shutdown")
def shutdown():
    """Quit cleanly from the web UI (the .app launcher has no terminal)."""
    if REC.is_recording:
        return jsonify({"error": "Stop the recording before quitting"}), 409
    if any(j.get("state") == "processing" for j in list(JOBS.values()) + list(SUMMARY_JOBS.values())):
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
    print(f"\n  MeetingScribe running at http://127.0.0.1:{port}\n")
    app.run(host="127.0.0.1", port=port, threaded=True, debug=False, use_reloader=False)
