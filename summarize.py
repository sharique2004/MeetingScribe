"""Meeting summary + action items — written by the user's own Claude.

Preferred engine: the `claude` CLI already on this machine (the user's
Claude account — no API key). A frontier model reads the WHOLE transcript
in one pass, so it genuinely understands who's who and what happened.
Only the transcript TEXT is sent, never audio. When the CLI isn't
installed or signed in, the UI walks the user through logging in.

Fallback engine (config "summary_engine": "apple"): the on-device Apple
Intelligence model — fully offline but noticeably shallower.

Either way _coerce() validates + de-duplicates before anything is stored.
"""

import difflib
import json
import logging
import os
import pwd
import re
import shutil
import subprocess
from pathlib import Path

import local_llm
from config import load_config

log = logging.getLogger("meetingscribe.summarize")

MAX_CHUNK_CHARS = 10000    # transcript text per model call (4K-token context)
MAX_TOTAL_CHARS = 240000   # sanity cap for pathological transcripts
MAP_WORKERS = 3            # map chunks summarized concurrently (= llm MAX_INFLIGHT)

_ACTION_ITEM = {
    "type": "object", "name": "ActionItem", "properties": [
        {"name": "owner", "type": "string",
         "description": "who will do it — a speaker name, or 'You' for the local user"},
        {"name": "task", "type": "string", "description": "what they will do"},
        {"name": "due", "type": "string",
         "description": "when it is due, or empty string if no deadline was said"},
    ],
}

# Tight caps keep map-phase OUTPUT short — on-device generation speed is
# dominated by output tokens, so lean notes are what make long meetings fast.
CHUNK_SCHEMA = {
    "type": "object", "name": "ChunkNotes", "properties": [
        {"name": "points", "type": "array", "items": {"type": "string"}, "max": 7,
         "description": "the important things discussed in this portion, most important first"},
        {"name": "decisions", "type": "array", "items": {"type": "string"}, "max": 5,
         "description": "concrete decisions made in this portion"},
        {"name": "action_items", "type": "array", "items": _ACTION_ITEM, "max": 6},
        {"name": "open_questions", "type": "array", "items": {"type": "string"}, "max": 4,
         "description": "questions raised but not resolved in this portion"},
    ],
}

SUMMARY_SCHEMA = {
    "type": "object", "name": "MeetingSummary", "properties": [
        {"name": "headline", "type": "string",
         "description": "a punchy 6-10 word headline stating the single most important outcome, like a news headline (e.g. 'Friday launch locked; QA owner still open')"},
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
    "the follow-up email should be written as if they are sending it — a warm, "
    "specific, human email, never a list of bullet points. "
    "Never repeat the same fact in more than one section: a committed task belongs "
    "only in action_items, a decision only in decisions. Omit vacuous items like "
    "'X learned a lot' or 'they discussed plans'. If no deadline was said, use an "
    "empty string — never write 'TBD'. " + _FAITHFUL
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

    _NO_DUE = {"tbd", "n/a", "na", "none", "unknown", "unspecified", "-", "—", "not specified"}

    actions = []
    for item in (raw.get("action_items") if isinstance(raw.get("action_items"), list) else []):
        if isinstance(item, dict):
            task = _strip(item.get("task"), 400)
            due = _strip(item.get("due"), 80)
            if (due.lower() in _NO_DUE or len(due) > 40
                    or re.search(r"stated timing|empty string|omit|never invent", due, re.I)):
                due = ""  # "TBD" noise or the model echoing the schema text
            if task:
                actions.append({
                    "owner": _strip(item.get("owner"), 60) or "—",
                    "task": task,
                    "due": due,
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

    out = {
        "headline": _strip(raw.get("headline"), 90),
        "tldr": _strip(raw.get("tldr"), 1500),
        "key_points": _str_list(raw.get("key_points")),
        "decisions": _str_list(raw.get("decisions")),
        "action_items": actions[:25],
        "follow_ups": _str_list(raw.get("follow_ups")),
        "open_questions": _str_list(raw.get("open_questions")),
        "follow_up_email": email,
    }
    return _dedupe(out)


def _norm_key(text):
    return re.sub(r"[^a-z0-9 ]+", "", str(text).lower()).strip()


def _similar(a, b):
    if a == b:
        return True
    if len(a) > 20 and (a in b or b in a):
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.87


def _dedupe(summary):
    """Small models love repeating one thought across every section — drop
    exact and near-duplicate items within and across sections."""
    def clean(items, seen):
        kept = []
        for item in items:
            key = _norm_key(item)
            if not key or any(_similar(key, s) for s in seen):
                continue
            seen.add(key)
            kept.append(item)
        return kept

    seen = set()
    # Action items win first claim on their phrasing (owner + task).
    task_keys = set()
    kept_actions = []
    for a in summary["action_items"]:
        key = _norm_key(f"{a['owner']} {a['task']}")
        if any(_similar(key, s) for s in task_keys):
            continue
        task_keys.add(key)
        kept_actions.append(a)
        seen.add(_norm_key(a["task"]))
    summary["action_items"] = kept_actions

    summary["decisions"] = clean(summary["decisions"], seen)
    summary["key_points"] = clean(summary["key_points"], seen)
    # Questions form their own pool: follow-ups repeating open questions
    # (or either repeating a decision) get dropped.
    summary["open_questions"] = clean(summary["open_questions"], seen)
    summary["follow_ups"] = clean(summary["follow_ups"], seen)
    return summary


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
            try:
                notes = local_llm.generate(
                    CONDENSE_INSTRUCTIONS,
                    f'Notes from consecutive portions of the meeting "{title}":\n\n'
                    + "\n\n".join(g),
                    CHUNK_SCHEMA, max_tokens=700,
                )
                merged.append(_render_notes(notes))
            except local_llm.LocalLLMError as exc:
                if exc.code in ("guardrail", "refusal", "context_overflow"):
                    merged.append("\n".join(g)[: MAX_CHUNK_CHARS // 2])
                else:
                    raise
        blocks = merged
    return "\n".join(blocks)


# ------------------------------------------------------------------- main --

def _speaker_note(meta):
    names = ", ".join((meta.get("speakers") or {}).values())
    return f"Participants: {names}." if names else ""


def _extractive_fallback(chunk):
    """When the model declines a portion, keep its opening lines as raw
    notes so the rest of the meeting still summarizes."""
    excerpt = " ".join(chunk.split())[:500]
    return f"- (portion kept verbatim — could not be auto-summarized): {excerpt}…"


def _map_chunk(i, total, chunk, title, speaker_note, depth=0):
    try:
        notes = local_llm.generate(
            MAP_INSTRUCTIONS,
            f'Portion {i} of {total} of the meeting "{title}". '
            f'{speaker_note}\n\n{chunk}',
            CHUNK_SCHEMA, max_tokens=800,
        )
        return _render_notes(notes)
    except local_llm.LocalLLMError as exc:
        # Dense speech can overshoot the token estimate — split and retry
        # rather than losing the portion.
        if exc.code == "context_overflow" and depth < 2:
            lines = chunk.splitlines()
            mid = len(lines) // 2
            if mid:
                log.info("portion %d/%d overflowed; splitting", i, total)
                return "\n".join(
                    _map_chunk(i, total, part, title, speaker_note, depth + 1)
                    for part in ("\n".join(lines[:mid]), "\n".join(lines[mid:]))
                    if part.strip()
                )
        # A declined portion must not sink the whole summary.
        if exc.code in ("guardrail", "refusal", "context_overflow"):
            log.warning("portion %d/%d fell back to excerpt (%s)", i, total, exc.code)
            return _extractive_fallback(chunk)
        raise


# ------------------------------------------------- the big local model path --

FULL_INSTRUCTIONS = """You are writing meeting notes for the local user. You have the FULL transcript — read it as someone who attended: understand who each person is, what they want, and what actually happened between them.

WHO IS WHO — get this right before writing anything:
- The speaker labelled "You" is the LOCAL USER: the person these notes are for and the person the follow-up email is FROM. A name in the meeting title usually belongs to the local user (their calendar), NOT to the other side.
- Work out the other participants' identities from the conversation itself — introductions, how people address each other, whose company/role is being described. Use their real names once known.
- If other people address the local user by name in the transcript, that is the local user's name; use it for the email signature. If it never appears, end the email with just "Best," and no name. NEVER write the literal word "You" as a signature.

Return ONLY a JSON object, no markdown fences, with exactly these fields:
{
 "headline": "a punchy 6-10 word headline capturing the single most important outcome, like a news headline. No trailing period. Example: 'Friday launch locked; QA owner still open'.",
 "tldr": "2-4 sentences: who met with whom and why, what actually came of it. Name the participants and their context (company, role) when the conversation reveals it.",
 "key_points": ["the substantive things discussed, most important first — each one specific enough that a colleague who missed the meeting would actually learn something"],
 "decisions": ["only real decisions/agreements that were made in this conversation"],
 "action_items": [{"owner": "who (a participant's name, or 'You')", "task": "the concrete thing they committed to", "due": "the stated timing, or empty string — never invent one"}],
 "follow_ups": ["things explicitly deferred to a future conversation"],
 "open_questions": ["genuinely unresolved questions that matter"],
 "follow_up_email": {"subject": "...", "body": "an email You could actually send to the other participant(s): warm, specific to what was discussed, references the real next steps, signs off with You's name if it was spoken. Written like a person, not a bullet dump."}
}

Quality rules — these matter:
- Every item must be SPECIFIC: names, numbers, technologies, timelines that were actually said. Generic filler ("they discussed plans", "X learned a lot") is worthless — omit it.
- NEVER repeat the same fact across sections. A committed task goes in action_items only; a decision goes in decisions only; key_points carry the substance that isn't already a decision or task.
- If a section has nothing real, return an empty array — do not pad.
- Use the participants' real names as they appear or are spoken in the transcript. If a speaker's real name is stated in conversation, prefer it over labels like "Speaker 1".
- The transcript is auto-generated: expect misrecognized words and names; infer the intended meaning from context rather than quoting errors verbatim."""


def _local_user_name():
    """The Mac account's full name — tells the model who "You" actually is,
    so a Calendly-style meeting title carrying the user's own name can't be
    mistaken for the other participant."""
    try:
        name = pwd.getpwuid(os.getuid()).pw_gecos.split(",")[0].strip()
        return name or None
    except Exception:
        return None


class NeedsClaudeError(RuntimeError):
    """The `claude` CLI is missing or signed out — the UI walks the user
    through logging in."""


def _full_source(meta, lines):
    title = meta.get("title") or "Untitled meeting"
    notes = [f'Transcript of the meeting "{title}".', _speaker_note(meta)]
    me = _local_user_name()
    if me:
        notes.append(f'IMPORTANT: the local user — the speaker labelled "You" — '
                     f'is {me}. Anyone else in the conversation is a different '
                     f'person; find their names in the dialogue.')
    cal = meta.get("calendar_event") or {}
    if cal.get("names"):
        notes.append("Calendar attendees besides the local user: "
                     + ", ".join(cal["names"]) + ".")
    return " ".join(n for n in notes if n) + "\n\n" + "\n".join(lines)


def find_claude():
    """The user's Claude Code CLI, if installed."""
    exe = shutil.which("claude")
    if exe:
        return exe
    for cand in (
        Path.home() / ".local" / "bin" / "claude",
        Path("/usr/local/bin/claude"),
        Path("/opt/homebrew/bin/claude"),
    ):
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    return None


_CLAUDE_SETUP_HELP = (
    "MeetingScribe writes summaries with YOUR Claude account (no API key). "
    "One-time setup: install Claude Code — `npm install -g "
    "@anthropic-ai/claude-code` — then run `claude` in Terminal once and "
    "sign in. After that, just press Summary again."
)


def _extract_json(text):
    """First balanced {...} in the reply — string-aware so braces inside
    values can't fool the depth counter; tolerates ``` fences and prose."""
    text = str(text).strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fenced:
        text = fenced.group(1)
    start = text.find("{")
    if start == -1:
        raise ValueError("no JSON object in the reply")
    depth, in_str, escaped = 0, False, False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if escaped:
                escaped = False
            elif c == "\\":
                escaped = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start:i + 1])
    raise ValueError("unbalanced JSON in the reply")


def _summarize_claude(meta, lines, progress_cb):
    """One full-transcript pass through the user's own Claude."""
    exe = find_claude()
    if exe is None:
        raise NeedsClaudeError(_CLAUDE_SETUP_HELP)
    source = _full_source(meta, lines)
    progress_cb("Summarizing with your Claude account…")
    try:
        proc = subprocess.run(
            [exe, "-p", "--output-format", "json"],
            input=FULL_INSTRUCTIONS + "\n\n" + source,
            capture_output=True, text=True, timeout=900,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("Claude took too long to summarize — please try again.")
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        if re.search(r"log ?in|logged out|authent|credential|api key|/login", detail, re.I):
            raise NeedsClaudeError(
                "Your Claude account is signed out. Open Terminal, run "
                "`claude`, and sign in — then press Summary again.")
        last = detail.splitlines()[-1] if detail else "unknown error"
        raise RuntimeError(f"Claude could not summarize: {last}")
    try:
        envelope = json.loads(proc.stdout)
        return _extract_json(envelope.get("result") or "")
    except (ValueError, AttributeError) as exc:
        raise RuntimeError(f"Could not parse Claude's reply: {exc}") from exc


def _pick_engine():
    setting = str(load_config().get("summary_engine") or "claude").lower()
    return "apple" if setting == "apple" else "claude"


def summarize_meeting(meeting_dir, progress_cb=lambda msg: None):
    """Summarize one meeting; stores meta['summary'] and rewrites meeting.json."""
    meeting_dir = Path(meeting_dir)
    meta_path = meeting_dir / "meeting.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    if not (meta.get("turns") or []):
        raise RuntimeError("No transcript to summarize yet.")

    lines = _transcript_lines(meta)

    if _pick_engine() == "claude":
        raw = _summarize_claude(meta, lines, progress_cb)
        summary = _coerce(raw if isinstance(raw, dict) else {})
        summary["engine"] = "claude"
        return _store_summary(meta, meta_path, meeting_dir, summary)

    ok, reason = local_llm.available()
    if not ok:
        raise RuntimeError(local_llm.reason_message(reason))

    title = meta.get("title") or "Untitled meeting"
    speaker_note = _speaker_note(meta)
    chunks = _chunk_lines(lines)

    if len(chunks) == 1:
        progress_cb("Summarizing on this Mac…")
        source = f'Transcript of the meeting "{title}". {speaker_note}\n\n{chunks[0]}'
    else:
        # Map phase runs CONCURRENTLY — the helper handles parallel requests,
        # so a long meeting takes ~total/MAP_WORKERS instead of one-by-one.
        from concurrent.futures import ThreadPoolExecutor
        done = {"n": 0}

        def run_one(args):
            i, chunk = args
            block = _map_chunk(i, len(chunks), chunk, title, speaker_note)
            done["n"] += 1
            progress_cb(f"Reading the meeting… {done['n']}/{len(chunks)}")
            return block

        progress_cb(f"Reading the meeting… 0/{len(chunks)}")
        with ThreadPoolExecutor(max_workers=MAP_WORKERS) as pool:
            blocks = list(pool.map(run_one, enumerate(chunks, 1)))
        notes_text = _condense([b for b in blocks if b.strip()], title, progress_cb)
        progress_cb("Writing the summary…")
        source = (
            f'Notes covering the whole meeting "{title}", in order. {speaker_note}\n\n'
            f"{notes_text}"
        )

    try:
        raw = local_llm.generate(REDUCE_INSTRUCTIONS, source, SUMMARY_SCHEMA,
                                 max_tokens=1400)
    except local_llm.LocalLLMError as exc:
        if exc.code in ("guardrail", "refusal"):
            raise RuntimeError(
                "The on-device model declined to summarize this meeting's "
                "content. This should be rare — try Re-summarize once; if it "
                "persists, the transcript may contain content Apple "
                "Intelligence won't process.") from exc
        raise RuntimeError(str(exc)) from exc

    summary = _coerce(raw if isinstance(raw, dict) else {})
    summary["engine"] = "apple-intelligence"
    return _store_summary(meta, meta_path, meeting_dir, summary)


def _store_summary(meta, meta_path, meeting_dir, summary):
    meta["summary"] = summary
    try:
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError as exc:  # e.g. the meeting was deleted while summarizing
        raise RuntimeError(f"Could not save the summary: {exc}") from exc
    log.info("summarized %s [%s]: %d action item(s)",
             meeting_dir.name, summary.get("engine"), len(summary["action_items"]))
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
