"""Persistent voice profiles: recognize named speakers across meetings.

The diarization stage answers "how many voices and which windows belong
together" per meeting, but every meeting starts nameless. This module closes
that loop: when the user renames a cluster ("Speaker 2" -> "Jess"), the
cluster's voice centroid is saved as a profile; on every later meeting, the
new clusters are compared against the saved profiles and matching ones get
the real name from the first word — no rename needed.

Design constraints, in order:

  1. NEVER overwrite a name a human typed. Recognition only replaces the
     formulaic defaults ("Speaker 3", "Remote 1"); anything else on the
     speaker map is the user's and stays.
  2. Wrong names are worse than missing names. Matching is conservative:
     a cluster gets a name only when it is close to exactly one profile
     (distance <= threshold AND the runner-up profile is not breathing down
     its neck), and only when the cluster carries enough speech to trust
     its centroid.
  3. A PROFILE IS A VOICE, NOT A NAME. Two people are genuinely called Jess,
     so the name is a label the user attaches and the embedding is the
     identity. Enrolling under an existing name merges into that profile only
     when the voices actually match (MERGE_MAX_DIST below); otherwise it
     starts a separate profile that happens to share the name. Merging two
     humans into one profile would be wrong twice over: the blended centroid
     becomes a wide catchment that mislabels bystanders, and one "Forget"
     would delete a fingerprint the notice never mentioned.
  4. Profiles are derived data. They are rebuilt from per-meeting samples,
     so a bad sample can be corrected (re-renaming the same cluster moves
     its sample between profiles) and deleting a profile is always safe.

Storage is a single JSON document at DATA_DIR/voice_profiles.json. A profile
holds one *sample* per (meeting, speaker-key) it was enrolled from — the
duration-weighted centroid of that cluster's embedding windows — and the
profile's working centroid is the weighted mean of its samples, recomputed on
load. Everything is tagged with diarization.EMBED_VERSION: samples from a
different embedding behaviour are ignored rather than compared.

Embeddings never leave the machine; a profile is ~200 floats per sample.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import time
import uuid
from pathlib import Path

import numpy as np

import diarization
from config import DATA_DIR

log = logging.getLogger("meetingscribe.voice_profiles")

PROFILES_PATH = DATA_DIR / "voice_profiles.json"
PROFILES_VERSION = 1

# A cluster must carry at least this much WINDOW TIME before its centroid is
# trusted for anything. Below it, a centroid is a handful of 2-second
# windows and cosine distances get noisy enough to cross the threshold by
# luck. Enrollment demands more than recognition: a profile is copied into
# every future meeting, so a bad one keeps costing, while a skipped
# recognition costs one rename.
#
# WINDOW TIME, NOT WALL-CLOCK SPEECH — the name says so because the two differ
# by about 2x and confusing them is a real bug that already shipped once (the
# Voices pane read this quantity as "minutes of speech"). diarization's windows
# are WINDOW_S = 2.0 long and step by HOP_S = 1.0, so every second of speech
# falls inside about two of them and centroid_for_spans' weight sum counts it
# about twice. Measured over the 41 clusters on this machine carrying at least
# 15 window-seconds, real speech / window time runs 0.543 .. 0.761 (median
# 0.604), so these gates mean roughly:
#   ENROLL_MIN_WINDOW_S 15.0  ->  8.1 .. 11.4 s of real speech (median 9.1)
#   MATCH_MIN_WINDOW_S  10.0  ->  5.4 ..  7.6 s of real speech (median 6.0)
# The VALUES are unchanged from when they were called ENROLL_MIN_S/MATCH_MIN_S:
# they were tuned against this quantity, so recalibrating them is a separate
# decision with its own measurements, not a side effect of naming it properly.
ENROLL_MIN_WINDOW_S = 15.0
MATCH_MIN_WINDOW_S = 10.0

# A single meeting's sample is capped at this weight in the profile centroid
# so one marathon call cannot permanently outvote every later (possibly
# better-recorded) sample.
SAMPLE_WEIGHT_CAP_S = 600.0

# Keep only the most recent samples per profile. Voices drift (mics, rooms,
# head colds); ancient samples eventually stop describing the person.
MAX_SAMPLES_PER_PROFILE = 10

# When the best and second-best profile are this close in cosine distance,
# the match is ambiguous and no name is assigned. Two profiles that both
# nearly match are exactly the situation where guessing mislabels someone.
#
# UNMEASURED, and the corpus cannot measure it: swept alongside RECOGNIZE_MAX_
# DIST below, this rule never once fired. Among the clusters that matched a
# profile at all, the tightest gap between the winner and a DIFFERENTLY-named
# runner-up was 0.233 at threshold 0.40 and 0.101 at 0.45, two to five times
# the margin. Gaps under 0.05 do occur (down to 0.001) but only between two
# profiles that are both 0.58 or further away, i.e. only where the winner was
# going to be rejected anyway. So this is insurance against a situation this
# machine has never been in, and 0.05 is a guess that has never been tested.
# It stays because the cost of holding it is one skipped name.
AMBIGUITY_MARGIN = 0.05

# Enrolling a sample under a name that already exists merges into that profile
# only when the new voice is within this cosine distance of the profile's
# working centroid. Beyond it, the two are different humans who share a name
# and each gets their own profile.
#
# MEASURED on this machine's corpus: 32 meetings with a live analysis.npz, 43
# speaker clusters, identities established from the transcripts rather than
# assumed —
#   SAME PERSON, DIFFERENT MEETINGS (47 pairs): the user across 10 mic-only
#     recordings (transcripts name him in each), D'nika Voges across her two
#     calls, and one recruiter across a two-part interview. 39 of the 47 pairs
#     land at or below 0.408. All 8 above it involve one 77-second test
#     recording whose mic caught more than one person in the room; its cluster
#     sits 0.570 .. 0.804 from every other sample of the same speaker, and
#     refusing to merge THAT into a person's profile is the right answer.
#   DIFFERENT PEOPLE (384 pairs): every within-meeting cluster pair, every
#     pair of system-track counterparts from different calls (the system track
#     is never the user), and the user against each of them. The closest two
#     strangers in the whole corpus sit at 0.511.
# So every value in [0.408, 0.511) classifies all 431 labelled pairs
# identically. Swept end to end: at 0.45 and at 0.50 the counts are the same
# (39/47 same-person merges, 0/384 stranger merges); the first stranger merge
# appears at 0.55 and it is 7/384 by 0.60.
#
# In the real operating condition (a new sample against a profile's blended
# centroid rather than sample against sample) the margin moves but does not
# widen: see RECOGNIZE_MAX_DIST below, which swept it properly. (An earlier
# draft of this block claimed 0.342 against a 1-sample profile and a nearest
# stranger at 0.513. Both were informal and both are wrong: 0.408 and 0.505.
# A 1-sample profile IS a sample, so its worst case cannot be better than the
# worst sample pair, and the corrected figure is exactly that pair.)
#
# 0.45 is chosen from inside that band, deliberately just below its midpoint
# (0.4595): 0.042 above the worst true-same pair, 0.061 below the closest true-
# different pair, with the tie broken toward the safe side because the two
# errors are NOT symmetric. Merging two strangers is the defect this constant
# exists to prevent — it blends a centroid that then mislabels a third person
# standing between them (measured: a voice midway between two humans 0.935
# apart sits 0.000 from their merged profile) and it puts two people behind one
# "Forget". Failing to merge one person costs a duplicate row in the Voices
# list, and costs nothing in recognition because match() no longer treats
# same-named profiles as rivals.
#
# Enrollment is ENTITLED to be looser than recognition, because it has the
# user typing the same name, which is real corroborating evidence for sameness,
# where recognition has only the embedding. It is not looser in fact: measured
# separately, recognition landed on the same 0.45 (RECOGNIZE_MAX_DIST), because
# both bands open at the same 0.408 and the value that clears it with margin is
# the same value in both places. That entitlement is the
# reason the two are allowed to move apart later; it is not a reason to have
# split them tonight. What the data does rule out for BOTH is 0.4, which sits
# ON the corpus's worst true-same pair (0.408) with no margin at all: here it
# would split one person in two, and in recognition it lost that person their
# name on their second meeting.
MERGE_MAX_DIST = 0.45

# The RECOGNITION cutoff: how close a fresh cluster's centroid must sit to a
# saved profile's WORKING centroid before that profile's name is applied to it.
# The user can move it (config's voice_profile_threshold); this is the shipped
# default and the number the measurement below picked. config.py holds a second
# copy for the settings file, and tools/test_voice_profiles.py fails if the two
# ever drift apart.
#
# MEASURED on the same corpus as MERGE_MAX_DIST: 32 cached meetings, 43
# clusters, 31 of them with an identity read off the transcripts (the user in
# 10, D'nika Voges in 2, one hiring manager across a two-part call in 2, and 17
# people seen exactly once). Leave-one-MEETING-out throughout and through the
# real enroll/load/match path, so no profile is ever scored against a sample it
# contains:
#   TRUE MATCHES (14): a held-out cluster against a profile built from that
#     person's OTHER meetings. 13 land at or below 0.278. The 14th is 0.683,
#     and it is the 77-second recording whose mic caught more than one person
#     in the room; MERGE_MAX_DIST already refuses to put that in anyone's
#     profile, and recognition refusing to answer for it is the same right
#     answer, not a miss.
#   FALSE MATCHES (576): every cluster against every profile carrying a
#     DIFFERENT person's name. The closest is 0.511, 1% are under 0.593, and
#     half sit beyond 0.886.
# Neither list holds the number that actually sets the floor, because both are
# dominated by profiles that have filled up. A profile starts with ONE sample,
# and the worst same-person distance to a 1-sample profile is 0.408 (76 such
# pairs). Profiles tighten as they fill (worst same-person distance 0.408 at
# 1 sample, 0.380 at 2, 0.348 at 4, 0.278 at 8), so a person's SECOND meeting
# is the hardest recognition the module will ever be asked for, and it is the
# one the threshold has to clear. The other edge holds across every size: the
# closest a profile of ANY size gets to a cluster that is not its person is
# 0.505.
# So the empty band is [0.408, 0.505): every value in it recognises all 14
# clean true matches INCLUDING the worst second-meeting one, and rejects all
# 576 stranger comparisons. Swept end to end through match(), 0.30 / 0.35 /
# 0.40 / 0.45 / 0.50 all give the same 13 recognitions and 0 wrong names; the
# first clearly wrong name appears at 0.55, where a hackathon room cluster is
# handed a synthesized demo voice's name from 0.510 away.
#
# 0.45 sits inside that band and just below its midpoint (0.4565): 0.042 above
# the worst second-meeting true match, 0.055 below the closest stranger any
# profile has to anyone. The 0.4 it replaces was NOT in the band: it sat 0.008
# under that worst second-meeting match and lost it, and that is the single
# measured error this value fixes. Everything else about the corpus is
# unchanged by the move.
#
# Biased below the middle deliberately, because neither the cost nor the
# exposure is symmetric. A wrong name is a confident lie inside a transcript
# the user trusts; a missing name costs one rename, and that rename enrolls a
# second sample, after which every measured true match of that person lands at
# or below 0.380 and this threshold stops being the binding constraint. And
# recognition draws from the stranger distribution far more often than
# enrollment does: enrollment compares only against profiles the user just
# named, while every meeting scores every cluster against every profile, so
# stranger comparisons grow with (profiles x clusters per meeting). The 576
# measured here come from 20 people; a full profile list will draw that many
# in a week, and the left tail of a distribution is the part a small sample
# under-states.
RECOGNIZE_MAX_DIST = 0.45

# Names the app itself invents. These are never enrollable (renaming a
# speaker to "Speaker 2" is the user *undoing* a name) and always
# overridable by recognition.
_DEFAULT_NAME_RE = re.compile(r"^(?:speaker|remote)\s*\d+$|^you$", re.IGNORECASE)

_LOCK = threading.Lock()


def is_default_name(name):
    """True for the formulaic names the pipeline assigns on its own."""
    return bool(_DEFAULT_NAME_RE.match((name or "").strip()))


def _fold(name):
    """The comparable form of a display name. "  Jess " and "jess" are one
    label on two people; they are still not one voice.
    """
    return (name or "").strip().casefold()


# --- storage -----------------------------------------------------------------

def _load_raw():
    try:
        doc = json.loads(PROFILES_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"version": PROFILES_VERSION, "profiles": []}
    if not isinstance(doc, dict) or not isinstance(doc.get("profiles"), list):
        return {"version": PROFILES_VERSION, "profiles": []}
    return doc


def _save_raw(doc):
    # Atomic for the same reason meeting.json's writes are: a reader (the
    # pipeline thread) must never see a half-written document.
    tmp_fd, tmp_name = tempfile.mkstemp(
        dir=str(PROFILES_PATH.parent), prefix=".voice_profiles-", suffix=".tmp"
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False)
        os.replace(tmp_name, PROFILES_PATH)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _usable_samples(prof):
    """The samples of a raw profile dict that THIS build can compare against."""
    return [
        s for s in (prof.get("samples") or {}).values()
        if s.get("embed_version") == diarization.EMBED_VERSION
        and isinstance(s.get("centroid"), list) and s.get("seconds")
    ]


def _sample_speech_seconds(sample):
    """Wall-clock speech behind one sample, as opposed to its window time.

    Samples written before this field existed recorded only window time, which
    over-counts by WINDOW_S/HOP_S because diarization's windows overlap. Scale
    those back instead of showing the inflated figure: HOP_S/WINDOW_S is the
    exact ratio for one long run of speech and an UNDER-estimate for scattered
    speech, and under-stating how much of a person is held is the right
    direction to be wrong in.
    """
    value = sample.get("speech_seconds")
    if isinstance(value, (int, float)) and value >= 0:
        return float(value)
    return float(sample["seconds"]) * diarization.HOP_S / diarization.WINDOW_S


def _working_centroid(samples):
    """The voice a set of samples describes.

    Returns (unit centroid, window seconds, speech seconds, n) or None when
    nothing in `samples` is usable. This IS the profile as far as every
    comparison in this module is concerned: it is recomputed from the samples
    rather than stored, so moving or trimming a sample takes effect at once.
    """
    vecs, weights, window_s, speech_s = [], [], 0.0, 0.0
    for s in samples:
        v = np.asarray(s["centroid"], dtype=np.float64)
        n = np.linalg.norm(v)
        if not np.isfinite(n) or n == 0:
            continue
        vecs.append(v / n)
        weights.append(min(float(s["seconds"]), SAMPLE_WEIGHT_CAP_S))
        window_s += float(s["seconds"])
        speech_s += _sample_speech_seconds(s)
    if not vecs:
        return None
    centroid = np.average(np.stack(vecs), axis=0, weights=weights)
    norm = np.linalg.norm(centroid)
    if not np.isfinite(norm) or norm == 0:
        return None
    return centroid / norm, window_s, speech_s, len(vecs)


def load_profiles():
    """Profiles usable by THIS build: samples from other EMBED_VERSIONs are
    dropped from the working set (not from disk). Returns a list of
    {id, name, centroid(np.ndarray, unit norm), window_seconds, speech_seconds,
    n_samples, updated}.

    `window_seconds` is the internal weight (overlapping windows, counts each
    second about twice); `speech_seconds` is the honest wall-clock figure and
    the only one anything user-facing may show.
    """
    out = []
    for prof in _load_raw()["profiles"]:
        working = _working_centroid(_usable_samples(prof))
        if working is None:
            continue
        centroid, window_s, speech_s, n = working
        out.append({
            "id": prof.get("id"),
            "name": prof.get("name"),
            "owner": bool(prof.get("owner")),
            "centroid": centroid,
            "window_seconds": window_s,
            "speech_seconds": speech_s,
            "n_samples": n,
            "updated": prof.get("updated"),
        })
    return out


def list_profiles():
    """JSON-safe listing for the API — no embedding vectors, and no window
    time either: the UI has no business with a duration that counts every
    second of speech twice, so the field simply is not offered.
    """
    return [
        {k: p[k] for k in ("id", "name", "owner", "speech_seconds", "n_samples", "updated")}
        for p in load_profiles()
    ]


def get_profile(profile_id):
    """One JSON-safe profile record, or None. Lets a caller that just enrolled
    describe the profile it landed on without guessing at it by name.
    """
    return next((p for p in list_profiles() if p["id"] == profile_id), None)


def delete_profile(profile_id):
    """Remove a profile entirely. Returns True if something was deleted."""
    with _LOCK:
        doc = _load_raw()
        kept = [p for p in doc["profiles"] if p.get("id") != profile_id]
        if len(kept) == len(doc["profiles"]):
            return False
        doc["profiles"] = kept
        _save_raw(doc)
        return True


def forget_meeting(meeting_id):
    """Drop everything a meeting taught. Returns (samples_removed, profiles_removed).

    Deleting a recording has to delete what was learned from it, or "delete"
    is not true: the audio and the transcript go, and a fingerprint derived
    from them stays behind in a file the user was never shown. Samples are
    keyed "<meeting_id>:<speaker_key>" (see enroll_sample), so the meeting's
    contribution is exactly the samples with that prefix. A profile left with
    no samples is dropped, the same way enroll_sample drops husks.

    Best effort by contract: a recording must still delete when the profile
    store is unwritable, so the caller logs and continues rather than failing
    the delete.
    """
    prefix = f"{meeting_id}:"
    with _LOCK:
        doc = _load_raw()
        samples_removed = 0
        kept_profiles = []
        for prof in doc["profiles"]:
            samples = prof.get("samples") or {}
            survivors = {k: v for k, v in samples.items() if not k.startswith(prefix)}
            samples_removed += len(samples) - len(survivors)
            if not survivors:
                continue
            prof["samples"] = survivors
            kept_profiles.append(prof)
        profiles_removed = len(doc["profiles"]) - len(kept_profiles)
        if not samples_removed and not profiles_removed:
            return 0, 0
        doc["profiles"] = kept_profiles
        _save_raw(doc)
        return samples_removed, profiles_removed


# --- centroids ---------------------------------------------------------------

def centroid_for_spans(windows, embeddings, spans):
    """Duration-weighted centroid of the windows whose midpoint falls inside
    any of `spans` [(start, end), ...]. Pure numpy — no model, no audio.

    Returns (unit vector | None, window seconds, speech seconds). The two
    durations are NOT interchangeable and both are returned because the module
    needs both:

      window seconds — the sum of the windows' lengths, i.e. the weight this
        cluster carries. diarization.build_windows steps by HOP_S = 1.0 with
        WINDOW_S = 2.0 windows, so consecutive windows overlap and each second
        of speech is counted about twice. Every internal gate (ENROLL_MIN_
        WINDOW_S, MATCH_MIN_WINDOW_S, SAMPLE_WEIGHT_CAP_S) is calibrated
        against this number, so this is what they keep getting.
      speech seconds — the length of the UNION of those windows, i.e. how much
        of the meeting this person is actually audible for. This is the only
        one that may be shown to a human. 40 windows spanning 0 s to 41 s are
        80.0 window seconds and 41.0 speech seconds.
    """
    if windows is None or embeddings is None or not spans:
        return None, 0.0, 0.0
    win = np.asarray(windows, dtype=np.float64)
    emb = np.asarray(embeddings, dtype=np.float64)
    if win.ndim != 2 or len(win) == 0 or len(win) != len(emb):
        return None, 0.0, 0.0
    mids = (win[:, 0] + win[:, 1]) / 2.0
    mask = np.zeros(len(win), dtype=bool)
    for start, end in spans:
        mask |= (mids >= start) & (mids <= end)
    if not mask.any():
        return None, 0.0, 0.0
    durs = (win[mask, 1] - win[mask, 0])
    vecs = emb[mask]
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    centroid = np.average(vecs / norms, axis=0, weights=durs)
    norm = np.linalg.norm(centroid)
    if not np.isfinite(norm) or norm == 0:
        return None, 0.0, 0.0
    # Union length of the selected (overlapping) windows: sort by start, then
    # charge each window only the part beyond every window that started before
    # it. build_windows already emits them in order, but selection and callers
    # are not required to preserve that, so sort rather than assume.
    sel = win[mask]
    sel = sel[np.argsort(sel[:, 0], kind="stable")]
    reached = np.maximum.accumulate(sel[:, 1])
    prior_end = np.concatenate(([-np.inf], reached[:-1]))
    fresh = sel[:, 1] - np.maximum(sel[:, 0], prior_end)
    speech = float(np.clip(fresh, 0.0, None).sum())
    return centroid / norm, float(durs.sum()), speech


def _spans_by_speaker(segments):
    """{(track, speaker_key): [(start, end), ...]} from labelled segments or
    turns — anything carrying speaker/track/start/end."""
    spans = {}
    for seg in segments:
        skey, track = seg.get("speaker"), seg.get("track")
        if not skey or not track:
            continue
        spans.setdefault((track, skey), []).append((seg["start"], seg["end"]))
    return spans


def cluster_centroids(track_state, segments):
    """{speaker_key: (centroid, window seconds)} for every labelled speaker
    whose track has embeddings in track_state ({track: {windows, embeddings}}).

    Recognition never shows a duration to anyone, so it keeps the window-time
    weight MATCH_MIN_WINDOW_S is calibrated against and drops the rest.
    """
    out = {}
    for (track, skey), spans in _spans_by_speaker(segments).items():
        state = (track_state or {}).get(track)
        if not state:
            continue
        centroid, seconds, _speech = centroid_for_spans(
            state.get("windows"), state.get("embeddings"), spans
        )
        if centroid is not None:
            # A speaker split across tracks (does not happen today) would
            # keep whichever track carried more speech.
            if skey in out and out[skey][1] >= seconds:
                continue
            out[skey] = (centroid, seconds)
    return out


# --- recognition -------------------------------------------------------------

def match(centroids, profiles, threshold):
    """One-to-one greedy match of cluster centroids against profiles.

    centroids: {speaker_key: (unit vector, window seconds)}
    Returns {speaker_key: profile name}. A pair is assigned only when its
    cosine distance is <= threshold, the cluster has MATCH_MIN_WINDOW_S of
    window time, and no profile carrying a DIFFERENT name sits within
    AMBIGUITY_MARGIN of the winner.
    """
    candidates = []  # (distance, skey, profile index)
    per_cluster = {}  # skey -> [(distance, profile name)], for the ambiguity check
    for skey, (vec, seconds) in centroids.items():
        if seconds < MATCH_MIN_WINDOW_S:
            continue
        for i, prof in enumerate(profiles):
            dist = float(1.0 - np.dot(vec, prof["centroid"]))
            per_cluster.setdefault(skey, []).append((dist, prof["name"]))
            if dist <= threshold:
                candidates.append((dist, skey, i))
    assigned, used_clusters, used_profiles = {}, set(), set()
    for dist, skey, i in sorted(candidates):
        if skey in used_clusters or i in used_profiles:
            continue
        winner = profiles[i]["name"]
        # Only a profile carrying a DIFFERENT name is a rival. The check exists
        # to stop the module GUESSING between two answers, and two profiles
        # that agree on the label do not offer two answers — whichever wins,
        # the speaker map reads the same. This matters because same-named
        # profiles are now normal: enrollment splits one person in two rather
        # than merge a stranger in (MERGE_MAX_DIST), and without this the split
        # would silently cost that person every future recognition.
        rival = min((d for d, other in per_cluster[skey]
                     if _fold(other) != _fold(winner)), default=None)
        if rival is not None and (rival - dist) < AMBIGUITY_MARGIN:
            log.info("voice match for %s ambiguous (%.3f vs %.3f) — skipped",
                     skey, dist, rival)
            used_clusters.add(skey)
            continue
        assigned[skey] = winner
        used_clusters.add(skey)
        used_profiles.add(i)
    return assigned


def apply_recognition(track_state, segments, speakers, cfg):
    """Fill recognized names into the speaker map, defaults-only.

    Mutates `speakers` ({key: display name}) in place; returns the {key:
    name} dict of what was applied (possibly empty). Never raises past a log:
    recognition is a bonus layer and must not cost anyone a transcript.
    """
    try:
        if not cfg.get("voice_profiles", True):
            return {}
        # The owner's print never names a far-end cluster: the user's voice on
        # the call-audio track is self-echo, which pipeline.drop_self_echo
        # removes before this runs, and a "You" label on the far side would
        # contradict the mic. The owner is matched by recognize_owner, on the
        # mic, on the meetings where the mic is not "You" by construction.
        profiles = [p for p in load_profiles() if not p.get("owner")]
        if not profiles:
            return {}
        replaceable = {k for k, v in speakers.items()
                       if is_default_name(v) and k != "you"}
        if not replaceable:
            return {}
        centroids = cluster_centroids(track_state, segments)
        centroids = {k: v for k, v in centroids.items() if k in replaceable}
        if not centroids:
            return {}
        threshold = float(cfg.get("voice_profile_threshold", RECOGNIZE_MAX_DIST))
        names = match(centroids, profiles, threshold)
        # Never introduce a duplicate display name: if "Jess" is already on
        # the map (user-typed on another cluster), recognition backs off.
        taken = {_fold(v) for v in speakers.values()}
        applied = {}
        for skey, name in names.items():
            if _fold(name) in taken:
                continue
            speakers[skey] = name
            taken.add(_fold(name))
            applied[skey] = name
        if applied:
            log.info("voice profiles recognized: %s", applied)
        return applied
    except Exception:
        log.exception("voice recognition failed — leaving default names")
        return {}


# --- the owner's own voice ---------------------------------------------------
# Online, the mic is "You" by construction, and until 2026-08-21 that was the
# end of it: the mic was never embedded, so the one clean recording of the
# user's voice the product takes on every call was thrown away, and on the
# meetings where the mic is NOT "You" by construction — in-person, and the
# mic-only fallback — the user was just another "Speaker N". The owner
# profile closes that: it is enrolled automatically from healthy online calls
# (pipeline decides what "healthy" means: no echo cleanup fired) and matched
# against the mic clusters of in-person and fallback meetings.
#
# It is ONE profile, flagged "owner", displayed as OWNER_NAME, and kept out of
# the ordinary far-end recognition (apply_recognition). It is derived data
# like every other profile: samples per meeting, MAX_SAMPLES_PER_PROFILE
# newest kept, SAMPLE_WEIGHT_CAP_S per sample, forgotten with the meeting and
# deletable from the Voices list.

OWNER_NAME = "You"


def _owner_raw(doc):
    return next((p for p in doc["profiles"] if p.get("owner")), None)


def enroll_owner(meeting_id, speaker_key, centroid, seconds, speech_seconds=None):
    """File one meeting's sample of the user's own voice. Returns the owner
    profile's id, or None when the sample is refused (too little speech, or
    no usable centroid). Same gate as any enrollment: ENROLL_MIN_WINDOW_S.
    """
    if centroid is None or seconds < ENROLL_MIN_WINDOW_S:
        return None
    vec = np.asarray(centroid, dtype=np.float64).ravel()
    vec_norm = np.linalg.norm(vec)
    if not np.isfinite(vec_norm) or vec_norm == 0:
        return None
    vec = vec / vec_norm
    sample_key = f"{meeting_id}:{speaker_key}"
    now = int(time.time())
    sample = {
        "centroid": [round(float(x), 6) for x in vec],
        "seconds": round(float(seconds), 2),
        "embed_version": diarization.EMBED_VERSION,
        "updated": now,
    }
    if speech_seconds is not None:
        sample["speech_seconds"] = round(float(speech_seconds), 2)
    with _LOCK:
        doc = _load_raw()
        target = _owner_raw(doc)
        if target is None:
            target = {"id": f"vp_{uuid.uuid4().hex[:12]}", "name": OWNER_NAME,
                      "owner": True, "created": now, "samples": {}}
            doc["profiles"].append(target)
        target.setdefault("samples", {})[sample_key] = sample
        target["updated"] = now
        if len(target["samples"]) > MAX_SAMPLES_PER_PROFILE:
            keep = sorted(target["samples"].items(),
                          key=lambda kv: kv[1].get("updated", 0),
                          reverse=True)[:MAX_SAMPLES_PER_PROFILE]
            target["samples"] = dict(keep)
        _save_raw(doc)
        profile_id = target["id"]
    log.info("enrolled owner voice sample %s (%.0f window-s)", sample_key, seconds)
    return profile_id


def enroll_owner_from_state(meeting_id, state, turns):
    """Enroll the owner from a finished ONLINE meeting's mic embeddings.

    `state` is {windows, embeddings} of the mic track; `turns` the meeting's
    turns, of which the ones labelled "you" on the mic are the user by
    construction. Best effort: returns the profile id or None, never raises.
    """
    try:
        if not state or not turns:
            return None
        spans = [(t["start"], t["end"]) for t in turns
                 if t.get("speaker") == "you" and t.get("track", "mic") == "mic"]
        if not spans:
            return None
        centroid, seconds, speech = centroid_for_spans(
            state.get("windows"), state.get("embeddings"), spans)
        return enroll_owner(meeting_id, "you", centroid, seconds, speech)
    except Exception:
        log.exception("owner voice enrollment failed")
        return None


def owner_profile():
    """The owner's working profile (see load_profiles), or None."""
    return next((p for p in load_profiles() if p.get("owner")), None)


def recognize_owner(track_state, segments, speakers, cfg, track="mic"):
    """Which default-named cluster on `track` is the user? Returns its
    speaker key, or None when there is no owner profile, no cluster is close
    enough, or two clusters are both close (then guessing would label a
    stranger "You", which is worse than a rename).

    Same bars as ordinary recognition: MATCH_MIN_WINDOW_S of window time,
    cosine distance <= the configured threshold, and the runner-up CLUSTER at
    least AMBIGUITY_MARGIN further away than the winner. Never raises.
    """
    try:
        if not cfg.get("voice_profiles", True):
            return None
        owner = owner_profile()
        if owner is None:
            return None
        candidates = {k for k, v in speakers.items() if is_default_name(v)}
        if not candidates:
            return None
        centroids = cluster_centroids(
            {track: (track_state or {}).get(track)},
            [s for s in segments if s.get("track") == track])
        scored = sorted(
            (float(1.0 - np.dot(vec, owner["centroid"])), skey)
            for skey, (vec, seconds) in centroids.items()
            if skey in candidates and seconds >= MATCH_MIN_WINDOW_S)
        if not scored:
            return None
        threshold = float(cfg.get("voice_profile_threshold", RECOGNIZE_MAX_DIST))
        dist, skey = scored[0]
        if dist > threshold:
            return None
        if len(scored) > 1 and (scored[1][0] - dist) < AMBIGUITY_MARGIN:
            log.info("owner match ambiguous (%.3f vs %.3f) — skipped",
                     dist, scored[1][0])
            return None
        log.info("owner voice recognized as %s (distance %.3f)", skey, dist)
        return skey
    except Exception:
        log.exception("owner recognition failed — leaving default names")
        return None


# --- enrollment --------------------------------------------------------------

def enroll_sample(name, meeting_id, speaker_key, centroid, seconds,
                  speech_seconds=None):
    """File one meeting's sample under `name`. Returns the id of the profile it
    landed on, or None when the sample was refused.

    The VOICE picks the profile; `name` only narrows the field. Enrolling under
    a name that already exists merges into that profile when the two are the
    same voice (MERGE_MAX_DIST) and starts a separate profile when they are
    not, because two people really can both be called Jess and blending them
    would produce a centroid that is neither of them.

    The sample is keyed "<meeting_id>:<speaker_key>" and lives on exactly one
    profile, so re-renaming the same cluster REPLACES its sample rather than
    double-counting it, and any copy sitting elsewhere is removed — which is
    what corrects an earlier wrong rename ("s1 is Sarah… no, s1 is Bob").
    """
    if (not (name or "").strip() or is_default_name(name)
            or centroid is None or seconds < ENROLL_MIN_WINDOW_S):
        return None
    vec = np.asarray(centroid, dtype=np.float64).ravel()
    vec_norm = np.linalg.norm(vec)
    if not np.isfinite(vec_norm) or vec_norm == 0:
        return None
    vec = vec / vec_norm
    label = name.strip()
    sample_key = f"{meeting_id}:{speaker_key}"
    now = int(time.time())
    sample = {
        "centroid": [round(float(x), 6) for x in vec],
        "seconds": round(float(seconds), 2),
        "embed_version": diarization.EMBED_VERSION,
        "updated": now,
    }
    if speech_seconds is not None:
        sample["speech_seconds"] = round(float(speech_seconds), 2)
    with _LOCK:
        doc = _load_raw()
        named = [p for p in doc["profiles"] if _fold(p.get("name")) == _fold(label)]
        # 1. This cluster already sits on a profile of this name: an idempotent
        #    re-rename, or a Reprocess replaying the same rename. Same person by
        #    construction, so it stays put and the caller gets the same id back.
        target = next((p for p in named
                       if sample_key in (p.get("samples") or {})), None)
        distance = None
        if target is None:
            # 2. Otherwise the embedding decides which same-named profile — if
            #    any — this person is. Compare against the profile's working
            #    centroid, which cannot contain this sample: a sample lives on
            #    one profile only, and case 1 already claimed that profile.
            best = None
            for prof in named:
                working = _working_centroid(_usable_samples(prof))
                if working is None:
                    continue
                dist = float(1.0 - np.dot(vec, working[0]))
                if distance is None or dist < distance:
                    best, distance = prof, dist
            if best is not None and distance <= MERGE_MAX_DIST:
                target = best
            elif best is not None:
                log.info(
                    "voice sample %s is a DIFFERENT person also called %r "
                    "(distance %.3f > %.2f) — new profile",
                    sample_key, label, distance, MERGE_MAX_DIST)
        if target is None:
            # 3. A same-named profile this build cannot read (its samples
            #    predate an EMBED_VERSION bump) offers no voice to compare, so
            #    it lost case 2 by default. Adopt it rather than orphan it: its
            #    embeddings are already on disk, and a profile that load_
            #    profiles skips is one the Voices list cannot show and Forget
            #    cannot reach. New samples push the unreadable ones out by age.
            target = next((p for p in named if not _usable_samples(p)), None)
        if target is None:
            target = {"id": f"vp_{uuid.uuid4().hex[:12]}", "name": label,
                      "created": now, "samples": {}}
            doc["profiles"].append(target)
        # One cluster, one profile. Any older copy of this sample — under a
        # different name, or under a same-named profile now known to be someone
        # else — is a stale claim on this person and goes.
        for prof in doc["profiles"]:
            if prof is target:
                continue
            if (prof.get("samples") or {}).pop(sample_key, None) is not None:
                prof["updated"] = now
        target.setdefault("samples", {})[sample_key] = sample
        target["updated"] = now
        # Trim to the newest samples; "newest" by their updated stamp.
        if len(target["samples"]) > MAX_SAMPLES_PER_PROFILE:
            keep = sorted(target["samples"].items(),
                          key=lambda kv: kv[1].get("updated", 0),
                          reverse=True)[:MAX_SAMPLES_PER_PROFILE]
            target["samples"] = dict(keep)
        # Profiles whose samples all moved away are empty husks — drop them.
        doc["profiles"] = [p for p in doc["profiles"] if p.get("samples")]
        _save_raw(doc)
        profile_id = target["id"]
    log.info("enrolled voice sample %s -> %r (%s, %.0f window-s%s)", sample_key,
             label, profile_id, seconds,
             "" if distance is None else f", voice distance {distance:.3f}")
    return profile_id


def enroll_from_meeting(meeting_dir, meta, speaker_key, name):
    """Enroll one speaker of a finished meeting from its cached analysis.

    Called from the rename endpoint. Returns the id of the profile the sample
    landed on, so the caller can describe THAT profile instead of hunting for
    one by name — names are not unique and never were. Best effort by contract:
    returns None (never raises) when the meeting has no usable cache — no npz,
    a stale EMBED_VERSION, a cache that belongs to different transcripts, or
    too little speech on the cluster.
    """
    try:
        if is_default_name(name):
            return None
        meeting_dir = Path(meeting_dir)
        npz_path = meeting_dir / "analysis.npz"
        json_path = meeting_dir / "analysis.json"
        if not npz_path.exists() or not json_path.exists():
            return None
        try:
            analysis = json.loads(json_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        # "state_id" mirrors pipeline.STATE_ID_KEY (not imported: pipeline
        # imports this module). Same pairing rule as _load_analysis_cache.
        state_id = analysis.get("state_id") if isinstance(analysis, dict) else None
        spans = [
            (t["start"], t["end"])
            for t in (meta.get("turns") or [])
            if t.get("speaker") == speaker_key
        ]
        if not spans:
            return None
        track = next((t.get("track") for t in meta["turns"]
                      if t.get("speaker") == speaker_key), None)
        if not track:
            return None
        with np.load(npz_path) as npz:
            cached_version = int(npz["embed_version"]) if "embed_version" in npz else 1
            if cached_version != diarization.EMBED_VERSION:
                return None
            npz_state_id = npz["state_id"].item() if "state_id" in npz else None
            if npz_state_id != state_id:
                return None
            if f"{track}_windows" not in npz or f"{track}_embeddings" not in npz:
                return None
            centroid, seconds, speech = centroid_for_spans(
                npz[f"{track}_windows"], npz[f"{track}_embeddings"], spans
            )
        if centroid is None:
            return None
        return enroll_sample(name, meta.get("id") or meeting_dir.name,
                             speaker_key, centroid, seconds, speech)
    except Exception:
        log.exception("voice enrollment failed for %r", name)
        return None
