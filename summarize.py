"""Meeting summary + action items — fully on-device via Apple Intelligence.

The transcript TEXT (never audio) is fed to the ~3B on-device model on the
Neural Engine through local_llm.py. Long meetings are summarized map-reduce
style: each portion becomes structured notes, the notes are condensed if
needed, and a final pass writes the summary (TL;DR, key points, decisions,
action items with owners, follow-ups, open questions, and a ready-to-send
follow-up email). Guided generation guarantees the JSON shape; _coerce()
still validates before anything is stored. Nothing leaves this Mac.
"""

import json
import logging
from pathlib import Path

import local_llm

log = logging.getLogger("meetingscribe.summarize")

MAX_CHUNK_CHARS = 8500     # transcript text per model call (4K-token context)
MAX_TOTAL_CHARS = 240000   # sanity cap for pathological transcripts

_ACTION_ITEM = {
    "type": "object", "name": "ActionItem", "properties": [
        {"name": "owner", "type": "string",
         "description": "who will do it — a speaker name, or 'You' for the local user"},
        {"name": "task", "type": "string", "description": "what they will do"},
        {"name": "due", "type": "string",
         "description": "when it is due, or empty string if no deadline was said"},
    ],
}

CHUNK_SCHEMA = {
    "type": "object", "name": "ChunkNotes", "properties": [
        {"name": "points", "type": "array", "items": {"type": "string"}, "max": 10,
         "description": "the important things discussed in this portion, most important first"},
        {"name": "decisions", "type": "array", "items": {"type": "string"}, "max": 6,
         "description": "concrete decisions made in this portion"},
        {"name": "action_items", "type": "array", "items": _ACTION_ITEM, "max": 8},
        {"name": "open_questions", "type": "array", "items": {"type": "string"}, "max": 5,
         "description": "questions raised but not resolved in this portion"},
    ],
}

SUMMARY_SCHEMA = {
    "type": "object", "name": "MeetingSummary", "properties": [
        {"name": "tldr", "type": "string",
         "description": "2-4 sentence plain-English summary of what the meeting was about and what came of it"},
        {"name": "key_points", "type": "array", "items": {"type": "string"}, "max": 10,
         "description": "the most important things discussed, most important first"},
        {"name": "decisions", "type": "array", "items": {"type": "string"}, "max": 8,
         "description": "concrete decisions that were made"},
        {"name": "action_items", "type": "array", "items": _ACTION_ITEM, "max": 10},
        {"name": "follow_ups", "type": "array", "items": {"type": "string"}, "max": 8,
         "description": "things explicitly left for later / next steps"},
        {"name": "open_questions", "type": "array", "items": {"type": "string"}, "max": 6,
         "description": "questions raised but not resolved"},
        {"name": "follow_up_email", "type": "object", "properties": [
            {"name": "subject", "type": "string"},
            {"name": "body", "type": "string",
             "description": "a short, warm, professional follow-up email the user could send "
                            "to the other participant(s), summarizing what was agreed and any next steps"},
        ]},
    ],
}

_FAITHFUL = (
    "Be faithful to the transcript: never invent decisions, commitments, numbers, "
    "or names that were not actually said. The transcript is auto-generated and may "
    "contain small errors; capture the intent, don't quote verbatim. "
    "Keep every item concise and specific — no filler. "
    "Use empty arrays (or empty strings) for sections that genuinely have nothing."
)

MAP_INSTRUCTIONS = (
    "You are taking structured notes on ONE PORTION of a longer meeting transcript. "
    "Extract only what is in this portion. " + _FAITHFUL
)

CONDENSE_INSTRUCTIONS = (
    "You are condensing several sets of meeting notes into one smaller set, keeping "
    "the most important points, all decisions, and all action items. " + _FAITHFUL
)

REDUCE_INSTRUCTIONS = (
    "You are writing the final summary of a meeting for the local user, who is the "
    "speaker called \"You\". Write for that user: their action items matter most, and "
    "the follow-up email should be written as if they are sending it. " + _FAITHFUL
)


def _strip(text, limit):
    return str(text or "").strip()[:limit]


def _coerce(raw):
    """Validate the model's reply into the exact summary shape the app stores."""
    def _str_list(v):
        out = []
        for item in v if isinstance(v, list) else []:
            s = str(item).strip()
            if s:
                out.append(s[:600])
        return out[:25]

    actions = []
    for item in (raw.get("action_items") if isinstance(raw.get("action_items"), list) else []):
        if isinstance(item, dict):
            task = _strip(item.get("task"), 400)
            if task:
                actions.append({
                    "owner": _strip(item.get("owner"), 60) or "—",
                    "task": task,
                    "due": _strip(item.get("due"), 80),
                })
        elif str(item).strip():
            actions.append({"owner": "—", "task": str(item).strip()[:400], "due": ""})

    email = raw.get("follow_up_email")
    if not isinstance(email, dict):
        email = {}
    email = {
        "subject": _strip(email.get("subject"), 200),
        "body": _strip(email.get("body"), 4000),
    }

    return {
        "tldr": _strip(raw.get("tldr"), 1500),
        "key_points": _str_list(raw.get("key_points")),
        "decisions": _str_list(raw.get("decisions")),
        "action_items": actions[:25],
        "follow_ups": _str_list(raw.get("follow_ups")),
        "open_questions": _str_list(raw.get("open_questions")),
        "follow_up_email": email,
    }


# ------------------------------------------------------------ transcript in --

def _fmt_time(seconds):
    s = max(0, int(seconds or 0))
    return f"{s // 60}:{s % 60:02d}"


def _transcript_lines(meta):
    speakers = meta.get("speakers") or {}
    lines, used = [], 0
    for t in meta.get("turns") or []:
        name = speakers.get(t["speaker"], t["speaker"])
        line = f"[{_fmt_time(t.get('start'))}] {name}: {t['text']}"
        lines.append(line)
        used += len(line)
        if used > MAX_TOTAL_CHARS:
            log.warning("transcript truncated at %d chars for summarizing", used)
            break
    return lines


def _chunk_lines(lines, limit=MAX_CHUNK_CHARS):
    chunks, cur, size = [], [], 0
    for line in lines:
        if cur and size + len(line) > limit:
            chunks.append("\n".join(cur))
            cur, size = [], 0
        cur.append(line)
        size += len(line) + 1
    if cur:
        chunks.append("\n".join(cur))
    return chunks


# ------------------------------------------------------------------- notes --

def _render_notes(notes):
    """One ChunkNotes dict -> compact text block for the next pass."""
    out = []
    for p in notes.get("points") or []:
        out.append(f"- {p}")
    for d in notes.get("decisions") or []:
        out.append(f"- DECISION: {d}")
    for a in notes.get("action_items") or []:
        if not isinstance(a, dict):
            continue
        due = f" (due {a['due']})" if a.get("due") else ""
        out.append(f"- ACTION: {a.get('owner') or '—'} — {a.get('task') or ''}{due}")
    for q in notes.get("open_questions") or []:
        out.append(f"- OPEN QUESTION: {q}")
    return "\n".join(out)


def _condense(blocks, title, progress_cb):
    """Recursively condense note blocks until they fit one model call."""
    while len(blocks) > 1 and sum(len(b) for b in blocks) + 200 > MAX_CHUNK_CHARS:
        progress_cb("Condensing notes…")
        merged = []
        group, size = [], 0
        groups = []
        for b in blocks:
            if group and size + len(b) > MAX_CHUNK_CHARS:
                groups.append(group)
                group, size = [], 0
            group.append(b)
            size += len(b) + 2
        if group:
            groups.append(group)
        if len(groups) == len(blocks):  # can't group further; hard-truncate
            return "\n".join(blocks)[:MAX_CHUNK_CHARS]
        for g in groups:
            if len(g) == 1:
                merged.append(g[0])
                continue
            notes = local_llm.generate(
                CONDENSE_INSTRUCTIONS,
                f'Notes from consecutive portions of the meeting "{title}":\n\n'
                + "\n\n".join(g),
                CHUNK_SCHEMA, max_tokens=900,
            )
            merged.append(_render_notes(notes))
        blocks = merged
    return "\n".join(blocks)


# ------------------------------------------------------------------- main --

def _speaker_note(meta):
    names = ", ".join((meta.get("speakers") or {}).values())
    return f"Participants: {names}." if names else ""


def summarize_meeting(meeting_dir, progress_cb=lambda msg: None):
    """Summarize one meeting; stores meta['summary'] and rewrites meeting.json."""
    meeting_dir = Path(meeting_dir)
    meta_path = meeting_dir / "meeting.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    if not (meta.get("turns") or []):
        raise RuntimeError("No transcript to summarize yet.")
    ok, reason = local_llm.available()
    if not ok:
        raise RuntimeError(local_llm.reason_message(reason))

    title = meta.get("title") or "Untitled meeting"
    lines = _transcript_lines(meta)
    chunks = _chunk_lines(lines)

    if len(chunks) == 1:
        progress_cb("Summarizing on this Mac…")
        source = f'Transcript of the meeting "{title}". {_speaker_note(meta)}\n\n{chunks[0]}'
    else:
        blocks = []
        for i, chunk in enumerate(chunks, 1):
            progress_cb(f"Reading part {i}/{len(chunks)}…")
            notes = local_llm.generate(
                MAP_INSTRUCTIONS,
                f'Portion {i} of {len(chunks)} of the meeting "{title}". '
                f'{_speaker_note(meta)}\n\n{chunk}',
                CHUNK_SCHEMA, max_tokens=900,
            )
            blocks.append(_render_notes(notes))
        notes_text = _condense([b for b in blocks if b.strip()], title, progress_cb)
        progress_cb("Writing the summary…")
        source = (
            f'Notes covering the whole meeting "{title}", in order. {_speaker_note(meta)}\n\n'
            f"{notes_text}"
        )

    try:
        raw = local_llm.generate(REDUCE_INSTRUCTIONS, source, SUMMARY_SCHEMA,
                                 max_tokens=1600)
    except local_llm.LocalLLMError as exc:
        raise RuntimeError(str(exc)) from exc

    summary = _coerce(raw if isinstance(raw, dict) else {})
    meta["summary"] = summary
    try:
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError as exc:  # e.g. the meeting was deleted while summarizing
        raise RuntimeError(f"Could not save the summary: {exc}") from exc
    log.info("summarized %s: %d action item(s)", meeting_dir.name, len(summary["action_items"]))
    return summary


def to_markdown(summary):
    """Render a stored summary as a Markdown section for transcript.md / export."""
    if not summary:
        return ""
    lines = ["## Summary", ""]
    if summary.get("tldr"):
        lines += [summary["tldr"], ""]
    if summary.get("key_points"):
        lines += ["**Key points**", ""] + [f"- {p}" for p in summary["key_points"]] + [""]
    if summary.get("decisions"):
        lines += ["**Decisions**", ""] + [f"- {d}" for d in summary["decisions"]] + [""]
    if summary.get("action_items"):
        lines += ["**Action items**", ""]
        for a in summary["action_items"]:
            due = f" _(by {a['due']})_" if a.get("due") else ""
            lines.append(f"- **{a.get('owner', '—')}:** {a['task']}{due}")
        lines.append("")
    if summary.get("follow_ups"):
        lines += ["**Follow-ups**", ""] + [f"- {f}" for f in summary["follow_ups"]] + [""]
    if summary.get("open_questions"):
        lines += ["**Open questions**", ""] + [f"- {q}" for q in summary["open_questions"]] + [""]
    email = summary.get("follow_up_email") or {}
    if email.get("body"):
        lines += ["**Draft follow-up email**", ""]
        if email.get("subject"):
            lines.append(f"*Subject:* {email['subject']}")
            lines.append("")
        lines += ["> " + ln if ln else ">" for ln in email["body"].splitlines()]
        lines.append("")
    return "\n".join(lines)
