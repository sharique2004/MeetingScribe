---
name: MeetingScribe
description: Local-first meeting transcription for the Mac; a terminal-first landing that inhabits the shipped app's converged aurora world.
colors:
  night: "#0a0a12"
  ink: "#eef1f9"
  ink-dim: "rgba(226, 232, 248, 0.72)"
  ink-faint: "rgba(226, 232, 248, 0.48)"
  aqua: "#5eead4"
  sky: "#7dd3fc"
  hairline: "rgba(255, 255, 255, 0.08)"
  hairline-lit: "rgba(255, 255, 255, 0.14)"
  glass: "rgba(255, 255, 255, 0.03)"
  glass-raised: "rgba(255, 255, 255, 0.055)"
  console: "rgba(13, 13, 22, 0.88)"
  aurora-blue: "#3b82f6"
  aurora-iris: "#a5b4fc"
  aurora-lilac: "#c4b5fd"
  speaker-blue: "#5b8cff"
  speaker-violet: "#c07af6"
  speaker-green: "#2fbf87"
typography:
  display:
    fontFamily: "Bricolage Grotesque, Hanken Grotesk, system-ui, sans-serif"
    fontSize: "clamp(38px, 6.4vw, 68px)"
    fontWeight: 700
    lineHeight: 1.04
    letterSpacing: "-0.025em"
  headline:
    fontFamily: "Bricolage Grotesque, Hanken Grotesk, system-ui, sans-serif"
    fontSize: "clamp(24px, 3.2vw, 34px)"
    fontWeight: 600
    letterSpacing: "-0.02em"
  title:
    fontFamily: "Bricolage Grotesque, Hanken Grotesk, system-ui, sans-serif"
    fontSize: "clamp(20px, 2.4vw, 26px)"
    fontWeight: 600
    lineHeight: 1.2
    letterSpacing: "-0.02em"
  body:
    fontFamily: "Hanken Grotesk, system-ui, -apple-system, sans-serif"
    fontSize: "15px"
    fontWeight: 400
    lineHeight: 1.65
  label:
    fontFamily: "JetBrains Mono, ui-monospace, SFMono-Regular, Menlo, monospace"
    fontSize: "10px"
    fontWeight: 500
    lineHeight: 1.4
    letterSpacing: "0.08em"
  mono:
    fontFamily: "JetBrains Mono, ui-monospace, SFMono-Regular, Menlo, monospace"
    fontSize: "13.5px"
    fontWeight: 400
    lineHeight: 2.05
rounded:
  chip: "7px"
  sm: "10px"
  md: "12px"
  lg: "14px"
  xl: "16px"
  pill: "99px"
spacing:
  hairpad: "4px"
  xs: "8px"
  sm: "12px"
  md: "20px"
  card: "22px"
  gutter: "24px"
  section: "84px"
  section-lg: "96px"
components:
  button-primary:
    backgroundColor: "linear-gradient(180deg, #71efdb, #43c4ae)"
    textColor: "#04251f"
    rounded: "{rounded.md}"
    height: "46px"
    padding: "0 22px"
  button-ghost:
    backgroundColor: "{colors.glass}"
    textColor: "{colors.ink}"
    rounded: "{rounded.md}"
    height: "46px"
    padding: "0 22px"
  copy-chip:
    backgroundColor: "{colors.glass-raised}"
    textColor: "{colors.aqua}"
    rounded: "{rounded.chip}"
    padding: "4px 12px"
  card-glass:
    backgroundColor: "{colors.glass}"
    textColor: "{colors.ink}"
    rounded: "{rounded.lg}"
    padding: "20px 22px"
  console-surface:
    backgroundColor: "{colors.console}"
    textColor: "{colors.ink}"
    rounded: "{rounded.xl}"
    padding: "20px 22px 22px"
  nav-bar:
    backgroundColor: "rgba(10, 10, 18, 0.72)"
    textColor: "{colors.ink}"
    rounded: "{rounded.lg}"
    padding: "16px 18px"
---

# Design System: MeetingScribe

## Overview

**Creative North Star: "The Terminal Under the Aurora"**

MeetingScribe's visual world is a developer's night desk: a near-black violet night (#0a0a12), a restrained blue-to-lilac aurora glowing only in the top third of the sky, and below it, machines that actually run. The landing page's centerpiece is not a screenshot but a working terminal that types the real install command and prints a truthful transcript; the app window, the phone, the talk-time bars are all rendered as live instruments, not marketing art. Everything sits on hairline glass: white fills at 3–5.5% opacity, 1px hairline borders, a single top-lit inset highlight.

The system is deliberately quiet so its one recurring signature lands: the conic `--angle` border-beam, a thin aqua/sky arc orbiting the terminal and the install command. Cool blue-violet atmosphere is decoration; warm aqua/mint is work. Every interactive or affirmative element (links, primary button, checkmarks, the caret, live-line halos) speaks aqua, which keeps the page from reading as textbook AI-purple. This world spans the shipped Mac app (templates/index.html, ui-demos/DESIGN_BRIEF.md); the landing is the app's first screen, built from the same materials with the same restraint.

**Key Characteristics:**
- Night base with an aurora wash confined to the top third; never behind reading columns at full strength
- Hairline glass surfaces, flat by default; deep black shadows only under the "machine" artifacts
- One signature: the animated conic border-beam, aqua-to-sky, glow-blurred twin layer
- Three typographic voices: Bricolage Grotesque declares, Hanken Grotesk explains, JetBrains Mono measures
- Honest instruments over marketing apparatus: real commands, real output, labeled demo data

## Colors

A dark violet-black field, ink at three opacities, a working aqua/sky accent pair, and an atmospheric aurora trio that never touches controls.

### Primary
- **Working Aqua** (#5eead4): the product's voice. Links, the primary button gradient, terminal checkmarks, the typing caret, focus rings (rgba(94, 234, 212, 0.75)), the live-turn halo, and one half of the border-beam.
- **Signal Sky** (#7dd3fc): aqua's partner. The terminal prompt `$` and the second stop of the border-beam. Always adjacent to aqua, never a competing accent.

### Secondary
- **Aurora Blue** (#3b82f6), **Aurora Iris** (#a5b4fc), **Aurora Lilac** (#c4b5fd): atmosphere only. They appear exclusively as low-alpha radial washes (0.26 / 0.20 / 0.14) in the page's top third, fading to transparent by ~60% of each gradient. Never on text, borders, or interactive elements.

### Tertiary
- **Speaker Blue / Violet / Green** (#5b8cff / #c07af6 / #2fbf87): functional identity colors for meeting participants. Used consistently for a given speaker across the transcript name, talk-share bar, and action-item owner dot. They are data ink, not accents.

### Neutral
- **Night** (#0a0a12): the only page background.
- **Console** (rgba(13, 13, 22, 0.88)): the darker translucent surface of machine artifacts (terminal, app window at 0.82, phone at 0.9, install command).
- **Signal Ink** (#eef1f9): headings, emphasized copy, terminal input.
- **Dimmed Ink** (rgba(226, 232, 248, 0.72)): body paragraphs, terminal output, transcript dialogue.
- **Faint Ink** (rgba(226, 232, 248, 0.48)): labels, requirements lines, timestamps, footer links, nav links at rest.
- **Hairline / Hairline Lit** (rgba(255,255,255,0.08) / 0.14): all borders; the lit value is the hover/emphasis border.
- **Glass / Raised Glass** (rgba(255,255,255,0.03) / 0.055): panel fills; raised glass marks slightly elevated interior surfaces (rails, chips).

### Named Rules
**The Aqua Does the Work Rule.** Cool blue-violet is weather; aqua/sky is machinery. Anything a visitor can click, copy, or read as confirmation is aqua or sky. Aurora hues never appear on an interactive element.

**The Top-Third Wash Rule.** The aurora exists as pointer-events-none radial gradients pinned to the top of the page, each dissolving to transparent by ~60%. It sets atmosphere behind the hero and must never compete with running text.

## Typography

**Display Font:** Bricolage Grotesque (with Hanken Grotesk, system-ui fallback)
**Body Font:** Hanken Grotesk (with system-ui fallback)
**Label/Mono Font:** JetBrains Mono (with ui-monospace, Menlo fallback)

**Character:** A deliberately non-convergent trio (no Inter, no Geist). Bricolage brings warmth and slight eccentricity to tight, heavy headlines; Hanken is a plainspoken explainer; JetBrains Mono carries anything the machine says or measures.

### Hierarchy
- **Display** (700, clamp(38px, 6.4vw, 68px), 1.04, -0.025em): the dual-claim hero headline only. Balanced wrapping (`text-wrap: balance`).
- **Headline** (600, clamp(24px, 3.2vw, 34px), -0.02em): section openers ("Then it opens.", "Install it").
- **Title** (600, clamp(20px, 2.4vw, 26px), 1.2, -0.02em): proof-row heads; balanced wrapping.
- **Body** (400, 15px, 1.65): paragraphs; proof-row copy capped at 42ch, lead paragraphs at 560–640px containers.
- **Label** (500, 10px, 0.08em, UPPERCASE, mono): section micro-labels inside panels ("Summary", "talk time · demo"). Always faint ink.
- **Mono** (400, 13.5px, 2.05 in the terminal; 10.5–12.5px elsewhere): commands, prompts, timestamps, durations, percentages, requirements lines, window titles.

### Named Rules
**The Measured-In-Mono Rule.** If a string is executable, timestamped, counted, or a system requirement, it is set in JetBrains Mono. Prose never wears mono; measurements never wear the sans.

## Layout

A single centered column, max-width 1020px, on 24px page gutters (18px below 720px). The page is one scroll story: sticky glass nav (top offset 10px), centered hero, monumental terminal (max-width 720px), app-window payoff, alternating two-column proof rows, install section, hairline-topped footer.

- **Proof rows:** `grid-template-columns: 1fr 1fr` with `gap: clamp(28px, 5vw, 64px)`, 44px vertical padding; alternate rows flip text/art order via `.flip`. Below 860px they stack single-column (text first, flip neutralized) with 20px gap.
- **Artifact widths:** terminal 720px, install block 680px, command bar 620px, quiet captions 620px, hero sub 640px. The narrowing widths keep reading measures honest inside the wide column.
- **Section rhythm:** 84–96px between major sections on desktop, compressed to 64–72px below 720px.
- **App window interior:** 1.55fr / 1fr split (transcript / summary rail), collapsing to one column below 860px with the divider rotating from right border to bottom border.
- **Responsive breakpoints:** 860px (grids collapse) and 720px (gutters tighten, nav links hide, hero actions stack, terminal type drops to 12px).

## Elevation & Depth

Flat glass by default: depth comes from layered translucency (glass 3% over night, raised glass 5.5%, console surfaces at 82–90% dark) plus a 1px top-lit inset highlight, not from ambient drop shadows. Cards, nav, and panels cast nothing.

### Shadow Vocabulary
- **Top-light** (`inset 0 1px 0 rgba(255,255,255,0.05–0.07)`): the universal glass edge; every panel and console surface carries it.
- **Machine shadow, terminal** (`0 30px 90px rgba(0,0,0,0.55)`): under the hero terminal.
- **Machine shadow, window** (`0 40px 110px rgba(0,0,0,0.55)`): under the app-window payoff.
- **Machine shadow, phone** (`0 20px 60px rgba(0,0,0,0.5)`): under the phone artifact.
- **Accent glow** (`0 10px 32px rgba(94,234,212,0.22)`): under the primary button only.
- **Live halo** (`0 0 0 1px rgba(94,234,212,0.32), 0 0 18px rgba(94,234,212,0.14)`): the currently-speaking transcript turn.

### Named Rules
**The Machine Shadow Rule.** Only the three "machine" artifacts (terminal, app window, phone) and the primary button cast shadows. Informational glass floats flat on the night; hardware sits heavy on it.

## Shapes

Rounded-rectangle language throughout, scaled to mass: 7px copy chips, 9–10px interior rows and turns, 11–12px buttons and command bars, 14px cards and the nav capsule, 15–16px machine windows, 17px the beam frame that wraps them, 22px the phone, 99px pills and progress bars. Borders are always 1px hairlines; there are no hard corners, no thick strokes, no clipping tricks beyond `overflow: hidden` on windowed surfaces. Machine artifacts carry macOS chrome: three 11px traffic-light dots (#e0604f / #d9a44a / #63c78a) and a centered mono title.

**The One Beam Rule.** The conic border-beam (`@property --angle`, 1px conic-gradient padding-mask, transparent through 0.62turn, aqua 0.9-alpha at 0.72turn, sky at 0.78turn, gone by 0.88turn; 7s linear spin; a blurred 9px, 0.5-opacity twin behind it) is the single recurring signature. On the landing it traces exactly two elements: the hero terminal and the install command bar. In the shipped app it traces the Summary button and the playing line. It never multiplies onto ordinary cards.

## Components

### Buttons
- **Shape:** softly rounded (12px), 46px tall, 0 22px padding, 600 weight at 14.5px.
- **Primary:** vertical aqua-mint gradient (linear-gradient(180deg, #71efdb, #43c4ae)) with deep-teal text (#04251f) and the accent glow shadow. Borderless.
- **Ghost:** glass fill ({colors.glass}) with hairline border and ink text; hover lifts the border to hairline-lit.
- **Hover / Active:** primary brightens (filter: brightness(1.06)); press is scale(0.97), gated behind `(hover: hover) and (pointer: fine)`; trailing arrow glyphs slide 3px right on hover. Transitions 120–150ms on the shared ease-out.

### Chips
- **Copy chip:** raised glass, hairline border, 7px radius, aqua mono text at 11.5px; hover tints the border toward aqua (rgba(94,234,212,0.5)); flips to "Copied" for 1.6s.
- **Window tag:** faint mono 10px in a hairline 99px pill ("demo meeting"); the honesty label for fixture data.

### Cards / Containers
- **Corner Style:** 14px (`.lp-art`), 20px 22px internal padding.
- **Background:** glass 3% on night; interior rails use glass, raised rows use rgba(10,10,18,0.4).
- **Shadow Strategy:** top-light inset only (see The Machine Shadow Rule).
- **Border:** 1px hairline.
- **Structure:** each card opens with an uppercase mono label, faint ink, 14px below it.

### Navigation
- **Style:** a sticky floating glass capsule (rgba(10,10,18,0.72), 12px backdrop-blur, hairline border, 14px radius, top offset 10px). Brand mark = aqua mic glyph + Bricolage 600 wordmark at 16.5px.
- **Links:** faint ink 13.5px/500 resting, brightening to full ink on hover (150ms); the app-entry link is aqua 600. Below 720px the plain links hide and only the aqua action remains.

### Terminal (signature component)
The hero artifact: console surface (rgba(13,13,22,0.88), 16px radius, hairline border, terminal machine shadow), traffic-light bar with centered mono title and a Copy chip, then a mono body (13.5px / 2.05, min-height 210px). The sky-colored `$` prompt precedes ink command text typed character-by-character (14ms/char after a 700ms beat); output lines land in dimmed ink at 330ms intervals, success lines prefixed with an aqua ✓; an aqua block caret (8×15px) blinks at 1.1s steps when idle. Wrapped in the border-beam. The transcript it prints mirrors what tools/install.sh actually does.

### App Window (signature component)
A faux macOS window on the console surface (15px radius, window machine shadow): titlebar with traffic lights, mono title, demo tag; 1.55fr/1fr body of transcript turns (44px mono timestamp gutter, speaker-colored 600-weight names, dimmed-ink dialogue) beside a summary rail (mono labels, decisions list, action rows with speaker-colored owner dots and mono due dates). The live turn gets the aqua halo and full-ink text with word-by-word reveal.

### Data Bars
Talk-share and speed comparisons: 6px 99px-pill tracks in rgba(255,255,255,0.06); fills are speaker colors or aqua (comparison baseline in rgba(226,232,248,0.35)); values in faint/dim mono, right-aligned. Fills scaleX from 0 over 0.9–1s on scroll reveal.

### Motion (applies across components)
One easing token, `cubic-bezier(0.22, 1, 0.32, 1)` (ease-out), everywhere. Micro-interactions 120–150ms; scroll reveals are one-shot IntersectionObserver flips: opacity + 14px rise over 0.6s, word-cascades at 55–70ms steps. `prefers-reduced-motion` keeps opacity fades, drops transforms, kills the beam spin, caret blink, typing, and bar transitions. Only transform, opacity, color, border-color, and filter animate.

## Do's and Don'ts

### Do:
- **Do** route every interactive or affirmative element through Working Aqua (#5eead4) / Signal Sky (#7dd3fc); atmosphere stays blue-violet, work stays aqua.
- **Do** build every surface as hairline glass: ≤5.5% white fill, 1px rgba(255,255,255,0.08) border, inset top-light.
- **Do** set commands, timestamps, durations, percentages, and requirements in JetBrains Mono, in faint or dimmed ink.
- **Do** label fixture content honestly (mono "demo" tags) and render product proof as working instruments with real values, never as static screenshots.
- **Do** reuse the single ease-out (cubic-bezier(0.22,1,0.32,1)), keep micro-motion under 300ms, and honor prefers-reduced-motion by dropping transforms and loops while keeping opacity.

### Don't:
- **Don't** use gradient text on headings; the primary button's aqua-mint fill is the only gradient on a foreground element. (The shipped app's white title shimmer is the world's one sanctioned exception; the landing ships none.)
- **Don't** add a second signature: the conic border-beam appears on at most two elements per surface, and its glow blur stays ≤ 20px.
- **Don't** ship AI tells: no ≥2px colored side-accents on cards, no icon-tile stacks, no fake-liveness pulsing dots. The blinking terminal caret and the static speaker-colored owner dots are native devices of this world and exempt.
- **Don't** use em dashes anywhere in site copy (user mandate, matching the app brief).
- **Don't** let aurora hues (#3b82f6 / #a5b4fc / #c4b5fd) reach text, borders, or controls, or extend the wash below the page's top third at legibility-affecting strength.
