# MeetingScribe transcript redesign — converged brief

**Decision (from user):** combine **Aurora's UI/visual system** with **Split Focus's two-pane layout + scrolling**.
> "I like Aurora and Split Focus — the scrolling side for Split Focus but the UI is better on Aurora."

Build this **in OpenPencil** (real design doc → export code), guided by the installed design skills. Do NOT hand-code from vibes.

---

## The convergence

Take Split Focus's **structure** and dress it in Aurora's **skin**.

### Layout (from Split Focus)
- Two panes. **Left (~62%)**: the transcript — mono timestamps in a gutter, small colored speaker name at each turn, clean dialogue; its own **lenis** smooth scroll; slim bottom transport bar pinned to this pane only (play/pause, clock, waveform seek, Mix / Your mic / Meeting).
- **Right context rail (~340px)**: segmented switcher — **Summary / Speakers / Ask**. Summary = tldr + decisions + action items (owner chips + due) + copy-email. Speakers = talk-time + share bars + % + rename pencil + Auto/1–8 recluster (all the relocated header data lives here). Ask = chat thread + composer.
- **⌘K command palette** = the single place to do anything (search transcript, jump to speaker, run Summary/Tidy/Sync/Export, change size, toggle rail). Opens FAST (~120ms fade+scale 0.97, center origin — Emil: keyboard/high-frequency actions barely animate).
- **Speaker minimap seam** between the panes: who talks where, click to seek.
- Minimal top bar: editable title, ⌘K hint, rail toggle, small Aa size control.

### Skin (from Aurora)
- **Palette:** night base `#0a0a12`; glass panels `rgba(255,255,255,.03)` with a hairline top-lit border. Aurora WASH (blue→soft-violet `#3b82f6 / #60a5fa / #a5b4fc / #c4b5fd`) kept subtle and confined to the top third + behind the rail — must never fight the transcript's legibility. **Working accent = aqua/mint `#5eead4` / `#7dd3fc`** (buttons, active line, links, tickers) so it does NOT read as textbook AI-purple.
- **Signature (one, recurring):** the `@property --angle` conic **border-beam** — traces the primary Summary button and reappears as the halo on the currently-playing transcript line. Glow blur ≤ 20px.
- Restrained **shimmer** on the title only (white light-sweep over solid text, not rainbow gradient text).
- **Spotlight** cards in the Speakers rail (pointer-tracked radial). Talk-share via **number ticker**.
- Progressive-**blur** edge fades at top/bottom of the transcript scroll so text melts into the night.
- lenis **auto-follows** the active line during (fake) playback, parked ~40% from top.

### Type
- Display: **Bricolage Grotesque**; body: **Hanken Grotesk**; mono: **JetBrains Mono** (timestamps/⌘K/clock). (Deliberately off the AI-convergent Inter/Geist/Space-Grotesk set.)
- Display line-height ~1.05–1.15, tracking ~-0.02em. Body 1.5–1.7, tracking ~0. Reading measure 55–75ch.

### Craft rules (installed skills enforce these)
- Easing tokens reused everywhere: `--ease-out: cubic-bezier(0.22,1,0.32,1)`, `--ease-in-out: cubic-bezier(0.77,0,0.175,1)`, `--ease-drawer: cubic-bezier(0.32,0.72,0,1)`. Never ease-in on UI.
- UI motion < 300ms (press 100–160, popover 125–200, menus/tabs 150–260, rail 220–460). Exit faster than enter.
- Animate transform/opacity ONLY. No `transition: all`. Press `scale(0.97)`. Entrances from `scale(0.95)` (never 0).
- Popovers grow from trigger; palette/modals from center. Springs: bounce 0.1–0.3, reserve overshoot for tab pill / drag only.
- prefers-reduced-motion: keep opacity, drop transforms/loops. Gate hover transforms behind `(hover:hover) and (pointer:fine)`.
- No AI tells: no ≥2px colored side-accent on cards, no gradient text on headings (shimmer is the one exception), no icon-tile-stack, no fake-liveness pulsing dots, no em-dashes in UI chrome copy (transcript dialogue in data is real content, exempt).

### Features that must keep a home
4 tabs (Overview→Summary rail, Transcript=left pane, Insights→stats in Speakers rail, Ask=rail) · editable title · Summary/Tidy/Sync/Export/More · meta chips · speaker rename + share% + recluster · find-in-transcript (filter + `<mark>` + count) · font size · seekable timestamps · transport (play/clock/waveform/track toggles) · privacy assurance (subtle).

---

## OpenPencil build plan (post-restart, via `mcp__open-pencil__*` — 106 tools)

1. `new_document` / `create_page` — a 1440-wide artboard (plus a ~820 responsive check frame).
2. Establish the **token system** first (colors above, type scale, spacing, radii) — use variables/styles so the design is systematic, not ad-hoc.
3. Frames with **auto-layout** (`set_layout` / `set_layout_child`): top bar → body row [left transcript pane | minimap seam | right rail] → left-pane bottom transport.
4. **Components** (`create_component` / `create_instance`) for the repeated units: a transcript turn (timestamp + speaker cue + dialogue), a rail summary card, an action-item row, a speaker row, the tab segmented control, the border-beam button.
5. Fill / stroke / effects for the glass + aurora + beam signature (`set_fill`, `set_effects`, `set_stroke`, `set_radius`, `set_opacity`).
6. Real dummy content from `ui-demos/data.js` (Q3 Roadmap Sync, You/Priya/Marcus, 20 turns).
7. `render` to eyeball; iterate with **apple-design**, **emil-design-eng**, **impeccable** (`/impeccable audit|critique|polish`), and **taste-skill** actively critiquing.
8. `get_jsx` / codegen → export → adapt into a new self-contained demo (`ui-demos/converged.html`) for side-by-side with Aurora + Split Focus, before anything touches the real `templates/index.html`.

## Reference demos (identical dummy data)
- Aurora: `ui-demos/aurora.html` — the winning skin
- Split Focus: `ui-demos/split-focus.html` — the winning structure/scroll
- Shared data: `ui-demos/data.js`
- Local server: `http://127.0.0.1:8777`
