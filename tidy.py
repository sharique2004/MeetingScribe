"""Optional transcript clean-up through the local `claude` CLI.

Uses the Claude Code subscription already on this machine — no API key.
Only the transcript TEXT is sent, never audio. Claude returns edit
*operations* (drop / trim / merge speakers / rename), which are validated
and applied locally: a trim may only keep words that are already in the
turn, so the model cannot invent words that were never transcribed.

A backup of the pre-tidy transcript is kept as meeting.pretidy.json and can
be restored from the UI.
"""

import json
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path

import stats as stats_mod

log = logging.getLogger("meetingscribe.tidy")

TIMEOUT_S = 900
TURN_MERGE_GAP_S = 3.0

PROMPT = """You are cleaning an automatically generated meeting transcript. It has two known defect types:

1. ECHO DUPLICATES: when the local user is not wearing headphones, the remote
   side's voice leaks from the speakers into the microphone. The same sentence
   then appears twice: once on the "system" track (the clean copy, belongs to a
   remote speaker) and once on the "mic" track (the echo), a few seconds apart
   and often split across turn boundaries. The mic track is genuine only when
   the local user ("you") is actually speaking.
2. OVER-SPLIT SPEAKERS: one real person may have been split into several labels
   (e.g. s1, s2 and s4 are clearly the same voice given the conversation flow).
   The label "you" is always the local user and is never merged into others.

INPUT: a JSON object with "speakers" (label -> display name) and "turns"
(id, speaker label, track, start time in seconds, text).

OUTPUT: ONLY a JSON object — no markdown fences, no commentary:
{
 "merge_speakers": {"s2": "s1"},
 "drop_turns": [12, 47],
 "trim_turns": {"33": "text to keep"},
 "rename_speakers": {"s1": "Jess"}
}

Field meanings and rules:
- merge_speakers: fold the key label into the value label when they are the
  same person. Never use "you" as a key.
- drop_turns: ids of turns that are pure echo duplicates of a nearby turn
  (within ~20 seconds) by ANOTHER speaker on the OTHER track. Drop the mic
  copy of remote speech; keep what the local user genuinely said.
- trim_turns: turns that are PART echo, PART genuine: give the text to KEEP.
  The kept text must use ONLY words already present in that turn, in the same
  order — never add, reorder or rephrase words.
- rename_speakers: only when someone's real name is clearly stated in the
  conversation (introductions, "thanks <name>", an interviewer naming
  themselves). Otherwise leave names alone.
- When unsure about any operation, do nothing — prefer keeping turns.
- Use empty objects/arrays for operations you do not need.
"""


def find_claude():
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


def _norm_tokens(text):
    return re.findall(r"[\w']+", str(text).lower())


def _is_ordered_subset(small, big):
    it = iter(big)
    return all(tok in it for tok in small)


def _extract_json(text):
    """Claude was told to return bare JSON, but tolerate fences/prose."""
    text = str(text).strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fenced:
        text = fenced.group(1)
    start = text.find("{")
    if start == -1:
        raise ValueError("no JSON object in Claude's reply")
    depth = 0
    for i in range(start, len(text)):  # first balanced {...}, ignore trailing prose
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start : i + 1])
    raise ValueError("unbalanced JSON object in Claude's reply")


def _resolve_merges(merges, speakers):
    """Validated, transitively resolved label -> label mapping."""
    flat = {}
    for src, dst in (merges or {}).items():
        if src == "you" or src not in speakers or dst not in speakers or src == dst:
            continue
        flat[src] = dst
    resolved = {}
    for src in flat:
        dst, seen = flat[src], {src}
        while dst in flat and dst not in seen:
            seen.add(dst)
            dst = flat[dst]
        if dst not in seen:  # drop cycles
            resolved[src] = dst
    return resolved


def apply_ops(meta, ops):
    """Apply validated edit operations; returns (turns, speakers, summary)."""
    turns = meta.get("turns") or []
    speakers = dict(meta.get("speakers") or {})

    merge_map = _resolve_merges(ops.get("merge_speakers"), speakers)
    drops = {int(i) for i in (ops.get("drop_turns") or []) if 0 <= int(i) < len(turns)}

    trims = {}
    for key, kept_text in (ops.get("trim_turns") or {}).items():
        try:
            idx = int(key)
        except (TypeError, ValueError):
            continue
        if not (0 <= idx < len(turns)) or idx in drops:
            continue
        kept_tokens = _norm_tokens(kept_text)
        if kept_tokens and _is_ordered_subset(kept_tokens, _norm_tokens(turns[idx]["text"])):
            trims[idx] = str(kept_text).strip()

    kept_turns = []
    for i, turn in enumerate(turns):
        if i in drops:
            continue
        turn = dict(turn)
        if i in trims:
            # Scale the turn length with the kept words so talk-time and
            # WPM stats stay honest after the echo part is cut.
            old_n = len(_norm_tokens(turn["text"]))
            new_n = len(_norm_tokens(trims[i]))
            if old_n and new_n < old_n:
                length = max(0.0, float(turn["end"]) - float(turn["start"]))
                turn["end"] = round(float(turn["start"]) + length * new_n / old_n, 2)
            turn["text"] = trims[i]
        turn["speaker"] = merge_map.get(turn["speaker"], turn["speaker"])
        kept_turns.append(turn)

    merged = []
    for turn in kept_turns:
        prev = merged[-1] if merged else None
        if (
            prev is not None
            and prev["speaker"] == turn["speaker"]
            and turn["start"] - prev["end"] <= TURN_MERGE_GAP_S
        ):
            prev["text"] = (prev["text"] + " " + turn["text"]).strip()
            prev["end"] = max(prev["end"], turn["end"])
        else:
            merged.append(turn)

    in_use = {t["speaker"] for t in merged}
    new_speakers = {k: v for k, v in speakers.items() if k in in_use}
    renames = 0
    for key, name in (ops.get("rename_speakers") or {}).items():
        name = str(name).strip()[:60]
        if key in new_speakers and name:
            new_speakers[key] = name
            renames += 1

    summary = {
        "dropped_turns": len(drops),
        "trimmed_turns": len(trims),
        "merged_speakers": merge_map,
        "renamed_speakers": renames,
    }
    return merged, new_speakers, summary


def tidy_meeting(meeting_dir, progress_cb=lambda msg: None):
    """Run the clean-up on one meeting; rewrites meeting.json in place."""
    meeting_dir = Path(meeting_dir)
    meta_path = meeting_dir / "meeting.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    turns = meta.get("turns") or []
    if not turns:
        raise RuntimeError("No transcript to tidy yet.")
    exe = find_claude()
    if exe is None:
        raise RuntimeError("The `claude` CLI was not found on this machine.")

    payload = {
        "speakers": meta.get("speakers", {}),
        "turns": [
            {
                "id": i,
                "speaker": t["speaker"],
                "track": t.get("track"),
                "start": t["start"],
                "text": t["text"],
            }
            for i, t in enumerate(turns)
        ],
    }
    progress_cb("Asking Claude to tidy the transcript (uses your Claude subscription)…")
    proc = subprocess.run(
        [exe, "-p", "--output-format", "json"],
        input=PROMPT + "\n\n" + json.dumps(payload, ensure_ascii=False),
        capture_output=True,
        text=True,
        timeout=TIMEOUT_S,
        cwd=str(meeting_dir),
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise RuntimeError("claude CLI failed: " + (detail[-1] if detail else "unknown error"))

    try:
        envelope = json.loads(proc.stdout)
        ops = _extract_json(envelope.get("result") or "")
    except (ValueError, AttributeError) as exc:
        raise RuntimeError(f"Could not parse Claude's reply: {exc}") from exc

    progress_cb("Applying Claude's clean-up…")
    new_turns, new_speakers, summary = apply_ops(meta, ops)
    if not new_turns:
        raise RuntimeError("Clean-up would have removed the whole transcript; nothing applied.")

    backup = meeting_dir / "meeting.pretidy.json"
    if not backup.exists():
        backup.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")

    meta["turns"] = new_turns
    meta["speakers"] = new_speakers
    meta["stats"] = stats_mod.compute(new_turns, new_speakers, meta.get("duration") or 0.0)
    meta["tidied"] = summary
    meta["status"] = "done"
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    log.info("tidied %s: %s", meeting_dir.name, summary)
    return summary
