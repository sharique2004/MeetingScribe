#!/usr/bin/env python3
"""Regression tests for voice_profiles — persistent cross-meeting speaker names.

Everything here runs on SYNTHETIC embeddings (fixed seed, no audio, no model,
no real meeting named), so the suite works on a clean clone. The contract under
test, in the order the guarantees matter:

  1. Recognition NEVER overwrites a name a human typed — defaults only.
  2. A profile is a VOICE, not a name. Two different humans called "Jess" stay
     two profiles; the same human across two meetings stays one.
  3. No guessing: a cluster below the distance threshold, below the speech
     minimum, or ambiguously close to two DIFFERENTLY-named profiles gets NO
     name.
  4. Enrollment is idempotent per (meeting, speaker-key) and a re-rename MOVES
     the sample to the new profile (the correction path) rather than leaving a
     poisoned copy behind.
  5. Formulaic names ("Speaker 2", "you") are never enrollable.
  6. Durations shown to a human are wall-clock speech, not the module's
     internal window time — diarization's windows overlap 2:1.

Run:  ~/.meetingscribe/venv/bin/python tools/test_voice_profiles.py
"""
import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402
import voice_profiles as vp  # noqa: E402

RNG = np.random.default_rng(7)
DIM = 192


def _voice():
    v = RNG.normal(size=DIM)
    return v / np.linalg.norm(v)


def _voice_at(base, distance):
    """A voice sitting exactly `distance` (cosine) from `base` — for probing
    either side of a threshold without depending on how far apart two random
    192-dim vectors happen to land."""
    orth = _voice()
    orth = orth - np.dot(orth, base) * base
    orth /= np.linalg.norm(orth)
    cos = 1.0 - distance
    return cos * base + np.sqrt(max(1.0 - cos * cos, 0.0)) * orth


def _sample(voice, n=40, noise=0.05):
    """(centroid, window seconds, speech seconds) for one meeting's worth of a
    voice — what enroll_from_meeting hands enroll_sample."""
    wins, embs = _windows_for(voice, n, noise=noise)
    return vp.centroid_for_spans(wins, embs, [(0, n + 5)])


def _windows_for(voice, n, noise=0.05, t0=0.0):
    """n 2-second windows of `voice` with a little per-window noise, plus the
    matching (start, end) coords starting at t0."""
    embs, wins = [], []
    for i in range(n):
        e = voice + RNG.normal(scale=noise, size=DIM)
        embs.append(e / np.linalg.norm(e))
        start = t0 + i * 1.0
        wins.append((start, start + 2.0))
    return np.array(wins), np.array(embs)


def _fresh_store():
    vp.PROFILES_PATH = Path(tempfile.mkdtemp()) / "voice_profiles.json"


# The shipped default, not a literal: the suite should be testing what users
# actually run, and recognition_threshold_is_the_measured_one pins the value.
CFG = {"voice_profiles": True,
       "voice_profile_threshold": config.DEFAULTS["voice_profile_threshold"]}
CHECKS = []


def check(fn):
    CHECKS.append(fn)
    return fn


@check
def centroid_uses_only_the_spans():
    voice_a, voice_b = _voice(), _voice()
    wins_a, embs_a = _windows_for(voice_a, 30, t0=0)
    wins_b, embs_b = _windows_for(voice_b, 30, t0=100)
    wins = np.vstack([wins_a, wins_b])
    embs = np.vstack([embs_a, embs_b])
    c, secs, _speech = vp.centroid_for_spans(wins, embs, [(0, 40)])
    assert c is not None and secs > 0
    assert 1 - np.dot(c, voice_a) < 0.05, "centroid drifted off its speaker"
    assert 1 - np.dot(c, voice_b) > 0.5, "centroid absorbed the other speaker"


@check
def recognition_round_trip_and_negative():
    _fresh_store()
    voice = _voice()
    wins, embs = _windows_for(voice, 40)
    c, secs, _speech = vp.centroid_for_spans(wins, embs, [(0, 45)])
    assert vp.enroll_sample("Priya", "m1", "s1", c, secs)
    # Same voice, new "meeting": recognized.
    wins2, embs2 = _windows_for(voice, 30)
    state = {"system": {"windows": wins2, "embeddings": embs2}}
    segs = [{"speaker": "s1", "track": "system", "start": 0, "end": 32}]
    speakers = {"you": "You", "s1": "Speaker 1"}
    applied = vp.apply_recognition(state, segs, speakers, CFG)
    assert speakers["s1"] == "Priya" and applied == {"s1": "Priya"}
    # A different voice: NOT recognized.
    wins3, embs3 = _windows_for(_voice(), 30)
    state3 = {"system": {"windows": wins3, "embeddings": embs3}}
    speakers3 = {"you": "You", "s1": "Speaker 1"}
    assert not vp.apply_recognition(state3, segs, speakers3, CFG)
    assert speakers3["s1"] == "Speaker 1"


@check
def human_names_are_untouchable():
    _fresh_store()
    voice = _voice()
    wins, embs = _windows_for(voice, 40)
    c, secs, _speech = vp.centroid_for_spans(wins, embs, [(0, 45)])
    vp.enroll_sample("Priya", "m1", "s1", c, secs)
    state = {"system": {"windows": wins, "embeddings": embs}}
    segs = [{"speaker": "s1", "track": "system", "start": 0, "end": 45}]
    speakers = {"you": "You", "s1": "Katy"}  # user typed this
    assert not vp.apply_recognition(state, segs, speakers, CFG)
    assert speakers["s1"] == "Katy"
    # And no duplicate names: if "Priya" is already taken by another key,
    # recognition backs off rather than showing two Priyas.
    speakers2 = {"you": "You", "s1": "Speaker 1", "s2": "Priya"}
    assert not vp.apply_recognition(state, segs, speakers2, CFG)
    assert speakers2["s1"] == "Speaker 1"


@check
def ambiguous_matches_are_skipped():
    _fresh_store()
    voice = _voice()
    # Two profiles from nearly the same voice (twins, or one person enrolled
    # under two names): the cluster sits close to BOTH -> no assignment.
    wins, embs = _windows_for(voice, 40)
    c, secs, _speech = vp.centroid_for_spans(wins, embs, [(0, 45)])
    vp.enroll_sample("Twin A", "m1", "s1", c, secs)
    wins_b, embs_b = _windows_for(voice, 40, noise=0.06)
    cb, secs_b, _speech = vp.centroid_for_spans(wins_b, embs_b, [(0, 45)])
    vp.enroll_sample("Twin B", "m2", "s1", cb, secs_b)
    state = {"system": {"windows": wins, "embeddings": embs}}
    segs = [{"speaker": "s1", "track": "system", "start": 0, "end": 45}]
    speakers = {"s1": "Speaker 1"}
    assert not vp.apply_recognition(state, segs, speakers, CFG)
    assert speakers["s1"] == "Speaker 1"


@check
def short_clusters_are_not_trusted():
    _fresh_store()
    voice = _voice()
    wins, embs = _windows_for(voice, 40)
    c, secs, _speech = vp.centroid_for_spans(wins, embs, [(0, 45)])
    vp.enroll_sample("Priya", "m1", "s1", c, secs)
    # 4 windows ≈ 8s of window time < MATCH_MIN_WINDOW_S.
    wins2, embs2 = _windows_for(voice, 4)
    state = {"system": {"windows": wins2, "embeddings": embs2}}
    segs = [{"speaker": "s1", "track": "system", "start": 0, "end": 6}]
    speakers = {"s1": "Speaker 1"}
    assert not vp.apply_recognition(state, segs, speakers, CFG)
    # Enrollment refuses short clusters too.
    c2, secs2, _speech = vp.centroid_for_spans(wins2, embs2, [(0, 6)])
    assert not vp.enroll_sample("Rushed", "m2", "s1", c2, secs2)


@check
def correction_moves_the_sample():
    _fresh_store()
    voice = _voice()
    wins, embs = _windows_for(voice, 40)
    c, secs, _speech = vp.centroid_for_spans(wins, embs, [(0, 45)])
    assert vp.enroll_sample("Sarah", "m1", "s1", c, secs)
    # Same meeting, same cluster, new name: the sample MOVES.
    assert vp.enroll_sample("Bob", "m1", "s1", c, secs)
    profs = vp.load_profiles()
    assert [p["name"] for p in profs] == ["Bob"], profs
    # Re-enrolling the same key twice does not double-count.
    assert vp.enroll_sample("Bob", "m1", "s1", c, secs)
    assert vp.load_profiles()[0]["n_samples"] == 1


@check
def two_strangers_sharing_a_name_stay_apart():
    """THE regression. Two different humans both called "Jess", named in two
    different meetings, used to collapse into one profile: a blended centroid
    that is neither of them, a catchment wide enough to mislabel a third
    person standing between them, and one Forget that silently deletes two
    people's fingerprints while the notice speaks about one."""
    _fresh_store()
    jess_one, jess_two = _voice(), _voice()
    apart = 1 - float(np.dot(jess_one, jess_two))
    assert apart > vp.MERGE_MAX_DIST, f"test voices only {apart:.3f} apart"
    c1, s1, sp1 = _sample(jess_one)
    c2, s2, sp2 = _sample(jess_two)
    id1 = vp.enroll_sample("Jess", "m1", "s1", c1, s1, sp1)
    id2 = vp.enroll_sample("Jess", "m2", "s1", c2, s2, sp2)
    assert id1 and id2 and id1 != id2, (id1, id2)
    profs = vp.load_profiles()
    assert len(profs) == 2, [(p["id"], p["n_samples"]) for p in profs]
    assert [p["name"] for p in profs] == ["Jess", "Jess"]
    assert all(p["n_samples"] == 1 for p in profs)
    # Each profile still IS its own person, not an average of the two.
    by_id = {p["id"]: p for p in profs}
    assert 1 - np.dot(by_id[id1]["centroid"], jess_one) < 0.05
    assert 1 - np.dot(by_id[id2]["centroid"], jess_two) < 0.05
    # And neither profile is a catchment for the space BETWEEN the two. Merging
    # them produced a centroid that was closer to a bystander standing midway
    # (distance ~0) than to either real Jess (~0.29) — the merged profile had
    # stopped describing anyone and started describing the gap.
    mid = jess_one + jess_two
    mid /= np.linalg.norm(mid)
    blend = by_id[id1]["centroid"] + by_id[id2]["centroid"]
    blend /= np.linalg.norm(blend)
    assert 1 - np.dot(blend, mid) < 0.01 < 1 - np.dot(blend, jess_one)
    for person, pid in ((jess_one, id1), (jess_two, id2)):
        own = 1 - np.dot(by_id[pid]["centroid"], person)
        assert own < 0.05 < 1 - np.dot(by_id[pid]["centroid"], mid), pid
    # And Forget is per person: the other Jess survives intact.
    assert vp.delete_profile(id1)
    left = vp.load_profiles()
    assert [p["id"] for p in left] == [id2] and left[0]["n_samples"] == 1


@check
def one_voice_across_meetings_still_merges():
    _fresh_store()
    voice = _voice()
    c1, s1, sp1 = _sample(voice)
    c2, s2, sp2 = _sample(voice)            # same person, second meeting
    id1 = vp.enroll_sample("Priya", "m1", "s1", c1, s1, sp1)
    id2 = vp.enroll_sample("Priya", "m2", "s1", c2, s2, sp2)
    assert id1 == id2, (id1, id2)
    profs = vp.load_profiles()
    assert len(profs) == 1 and profs[0]["n_samples"] == 2, profs
    # The merge rule is a distance, not a coin flip: a voice inside
    # MERGE_MAX_DIST joins, one outside it does not.
    _fresh_store()
    base = _voice()
    near, far = _voice_at(base, 0.30), _voice_at(base, 0.60)
    cb, sb, spb = _sample(base)
    home = vp.enroll_sample("Sam", "m1", "s1", cb, sb, spb)
    cn, sn, spn = _sample(near, noise=0.02)
    assert vp.enroll_sample("Sam", "m2", "s1", cn, sn, spn) == home
    cf, sf, spf = _sample(far, noise=0.02)
    assert vp.enroll_sample("Sam", "m3", "s1", cf, sf, spf) != home
    assert len(vp.load_profiles()) == 2


@check
def enrollment_returns_the_profile_it_used():
    """The rename endpoint reports THIS id to the UI, so it has to be the
    profile the sample actually landed on — including when the sample moves."""
    _fresh_store()
    voice = _voice()
    c, secs, speech = _sample(voice)
    sarah = vp.enroll_sample("Sarah", "m1", "s1", c, secs, speech)
    assert sarah == vp.load_profiles()[0]["id"]
    assert vp.get_profile(sarah)["name"] == "Sarah"
    # Correction: the id follows the sample to Bob, and Sarah is gone.
    bob = vp.enroll_sample("Bob", "m1", "s1", c, secs, speech)
    assert bob != sarah and vp.get_profile(sarah) is None
    assert [p["id"] for p in vp.load_profiles()] == [bob]
    # Idempotent: renaming to the same thing lands on the same profile.
    assert vp.enroll_sample("Bob", "m1", "s1", c, secs, speech) == bob
    # A name the engine trims or refuses gives back no id at all.
    assert vp.enroll_sample("  Bob  ", "m1", "s1", c, secs, speech) == bob
    assert vp.enroll_sample("Speaker 4", "m1", "s1", c, secs, speech) is None


@check
def same_name_profiles_do_not_silence_recognition():
    """Splitting one person across two same-named profiles is the price of
    never merging strangers. It must not cost that person their name: two
    profiles that agree on the label are not an ambiguous answer."""
    _fresh_store()
    base = _voice()
    drifted = _voice_at(base, 0.60)         # too far to merge, same name
    c1, s1, sp1 = _sample(base)
    c2, s2, sp2 = _sample(drifted, noise=0.02)
    assert vp.enroll_sample("Ana", "m1", "s1", c1, s1, sp1)
    assert vp.enroll_sample("Ana", "m2", "s1", c2, s2, sp2)
    assert len(vp.load_profiles()) == 2
    wins, embs = _windows_for(base, 30)
    state = {"system": {"windows": wins, "embeddings": embs}}
    segs = [{"speaker": "s1", "track": "system", "start": 0, "end": 35}]
    speakers = {"s1": "Speaker 1"}
    assert vp.apply_recognition(state, segs, speakers, CFG) == {"s1": "Ana"}
    # Two DIFFERENT names that close together are still refused (the check
    # above narrows the ambiguity rule, it does not delete it).
    _fresh_store()
    assert vp.enroll_sample("Ana", "m1", "s1", c1, s1, sp1)
    cb, sb, spb = _sample(base, noise=0.06)
    assert vp.enroll_sample("Bea", "m2", "s1", cb, sb, spb)
    speakers = {"s1": "Speaker 1"}
    assert not vp.apply_recognition(state, segs, speakers, CFG)


@check
def displayed_duration_is_wall_clock_not_window_time():
    """diarization emits 2.0 s windows every 1.0 s, so summing window lengths
    counts every second of speech about twice. What a human is shown must be
    the union, not the sum."""
    _fresh_store()
    voice = _voice()
    wins, embs = _windows_for(voice, 40)                 # 0.0 .. 41.0
    c, window_s, speech_s = vp.centroid_for_spans(wins, embs, [(0, 45)])
    assert window_s == 80.0, window_s
    assert speech_s == 41.0, speech_s
    # Two disjoint runs stay disjoint: the union is not "half the sum".
    far_wins, far_embs = _windows_for(voice, 10, t0=200.0)   # 200.0 .. 211.0
    _, w2, s2 = vp.centroid_for_spans(
        np.vstack([wins, far_wins]), np.vstack([embs, far_embs]),
        [(0, 45), (199, 215)])
    assert (w2, s2) == (100.0, 52.0), (w2, s2)
    # It reaches the API as speech, and the inflated number is not offered.
    pid = vp.enroll_sample("Priya", "m1", "s1", c, window_s, speech_s)
    listed = vp.get_profile(pid)
    assert listed["speech_seconds"] == 41.0, listed
    assert "seconds" not in listed and "window_seconds" not in listed, listed
    # Old samples predate the field; they are scaled back, never shown raw.
    assert vp._sample_speech_seconds({"seconds": 80.0}) == 40.0
    # The gates keep reading window time, so enrollment did NOT change: 15.0
    # window-seconds still means ~8 s of real speech, exactly as before.
    assert vp.ENROLL_MIN_WINDOW_S == 15.0 and vp.MATCH_MIN_WINDOW_S == 10.0
    short_wins, short_embs = _windows_for(voice, 7)          # 14.0 window-s
    cs, ws, ss = vp.centroid_for_spans(short_wins, short_embs, [(0, 20)])
    assert ws == 14.0 and ss == 8.0, (ws, ss)
    assert vp.enroll_sample("Rushed", "m2", "s1", cs, ws, ss) is None


@check
def recognition_threshold_is_the_measured_one():
    """The recognition cutoff is measured, not picked (see RECOGNIZE_MAX_DIST).

    On this machine's corpus the empty band is [0.408, 0.505): every value in
    it recognises all 14 held-out true matches and refuses all 576 stranger
    comparisons. 0.45 sits inside it; the 0.4 it replaced sat 0.008 BELOW the
    band, which cost a real person their name on their second meeting: the
    hardest recognition there is, because a profile holding one sample is at
    its widest. This pins the value, both copies of it, and the two decisions
    that changed shape when it moved.
    """
    assert vp.RECOGNIZE_MAX_DIST == 0.45, vp.RECOGNIZE_MAX_DIST
    assert config.DEFAULTS["voice_profile_threshold"] == vp.RECOGNIZE_MAX_DIST, (
        "config.py's default and the measured constant have drifted apart")
    _fresh_store()
    base = _voice()
    assert vp.enroll_sample("Priya", "m1", "s1", *_sample(base))
    segs = [{"speaker": "s1", "track": "system", "start": 0, "end": 35}]

    def recognized(distance, cfg):
        """The name a cluster `distance` from Priya's profile comes back with."""
        wins, embs = _windows_for(_voice_at(base, distance), 30, noise=0.02)
        speakers = {"s1": "Speaker 1"}
        return vp.apply_recognition(
            {"system": {"windows": wins, "embeddings": embs}},
            segs, speakers, cfg).get("s1")

    # 0.42 is inside the band: the same person, back for a second meeting,
    # against a profile still holding one sample. This is the recognition the
    # old value was losing, and the ONLY behaviour the move changes.
    assert recognized(0.42, CFG) == "Priya"
    assert recognized(0.42, dict(CFG, voice_profile_threshold=0.4)) is None
    # 0.52 is past the far edge, where the corpus's nearest stranger (0.505)
    # sits. Still refused, and the margin is what stops a confident lie.
    assert recognized(0.52, CFG) is None
    # A config that predates the setting must fall back to the measured value,
    # not to a literal that can go stale in a second place.
    assert recognized(0.42, {"voice_profiles": True}) == "Priya"


@check
def default_names_never_enroll_and_toggle_respected():
    _fresh_store()
    voice = _voice()
    wins, embs = _windows_for(voice, 40)
    c, secs, _speech = vp.centroid_for_spans(wins, embs, [(0, 45)])
    for bad in ("Speaker 2", "speaker 10", "Remote 1", "You", "you", "  "):
        assert not vp.enroll_sample(bad, "m1", "s1", c, secs), bad
    assert vp.enroll_sample("Priya", "m1", "s1", c, secs)
    state = {"system": {"windows": wins, "embeddings": embs}}
    segs = [{"speaker": "s1", "track": "system", "start": 0, "end": 45}]
    speakers = {"s1": "Speaker 1"}
    off = dict(CFG, voice_profiles=False)
    assert not vp.apply_recognition(state, segs, speakers, off)
    assert speakers["s1"] == "Speaker 1"


@check
def stale_embed_version_is_ignored():
    _fresh_store()
    voice = _voice()
    wins, embs = _windows_for(voice, 40)
    c, secs, _speech = vp.centroid_for_spans(wins, embs, [(0, 45)])
    vp.enroll_sample("Priya", "m1", "s1", c, secs)
    # Simulate a future embedding change: bump the version the module sees.
    real = vp.diarization.EMBED_VERSION
    try:
        vp.diarization.EMBED_VERSION = real + 1
        assert vp.load_profiles() == [], "stale sample crossed EMBED_VERSION"
    finally:
        vp.diarization.EMBED_VERSION = real


@check
def deleting_a_meeting_forgets_what_it_taught():
    """Delete has to mean delete: a recording's fingerprints must not outlive
    the recording. A person who also appears in a meeting you keep must keep
    their profile, minus the deleted meeting's contribution."""
    _fresh_store()
    priya, marcus = _voice(), _voice()
    # Priya is in both meetings, Marcus only in the one we delete.
    vp.enroll_sample("Priya", "m1", "s1", *_sample(priya))
    vp.enroll_sample("Priya", "m2", "s1", *_sample(priya))
    marcus_id = vp.enroll_sample("Marcus", "m1", "s2", *_sample(marcus))
    assert marcus_id, "setup: Marcus should have enrolled"
    before = {p["name"]: p["n_samples"] for p in vp.list_profiles()}
    assert before == {"Priya": 2, "Marcus": 1}, before

    samples, profiles = vp.forget_meeting("m1")
    assert (samples, profiles) == (2, 1), (samples, profiles)
    after = {p["name"]: p["n_samples"] for p in vp.list_profiles()}
    assert after == {"Priya": 1}, f"Marcus should be gone, Priya thinned: {after}"
    assert vp.get_profile(marcus_id) is None, "deleted meeting left a profile behind"

    # Idempotent, and it does not touch meetings that were not deleted.
    assert vp.forget_meeting("m1") == (0, 0)
    assert {p["name"]: p["n_samples"] for p in vp.list_profiles()} == {"Priya": 1}
    # A meeting id that only PREFIXES a real one must not match.
    assert vp.forget_meeting("m") == (0, 0), "prefix matched the wrong meeting"


def main():
    failures = []
    for fn in CHECKS:
        try:
            fn()
            print(f"  ok  {fn.__name__}")
        except AssertionError as exc:
            failures.append(f"{fn.__name__}: {exc}")
            print(f"FAIL  {fn.__name__}: {exc}")
    print("=" * 60)
    if failures:
        print(f"{len(failures)} FAILURE(S)")
        return 1
    print(f"OK — {len(CHECKS)} checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
