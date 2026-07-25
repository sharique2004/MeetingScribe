<!-- GENERATED FILE — edit tools/make_synthetic_fixtures.py instead. -->
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
| `analysis.json` | `{"transcripts": {"mic": [...], "system": [...]}}`, with word timings |
| `analysis.npz` | `<track>_windows` (N,2) float64, `<track>_embeddings` (N,192) float32, `embed_version` |
| `labels.json` | **ground truth** — per-window true voice, true speaker count, generation parameters |

Window COORDINATES come from the real `diarization.build_windows()` over the
synthesized segments, so they are laid out exactly as a real run would lay them
out. Only the embedding VECTORS are synthesized.

## No personal data, by construction

No audio is read or produced and no real recording is involved. The vectors come
from numpy's PCG64 seeded with `20260725`; the scripts contain no personal names,
company names, or real meeting titles. The generator refuses to write text that
trips its name screen.

## Generation parameters

| parameter | value |
|---|---|
| `seed` | `20260725` |
| `embedding_dim` | `192` |
| `max_voices` | `8` |
| `between_speaker_cosine_distance` | `0.85` |
| `within_speaker_sigma` | `0.35` |
| `within_speaker_pair_distance_expected` | `0.109131` |
| `drift_degrees` | `80.0` |
| `phantom_cosine_distance` | `0.65` |
| `phantom_window_share` | `0.12` |
| `turn_gap_seconds` | `0.18` |

A voice's centroid is `sqrt(1-d)*common + sqrt(d)*anchor_i`, so two distinct
voices sit at cosine distance exactly `between_speaker_cosine_distance`. A window
is `normalise(centroid + sigma*noise)` with the noise drawn orthogonal to every
centroid, so two windows of one voice sit at about
`sigma^2/(1+sigma^2)`. Full derivation in the generator's module docstring.

## Fixtures

| slug | mode | true speakers | embeddings | windows | what it is for |
|---|---|---|---|---|---|
| `synthetic-solo-one-voice-20200101-000001` | inperson | 1 | clean | mic:60 | The trivial positive control: one speaker, nothing to separate. |
| `synthetic-two-long-turns-20200101-000002` | inperson | 2 | clean | mic:80 | Two genuine speakers taking long turns — the easiest case for a coherence metric, and the shape the only pre-existing positive control had. |
| `synthetic-two-medium-turns-20200101-000009` | inperson | 2 | clean | mic:64 | Two genuine speakers at 5 s a turn — the middle of the bracket around the 0.30 fragmentation gate. |
| `synthetic-two-rapid-alternation-20200101-000003` | inperson | 2 | clean | mic:72 | Two genuine speakers swapping every ~3 s. Named by docs/COUNT_ESTIMATION_DESIGN.md as the case the fragmentation rule was never tested against: a real speaker here produces many short runs, which is what the rule reads as a phantom. This fixture is the counter-example — its speaker count is KNOWN to be 2. |
| `synthetic-two-backchannel-20200101-000004` | inperson | 2 | clean | mic:88 | A genuine second speaker who only ever says 'mm-hm' and 'right'. Low duration ratio, and the interjections are spread through the whole meeting — the quiet-but-real participant the duration gate is supposed to protect. |
| `synthetic-three-voices-20200101-000005` | inperson | 3 | clean | mic:72 | Three genuine speakers, medium turns. |
| `synthetic-four-voices-20200101-000006` | inperson | 4 | clean | mic:80 | Four genuine speakers — the counter-direction control: whatever folds phantoms must leave these four apart. |
| `synthetic-same-voice-drift-20200101-000007` | inperson | 1 | drift | mic:60 | ONE person whose embeddings rotate 80 degrees across the meeting. The ends are further apart than the clustering threshold, but the halves' centroids are close enough that _fold_weak_clusters merges them. Must not be two people. |
| `synthetic-same-voice-split-20200101-000008` | inperson | 1 | phantom | mic:80 | ONE person, 12% of whose windows sit 0.65 away from their own centroid, scattered so they land in runs of one. Reproduces the real-corpus phantom: the split survives BOTH folds, so the auto path returns 2 clusters for 1 person. Anything that counts clusters gets this wrong. |
| `synthetic-same-voice-big-split-20200101-000015` | inperson | 1 | phantom | mic:80 | The same one person, but with a lobe big enough that the runner-up carries ~40% of the dominant cluster's speech. Still one person. Included because it is the case a duration test alone cannot reject — only the scatter distinguishes it. |
| `synthetic-call-clean-audio-20200101-000010` | online | 2 | clean | — | System/mic speech ratio ~0.97. The fallback must never fire. |
| `synthetic-call-half-volume-20200101-000011` | online | 2 | clean | — | System/mic ratio ~0.53. |
| `synthetic-call-third-volume-20200101-000012` | online | 2 | clean | — | System/mic ratio ~0.31. |
| `synthetic-call-brief-far-end-20200101-000013` | online | 2 | clean | — | System/mic ratio ~0.11 — well under any plausible ratio gate, and still a call whose audio was captured perfectly. |
| `synthetic-call-quiet-far-end-20200101-000014` | online | 2 | clean | — | System/mic ratio ~0.03, and its shortest single segment is ~1 s against 96 s of mic. This is the fixture the absolute-vs-ratio check is aimed at: a ratio gate fires here, an absolute one cannot. |
| `synthetic-ask-demo-call-20200101-000020` | online | 3 | clean | mic:8, system:45 | Already-labelled meeting used by the citation tests: two turns start at exactly 0.00 and two more pairs 0.30 s apart, so a cited timestamp cannot choose between them. |
