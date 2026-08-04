# Multi-party ground truth — the corpus hole, measured (2026-08-03)

The owner reproduced the reported failure by playing a multi-speaker YouTube
video into the room mic: words right, speakers wrong (3 found against a
narrated truth of 4–5, several video voices fused into one label). The eval
corpus could never see this — 17 of its 21 truth-backed fixtures hold ONE
voice. `score_multiparty.py` closes the hole: it scores the engines against
per-second reference truth (AMI meetings; any WAV + RTTM works, including a
labelled YouTube clip) and reports time-weighted speaker CONFUSION, the
number the count metric is blind to.

## Results (AMI, 4 speakers, reference RTTMs; collar 0.25 s)

| audio | engine | count | confusion |
|---|---|---|---|
| ES2004a Mix-Headset (clean ≈ system track) | classic | 4/4 | 1.0% |
| | neural auto | 4/4 | 0.8% |
| | **hybrid (shipping)** | 4/4 | **0.9%** |
| ES2004a Array1-01 (far-field ≈ room mic) | classic | 4/4 | **21.7%** |
| | neural auto | 4/4 | 1.7% |
| | **hybrid (shipping)** | 4/4 | **1.6%** |
| IS1009c Mix-Headset | hybrid | 4/4 | 0.5% |
| IS1009c Array1-01 | hybrid | 4/4 | 0.8% |

Read the far-field row: on real room audio the classic window-vote engine
puts one word in five on the wrong person; the hybrid's neural turns cut
that to 1.6%. The 2026-08-03 hybrid is doing exactly its job on genuine
multi-party audio, clean or far-field. Full rows (miss/FA, strict variant):
`results/multiparty-*.json`.

## The owner's regime: produced audio replayed through a loudspeaker

`ES2004a.Playback-sim.wav` degrades the clean mix through a loudspeaker-
into-room chain (120 Hz–4 kHz band-limit, 0.35 s decaying-noise reverb,
soft compression, mic noise; generator inline in the session record). Truth
unchanged: 4 speakers.

| engine (auto) | count | confusion |
|---|---|---|
| classic | 3/4 | 23.7% |
| neural | 3/4 | 20.9% |
| hybrid | 3/4 | 21.9% |

Every auto path under-counts — the loudspeaker re-radiates every voice
through one physical channel, and that shared channel dominates the voice
differences. This reproduces the YouTube failure exactly, and no threshold
rescues it (swept 0.7–0.9 on the owner's clip: 3 substantial voices at
every point).

**The decisive cell — count FORCED to the true 4:**

| forced k=4 | confusion |
|---|---|
| classic (the recluster dropdown's old path) | **27.6%** |
| neural turns | **2.7%** |

## What shipped from this measurement

1. `pipeline._neural_refine(user_forced=...)`: a count the USER typed skips
   the neural self-validation gate and runs the engine pinned to that
   number. The gate's "engine hears fewer → keep classic" rule was built
   for MACHINE counts (Room T) and was exactly wrong for the human-rescue
   path — the table above is the 10x. Calendar-guessed counts never take
   this branch.
2. `recluster_meeting` allows a fresh neural run when the user typed a
   count (a few seconds, narrated by "Refining speaker turns…"); Auto
   recluster stays cache-only and sub-second.

## What this file does NOT conclude

* The under-COUNT on loudspeaker playback is unsolved in auto mode; the
  rescue is the user's speaker-count control. The product answer is
  upstream anyway: this audio only reaches the room mic when the
  system-audio tap is broken. The owner's failing recording had an
  all-zero system track — "system-audio tap helper did not become ready" —
  traced to the /Applications bundle swap at 01:50 that morning detaching
  the macOS System Audio Recording grant. With the tap healthy, video
  audio arrives on the system track, which measures in the clean row.
* AMI numbers here use this scorer's frame-based metric (Hungarian
  mapping, 0.25 s collar, overlap scored) — comparable within this file,
  not to published DER tables.
