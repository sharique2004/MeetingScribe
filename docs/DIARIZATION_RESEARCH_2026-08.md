# Diarization — how it works today, where it fails, and what should happen next

*Research pass 2026-08-21. Four parallel audits: two over this tree (HEAD `1281f04`, branch `Production`),
two over 2025–2026 literature and shipping products. Every claim about the repo carries a file:line against
that tree; every external claim carries a link. Meeting labels are the corpus pseudonyms from
`DIARIZATION_AUDIT_ADDENDUM.md` §0.*

> **Frame.** "Diarization" in this product is two different problems that share one word.
> The **mic end** is *who is in this room* — online it is "You" by construction; in person it is a real
> clustering problem over one shared microphone. The **speakers end** is *who is inside the far-end mixdown*
> — one stream that Zoom/Meet/Teams has already mixed, which no local API can un-mix. They fail differently,
> they are measured differently, and most past regressions came from treating them as one.

---

## 0. The short version

**What is true today**

- Counting is solved on the corpus you have: the GEN3 fold cascade scores 21/21 real, 10/11 synthetic, 4/4 on
  the only fixtures that discriminate a diarizer from a constant (`tools/eval_diarization.py --check`, exit 0,
  re-run today). The neural hybrid (FluidAudio offline community-1, forced to the classic count) cut
  far-field AMI confusion from 21.7% → 1.6% (`native/diarization-ab/MULTIPARTY.md:12-29`).
- **Nothing measures the deliverable.** The product ships a *speaker-labelled transcript*; the harness scores
  *how many speakers*. `ATTRIBUTION_GEN1` is empty — 0 of 22 real fixtures have turn truth
  (`tools/eval_diarization.py:3227-3238`). A constant "1 speaker" predictor scores 81% on the same corpus.
- The three defects that survive are **not clustering defects**: a calendar count that still forces k on the
  system track (`pipeline.py:1979`, `:2453`); echo handling that is text-only and one-directional
  (`pipeline.py:1120-1197`); and the local user's own voice never being enrolled because online the mic is
  never embedded (`pipeline.py:1965-1970`, `voice_profiles.py:571`).

**What should happen, in order** (each gated, details in §5)

1. Turn truth on Demo, Room P, Q, T with `tools/label_turns.py` — ~20 min/meeting, not the 6–12 h the roadmap
   assumed. Report tcpWER + turn accuracy beside counts. Everything below is unfalsifiable until this exists.
2. Calendar count → prior (cap + `min_speakers`), never a forced k. User-typed stays forced.
3. One aggregate, one clock (mic as drift-compensated sub-device of the tap aggregate) + host-time anchors +
   resync on every rebuild. Cheap, and it makes every later acoustic signal a constant-lag problem.
4. Symmetric echo: self-echo (your voice inside the system track) detected by lagged correlation and dropped
   from the far-end cluster set.
5. Enroll "You" from the online mic (the one clean sample the product throws away); pin that print on
   in-person and mic-fallback meetings. Disclosure already ships.
6. Run the neural engine on a *real* backchannel recording before touching another constant — the synthetic
   backchannel fixture has no audio, so the engine built for frame-level turns has never been tested on the
   one failure the classic path cannot fix.
7. Only then: LS-EEND for live in-person labels; coherence-based leak attribution for the no-headphones case.

**What not to do** (already measured or licence-blocked): swap the counter for a neural one; VPIO on the mic;
port sherpa-onnx as the main engine; tighten `FOLD_*` constants; DiariZen (CC-BY-NC); Sortformer for rooms
(4-speaker cap).

---

## 1. How it works today — the mic end

### 1.1 Data flow

Two WAVs, never mixed (`audio_recorder.py:1-21`). Mic = PortAudio default input at the device's native rate,
≤2 ch, int16, 1024-frame chunks (`audio_recorder.py:310-332`); no voice processing, no AEC. ASR runs per
track (`pipeline.py:2464-2477`), each track's segments shifted by its `start_offset` onto one timeline
(`_apply_offset`, `pipeline.py:944-953`).

**Windows come from ASR segments, not from audio** — ASR is the VAD (`diarization.build_windows`,
`diarization.py:424-441`): segments < 0.4 s skipped, ≤ 2.0 s → one window, longer → 2.0 s / 1.0 s hop.
ECAPA-TDNN (speechbrain export, onnxruntime CPU, 192-d, L2-normed; `diarization.py:394-421`, `:499-507`).
A dropped echo segment is therefore **invisible to the embedder** — which is why capture-time AEC was
measured as inert for clustering (`ATTRIBUTION_AND_ROADMAP_PRD.md:219-235`).

### 1.2 Which track is clustered depends on mode (`pipeline.py:1896-1990`)

| mode | mic | system | `"you"` key |
|---|---|---|---|
| online, system has speech | **not embedded**; every segment = "You" (`:1965-1970`) | clustered `s1..` with `expected` forced (`:1979`) | yes |
| online, system transcribed to zero segments | clustered `s1..`, nobody is "You" (`:1902-1964`) | — | **no** |
| in-person | clustered `s1..` with `expected` forced (`:1985`) | clustered `r1..` "Remote N", auto (`:1990`) | **no** |

The mic-only fallback fires only on **zero** system segments with ≥ 30 s of mic speech
(`_system_track_lost`, `pipeline.py:1287-1339`); ratio gates were rejected with corpus numbers (`:1293-1305`).

### 1.3 The count: GEN3 (`diarization.cluster`, `diarization.py:627-666`; `_fold_weak_clusters`, `:547-624`)

Agglomerative, cosine, average linkage, `distance_threshold = 0.6`. On every real track that yields
22–863 raw clusters (> `MAX_AUTO_SPEAKERS = 8`), so the result is discarded and refit at k = 8; the count is
then decided entirely by the fold cascade, in this load-bearing order:

| step | constant | value | evidence band |
|---|---|---|---|
| keep if ≥ | `FOLD_KEEP_ABOVE_S` | 150 s | (129.92, 203.86] — **both edges single fixtures**, upper edge Room W whose truth is UNKNOWN (`diarization.py:82-102`) |
| keep if fragmentation ≤ | `FOLD_MAX_FRAGMENTATION` | 0.30 | [0.139, 0.333] |
| weak if ratio-to-leader < | `FOLD_MAX_DURATION_RATIO` | 0.50 | [0.356, >0.99] — upper edge unmeasurable |
| or seconds < | `MIN_CLUSTER_S` | 10 s | no lower edge |
| final pairwise merge below | `threshold × 0.9` | 0.54 | — |

Forced k (`n_speakers` given) returns exactly k clusters and **skips every fold** (`diarization.py:638-641`).

### 1.4 The turns: neural hybrid (`pipeline._neural_refine`, `pipeline.py:1589-1691`; `diarization_neural.py`)

FluidAudio offline community-1 (powerset seg + WeSpeaker 256-d + VBx; CoreML, ~22 MB, pinned rev
`5390df97…`) via `native/fluiddiarizer`. Division of labour is asymmetric and measured:

- Classic decides **how many** (21/21 vs neural-auto best 19/21; Room T — two people in one room —
  under-counted by neural at every threshold, `OFFLINE_RETEST.md:27-55`).
- Neural, pinned to that count, decides **who-when** (AMI far-field confusion 21.7% → 1.6%; forced k=4 on
  loudspeaker playback 27.6% → 2.7%, `MULTIPARTY.md:12-29, 50-62`).
- Neural may **raise** the count on the unforced path when it hears more ≥ 5 s voices than classic kept
  (`pipeline.py:1670-1673`; shipped after the Sidemen-video test, `MULTIPARTY.md:78-84`). Never lowers.
- User-typed count (`speaker_count_source == "user"`) runs pinned with no self-validation (`:1648-1651`);
  a calendar-forced count keeps the count and merges extras down (`:1665-1669`).

### 1.5 Identity: voice profiles (`voice_profiles.py`)

Enrollment fires **only on rename** (`app.py:2513-2551` → `enroll_from_meeting`, `voice_profiles.py:702-756`),
needs ≥ 15 window-seconds (`ENROLL_MIN_WINDOW_S`, ≈ 8–11 s real speech), refuses default names including
`you` (`_DEFAULT_NAME_RE`, `:229`). Recognition (`apply_recognition`, `:557-595`, called `pipeline.py:2015`)
matches only default-named keys **and `k != "you"`** (`:571`), greedy one-to-one, `RECOGNIZE_MAX_DIST = 0.45`
(measured: 2 of 120 cross-meeting pairs under 0.4, next at 0.594 — the threshold sits in a 0.48-wide empty
band), `AMBIGUITY_MARGIN = 0.05` "UNMEASURED … never once fired" (`:100-108`).

**The local user is never enrolled online**: the mic is not embedded, so `analysis.npz` has no `mic_windows`
and `enroll_from_meeting` returns None even if "You" is renamed (`voice_profiles.py:745-746`). In-person and
mic-fallback meetings put the user in `s1..sN` like anyone else — with no print to pin them to.

### 1.6 Live captions

Track-labelled only: `TRACK_LABELS = {"mic": "You", "system": "Them"}` (`live_captions.py:30`). Apple
SpeechAnalyzer per track (`tools/apple_live.swift`), 0.25 s analyzer buffers, queue ≤ ~2.3 s. No diarization.

---

## 2. How it works today — the speakers end

### 2.1 Capture

Core Audio **global** process tap (`tools/apple_syscap.swift:396-403`: `stereoGlobalTapButExcludeProcesses`,
`.unmuted`, `isPrivate`) excluding only MeetingScribe's own PIDs. Private aggregate "MeetingScribe Tap" whose
main sub-device is the user's real default output, tap as a drift-compensated sub-tap (`:432-454`). Output
locked int16 / 48 kHz / stereo, resampled in the IO callback (`:476-489`). Rebuild on default-output change,
rate change, and a 10 s all-zero watchdog (`:747-808`); helper crash → restart once with wall-clock silence
fill (`audio_recorder.py:474-511`). Phase 0 landed in `94c3d42`; BlackHole is the fallback.

**No per-app filtering exists** — Slack, Mail, music, every browser tab lands on `system.wav`
(`COREAUDIO_TAP_CAPTURE_PRD.md:102-106`). **No AEC anywhere**; VPIO rejected twice with reasons that still
hold (`COREAUDIO_TAP_CAPTURE_PRD.md:58-62`; `ATTRIBUTION_AND_ROADMAP_PRD.md:219-235`).

### 2.2 Clocks

**Not on a shared clock.** Mic = PortAudio stream on the input device; system = tap aggregate clocked by the
output device. Each thread stamps `started_at = perf_counter()` (`audio_recorder.py:227`); `stop()` writes
per-track `start_offset` (`:918-932`). Nothing corrects drift (`audio_recorder.py:501-504`;
`apple_syscap.swift:735-736`). The Phase-0 "zero drift" number is tap-vs-BlackHole on the *same* system audio
(`tools/ab_capture.py:151-205`) — **mic-vs-system drift has never been measured in this repo.**

### 2.3 Echo (`drop_echo`, `pipeline.py:1120-1197`)

Text-only, runs after ASR and before labelling, whenever **both** tracks carry speech (evidence-gated, not
mode-gated — `pipeline.py:2480-2489`; the addendum's "in-person gets no echo containment" is no longer true).
Per mic segment: system words within ±`ECHO_SLACK_S = 1.5` s → difflib token containment ≥ 0.7 drops; 0.35–0.7
→ character containment gated on ≥ 50% far-end cover (`c7cb561`: mistakes ≤ 0.33, real echo ≥ 0.66, nothing
between) → drop, else edge-trim. **Strictly mic ← system.** Your own voice returning in the system track is
not handled and becomes a phantom "Speaker N" (`DIARIZATION_UPGRADE_PLAN.md:83`, still open).

### 2.4 Then the same clusterer

Online: system track clustered alone with `expected` forced (§1.2). `expected` is the calendar attendee count
whenever the user typed nothing (`app.py:1654-1659`, clamped 1..8), read back at `pipeline.py:2453` with no
source check. The `speaker_count_source` field that shipped is consulted **only** by the mic-only fallback
(`_user_speaker_count`, `pipeline.py:1563-1586`; its own comment: "everywhere else … a calendar guess is a
reasonable prior" — but it is applied as a forced k, not a prior). The harness itself warns: *14 of 33
fixtures shipped with expected_speakers FORCED from the calendar; their 'ship' column is worthless.*

---

## 3. Where it actually fails — ranked by evidence

| # | failure | side | evidence | class |
|---|---|---|---|---|
| 1 | **Attribution is unmeasured.** A word on the wrong person is invisible to every gate. | both | `ATTRIBUTION_GEN1` empty; 0/22 truthed; constant-1 = 81% | metric |
| 2 | **Calendar forces k.** A 3-person Zoom under a 1:1 invite is a monologue; forced k skips all folds, so one confetti cluster becomes a named speaker. | speakers | `pipeline.py:1979`, `:2453`; `diarization.py:638-641`; audit §1: the calendar count *hid* the clustering bug for a month | logic |
| 3 | **Self-echo.** Local voice in the system track (remote on speakers / "original sound" / two remotes in one room) → phantom far-end speaker. | speakers | `DIARIZATION_UPGRADE_PLAN.md:83`; `drop_echo` one-directional | echo |
| 4 | **Far end louder than you in the mic without headphones** (0.4–6.2 dB on 12/14 calls). Residual escapes are word-matcher misses, now mostly caught by the char path; muffled loudspeaker duplicates (49 on one recording) are unmatchable by text. | mic | `ATTRIBUTION_AND_ROADMAP_PRD.md:229-233`; `MULTIPARTY.md:86-96` (envelope correlation r = 0.30 and cross-track ECAPA both measured dead ends) | leak |
| 5 | **Genuine backchanneler folded away** (12.8 s of "mm-hm"; sits inside the phantom band on every signal; the only separating rule was overfit and rejected). | mic (in-person) | `COUNT_ESTIMATION_DESIGN.md:146-172`; `diarization.py:131-157` | identity |
| 6 | **Loudspeaker playback under-counts in auto** (AMI playback-sim 3/4 on every engine; ~6 similar voices → 1). | mic | `MULTIPARTY.md:31-48` | physics |
| 7 | **Non-meeting audio** (Slack/Mail/music) becomes clusters and feeds echo comparison. | speakers | `COREAUDIO_TAP_CAPTURE_PRD.md:286-290`; no mitigation in code | capture |
| 8 | **No identity across meetings for the user**; `"you"` skipped, mic never embedded online. | mic | `voice_profiles.py:571`, `:745-746` | identity |
| 9 | **Clock**: two free-running streams; offset jumps on every rebuild/device switch are unlogged. | both | §2.2 | capture |
| 10 | `FOLD_KEEP_ABOVE_S` bound by single fixtures, one of unknown truth; `AMBIGUITY_MARGIN` never fired; D8 21 ms slicing offset deferred behind `EMBED_VERSION`. | mic | `diarization.py:97-102`, `:461-489`; `voice_profiles.py:100-108` | fragility |

Two things the earlier chat in this session got wrong, corrected here:

- *"Drift smears `drop_echo`'s ±1.5 s match on long calls."* Not by drift: at 50 ppm the window survives ~8 h.
  Built-in mic + built-in speakers share a clock domain (drift ≈ 0); drift appears with USB/Bluetooth mics.
  What breaks the match is **offset jumps** — tap rebuilds, AirPods renegotiation, device switches — which
  today are filled with wall-clock silence and never recorded as resync points.
- *"In-person gets no echo containment."* Fixed since `c7cb561`; `drop_echo` is evidence-gated.

---

## 4. What the field does in 2026 (filtered for "runs on Apple Silicon, no network")

### 4.1 Diarizers

| system | type | max spk | on Apple Silicon | licence | note |
|---|---|---|---|---|---|
| pyannote **community-1** (what you run) | seg + WeSpeaker + VBx | ∞ | FluidAudio CoreML: 10.6% DER AMI-SDM, 323× RTF | CC-BY-4.0 | AMI-IHM 17.0 vs 3.1's 18.8 |
| **LS-EEND** | streaming EEND, online attractors | 8–10 | FluidAudio: ~1 s latency, 100 ms updates, **16/16 correct counts on AMI**, 20.7% DER | Apache-2.0 | can't force a slot; fails enrolment on similar voices |
| **Streaming Sortformer v2.1** | EEND, arrival-order cache | **4** | CoreML ports; 31.7% DER AMI-SDM at 30 s config | NVIDIA Open Model | 41% DER at ≥5 spk; "struggles with overlap" |
| DiariZen | WavLM-L + cVBx | ∞ | torch only | **CC-BY-NC** | best OSS accuracy, commercially blocked |
| sherpa-onnx | seg-3.0 + CAM++ + AHC | ∞ | onnxruntime | Apache-2.0 | pyannote 3.x minus VBx — i.e. a step *back* from community-1 |
| speaker-attributed ASR (multitalker-Parakeet, DiCoW) | joint | — | **none on Mac** | — | watch DiCoW (Whisper + per-frame projection) |

Sources: [community-1](https://huggingface.co/pyannote/speaker-diarization-community-1) ·
[FluidAudio benchmarks](https://github.com/FluidInference/FluidAudio/blob/main/Documentation/Benchmarks.md) ·
[LS-EEND](https://arxiv.org/html/2410.06670v2) · [Sortformer v2.1](https://huggingface.co/nvidia/diar_streaming_sortformer_4spk-v2.1) ·
[DiariZen](https://huggingface.co/BUT-FIT/diarizen-wavlm-large-s80-md-v2) · [Benchmarking diarization models](https://arxiv.org/html/2509.26177v1) ·
[DiCoW](https://arxiv.org/abs/2501.00114).

Independent 196 h benchmark: ~60% of all diarization error is **missed speech from boundary imprecision,
not short segments**; models are "mostly unbiased across segment lengths". Apple ships **no speaker-ID or
diarization API** in macOS 26 or 27 — Core AI (WWDC26) is a runtime, not a model
([WWDC26 324](https://developer.apple.com/videos/play/wwdc2026/324/)).

### 4.2 Clustering state of practice

AHC deliberately under-clustered (thr ≈ 0.6) → **VBx** (Fa 0.07, Fb 0.8) with **short-embedding filtering
before clustering and Hungarian reassignment after** ([BUT, Oct 2025](https://arxiv.org/html/2510.19572)) —
this is precisely the mechanism that prevents "2 detected as 4" (phantoms are spawned by short/overlapped
embeddings) — plus **relative min-cluster-size** `round(0.01 × n_embeddings)` for stride-accelerated
on-device pipelines ([June 2026, tested on M4](https://arxiv.org/abs/2606.08505)). Your GEN3 cascade is a
hand-built approximation of the first half of this (fold the scattered small ones); community-1 already does
the second half inside the Swift helper.

### 4.3 Owner identity and backchannels

No embedder is reliable below ~1 s: ECAPA EER 26.8% @ 0.5 s, 12.8% @ 1 s, 5.4% @ 2 s
([table](https://arxiv.org/pdf/2407.11365)); ERes2NetV2 is the best short-segment embedder that ships as
ONNX (0.98% @ 3 s, 1.48% @ 2 s, Apache-2.0). Enrolment: 20–30 s, ideally two sessions. Calibration: AS-norm +
duration QMF so a 0.5 s burst needs a higher raw cosine. **Attribute sub-second backchannels through the
diarizer's frame-level slot (EEND/community-1) or a personal-VAD head — never by embedding them.** Even human
annotators attributed a 0.41 s "hmm" correctly only 2 times in 10
([Kili](https://kili-technology.com/blog/speaker-diarization-models-guide-benchmarks-and-failure-modes-2026)).

Shipping thresholds for comparison: OpenWhispr CAM++ ≥ 0.70 accept / 0.55–0.70 suggest, 0.8 s minimum
segment ([OpenWhispr](https://openwhispr.com/blog/local-speaker-diarization)). Your 0.45 cosine-distance
gate (= 0.55 similarity) is in the same band.

### 4.4 Capture: one clock, echo reference, contamination

- **One aggregate, one clock**: output device as main/clock, mic as sub-device with
  `kAudioSubDeviceDriftCompensationKey`, tap with `kAudioSubTapDriftCompensationKey`; one IOProc, one
  `AudioTimeStamp` ([Apple](https://support.apple.com/guide/audio-midi-setup/set-aggregate-device-settings-ams094c7edb4/mac),
  [Rogue Amoeba](https://rogueamoeba.com/support/knowledgebase/?showArticle=Loopback-AggregateDeviceHandling)).
  Caveats: aggregate with an input device needs mic TCC too; don't stack VPIO in the same aggregate. **None of
  the surveyed OSS notetakers (Recap, Quill, Hyprnote, meeting-transcriber, Screenpipe) implement drift
  correction** — they store start offsets like you do.
- **Silent-tap bug** (macOS 26.5 beta, [forum 825780](https://developer.apple.com/forums/thread/825780)): only a
  full tap+aggregate teardown recovers; your watchdog already does this (`apple_syscap.swift:747-762`).
- **WebRTC AEC3** standalone ([tonarino crate](https://github.com/tonarino/webrtc-audio-processing),
  [pywebrtc-audio](https://github.com/strands-labs/pywebrtc-audio)): ~510 ms default delay coverage, ≈1% of a
  core, re-converges over seconds after every offset jump — which is why it wants the shared clock first. With
  the tap as reference the acoustic path is short and constant; the network latency is upstream of the
  reference and irrelevant. AEC artefacts degrade speaker verification
  ([ARADIGIT study](https://www.researchgate.net/publication/269650830_Impact_of_Acoustic_Echo_Cancellation_on_Speaker_Verification_in_Mobile_Environment)):
  **embed from raw, use AEC output only to decide.**
- **Attribute, don't cancel** (no-headphones): magnitude-squared coherence between aligned mic and reference
  is level-invariant — it does not care that the far end is 6 dB louder — and is the canonical double-talk
  detector ([Tashev](https://www.microsoft.com/en-us/research/wp-content/uploads/2016/02/Tashev_ET11_DoubleTalkDetector_final.pdf)).
  **Caveat from your own data:** envelope correlation (r = 0.30 on bleed) was measured dead on loudspeaker
  playback; coherence is a different statistic (per-band, lag-aligned, level-free) but it is untested on
  this corpus. Treat as a hypothesis with a gate, not a plan.
- **Contamination**: bundle-ID exclude list on the global tap (Screenpipe pattern) — but macOS 12+ plays
  app notifications from a *system* process, so Slack dings can't be excluded by PID; gate them by content
  (Apple `SNClassifySoundRequest` built-in classifier, no model to ship; <0.7 s + low speech-prob rule).
- **Far-end names without an SDK**: Granola reads Zoom's active-speaker indicator via Accessibility and
  Meet's caption DOM via an extension ([Granola](https://docs.granola.ai/help-center/taking-notes/speaker-attribution-zoom)).
  The roadmap rejected this for TCC cost and maintenance (`ATTRIBUTION_AND_ROADMAP_PRD.md:159-175`); that
  verdict stands. Zoom's "separate audio file per participant" local recording is a **labelling import**, not
  a capture path.

### 4.5 Metrics

Primary: **tcpWER** (MeetEval, `pip install meeteval`) — the only number that measures what the user reads.
Secondary: DER/JER (pyannote.metrics, collar 0.25 s, overlap scored), speaker-count error, owner-turn miss/FA
**binned by duration** (0.3–1, 1–2, 2–5, >5 s — the bin that will move). Label by **correcting machine
output**, never from scratch (~1% vs 15.6% inter-annotator DER). `tools/label_turns.py` already does exactly
this (dispute-first, seeded 10% audit of auto-accepted words as the error bar).

---

## 5. What should happen — ordered, with gates

Each step is independent of the next except where stated. Value ÷ risk descending.

### 5.1 Measure the deliverable (founder, ~2 h total, unblocks everything)

Run `tools/label_turns.py` on Demo, Room P, Room Q, Room T → `truth_words.json`. Add tcpWER and turn-level
accuracy to `eval_diarization.py` (`tools/attribution.py` scores it already). **Gate:** the harness prints an
attribution number for ≥ 4 fixtures; every later change reports its delta.

Then the two recordings the corpus has never had: one 4+ person online call, one genuine multi-party room
(the PRD's Phase 2 item 6). Both are unlabelled founder time; the harness is blind to truth ≥ 3 otherwise.

### 5.2 Calendar → prior — **SHIPPED 2026-08-21**

The calendar's guess now lives under `meta["speaker_count_hint"]` (`app.py`, and the native record form sends
it under that key — it used to send attendees as `expected_speakers`, re-forcing the count from the client).
`diarization.cluster(max_speakers=…)` applies it on the auto path only, after the fold cascade: surplus
clusters fold smallest-first into their nearest voice, but a cluster carrying `HINT_OVERRIDE_S` (= 150 s) of
speech is kept whatever the invite said. `expected_speakers` is now only ever a number a person typed and is
still forced literally. **Not migrated:** older `meeting.json` files hold the calendar guess in
`expected_speakers` with no source stamp, indistinguishable from a typed count; they keep forced behaviour
until an Auto recluster clears it. **Gate met:** `eval_diarization.py --check` exit 0 (21/21, 10/11, golden
OK — the harness replays the auto path, which this does not touch), `tools/test_diarization.py` 26/26 with a
new check covering: hint above truth manufactures nothing; hint below truth caps; ≥ 150 s voices override;
typed count still forces.

### 5.3 One clock + resync anchors (1–2 days in `apple_syscap.swift` + `audio_recorder.py`)

Add the physical mic as a drift-compensated sub-device of the existing tap aggregate; one IOProc emits both
channels; `mic.wav` and `system.wav` stay separate files. Write every `device_changed`/`rebuilt` event with its
host-time and frame index into `meeting.json` so the pipeline can re-anchor `drop_echo`'s window per span.
Requires mic TCC on the aggregate (already granted). BT/HFP: keep today's `IOProcStreamUsage` guard so a
headset mic never forces the SCO downgrade of the output. **Gate:** `tools/ab_capture.py` extended to mic-vs-
system lag on a ≥ 20 min session with a USB or Bluetooth mic (the case where drift exists); eval invariance.

### 5.4 Symmetric echo — **SHIPPED 2026-08-21**

`pipeline.drop_self_echo(system, mic)` runs after `drop_echo`, on the mic segments it kept: a far-end
segment of ≥ 4 words whose word containment in the surrounding mic words is ≥ 0.7 **and** whose median
matched-word lag is in [0.15, 0.7] s is dropped. The gates were measured, not chosen (constants' comment in
`pipeline.py`): over the 20 saved meetings with both tracks, one call carries real self-echo — 10 far-end
segments of 6–92 words at 0.33–0.41 s lag, all of which shipped as a phantom far-end speaker; the far end
*replying* in the user's words matches at 0.97–1.21 s; leak residue sits at lag ≤ 0.01 s; a 600 s decoy
scores zero. Replay of the shipped rule: 10 drops live, 0 decoy, all 10 on that one call. Both lag edges are
set by a single call each — re-measure when another self-echo recording lands. Unit tests in
`tools/test_pipeline.py`; `eval_diarization.py --check` unchanged (its echo section replays `drop_echo`
only). The acoustic confirmation (GCC-PHAT at positive lag) still waits on 5.3.

### 5.5 Enroll "You" — **SHIPPED 2026-08-21**

Online (not the fallback), the mic is now embedded once per meeting and written to `analysis.npz` as
`mic_windows`/`mic_embeddings`; nothing on it is clustered. `process_meeting` enrolls the **owner profile**
(`voice_profiles.enroll_owner_from_state`, one profile flagged `owner`, shown as "You") from a *healthy* call
only — no echo warning fired, so nothing of the far end is in those windows — under the usual
`ENROLL_MIN_WINDOW_S`, `MAX_SAMPLES_PER_PROFILE` and `SAMPLE_WEIGHT_CAP_S`; it is forgotten with the meeting
and deletable from the Voices list. On **in-person and mic-fallback** meetings `pipeline.pin_owner` relabels
the mic cluster nearest the print as `"you"` (`voice_profiles.recognize_owner`: ≥ `MATCH_MIN_WINDOW_S`,
distance ≤ `voice_profile_threshold`, runner-up cluster ≥ `AMBIGUITY_MARGIN` further — a same-voice split pins
nobody). The owner is excluded from far-end recognition. A pinned fallback drops the `mic_fallback` marker
and gets `MIC_ONLY_WARNING_PINNED`. `EMBED_VERSION` unchanged. **Gate met:** `--check` exit 0; voice-profile
suite 16/16; diarization suite 28/28 (in-person four-voices: voice A → "you" on 100 % of windows, nobody is
"you" without a print); end-to-end demo reprocess into an isolated store enrolls the owner with the golden
speaker map unchanged. Still owed: Room P/Q/T attribution truth (5.1) to measure the pin on real rooms.

### 5.6 Backchannels: test the engine you already have before any constant (half a day + one recording)

`synthetic-two-backchannel` has **no audio** (`test/synthetic/README.md:39`), so `fluid-diarizer` has never
seen the case. Record a 3-minute real two-person clip where one person only says "mm-hm"/"right"; run
`score_ab.py --mode offline`. If community-1 finds the second slot (its frame-level powerset is built for
exactly this), the fix is in `_neural_refine`'s raise rule — lower `CONFETTI_MIN_S` for a slot whose
accumulated speech embeds ≥ 0.85 from the leader — not in `FOLD_*`. If it does not, the honest answer is a
personal-VAD head on the You print (§4.3), which is a training project, and it goes on the list after 5.7.

### 5.7 Live in-person labels (1 week, Swift)

LS-EEND via FluidAudio for the live caption ribbon in **in-person mode only** (online stays You/Them by
track). ~1 s latency, 100 ms updates, count-correct on AMI. The batch pass after Stop remains canonical, so a
wrong live label never reaches the transcript. **Gate:** live label agreement with the batch hybrid on Room
T-class clips ≥ 90% by duration.

### 5.8 Leak attribution for the no-headphones case (experiment, gated)

After 5.3: per mic segment compute MSC (300–3400 Hz) and GCC-PHAT lag (0–80 ms) against the system track;
label "leak" when coherence is high at a short lag. Embed nothing labelled leak. **Gate:** the 49 muffled
duplicates on the Sidemen recording; if coherence separates them from the user's bookends where envelope
correlation (r = 0.30) did not, ship; if not, the product answer stays "mute or headphones" and the detector
is deleted.

### 5.9 Contamination (small, after 5.3)

Bundle-ID exclude list (Music, Spotify, Slack, Mail) as an opt-in setting; Apple SoundAnalysis built-in
classifier on system windows; drop windows < 0.7 s with low speech probability. **Gate:** synthetic fixture
with injected chimes; eval invariance.

---

## 6. Explicitly not doing, and why

| proposal | why not |
|---|---|
| Replace GEN3 with a neural counter | measured: neural-auto 19/21 best, Room T under-count unrepairable; hybrid already takes neural turns and upward counts |
| sherpa-onnx as main engine | it is pyannote 3.x minus VBx; community-1 via FluidAudio is strictly newer and already shipped |
| VPIO / voice-processing on the mic | forfeits the raw track, conflicts with the meeting app's VP, AGC/NS out-of-domain for ECAPA; dropped echo is already invisible to the embedder |
| Another `FOLD_*` constant for backchannels | the only passing rule was fitted to 0.8 s and 0.013 gaps, one fixture each; rejected on purpose |
| DiariZen | CC-BY-NC weights |
| Sortformer for rooms | 4-speaker cap, 41% DER at ≥ 5; fine for a 1:1 but the hybrid already handles that |
| AX / DOM active-speaker scraping | roadmap §3.1 stands: roster empty on 21/22 fixtures, two invasive TCC grants, per-client maintenance |
| Fix the 21 ms slicing offset now | flips 3/9 counts; land it with the next `EMBED_VERSION` wave, measured against 5.1's metric, which is the first time it can be judged on attribution rather than counts |

---

## 7. Open questions only a recording can answer

1. Mic-vs-system drift on *this* hardware with a Bluetooth mic (5.3 gate) — never measured.
2. Does community-1 find a backchannel-only speaker on real audio (5.6)? The synthetic corpus cannot say.
3. Does coherence beat envelope correlation on loudspeaker bleed (5.8)? The measured dead end was a different statistic.
4. What is the attribution error of the shipping hybrid on Demo/Room P/Q/T (5.1)? Everything else is downstream of this number.
