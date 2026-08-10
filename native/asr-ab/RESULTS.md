# Parakeet INT8 evaluation — RESULTS (2026-08-09)

Measured against `PROTOCOL.md`, which was written and committed **before any
number in this file existed**. Read that first; it defines the arms, the floors,
and the ship rule. This file only reports what happened.

## Verdict in one line

**No candidate ships.** Every arm fails at least two clauses of the §4 ship
rule. The interesting arm is **`coreml-int8`**, which is *more accurate than the
shipping baseline on both reference meetings*, **6.8× faster** and **15× smaller
in RSS** (**68× smaller in phys_footprint**) on the longest fixture — and still
fails, on transcript divergence and word-time agreement.

Two clauses could not be discharged by machine and are **PENDING** (§9).

| arm | 1 WER | 2 divergence | 3 human review | 4 word-time | 5 diarization | 6 speed+memory | ships? |
|---|---|---|---|---|---|---|---|
| `mlx-fp-v3` | **FAIL** | **FAIL** | PENDING | **FAIL** | not measured | **FAIL** | no |
| `mlx-int8` | **FAIL** | **FAIL** | PENDING | **FAIL** | not measured | **FAIL** | no |
| `coreml-int8` | PASS | **FAIL** | PENDING | **FAIL** | not measured | PASS | no |
| `coreml-int4` | **FAIL** | **FAIL** | PENDING | **FAIL** | not measured | PASS | no |

No arm was dropped. The timeboxed `mlx-int8` arm loaded and ran on the first
attempt (§8).

## What was run

24 fixtures × 9.6 hours of audio per arm; 5 arms plus 2 repeat baseline passes
(the floor, and the container-equivalence check of §5) — **7 full passes, 168
fixture-runs, ~67 hours of audio decoded, zero failures and zero skips** in the
final grid. Every row below is a **GATE** row: no arm's table is a subset.

Corpora: AMI `ES2004a` + `IS1009c` (Mix-Headset, references exist) and all 22
real fixtures, resolved to their original audio by the shared timestamp suffix
and keyed by pseudonym.

---

## 1. The two degenerate floors

Both floors are required on every table by PROTOCOL.md §2. Here they are.

### (a) Empty-transcript floor

**WER = 100.00%.** That is what "no transcript at all" scores on the same axis
as every candidate. Every WER in §3 should be read against it.

### (b) Non-determinism floor — measured FIRST, before any candidate ran

`mlx-fp` was run twice over the **entire** corpus (the protocol asked for 3+
fixtures; running all 24 cost 25 minutes and removes any question of fixture
selection). Result:

> ### **Floor = 0.000% divergence and 0 ms word-time p95, on all 24 of 24 fixtures.**

Not "small" — **zero**. Identical text and identical word timings to the last
digit, on every fixture including the 111-minute Room W (12 300 words). The two
passes are genuinely separate executions: Room W took 180.7 s in one and 144.1 s
in the other.

`results/mlx-fp-rerun.json`.

**What this buys, and what it breaks.**

* It buys the strongest possible attribution: the shipping engine is bit-exact
  deterministic on this hardware, so **every divergence reported in §4 is caused
  by the candidate**, with no noise term to argue about. This is a better
  outcome than a small non-zero floor.
* It breaks the ship rule's own interpretation guard. Clause 2 requires
  divergence "≥ 3× the non-determinism floor"; 3 × 0 = 0, so **every arm passes
  that half of the clause trivially and it carries no information.** Recorded
  here as **DEGENERATE — SATISFIED VACUOUSLY**, not as evidence of anything. The
  clause's real work is done by its ≤3.0% ceiling, which every arm fails.

The guard was written to catch a candidate hiding inside baseline noise. There
is no baseline noise. It never fires.

---

## 2. A corpus that changed underneath the experiment

Roughly 90 minutes into the grid, `mlx-fp-v3` failed on 11 of 24 fixtures with
`LibsndfileError: System error` on paths that had existed minutes earlier.

Cause: `audio_archive.py` — another agent's work, landing in this repo during
this run — was converting finished recordings from `<track>.wav` to
`<track>.flac` and deleting the WAVs. `score_asr.py` probes both extensions, but
it had resolved every path **once, up front**, and a path resolved minutes ago is
a cached fact about a mutable directory.

Two things were done, in this order:

1. **Fixed the harness.** `refresh_audio()` re-resolves each fixture's audio
   immediately before it is spawned, never trusting the planning sweep. Each row
   now also records `audio_container`, so a container change is visible in the
   data instead of inferred from a footnote.
2. **Proved the change was inert rather than assuming it.** `audio_archive.py`
   is verified-lossless by construction (it decodes both files and compares
   int16 samples, and deletes the WAV only after re-verifying), but that is the
   converter vouching for itself. So `mlx-fp` was re-run over the FLAC corpus
   and scored against the WAV baseline:

> **24 of 24 fixtures: byte-identical transcript text, identical word timings,
> identical durations. Divergence 0.000%, word-time p95 0 ms.**

`results/mlx-fp-flacw.json` (Room W) plus the transcript comparison over the
other 23. The WAV-era baseline and the FLAC-era candidates are therefore
comparing the same audio, and this is a measurement, not an inference.

A useful side effect: this is a **second, independent sample of the
non-determinism floor**, taken on different input files, and it also came back
0.000%.

---

## 3. Accuracy where references exist (AMI)

Reference: `ami_public_manual_1.6.2` `words/*.xml` (CC-BY), all four speakers
merged in time order, punctuation/`vocalsound`/`disfmarker`/`gap` excluded.
Normalization: lowercase, strip punctuation (`WER`), plus the digit- and
formatting-insensitive variant (`WER norm`) beside it.

| meeting | arm | WER % | WER norm % | CER % | ref words | hyp words | Δ vs mlx-fp |
|---|---|---:|---:|---:|---:|---:|---:|
| ES2004a | `mlx-fp` *(baseline)* | 20.96 | 20.96 | 16.71 | 2801 | 2366 | — |
| ES2004a | `mlx-fp-v3` | 24.63 | 24.63 | 20.78 | 2801 | 2248 | **+3.67** |
| ES2004a | `mlx-int8` | 24.31 | 24.31 | 20.52 | 2801 | 2262 | **+3.35** |
| ES2004a | `coreml-int8` | **19.56** | 19.56 | 15.28 | 2801 | 2409 | **−1.40** |
| ES2004a | `coreml-int4` | 30.99 | 30.99 | 27.03 | 2801 | 2080 | **+10.03** |
| ES2004a | *empty-transcript floor* | 100.00 | 100.00 | 100.00 | | | |
| IS1009c | `mlx-fp` *(baseline)* | 23.93 | 23.46 | 15.55 | 4647 | 3775 | — |
| IS1009c | `mlx-fp-v3` | 15.88 | 15.77 | 10.24 | 4647 | 4172 | −8.05 |
| IS1009c | `mlx-int8` | 15.82 | 15.71 | 10.16 | 4647 | 4176 | −8.11 |
| IS1009c | `coreml-int8` | **15.77** | 15.77 | 10.30 | 4647 | 4230 | −8.16 |
| IS1009c | `coreml-int4` | 21.84 | 21.76 | 15.57 | 4647 | 3864 | −2.09 |
| IS1009c | *empty-transcript floor* | 100.00 | 100.00 | 100.00 | | | |

**Read these numbers with three caveats.**

1. **They are not comparable to published AMI WER tables** (PROTOCOL.md §7).
   They are deletion-dominated: on ES2004a the baseline's errors are 116
   substitutions, **453 deletions**, 18 insertions. AMI's manual transcription
   records backchannels and filler (`Hmm`, `Um`, `Mm-hmm`) that a modern ASR
   deliberately does not emit. That penalty is identical for every arm, so the
   arm-to-arm comparison the ship rule actually gates on is unaffected — but the
   absolute level is an artifact of the reference convention, not a defect rate.
2. **The v2→v3 generation change dominates the quantization change.** That is
   exactly what the `mlx-fp-v3` control arm was for: `mlx-int8` and `mlx-fp-v3`
   land within 0.32 pt of each other on ES2004a and 0.06 pt on IS1009c. Whatever
   is happening to these numbers is v3 being different from v2, **not** INT8
   being lossy. Without that arm the entire delta would have been misattributed
   to quantization.
3. **The two meetings disagree in direction.** Every non-baseline arm is worse on
   ES2004a and better on IS1009c. This is precisely why clause 1 gates on *every*
   reference file rather than the mean: the mean would have passed `mlx-fp-v3`
   and `mlx-int8`, and it would have been wrong to.

---

## 4. Divergence from the shipping arm (real corpus)

No references exist here, so this measures **agreement with what ships**, not
accuracy. Floor for every cell below: **0.000%** (§1b).

| fixture | audio s | `mlx-fp-v3` | `mlx-int8` | `coreml-int8` | `coreml-int4` |
|---|---:|---:|---:|---:|---:|
| Call A | 1489 | 6.32 | 6.59 | 6.99 | 7.93 |
| Call B | 2953 | 6.39 | 6.36 | 4.84 | 8.73 |
| Call C | 1050 | 18.55 | 18.48 | 11.65 | 24.83 |
| Call D | 2876 | 11.45 | 11.82 | 11.45 | 16.53 |
| Call E | 2047 | 5.99 | 5.96 | 5.54 | 9.48 |
| Call F | 1908 | 8.48 | 8.55 | 8.31 | 12.08 |
| Call G | 1207 | 11.74 | 11.62 | 11.51 | 19.98 |
| Call H | 2716 | 6.89 | 6.94 | 6.04 | 7.64 |
| Call J | 23 | 22.03 | 22.03 | 20.34 | 20.34 |
| Call K | 1620 | 11.21 | 9.74 | 10.13 | 19.20 |
| Call L | 624 | 15.09 | 11.99 | 9.09 | 13.54 |
| Call N | 50 | 2.00 | 2.00 | 0.00 | 10.00 |
| Demo | 49 | 0.00 | 0.00 | 0.00 | 0.00 |
| E2E | 22 | 0.00 | 0.00 | 0.00 | 0.00 |
| Room P | 357 | 20.44 | 20.44 | 20.91 | 25.82 |
| Room Q | 1613 | 10.04 | 9.99 | 10.66 | 13.83 |
| Room R | 1304 | 17.77 | 17.73 | 5.14 | 7.15 |
| Room S | 647 | 13.20 | 11.24 | 8.12 | 11.02 |
| Room T | 104 | 29.53 | 29.53 | 30.87 | 34.23 |
| Room U | 1170 | 11.17 | 11.13 | 9.37 | 12.79 |
| Room V | 1038 | 11.52 | 11.40 | 10.60 | 12.13 |
| **Room W** *(longest, 111 min)* | 6652 | 21.45 | 20.73 | 19.35 | 26.70 |
| *empty-transcript floor* | | 100.00 | 100.00 | 100.00 | 100.00 |
| *non-determinism floor* | | **0.000** | **0.000** | **0.000** | **0.000** |

Summary (24 fixtures each, AMI rows included in the stats):

| arm | min | median | max | fixtures over the 3.0% ceiling |
|---|---:|---:|---:|---:|
| `mlx-fp-v3` | 0.00 | 11.48 | 29.53 | **21 / 24** |
| `mlx-int8` | 0.00 | 11.32 | 29.53 | **21 / 24** |
| `coreml-int8` | 0.00 | 9.75 | 30.87 | **21 / 24** |
| `coreml-int4` | 0.00 | 13.17 | 34.23 | **22 / 24** |

**Clause 2 fails for every arm, by an order of magnitude.** The two fixtures that
pass everywhere are the two synthesized clips (Demo, E2E — clean single-speaker
studio audio, 0.00%). Real meeting audio produces 5–30% divergence between *any*
two Parakeet checkpoints.

Note the control again: `mlx-fp-v3` — no quantization at all, just the next
checkpoint of the same model family — diverges a median 11.48%. **Roughly all of
the divergence attributed to "INT8" is actually v2-vs-v3.** `mlx-int8` tracks its
own fp control to within ~0.2 pt on most fixtures.

---

## 5. Word-time agreement

Median and p95 of |Δstart| over textually agreeing words.

| arm | p95 min | p95 median | p95 max | fixtures over the 100 ms gate |
|---|---:|---:|---:|---:|
| `mlx-fp-v3` | 80 | 160 | 240 | **23 / 24** |
| `mlx-int8` | 80 | 160 | 240 | **23 / 24** |
| `coreml-int8` | 160 | 200 | 240 | **24 / 24** |
| `coreml-int4` | 160 | 200 | 320 | **24 / 24** |
| *non-determinism floor* | **0** | **0** | **0** | 0 / 24 |

**Clause 4 fails for every arm.** But the failure needs reading, because the gate
may be mis-specified rather than the candidates being sloppy.

Parakeet emits timestamps on an **80 ms frame grid** (8× subsampling of 10 ms
frames); the pipeline's 105-second chunk hop adds a 40 ms offset to alternate
chunks. So |Δstart| is quantized, and the distribution on Room W is:

| |Δstart| | 0 ms | 40 ms | 80 ms | 120 ms | 160 ms | ≥200 ms |
|---|---:|---:|---:|---:|---:|---:|
| `mlx-int8` | 5343 | 120 | 4679 | 48 | 757 | 87 |
| `coreml-int8` | 2649 | 4170 | 2496 | 1193 | 519 | 469 |

`mlx-int8` puts **91.0%** of agreeing words within 100 ms and `coreml-int8`
**80.0%** — but p95 asks for 95%, and the next grid point above 100 ms is 160 ms.
**A p95 ≤ 100 ms gate on an 80 ms grid demands agreement within one frame on 95%
of words**, which only a bit-identical model achieves (the floor: 0 ms). The
clause as written is close to "be the baseline". Recorded as a **failure** —
pre-registered thresholds are not renegotiated after seeing data — and flagged
as a **calibration finding** for whoever writes the next protocol.

---

## 6. Speed and memory

Per-arm over all 24 fixtures, and on the longest fixture (Room W, 111 min), which
is what clause 6 gates.

| arm | median RTF | Room W RTF | Room W peak RSS | Room W phys_footprint | Room W elapsed |
|---|---:|---:|---:|---:|---:|
| `mlx-fp` *(baseline)* | 36.8× | 36.8× | 1071 MB | 4501 MB | 180.7 s |
| `mlx-fp-v3` | 38.7× | 37.0× | 1091 MB | 4539 MB | 179.9 s |
| `mlx-int8` | 41.0× | 37.2× | **1401 MB** | 4255 MB | 178.7 s |
| `coreml-int8` | 244.7× | **251.8×** | **69 MB** | **66 MB** | 26.4 s |
| `coreml-int4` | 192.5× | **271.4×** | **80 MB** | **65 MB** | 24.5 s |

Three results worth stating plainly:

* **`mlx-int8` uses MORE peak RSS than the fp baseline it was supposed to
  shrink** (1401 MB vs 1071 MB), despite a checkpoint that is 755 MB on disk
  against 2.4 GB. The shipping fp path memory-maps its safetensors and lets the
  OS drop pages; the INT8 adapter builds the architecture, quantizes it, then
  loads weights into it, which materialises them. **Clause 6 fails for
  `mlx-int8` on the memory half** — the arm's entire motivation.
* **The CoreML arms are the real memory story**, and it is much larger than
  INT8-on-MLX was ever going to be: 4501 MB → 66 MB of phys_footprint, the
  number macOS actually charges the process. That is a 68× reduction, on the
  Neural Engine, at 6.8× the speed.
* `mlx-fp-v3` and `mlx-int8` are within noise of the baseline on speed. On this
  hardware, quantization on the MLX/GPU path bought **nothing measurable**.

---

## 7. Per-clause verdict

Ship rule from PROTOCOL.md §4. All clauses conjunctive.

| # | clause | `mlx-fp-v3` | `mlx-int8` | `coreml-int8` | `coreml-int4` |
|---|---|---|---|---|---|
| 1 | WER ≤ mlx-fp + 0.5 pt on **every** reference file | **FAIL** (+3.67 ES2004a) | **FAIL** (+3.35 ES2004a) | **PASS** (−1.40, −8.16) | **FAIL** (+10.03 ES2004a) |
| 2a | divergence ≤ 3.0% | **FAIL** (21/24 over) | **FAIL** (21/24 over) | **FAIL** (21/24 over) | **FAIL** (22/24 over) |
| 2b | divergence ≥ 3× floor | vacuous (floor = 0) | vacuous | vacuous | vacuous |
| 3 | 40 most-divergent spans, zero meaning-changing errors | **PENDING** | **PENDING** | **PENDING** | **PENDING** |
| 4a | word-time p95 ≤ 100 ms | **FAIL** (23/24 over) | **FAIL** (23/24 over) | **FAIL** (24/24 over) | **FAIL** (24/24 over) |
| 4b | eval echo gate green | not measured | not measured | not measured | not measured |
| 5 | 21/21 counts + `--check` green | not measured | not measured | not measured | not measured |
| 6 | RTF ≥ baseline **and** peak RSS < baseline on longest | **FAIL** (RSS 1091 > 1071) | **FAIL** (RSS 1401 > 1071) | **PASS** (251.8×, 69 MB) | **PASS** (271.4×, 80 MB) |

**Every arm fails. None ships.**

Clauses 4b and 5 are **not measured**. They gate the diarization side, and this
work changed no shipping code — `score_asr.py` and `fluid-transcribe` are
eval-only, nothing in the app imports or invokes them, and `pipeline.py` was read
but not edited. They are also **moot for this decision**: every arm already fails
clauses 1, 2 or 4a, and no diarization result can rescue a conjunctive rule.
They must be run before any future arm is proposed as shippable.

---

## 8. Dropped and degraded arms

**None dropped.** The protocol pre-authorised dropping `mlx-int8` if the
`class_predicate` could not be reconstructed. It could:
`quantization_config.json` gives `bits=8, group_size=64, strategy=encoder_only`,
which reconstructs as "under `encoder.`, quantizable, and last dimension a
multiple of 64". Loaded with `strict=True` **on the first attempt** — a strict
load is the point, since a predicate that quantized the wrong module set would
otherwise produce a model running on half-random weights and a plausible-looking
WER. Well under the one-day timebox.

The arm works. It simply does not earn its place: no speed gain, *more* memory
than the baseline, and its accuracy delta is v3's, not INT8's.

**Degraded:** clause 2b is degenerate for all arms (floor = 0, §1b), and
`mlx-fp-v3`'s first pass lost 11 fixtures to the container change (§2) and was
re-run in full. The reported `mlx-fp-v3` table is the complete re-run.

---

## 9. PENDING

1. **PENDING FOUNDER REVIEW — the divergent spans.** Clause 3 cannot be
   discharged by machine. The dumps are written and waiting:
   `results/transcripts/<arm>/<pseudonym>.spans.json`, **404–408 spans per arm**
   (top 20 per fixture), each with the baseline text, the candidate text, six
   words of context either side, and a timestamp to find the moment in the
   meeting. What matters is numbers, negations, names and commitments — not
   phrasing. **Gitignored: verbatim private speech.** Until a human reads them,
   no arm may be described as "no meaning-changing errors", and this file does
   not claim it.
2. **PENDING — the dictated fixture.** The corpus has no clean single-speaker
   dictation fixture with a known script. Demo and E2E are the closest, and both
   score 0.00% divergence on every arm — they are too easy to discriminate
   anything. A read-aloud passage with a written reference would give a true WER
   on the product's own microphone path, which AMI (headset-mixed conference
   audio) does not represent. Not required by the ship rule; the obvious hole in
   the corpus.
3. **NOT MEASURED — clauses 4b and 5** (eval echo gate; 21/21 counts and
   `--check`). See §7 for why this is safe here and mandatory next time.

---

## 10. `fluid-diarizer` is byte-identical

`fluid-transcribe` was added as a **second** `executableTarget` in
`native/fluiddiarizer/Package.swift`. The FluidAudio pin and
`Sources/fluid-diarizer/` were not touched. The shipping binary must not move:

| when | SHA-256 of `.build/release/fluid-diarizer` |
|---|---|
| before the `Package.swift` edit | `b2c7d8acf872b7b96f2ba754343f430b8f18eecfcba733f7388f612bdbfe51c0` |
| after the edit, `swift build -c release` | `b2c7d8acf872b7b96f2ba754343f430b8f18eecfcba733f7388f612bdbfe51c0` |
| after deleting the binary and forcing a relink | `b2c7d8acf872b7b96f2ba754343f430b8f18eecfcba733f7388f612bdbfe51c0` |

**Identical.** SPM did not even recompile the diarizer target when the second
target was added; the forced relink was done because "it wasn't rebuilt" and "it
rebuilds identically" are different claims and both were wanted. `swift build`
(debug) also still links `fluid-diarizer` cleanly.

---

## 11. What this evaluation does not conclude

* **Nothing about whether `coreml-int8` is actually worse.** It is *more accurate
  than the shipping baseline on both reference meetings*. Its clause-2 failure is
  a divergence measurement, and divergence is not accuracy: where the baseline is
  wrong and the candidate is right, this harness scores it against the candidate.
  That was pre-registered (PROTOCOL.md §7) and it is why clause 3 exists. On the
  evidence here, **the honest description of `coreml-int8` is "different, faster,
  much smaller, and better on the only files where truth exists" — not "worse".**
  What blocks it is that nobody has yet read what changed in the real meetings.
* **Nothing about the ship rule's calibration being right.** Two clauses look
  mis-specified in hindsight: a 3.0% divergence ceiling when *any* two Parakeet
  checkpoints differ by 5–30% on real meeting audio, and a 100 ms word-time p95
  on an 80 ms timestamp grid. Both were applied exactly as written. A future
  protocol should set the divergence ceiling from the measured cross-checkpoint
  spread and the word-time gate in frames.
* **Nothing about languages other than English, or streaming.** Every arm is
  offline, whole-file, English.
* **Nothing about AMI standing in for the product's audio.** AMI is headset-mixed
  conference speech; the product's hard case is a room microphone (see the
  Room-prefixed fixtures' divergence, consistently the highest in §4).

---

## Reproduce

```sh
cd native/fluiddiarizer && swift build -c release        # builds both binaries
cd ../asr-ab
python score_asr.py --list                               # what resolves, and from where
python score_asr.py --engine mlx-fp                      # baseline
python score_asr.py --engine mlx-fp --tag rerun          # the non-determinism floor
python score_asr.py --engine coreml-int8                 # any candidate
```

Use `~/.meetingscribe/venv/bin/python`. AMI references need
`ami_public_manual_1.6.2` unpacked under `refs/` (PROTOCOL.md §5). Committed
results are pseudonym-keyed; `results/transcripts/` is gitignored verbatim
speech and must stay that way.
