---
version: 1
slug: "mobile-src-landing-jsx"
primary_target: "mobile/src/Landing.jsx"
related_targets: ["mobile/src/styles.css", "mobile/index.html", "mobile/public/shots"]
---

Scope: mobile/src/Landing.jsx (signed-out desktop landing; phone variant sells the Mac app and offers the synced viewer). Mode: Persuade.
Audience: developers and technical Mac users deciding whether to run a shell command from a stranger's site; they arrive from the portfolio or GitHub. On phones, the same people away from their Mac.
Job/action: see the product in seconds, understand it runs on the Mac, then run the install one-liner or download the DMG. On phones: copy the link to take to a Mac, or open synced meetings if they already record.
Proof on hand: six real screenshots of the shipped app in mobile/public/shots (home, record, notes, transcript, ask, settings; 1280x800 + @2x WebP, transparent window corners), captured 2026-08-22 from a showcase data set (the synthesized demo meeting under fictional titles, speakers Priya Nair / Marcus Bell). Verified claims: GPLv3 (LICENSE); DMG 225 MB (GitHub release v3.3.0 asset, decimal MB); a 45-minute meeting takes about a minute, on-device (README); Ask citations seek playback (FloatingBar.swift). Nothing leaves the Mac: stated once, in the hero sentence.
Constraints: user-pinned visual world = the shipped app's converged aurora system (night #0a0a12, glass hairline panels, restrained aurora wash, aqua #5eead4/#7dd3fc accent, conic --angle border-beam as the one signature, Bricolage/Hanken/JetBrains type). Copy budget per DESIGN.md (headline <= 8 words, sentence <= 22, section head <= 7 + one line <= 18). Zero em dashes. No icon-card grids, no CSS app mocks, no typed terminal, no fake liveness, nothing "live" on the phone variant. Keep onOpenApp/phone contract, /install.sh serving, InsForge deploy pipeline; App.jsx and the viewer CSS above the landing marker stay untouched.
Direction (2026-08-22, owner-requested redesign): PRODUCT-FIRST. First viewport = six-word headline, one sentence, the command row with Copy, the DMG text link and mono requirements, then the Notes screenshot at 1180px. Story = Today, set up, transcript, Ask, who writes the notes, each a numbered caption row over a full-width screenshot, hairline dividers between. Closing install block repeats the command row. Phone = same headline and screens stacked, the Mac card (Copy the link, Open your synced meetings) before and after the story.
Memorable moment: the first scroll is the app itself, at full width, with the user's own words in it.
Unresolved: the Today screenshot carries the owner's first name in its greeting; swap for a recapture if that ever bothers him. Recapture recipe in the project memory (product-screenshot-recipe).
