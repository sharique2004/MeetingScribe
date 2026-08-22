---
name: MeetingScribe
description: Local-first meeting transcription for the Mac; a product-first landing where six real screenshots of the shipped app carry the page inside its converged aurora world.
colors:
  night: "#0a0a12"
  ink: "#eef1f9"
  ink-dim: "rgba(226, 232, 248, 0.72)"
  ink-faint: "rgba(226, 232, 248, 0.62)"
  aqua: "#5eead4"
  sky: "#7dd3fc"
  hairline: "rgba(255, 255, 255, 0.08)"
  hairline-lit: "rgba(255, 255, 255, 0.14)"
  glass: "rgba(255, 255, 255, 0.03)"
  glass-raised: "rgba(255, 255, 255, 0.055)"
  console: "rgba(13, 13, 22, 0.88)"
  shot-ring: "rgba(255, 255, 255, 0.11)"
  shot-glow: "rgba(59, 130, 246, 0.10)"
  aurora-blue: "#3b82f6"
  aurora-iris: "#a5b4fc"
  aurora-lilac: "#c4b5fd"
typography:
  display:
    fontFamily: "Bricolage Grotesque, Hanken Grotesk, system-ui, sans-serif"
    fontSize: "clamp(40px, 6vw, 72px)"
    fontWeight: 700
    lineHeight: 1.02
    letterSpacing: "-0.03em"
  display-phone:
    fontFamily: "Bricolage Grotesque, Hanken Grotesk, system-ui, sans-serif"
    fontSize: "clamp(36px, 10vw, 44px)"
    fontWeight: 700
    lineHeight: 1.02
    letterSpacing: "-0.03em"
  headline:
    fontFamily: "Bricolage Grotesque, Hanken Grotesk, system-ui, sans-serif"
    fontSize: "clamp(22px, 2.6vw, 30px)"
    fontWeight: 600
    lineHeight: 1.15
    letterSpacing: "-0.02em"
  title:
    fontFamily: "Bricolage Grotesque, Hanken Grotesk, system-ui, sans-serif"
    fontSize: "20px"
    fontWeight: 600
    letterSpacing: "-0.02em"
  lead:
    fontFamily: "Hanken Grotesk, system-ui, -apple-system, sans-serif"
    fontSize: "clamp(16px, 1.8vw, 18px)"
    fontWeight: 400
    lineHeight: 1.6
  body:
    fontFamily: "Hanken Grotesk, system-ui, -apple-system, sans-serif"
    fontSize: "16px"
    fontWeight: 400
    lineHeight: 1.6
  button:
    fontFamily: "Hanken Grotesk, system-ui, -apple-system, sans-serif"
    fontSize: "14.5px"
    fontWeight: 600
  ui:
    fontFamily: "Hanken Grotesk, system-ui, -apple-system, sans-serif"
    fontSize: "13.5px"
    fontWeight: 500
  mono:
    fontFamily: "JetBrains Mono, ui-monospace, SFMono-Regular, Menlo, monospace"
    fontSize: "13.5px"
    fontWeight: 400
  mono-small:
    fontFamily: "JetBrains Mono, ui-monospace, SFMono-Regular, Menlo, monospace"
    fontSize: "12px"
    fontWeight: 500
    lineHeight: 1.6
  label:
    fontFamily: "JetBrains Mono, ui-monospace, SFMono-Regular, Menlo, monospace"
    fontSize: "11px"
    fontWeight: 500
    letterSpacing: "0.08em"
rounded:
  chip: "7px"
  sm: "10px"
  md: "12px"
  lg: "14px"
  xl: "16px"
  window: "2.8% / 4.5%"
  pill: "99px"
spacing:
  hairpad: "4px"
  xs: "8px"
  sm: "12px"
  md: "20px"
  card: "18px"
  gutter: "24px"
  caption: "28px"
  section: "64px"
  section-lg: "128px"
  story: "144px"
components:
  button-primary:
    backgroundColor: "linear-gradient(180deg, #71efdb, #43c4ae)"
    textColor: "#04251f"
    rounded: "{rounded.md}"
    height: "46px"
    padding: "0 22px"
  copy-chip:
    backgroundColor: "rgba(94, 234, 212, 0.12)"
    textColor: "{colors.aqua}"
    rounded: "{rounded.chip}"
    padding: "7px 14px"
  command-row:
    backgroundColor: "{colors.console}"
    textColor: "{colors.ink}"
    rounded: "{rounded.md}"
    height: "50px"
    padding: "0 8px 0 18px"
  screenshot:
    backgroundColor: "transparent"
    textColor: "{colors.ink}"
    rounded: "{rounded.window}"
  mac-card:
    backgroundColor: "{colors.glass}"
    textColor: "{colors.ink}"
    rounded: "{rounded.lg}"
    padding: "18px"
  nav-bar:
    backgroundColor: "rgba(10, 10, 18, 0.72)"
    textColor: "{colors.ink}"
    rounded: "{rounded.lg}"
    padding: "16px 18px"
---

# Design System: MeetingScribe

## Overview

**Creative North Star: "The App Under the Aurora"**

MeetingScribe's visual world is a developer's night desk: a near-black violet night (#0a0a12), a restrained blue-to-lilac aurora glowing only in the top third of the sky, and below it the product itself. The landing page's argument is six real screenshots of the shipped Mac app (Today, the set-up sheet, Notes, Transcript, Ask, the engine settings), captured from fixture data and shown at full column width; text is captions. Nothing on the page is a mock, nothing types, nothing pretends to be live. Everything that is not a screenshot sits on hairline glass: white fills at 3–5.5% opacity, 1px hairline borders, a single top-lit inset highlight.

The system is deliberately quiet so its one recurring signature lands: the conic `--angle` border-beam, a thin aqua/sky arc orbiting the install command row. Cool blue-violet atmosphere is decoration; warm aqua/mint is work. Every interactive or affirmative element (links, the primary button, the Copy chip) speaks aqua. This world spans the shipped Mac app; the landing is the app's first screen, built from the same materials with the same restraint, and since 2026-08-22 it shows the app rather than describing it.

**Key Characteristics:**
- Night base with an aurora wash confined to the top third; never behind reading columns at full strength
- Real screenshots as the proof, at full column width so the app's own type stays legible
- Hairline glass surfaces, flat by default; tinted shadows only under the screenshots
- One signature: the animated conic border-beam on the command row
- Three typographic voices: Bricolage Grotesque declares, Hanken Grotesk explains, JetBrains Mono measures
- A copy budget: a six-word headline, one sentence, one line per screen

## Colors

A dark violet-black field, ink at three opacities, a working aqua/sky accent pair, and an atmospheric aurora trio that never touches controls.

### Primary
- **Working Aqua** (#5eead4): the product's voice. Links, the primary button gradient, the Copy chip, focus rings (rgba(94, 234, 212, 0.85)), and one half of the border-beam.
- **Signal Sky** (#7dd3fc): aqua's partner. The `$` prompt on the command row and the second stop of the border-beam. Always adjacent to aqua, never a competing accent.

### Secondary
- **Aurora Blue** (#3b82f6), **Aurora Iris** (#a5b4fc), **Aurora Lilac** (#c4b5fd): atmosphere only. They appear exclusively as low-alpha radial washes (0.24 / 0.19 / 0.13) in the page's top 90vh, fading to transparent by ~60% of each gradient, and as the blue tint of the screenshot shadow (shot-glow). Never on text, borders, or interactive elements.

### Neutral
- **Night** (#0a0a12): the only page background.
- **Console** (rgba(13, 13, 22, 0.88)): the darker translucent surface of the command row.
- **Signal Ink** (#eef1f9): headings, emphasized copy, the command text.
- **Dimmed Ink** (rgba(226, 232, 248, 0.72)): the hero sentence and section lines.
- **Faint Ink** (rgba(226, 232, 248, 0.62)): numerals, requirements lines, helper text, footer and nav links at rest. Raised from 0.48 on 2026-08-22 so small mono text clears AA on night.
- **Hairline / Hairline Lit** (rgba(255,255,255,0.08) / 0.14): all borders and the section dividers; the lit value is the hover border.
- **Glass / Raised Glass** (rgba(255,255,255,0.03) / 0.055): panel fills (the phone's Mac card).
- **Shot Ring** (rgba(255,255,255,0.11)): the 1px ring that follows a screenshot's transparent window corners.

### Named Rules
**The Aqua Does the Work Rule.** Cool blue-violet is weather; aqua/sky is machinery. Anything a visitor can click, copy, or read as confirmation is aqua or sky. Aurora hues never appear on an interactive element.

**The Top-Third Wash Rule.** The aurora exists as pointer-events-none radial gradients pinned to the top of the page, each dissolving to transparent by ~60%. It sets atmosphere behind the hero and must never compete with running text.

## Typography

**Display Font:** Bricolage Grotesque (with Hanken Grotesk, system-ui fallback)
**Body Font:** Hanken Grotesk (with system-ui fallback)
**Label/Mono Font:** JetBrains Mono (with ui-monospace, Menlo fallback)

**Character:** A deliberately non-convergent trio (no Inter, no Geist). Bricolage brings warmth and slight eccentricity to tight, heavy headlines; Hanken is a plainspoken explainer; JetBrains Mono carries anything the machine says or measures.

### Hierarchy
- **Display** (700, clamp(40px, 6vw, 72px), 1.02, -0.03em): the six-word hero headline only, balanced wrapping, capped at 20ch. On phones **Display-phone** (clamp(36px, 10vw, 44px)) keeps three lines.
- **Headline** (600, clamp(22px, 2.6vw, 30px), 1.15, -0.02em): the section heads ("Who said what, when.", "Install it.").
- **Title** (600, 20px, -0.02em): the phone Mac card's title.
- **Lead** (400, clamp(16px, 1.8vw, 18px), 1.6): the hero sentence, dimmed ink, 560px measure.
- **Body** (400, 16px, 1.6): the one line under each section head, dimmed ink, 44ch measure.
- **Button** (600, 14.5px): buttons and the aqua action links.
- **UI** (500, 13.5px): nav links, footer links, the DMG text link, helper text.
- **Mono** (400, 13.5px): the install command, the prompt, the revealed URL.
- **Mono-small** (500, 12px, 1.6): the Copy chip, requirements lines, the DMG size.
- **Label** (500, 11px, 0.08em, UPPERCASE, mono): section numerals ("01"). Always faint ink.

### Named Rules
**The Measured-In-Mono Rule.** If a string is executable, counted, or a system requirement, it is set in JetBrains Mono. Prose never wears mono; measurements never wear the sans.

**The Copy Budget Rule.** Headline at most 8 words, hero sentence at most 22, each section a heading of at most 7 words plus one sentence of at most 18. No adjectives about the product, no exclamation marks, no em dashes. Every factual claim must be verifiable from the repository (README.md, LICENSE, the GitHub release assets).

## Layout

A single centered column, max-width 1020px (the hero and its screenshot stretch to 1180px), on 24px page gutters (18px below 720px). The page is one scroll story: sticky glass nav (top offset 10px), centered hero (headline, sentence, command row, DMG line), the Notes screenshot, then five story sections separated by hairline dividers, a centered install block, and a hairline-topped footer.

- **Story section:** a caption row (`grid-template-columns: 1fr 1fr`, 48px gap, end-aligned: numeral and headline left, the one line right at 44ch) 28px above a full-width screenshot. 64px vertical padding, 144px before the first section, 128px before the install block. Below 860px the caption row stacks and sections compress to 40px.
- **Screenshots are never side by side.** At half column width the app's 13px UI type falls under 7px; every screenshot gets the full column.
- **Phone:** left-aligned hero, the Mac card under the sentence, the same screenshots stacked at 32px intervals, the Mac card again after the story. No install path on phones.
- **Responsive breakpoints:** 860px (caption grid collapses), 720px (gutters tighten, nav links hide), 480px (the DMG line stacks).

## Elevation & Depth

Flat glass by default: depth comes from layered translucency plus a 1px top-lit inset highlight, not from ambient drop shadows. Cards, nav, and the command row cast nothing.

### Shadow Vocabulary
- **Top-light** (`inset 0 1px 0 rgba(255,255,255,0.05)`): the universal glass edge; every panel carries it.
- **Shot ring + shadow** (`0 0 0 1px rgba(255,255,255,0.11), 0 30px 90px rgba(59,130,246,0.10), 0 12px 40px rgba(0,0,0,0.6)`): under every screenshot; the blue-tinted layer ties the window to the aurora. On phones the black layer is dropped.
- **Hero halo**: a blurred (40px) aqua/sky radial pair behind the hero screenshot only, inset -120px, z-index -1. Hidden on phones.
- **Accent glow** (`0 10px 32px rgba(94,234,212,0.22)`): under the primary button only.

### Named Rules
**The Screenshot Shadow Rule.** Only the screenshots and the primary button cast shadows. Informational glass floats flat on the night; the app sits heavy on it.

## Shapes

Rounded-rectangle language throughout, scaled to mass: 7px the Copy chip and focus rings, 12px buttons and the command row (and the beam frame that wraps it), 14px the Mac card and the nav capsule, 99px pills. Screenshots keep the corner the app itself has: the WebP is captured from the window buffer with transparent corners, and the 1px ring follows them with `border-radius: 2.8% / 4.5%` (36px at 1280 wide), so the ring scales with the image. Borders are always 1px hairlines; there are no hard corners, no thick strokes, no clipping tricks.

**The One Beam Rule.** The conic border-beam (`@property --angle`, 1px conic-gradient padding-mask, transparent through 0.62turn, aqua 0.9-alpha at 0.72turn, sky at 0.78turn, gone by 0.88turn; 9s linear spin at 0.5 opacity; a blurred 9px twin behind it) is the single recurring signature. On the landing it traces exactly one element, the install command row, which appears twice (hero and install block). It never multiplies onto screenshots or cards.

## Components

### Buttons
- **Shape:** softly rounded (12px), 46px tall, 0 22px padding, 600 weight at 14.5px. The phone's full-width primary is 48px tall.
- **Primary:** vertical aqua-mint gradient (linear-gradient(180deg, #71efdb, #43c4ae)) with deep-teal text (#04251f) and the accent glow shadow. Borderless.
- **Text link (DMG):** no fill, no border; dimmed ink at 13.5px with a low-alpha underline that brightens on hover. The DMG is the secondary path, so it never competes with the command row.
- **Hover / Active:** primary brightens (filter: brightness(1.06)); press is scale(0.97), gated behind `(hover: hover) and (pointer: fine)`. Transitions 120–150ms on the shared ease-out.

### Copy chip
Aqua-tinted fill (rgba(94,234,212,0.12)), aqua hairline (0.38), 7px radius, aqua mono at 12px, 7px 14px padding; hover deepens both. Flips to "Copied" for 1.6s and announces it through a polite live region. The same control, styled as the full-width primary, copies the site link on phones; if the clipboard is refused it reveals the URL as selectable mono text.

### Command row (signature component)
Console surface (rgba(13,13,22,0.88), 12px radius, hairline border, 50px tall): a sky `$` prompt, the real install command in 13.5px mono (nowrap, the row scrolls, never the page), and the Copy chip. Wrapped in the border-beam. Beneath it one wrapping row: the DMG text link with its mono size, and the mono requirements line.

### Screenshots (signature component)
`<figure>` with a single `<img>`: 1280×800 WebP plus a @2x twin via `srcSet`, explicit `width`/`height` so nothing shifts, the hero eager with `fetchpriority="high"`, the rest lazy. Alt text says what the screen shows, including the names on it. The ring-and-shadow stack above, corners following the image. Captured from a showcase data set (the synthesized demo meeting under fictional titles) so no real meeting or person appears.

### Mac card (phone)
Glass panel, hairline, 14px radius, 18px padding: "It's a Mac app." in Title, the full-width Copy the link primary, the mono requirements line with the bare domain, then the aqua "Open your synced meetings →" action link and one helper line in UI size.

### Navigation
- **Style:** a sticky floating glass capsule (rgba(10,10,18,0.72), 12px backdrop-blur, hairline border, 14px radius, top offset 10px, 56px min height). Brand mark = aqua mic glyph + Bricolage 600 wordmark at 16px.
- **Links:** faint ink 13.5px/500 resting, brightening to full ink on hover (150ms); the app-entry link is aqua 600 at 14.5px. Below 720px the plain link hides; on phones the nav carries the brand alone.

### Motion (applies across components)
One easing token, `cubic-bezier(0.22, 1, 0.32, 1)` (ease-out), everywhere. Micro-interactions 120–150ms; scroll reveals are one-shot IntersectionObserver flips: opacity + 14px rise over 0.6s per section. `prefers-reduced-motion` keeps a 0.3s opacity fade, drops the rise, and stops the beam. Only transform, opacity, color, border-color, and filter animate. Nothing types, nothing pulses, nothing pretends to be live.

## Do's and Don'ts

### Do:
- **Do** route every interactive or affirmative element through Working Aqua (#5eead4) / Signal Sky (#7dd3fc); atmosphere stays blue-violet, work stays aqua.
- **Do** build every non-screenshot surface as hairline glass: ≤5.5% white fill, 1px rgba(255,255,255,0.08) border, inset top-light.
- **Do** set commands, counts, sizes and requirements in JetBrains Mono, in faint or dimmed ink.
- **Do** show the product as real screenshots of the shipped app, captured from fixture data, at full column width, with honest alt text.
- **Do** keep the copy budget; cut words before adding sections.
- **Do** reuse the single ease-out, keep micro-motion under 300ms, and honor prefers-reduced-motion by dropping transforms and loops while keeping opacity.

### Don't:
- **Don't** rebuild the app in CSS, type a fake terminal, animate captions, or draw talk-time bars; the screenshots are the proof.
- **Don't** place two screenshots side by side; the app's own type must stay legible.
- **Don't** use gradient text on headings; the primary button's aqua-mint fill is the only gradient on a foreground element.
- **Don't** add a second signature: the border-beam traces the command row and nothing else.
- **Don't** ship AI tells: no ≥2px colored side-accents on cards, no icon-tile stacks, no fake-liveness pulsing dots.
- **Don't** use em dashes anywhere in site copy (user mandate, matching the app brief).
- **Don't** let aurora hues (#3b82f6 / #a5b4fc / #c4b5fd) reach text, borders, or controls, or extend the wash below the page's top third at legibility-affecting strength.
- **Don't** state a number the repository cannot back: licence from LICENSE, download size from the GitHub release, speed from README.
