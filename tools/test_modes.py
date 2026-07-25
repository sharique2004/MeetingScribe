#!/usr/bin/env python3
"""CONTRACT 1: what the speaker count MEANS, across mode x basis x source.

THE DEFECT CLASS
----------------
"3 speakers" is not one number. In the normal controls it means "3 people BESIDES
me"; under the mic-only fallback the control counts every voice the microphone
picked up, me included. The same integer therefore means 3 or 4 clusters
depending on which screen produced it, and the meeting.json it is stored in is
read back by a later Reprocess that no longer knows which screen that was. Every
off-by-one here is a phantom speaker or a merged one, and neither is visible
until a human reads the transcript.

THE CONTRACT UNDER TEST (from the shared brief, implemented in pipeline.py)

    meta["speaker_count_source"] == "user"   a human picked the count
    meta["speaker_count_basis"]  == "total"  when diarization_mode == "mic_fallback"
                                 == "others" otherwise
    "others" EXCLUDES the local user ("Speakers besides you")
    "total"  INCLUDES every voice on the mic ("Voices on the mic (you are one of them)")
    _user_speaker_count() returns count when basis == "total", else count + 1

LEGACY MEETINGS (no basis key at all) read as "others", because the only
speaker-count control that has ever SHIPPED asked "Speakers besides you" — the
mic-fallback control and diarization_mode are both new in this uncommitted work,
so no meeting.json on disk can hold a "total"-basis count. That premise is what
makes the rule safe, so it is asserted below rather than assumed: if a
mic-fallback meeting could predate the basis key, this reading would silently add
one to a count the user had already corrected by hand, and the speaker count
would climb by one on every Reprocess.

Runs from the frozen fixture corpora — no audio, no transcription, no network.
Nothing under recordings/ or ~/.meetingscribe/ is read or written. The committed
test/synthetic/ corpus is the baseline and always runs; test/fixtures/ is an
optional deeper LOCAL corpus and is skipped with a message when absent (it is
gitignored: real participants' names and voice embeddings). See test_diarization
.py, which owns the corpus plumbing.

    ~/.meetingscribe/venv/bin/python tools/test_modes.py

Check functions are named check_* and all work sits behind __main__, so a stray
`pytest tools/` collects and runs nothing.
"""

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import pipeline  # noqa: E402

sys.path.insert(0, str(REPO / "tools"))
from test_diarization import (  # noqa: E402  (fixture plumbing, no side effects)
    label_as_online_mic_only,
    recluster_workdir,
    report,
    run,
)

TEMPLATE = REPO / "templates" / "index.html"

USER = pipeline.SPEAKER_COUNT_USER  # "user"

# ---------------------------------------------------------------------------
# (diarization_mode, speaker_count_basis, speaker_count_source, expected)
#   -> what _user_speaker_count() must return, and why.
#
# The return value is always "how many voices are on the mic", because that is
# the only thing the caller (the mic-only fallback) can act on.
# ---------------------------------------------------------------------------
COUNT_TABLE = [
    # mode,           basis,    source,     expected, want, why
    ("mic_fallback", "total",  USER,        2, 2,
     "user counted every voice on the mic; use it as-is"),
    ("mic_fallback", "total",  USER,        1, 1,
     "1 total voice = the mic was just you; this is how a false positive is undone"),
    ("mic_fallback", "total",  USER,        4, 4,
     "a busy room, taken literally"),
    (None,           "others", USER,        1, 2,
     "1 speaker BESIDES you = 2 voices on the mic"),
    (None,           "others", USER,        2, 3,
     "2 besides you = 3 on the mic"),
    (None,           "others", USER,        0, None,
     "0 is not a count anyone chose"),
    (None,           "others", None,        2, None,
     "no source: expected_speakers is app.py's calendar guess, not an observation"),
    ("mic_fallback", "total",  None,        2, None,
     "same guess, same answer, even under the fallback"),
    ("mic_fallback", "total",  "calendar",  3, None,
     "any source that is not the human is a guess"),
    ("mic_fallback", "total",  USER,     None, None, "nothing was chosen"),
    ("mic_fallback", "total",  USER,       "", None, "nothing was chosen"),
    ("mic_fallback", "total",  USER,    "junk", None, "unparseable stays unforced"),
    ("mic_fallback", "total",  USER,       -2, None, "negative is not a count"),
    ("mic_fallback", "total",  USER,      "3", 3,
     "a string from a <select> is still a count"),
]

# Legacy rows: written before speaker_count_basis existed, so the key is absent
# entirely. Every one of them came from the shipped "Speakers besides you"
# control, so they read as "others" — see check_legacy_premise_still_holds for
# why that is a fact about the data and not an assumption.
LEGACY_TABLE = [
    (None, USER, 1, 2,
     "legacy meeting: the shipped control said 'Speakers besides you', so 1 "
     "excludes the user and the mic holds 2"),
    (None, USER, 2, 3,
     "legacy meeting: 2 besides you means 3 voices on the mic"),
    (None, None, 2, None, "legacy calendar guess is still a guess"),
    ("mic_fallback", USER, 2, 3,
     "a meeting that reached the fallback WITHOUT a basis key can only be one "
     "this uncommitted build produced, and the fallback control is new too — "
     "the read stays 'others' so there is exactly one back-compat rule"),
]

# CONTRACT 1 also pins the UI labels. These are the exact strings the template
# must use, because they are what makes the stored number mean what the basis
# says it means.
REQUIRED_LABELS = {
    "Voices on the mic (you are one of them)": 'the "total" basis (mic_fallback)',
    "People in the room (you included)": 'the "others" basis, in-person wording',
    "Speakers besides you": 'the "others" basis, online wording',
}


def make_meta(mode, basis, source, expected):
    meta = {"expected_speakers": expected}
    if mode is not None:
        meta["diarization_mode"] = mode
    if basis is not None:
        meta["speaker_count_basis"] = basis
    if source is not None:
        meta["speaker_count_source"] = source
    return meta


# -------------------------------------------------------------------- checks --

def check_user_speaker_count_table(fail):
    """_user_speaker_count() over the full (mode x basis x source x count) grid."""
    for mode, basis, source, expected, want, why in COUNT_TABLE:
        meta = make_meta(mode, basis, source, expected)
        got = pipeline._user_speaker_count(meta)
        mark = "ok " if got == want else "BAD"
        print(f"    {mark} mode={str(mode):13s} basis={str(basis):7s} "
              f"source={str(source):9s} count={str(expected):6s} -> {str(got):5s} "
              f"(want {want})")
        if got != want:
            fail(f"_user_speaker_count(mode={mode!r}, basis={basis!r}, "
                 f"source={source!r}, expected_speakers={expected!r}) returned "
                 f"{got!r}, contract says {want!r} — {why}")


def check_legacy_meetings_keep_their_meaning(fail):
    """A meeting.json with NO speaker_count_basis must not shift by one."""
    for mode, source, expected, want, why in LEGACY_TABLE:
        meta = make_meta(mode, None, source, expected)
        if "speaker_count_basis" in meta:
            fail("test bug: the legacy row was built with a basis key")
        got = pipeline._user_speaker_count(meta)
        mark = "ok " if got == want else "BAD"
        print(f"    {mark} legacy mode={str(mode):13s} source={str(source):5s} "
              f"count={expected} -> {str(got):5s} (want {want})")
        if got != want:
            fail(f"legacy meeting (diarization_mode={mode!r}, no "
                 f"speaker_count_basis, count={expected}) resolved to {got!r}, "
                 f"contract says {want!r} — {why}")


def check_legacy_premise_still_holds(fail, corpus):
    """Guards the assumption the back-compat rule rests on.

    "A missing basis means 'others'" is only safe while no meeting on disk can
    have been counted against the mic-fallback control. Two things must stay
    true for that:

      * no meeting.json in the corpus carries diarization_mode, so none of them
        was ever presented with the "you are one of them" question;
      * the fallback control's own label is reachable only through
        diarization_mode in the template, so it cannot be shown on a meeting
        that is not flagged.

    If a future build ever ships a "total"-basis count without the basis key,
    this premise breaks and the rule must change with it.
    """
    flagged = []
    dirs = corpus.dirs()
    for d in dirs:
        meta = json.loads((d / "meeting.json").read_text(encoding="utf-8"))
        if meta.get("diarization_mode") is not None:
            flagged.append((d.name, meta.get("diarization_mode"),
                            meta.get("speaker_count_basis")))
    print(f"    {len(dirs)} frozen meeting.json files, "
          f"{len(flagged)} carrying diarization_mode")
    for slug, mode, basis in flagged:
        if basis is None:
            fail(f"{slug} is flagged diarization_mode={mode!r} but has no "
                 "speaker_count_basis — the 'missing basis means others' rule "
                 "would misread any user count on it")

    html = TEMPLATE.read_text(encoding="utf-8") if TEMPLATE.exists() else ""
    label = "Voices on the mic (you are one of them)"
    if label in html and "mic_fallback" not in html:
        fail("the template can show the mic-fallback count label without "
             "consulting diarization_mode, so a 'total'-basis count could be "
             "produced on an unflagged meeting")


def check_basis_records_the_question_on_screen(fail, corpus):
    """THE off-by-one that survives a Reprocess. Driven through recluster_meeting.

    The basis must describe the question the user was ANSWERING, not the verdict
    the run that follows reaches. The case that separates them:

      1. a meeting is in the fallback, so the control asks "voices on the mic";
      2. the user answers 1, meaning "it was just me";
      3. the run honours that, reverts to "You", and CLEARS diarization_mode.

    Stamped from the mode on disk (correct) the basis is "total", and 1 keeps
    meaning one voice for ever. Stamped from the mode the run concluded, it
    would be "others" — and the next Reprocess reads that same 1 as "one person
    besides me", forces 2 clusters back onto the mic, and destroys the "You"
    label the user just restored. The user then fixes it again, and again.
    """
    with recluster_workdir(corpus, corpus.multi_mic) as work:
        meta_path = work / "meeting.json"

        pipeline.recluster_meeting(work, None)
        disk = json.loads(meta_path.read_text(encoding="utf-8"))
        print(f"    auto        -> mode={str(disk.get('diarization_mode')):13s} "
              f"basis={str(disk.get('speaker_count_basis')):8s} "
              f"speakers={len(disk['speakers'])}")
        if disk.get("diarization_mode") != "mic_fallback":
            fail("setup: the multi-voice fixture did not enter the fallback")
        if "speaker_count_basis" in disk:
            fail("an auto re-cluster (no user count) left a stale "
                 f"speaker_count_basis={disk['speaker_count_basis']!r} behind")
        if "speaker_count_source" in disk:
            fail("an auto re-cluster left a stale speaker_count_source behind")

        # The user, looking at the fallback, says "one voice — that was just me".
        pipeline.recluster_meeting(work, 1)
        disk = json.loads(meta_path.read_text(encoding="utf-8"))
        print(f"    user says 1 -> mode={str(disk.get('diarization_mode')):13s} "
              f"basis={str(disk.get('speaker_count_basis')):8s} "
              f"speakers={json.dumps(disk['speakers'])}")
        if disk["speakers"] != {"you": "You"}:
            fail(f"a user count of 1 under the fallback did not restore 'You': "
                 f"{disk['speakers']}")
        if disk.get("speaker_count_basis") != "total":
            fail("the count was taken on the mic-fallback control but stamped "
                 f"speaker_count_basis={disk.get('speaker_count_basis')!r}; it "
                 "must be 'total' — derived from the diarization_mode ON DISK "
                 "when the user clicked, not from the mode this run concluded")
        if disk.get("speaker_count_source") != USER:
            fail("a count taken from the speaker-count control was not marked "
                 f"speaker_count_source={USER!r}")

        # A later Reprocess re-reads exactly this meeting.json. It must reach
        # the same conclusion — this is the "again, and again" that a wrong
        # basis produces.
        again, _ = label_as_online_mic_only(corpus, corpus.multi_mic, meta_overrides={
            k: disk[k] for k in
            ("expected_speakers", "speaker_count_source", "speaker_count_basis")
            if k in disk
        })
        print(f"    reprocess   -> speakers={json.dumps(again['speakers'])}")
        if again["speakers"] != {"you": "You"}:
            fail("the correction did not survive a Reprocess: the meeting fell "
                 f"back into {again['speakers']} — the stored count is being "
                 "read on a different basis than it was written on")


def check_round_trip_does_not_drift(fail, corpus):
    """Reprocess must be a fixed point for a count > 1 as well.

    The user sets the count once. It is stored, read back on the next run, and
    stored again. If the write basis and the read basis disagree by one, the
    speaker count climbs by one on every Reprocess — the failure mode that is
    invisible until someone counts the speakers in the transcript.
    """
    with recluster_workdir(corpus, corpus.multi_mic) as work:
        meta_path = work / "meeting.json"
        pipeline.recluster_meeting(work, None)  # into the fallback first
        counts = []
        for i in range(3):
            pipeline.recluster_meeting(work, 3)
            disk = json.loads(meta_path.read_text(encoding="utf-8"))
            counts.append(len(disk["speakers"]))
            print(f"    pass {i + 1}: user count 3 -> {counts[-1]} speaker(s), "
                  f"basis={disk.get('speaker_count_basis')!r}")
        if counts != [3, 3, 3]:
            fail(f"the speaker count drifted across repeated Reprocesses: "
                 f"{counts} for a user-chosen 3. The basis written on one pass "
                 "is not the basis the next pass reads it on.")


def check_template_labels_match_the_basis(fail):
    """The UI wording IS the contract — read-only check of templates/index.html.

    If the fallback control ever says "Speakers besides you", the number the
    user types stops matching the basis pipeline stores it on, and no amount of
    correctness in pipeline.py can recover the meaning.
    """
    if not TEMPLATE.exists():
        fail(f"no template at {TEMPLATE}")
        return
    html = TEMPLATE.read_text(encoding="utf-8")
    for label, why in REQUIRED_LABELS.items():
        present = label in html
        print(f"    {'ok ' if present else 'BAD'} {label!r} -> {why}")
        if not present:
            fail(f"templates/index.html no longer contains {label!r}, the label "
                 f"CONTRACT 1 pins for {why}")
    if "mic_fallback" not in html:
        fail("templates/index.html never mentions mic_fallback, so it cannot be "
             "choosing the label from diarization_mode")


def check_no_path_forces_a_guess(fail):
    """Across the grid, only a human-chosen count may reach the clusterer.

    Restates the table as the property that matters: for every combination
    where speaker_count_source is not "user", _user_speaker_count is None and
    the fallback therefore runs the auto path, which alone may collapse to one
    cluster. A forced count cannot: cluster(n_speakers=N) applies neither
    _merge_tiny_clusters nor _fold_weak_clusters and returns exactly N.
    """
    for mode in (None, "mic_fallback"):
        for basis in (None, "others", "total"):
            for source in (None, "calendar", "auto", "", USER):
                for count in (1, 2, 5):
                    meta = make_meta(mode, basis, source, count)
                    got = pipeline._user_speaker_count(meta)
                    if source != USER and got is not None:
                        fail(f"a non-human count leaked through: mode={mode!r} "
                             f"basis={basis!r} source={source!r} count={count} "
                             f"-> {got!r} (must be None)")
                    if source == USER and got is None:
                        fail(f"a human-chosen count was ignored: mode={mode!r} "
                             f"basis={basis!r} source={source!r} count={count}")
                    if source == USER and got is not None and got < 1:
                        fail(f"a human-chosen count resolved below 1: mode={mode!r} "
                             f"basis={basis!r} count={count} -> {got!r}")
    print("    90 (mode x basis x source x count) combinations: "
          "guesses blocked, human counts honoured")


# CONTRACT 1 is arithmetic over a meta dict, so most of it is corpus-independent
# and runs exactly once. Only the three checks that read frozen meetings or drive
# recluster_meeting() need fixtures, and those run against every corpus present.
PURE_CHECKS = [
    ("_user_speaker_count over the full grid", check_user_speaker_count_table),
    ("legacy meetings with no basis key", check_legacy_meetings_keep_their_meaning),
    ("template labels match the basis", check_template_labels_match_the_basis),
    ("no path forces a guessed count", check_no_path_forces_a_guess),
]

CORPUS_CHECKS = [
    ("the legacy premise still holds", check_legacy_premise_still_holds),
    ("basis records the question on screen", check_basis_records_the_question_on_screen),
    ("the count survives repeated Reprocesses", check_round_trip_does_not_drift),
]


def main():
    failures = []
    ran = 0
    for name, fn in PURE_CHECKS:
        print(f"\n== {name}")
        before = len(failures)
        try:
            fn(failures.append)
        except Exception as exc:
            failures.append(f"{name}: raised {type(exc).__name__}: {exc}")
            import traceback
            traceback.print_exc()
        ran += 1
        print(f"   {'FAIL' if len(failures) > before else 'PASS'}")

    ran += run(CORPUS_CHECKS, failures)
    return report(failures, ran)


if __name__ == "__main__":
    sys.exit(main())
