# MeetingScribe native UI redesign — locked direction

**Status:** Direction locked with founder (Aug 2, 2026) · supersedes the web
`ui-demos/` convergence for the app itself (those prototypes remain useful as
content/IA reference).

## The one-line brief

Kill the WKWebView. Rebuild the entire front end as a native SwiftUI app that
is indistinguishable from something Apple shipped with macOS 26 — real Liquid
Glass, native scroll physics, system materials — with MeetingScribe's identity
carried only by an aqua accent tint and the icon.

## Hard decisions (founder-confirmed)

| Question | Decision |
|---|---|
| UI technology | SwiftUI, native components only. No web views anywhere. |
| OS floor | **macOS 26 (Tahoe) only.** One codepath, real Liquid Glass APIs (`glassEffect`, etc.). Older-macOS users stay on the current release. |
| Main window layout | **Three-column**: meetings sidebar → transcript center → right inspector rail. |
| Rail mode switching | Liquid Glass segmented control: **Summary / Speakers / Ask**. |
| Personality | **Voice Memos + Notes hybrid** — Voice Memos' waveform/recording DNA for capture & playback, Notes' calm document reading for transcripts & summaries. |
| Color | **Pure system structure + aqua/mint accent tint** (the Aurora accent survives as the app accent color, like Things is blue). SF Pro, system materials, full light *and* dark mode. No custom skin. |
| Scope | **Everything**: main window, recording HUD, notes, setup/onboarding, menu-bar item. |
| Transcript style | **Screenplay**: colored speaker name above each turn, timestamp in a quiet gutter, full-width text blocks. |
| Sidebar rows | **Rich cards**: title, time · duration, participant dots + count, one-line AI summary snippet, processing badge while summarizing. Grouped under date headers (Today / Yesterday / date). |

## The recording HUD (complete replacement of RecordingPanel + NotesPanel)

Today: a 300×58pt panel (dot, waveform, timer, buttons) plus a separate
2,100-line notes panel. Founder verdict: "both those items look like ass."
Both are replaced by **one unit**:

1. **Collapsed capsule — the default state.** A tiny Liquid Glass pill,
   roughly **90×28pt**, containing only: recording dot · elapsed timer ·
   chevron. Visual benchmark: macOS's own screen-recording indicator.
   - **Hover** morphs pause/stop controls into the same pill (no second
     window, no size jump beyond the pill widening).
   - Always-on-top, joins all Spaces, frame position remembered
     (keep the existing `setFrameUsingName`-before-`setFrameAutosaveName`
     fix from `RecordingPanel.swift:325` — it guards a real regression).
2. **Notes dropdown — attached, collapsible.** Clicking the capsule (or its
   chevron) springs open a glass sheet **attached directly beneath the
   pill** containing *only* a notes editor. No captions, no tabs, no chrome.
   Click again (chevron flips) to collapse back to the bare capsule so the
   screen is clear. Native spring animation; exit faster than enter.
3. The capsule + dropdown are one `NSPanel` hosting SwiftUI
   (`.nonactivatingPanel` so jotting a note never steals focus from Zoom).

## Main window details

- `NavigationSplitView` (sidebar / content / inspector). Sidebar and
  inspector collapsible; state restored across launches.
- **Sidebar**: rich meeting cards as above. Search field at top
  (native `.searchable`).
- **Center — transcript**: screenplay layout. Native `ScrollView` with
  `scrollPosition` — auto-follows the active line during playback, parked
  ~40% from the top; user scroll breaks the follow, a "jump to live" pill
  restores it. Text is selectable.
- **Playback transport**: slim bar pinned to the bottom of the center pane
  (Voice Memos DNA): play/pause, clock, waveform seek strip, source picker
  (Mix / Your mic / Meeting).
- **Inspector rail (~340pt)** behind the glass segmented control:
  - **Summary** — TL;DR, decisions, action items (owner + due, checkable),
    copy-as-email.
  - **Speakers** — talk-time share bars, rename, Auto/1–8 recluster.
  - **Ask** — chat thread + composer against the meeting.

## Defaults chosen by design (revisit only if they feel wrong in the build)

- Both light and dark mode ship day one; screenshots/marketing use dark.
- Meeting title is inline-editable in the toolbar (Notes-style).
- ⌘K command palette from the web brief is **deferred** — native menu bar +
  `.searchable` cover it; revisit after v1 of the rebuild.
- Setup/onboarding becomes a native welcome flow (permissions: mic → system
  audio on first recording; fire-on-first-use, since there is no public API
  to pre-check the system-audio TCC state).
- Menu-bar item: native `MenuBarExtra` with start/stop, last meeting,
  open-main-window.

## Architecture

- The Python backend (Flask on `127.0.0.1:5005`) **stays** — transcription,
  diarization, summarization, storage are untouched. The SwiftUI app is a
  pure client of the existing local HTTP API (same endpoints the web UI
  uses today). `BackendManager`, `StatusPoller`, `NudgePoller` port over
  largely as-is.
- This rebuild is also the first prerequisite on the Mac App Store path
  (see `docs/APP_STORE_RESEARCH.md`) — the WKWebView dies here; the Python
  backend swap is a separate later phase and NOT part of this redesign.

## Build order

1. New SwiftUI app target alongside the existing `macapp` (nothing shipped
   is touched until parity).
2. Recording capsule + notes dropdown first (smallest surface, highest
   founder pain, exercises glass + panels + backend API).
3. Main window: sidebar cards → screenplay transcript → playback → rail
   (Summary, Speakers, Ask in that order).
4. Onboarding + menu-bar item.
5. Swap the app entry point; retire `MainWindow.swift` (WKWebView),
   `RecordingPanel.swift`, `NotesPanel.swift`, `SetupWindow.swift`.
