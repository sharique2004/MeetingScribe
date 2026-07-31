# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Mac-owning professionals who sit in meetings and interviews all day and do not trust cloud transcription bots: engineers, founders, consultants, job candidates. Situation: they discover MeetingScribe through the developer's portfolio or GitHub and land on meetingscribe.shariquekhatri.com deciding whether to install a free indie Mac app. A secondary visitor is the same person on their phone, signed in, reading transcripts they synced from their Mac (that surface is the companion viewer, not the landing page).

## Product Purpose

MeetingScribe records any call or conversation on a Mac, shows live captions, produces a speaker-labelled transcript, and turns it into an AI summary with action items, all running on the user's own machine. Success for the landing page: a visitor understands the local-first difference and either runs the install one-liner or downloads the DMG.

## Positioning

"Granola removed the bot from your meeting. MeetingScribe removes the cloud." No bot joins the call; audio is captured at the OS layer and never uploaded. User-confirmed lead story for the landing: privacy and speed carry EQUAL weight (dual-claim hero; 2026-07-31 interview).

## Operating Context

Distribution: self-contained .app (bundled CPython runtime) on GitHub Releases; primary install is `curl -fsSL https://meetingscribe.shariquekhatri.com/install.sh | sh` (zero Gatekeeper dialogs), secondary is the DMG with a macOS 26 "Open Anyway" walkthrough. Requirements: Apple Silicon, macOS 26 Tahoe, ~370 MB download (1.2 GB on disk), speech models fetched on first run. Optional BlackHole driver for system audio; summaries use the user's Claude CLI or the on-device Apple engine. The site is a Vite + React 18 SPA (plain CSS) deployed to InsForge hosting behind meetingscribe.shariquekhatri.com; /install.sh must keep serving from the site root; the signed-in viewer and phone routing in App.jsx must keep working.

## Capabilities and Constraints

Live captions, speaker diarization, AI summaries with action items, phone sync of text only. Claims the user approved for the landing (2026-07-31): a 45-minute call transcribes in about 90 seconds on the Apple Neural Engine; free and open source under MIT (github.com/sharique2004/MeetingScribe); nothing leaves the Mac (audio and transcripts stay local; only text the user chooses to sync reaches the phone viewer). The old hero capsule ("100% local · your audio never leaves your Mac") is banned by the user; the local claim may appear once, subtly, elsewhere. No em dashes anywhere in site copy (user mandate, matches the app design brief's ban).

## Brand Commitments

The landing must inhabit the shipped app's converged visual world (user-binding): night base #0a0a12, glass panels rgba(255,255,255,.03) with hairline top-lit borders, subtle aurora wash (#3b82f6/#60a5fa/#a5b4fc/#c4b5fd) confined and legibility-safe, working accent aqua/mint #5eead4 / #7dd3fc, conic `--angle` border-beam as the single recurring signature, restrained white shimmer on display titles only. Type: Bricolage Grotesque display, Hanken Grotesk body, JetBrains Mono for timestamps/commands. Motion per ui-demos/DESIGN_BRIEF.md easing tokens; no AI tells (no colored side-accents, no gradient text, no fake pulsing dots, no icon-tile stacks). Authority: ui-demos/DESIGN_BRIEF.md and the shipped templates/index.html.

## Evidence on Hand

Real demo meeting data for product mocks: ui-demos/data.js (Q3 Roadmap Sync; You/Priya/Marcus; 20 turns). Reference implementations of the app's skin: templates/index.html (shipped), ui-demos/converged.html, ui-demos/aurora.html. Release evidence: v2.0.0 on GitHub with 365 MB DMG. No testimonials, user counts, or press exist; do not fabricate any.

## Product Principles

- Privacy is the product, speed is the proof: every claim must reinforce that local-first costs nothing in capability.
- The site is the app's first screen: same world, same materials, same restraint as the shipped transcript UI.
- One command is the whole funnel: nothing on the page may upstage the install one-liner and DMG button.
- Honest indie: state requirements and the Gatekeeper reality plainly; never imitate big-vendor marketing apparatus.
