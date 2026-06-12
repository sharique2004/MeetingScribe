# 🎙️ MeetingScribe

Record any meeting on this PC, get a full transcript of **who said what**, and
review your own speaking performance — talk time, pace, questions asked,
filler words. Everything runs **locally on your machine**: no cloud, no API
keys, no subscription, and your meeting audio never leaves your PC.

## How to start

**Windows:** double-click **`setup.bat`** once, then **`run.bat`** from then on.
**macOS:** open Terminal in this folder and run `bash setup.sh` once. That
also installs **MeetingScribe.app** into Applications — from then on just
launch it like any Mac app (Spotlight, Launchpad, or drag it to the Dock; no
Terminal needed). Quit it with the **⏻ Quit** button in the web UI.
(`run.command` still works if you prefer the Terminal.)

A browser tab opens at <http://127.0.0.1:5005>. Keep the console/Terminal
window open while you use the app — closing it stops MeetingScribe (your
recordings are always saved first).

## Setting up on a Mac

1. Get this folder onto the Mac (OneDrive sync, AirDrop, USB — anything).
2. Open **Terminal**, `cd` into the folder, run `bash setup.sh` once. It
   finds (or installs via Homebrew) Python and downloads the Python packages
   (~2 GB, needs internet). The Python environment lives in
   `~/.meetingscribe/` — outside this folder, so OneDrive never syncs it.
3. Start the app with **`run.command`**. The first time you record, macOS
   will ask for **Microphone** permission — allow it.

### Capturing the other side of online calls on macOS

macOS can't record "what comes out of the speakers" by itself — that needs
the free **BlackHole** virtual audio driver. Install it once:

```
brew install blackhole-2ch
```

(`setup.sh` offers to do this for you; no Homebrew? Download the
*BlackHole 2ch* installer from <https://existential.audio/blackhole/>.)

**That's the only manual step.** Everything else is automatic: when you
press *Start recording*, MeetingScribe creates a **"MeetingScribe Output"**
Multi-Output Device (your speakers/headphones + BlackHole), switches the
sound output to it for the duration of the recording, and switches back
when you stop. You hear everything normally the whole time, and the
BlackHole copy is recorded as the "system" track. The *Audio devices* panel
in the sidebar shows whether both devices are ready before you record.

(Prefer to manage audio devices yourself? Set `"auto_route_macos": false`
in `config.json` and pick *MeetingScribe Output* in Control Centre → Sound
manually while recording.)

Without BlackHole, MeetingScribe still records *your* mic perfectly — you
just won't get the other participants transcribed (the app will warn you).

The app records whichever computer it runs on, so install it on the machine
where your meetings actually happen.

## How to use it

1. Before your meeting starts, type a title and press **● Start recording**.
   On a Mac, today's **calendar events appear as suggestions** above the
   title field — click one to use its name (and its attendee count as the
   speaker count). Leave the title empty and the recording is **named
   automatically** after the event happening right now. (macOS asks for
   calendar permission once; meetings work fine without it.)
2. Pick the meeting type:
   - **💻 Online call** (Zoom / Meet / Teams on this PC): your microphone is
     recorded as **You**, and the meeting audio coming out of your
     speakers/headphones is recorded separately and split into
     *Speaker 1, Speaker 2, …* automatically.
   - **🧑‍🤝‍🧑 In person**: everyone shares this PC's microphone and voices are
     told apart automatically.
3. If you know how many people are talking, enter the number — it makes
   speaker separation more accurate. Otherwise leave it on auto.
4. When the meeting ends, press **■ Stop & transcribe**. On macOS 26+
   transcription uses Apple's on-device Speech engine (Neural Engine) — a
   45-minute meeting finishes in **a minute or two**, with almost no CPU use
   or heat. Older Macs fall back to Whisper on the GPU, and other machines to
   Whisper on the CPU (about a third of the meeting's length).
5. Open the meeting to read the transcript. Click the **meeting title** to
   rename the meeting, a **speaker name** to rename them (e.g. "Speaker 1" →
   "Priya"), or a **timestamp** to replay that moment. Export as
   Markdown/Text, or copy to clipboard to paste into ChatGPT/Claude for
   feedback on your conversation skills.
   If the speakers were split wrongly, use the **"Speakers besides you"**
   selector above the stats: set the real number and the transcript is
   re-clustered instantly (the voice analysis is saved, so nothing needs to
   be re-transcribed).
6. **✨ Tidy with Claude** (optional): if the transcript has echo duplicates
   or split-up speakers, this button cleans it using the `claude` CLI already
   on your machine (your Claude Code subscription — no API key). Only the
   transcript *text* is sent, never audio, and a backup is kept so you can
   **↩ Undo tidy**.

## Tips for best results

- **Wear headphones** during online calls. Without them, the other people's
  voices leak from your speakers into your mic; MeetingScribe detects and
  removes most of that echo, but headphones make it perfect.
- The **first run** downloads two small AI models (~550 MB total) into the
  `models/` folder; later runs are fully offline.
- Speaker separation is statistical — for important meetings, set the speaker
  count before recording, and use **🔁 Reprocess** if you change settings.

## Settings (optional)

Create/edit `config.json` in this folder (defaults shown):

```json
{
  "whisper_model": "auto",
  "whisper_backend": "auto",
  "language": null,
  "diarization_threshold": 0.6,
  "auto_route_macos": true,
  "port": 5005,
  "open_browser": true
}
```

- `whisper_model`: `auto` / `tiny` / `base` / `small` / `medium` / `large-v3`
  / `large-v3-turbo`. Bigger = more accurate, slower. `auto` picks
  `large-v3-turbo` on the Apple-GPU backend and `small` on CPU.
- `whisper_backend`: `auto` / `mlx` / `faster`. `mlx` is the Apple-GPU
  backend (Apple Silicon only) — much faster and cooler than CPU.
- `language`: force a language (`"en"`, `"hi"`, …) or `null` to auto-detect.
- `diarization_threshold`: lower → more likely to split similar voices into
  separate speakers; higher → more likely to merge them.
- `auto_route_macos`: let the app switch the sound output to the
  Multi-Output Device while recording (macOS).

## Where things are stored

Each meeting lives in `recordings/<title> — <id>/` — the folder is named
after the meeting (and renames itself when you rename the meeting), with a
timestamp id kept at the end so names can never collide.

- `…/mic.wav` — your microphone
- `…/system.wav` — the meeting audio
- `…/meeting.json` — transcript, speakers, stats (the app's data)
- `…/transcript.md` — a **human-readable Markdown transcript**,
  written automatically when transcription finishes and kept up to date when
  you rename the meeting or speakers, adjust the speaker count, or tidy.
  Open it in any editor — no app needed. The **📂 Reveal .md** button shows
  it in Finder.
- `…/analysis.json` / `analysis.npz` — saved voice analysis
  that makes the instant speaker-count adjustment possible.

Delete a meeting from the UI, or just delete its folder.

## Troubleshooting

- **"BlackHole is not installed" (macOS)** — run `brew install blackhole-2ch`
  once (or re-run `bash setup.sh`), then start the recording again. No
  restart of the app is needed.
- **Volume keys don't work during a recording (macOS)** — that's a macOS
  limitation of Multi-Output Devices. Your output (and volume control)
  comes back automatically when you stop the recording.
- **"No system-audio (loopback) device found" (Windows)** — your output
  device changed mid-session. Stop and start the recording again after
  plugging in headphones, not before.
- **Other participants missing from the transcript** — make sure the meeting
  sound actually plays through this PC (not a phone or external speaker).
- **Transcript quality poor** — switch `whisper_model` to `"medium"` in
  `config.json`, then use **🔁 Reprocess** on the meeting.
