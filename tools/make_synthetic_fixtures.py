#!/usr/bin/env python3
"""Generate the committable, privacy-free diarization fixtures in test/synthetic/.

WHY THIS EXISTS
---------------
Two separate promises depend on labelled speaker fixtures, and neither can be
kept by test/fixtures/:

  1. The four regression suites (test_diarization / test_modes / test_ask /
     test_atomic_writes) are the tests the owner is told to rely on, but they
     read test/fixtures/, which is GITIGNORED — it holds real participants'
     ECAPA voice embeddings and meeting titles carrying their names. On a clean
     clone that directory does not exist, so the suite cannot run at all. This
     generator produces a baseline corpus that ships IN the repo.

  2. docs/COUNT_ESTIMATION_DESIGN.md states its own precondition: the
     speaker-count rule cannot be trusted until there are synthesized labelled
     fixtures with KNOWN turn boundaries, deliberately including rapid
     alternation and short backchannels from a genuine second speaker. The only
     positive control in the corpus today is one synthetic demo of long scripted
     monologues — the easiest possible case for a coherence metric.

WHAT IS SYNTHESIZED, AND WHAT IS NOT
------------------------------------
NO AUDIO IS USED OR PRODUCED, and no real recording is read. The fixtures are
written in exactly the cached shape the pipeline consumes when it re-clusters a
meeting without re-transcribing it:

    <slug>/meeting.json    the descriptor (mode, tracks, duration, title)
    <slug>/analysis.json   {"transcripts": {"mic": [...], "system": [...]}}
    <slug>/analysis.npz    {track}_windows (N,2) float64
                           {track}_embeddings (N,192) float32
                           embed_version int32
    <slug>/labels.json     GROUND TRUTH — not read by the pipeline, only by the
                           tests: per-window true voice id, true speaker count,
                           and the generation parameters for this scenario.

The window COORDINATES are produced by the real diarization.build_windows() over
the synthesized segments, so the windows are laid out exactly as a real run would
lay them out. Only the embedding VECTORS are synthesized.

HOW THE EMBEDDINGS ARE SYNTHESIZED (reproducible, and reviewable by hand)
------------------------------------------------------------------------
Everything is driven by one seed and a random orthonormal basis Q of R^192
(192 = the ECAPA-TDNN embedding width, so the arrays are shaped like the real
ones). Writing q0, q1, ... for the columns of Q:

    common       = q0
    anchor_i     = q1+i                      one per voice, i = 0..MAX_VOICES-1
    phantom dir  = q(1+MAX_VOICES)
    drift dir    = q(2+MAX_VOICES)
    noise space  = the remaining 183 columns

A voice's centroid is

    c_i = sqrt(1 - BETWEEN_D) * common + sqrt(BETWEEN_D) * anchor_i

which is a unit vector with  c_i . c_j = 1 - BETWEEN_D  for i != j, i.e. the
cosine DISTANCE between any two distinct voices is exactly BETWEEN_D. That is
the single knob for between-speaker separation.

A window of voice i is

    v = normalise(c + WITHIN_SIGMA * n),   n a unit vector drawn from the noise
                                           space (orthogonal to every centroid)

so two windows of the same voice sit at cosine distance ~= s^2 / (1 + s^2) with
s = WITHIN_SIGMA — the single knob for within-speaker spread. At the shipped
values that is 0.85 between speakers and ~0.109 within one, against
config's 0.6 clustering threshold.

Two scenarios bend a centroid instead of replacing it, and both are the SAME
PERSON in the ground truth:

  drift    c(u) = rotate(c_i, drift_dir, DRIFT_DEG * u), u = window position in
           [0,1]. A voice that slowly changes over the meeting (moving away from
           the mic, a codec settling). Must not be counted as two people.

  phantom  a scattered minority of windows (PHANTOM_SHARE of them, picked
           independently so they land in runs of one) is rotated by
           arccos(1 - PHANTOM_D) away from the voice centroid. This is the
           real-corpus failure mode reproduced deliberately: PHANTOM_D is set
           ABOVE the 0.6 clustering threshold so the raw clustering splits the
           voice in two, and ABOVE 0.9*0.6 = 0.54 so _fold_weak_clusters does
           NOT merge the halves back, and the minority carries more than
           MIN_CLUSTER_S = 10 s so the duration fold does not remove it either.
           One person; two surviving clusters. Nothing may call it two people.

REPRODUCIBILITY
---------------
Every array is a pure function of (SEED, the constants below, the scenario
scripts). Re-running this script rewrites byte-identical files. The parameters
are copied into test/synthetic/index.json and into each labels.json so a
reviewer can check a fixture without re-reading this source.

    ~/.meetingscribe/venv/bin/python tools/make_synthetic_fixtures.py
    ~/.meetingscribe/venv/bin/python tools/make_synthetic_fixtures.py --check

--check regenerates into a temp directory and diffs, so CI can prove the
committed fixtures still match the generator.

PRIVACY
-------
There is no personal data of any kind here by construction: no real audio is
read, the vectors come from numpy's PCG64 given a fixed seed, and the scripts
contain no personal names, company names, or real meeting titles. The generator
refuses to write a fixture whose text trips the name screen in _assert_clean().
"""

import argparse
import filecmp
import io
import json
import math
import re
import shutil
import sys
import tempfile
import zipfile
import zlib
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import diarization  # noqa: E402

OUT = REPO / "test" / "synthetic"

# --------------------------------------------------------------- parameters --
SEED = 20260725
DIM = 192                # ECAPA-TDNN embedding width
MAX_VOICES = 8           # anchors reserved in the basis
BETWEEN_D = 0.85         # cosine distance between two distinct voices
WITHIN_SIGMA = 0.35      # noise scale -> within-voice pair distance ~0.109
DRIFT_DEG = 80.0         # total rotation across a drifting voice's meeting
PHANTOM_D = 0.65         # cosine distance of the phantom lobe from its own voice
# Fraction of a voice's windows that land in the lobe. 0.12 puts the resulting
# duration ratio at ~0.14, inside the 0.004-0.16 band the real corpus phantoms
# actually occupy (see the tables in pipeline.py and COUNT_ESTIMATION_DESIGN.md).
# It is calibrated to the real failure, NOT to whatever gate is in force.
PHANTOM_SHARE = 0.12
GAP_S = 0.18             # silence between consecutive turns

PARAMS = {
    "seed": SEED,
    "embedding_dim": DIM,
    "max_voices": MAX_VOICES,
    "between_speaker_cosine_distance": BETWEEN_D,
    "within_speaker_sigma": WITHIN_SIGMA,
    "within_speaker_pair_distance_expected": round(
        WITHIN_SIGMA ** 2 / (1 + WITHIN_SIGMA ** 2), 6),
    "drift_degrees": DRIFT_DEG,
    "phantom_cosine_distance": PHANTOM_D,
    "phantom_window_share": PHANTOM_SHARE,
    "turn_gap_seconds": GAP_S,
    "windowing": {
        "source": "diarization.build_windows (the real one)",
        "window_s": diarization.WINDOW_S,
        "hop_s": diarization.HOP_S,
        "min_window_s": diarization.MIN_WINDOW_S,
    },
}


# ------------------------------------------------------------- voice vectors --

class VoiceSpace:
    """The orthonormal scaffolding described in the module docstring."""

    def __init__(self, seed=SEED, dim=DIM, n_voices=MAX_VOICES):
        rng = np.random.default_rng(seed)
        basis, _ = np.linalg.qr(rng.standard_normal((dim, dim)))
        self.common = basis[:, 0]
        self.anchors = [basis[:, 1 + i] for i in range(n_voices)]
        self.phantom_dir = basis[:, 1 + n_voices]
        self.drift_dir = basis[:, 2 + n_voices]
        self.noise = basis[:, 3 + n_voices:]

    def centroid(self, voice_index):
        vec = (math.sqrt(1.0 - BETWEEN_D) * self.common
               + math.sqrt(BETWEEN_D) * self.anchors[voice_index])
        return vec / np.linalg.norm(vec)

    def rotate(self, vec, direction, degrees):
        """Rotate `vec` toward `direction` inside their plane (both unit)."""
        perp = direction - float(np.dot(direction, vec)) * vec
        norm = np.linalg.norm(perp)
        if norm == 0:
            return vec
        perp = perp / norm
        rad = math.radians(degrees)
        out = math.cos(rad) * vec + math.sin(rad) * perp
        return out / np.linalg.norm(out)

    def sample(self, centroid, rng):
        noise = self.noise @ rng.standard_normal(self.noise.shape[1])
        noise = noise / np.linalg.norm(noise)
        vec = centroid + WITHIN_SIGMA * noise
        return vec / np.linalg.norm(vec)


# ------------------------------------------------------------------ scripts --
# Deliberately anodyne: a fictional team talking about a fictional inventory
# sync. No personal names, no company names, no real meeting titles.

LONG = [
    "Let us start with where the inventory sync stands at the moment, because "
    "the numbers on the dashboard have not matched the warehouse count for "
    "about a week now and nobody has been able to say why.",
    "My reading is that the nightly job is finishing before the last batch of "
    "updates lands, so the snapshot it takes is always one cycle behind what "
    "the warehouse actually holds at that point in the evening.",
    "If that is right then the fix is not in the reconciliation logic at all, "
    "it is in the scheduling, and we should move the job later rather than "
    "keep patching the comparison step every time it drifts.",
    "I would rather we measure it before we move anything, because the last "
    "two times we changed the schedule the failure simply moved somewhere else "
    "and we spent another fortnight finding it again.",
    "That seems fair. Let us instrument the job first, collect a week of "
    "timings, and then decide whether the schedule or the batching is the "
    "thing that actually needs to change.",
    "One more thing worth raising is that the alert only fires on a full "
    "mismatch, so a partial drift of a handful of items sits there quietly "
    "until somebody happens to look at the dashboard.",
    "We could lower that threshold, though I suspect we would then get an "
    "alert every single night, and an alert that fires every night is the same "
    "as no alert at all as far as anyone reading it is concerned.",
    "Then let us make it a trend rather than a threshold, and only page "
    "somebody when the drift grows three days running instead of the moment it "
    "crosses a fixed line.",
]

MEDIUM = [
    "The nightly job is running late again.",
    "That would explain the mismatch we saw.",
    "I can pull the timings this afternoon.",
    "How far behind is it, roughly speaking?",
    "About one cycle, sometimes two on a busy day.",
    "Can we just move the schedule back an hour?",
    "We tried that in the spring and it moved the problem.",
    "Then instrument it first and decide afterwards.",
    "Agreed, let us not guess at it twice.",
    "I will write the timings into the shared doc.",
    "Do we need to tell the warehouse team?",
    "Only once we know what we are telling them.",
    "Understood, I will hold off until the numbers land.",
    "What about the alert threshold in the meantime?",
    "Leave it, a noisy alert helps nobody.",
    "Let us look again on Thursday with real data.",
]

BACKCHANNEL = [
    "Mm-hm.", "Right.", "Yeah.", "Okay.", "Sure.", "Got it.", "Mm.", "Exactly.",
]


def _words(text, start, end):
    """Per-word timings shaped like the real transcripts.

    Not decoration: diarize_track uses word timings to split a Whisper segment
    when the voice changes mid-segment, and a fixture without them can only ever
    be labelled a whole segment at a time by its midpoint. Time is shared out in
    proportion to token length, which is close enough to speech rate for the
    boundaries to land where they would in a real transcript.
    """
    tokens = text.split()
    if not tokens:
        return []
    weights = [len(tok) + 1 for tok in tokens]
    total = float(sum(weights))
    out = []
    t = float(start)
    span = float(end) - float(start)
    for i, (tok, weight) in enumerate(zip(tokens, weights)):
        nxt = t + span * weight / total
        out.append({
            "w": tok if i == 0 else " " + tok,
            "s": round(t, 3),
            "e": round(min(nxt, float(end)), 3),
        })
        t = nxt
    return out


class Script:
    """Lays synthesized turns out on a timeline, one track at a time."""

    def __init__(self):
        self.segments = {}
        self.clock = {}

    def say(self, voice, seconds, text, track="mic", gap=GAP_S, at=None):
        start = self.clock.get(track, 0.0) if at is None else float(at)
        end = round(start + seconds, 3)
        self.segments.setdefault(track, []).append({
            "start": round(start, 3),
            "end": end,
            "text": text,
            "words": _words(text, start, end),
            "voice": voice,          # ground truth; stripped before writing
        })
        self.clock[track] = round(end + gap, 3)
        return self

    def tracks(self):
        return self.segments


def script_solo():
    s = Script()
    for i in range(6):
        s.say("A", 11.0, LONG[i])
    return s


def script_two_long():
    s = Script()
    for i in range(8):
        s.say("AB"[i % 2], 11.0, LONG[i])
    return s


def script_two_alternating(turn_seconds, turns):
    """Two genuine speakers swapping every `turn_seconds`.

    Turn LENGTH is the variable of interest. build_windows lays 2 s windows on a
    1 s hop, so a turn of L seconds yields about L-1 windows and a strictly
    alternating conversation has fragmentation ~= 1/(L-1) — a function of how
    fast people are taking turns, not of who is speaking. The corpus carries
    three of these so the 0.30 fragmentation gate is bracketed by fixtures whose
    speaker count is KNOWN.
    """
    s = Script()
    for i in range(turns):
        s.say("AB"[i % 2], turn_seconds, MEDIUM[i % len(MEDIUM)])
    return s


def script_two_backchannel():
    """A genuine second speaker who only ever interjects "mm-hm" and "right"."""
    s = Script()
    for i in range(8):
        s.say("A", 11.0, LONG[i])
        s.say("B", 1.6, BACKCHANNEL[i % len(BACKCHANNEL)])
    return s


def script_three():
    s = Script()
    for i in range(12):
        s.say("ABC"[i % 3], 7.0, MEDIUM[i % len(MEDIUM)])
    return s


def script_four():
    s = Script()
    for i in range(16):
        s.say("ABCD"[i % 4], 6.0, MEDIUM[i % len(MEDIUM)])
    return s


def script_drift():
    s = Script()
    for i in range(6):
        s.say("A", 11.0, LONG[i])
    return s


def script_phantom(turns=8):
    s = Script()
    for i in range(turns):
        s.say("A", 11.0, LONG[i % len(LONG)])
    return s


def script_online(mic_seconds, system_seconds, mic_turns=6, system_turns=6):
    """An ordinary online call: the local user on the mic, the far end on the
    system track. Used only for the "is the call-audio track really lost?"
    checks, which read transcripts and never touch embeddings."""
    s = Script()
    for i in range(mic_turns):
        s.say("A", mic_seconds / mic_turns, MEDIUM[i % len(MEDIUM)], track="mic")
    for i in range(system_turns):
        s.say("B", system_seconds / system_turns,
              MEDIUM[(i + 3) % len(MEDIUM)], track="system")
    return s


def script_ask_demo():
    """A processed 2+1 call whose turn STARTS deliberately collide.

    The timings mirror the frozen demo meeting the citation tests were written
    against: two turns starting at exactly 0.00, and two more pairs 0.30 s
    apart — all far inside ask.CITATION_TOLERANCE_S — so a citation timestamp
    genuinely cannot choose between them and only the quote can.
    """
    s = Script()
    rows = [
        # voice, track, start, end, text
        ("A", "mic",     0.00,  1.02, "Hello there, everyone."),
        ("B", "system",  0.00,  1.26, "Morning to you all."),
        ("A", "mic",     1.02,  3.96, "I am dialling in from the other office today."),
        ("B", "system",  1.32,  4.20, "Thanks for joining the quarterly planning review."),
        ("A", "mic",     3.96,  9.90, "I agree with the pilot idea, and I can set up the "
                                      "tracking dashboard before the next cycle begins."),
        ("B", "system",  4.26, 13.08, "I wanted to start with the budget review because "
                                      "there are a few line items that moved this quarter."),
        ("C", "system", 13.14, 29.34, "I actually have the numbers right here. Revenue grew "
                                      "12% compared to last quarter, and customer "
                                      "acquisition cost went up as well, which is the part "
                                      "worth talking through before we decide anything."),
        ("B", "system", 29.52, 41.82, "That is a great question. I think we should run a "
                                      "small pilot for two weeks and see whether the trend "
                                      "holds before committing the whole budget to it."),
        ("C", "system", 41.88, 48.24, "I will draft the proposal and share it with the group "
                                      "by Thursday evening so everyone has time to read it."),
    ]
    for voice, track, start, end, text in rows:
        s.say(voice, end - start, text, track=track, at=start)
    return s


# ---------------------------------------------------------------- scenarios --
# truth = the number of real PEOPLE on the clustered track. `note` is written
# into labels.json so a reviewer sees what the fixture is for without reading
# this file.

SCENARIOS = [
    dict(slug="synthetic-solo-one-voice-20200101-000001",
         title="Synthetic — one voice, long turns",
         mode="inperson", script=script_solo, embed="clean", truth=1,
         note="The trivial positive control: one speaker, nothing to separate."),

    dict(slug="synthetic-two-long-turns-20200101-000002",
         title="Synthetic — two voices, long turns",
         mode="inperson", script=script_two_long, embed="clean", truth=2,
         note="Two genuine speakers taking long turns — the easiest case for a "
              "coherence metric, and the shape the only pre-existing positive "
              "control had."),

    dict(slug="synthetic-two-medium-turns-20200101-000009",
         title="Synthetic — two voices, five-second turns",
         mode="inperson",
         script=lambda: script_two_alternating(5.0, 16), embed="clean", truth=2,
         note="Two genuine speakers at 5 s a turn — the middle of the bracket "
              "around the 0.30 fragmentation gate."),

    dict(slug="synthetic-two-rapid-alternation-20200101-000003",
         title="Synthetic — two voices, rapid alternation",
         mode="inperson",
         script=lambda: script_two_alternating(3.2, 24), embed="clean", truth=2,
         note="Two genuine speakers swapping every ~3 s. Named by "
              "docs/COUNT_ESTIMATION_DESIGN.md as the case the fragmentation "
              "rule was never tested against: a real speaker here produces many "
              "short runs, which is what the rule reads as a phantom. This "
              "fixture is the counter-example — its speaker count is KNOWN to "
              "be 2."),

    dict(slug="synthetic-two-backchannel-20200101-000004",
         title="Synthetic — two voices, one only backchannels",
         mode="inperson", script=script_two_backchannel, embed="clean", truth=2,
         note="A genuine second speaker who only ever says 'mm-hm' and 'right'. "
              "Low duration ratio, and the interjections are spread through the "
              "whole meeting — the quiet-but-real participant the duration gate "
              "is supposed to protect."),

    dict(slug="synthetic-three-voices-20200101-000005",
         title="Synthetic — three voices",
         mode="inperson", script=script_three, embed="clean", truth=3,
         note="Three genuine speakers, medium turns."),

    dict(slug="synthetic-four-voices-20200101-000006",
         title="Synthetic — four voices",
         mode="inperson", script=script_four, embed="clean", truth=4,
         note="Four genuine speakers — the counter-direction control: whatever "
              "folds phantoms must leave these four apart."),

    dict(slug="synthetic-same-voice-drift-20200101-000007",
         title="Synthetic — one voice that drifts",
         mode="inperson", script=script_drift, embed="drift", truth=1,
         note=f"ONE person whose embeddings rotate {DRIFT_DEG:.0f} degrees "
              "across the meeting. The ends are further apart than the "
              "clustering threshold, but the halves' centroids are close enough "
              "that _fold_weak_clusters merges them. Must not be two people."),

    dict(slug="synthetic-same-voice-split-20200101-000008",
         title="Synthetic — one voice with a scattered second lobe",
         mode="inperson", script=script_phantom, embed="phantom", truth=1,
         note=f"ONE person, {PHANTOM_SHARE:.0%} of whose windows sit "
              f"{PHANTOM_D} away from their own centroid, scattered so they "
              "land in runs of one. Reproduces the real-corpus phantom: the "
              "split survives BOTH folds, so the auto path returns 2 clusters "
              "for 1 person. Anything that counts clusters gets this wrong."),

    dict(slug="synthetic-same-voice-big-split-20200101-000015",
         title="Synthetic — one voice with a large scattered second lobe",
         mode="inperson", script=script_phantom, embed="phantom",
         truth=1, phantom_share=0.30,
         note="The same one person, but with a lobe big enough that the "
              "runner-up carries ~40% of the dominant cluster's speech. Still "
              "one person. Included because it is the case a duration test "
              "alone cannot reject — only the scatter distinguishes it."),

    dict(slug="synthetic-call-clean-audio-20200101-000010",
         title="Synthetic — online call, call audio captured",
         mode="online", script=lambda: script_online(72.0, 70.0), embed="clean",
         truth=2, embed_tracks=(),
         note="System/mic speech ratio ~0.97. The fallback must never fire."),

    dict(slug="synthetic-call-half-volume-20200101-000011",
         title="Synthetic — online call, far end talks half as much",
         mode="online", script=lambda: script_online(72.0, 38.0), embed="clean",
         truth=2, embed_tracks=(),
         note="System/mic ratio ~0.53."),

    dict(slug="synthetic-call-third-volume-20200101-000012",
         title="Synthetic — online call, far end talks a third as much",
         mode="online", script=lambda: script_online(72.0, 22.0), embed="clean",
         truth=2, embed_tracks=(),
         note="System/mic ratio ~0.31."),

    dict(slug="synthetic-call-brief-far-end-20200101-000013",
         title="Synthetic — online call, far end barely speaks",
         mode="online", script=lambda: script_online(72.0, 8.0), embed="clean",
         truth=2, embed_tracks=(),
         note="System/mic ratio ~0.11 — well under any plausible ratio gate, "
              "and still a call whose audio was captured perfectly."),

    dict(slug="synthetic-call-quiet-far-end-20200101-000014",
         title="Synthetic — online call, quietest far end in the corpus",
         mode="online",
         script=lambda: script_online(96.0, 3.0, mic_turns=12, system_turns=3),
         embed="clean", truth=2, embed_tracks=(),
         note="System/mic ratio ~0.03, and its shortest single segment is ~1 s "
              "against 96 s of mic. This is the fixture the absolute-vs-ratio "
              "check is aimed at: a ratio gate fires here, an absolute one "
              "cannot."),

    dict(slug="synthetic-ask-demo-call-20200101-000020",
         title="Synthetic — processed call with colliding turn starts",
         mode="online", script=script_ask_demo, embed="clean", truth=3,
         processed=True,
         note="Already-labelled meeting used by the citation tests: two turns "
              "start at exactly 0.00 and two more pairs 0.30 s apart, so a "
              "cited timestamp cannot choose between them."),
]


# ------------------------------------------------------------------- writing --

def _voice_of(segments, t):
    for seg in segments:
        if seg["start"] <= t <= seg["end"]:
            return seg["voice"]
    return min(segments, key=lambda s: min(abs(s["start"] - t),
                                           abs(s["end"] - t)))["voice"]


def _build_track(space, scenario, segments, rng):
    """(windows, embeddings, per-window true voice) for one track."""
    windows = diarization.build_windows(segments)
    voices = [_voice_of(segments, (w[0] + w[1]) / 2.0) for w in windows]
    order = sorted(set(voices))
    index = {v: i for i, v in enumerate(order)}

    mode = scenario["embed"]
    n = len(windows)
    share = scenario.get("phantom_share", PHANTOM_SHARE)
    # Deterministic, independent choice of which windows land in the phantom
    # lobe. Independence is the point: it puts them in runs of one, which is
    # what a same-voice split looks like in the real corpus.
    lobe = rng.random(n) < share if mode == "phantom" else np.zeros(n, bool)

    vectors = np.empty((n, DIM), dtype=np.float32)
    for i, voice in enumerate(voices):
        centre = space.centroid(index[voice])
        if mode == "drift":
            u = i / max(n - 1, 1)
            centre = space.rotate(centre, space.drift_dir, DRIFT_DEG * u)
        elif mode == "phantom" and lobe[i]:
            centre = space.rotate(centre, space.phantom_dir,
                                  math.degrees(math.acos(1.0 - PHANTOM_D)))
        vectors[i] = space.sample(centre, rng).astype(np.float32)

    lobes = [bool(x) for x in lobe]
    return (np.asarray(windows, dtype=np.float64), vectors, voices, lobes)


_BANNED = None


def _assert_clean(text):
    """Refuse to write anything that looks like it names a real person.

    Cheap and deliberately over-broad: the corpus this replaces exists precisely
    because meeting titles carried participants' names, and a generator that can
    leak one is worse than no generator. Scripts here are written name-free, so
    any hit is a bug in this file.
    """
    global _BANNED
    if _BANNED is None:
        _BANNED = set()
        local = REPO / "test" / "fixtures"
        # Only ever used to REJECT: tokens from the private corpus must not
        # appear in anything committed. Nothing from it is ever written out, and
        # when the private corpus is absent (a clean clone) this is a no-op —
        # the scripts are written name-free in the first place.
        sources = []
        index = local / "index.json"
        if index.exists():
            sources += list(json.loads(index.read_text(encoding="utf-8")).values())
        for meta_path in sorted(local.glob("*/meeting.json")):
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            sources.append(str(meta.get("title") or ""))
            sources += [str(v) for v in (meta.get("speakers") or {}).values()]
        for text in sources:
            for token in re.split(r"[^A-Za-z']+", text):
                if len(token) > 3:
                    _BANNED.add(token.lower())
        # Words that are ordinary English and only appear in the private titles
        # by coincidence ("Palace of Fine Arts", "Interview Practice", ...).
        # Everything else stays banned, including every given name.
        _BANNED -= {"call", "meeting", "with", "interview", "practice", "test",
                    "minutes", "zoom", "role", "prep", "sync", "group", "demo",
                    "starts", "daily", "rise", "voices", "verification",
                    "candidate", "screen", "software", "engineering", "virtual",
                    "synthesized", "hackathon", "builders", "recruiter",
                    "speaker", "july", "june", "arts", "palace", "summit",
                    "intelligence", "agentic"}
    words = {w.strip("(),.'\"").lower() for w in str(text).split()}
    hit = words & _BANNED
    if hit:
        raise SystemExit(
            f"REFUSING to write synthetic text containing {sorted(hit)} — those "
            f"tokens come from the private corpus index. Rewrite the script in "
            f"{__file__}."
        )


def write_scenario(scenario, out_root, space):
    slug = scenario["slug"]
    _assert_clean(scenario["title"])
    d = out_root / slug
    d.mkdir(parents=True, exist_ok=True)

    script = scenario["script"]()
    tracks = script.tracks()
    for segs in tracks.values():
        for seg in segs:
            _assert_clean(seg["text"])

    # zlib.crc32, not hash(): Python randomises string hashing per process, so
    # hash() here would make the fixtures differ between runs.
    rng = np.random.default_rng(SEED + zlib.crc32(slug.encode("utf-8")))
    embed_tracks = scenario.get("embed_tracks")
    if embed_tracks is None:
        embed_tracks = tuple(tracks)

    arrays = {}
    truth = {}
    for track, segs in tracks.items():
        if track not in embed_tracks or not segs:
            continue
        windows, vectors, voices, lobes = _build_track(space, scenario, segs, rng)
        arrays[f"{track}_windows"] = windows
        arrays[f"{track}_embeddings"] = vectors
        truth[track] = {
            "window_voices": voices,
            "phantom_lobe": lobes if scenario["embed"] == "phantom" else None,
            "windows": len(windows),
            "true_voices": sorted(set(voices)),
        }
    arrays["embed_version"] = np.asarray(diarization.EMBED_VERSION, dtype=np.int32)

    # analysis.npz — written with a deterministic mtime so regeneration is
    # byte-stable (np.savez_compressed stamps the zip entries otherwise).
    buf = {}
    for name, arr in arrays.items():
        bio = io.BytesIO()
        np.lib.format.write_array(bio, np.asarray(arr), allow_pickle=False)
        buf[name] = bio.getvalue()
    with zipfile.ZipFile(d / "analysis.npz", "w", zipfile.ZIP_DEFLATED) as zf:
        for name in sorted(buf):
            info = zipfile.ZipInfo(f"{name}.npy", date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, buf[name])

    transcripts = {
        track: [{k: v for k, v in seg.items() if k != "voice"} for seg in segs]
        for track, segs in tracks.items()
    }
    transcripts.setdefault("mic", [])
    transcripts.setdefault("system", [])
    _write_json(d / "analysis.json", {"transcripts": transcripts})

    duration = max(
        (seg["end"] for segs in tracks.values() for seg in segs), default=0.0)
    meta = {
        "id": slug.rsplit("-", 2)[-2] + "-" + slug.rsplit("-", 1)[-1],
        "title": scenario["title"],
        "created": "2020-01-01T00:00:00",
        "mode": scenario["mode"],
        "expected_speakers": None,
        "status": "done",
        "tracks": {
            "mic": {"file": "mic.wav", "device": "synthetic", "rate": 16000,
                    "seconds": round(duration, 2), "start_offset": 0.0},
            "system": {"file": "system.wav", "device": "synthetic", "rate": 16000,
                       "seconds": round(duration, 2), "start_offset": 0.0},
        },
        "warnings": [],
        "duration": round(duration, 2),
        "speakers": {},
        "turns": [],
        "stats": {"per_speaker": {}, "total_spoken_seconds": 0.0,
                  "duration": round(duration, 2), "total_words": 0},
        "languages": {"mic": "en_US", "system": "en_US"},
        "processing": {"model": "synthetic", "backend": "synthetic",
                       "seconds": 0.0, "mode": scenario["mode"]},
        "synthetic": True,
    }
    if scenario.get("processed"):
        meta.update(_processed_view(tracks))
    _write_json(d / "meeting.json", meta)

    _write_json(d / "labels.json", {
        "slug": slug,
        "title": scenario["title"],
        "note": scenario["note"],
        "embedding_mode": scenario["embed"],
        "true_speaker_count": scenario["truth"],
        "tracks": truth,
        "generated_by": "tools/make_synthetic_fixtures.py",
        "parameters": dict(PARAMS, phantom_window_share=scenario.get(
            "phantom_share", PHANTOM_SHARE)),
    })
    return d, truth


def _processed_view(tracks):
    """Fill in speakers/turns for a fixture that is meant to look already
    labelled (the citation tests read meeting.json, not the transcripts)."""
    names = {}
    turns = []
    for track, segs in tracks.items():
        for seg in segs:
            voice = seg["voice"]
            if track == "mic":
                key = "you"
                names[key] = "You"
            else:
                key = f"s{sorted({s['voice'] for s in tracks['system']}).index(voice) + 1}"
                names[key] = f"Speaker {key[1:]}"
            turns.append({"speaker": key, "track": track,
                          "start": seg["start"], "end": seg["end"],
                          "text": seg["text"]})
    turns.sort(key=lambda t: (t["start"], t["end"]))
    ordered = {}
    for key in ["you"] + sorted(k for k in names if k != "you"):
        if key in names:
            ordered[key] = names[key]
    return {"speakers": ordered, "turns": turns}


def _write_json(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1) + "\n",
                    encoding="utf-8")


def generate(out_root):
    out_root.mkdir(parents=True, exist_ok=True)
    space = VoiceSpace()
    index = {}
    for scenario in SCENARIOS:
        _, truth = write_scenario(scenario, out_root, space)
        index[scenario["slug"]] = {
            "title": scenario["title"],
            "mode": scenario["mode"],
            "embedding_mode": scenario["embed"],
            "true_speaker_count": scenario["truth"],
            "windows": {t: v["windows"] for t, v in truth.items()},
            "note": scenario["note"],
        }
    _write_json(out_root / "index.json", {
        "generated_by": "tools/make_synthetic_fixtures.py",
        "embed_version": int(diarization.EMBED_VERSION),
        "parameters": PARAMS,
        "fixtures": index,
    })
    (out_root / "README.md").write_text(_readme(index), encoding="utf-8")
    return index


def _readme(index):
    rows = "\n".join(
        f"| `{slug}` | {info['mode']} | {info['true_speaker_count']} | "
        f"{info['embedding_mode']} | "
        f"{', '.join(f'{t}:{n}' for t, n in sorted(info['windows'].items())) or '—'} | "
        f"{info['note']} |"
        for slug, info in index.items()
    )
    params = "\n".join(f"| `{k}` | `{v}` |" for k, v in PARAMS.items()
                       if not isinstance(v, dict))
    return f"""<!-- GENERATED FILE — edit tools/make_synthetic_fixtures.py instead. -->
# test/synthetic — committed, privacy-free diarization fixtures

Regenerate (byte-identical, fixed seed):

```
~/.meetingscribe/venv/bin/python tools/make_synthetic_fixtures.py
~/.meetingscribe/venv/bin/python tools/make_synthetic_fixtures.py --check
```

## Why these exist

`test/fixtures/` is gitignored — it holds real participants' ECAPA voice
embeddings and meeting titles carrying their names — so the four regression
suites that read it cannot run on a clean clone. These fixtures are the baseline
those suites run against everywhere; `test/fixtures/` stays an optional deeper
local corpus and is skipped with a message when absent.

They also supply what the private corpus cannot: a KNOWN speaker count per
track. `docs/COUNT_ESTIMATION_DESIGN.md` makes labelled fixtures with rapid
alternation and short backchannels a precondition for trusting its
speaker-count rule.

## What is in each fixture

| file | contents |
|---|---|
| `meeting.json` | the descriptor: mode, tracks, duration, title |
| `analysis.json` | `{{"transcripts": {{"mic": [...], "system": [...]}}}}`, with word timings |
| `analysis.npz` | `<track>_windows` (N,2) float64, `<track>_embeddings` (N,192) float32, `embed_version` |
| `labels.json` | **ground truth** — per-window true voice, true speaker count, generation parameters |

Window COORDINATES come from the real `diarization.build_windows()` over the
synthesized segments, so they are laid out exactly as a real run would lay them
out. Only the embedding VECTORS are synthesized.

## No personal data, by construction

No audio is read or produced and no real recording is involved. The vectors come
from numpy's PCG64 seeded with `{SEED}`; the scripts contain no personal names,
company names, or real meeting titles. The generator refuses to write text that
trips its name screen.

## Generation parameters

| parameter | value |
|---|---|
{params}

A voice's centroid is `sqrt(1-d)*common + sqrt(d)*anchor_i`, so two distinct
voices sit at cosine distance exactly `between_speaker_cosine_distance`. A window
is `normalise(centroid + sigma*noise)` with the noise drawn orthogonal to every
centroid, so two windows of one voice sit at about
`sigma^2/(1+sigma^2)`. Full derivation in the generator's module docstring.

## Fixtures

| slug | mode | true speakers | embeddings | windows | what it is for |
|---|---|---|---|---|---|
{rows}
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true",
                    help="regenerate into a temp dir and diff against the "
                         "committed fixtures instead of overwriting them")
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()

    if args.check:
        with tempfile.TemporaryDirectory(prefix="ms-synth-check-") as tmp:
            fresh = Path(tmp) / "synthetic"
            generate(fresh)
            same, diff, errs = filecmp.cmpfiles(
                fresh, Path(args.out),
                [str(p.relative_to(fresh)) for p in sorted(fresh.rglob("*"))
                 if p.is_file()],
                shallow=False,
            )
            if diff or errs:
                print(f"MISMATCH — differing: {diff}\n  missing/unreadable: {errs}")
                return 1
            print(f"OK — {len(same)} committed files match the generator.")
            return 0

    out_root = Path(args.out).resolve()
    # This script rmtree's its output directory. --out is user-supplied, and one
    # slip ("--out ~/.meetingscribe/recordings") would delete real meetings that
    # exist nowhere else. Refuse anything that is not a synthetic-fixture dir:
    # it must be named like one, and must contain only files we generated.
    def _refuse(why):
        raise SystemExit(f"refusing to delete {out_root}: {why}")

    for guarded in (Path.home() / ".meetingscribe", REPO / "recordings",
                    REPO / "models", Path.home()):
        g = guarded.resolve()
        if out_root == g or g in out_root.parents:
            _refuse(f"it is inside {g}, which holds real recordings or user data")
    if out_root.exists():
        if not out_root.is_dir():
            _refuse("it is not a directory")
        if "synthetic" not in out_root.name:
            _refuse('its name does not contain "synthetic"')
        stray = [p for p in out_root.rglob("*")
                 if p.is_file() and p.suffix not in (".npz", ".json", ".md")]
        if stray:
            _refuse(f"it holds {len(stray)} file(s) this generator did not write, "
                    f"e.g. {stray[0].name}")
        shutil.rmtree(out_root)
    index = generate(out_root)
    total = sum(p.stat().st_size for p in out_root.rglob("*") if p.is_file())
    print(f"wrote {len(index)} fixtures to {out_root}")
    for slug, info in index.items():
        print(f"  {slug:48s} truth={info['true_speaker_count']} "
              f"windows={info['windows']}")
    print(f"total on disk: {total / 1024:.0f} KiB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
