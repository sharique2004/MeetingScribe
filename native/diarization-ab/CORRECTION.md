# CORRECTION to commit 417ef04

**Status of the decision: UNCHANGED.** The native neural stack is not adopted.
MeetingScribe keeps the Python ECAPA + clustering diarizer.

**Status of the reasons: TWO OF THEM ARE WRONG**, and a third framing problem
makes the headline scoreboard misleading. This file corrects the record so the
commit message is not cited later as if it established findings it did not
establish.

Commit under correction: `417ef04` "Native diarization A/B: FluidAudio and
SpeakerKit both fail the GEN3 gate".

Nothing in the A/B was re-run to produce this file. Every number below is read
out of the artifacts that commit and that run already left on disk, or out of
the vendored source at the pinned revision. No A/B source was modified.

---

## Summary of the correction

| # | What the commit says | What is actually true |
|---|---|---|
| a | FluidAudio was evaluated on its offline pyannote pipeline | It was evaluated on the **streaming/online** `DiarizerManager` at default config. The offline pipeline exists in the pinned checkout and was **never called**. |
| b | The engines "fracture one far-side voice", "splits the single remote speaker" | On all 8 one-to-one misses the leading voice keeps **94.0% to 99.9%** of speech. The surplus label is **1.0 s to 35.7 s of micro-fragments**, not a split voice. |
| c | "FluidAudio 11/21, SpeakerKit 10/21, GEN3 21/21" presented as a meaningful ranking | The gate is degenerate: a constant predictor that always answers "1" scores **17/21 = 81.0%**. Both neural candidates score **below the constant**. The scoreboard rewards saying "1", not diarizing. |

Item (c) does not rescue the candidates. It cuts the other way: on the only
fixtures where the metric carries information, FluidAudio makes exactly one
error that post-processing cannot repair, and that single error is the whole
honest case against it.

---

## (a) The wrong pipeline was benchmarked

### The call site

`FluidDiarize/Sources/fluid-diarize/FluidDiarize.swift`, lines 68 to 82:

```swift
68:        var config = DiarizerConfig()
69:        if let numSpeakers { config.numClusters = numSpeakers }
70:        if let threshold { config.clusteringThreshold = threshold }
...
73:            let manager = DiarizerManager(config: config)
75:            let models = try await DiarizerModels.downloadIfNeeded(to: dir)
...
82:            let result = try manager.performCompleteDiarization(samples, sampleRate: 16000)
```

`DiarizerManager` + `performCompleteDiarization` is FluidAudio's real-time
chunked path. The vendor's own module map calls
`DiarizerManager.swift` the "Real-time orchestrator and chunk scheduler"
(`Documentation/Diarization/GettingStarted.md:77`), and the vendor's engine
selection guide opens its entry with "Legacy online diarizer"
(`GettingStarted.md:13`).

The A/B ran that path with **no arguments**, so every default applied
(`Sources/FluidAudio/Diarizer/Core/DiarizerTypes.swift:7-58`):

| Knob | Default used by the A/B |
|---|---|
| `clusteringThreshold` | 0.7 |
| `chunkDuration` | 10.0 s |
| `chunkOverlap` | 0.0 s |
| `minSpeechDuration` | 1.0 s |
| `numClusters` | -1 (auto) |

The wrapper's own header comment, lines 3 to 4 of the same file, states that it
"runs FluidAudio's offline diarization pipeline". That comment is false. It is
almost certainly where the mislabeling entered the commit message.

### The offline pipeline was present and unused

At the pinned revision `5390df9752c8fc583596018360c5fd70d6fa6c75`
(`FluidDiarize/Sources/fluid-diarize/FluidDiarize.swift:26`, matching
`FluidDiarize/Package.resolved`), the vendored checkout at
`FluidDiarize/.build/checkouts/FluidAudio/` contains a complete second
pipeline:

```
Sources/FluidAudio/Diarizer/Offline/Core/OfflineDiarizerManager.swift
Sources/FluidAudio/Diarizer/Offline/Core/OfflineDiarizerTypes.swift
Sources/FluidAudio/Diarizer/Offline/Core/OfflineDiarizerModels.swift
Sources/FluidAudio/Diarizer/Offline/Segmentation/OfflineSegmentationProcessor.swift
Sources/FluidAudio/Diarizer/Offline/Extraction/OfflineEmbeddingExtractor.swift
Sources/FluidAudio/Diarizer/Offline/Extraction/PLDATransform.swift
Sources/FluidAudio/Diarizer/Offline/Clustering/VBxClustering.swift
Sources/FluidAudio/Diarizer/Offline/Clustering/AHCClustering.swift
Sources/FluidAudio/Diarizer/Offline/Utils/OfflineReconstruction.swift
```

Public entry points: `OfflineDiarizerManager.process(...)` at
`OfflineDiarizerManager.swift:94`, `:112`, `:135`; `prepare(...)` at `:153`,
`:165`; `cluster(...)` at `:270`. Model loading is
`OfflineDiarizerModels.load(from:configuration:progressHandler:)` at
`OfflineDiarizerModels.swift:74`, which takes a directory and reads from it.

Verification that it was never invoked:

```
$ grep -rn "Offline" FluidDiarize/Sources/ SpeakerKitDiarize/Sources/ score_ab.py
(none)
```

### What the vendor's own benchmarks say about the two paths

`FluidAudio/Documentation/Benchmarks.md`, section "Speaker Diarization"
(line 581 onward). Both paths use the same community-1 model
(`Benchmarks.md:583`).

**Offline** (`Benchmarks.md:611-642`), AMI SDM 16-meeting official split, M5
Pro, `--mode offline --threshold 0.7`:

```
AVERAGE          10.6 DER%   17.4 JER%    5.4 Miss%   2.0 FA%   3.3 SE%   323.2 RTFx
```

The vendor adds that 12 of 16 meetings get the speaker count right and that
average DER "matches published pyannote-community-1 offline numbers on this
split" (`Benchmarks.md:640`).

**Streaming** (`Benchmarks.md:644-646`), the path the A/B actually ran. The
vendor's own guidance for it: "Only use this when you critically need realtime
streaming speaker diarization." (`Benchmarks.md:646`)

The A/B's exact default config, 10 s chunks / 0 s overlap / threshold 0.7, has
a published table at `Benchmarks.md:686-712`, labeled by the vendor as the
"best streaming configuration found on this split":

```
AVERAGE          38.2 DER%   46.8 JER%    7.7 Miss%   1.5 FA%   29.0 SE%   207.5 RTFx
```

So on the vendor's own split, the configuration the A/B benchmarked has **3.6x
the DER of the offline pipeline** (38.2% vs 10.6%), and the gap is almost
entirely speaker error (29.0% SE vs 3.3% SE), which is exactly the failure
class the count metric measures.

The count column of that same streaming table is the decisive detail:

| Detected / truth | Meetings |
|---|---|
| correct | 5 of 16 (ES2004a, TS3003b, TS3003c, IS1009c, TS3003d) |
| over-count | 11 of 16 |
| under-count | 0 of 16 |

Every one of the 11 errors in the vendor's published table for this exact
config is an **over-count**. The A/B then observed 8 over-counts on 12
one-to-one calls and attributed them to the model family. They are the
documented, expected behaviour of the streaming chunker at its default
threshold. The A/B did not discover a property of pyannote-family
segmentation. It re-measured a known-degraded mode of one wrapper.

Note also `Benchmarks.md:642`: the offline path's clustering threshold is
consequential on meeting audio, and the vendor recommends 0.7 for AMI-SDM-like
material because the community-1 default of 0.6 merges too aggressively. The
A/B swept nothing.

---

## (b) The "fractured voice" mechanism is contradicted by the run's own data

The commit says FluidAudio "splits the single remote speaker on 8 of 12 online
1:1 calls" and that the shared segmentation "consistently fractures one
far-side voice".

The count is right: 8 of the 12 `Call *` fixtures over-count. The mechanism is
wrong. Measured from `results/segments/fluidaudio/*.json`, summing segment
durations per emitted speaker label:

| Fixture | truth | count | lead share | 2nd share | 2nd total | 2nd segs | 2nd median | 2nd longest |
|---|---|---|---|---|---|---|---|---|
| Call A | 1 | 2 | **94.0%** | 6.0% | 13.2 s | 7 | 1.86 s | 2.80 s |
| Call B | 1 | 2 | **98.1%** | 1.9% | 35.7 s | 18 | 1.52 s | 7.10 s |
| Call D | 1 | 2 | **97.5%** | 2.5% | 20.8 s | 12 | 1.59 s | 3.14 s |
| Call F | 1 | 2 | **99.7%** | 0.3% | 3.9 s | 2 | 2.01 s | 2.01 s |
| Call G | 1 | 2 | **99.4%** | 0.6% | 2.9 s | 2 | 1.60 s | 1.60 s |
| Call H | 1 | 2 | **99.9%** | 0.1% | 2.4 s | 2 | 1.32 s | 1.32 s |
| Call K | 1 | 2 | **99.5%** | 0.5% | 4.4 s | 2 | 2.26 s | 2.26 s |
| Call L | 1 | 2 | **99.3%** | 0.7% | 1.0 s | 1 | 1.05 s | 1.05 s |

Leading-voice share across the 8 misses: **94.0% to 99.9%**. Across all 8
fixtures combined the surplus label accounts for **46 segments totalling
84.3 s**, and the single longest surplus segment anywhere is **7.10 s**.

Three consequences.

1. **It is not a split.** A fractured voice would produce a roughly balanced
   two-way division. What the data shows is one dominant label plus a
   confetti of 1 to 2 second blips. Every one of the 8 misses is exactly
   count 2, never 3. The commit's picture of a voice being torn in half does
   not appear anywhere in the corpus.

2. **The blips are chunk-local, which is a streaming signature.** The surplus
   segments in each fixture land in a handful of disjoint 10 s chunks, with
   almost no runs: Call B's 18 surplus segments are spread across 14 distinct
   chunks in 11 separate runs; Call F, G, H and K each place their 2 surplus
   segments in 2 chunks that are hundreds of seconds apart. That is the
   running speaker database occasionally exceeding its 0.7 threshold on a
   single chunk, not a segmentation model disagreeing about who is talking.
   It reinforces (a).

3. **Merge-only post-processing repairs almost all of it.** The app's own fold
   gate keeps clusters above `FOLD_KEEP_ABOVE_S = 150.0` seconds
   (`diarization.py:93`). Every surplus label in the table above is between
   1.0 s and 35.7 s, at least 4x under that ceiling. Taking FluidAudio's full
   error list:

   | | FluidAudio | SpeakerKit |
   |---|---|---|
   | over-counts | 9 | 11 |
   | under-counts | **1 (Demo)** | 0 |
   | largest surplus mass on any fixture | 35.7 s (Call B) | 49.4 s (Call B) |

   FluidAudio's 9 over-counts are the 8 above plus Room Q, whose third label
   holds 8.4 s, **0.6%** of 1350 s of speech. All 9 sit far below the fold
   ceiling. The single error that no merge pass can ever repair is **Demo**,
   where two genuinely distinct voices were collapsed to one label holding
   100.0% of 36 s of speech.

So the real, defensible finding is the opposite shape from the one recorded:
the over-counting is cosmetic and cheaply fixable, and the disqualifying defect
is a lone under-count.

---

## (c) The gate that produced the headline numbers is degenerate

Truth distribution over the corpus, read from `results/fluidaudio.json` key
`rows`:

| truth | fixtures |
|---|---|
| 1 | 17 |
| 2 | 4 (Demo, Room P, Room Q, Room T) |
| >= 3 | 0 |
| unknown | 1 (Room W, excluded from scoring) |

Scoreboard, recomputed from the committed JSON:

| Predictor | Score | % |
|---|---|---|
| GEN3 (shipping) | 21/21 | 100.0 |
| **constant "1"** | **17/21** | **81.0** |
| FluidAudio | 11/21 | 52.4 |
| SpeakerKit | 10/21 | 47.6 |

Both neural candidates score below a predictor that ignores the audio entirely.
Reporting "11/21" and "10/21" against "21/21" without the 81.0% floor overstates
what the gate discriminates by roughly a factor of five: GEN3's real margin over
doing nothing is **four fixtures**, not ten.

On those four discriminating fixtures:

| Fixture | track | truth | GEN3 | FluidAudio | SpeakerKit |
|---|---|---|---|---|---|
| Demo | system | 2 | 2 | **1** | 2 |
| Room P | mic | 2 | 2 | 2 | 2 |
| Room Q | mic | 2 | 2 | **3** | **3** |
| Room T | mic | 2 | 2 | 2 | **5** |
| | | | **4/4** | **2/4** | **2/4** |

The load-bearing cell is Demo. FluidAudio emitted a single label covering
100.0% of the speech on a fixture with two genuinely distinct voices. That is
an under-count, and under-counts are structurally unrepairable downstream: a
merge-only post-processor can remove speakers, never invent one. Room Q's
extra label is 0.6% of speech and would fold. Every `Call *` surplus would
fold. Demo would not.

A secondary and non-load-bearing note on the commit's speed figures. Recomputed
from the per-fixture `elapsed_seconds` in the committed JSON
(`audio_seconds / elapsed_seconds`, the same formula as `score_ab.py:188`):

| Engine | per-fixture RTFx | median | aggregate |
|---|---|---|---|
| FluidAudio | 67x to 620x | 230x | 286x |
| SpeakerKit | 90x to 484x | 420x | 419x |

The commit's "~95-310x" for FluidAudio and "~430x" for SpeakerKit do not
reproduce from the committed data. This changes nothing, since speed was never
the deciding factor, but the ranges as written are not supported by the file.

---

## Corrected verdict

**Do not swap to the native neural stack.** The decision recorded in `417ef04`
stands, on the following corrected reasoning.

1. What was measured was FluidAudio's **streaming** `DiarizerManager` at stock
   defaults, a path the vendor documents as substantially worse than its
   offline pipeline and recommends only for realtime use. The offline
   `OfflineDiarizerManager` path, which is what published pyannote-community-1
   benchmarks describe, was present in the pinned checkout and never invoked.
   The A/B therefore does **not** license any claim about the pyannote model
   family, about CoreML diarization in general, or about what the native stack
   could achieve if configured correctly.

2. On the evidence that does exist, the single disqualifying result is
   **FluidAudio collapsing Demo to one speaker**, an under-count on a fixture
   with two distinct voices. Its nine over-counts are micro-fragment noise
   (0.1% to 6.0% of speech, 1.0 s to 35.7 s) and would be repaired by the
   existing fold pass. SpeakerKit's failure is different and broader: eleven
   over-counts including Room T at 5 against a truth of 2.

3. Neither candidate cleared the bar for a **swap**, which requires strict
   parity or better against a shipping 21/21. But "failed the gate" is a weak
   statement here, because the gate is degenerate: a constant "1" scores 81.0%,
   and only four fixtures carry any information at all. The A/B as run cannot
   distinguish "this engine is bad" from "this engine was run wrong" from "this
   metric cannot see the difference".

4. Therefore: the correct disposition is **not adopted, not disproven.** The
   native stack is unproven on this corpus, not demonstrated inadequate. The
   commit's framing of a settled negative finding about the model family should
   not be cited.

---

## What a fair re-test would require

If the native stack is ever reconsidered, none of the following is optional. A
re-run that changes only one or two of these is not a fair test and should not
be recorded as one.

### 1. Call the offline pipeline

Replace the `DiarizerManager` / `performCompleteDiarization` call site with
`OfflineDiarizerManager` and `OfflineDiarizerConfig`, using the vendor's
`.community` presets as the baseline:

- `OfflineDiarizerConfig.Segmentation.community`
  (`OfflineDiarizerTypes.swift:46`): 10.0 s window, 16 kHz, stepRatio 0.2.
- `OfflineDiarizerConfig.Embedding.community` (`:100`).
- `OfflineDiarizerConfig.Clustering.community` (`:137`): threshold 0.6,
  Fa 0.07, Fb 0.8.
- `OfflineDiarizerConfig.VBx.community` (`:168`).
- `OfflineDiarizerConfig.PostProcessing.community` (`:191`).

Entry point: `OfflineDiarizerManager.process(...)`
(`OfflineDiarizerManager.swift:94`). The output contract consumed by
`score_ab.py` does not need to change.

Also fix the header comment at `FluidDiarize.swift:3-4`, which currently
claims the offline pipeline is what runs.

### 2. Bundle the weights, run with no network

The current wrapper calls `DiarizerModels.downloadIfNeeded(to: dir)`
(`FluidDiarize.swift:75`), which fetches from HuggingFace on first run. That is
unacceptable for a privacy-first product and it makes the benchmark
non-reproducible, since the fetched revision is not pinned by anything in the
repo.

The offline loader takes an explicit directory:
`OfflineDiarizerModels.load(from: URL, ...)`
(`OfflineDiarizerModels.swift:74`). Stage the CoreML bundles once, pass them
through the existing `--models-dir` flag (`FluidDiarize.swift:51`), and run the
whole sweep with networking disabled so a silent re-download cannot occur.
Record the model bundle hashes alongside the results.

### 3. Sweep the clustering threshold

The A/B ran a single point and reported it as the engine's capability. The
vendor explicitly documents that this knob decides the outcome on meeting
audio: at `Benchmarks.md:642`, moving the offline threshold from 0.6 to 0.7
changes average AMI-SDM DER from 15.5% to 10.6% and fixes four meetings that
had been under-counted.

Minimum sweep: offline `clustering.threshold` over 0.5, 0.6, 0.7, 0.8 on every
fixture. Report the full grid, not the best cell. The `--threshold` flag
already exists (`FluidDiarize.swift:50`); it currently drives
`DiarizerConfig.clusteringThreshold` and would need to drive
`OfflineDiarizerConfig.Clustering.threshold` instead.

If the sweep is skipped, the result is a measurement of one arbitrary constant,
not of the engine.

### 4. Gate on attribution, not on counts

This is the most important item, and it applies to the shipping evaluator too,
not just to any future candidate.

The count metric is degenerate on this corpus. Constant "1" scores 81.0%. Only
4 of 21 fixtures can distinguish any two predictors that both handle the
one-speaker case. Worse, the metric does not constrain the constants it is used
to justify: `FOLD_KEEP_ABOVE_S` (`diarization.py:93`) can be set to 150 or 1000
with identical real-corpus accuracy, and `MAX_AUTO_SPEAKERS`
(`diarization.py:40`) can be anything in [4, 8] with zero real fixtures
changing. A metric that cannot see a 6.7x change in a threshold is not
measuring the threshold.

Counts also hide the asymmetry that actually matters. In section (b), nine
over-counts worth 0.1% to 6.0% of speech and one under-count worth 100% of a
fixture all cost exactly one point each, even though the first nine are
free to fix and the tenth is fatal.

A re-test should score **time-weighted speaker attribution**, and the harness
should be extended to compute it:

- **Primary metric: DER**, or at minimum speaker-confusion time as a fraction
  of speech time, computed against a reference timeline. Standard settings:
  0.25 s collar, overlap ignored, matching the vendor's tables so the numbers
  are comparable to `Benchmarks.md`.
- **Report Miss, False Alarm and Speaker Error separately.** The whole
  correction in section (b) exists because a single scalar count hid the
  difference between 84.3 s of confetti and one collapsed voice.
- **Report over-count and under-count separately**, and weight under-counts
  heavily, since merge-only post-processing can repair the former and never the
  latter.
- **Report the constant-"1" baseline on every scoreboard**, alongside the
  candidate and GEN3, so a below-floor result is visible immediately rather
  than buried.

This requires reference timelines, which the corpus does not currently have:
truth is stored as a single integer per fixture. Producing them is real work,
and the honest cost of a fair re-test is dominated by that labelling, not by
the Swift changes. Until it exists, no diarization A/B on this corpus can
support a confident verdict in either direction, including this one.

A reasonable scope reduction: label reference timelines for the 4 discriminating
fixtures plus Demo-like short clips first, since those are the only fixtures
that carry information under the current metric anyway.

---

## Reproducing the numbers in this file

The per-fixture segment dumps that sections (b) and (c) rely on are at
`results/segments/{fluidaudio,speakerkit}/*.json`. These are **gitignored**
(`.gitignore:11`), so they exist only in the working tree of the machine that
ran the A/B. If they are lost, the evidence for section (b) is not recoverable
from the repository, only the aggregate counts in `results/*.json` survive.

Shares, surplus mass and chunk placement:

```python
import json, collections, pathlib
base = pathlib.Path("native/diarization-ab")
rows = json.loads((base / "results/fluidaudio.json").read_text())["rows"]
for name, row in sorted(rows.items()):
    d = json.loads((base / f"results/segments/fluidaudio/{name}.json").read_text())
    dur = collections.defaultdict(float)
    for s in d["segments"]:
        dur[s["speaker"]] += s["end"] - s["start"]
    total = sum(dur.values())
    order = sorted(dur.values(), reverse=True)
    lead = order[0] / total * 100
    surplus = sum(order[row["truth"]:]) if row["truth"] else 0.0
    print(f"{name:8s} truth={row['truth']} count={row['count']} "
          f"gen3={row['gen3']} lead={lead:5.1f}% surplus={surplus:6.1f}s")
```

Scoreboard and the constant baseline:

```python
scorable = [r for r in rows.values() if r["truth"] is not None]
print("n            ", len(scorable))                                    # 21
print("constant '1' ", sum(r["truth"] == 1 for r in scorable))           # 17
print("fluidaudio   ", sum(r["count"] == r["truth"] for r in scorable))  # 11
print("gen3         ", sum(r["gen3"]  == r["truth"] for r in scorable))  # 21
```

Vendor claims quoted above are in the pinned checkout at
`FluidDiarize/.build/checkouts/FluidAudio/`, revision
`5390df9752c8fc583596018360c5fd70d6fa6c75`, Apache License 2.0. Specific lines:
`Documentation/Benchmarks.md:583, 611-642, 644-646, 686-712` and
`Documentation/Diarization/GettingStarted.md:13, 15, 77, 194`.
