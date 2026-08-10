# Parakeet INT8 evaluation — PRE-REGISTERED PROTOCOL

**Written before a single measurement was taken.** Nothing below may be edited
once a number exists; RESULTS.md records what happened against this text. A
protocol written after the numbers is not a protocol, it is a caption.

Plan item C2. The question: **can MeetingScribe replace its shipping fp
Parakeet with an INT8 one and stay honest about the transcript?** The answer is
allowed to be no.

---

## 0. Why this file exists at all

The shipping ASR is `pipeline._transcribe_parakeet` — parakeet-mlx running
`mlx-community/parakeet-tdt-0.6b-v2` on the Apple GPU. An INT8 candidate is
attractive for one reason (memory: the fp checkpoint is ~2.4 GB of weights and
the peak RSS shows it) and dangerous for one reason (a quantized transcript can
be *quietly* worse — a wrong number, a flipped negation, a dropped name — in
ways an aggregate WER of "+0.3 points" will not surface).

So this protocol measures three separate things and gates on all of them:

1. **Accuracy where truth exists** (AMI reference transcripts): WER and CER.
2. **Agreement where truth does not exist** (the real meeting corpus, which is
   the actual product domain and has no reference transcripts): divergence-WER
   against the shipping arm, floored by the shipping arm's own non-determinism.
3. **Everything downstream depends on word times**, so word-time agreement is
   gated separately from text.

## 1. Arms

Every arm below transcribes the SAME audio file and produces the SAME segment
shape (`{"start","end","text","words":[{"w","s","e"}]}`) that
`pipeline._transcribe_parakeet` returns, so the comparison is like-for-like all
the way down to word timings.

| arm | what it is | why it is in the grid |
|---|---|---|
| `mlx-fp` | **the shipping baseline.** `PARAKEET_REPO_EN` = `mlx-community/parakeet-tdt-0.6b-v2` via `pipeline._transcribe_parakeet`, called through the real function, not a copy of it | this IS production. Every other arm is judged against it |
| `mlx-fp-v3` | `mlx-community/parakeet-tdt-0.6b-v3`, same parakeet-mlx code path, no quantization | **isolates v2→v3 from quantization.** Without this arm, a v3-int8 candidate's whole delta is unattributable: model-generation change and precision change are confounded |
| `mlx-int8` | `sonic-speech/parakeet-tdt-0.6b-v3-int8` loaded through an adapter in this harness: `mlx.nn.quantize` with a `class_predicate` reconstructed from the repo's `quantization_config.json`, then `load_weights` **without** parakeet-mlx's bfloat16 cast | the like-for-like INT8 arm on the shipping runtime (MLX/GPU) |
| `coreml-int8` | `FluidInference/parakeet-tdt-0.6b-v3-coreml`, INT8 encoder, via the new `fluid-transcribe` helper at the pinned FluidAudio revision `5390df9752c8fc583596018360c5fd70d6fa6c75` | INT8 on the Neural Engine — a different runtime, different memory profile |
| `coreml-int4` | same, INT4 encoder | the aggressive end of the quantization ladder; included so the ladder has a visible bottom, not because it is expected to pass |

**Timebox.** `mlx-int8` is timeboxed to roughly one day of effort. If the
`class_predicate` cannot be made to load the checkpoint, the arm is **DROPPED**
and the reason is recorded in RESULTS.md. A dropped arm is an acceptable
outcome and is not a silent omission.

**Pin discipline.** The CoreML arms run at the FluidAudio revision the
diarization A/B already validated. `fluid-transcribe` is a *second* executable
target in the same package; the `fluid-diarizer` binary must not change. The
before/after SHA-256 of `fluid-diarizer` is reported in RESULTS.md.

## 2. Degenerate floors — printed on every table

A metric with no floor under it is decoration. Two floors, both required on
every table:

**(a) The empty-transcript floor.** An arm that returns nothing scores
**WER = 100%**. This is what "as good as no transcript at all" looks like on the
same axis as the candidates, and it is the reason a WER number of "12%" means
something.

**(b) The non-determinism floor — MEASURED FIRST, BEFORE ANY CANDIDATE.**
`mlx-fp` is run **twice** on the same audio and scored against itself. Whatever
divergence that produces is the floor: it is the amount of disagreement the
shipping engine has with *itself*, from GPU nondeterminism and chunk-boundary
merge effects, with no model change whatsoever. A candidate whose divergence is
near this floor is indistinguishable from the baseline; a candidate whose
divergence is far above it is genuinely a different transcriber.

Run on **3+ fixtures including the longest** (Room W, 111 min — the longest
fixture is where chunk-merge nondeterminism has the most opportunity to
accumulate, so a floor measured only on short clips would be flatteringly
small).

**This ordering is not negotiable.** The floor is measured before any candidate
is run, so that no candidate's number can influence what counts as "close to
the floor".

## 3. Metrics, per arm × fixture

* **WER and CER vs references, where references exist.** References exist for
  the AMI meetings only (§5). Normalization for the primary number: lowercase,
  strip punctuation. A **normalized-divergence variant** (additionally
  digit- and punctuation-insensitive: numbers spelled out and folded) is
  computed and printed *beside* the raw number, never instead of it — "1,500"
  vs "fifteen hundred" is a formatting difference, and a gate that cannot tell
  it from a content difference will fail candidates for the wrong reason.
* **Divergence-WER vs `mlx-fp`** on the real fixtures. This is the same edit
  distance, with the baseline transcript standing in for a reference. It is
  *not* an accuracy number and is never reported as one. The **top-20 most
  divergent spans** are dumped per arm×fixture for human reading; they contain
  real meeting speech and are therefore **gitignored**.
* **Word-time agreement.** On words that agree *textually* between the
  candidate and `mlx-fp` (aligned by the same edit path), the median and p95 of
  `|Δstart|`. Word→speaker attribution reads these timestamps; text that is
  right at the wrong time is still a defect.
* **RTF** (audio seconds / wall seconds; higher is faster).
* **Peak RSS.** Each arm is spawned in a **fresh subprocess** and its RSS is
  sampled the way `tools/rss_probe.py` samples it (`ps -o rss=`, plus
  `footprint` at marks). `rss_probe.py` is imported/reused read-only. In-process
  measurement is not acceptable here: a second arm inheriting the first arm's
  warmed allocator measures the wrong thing.

## 4. SHIP RULE — all clauses conjunctive

An INT8 arm ships **only if every one of these is true.** Any single failure
means it does not ship. There is no aggregate score and no trading one clause
off against another.

1. **WER ≤ `mlx-fp` + 0.5 points absolute on EVERY reference file.** Not on
   average — on every file. An arm that is 2 points worse on one meeting and
   better on another has not earned an average.
2. **Divergence ≤ 3.0% AND ≥ 3× the non-determinism floor.**
   Read that carefully: the divergence must be *below* 3.0% to pass, and the
   3× term is the **interpretation guard** — a divergence that is *not* at least
   3× the floor is statistically indistinguishable from the baseline talking to
   itself, so passing "3.0%" on such a measurement proves nothing about the
   candidate and the result is reported as UNINFORMATIVE rather than as a pass.
3. **Human review of the 40 most-divergent spans finds ZERO meaning-changing
   errors.** Numbers, negations, names, and commitments are what matter. This
   clause cannot be discharged by a machine and is recorded as **PENDING
   FOUNDER REVIEW** in RESULTS.md until a human has read the spans.
4. **Word-time p95 ≤ 100 ms**, and the **eval echo gate is green**.
5. **21/21 counts and `tools/eval_diarization.py --check` green.**
6. **RTF ≥ baseline and peak RSS < baseline on the longest fixture.** Faster and
   smaller, on the file where it matters. A candidate that is smaller but slower
   on a 111-minute meeting has moved the cost, not removed it.

**Partial runs are smoke evidence, never gate results.** A run over a subset of
fixtures (`--only`) demonstrates that the machinery works. It does not
discharge any clause above. Every table in RESULTS.md is labelled
**SMOKE** or **GATE** and no clause is marked satisfied from a SMOKE table.

## 5. Corpora

**AMI (references exist).** `ES2004a` and `IS1009c`, already on this machine
under `~/.meetingscribe/multiparty`. Reference transcripts are built from the
AMI **manual word annotations** (`ami_public_manual_1.6.2`, CC-BY, `words/*.xml`
from the official AMI corpus mirror), downloaded into `native/asr-ab/refs/`
(gitignored — bulky, and third-party). Scoring is **Mix-Headset audio against
the mixed reference**: every speaker's words concatenated in time order, which
is what a single-channel transcriber can be asked to produce. Both arms of the
comparison see the same thing.

**Real corpus (no references).** The gitignored fixtures under
`test/fixtures/`, resolved to their original audio under
`~/.meetingscribe/recordings/` by the shared trailing `YYYYMMDD-HHMMSS`
timestamp — the same join key `native/diarization-ab/score_ab.py` uses. Both
`.wav` and `.flac` extensions are probed. Here there is no truth, so the only
available question is divergence from the shipping arm, floored per §2(b).

**Privacy — the same rule as the rest of the harness.** Everything committed or
printed is keyed by the stable pseudonym (`Call A`, `Room W`). Directory names
are real meeting titles and are resolved in memory only. The pseudonym map and
fixture helpers are **imported from `tools/eval_diarization.py`**, never copied,
so this report and the diarization report can never drift apart. Per-fixture
transcripts and divergent-span dumps go to `results/transcripts/` and are
**gitignored**: they are verbatim private speech. Only the aggregate,
pseudonym-keyed numbers are committed.

## 6. Run order

1. `mlx-fp` twice on 3+ fixtures including the longest → **the floor** (§2b).
2. The full grid: every arm × (AMI files + real fixtures), at least 8
   representative real fixtures **including the longest** (Room W, 111 min). If
   wall-clock makes all 22 impractical, ≥ 8 run and the remainder are marked
   PENDING, clearly labelled SMOKE vs GATE per §4.
3. RESULTS.md: the full table, **both floors printed**, a per-clause verdict for
   §4, and every PENDING item named.

## 7. What this protocol cannot conclude

* Nothing about languages other than English. The shipping English path is
  `parakeet-tdt-0.6b-v2`; the multilingual path is out of scope here.
* Nothing about streaming/live captions. Every arm is offline, whole-file.
* AMI WER computed here is a within-this-file number (this normalization, this
  mixed-reference construction). It is **not** comparable to published AMI WER
  tables, and no row in RESULTS.md may be quoted as one.
* Divergence-WER is not accuracy. If `mlx-fp` is wrong and a candidate is right,
  that shows up here as divergence — i.e. as a mark *against* the candidate.
  This is deliberate (the baseline is what ships and what users have accepted),
  and it is exactly why clause 3 requires a human to read the spans.
