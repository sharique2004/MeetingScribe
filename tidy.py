"""Optional transcript clean-up — fully on-device via Apple Intelligence.

The transcript TEXT (never audio) goes to the ~3B on-device model through
local_llm.py. The model returns edit *operations* (drop / trim / merge
speakers / rename), which are validated and applied locally: a trim may only
keep words that are already in the turn, so the model cannot invent words
that were never transcribed. Long transcripts are cleaned in overlapping
windows (the model has a 4K-token context) and the operations are merged.

A backup of the pre-tidy transcript is kept as meeting.pretidy.json and can
be restored from the UI.
"""

import json
import logging
import os
import re
import stat
import tempfile
from pathlib import Path

import local_llm
import stats as stats_mod

log = logging.getLogger("meetingscribe.tidy")

TURN_MERGE_GAP_S = 3.0
WINDOW_TURNS = 36          # turns per model call
WINDOW_OVERLAP = 6         # turns repeated between windows (echo pairs sit close)
WINDOW_MAX_CHARS = 8000    # char cap per window payload (4K-token context)

INSTRUCTIONS = """You are cleaning one window of an automatically generated meeting transcript. It has two known defect types:

1. ECHO DUPLICATES: when the local user is not wearing headphones, the remote side's voice leaks from the speakers into the microphone. The same sentence then appears twice: once on the "system" track (the clean copy, belongs to a remote speaker) and once on the "mic" track (the echo), a few seconds apart and often split across turn boundaries. The mic track is genuine only when the local user ("you") is actually speaking.
2. OVER-SPLIT SPEAKERS: one real person may have been split into several labels (e.g. s1, s2 and s4 are clearly the same voice given the conversation flow). The label "you" is always the local user and is never merged into others.

INPUT: a JSON object with "speakers" (label -> display name) and "turns" (id, speaker label, track, start time in seconds, text). The ids are global — return them exactly as given.

Rules for the operations you return:
- merge_speakers: fold one label into another when they are clearly the same person. "you" is the local user's own microphone — never fold "you" into another label, and never fold another label into "you" (an identical sentence on both tracks is an echo, not the same speaker).
- drop_turns: ids of turns that are pure echo duplicates of a nearby turn (within ~20 seconds) by ANOTHER speaker on the OTHER track. Drop the mic copy of remote speech; keep what the local user genuinely said.
- trim_turns: turns that are PART echo, PART genuine: give the text to KEEP. The kept text must use ONLY words already present in that turn, in the same order — never add, reorder or rephrase words.
- rename_speakers: only when someone's real name is clearly stated in the conversation (introductions, "thanks <name>", an interviewer naming themselves). Otherwise leave names alone.
- When unsure about any operation, do nothing — prefer keeping turns.
- Use empty arrays for operations you do not need."""

TIDY_SCHEMA = {
    "type": "object", "name": "TidyOps", "properties": [
        {"name": "merge_speakers", "type": "array", "max": 6,
         "items": {"type": "object", "name": "Merge", "properties": [
             {"name": "fold", "type": "string", "description": "the label to fold away (never 'you')"},
             {"name": "into", "type": "string", "description": "the label it belongs to"}]},
         "description": "labels that are the same person"},
        {"name": "drop_turns", "type": "array", "items": {"type": "integer"}, "max": 40,
         "description": "ids of pure-echo turns to delete"},
        {"name": "trim_turns", "type": "array", "max": 20,
         "items": {"type": "object", "name": "Trim", "properties": [
             {"name": "id", "type": "integer"},
             {"name": "keep", "type": "string",
              "description": "the genuine part to keep, using only words already in the turn, in order"}]},
         "description": "part-echo turns and the text to keep"},
        {"name": "rename_speakers", "type": "array", "max": 8,
         "items": {"type": "object", "name": "Rename", "properties": [
             {"name": "label", "type": "string"},
             {"name": "name", "type": "string", "description": "the real name stated in the conversation"}]},
         "description": "labels whose real name was clearly stated"},
    ],
}


def _norm_tokens(text):
    return re.findall(r"[\w']+", str(text).lower())


def _is_ordered_subset(small, big):
    it = iter(big)
    return all(tok in it for tok in small)


def _resolve_merges(merges, speakers):
    """Validated, transitively resolved label -> label mapping.

    "you" can be neither source nor target: it is the local user's own mic,
    a physically different audio source from every other label.
    """
    flat = {}
    for src, dst in (merges or {}).items():
        if src == "you" or dst == "you":
            continue
        if src not in speakers or dst not in speakers or src == dst:
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


def _umask_default_mode():
    """The mode a plain open()/write_text() would give a new file here.

    Read once at import, because querying the umask means setting it (there is
    no read-only call) and doing that from a request thread would race every
    other file this process creates in that window.
    """
    current = os.umask(0)
    os.umask(current)
    return 0o666 & ~current


_DEFAULT_FILE_MODE = _umask_default_mode()


def _atomic_write_json(path, obj):
    """Write JSON so a concurrent reader never sees a partial file.

    Twin of summarize.py's helper of the same name (kept local rather than
    imported so tidy doesn't take a dependency on the summarizer). Same
    tmp-file + os.replace dance app.py's _write_meeting uses; the temp name is
    unique per call so two writers can never scribble into the same scratch
    file and leave a torn meeting.json behind.

    PERMISSIONS: os.replace carries the TEMP file's mode onto the destination,
    and tempfile.mkstemp hardcodes 0600. Left alone, every clean-up would
    quietly turn a 0644 meeting.json (app.py's _write_meeting creates it through
    the umask) into an owner-only file the user never asked for. So the temp
    file is chmod'ed to the target's existing mode first — via the fd, so
    nothing can swap the path underneath us — and to the umask default when
    creating a new file (meeting.pretidy.json), which is exactly what a plain
    write would have produced.
    """
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        try:
            mode = stat.S_IMODE(os.stat(path).st_mode)
        except OSError:                       # new file (or unreadable target)
            mode = _DEFAULT_FILE_MODE
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(obj, ensure_ascii=False, indent=1))
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ------------------------------------------------------------------ windows --

def _windows(turns):
    """Overlapping windows of (global_id, turn) small enough for one call."""
    entries = [
        {"id": i, "speaker": t["speaker"], "track": t.get("track"),
         "start": t["start"], "text": t["text"]}
        for i, t in enumerate(turns)
    ]
    windows, start = [], 0
    while start < len(entries):
        window, size = [], 0
        for e in entries[start:]:
            cost = len(e["text"]) + 40
            if window and (len(window) >= WINDOW_TURNS or size + cost > WINDOW_MAX_CHARS):
                break
            window.append(e)
            size += cost
        windows.append(window)
        if start + len(window) >= len(entries):
            break
        start += max(1, len(window) - WINDOW_OVERLAP)
    return windows


def _merge_window_ops(all_ops):
    """Union the per-window array ops into the dict shape apply_ops expects."""
    merged = {"merge_speakers": {}, "drop_turns": [], "trim_turns": {}, "rename_speakers": {}}
    seen_drops = set()
    for ops in all_ops:
        for m in ops.get("merge_speakers") or []:
            src, dst = str(m.get("fold", "")), str(m.get("into", ""))
            if src and dst and src not in merged["merge_speakers"]:
                merged["merge_speakers"][src] = dst
        for i in ops.get("drop_turns") or []:
            try:
                i = int(i)
            except (TypeError, ValueError):
                continue
            if i not in seen_drops:
                seen_drops.add(i)
                merged["drop_turns"].append(i)
        for t in ops.get("trim_turns") or []:
            try:
                idx = str(int(t.get("id")))
            except (TypeError, ValueError):
                continue
            if idx not in merged["trim_turns"] and str(t.get("keep") or "").strip():
                merged["trim_turns"][idx] = str(t["keep"])
        for r in ops.get("rename_speakers") or []:
            label, name = str(r.get("label", "")), str(r.get("name", "")).strip()
            if label and name and label not in merged["rename_speakers"]:
                merged["rename_speakers"][label] = name
    return merged


def tidy_meeting(meeting_dir, progress_cb=lambda msg: None):
    """Run the clean-up on one meeting; rewrites meeting.json in place."""
    meeting_dir = Path(meeting_dir)
    meta_path = meeting_dir / "meeting.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    turns = meta.get("turns") or []
    if not turns:
        raise RuntimeError("No transcript to tidy yet.")
    ok, reason = local_llm.available()
    if not ok:
        raise RuntimeError(local_llm.reason_message(reason))

    speakers = meta.get("speakers", {})
    windows = _windows(turns)
    all_ops = []
    for w, window in enumerate(windows, 1):
        if len(windows) > 1:
            progress_cb(f"Tidying on this Mac ({w}/{len(windows)})…")
        else:
            progress_cb("Tidying on this Mac…")
        payload = {"speakers": speakers, "turns": window}
        try:
            ops = local_llm.generate(
                INSTRUCTIONS,
                "Transcript window to clean:\n" + json.dumps(payload, ensure_ascii=False),
                TIDY_SCHEMA, max_tokens=1200,
            )
        except local_llm.LocalLLMError as exc:
            raise RuntimeError(str(exc)) from exc
        if isinstance(ops, dict):
            all_ops.append(ops)

    progress_cb("Applying the clean-up…")
    new_turns, new_speakers, summary = apply_ops(meta, _merge_window_ops(all_ops))
    if not new_turns:
        raise RuntimeError("Clean-up would have removed the whole transcript; nothing applied.")

    # meta above is a snapshot taken BEFORE a model pass that runs for minutes,
    # and app.py's rename-speaker / rename-title endpoints (and the sync push
    # they trigger) are not blocked meanwhile — they write this same file.
    # Writing the whole snapshot back would silently revert them, so re-read and
    # merge only the keys this operation actually owns.
    try:
        latest = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Could not save the clean-up: {exc}") from exc
    if not isinstance(latest, dict):
        raise RuntimeError("meeting.json is unreadable; nothing applied.")

    # drop_turns/trim_turns are INDICES into the snapshot's turn list. If the
    # transcript itself changed underneath us (a recluster, another tidy) those
    # indices now address different turns, and the ops we just applied describe
    # a transcript that no longer exists. Refuse rather than corrupt it.
    if (latest.get("turns") or []) != turns:
        raise RuntimeError(
            "The transcript changed while it was being tidied; nothing applied. "
            "Please try again.")

    # Display names are only half ours. A label whose on-disk name still matches
    # the snapshot was not touched while we ran, so our value stands (including
    # a rename this run inferred). A label whose name CHANGED was renamed by the
    # user mid-run, and a human typing a name beats a model guessing one — keep
    # theirs. (A label merged away takes its name with it — unavoidable.)
    live_names = latest.get("speakers") or {}
    for label in new_speakers:
        live = live_names.get(label)
        if live is not None and live != speakers.get(label):
            new_speakers[label] = live

    backup = meeting_dir / "meeting.pretidy.json"
    if not backup.exists():
        # Back up what is on disk now, not the stale snapshot, so Undo restores
        # the user's current title/names along with the untidied transcript.
        _atomic_write_json(backup, latest)

    latest["turns"] = new_turns
    latest["speakers"] = new_speakers
    latest["stats"] = stats_mod.compute(new_turns, new_speakers, latest.get("duration") or 0.0)
    latest["tidied"] = summary
    latest["status"] = "done"
    try:
        _atomic_write_json(meta_path, latest)
    except OSError as exc:  # e.g. the meeting was deleted while tidying
        raise RuntimeError(f"Could not save the clean-up: {exc}") from exc
    log.info("tidied %s: %s", meeting_dir.name, summary)
    return summary
