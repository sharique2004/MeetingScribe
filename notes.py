"""Live meeting notes and pre-meeting cues — the storage behind the note-taker.

Two different kinds of writing happen around a meeting, and they want two
different files:

  * CUES are written BEFORE the meeting exists — talking points and questions
    the user wants to raise. There is no meeting folder to put them in yet, so
    they live in one staging file under the data directory and survive from
    meeting to meeting until the user clears them. When a recording starts they
    are COPIED into the meeting folder, frozen as "what I meant to cover this
    time", so the summary can later tell which of them were never addressed.
    The staging copy keeps its life afterwards; editing it cannot rewrite the
    history of a meeting that already happened.

  * NOTES are written DURING the meeting, and are the one thing here that
    cannot be recreated: the audio can be re-transcribed, a typed thought
    cannot. They are appended to notes.jsonl inside the meeting folder as they
    arrive — never accumulated in memory and written at stop, because the crash
    that loses a meeting is exactly the one that happens mid-meeting.

Every note is stored with the second it was typed at ("t", seconds into the
meeting). That timestamp is the whole point of a note file over a text blob: it
lets the summary line up what the user wrote against what was being said at
that moment, and lets the review UI turn a note into a click that seeks the
audio there.

THE "t" CONTRACT — null MEANS UNTIMED, 0.0 MEANS 0:00
------------------------------------------------------
A note's "t" is EITHER a float in [0, 86400) — real seconds into the meeting —
OR JSON null, meaning "we do not know when in the meeting this was typed".

Those two are not interchangeable and this module never turns one into the
other. An offset it cannot trust (missing, a string, NaN, inf, negative,
an epoch stamp) becomes null, NEVER 0.0, because 0.0 is a real position: the
review UI drops an untimed note off the transcript timeline and marks it "—" in
the Overview, but places a t=0.0 note against the first words of the meeting.
Coercing "no idea" to "the very start" is a quiet lie about where the user was
in the conversation when they wrote it, right next to words they were not
writing about — so the unusable value is thrown away and the ignorance kept.

The bounds are the ones the two readers already enforce, so all three agree on
the same JSON: templates/index.html's entryTime() and summarize.py's _entry()
both accept only 0 <= t < 24h and treat everything else as untimed.

Untimed notes sort last (see _merge_notes) — they have no place on a timeline —
and keep the order they were written in among themselves.

WHY JSONL FOR NOTES, AND WHY THAT IS STILL CRASH-SAFE
-----------------------------------------------------
The rest of this codebase publishes files with pipeline._atomic_write (unique
temp file in the same directory, target's mode carried over, fsync, os.replace)
because its writers REPLACE whole documents. A note append is not that: it adds
one short line while a meeting is live, and rewriting the entire file per note
would put every earlier note back at risk for each new one.

So notes.jsonl is opened O_APPEND and each note is handed to the OS as ONE
write() of one complete line, then fsync'd — and on the append that CREATES the
file, the parent directory is fsync'd too, because a file's bytes reaching the
device are worth nothing if the directory entry naming them does not (see
_fsync_dir). Concurrent appenders cannot
interleave (O_APPEND makes the seek-and-write indivisible), and a crash can
damage nothing but the tail: the worst case on disk is a truncated final line,
with every note before it byte-for-byte intact. read_notes() therefore skips
lines it cannot parse instead of refusing the file — a half-written last line
costs that one note, never the meeting.

Cues ARE whole-document writes, so they go through pipeline._atomic_write_text
like every other document in the app.

NOTHING TYPED IS EVER DROPPED ON THE WAY TO meeting.json
--------------------------------------------------------
notes.jsonl is the durable record, but every reader — the review UI, the
summary, the phone copy — looks at meeting.json. fold() is the one bridge
between the two, and it is a UNION rather than a copy: whichever of the two
files a note is in, it comes out. That is what lets it run on any path (stop,
load, reprocess, a late note arriving after stop) without one of them being
able to undo another.
"""

import json
import math
import os
import threading
from collections import Counter
from pathlib import Path

import pipeline
from config import BASE_DIR, DATA_DIR


def _state_dir():
    """Where cross-meeting user state belongs — never the code checkout.

    DATA_DIR is normally ~/.meetingscribe (the packaged app sets
    MEETINGSCRIBE_DATA so recordings survive an app update). But run from a
    SOURCE CHECKOUT there is no such variable and config.py falls back to
    BASE_DIR — the repository itself. Cues are the user's private preparation
    for a meeting that has not happened yet, and this repo is PUBLIC: dropped in
    the working tree they are not ignored, so one `git add -A && git push`
    publishes them. recordings/ and config.json get away with living there
    because .gitignore covers them by name; a new file gets no such protection,
    and a privacy leak must not depend on someone remembering to add a line.

    So when the data directory IS the checkout, this returns ~/.meetingscribe —
    the same place the packaged app already keeps its state — and the cues stay
    out of git no matter what .gitignore says.
    """
    try:
        inside_checkout = DATA_DIR.resolve() == BASE_DIR.resolve()
    except OSError:  # unreadable path: assume the risky case
        inside_checkout = DATA_DIR == BASE_DIR
    if not inside_checkout:
        return DATA_DIR
    try:
        return Path.home() / ".meetingscribe"
    except RuntimeError:  # no home directory at all
        return DATA_DIR


# Pre-meeting cues, staged outside any meeting. Kept beside config.json in the
# user's state directory (the packaged .app's bundle is read-only, and a source
# checkout is a git working tree — see _state_dir).
CUES_PATH = _state_dir() / "cues.json"
# Where earlier builds put it. Only different in a source checkout.
LEGACY_CUES_PATH = DATA_DIR / "cues.json"

# Names inside a meeting folder. NOTES_FILE is referenced from meeting.json so
# a reader that only has the meeting can find the notes without guessing.
NOTES_FILE = "notes.jsonl"
CUES_FILE = "cues.json"

# Limits.
#
# MAX_NOTE_CHARS is a REFUSAL boundary, not a truncation one. It used to be 2000
# and append_note silently cut the text down to it while the route still
# answered 200 {"ok": true} — so a pasted agenda came back shorter than it went
# in and nothing anywhere said so. A note is the one thing in this app that
# cannot be reconstructed; losing half of one and calling it a success is worse
# than either keeping it whole or refusing it out loud.
#
# So: anything up to this length is stored EXACTLY as typed, and the ceiling is
# now ten times what a person types during a meeting, which puts every real note
# — pastes included — safely under it. Past it, append_note raises NoteTooLong
# and the caller must tell the user rather than store a shortened copy; the
# ceiling still exists so a stuck client cannot fill the disk or push a
# meeting.json the review UI has to render in one paragraph.
MAX_NOTE_CHARS = 20000
MAX_CUE_CHARS = 300
MAX_CUES = 50

# Anything at or beyond this is a wall-clock stamp, not an offset into a
# meeting — the same bound templates/index.html and summarize.py apply.
DAY_SECONDS = 24 * 3600

# A "t" we could not trust. Spelled out so callers can say what they mean.
UNTIMED = None


class NoteTooLong(ValueError):
    """append_note refused: the text is longer than MAX_NOTE_CHARS.

    Carries the numbers so the caller can tell the user what to do about it.
    Nothing was written — the note is still only in the client's hands, which is
    the point: it must decide, not have a shortened copy filed on its behalf.
    """

    def __init__(self, length, limit=MAX_NOTE_CHARS):
        super().__init__(f"note is {length} characters; the limit is {limit}")
        self.length = length
        self.limit = limit

# Appends are one write() each, but two threads can still race between opening
# the file and counting the result. The lock keeps the returned count honest;
# it is never held across anything slower than a single small write + fsync.
_APPEND_LOCK = threading.Lock()


# ------------------------------------------------------------------ notes ----

def seconds(value):
    """A client-supplied offset -> float seconds into the meeting, or UNTIMED.

    The one place the "t" contract at the top of this file is enforced. Returns
    UNTIMED (None) for anything it cannot vouch for, and NEVER 0.0 as a stand-in
    for that — see the contract for why "at 0:00" and "no idea when" have to
    stay distinguishable all the way to the UI.

    What gets rejected, and why each one matters:

      * not a number at all (None, "", a string, a list) — a missing or
        hand-edited field.
      * bool — True is not "1 second in". `isinstance(True, int)` is True in
        Python, so this has to be said explicitly.
      * NaN / inf — json.loads turns `1e999` into inf and json.dumps writes it
        back out as the bare token `Infinity`, which Python accepts but JSON.parse
        does not: ONE such note would make the whole /notes response unparseable
        in the browser and take every other note in the meeting down with it. NaN
        is the same story and additionally makes the notes unsortable.
      * >= DAY_SECONDS — a wall-clock/epoch stamp that leaked in where an offset
        belonged. Placing it on the timeline would put the note past the end of
        every meeting; the readers already ignore it, so store what they see.
      * < 0 by more than a second — an offset from before the meeting started
        cannot be a position in it.

    A small negative (down to -1s) is float noise from two clocks being
    subtracted, not a wrong answer, so it clamps to 0.0 rather than losing its
    timestamp.
    """
    if isinstance(value, bool):
        return UNTIMED
    try:
        t = float(value)
    except (TypeError, ValueError):
        return UNTIMED
    if not math.isfinite(t) or t < -1.0 or t >= DAY_SECONDS:
        return UNTIMED
    return round(max(0.0, t), 3)


def _sort_key(note):
    """Order notes for a timeline: timed first by offset, untimed last.

    An untimed note has no position, so it cannot be placed among the ones that
    do without inventing one. Sorting it to the end keeps it visible in the
    Overview list without claiming a moment for it, and — because list.sort is
    stable — untimed notes keep the order they were typed in.
    """
    t = note.get("t")
    return (1, 0.0) if t is None else (0, t)


def notes_path(meeting_dir):
    return Path(meeting_dir) / NOTES_FILE


def _fsync_dir(directory):
    """Flush a DIRECTORY's own contents to the device. -> True if it landed.

    fsync on a file commits the bytes IN it, not the name that points AT it.
    The first note of a meeting creates notes.jsonl, and that creation is a
    change to the parent directory: until the directory itself is flushed, a
    power cut can come back to a folder with no notes.jsonl in it at all, bytes
    fsync'd or not. Every append after that only extends a name that already
    exists, so this is needed exactly once per meeting — the same reason
    pipeline._atomic_write's os.replace and the Swift draft store's rename
    (NotesPanel.swift's writeAtomically) both flush the directory after
    publishing a name.

    That window matters more here than anywhere else in the app: the panel
    deletes its on-disk draft the moment it sees the 200, so between the reply
    and the directory reaching the platter the user's note exists in exactly one
    place. This runs BEFORE append_note returns, so the 200 means both.

    Best effort, like the file fsync above it: a filesystem that refuses to open
    or flush a directory (some network mounts) must cost us nothing — the note
    is already written and fsync'd either way.
    """
    try:
        fd = os.open(str(directory), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return False
    try:
        os.fsync(fd)
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


def append_note(meeting_dir, text, t):
    """Append one note to a meeting, VERBATIM. Returns the total note count.

    `t` is seconds into the meeting, or anything unusable for an untimed note
    (see seconds()). Raises ValueError for an empty note and NoteTooLong for one
    over MAX_NOTE_CHARS — the text is never shortened to fit, because a caller
    that is told 'saved' has to be able to believe every character of it landed.

    Cheap by construction: one open, one write, one fsync — plus, on the note
    that creates notes.jsonl, one fsync of the meeting folder so the new file's
    NAME is as durable as its bytes (see _fsync_dir; every later note skips it).
    The panel calls this while the audio threads are running, so it takes no
    application lock and never touches meeting.json.

    When it returns, the note is on the device. That is the whole promise the
    route's 200 is repeating, and the client deletes its own draft on that 200.
    """
    text = str(text or "").strip()
    # A JSON body can carry a lone surrogate ("\ud800"), which is a legal str
    # but has no UTF-8 encoding: without this the write below would raise
    # UnicodeEncodeError — a ValueError, not the OSError the route expects —
    # and the note would be lost to an HTML 500 instead of being stored.
    text = text.encode("utf-8", "replace").decode("utf-8")
    if not text:
        raise ValueError("empty note")
    # Checked AFTER the cleanup above so the number in the error is the number
    # of characters that would actually have been stored, and refused rather
    # than trimmed: a truncated note reported as a success is the one outcome
    # this file exists to prevent.
    if len(text) > MAX_NOTE_CHARS:
        raise NoteTooLong(len(text))
    t = seconds(t)

    path = notes_path(meeting_dir)
    # json.dumps escapes \n and \r inside the text, so one line really is one
    # note. (It does NOT escape U+0085/U+2028/U+2029 — see read_notes.)
    line = json.dumps({"t": t, "text": text}, ensure_ascii=False) + "\n"
    with _APPEND_LOCK:
        # O_RDWR, not O_WRONLY, so the newline check below can read a byte.
        # O_EXCL first purely to LEARN whether this call is the one that creates
        # the file: a created name is a change to the directory, and only that
        # case has to pay for a directory fsync (see _fsync_dir). Falling back
        # to a plain O_CREAT open keeps the behaviour identical either way, and
        # a second appender — even in another process — simply takes the second
        # branch. Nothing is truncated on either path.
        created = True
        try:
            fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_APPEND,
                         0o666)
        except FileExistsError:
            created = False
            fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o666)
        try:
            # If the file does not end in a newline, the previous append died
            # part-way through its line. That note is already lost; opening a
            # fresh line keeps it from taking this one down with it, which is
            # what plain appending would do — the fragment and the new note
            # would fuse into one unparseable line and BOTH would disappear.
            try:
                end = os.fstat(fd).st_size
                if end and os.pread(fd, 1, end - 1) != b"\n":
                    line = "\n" + line
                elif not end:
                    # Empty, so either we just created it or the file another
                    # writer created is still empty — and in the one race the
                    # O_EXCL probe above cannot see (the file removed between
                    # the two opens, so the fallback open created it after all)
                    # this is what still buys the directory flush. Flushing a
                    # directory that did not change costs a syscall; skipping
                    # one that did costs the note.
                    created = True
            except OSError:
                pass
            buf = line.encode("utf-8")
            while buf:  # os.write may take only part of the buffer
                buf = buf[os.write(fd, buf):]
            # Durability is the reason this file exists at all: a note the user
            # typed must outlive the crash that happens ten seconds later.
            try:
                os.fsync(fd)
            except OSError:
                pass  # best effort — a filesystem refusing fsync must not
                      # cost us the note itself
            # …and the file's NAME, the first time round: fsync(fd) commits the
            # bytes, the directory commits the entry that leads to them. Both
            # happen before this function returns, because the caller answers
            # 200 on it and the client throws its draft away on the 200.
            if created:
                _fsync_dir(path.parent)
        finally:
            os.close(fd)
        return len(read_notes(meeting_dir))


def read_notes(meeting_dir):
    """Every note in a meeting, oldest first.

    Tolerant on purpose — see the module docstring. A line that will not parse
    is a note lost to a crash mid-append; the ones around it are still good.
    """
    path = notes_path(meeting_dir)
    out = []
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return out
    # split("\n"), NOT splitlines(). str.splitlines() also breaks on U+0085,
    # U+2028 and U+2029 — which json.dumps(ensure_ascii=False) writes out RAW,
    # unlike \n and \r which it escapes. A note containing one (they ride in on
    # text pasted from a word processor, and on some emoji through a mangled
    # encoding) would be cut in two, and both halves would then fail to parse:
    # the note would vanish silently. Only the "\n" this module writes as a
    # terminator ever separates two records.
    for line in raw.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except ValueError:
            continue  # truncated tail from a crash, or a partial write
        if isinstance(item, dict) and item.get("text"):
            # seconds(), not float(): a hand-edited or half-written line can
            # carry a "t" that is a string or null, and one bare float() raising
            # here would fail the WHOLE read — including at stop, where it would
            # propagate out of attach() and leave the meeting stuck in
            # "recording". One bad field costs its own timestamp (the note comes
            # back untimed), nothing else.
            out.append({"t": seconds(item.get("t")), "text": str(item["text"])})
    return out


def _clean_notes(values):
    """Normalize whatever meeting.json is carrying into [{"t", "text"}, …].

    Deliberately forgiving: this reads a document an OLDER build wrote, and the
    only wrong answer is dropping a note because its shape is not the current
    one. A bare string is a note with no timestamp, not nothing — and "no
    timestamp" is UNTIMED, not 0.0, so a build that stored notes as plain
    strings does not come back with all of them stacked on the first second of
    the meeting.
    """
    if not isinstance(values, (list, tuple)):
        return []
    out = []
    for v in values:
        if isinstance(v, dict):
            text = str(v.get("text") or "").strip()
            t = seconds(v.get("t"))
        else:
            text, t = str(v or "").strip(), UNTIMED
        if text:
            out.append({"t": t, "text": text})
    return out


def _merge_notes(stored, disk):
    """Union of the notes in meeting.json and the ones in notes.jsonl.

    A union, not a replacement, because either side can hold a note the other
    does not and neither is allowed to lose one:

      * notes.jsonl has what meeting.json missed — every note of a recording
        that was killed before stop could fold them in, and any note that
        arrived after the stop write.
      * meeting.json has what notes.jsonl lost — the file is append-only and
        crash-tolerant, but it is not immortal: a crash mid-append costs its
        tail line, and a folder copied or restored without it comes back with
        the notes only in the meeting document.

    Duplicates are matched by (t, text) as a MULTISET, so a note that genuinely
    appears twice in notes.jsonl stays twice; only the copy already carried in
    meeting.json is recognised as the same note and not doubled.

    Sorted by timestamp because that is the order the transcript is rendered in
    — the review UI interleaves each note into the turns by its `t`. The sort is
    stable, so notes sharing a timestamp keep the order they were written in,
    and it goes through _sort_key because an untimed note's `t` is None: a bare
    `key=lambda n: n["t"]` would raise TypeError comparing None to a float and
    take the whole fold — including the one on the stop path — down with it.
    """
    out = list(disk)
    unmatched = Counter((n["t"], n["text"]) for n in disk)
    for note in stored:
        key = (note["t"], note["text"])
        if unmatched.get(key):
            unmatched[key] -= 1  # already on disk — the same note, not a second
            continue
        out.append(note)
    out.sort(key=_sort_key)
    return out


def fold(meeting_dir, meta):
    """Make `meta` carry every note this meeting has. -> True if it changed.

    THE contract between the note-taker and everything that renders a meeting:

        meta["notes"]      = [{"t": float|None, "text": str}, …]
                             `t` is seconds into the meeting, or null when we do
                             not know when it was typed — null is NOT 0.0, see
                             the "t" contract at the top of this file. Ordered
                             by `t`, untimed last; absent when there are none.
        meta["notes_file"] = "notes.jsonl"   — the durable copy, same folder

    Idempotent and order-independent, which is what makes it safe to call from
    every path that touches a meeting (stop, load, reprocess, a late note):
    running it twice changes nothing the second time, and running it after
    another writer already folded the same notes in is a no-op rather than a
    fight. The return value exists so callers can skip a meeting.json write they
    do not need — rewriting it per read would churn mtimes for nothing.

    `meta["notes"]` is NOT among the keys pipeline.py's process/recluster runs
    claim, so both carry it over from disk untouched; it survives a Reprocess
    and a speaker-count change without either having to know about notes.

    Never raises: it runs on the stop path, where an exception would leave the
    meeting stuck in "recording" with its audio unprocessed.
    """
    try:
        disk = read_notes(meeting_dir)
    except Exception:  # noqa: BLE001 — a note is never worth failing a stop for
        disk = []
    before = meta.get("notes")
    merged = _merge_notes(_clean_notes(before), disk)
    if not merged:
        return False
    meta["notes"] = merged
    meta["notes_file"] = NOTES_FILE
    return merged != before


# ------------------------------------------------------------------- cues ----

def _clean_cues(values):
    """Normalize whatever a client sent into a list of short, non-empty lines."""
    # A bare string is iterable, so a stored/POSTed "abc" would otherwise come
    # back as three one-character cues. Anything that is not a sequence of
    # cues is no cues.
    if not isinstance(values, (list, tuple)):
        return []
    cleaned = []
    for v in values:
        if isinstance(v, dict):  # tolerate [{"text": …}] from a richer client
            v = v.get("text")
        s = " ".join(str(v or "").split())[:MAX_CUE_CHARS]
        # Same lone-surrogate hazard as a note: a cue that cannot be encoded to
        # UTF-8 would blow up the atomic write with a UnicodeEncodeError.
        s = s.encode("utf-8", "replace").decode("utf-8")
        if s:
            cleaned.append(s)
        if len(cleaned) >= MAX_CUES:
            break
    return cleaned


def read_cues():
    """The staged pre-meeting cues. Missing or corrupt file -> no cues."""
    try:
        data = json.loads(CUES_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if isinstance(data, dict):
        data = data.get("cues")
    return _clean_cues(data)


def write_cues(values):
    """Replace the staged cues. Returns what was actually stored."""
    cues = _clean_cues(values)
    CUES_PATH.parent.mkdir(parents=True, exist_ok=True)
    pipeline._atomic_write_text(
        CUES_PATH, json.dumps({"cues": cues}, ensure_ascii=False, indent=1))
    return cues


def _migrate_legacy_cues():
    """Move a cues.json an earlier build left in the checkout out of it.

    Changing CUES_PATH alone would stop WRITING there but leave whatever is
    already on disk sitting in the working tree, which is the file that leaks.
    So the old one is moved, not just abandoned: its contents win if the new
    location is missing or older (it is then the user's most recent edit), and
    the old file is deleted either way.

    Best effort from start to finish — a failure here must never stop the app
    from starting, and the worst case is the same file staying where it was.
    """
    if LEGACY_CUES_PATH == CUES_PATH or not LEGACY_CUES_PATH.exists():
        return
    try:
        legacy_mtime = LEGACY_CUES_PATH.stat().st_mtime
        try:
            newer = legacy_mtime > CUES_PATH.stat().st_mtime
        except OSError:
            newer = True  # nothing at the new location yet
        if newer:
            body = LEGACY_CUES_PATH.read_text(encoding="utf-8")
            CUES_PATH.parent.mkdir(parents=True, exist_ok=True)
            pipeline._atomic_write_text(CUES_PATH, body)
        LEGACY_CUES_PATH.unlink()
    except (OSError, ValueError):
        pass


try:
    _migrate_legacy_cues()
except Exception:  # noqa: BLE001 — importing this module must never fail
    pass


def read_meeting_cues(meeting_dir):
    """The cues frozen into a meeting at record start (not the staged list)."""
    try:
        data = json.loads((Path(meeting_dir) / CUES_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if isinstance(data, dict):
        data = data.get("cues")
    return _clean_cues(data)


# ------------------------------------------------------- meeting lifecycle ----

def begin_meeting(meeting_dir, meta=None):
    """Called once when a recording starts.

    Freezes the staged cues into the meeting folder and, if `meta` is given,
    stamps them onto the meeting dict so the caller's existing meeting.json
    write carries them — no second write, no read-modify-write race with
    whoever else has the folder open. Returns the cues.

    Never raises: a note-taking feature must not be able to stop a recording.
    """
    try:
        cues = read_cues()  # already swallows a missing or unreadable file
    except Exception:
        cues = []
    if cues:
        try:
            pipeline._atomic_write_text(
                Path(meeting_dir) / CUES_FILE,
                json.dumps({"cues": cues}, ensure_ascii=False, indent=1))
        except Exception:
            pass  # meta still carries them into meeting.json below
    if meta is not None:
        meta["notes_file"] = NOTES_FILE  # where the live notes are being written
        if cues:
            meta["cues"] = cues
    return cues


def attach(meeting_dir, meta):
    """Fold the meeting's notes and cues into `meta` for the caller to write.

    Used at stop, at crash recovery and by anything else that rebuilds
    meeting.json, so the review UI and the summary see the notes without having
    to know about notes.jsonl. Mutates and returns `meta`.

    fold() does the notes half and is a union, which is what makes this safe to
    run over a meeting that already has some: a stop that raced one last note,
    and a recovery pass over a meeting stopped normally, both come out with the
    complete set instead of one version replacing the other.
    """
    # This runs on the /api/record/stop path, where meta is about to be written
    # with the track durations and status="processing". Nothing about notes is
    # worth failing that write for: an exception here would leave the meeting
    # marked "recording" forever with its audio unprocessed. Read defensively
    # and carry on with whatever was recoverable.
    try:
        fold(meeting_dir, meta)
    except Exception:  # noqa: BLE001 — fold() is already defensive; belt and braces
        pass
    # Set even when there are no notes yet: at stop this says "this build stores
    # notes here", which is what tells a later reader the file is worth looking
    # for. fold() deliberately leaves it alone when it has nothing to add, so
    # that a read of an old meeting does not dirty its meeting.json.
    meta["notes_file"] = NOTES_FILE
    try:
        cues = read_meeting_cues(meeting_dir)
    except Exception:
        cues = []
    if cues:
        meta["cues"] = cues
    return meta
