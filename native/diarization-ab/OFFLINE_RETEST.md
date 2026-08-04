# The offline re-test CORRECTION.md required — run 2026-08-03

**Outcome: the offline pipeline is adopted for ATTRIBUTION, not for counting.**
The hybrid that shipped: `diarization.cluster()`'s GEN3 cascade keeps deciding
HOW MANY voices a track holds; FluidAudio's offline community-1 pipeline
(`native/fluiddiarizer`, `diarization_neural.py`), forced to that count,
decides WHO SPEAKS WHEN. Config `diarization_engine`: `"auto"` (hybrid,
default) or `"classic"`.

Every requirement CORRECTION.md § "What a fair re-test would require" listed
was met except reference-timeline DER (item 4, still blocked on labels — see
below):

1. **The offline pipeline was actually called.** `FluidDiarize.swift` gained
   `--mode offline` (default) running `OfflineDiarizerManager` at the vendor's
   `.community` presets; the streaming path survives only behind
   `--mode streaming` for reproducing the correction.
2. **Models pinned and local.** Same pinned revision
   `5390df9752c8fc583596018360c5fd70d6fa6c75`; CoreML bundles cached locally
   (~22 MB); production runs pass an explicit `--models-dir` under
   `<DATA_DIR>/models/fluid-diarization`.
3. **The threshold was swept, not point-sampled.** 0.5 / 0.6 / 0.7 / 0.8 over
   every real fixture with a resolvable WAV (`score_ab.py --mode offline
   --threshold T --tag offline-tT --keep-segments`; full grids in
   `results/fluidaudio-offline-t*.json`).

## The grid (auto count vs truth, 21 truth-backed fixtures)

| threshold | correct | discriminating (Demo, Room P/Q/T) | misses |
|---|---|---|---|
| 0.5 | 19/21 | 3/4 | Call H +1 (1.7 s confetti), Room T −1 |
| 0.6 | 17/21 | 3/4 | Call A/D/H +1, Room T −1 |
| 0.7 | 18/21 | 3/4 | Call A +1 (1.9 s), Call H +1 (1.7 s), Room T −1 |
| 0.8 | 18/21 | 3/4 | Call A +1, Call H +1, Room T −1 |
| GEN3 (shipping) | **21/21** | **4/4** | — |
| constant "1" | 17/21 | 1/4 | — |

RTF 150–410x on this M4 (results carry per-fixture numbers). Room W (truth
UNKNOWN): 2 clusters at t0.5, 15 at t0.7, 19 at t0.8, against GEN3's 7 — the
low-threshold end over-merges exactly where an open floor needs splitting, so
t0.5's 19/21 is not the best cell, just the one this 1:1-heavy corpus rewards.

## What changed since the streaming A/B, and what did not

* **Demo = 2 at every threshold.** The single disqualifying error of the
  streaming run (two genuine voices collapsed to one, unrepairable
  downstream) is GONE on the offline path. Room P and Room Q are correct at
  every threshold too.
* **The remaining over-counts are confetti** — 1.7–1.9 s clusters at
  fragmentation 1.0, the class the shipped fold cascade removes — not voice
  fractures.
* **Room T (two people in one room, 104 s) under-counts at every threshold.**
  The classic engine separates them; community-1's VBx merges them. An
  under-count cannot be repaired by post-processing, so the neural pipeline
  cannot be trusted with the COUNT on this corpus.
* **Forced to the right count it attributes well**: `--num-speakers 2` splits
  Room T 10.9 s / 27.6 s across 15 turns and matches the classic engine on
  95% of Demo's windows (the 5% are turn boundaries, where frame-level
  segmentation is the better witness).

Hence the hybrid: counts stay where 21/21 lives; turns move to the engine
built for who-spoke-when. The count path is provably untouched —
`tools/eval_diarization.py --check` reproduces GEN3 (21/21, 4/4
discriminating, 33/33 baseline) after the change.

## Still owed (unchanged from CORRECTION.md item 4)

Turn-level attribution truth for the 4 discriminating fixtures. Until those
labels exist, "the neural turns attribute better" rests on the Demo window
agreement, the engine's published DER on meeting corpora, and construction
(frame-level powerset vs 2 s windows) — not on a measured DER for THIS
corpus. The labelling remains founder work.
