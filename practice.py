"""Practice — the mock-interview coach, mounted inside MeetingScribe.

A turn-based interview bot that judges your spoken answers like a recruiter
using the local `claude` CLI (your Claude subscription, no API key). Served at
/practice; its API lives under /api/practice/. Sessions persist as JSON +
Markdown in recordings/../practice, separate from your meetings.
"""

import json
import logging
import re
import threading
import uuid
from datetime import datetime

from flask import Blueprint, abort, current_app, jsonify, request, send_from_directory

import screener
from config import BASE_DIR

log = logging.getLogger("meetingscribe.practice")

SESSIONS_DIR = BASE_DIR / "practice"
SESSIONS_DIR.mkdir(exist_ok=True)

bp = Blueprint("practice", __name__)

SESSIONS = {}              # id -> session dict (also persisted to disk)
_LOCK = threading.Lock()   # guards SESSIONS + the _session_locks registry
_session_locks = {}        # id -> threading.Lock (serializes turns per session)
ID_RE = re.compile(r"^[0-9a-f]{12}$")
MAX_QUESTIONS = 15


def _session_lock(session_id):
    with _LOCK:
        lk = _session_locks.get(session_id)
        if lk is None:
            lk = _session_locks[session_id] = threading.Lock()
        return lk


# ----------------------------------------------------------------- storage ----

def _save(sess):
    try:
        path = SESSIONS_DIR / f"{sess['id']}.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(sess, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)  # atomic — an interrupted write can't corrupt the session
    except OSError as exc:
        log.warning("could not save session %s: %s", sess.get("id"), exc)


def _get(session_id):
    if not ID_RE.match(session_id or ""):
        abort(400, "bad session id")
    sess = SESSIONS.get(session_id)
    if sess is None:
        path = SESSIONS_DIR / f"{session_id}.json"
        if path.exists():
            sess = json.loads(path.read_text(encoding="utf-8"))
            SESSIONS[session_id] = sess
    if sess is None:
        abort(404, "session not found")
    return sess


def _public(sess):
    return {
        "id": sess["id"],
        "config": {k: sess["config"].get(k) for k in ("role", "company", "persona", "difficulty", "total")},
        "status": sess["status"],
        "index": sess["index"],
        "total": sess["config"]["total"],
        "turns": sess["turns"],
        "report": sess.get("report"),
        "current_question": sess.get("current_question"),
    }


# ------------------------------------------------------------------ routes ----

@bp.get("/practice")
def practice_index():
    return send_from_directory(str(BASE_DIR / "templates"), "practice.html")


@bp.get("/practice/sessions/<session_id>.md")
def session_markdown(session_id):
    if not ID_RE.match(session_id or ""):
        abort(400)
    if not (SESSIONS_DIR / f"{session_id}.md").exists():
        abort(404)
    return send_from_directory(str(SESSIONS_DIR), f"{session_id}.md",
                               mimetype="text/markdown", as_attachment=True)


@bp.get("/api/practice/health")
def health():
    llm_ok, llm_message = screener.llm_available()
    return jsonify({
        "llm": llm_ok,
        "llm_message": llm_message,
        "transcribe": screener.transcribe_available(),
    })


@bp.post("/api/practice/session")
def create_session():
    data = request.get_json(force=True, silent=True) or {}
    try:
        total = max(3, min(MAX_QUESTIONS, int(data.get("questions") or 6)))
    except (TypeError, ValueError):
        total = 6
    persona = data.get("persona") if data.get("persona") in screener.PERSONAS else "recruiter"
    difficulty = data.get("difficulty") if data.get("difficulty") in screener.DIFFICULTY else "normal"
    config = {
        "role": (data.get("role") or "").strip()[:160],
        "company": (data.get("company") or "").strip()[:120],
        "job_description": (data.get("job_description") or "").strip()[:8000],
        "resume": (data.get("resume") or "").strip()[:8000],
        "persona": persona,
        "difficulty": difficulty,
        "total": total,
    }
    session_id = uuid.uuid4().hex[:12]
    sess = {
        "id": session_id,
        "config": config,
        "status": "active",
        "index": 0,
        "turns": [],
        "current_question": None,
        "created": datetime.now().isoformat(timespec="seconds"),
    }
    llm_ok, llm_message = screener.llm_available()
    if not llm_ok:
        return jsonify({"error": llm_message}), 400
    try:
        question = screener.start_interview(config)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502
    sess["index"] = 1
    sess["current_question"] = question
    with _LOCK:
        SESSIONS[session_id] = sess
    _save(sess)
    return jsonify(_public(sess))


@bp.post("/api/practice/session/<session_id>/answer")
def answer(session_id):
    sess = _get(session_id)
    lock = _session_lock(session_id)
    # One answer at a time per interview — rejects a fast double-submit rather
    # than judging the same answer twice or scrambling the turn order.
    if not lock.acquire(blocking=False):
        return jsonify({"error": "An answer is already being processed for this interview."}), 409
    try:
        if sess["status"] != "active":
            return jsonify({"error": "This interview is already finished."}), 409
        if not sess.get("current_question"):
            return jsonify({"error": "No question is awaiting an answer."}), 409

        text = ""
        if request.content_type and "application/json" in request.content_type:
            text = ((request.get_json(silent=True) or {}).get("text") or "").strip()
        else:
            text = (request.form.get("text") or "").strip()
            audio = request.files.get("audio")
            if not text and audio is not None:
                tmp = SESSIONS_DIR / f"{session_id}-{uuid.uuid4().hex[:8]}.wav"
                audio.save(str(tmp))
                try:
                    text = screener.transcribe_wav(tmp)
                except Exception as exc:
                    return jsonify({"error": str(exc)}), 502
                finally:
                    tmp.unlink(missing_ok=True)
        if not text:
            return jsonify({"error": "No answer was provided."}), 400

        history = sess["turns"] + [{"question": sess["current_question"], "answer": text}]
        try:
            verdict = screener.judge_answer(sess["config"], history, text,
                                            sess["index"], sess["config"]["total"])
        except Exception as exc:
            return jsonify({"error": str(exc)}), 502

        turn = {
            "question": sess["current_question"],
            "answer": text,
            "assessment": verdict["assessment"],
            "scores": verdict["scores"],
            "strengths": verdict["strengths"],
            "concerns": verdict["concerns"],
            "is_follow_up": verdict["is_follow_up"],
            # Short model-written summary of the answer — later turns carry
            # this instead of the full text (the on-device model's context
            # is small).
            "digest": verdict.get("answer_digest", ""),
        }
        sess["turns"].append(turn)

        more = sess["index"] < sess["config"]["total"]
        if more and verdict["next_question"]:
            sess["index"] += 1
            sess["current_question"] = verdict["next_question"]
        else:
            sess["current_question"] = None
        _save(sess)
        return jsonify({"turn": turn, "next_question": sess["current_question"],
                        "index": sess["index"], "total": sess["config"]["total"]})
    finally:
        lock.release()


@bp.post("/api/practice/session/<session_id>/finish")
def finish(session_id):
    sess = _get(session_id)
    lock = _session_lock(session_id)
    if not lock.acquire(blocking=False):
        return jsonify({"error": "This interview is busy — try again in a moment."}), 409
    try:
        if not sess["turns"]:
            return jsonify({"error": "Answer at least one question first."}), 400
        if sess.get("report"):
            return jsonify({"report": sess["report"]})
        try:
            report = screener.final_report(sess["config"], sess["turns"])
        except Exception as exc:
            return jsonify({"error": str(exc)}), 502
        sess["status"] = "done"
        sess["current_question"] = None
        sess["report"] = report
        _save(sess)
        _write_markdown(sess)
        return jsonify({"report": report})
    finally:
        lock.release()


@bp.get("/api/practice/session/<session_id>")
def get_session(session_id):
    return jsonify(_public(_get(session_id)))


@bp.get("/api/practice/sessions")
def list_sessions():
    items = []
    for path in SESSIONS_DIR.glob("*.json"):
        try:
            s = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        items.append({
            "id": s["id"],
            "role": s["config"].get("role") or "Practice interview",
            "company": s["config"].get("company"),
            "created": s.get("created"),
            "status": s.get("status"),
            "answered": len(s.get("turns", [])),
            "recommendation": (s.get("report") or {}).get("recommendation"),
        })
    items.sort(key=lambda x: x.get("created") or "", reverse=True)
    return jsonify(items)


@bp.delete("/api/practice/session/<session_id>")
def delete_session(session_id):
    if not ID_RE.match(session_id or ""):
        abort(400)
    SESSIONS.pop(session_id, None)
    with _LOCK:
        _session_locks.pop(session_id, None)
    (SESSIONS_DIR / f"{session_id}.json").unlink(missing_ok=True)
    (SESSIONS_DIR / f"{session_id}.md").unlink(missing_ok=True)
    return jsonify({"ok": True})


# ----------------------------------------------------------------- markdown ----

def _write_markdown(sess):
    cfg, report = sess["config"], sess.get("report") or {}
    lines = [f"# Mock interview — {cfg.get('role') or 'Practice'}"]
    if cfg.get("company"):
        lines[0] += f" @ {cfg['company']}"
    lines += [
        "", f"*{sess.get('created', '')}*  ·  persona: {cfg['persona']}  ·  "
        f"difficulty: {cfg['difficulty']}  ·  {len(sess['turns'])} answers", ""]
    if report:
        lines += ["## Verdict", "", report.get("verdict", ""), "",
                  f"**Recommendation:** {report.get('recommendation', '')}", ""]
        sc = report.get("scores", {})
        lines += ["**Scores:** " + ", ".join(f"{k} {sc.get(k, 0)}/10" for k in screener.DIMENSIONS), ""]
        if report.get("top_fixes"):
            lines += ["### Top fixes", ""] + [f"{i}. {f}" for i, f in enumerate(report["top_fixes"], 1)] + [""]
    lines += ["## Transcript", ""]
    for i, t in enumerate(sess["turns"], 1):
        lines += [f"**Q{i}. {t['question']}**", "", f"> {t['answer']}", "",
                  f"*{t['assessment']}*", ""]
    try:
        (SESSIONS_DIR / f"{sess['id']}.md").write_text("\n".join(lines), encoding="utf-8")
    except OSError:
        pass
