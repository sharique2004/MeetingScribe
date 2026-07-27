// The note-taker: a second Liquid Glass panel that stays on screen through a
// meeting holding the prepared talking points and whatever gets typed live —
// and, like the HUD, is invisible to anyone the screen is shared with.
//
// ── The focus contract (F1) ───────────────────────────────────────────────────
// A `.nonactivatingPanel` can hold the keyboard while the meeting app keeps
// frontmost status — measured, with another app frontmost: a real click on the
// note field leaves NSWorkspace.frontmostApplication unchanged, this panel
// becomes key, and real keystrokes arrive in the field. So typing here does not
// interrupt Zoom; it keeps running and keeps being recorded. Only the keyboard
// moves.
//
// What was actually broken: nothing ever *asked* for the keyboard. The panel
// was ordered front without a first responder, and the two read-only
// NSTextViews (talking points, notes so far) are `isSelectable`, so they take
// first responder and silently swallow every keystroke. Measured on the old
// build, with real HID keys and the field read back afterwards:
//     open the panel and type          -> "" (panel never became key)
//     click the talking points, type   -> "" (a read-only text view ate them)
//     click the panel body, type       -> "" (same)
//     click the 34pt field exactly     -> text arrives
// i.e. you had to hit one small target or the panel was dead. That is the
// "not able to type in the notes area" report.
//
// The contract now, explicit in both directions so nobody is ever trapped:
//     recording starts   -> the panel appears, the keyboard is NOT touched
//     ⌥⌘N / menu         -> the note field takes the keyboard (you asked for it
//                           by name), opening the panel first if it was hidden;
//                           press it again while typing to put the panel away
//     click anywhere on it -> the note field takes the keyboard, on purpose
//     type into a reading pane -> redirected into the note field
//     Return             -> saves the note and hands the keyboard straight back
//     Escape             -> hands the keyboard straight back, draft kept
//
// ── The durability contract (N1–N3) ──────────────────────────────────────────
// A note the owner typed must never be lost, and must never be filed against a
// meeting they did not type it in. Those are two separate promises and each has
// its own mechanism.
//
// NEVER LOST. The live NSTextField used to be the only copy of an unconfirmed
// note: nothing wrote it anywhere until something called saveDraft(), and the
// only callers were hide() and Escape. Quit, crash, or any teardown that did
// not run those and the words were gone.
//
// Every keystroke now reaches disk under a MAXIMUM STALENESS bound: the field
// is written the moment a key lands unless the last successful write was less
// than `draftMaxStaleness` ago, in which case a write is armed for the moment
// that budget expires. The deadline is anchored to the last WRITE and is never
// pushed back by later keystrokes.
//
// That last sentence is the whole fix. This used to be a debounce — leading
// edge, then a one-shot timer restarted on every keystroke — and a debounce
// that restarts on every keystroke never fires for somebody who does not stop
// typing. Measured on the old build at 200 ms/key, which is ordinary 60 wpm
// typing, no pauses:
//     typed into the field  : "Ship the revised pricing deck to Alex before
//                              Friday standup"   (59 chars)
//     on disk at that moment: "S"                (1 char)
// i.e. a `kill -9` cost 58 of 59 characters, and it cost them from the FAST
// typist — the one with the most to lose. Slow typing (400 ms/key) reached the
// trailing edge and saved fine, which is why the earlier durability test, which
// typed with pauses, passed.
//
// A draft write was measured at 0.278 ms mean / 1.155 ms worst (NoteDraftStore
// .writeAtomically), so the bound is set below one keystroke interval for
// ordinary typing: at 100 ms every key slower than 10/second is on disk the
// instant it is typed, and a burst faster than that (autorepeat, a paste
// storm) coalesces to at most ten writes a second — ~3 ms of main thread per
// second of typing, worst case.
//
// NEVER MISFILED. Every note carries the id of the meeting it was typed in, and
// so does its draft. The exact wire contract, so both halves of this feature
// agree (app.py's /api/record/note documents the same shape):
//
//     POST /api/record/note
//     {"text": "…",                       always
//      "meeting_id": "YYYYMMDD-HHMMSS",   omitted only when the note was typed
//                                         with no meeting in view, which is the
//                                         one case where "whatever is live" is
//                                         what the owner means
//      "t": 123.4}                        seconds in; omitted once the meeting
//                                         clock has stopped, so the backend
//                                         stamps it with the meeting's real
//                                         duration instead of a time that never
//                                         happened
//
// The text in the field is bound to a meeting the moment it stops being empty,
// and that binding is never re-pointed at a later meeting. So a note left in
// the field when a meeting ends is flushed to THAT meeting (at Stop, and again
// if a new recording starts), and a draft that outlived a crash is posted to
// the meeting it was typed in — never to whatever happens to be recording when
// the app comes back.
//
// Notes are never dropped, and never posted without being confirmed *during a
// meeting*. Return is the only thing that publishes a note while a meeting is
// running. A failed POST leaves the text in the field and says so; hiding the
// panel keeps it as a draft. The end of the meeting is the one exception, and
// it is deliberate: at that point the note can only belong to the meeting that
// just ended, so it is filed rather than left to rot as a draft.

import AppKit
import Carbon.HIToolbox

// MARK: - Aurora palette (this panel only)

/// The note-taker's own skin — the web app's "Aurora" treatment: deep night
/// glass, an aqua/mint accent, calmer type. The recording HUD and the main
/// window keep the product's default `MS` palette, so this is scoped here on
/// purpose: restyling the notes panel must not disturb the other two surfaces
/// that share `MS`. AppKit is not CSS, so the web tokens are approximated with
/// SF Pro / SF Mono (never a bundled font) and painted onto the shared
/// `GlassBox`.
private enum Aurora {
    private static func hex(_ value: UInt32, _ alpha: CGFloat = 1) -> NSColor {
        NSColor(srgbRed: CGFloat((value >> 16) & 0xFF) / 255,
                green: CGFloat((value >> 8) & 0xFF) / 255,
                blue: CGFloat(value & 0xFF) / 255,
                alpha: alpha)
    }

    // Surfaces — deep night glass. A #0A0A12 base frosts the GlassBox; a
    // translucent #10101C panel is painted over it so the slab reads as dark
    // frosted glass rather than the default warm grey.
    static let base         = hex(0x0A0A12)
    static let glassTint    = hex(0x0A0A12, 0.40)             // frosts the GlassBox, cool + dark
    static let panelOverlay = hex(0x10101C, 0.66)            // painted over the glass surface
    static let inputFill    = NSColor.white.withAlphaComponent(0.05)   // a raised sheet of glass
    static let chipFill     = NSColor.white.withAlphaComponent(0.035)  // quieter still

    // Hairlines — ~8% white, a touch stronger where focus lands.
    static let border      = NSColor.white.withAlphaComponent(0.08)   // #FFFFFF14
    static let borderFocus = NSColor.white.withAlphaComponent(0.13)   // #FFFFFF22

    // Text
    static let text      = hex(0xF2F3F8)   // primary
    static let textDim   = hex(0xAEB4C2)   // secondary
    static let textFaint = hex(0x6B7079)   // timestamps, hints

    // Accent — aqua/mint, with a sky-blue second. Spent sparingly: the focus
    // ring and the title dot, never a wall of colour (a wall of it shouts).
    static let accent    = hex(0x5EEAD4)
    static let accent2   = hex(0x7DD3FC)

    // Radii — one consistent family of corners.
    static let rPanel: CGFloat = 14
    static let rCard:  CGFloat = 12
    static let rInput: CGFloat = 10
    static let rChip:  CGFloat = 7

    /// A label in Aurora's voice: SF Pro, one of the three text colours.
    static func label(_ text: String, size: CGFloat,
                      weight: NSFont.Weight = .regular,
                      color: NSColor) -> NSTextField {
        let field = NSTextField(labelWithString: text)
        field.font = MS.ui(size, weight)
        field.textColor = color
        return field
    }

    /// A small, faint, tracked-out section label — the web UI's `.sb-group`,
    /// tuned faint for Aurora.
    static func sectionHeaderText(_ text: String) -> NSAttributedString {
        NSAttributedString(string: text.uppercased(),
                           attributes: [.font: MS.ui(9.5, .semibold),
                                        .foregroundColor: textFaint,
                                        .kern: 1.0])
    }

    static func sectionHeader(_ text: String) -> NSTextField {
        let field = NSTextField(labelWithString: "")
        field.attributedStringValue = sectionHeaderText(text)
        return field
    }

    /// The bold panel title, with slightly tight tracking.
    static func title(_ text: String) -> NSTextField {
        let field = NSTextField(labelWithString: "")
        field.attributedStringValue = NSAttributedString(
            string: text,
            attributes: [.font: MS.ui(15, .bold),
                         .foregroundColor: Aurora.text,
                         .kern: -0.2])
        return field
    }
}

final class NotesPanel: NSObject, NSWindowDelegate, NSTextFieldDelegate {

    private let panel: FloatingPanel
    private let baseURL: URL
    private let box = GlassBox(corner: .radius(Aurora.rPanel), tint: Aurora.glassTint)

    private let cuesView = ReadingTextView()
    private let notesView = ReadingTextView()
    private let input = NoteField()
    private let statusLabel = NSTextField.msLabel("", size: 11, color: MS.danger)
    private let hintLabel = Aurora.label("Return saves · Esc gives the keyboard back",
                                         size: 10.5, color: Aurora.textFaint)
    private let notesHeader = Aurora.sectionHeader("Notes this meeting")

    /// Seconds into the meeting, supplied by the HUD's interpolated clock.
    var currentElapsed: (() -> TimeInterval)?
    /// Whether that clock is still running. Once the recording stops the clock
    /// is frozen (RecordingPanel), and a note committed afterwards must not be
    /// stamped with an extrapolated time — see `commit`.
    var clockIsLive: (() -> Bool)?
    var onVisibilityChange: ((Bool) -> Void)?

    private var cues: [String] = []
    private var notes: [(t: TimeInterval, text: String)] = []
    private var cuesHeight: NSLayoutConstraint!
    private var cuesScroll: NSScrollView!
    private var cuesText = NSAttributedString()
    /// The meeting the note-taker is filing against right now — the identity
    /// half of the contract at the top of this file. Readable so the app (and
    /// anything driving it) can see what the panel was last told, which is the
    /// state that used to go stale silently across a handover.
    private(set) var meetingID: String?
    /// The meeting the text now in the field belongs to.
    ///
    /// Bound when the field stops being empty and NEVER re-pointed at a later
    /// meeting: a note belongs to the meeting it was typed in, whatever happens
    /// afterwards. nil means "typed with no meeting in view" — the backend then
    /// files it against whatever is live, which is what the owner means when
    /// they start typing a second before hitting record.
    private var fieldMeetingID: String?
    /// The meeting-clock reading captured the instant the field went from empty
    /// to non-empty — the moment the note was *started*. A note is stamped with
    /// WHEN IT WAS BEGUN, not when Return was finally pressed: a thought jotted
    /// at 12:00 and confirmed at 12:02 lands at 12:00, where it actually
    /// happened. nil while the field is empty; reset the moment it is cleared or
    /// committed so the next note captures its own start. Persisted in the draft
    /// (only when the clock was live) so a crash-recovered note keeps it.
    private var fieldStartedElapsed: TimeInterval?
    /// Whether the meeting clock was actually running when the start above was
    /// captured. A start taken with no recording (a note typed a moment before
    /// hitting record) is not a real meeting time, so `commit` does not trust it.
    private var fieldStartedLive = false
    private var hotkey: GlobalHotkey?
    private var committing = false
    /// A Return pressed while the previous note's POST was still in flight.
    ///
    /// `commit` used to drop that press on the floor with no message at all:
    /// the text stayed in the field and on disk, so nothing was lost, but the
    /// owner had pressed Return, seen nothing happen, and moved on believing
    /// the note was saved. It is queued instead — re-run the moment the request
    /// in flight lands — and the owner is told so in the meantime.
    private var commitQueued = false
    private var queuedCommitKeepsFocus = false
    /// Auxiliary windows this panel sealed while the note field held the
    /// keyboard, with the sharingType each had before — restored when editing
    /// ends so the shield is scoped to the moment it is needed.
    private var sealedWindows: [(window: NSWindow, was: NSWindow.SharingType)] = []
    private var shieldObserver: NSObjectProtocol?
    private var terminateObserver: NSObjectProtocol?
    /// Set while we are deliberately dropping focus, so the end-of-editing
    /// notification does not post the note a second time.
    private var yielding = false

    /// Where an unconfirmed note lives between keystrokes.
    let drafts = NoteDraftStore()
    private var draftTimer: Timer?
    /// The longest a keystroke may sit in RAM before it is on disk — measured
    /// from the last SUCCESSFUL write, not from the last keystroke. See the
    /// durability section at the top of this file for why that distinction is
    /// the entire difference between losing one character and losing the note.
    static let draftMaxStaleness: TimeInterval = 0.1
    /// When the draft on disk last became current.
    private var draftSavedAt = Date.distantPast
    /// The field has changed since that write.
    private var draftDirty = false

    init(baseURL: URL) {
        self.baseURL = baseURL
        panel = FloatingPanel(contentRect: NSRect(x: 0, y: 0, width: 340, height: 460),
                              chrome: true, resizable: true)
        super.init()

        // A draft an earlier build left in UserDefaults is still a note the
        // owner typed: adopt it before anything else can overwrite it.
        drafts.adoptLegacyUserDefaultsDraft()

        panel.title = "Notes"
        panel.delegate = self
        panel.minSize = NSSize(width: 300, height: 280)
        panel.isMovableByWindowBackground = true

        buildContent()

        // Whenever this panel does become key, the caret belongs in the note
        // field — never in one of the read-only reading panes, and never
        // nowhere at all.
        panel.initialFirstResponder = input
        panel.unhandledKeyTarget = { [weak self] in self?.input }

        // Position and size are remembered across launches.
        panel.setFrameAutosaveName("MeetingScribeNotes")

        hotkey = GlobalHotkey(keyCode: UInt32(kVK_ANSI_N),
                              modifiers: UInt32(cmdKey | optionKey)) { [weak self] in
            self?.toggleFromUserGesture()
        }

        // A clean quit does not go through hide(), so it would otherwise be the
        // one teardown that outran the debounce.
        terminateObserver = NotificationCenter.default.addObserver(
            forName: NSApplication.willTerminateNotification, object: nil, queue: .main) {
                [weak self] _ in self?.flushDraft()
            }

        // Anything left over from a previous run belongs to the meeting it was
        // typed in. Give the backend a moment to come up, then file it there.
        DispatchQueue.main.asyncAfter(deadline: .now() + 10) { [weak self] in
            self?.recoverStrandedNotes()
        }
    }

    /// True when the global ⌥⌘N shortcut is live (so the menus can show it).
    var hotkeyRegistered: Bool { hotkey != nil }

    // MARK: - Content

    private func buildContent() {
        let title = Aurora.title("Notes")
        // A single mint dot beside the title — the one spot of accent in the
        // header, matching the web Aurora mark.
        let dot = NSView()
        dot.wantsLayer = true
        dot.layer?.backgroundColor = Aurora.accent.cgColor
        dot.layer?.cornerRadius = 3
        dot.translatesAutoresizingMaskIntoConstraints = false
        dot.widthAnchor.constraint(equalToConstant: 6).isActive = true
        dot.heightAnchor.constraint(equalToConstant: 6).isActive = true
        let titleRow = NSStackView(views: [dot, title])
        titleRow.orientation = .horizontal
        titleRow.alignment = .centerY
        titleRow.spacing = 7

        // A quiet reassurance that this is the point of the panel — a faint
        // chip, so it reads as metadata rather than a control.
        let shield = NSImageView()
        shield.image = NSImage(systemSymbolName: "eye.slash",
                               accessibilityDescription: nil)
        shield.contentTintColor = Aurora.textFaint
        shield.symbolConfiguration = NSImage.SymbolConfiguration(pointSize: 9, weight: .medium)
        let privacy = Aurora.label("Hidden from screen share", size: 10, color: Aurora.textFaint)
        let privacyInner = NSStackView(views: [shield, privacy])
        privacyInner.spacing = 4
        privacyInner.alignment = .centerY
        privacyInner.translatesAutoresizingMaskIntoConstraints = false
        let privacyChip = NSView()
        privacyChip.wantsLayer = true
        privacyChip.layer?.backgroundColor = Aurora.chipFill.cgColor
        privacyChip.layer?.cornerRadius = Aurora.rChip
        privacyChip.layer?.borderWidth = 1
        privacyChip.layer?.borderColor = Aurora.border.cgColor
        privacyChip.addSubview(privacyInner)
        NSLayoutConstraint.activate([
            privacyInner.leadingAnchor.constraint(equalTo: privacyChip.leadingAnchor, constant: 8),
            privacyInner.trailingAnchor.constraint(equalTo: privacyChip.trailingAnchor, constant: -8),
            privacyInner.topAnchor.constraint(equalTo: privacyChip.topAnchor, constant: 4),
            privacyInner.bottomAnchor.constraint(equalTo: privacyChip.bottomAnchor, constant: -4),
        ])

        let header = NSStackView(views: [titleRow, NSView(), privacyChip])
        header.orientation = .horizontal
        header.alignment = .centerY
        header.distribution = .fill

        let cuesHeader = Aurora.sectionHeader("Talking points")
        let cuesScroll = Self.makeScroll(cuesView)
        cuesScroll.translatesAutoresizingMaskIntoConstraints = false
        // A scroll view has no intrinsic height, so the talking points would
        // otherwise always claim their maximum and leave a hole above the
        // divider. Measure the text instead and cap it at 150.
        cuesHeight = cuesScroll.heightAnchor.constraint(equalToConstant: 46)
        cuesHeight.isActive = true
        self.cuesScroll = cuesScroll

        let rule = Hairline(color: Aurora.border)

        buildInput()
        statusLabel.isHidden = true
        statusLabel.lineBreakMode = .byWordWrapping
        statusLabel.maximumNumberOfLines = 2

        let notesScroll = Self.makeScroll(notesView)
        notesScroll.translatesAutoresizingMaskIntoConstraints = false
        notesScroll.heightAnchor.constraint(greaterThanOrEqualToConstant: 70).isActive = true
        notesScroll.setContentHuggingPriority(.init(1), for: .vertical)

        let column = NSStackView(views: [header, cuesHeader, cuesScroll, rule,
                                         input, hintLabel, statusLabel,
                                         notesHeader, notesScroll])
        column.orientation = .vertical
        column.alignment = .leading
        // Generous, varied spacing on an ~8px rhythm — room to breathe between
        // sections, tight where a label belongs to what follows it.
        column.spacing = 8
        column.setCustomSpacing(16, after: header)
        column.setCustomSpacing(6, after: cuesHeader)
        column.setCustomSpacing(16, after: cuesScroll)
        column.setCustomSpacing(16, after: rule)
        column.setCustomSpacing(8, after: input)
        column.setCustomSpacing(16, after: hintLabel)
        column.setCustomSpacing(6, after: notesHeader)
        column.translatesAutoresizingMaskIntoConstraints = false

        // Clicking anywhere that is not a control means "I want to write a
        // note" — see the focus contract at the top of the file.
        let content = PanelBodyView()
        content.onClick = { [weak self] in self?.focusInput() }
        // The deep-night panel, painted over the frosted GlassBox so the slab
        // reads as dark frosted glass. Rounded to the panel radius so the fill
        // follows the glass corners.
        content.wantsLayer = true
        content.layer?.backgroundColor = Aurora.panelOverlay.cgColor
        content.layer?.cornerRadius = Aurora.rPanel
        content.layer?.masksToBounds = true
        content.addSubview(column)
        // Top inset clears the transparent title bar's close button.
        NSLayoutConstraint.activate([
            column.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 20),
            column.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -20),
            column.topAnchor.constraint(equalTo: content.topAnchor, constant: 30),
            column.bottomAnchor.constraint(equalTo: content.bottomAnchor, constant: -18),
            header.widthAnchor.constraint(equalTo: column.widthAnchor),
            cuesScroll.widthAnchor.constraint(equalTo: column.widthAnchor),
            rule.widthAnchor.constraint(equalTo: column.widthAnchor),
            input.widthAnchor.constraint(equalTo: column.widthAnchor),
            statusLabel.widthAnchor.constraint(equalTo: column.widthAnchor),
            notesScroll.widthAnchor.constraint(equalTo: column.widthAnchor),
        ])

        // A keystroke that lands in a reading pane is a note the owner meant to
        // type, not a lost one: send it on to the field.
        for pane in [cuesView, notesView] {
            pane.onTypedInto = { [weak self] event in
                guard let self else { return }
                self.focusInput()
                NSApp.postEvent(event, atStart: true)
            }
        }

        box.frame = NSRect(origin: .zero, size: panel.frame.size)
        box.autoresizingMask = [.width, .height]
        box.setContent(content)
        panel.contentView = box

        renderCues()
        renderNotes()
    }

    private func buildInput() {
        input.cell = PaddedTextFieldCell(textCell: "")
        // Swapping the cell throws away the editable/selectable flags that
        // NSTextField's own cell is born with: a hand-made NSTextFieldCell is
        // neither. Without these two lines the field silently refuses every
        // click — the panel looks perfect and cannot be typed into at all.
        input.isEditable = true
        input.isSelectable = true
        // ── The other half of the screen-share guarantee. Do not remove. ───
        // `sharingType = .none` protects the *panel's* window. AppKit's text
        // input machinery draws its autocorrect bubble and its completion
        // candidate list in windows of its own making, which do not inherit
        // that setting — so a half-typed private note would have appeared in
        // the screen share even though the panel around it did not.
        //
        // Re-measured this round on macOS 26.5.2, same keystrokes into two
        // fields that differ only in these settings. A control NSTextField with
        // AppKit's defaults, typed "PRIVATENOTE budge" and asked for
        // completion, builds one NSTextViewCompletionWindow — canBecomeKey no,
        // sharingType .readOnly — which captures at 100.0000% ink and covers
        // 98.7630% of its own patch of screen. The shipped field, same input,
        // creates ZERO new windows. Nothing is lost by turning them off: a
        // meeting note is not prose being drafted, and a silent substitution in
        // a note somebody will quote back is a bug of its own.
        input.isAutomaticTextCompletionEnabled = false
        // ──────────────────────────────────────────────────────────────────
        input.isBordered = false
        input.drawsBackground = false
        input.focusRingType = .none
        input.usesSingleLineMode = true
        input.font = MS.ui(13)
        input.textColor = Aurora.text
        input.delegate = self
        input.wantsLayer = true
        // A raised sheet of glass with an ~8% hairline and a 10pt radius — the
        // Aurora note input.
        input.layer?.backgroundColor = Aurora.inputFill.cgColor
        input.layer?.cornerRadius = Aurora.rInput
        input.layer?.borderWidth = 1
        input.layer?.borderColor = Aurora.border.cgColor
        input.placeholderAttributedString = NSAttributedString(
            string: "Add a note…",
            attributes: [.foregroundColor: Aurora.textFaint, .font: MS.ui(13)])
        input.translatesAutoresizingMaskIntoConstraints = false
        // Room to type: a taller field than the default.
        input.heightAnchor.constraint(equalToConstant: 36).isActive = true
    }

    /// The field borrows the accent while it holds the keyboard — the web UI's
    /// `input:focus { border-color: … accent … }`, and the one place the accent
    /// is spent on this panel.
    private func setInputFocused(_ focused: Bool) {
        // A mint focus ring while the field holds the keyboard; a plain ~8%
        // hairline otherwise.
        input.layer?.borderColor = focused
            ? Aurora.accent.withAlphaComponent(0.9).cgColor
            : Aurora.border.cgColor
        hintLabel.textColor = focused ? Aurora.textDim : Aurora.textFaint
    }

    private static func makeScroll(_ text: NSTextView) -> NSScrollView {
        let scroll = NSScrollView()
        scroll.drawsBackground = false
        scroll.borderType = .noBorder
        scroll.hasVerticalScroller = true
        scroll.autohidesScrollers = true
        scroll.verticalScrollElasticity = .allowed

        text.drawsBackground = false
        text.isEditable = false
        text.isSelectable = true
        text.isVerticallyResizable = true
        text.isHorizontallyResizable = false
        text.textContainerInset = NSSize(width: 0, height: 1)
        text.minSize = NSSize(width: 0, height: 0)
        text.maxSize = NSSize(width: CGFloat.greatestFiniteMagnitude,
                              height: CGFloat.greatestFiniteMagnitude)
        text.autoresizingMask = [.width]
        text.textContainer?.widthTracksTextView = true
        text.textContainer?.containerSize = NSSize(width: 0,
                                                   height: CGFloat.greatestFiniteMagnitude)
        scroll.documentView = text
        return scroll
    }

    // MARK: - Show / hide

    /// Show the panel without touching the keyboard. This is the automatic
    /// path — a meeting starting must never steal focus mid-sentence.
    func show() {
        show(takingKeyboard: false)
    }

    private func show(takingKeyboard: Bool) {
        loadCues()
        positionIfNeeded()
        panel.orderFrontRegardless()     // ordering front is not taking focus
        restoreDraft()
        recoverStrandedNotes()
        // The first pass runs before the scroll view has a real width.
        DispatchQueue.main.async { [weak self] in self?.updateCuesHeight() }
        if takingKeyboard { focusInput() }
        onVisibilityChange?(true)
    }

    /// Put the panel away. Hiding is *not* confirming.
    ///
    /// Ordering the panel out ends the field's editing session, and ending an
    /// editing session fires `controlTextDidEndEditing`, whose whole job is to
    /// post the note. So closing the panel — from the close button, from ⌥⌘N,
    /// or automatically when the recording stopped — used to silently publish
    /// whatever half-sentence was in the field. It is kept as a draft instead:
    /// it survives hiding and quitting, and comes back the next time the panel
    /// opens, with a line saying it has not been saved yet.
    func hide() {
        yielding = true                   // ending the edit must not post
        flushDraft()
        if input.currentEditor() != nil { panel.makeFirstResponder(nil) }
        if panel.isKeyWindow { panel.yieldKeyboard() }
        setInputFocused(false)
        panel.orderOut(nil)
        yielding = false
        onVisibilityChange?(false)
    }

    func toggle() {
        if panel.isVisible { hide() } else { show(takingKeyboard: false) }
    }

    /// ⌥⌘N and the menu item. Asking for the note-taker by name means you want
    /// to type in it, so this path takes the keyboard deliberately.
    ///
    /// It toggles *the keyboard*, not visibility, because visibility alone did
    /// not keep the promise at the top of this file. Mid-meeting the panel is
    /// already on screen — it appears when recording starts — so the gesture
    /// that is documented as "ask for it by name and it takes the keyboard"
    /// was instead hiding it, and there was no way at all to reach the field
    /// from the keyboard. Now:
    ///     hidden               -> show it and put the caret in the field
    ///     visible, not typing  -> take the keyboard (the mid-meeting case)
    ///     visible and typing   -> put it away
    /// so the same key still both opens and closes, and Escape still hands the
    /// keyboard back without hiding anything.
    func toggleFromUserGesture() {
        if !panel.isVisible {
            show(takingKeyboard: true)
        } else if !holdingKeyboard {
            focusInput()
        } else {
            hide()
        }
    }

    /// True when the note field actually has the caret right now.
    private var holdingKeyboard: Bool {
        panel.isKeyWindow && input.currentEditor() != nil
    }

    var isVisible: Bool { panel.isVisible }

    /// The user closed the panel: that must never stop the recording.
    func windowShouldClose(_ sender: NSWindow) -> Bool {
        hide()
        return false
    }

    // MARK: - Meeting state

    /// The meeting being recorded is now `id`.
    ///
    /// IDENTITY ONLY — it never puts the panel on screen. That separation is
    /// the fix for a note filed to the wrong meeting, and RecorderRouter
    /// carries the full account of it. In short: this used to be reachable only
    /// on a not-recording -> recording edge, so stopping meeting A and starting
    /// meeting B inside one 2-second poll window left the panel still filing
    /// against A — with B's clock on it — and left A's own unconfirmed note
    /// unflushed, because the stop that would have flushed it was never seen
    /// either. Back-to-back meetings are exactly the handover this panel exists
    /// for.
    ///
    /// Cheap and idempotent on purpose: it is called from every route that
    /// might notice the change first (the 2 s status poll, its tick, and the
    /// HUD's 5 Hz recorder poll), and all but the first are no-ops.
    ///
    /// nil is ignored rather than adopted. It means "the recorder named no
    /// meeting", which is not the same as "the meeting ended" — the end of a
    /// meeting comes through `recordingStopped()`, and unbinding here would
    /// strand whatever is in the field.
    func meetingChanged(to id: String?) {
        guard let id, id != meetingID else { return }
        let previous = fieldMeetingID
        // The new meeting becomes current FIRST. The meeting clock already
        // belongs to it by the time this runs (the HUD is updated from the same
        // sample, before we are told), and `commit` refuses to stamp a note
        // with a clock that is measuring a different meeting — which it cannot
        // work out unless `meetingID` has moved on. Measured before this
        // ordering: a note typed in A and flushed as B started went to A with
        // B's five-second elapsed on it.
        meetingID = id
        // "Notes this meeting" means this meeting. A's notes must not still be
        // on screen while B records.
        notes.removeAll()
        renderNotes()
        // Text bound to the meeting that just ended goes there. Text that was
        // never bound to one — typed a moment before hitting record — stays in
        // the field and becomes a note for THIS meeting.
        if previous != nil {
            flushPendingNote(reason: "the meeting it was typed in ended")
        }
    }

    /// The recording stopped. Whatever is in the field belongs to the meeting
    /// that just ended — it is the only meeting it could belong to — so it is
    /// filed there rather than kept as a draft for some future meeting to
    /// inherit. If the POST fails the text stays exactly where it is.
    func recordingStopped() {
        flushPendingNote(reason: "the meeting ended")
        hide()
    }

    /// Post the note sitting in the field to the meeting it is bound to.
    ///
    /// Deliberately not `guard fieldMeetingID != nil`: text typed with nothing
    /// recording is still worth filing, and the backend's grace window puts it
    /// in the meeting that just stopped.
    private func flushPendingNote(reason: String) {
        guard !fieldText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return }
        flushDraft()          // on disk before the network is involved at all
        commit(thenFocus: false, failureNote: "Kept here — \(reason) but this note "
                                            + "couldn’t be saved. Press Return to retry.")
    }

    // MARK: - Cues

    private func loadCues() {
        API.get(baseURL.appendingPathComponent("api/cues")) { [weak self] obj, status in
            guard let self, status == 200 else { return }
            self.cues = (obj?["cues"] as? [String])?
                .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
                .filter { !$0.isEmpty } ?? []
            self.renderCues()
        }
    }

    private func renderCues() {
        let out = NSMutableAttributedString()
        if cues.isEmpty {
            out.append(NSAttributedString(
                string: "No talking points yet — add them before the meeting.",
                attributes: Self.attrs(size: 12, color: Aurora.textFaint)))
        } else {
            let para = NSMutableParagraphStyle()
            para.paragraphSpacing = 6
            para.lineHeightMultiple = 1.1
            para.headIndent = 13
            for cue in cues {
                let bullet = NSMutableAttributedString(
                    string: "•  ",
                    attributes: [.font: MS.ui(12.5),
                                 .foregroundColor: Aurora.textFaint,
                                 .paragraphStyle: para])
                var body = Self.attrs(size: 12.5, color: Aurora.textDim)
                body[.paragraphStyle] = para
                bullet.append(NSAttributedString(string: "\(cue)\n", attributes: body))
                out.append(bullet)
            }
        }
        cuesText = out
        cuesView.textStorage?.setAttributedString(out)
        updateCuesHeight()
    }

    /// Size the talking-points box to its text (capped), so it hugs instead of
    /// leaving dead space. Measured off the attributed string rather than the
    /// layout manager, which is nil under TextKit 2.
    private func updateCuesHeight() {
        guard cuesHeight != nil else { return }
        let width = max(1, cuesScroll.contentSize.width)
        let measured = cuesText.boundingRect(
            with: NSSize(width: width, height: .greatestFiniteMagnitude),
            options: [.usesLineFragmentOrigin, .usesFontLeading]).height
        let target = min(150, max(24, ceil(measured) + 6))
        if abs(cuesHeight.constant - target) > 0.5 { cuesHeight.constant = target }
    }

    func windowDidResize(_ notification: Notification) {
        updateCuesHeight()
    }

    // MARK: - Notes

    private func renderNotes() {
        notesHeader.attributedStringValue = Aurora.sectionHeaderText(
            notes.isEmpty ? "Notes this meeting" : "Notes this meeting (\(notes.count))")
        let out = NSMutableAttributedString()
        if notes.isEmpty {
            out.append(NSAttributedString(string: "Nothing yet.",
                                          attributes: Self.attrs(size: 12, color: Aurora.textFaint)))
        }
        let para = NSMutableParagraphStyle()
        para.paragraphSpacing = 8
        para.lineHeightMultiple = 1.12      // comfortable line-height for note text
        para.headIndent = 46
        for note in notes {                       // newest first
            // Timestamps are monospaced and faint, like .note-time in the web
            // UI — never the accent, or every line would shout.
            let stamp = NSMutableAttributedString(
                string: Self.clock(note.t) + "   ",
                attributes: [.font: MS.mono(11, .semibold),
                             .foregroundColor: Aurora.textFaint,
                             .paragraphStyle: para])
            var body = Self.attrs(size: 12.5, color: Aurora.text)
            body[.paragraphStyle] = para
            stamp.append(NSAttributedString(string: note.text + "\n", attributes: body))
            out.append(stamp)
        }
        notesView.textStorage?.setAttributedString(out)
        notesView.scroll(.zero)                   // newest is at the top
    }

    private static func clock(_ t: TimeInterval) -> String {
        let s = max(0, Int(t))
        return s >= 3600
            ? String(format: "%d:%02d:%02d", s / 3600, (s % 3600) / 60, s % 60)
            : String(format: "%d:%02d", s / 60, s % 60)
    }

    private static func attrs(size: CGFloat, color: NSColor) -> [NSAttributedString.Key: Any] {
        [.font: MS.ui(size), .foregroundColor: color]
    }

    // MARK: - Focus

    /// Take the keyboard on purpose and put the caret in the note field.
    ///
    /// Synchronous, because the reading-pane redirect re-posts the keystroke
    /// immediately afterwards and a deferred focus would drop that character.
    /// The retry is not belt-and-braces: a window ordered front in this same
    /// turn of the run loop can refuse key status, and it was caught doing
    /// exactly that once in ~10 acceptance runs — ⌥⌘N opened the panel and it
    /// then quietly ignored the keyboard.
    private func focusInput() {
        guard panel.isVisible else { return }
        if holdingKeyboard { return }                       // already typing
        panel.takeKeyboard(focusing: input)
        guard !holdingKeyboard else { return }
        DispatchQueue.main.async { [weak self] in
            guard let self, self.panel.isVisible, !self.holdingKeyboard else { return }
            self.panel.takeKeyboard(focusing: self.input)
        }
    }

    /// Hand the keyboard straight back to the meeting app. The panel stays on
    /// screen, the recording is untouched, and an uncommitted note stays in the
    /// field as a draft.
    private func yieldFocus(committing shouldCommit: Bool) {
        yielding = true
        if shouldCommit {
            commit(thenFocus: false)
        } else {
            flushDraft()
        }
        panel.yieldKeyboard()
        setInputFocused(false)
        yielding = false
    }

    /// Return and Escape, handled explicitly rather than inferred from an
    /// NSTextMovement code, so both are predictable.
    func control(_ control: NSControl, textView: NSTextView,
                 doCommandBy selector: Selector) -> Bool {
        switch selector {
        case #selector(NSResponder.insertNewline(_:)):
            yieldFocus(committing: true)
            return true
        case #selector(NSResponder.cancelOperation(_:)):
            yieldFocus(committing: false)
            return true
        default:
            return false
        }
    }

    func controlTextDidBeginEditing(_ note: Notification) {
        setInputFocused(true)
        harden(input.currentEditor() as? NSTextView)
        beginShielding()
    }

    /// Every keystroke, which is the only moment at which an unconfirmed note
    /// exists nowhere but in RAM. It leaves here already on disk.
    func controlTextDidChange(_ note: Notification) {
        shieldAuxiliaryWindows()
        if fieldText.isEmpty {
            // The owner cleared the field: the draft goes with it, or the note
            // they just deleted would come back the next time the panel opens.
            // forgetDraftState() also drops the captured start.
            forgetDraftState()
            drafts.clear(for: fieldMeetingID)
            fieldMeetingID = nil
            setStatus(nil)
            return
        }
        // Empty -> non-empty: this is the moment the note was started. Stamp it
        // now, not at Return — see `captureStartIfNeeded` and `commit`.
        captureStartIfNeeded()
        scheduleDraftSave()
    }

    /// Record the moment the note was started — the first character into an
    /// empty field. Captured once and held until the field is cleared or the
    /// note is committed, so the timestamp reflects when the owner began the
    /// thought rather than when they finally pressed Return. Idempotent: called
    /// from both the keystroke path and `bindFieldIfNeeded`, and a no-op once a
    /// start is already on record.
    private func captureStartIfNeeded() {
        guard fieldStartedElapsed == nil, !fieldText.isEmpty else { return }
        fieldStartedLive = clockIsLive?() ?? false
        fieldStartedElapsed = currentElapsed?()
    }

    // MARK: - Screen-share safety for the text input

    /// The field editor is where the leak would have been.
    ///
    /// AppKit's autocorrect bubble and completion candidate list are *separate
    /// windows*, created by AppKit, at pop-up level, with the default
    /// `sharingType = .readOnly`. They are not children of this panel and do
    /// not inherit its `.none`, so the panel could be perfectly excluded from a
    /// screen share while a bubble spelling out the owner's half-typed private
    /// note floated above it, fully captured.
    ///
    /// The fix is to not create them: every automatic behaviour that can put a
    /// window on screen is turned off on the field editor before the first
    /// keystroke. Nothing is lost — a meeting note is not prose being drafted,
    /// and silent substitutions in a verbatim note are a bug of their own.
    func control(_ control: NSControl, textShouldBeginEditing fieldEditor: NSText) -> Bool {
        harden(fieldEditor as? NSTextView)
        return true
    }

    /// The completion candidate list, refused at the source: an empty list
    /// means AppKit has nothing to show and never opens the window.
    func control(_ control: NSControl, textView: NSTextView, completions words: [String],
                 forPartialWordRange charRange: NSRange,
                 indexOfSelectedItem index: UnsafeMutablePointer<Int>) -> [String] {
        []
    }

    private func harden(_ editor: NSTextView?) {
        guard let editor else { return }
        editor.isAutomaticTextCompletionEnabled = false   // candidate list window
        editor.isAutomaticSpellingCorrectionEnabled = false  // correction bubble
        editor.isAutomaticTextReplacementEnabled = false     // replacement bubble
        editor.isContinuousSpellCheckingEnabled = false
        editor.isGrammarCheckingEnabled = false
        editor.isAutomaticQuoteSubstitutionEnabled = false
        editor.isAutomaticDashSubstitutionEnabled = false
        editor.isAutomaticDataDetectionEnabled = false
        editor.isAutomaticLinkDetectionEnabled = false
    }

    /// Belt and braces behind `harden`. While the note field holds the keyboard,
    /// every window of ours that the owner cannot type into is excluded from
    /// capture — whenever it was created.
    ///
    /// THE BUG THIS FIXES. The shield used to snapshot `NSApp.windows` when
    /// editing began and exempt everything already in it, on the theory that a
    /// leak could only come from a window created *later*. AppKit does not work
    /// that way: it builds its text-input helper windows once and reuses them,
    /// so the completion window from the owner's first note is already in that
    /// snapshot when they type their second — permanently exempt, at its default
    /// `.readOnly`, for the entire rest of the app's life. Age is not what makes
    /// a window dangerous; being an ephemeral helper is, and the test for that
    /// is that it cannot take the keyboard.
    ///
    /// Scoped in the two directions that matter. It never touches a window the
    /// owner can type into — the main window is something they may legitimately
    /// want to share, and blanket-hiding it would be a bug dressed up as a fix —
    /// and every change is UNDONE when editing ends, so a window that is only
    /// dangerous while a private note is half-typed is only hidden while a
    /// private note is half-typed.
    ///
    /// MEASURED, on macOS 26.5.2, against the real thing rather than a mock.
    /// A control NSTextField with AppKit's defaults, asked for completion after
    /// typing "PRIVATENOTE budge", produces exactly one new window:
    ///     NSTextViewCompletionWindow  level=0  canBecomeKey=no  sharing=readOnly
    /// Typed during one editing session and still alive in the next, the old
    /// snapshot contains it and the old predicate declines to seal it; this one
    /// seals it. What that is worth, in pixels, from a capture of exactly that
    /// patch of screen compared against the same patch with the popup gone:
    ///     exempt  (.readOnly)  meanDiff 48.26/255, 98.7630% of 38400 px changed
    ///     sealed  (.none)      meanDiff  0.00/255,  0.0000% of 38400 px changed
    /// Zero pixels of it appear. A single-window capture of the same window
    /// agrees and alternates cleanly across three rounds: 100.0000% ink exempt,
    /// 0.0000% sealed.
    ///
    /// Both captures are `CGWindowListCreateImage` — the display-composite and
    /// single-window forms. A ScreenCaptureKit capture could NOT be run here:
    /// SCK refuses this process with SCStreamErrorDomain -3801 ("the user
    /// declined TCCs"), and `CGPreflightScreenCaptureAccess()` is false, so
    /// nothing in this round exercised that path and nothing here claims it.
    /// Note what the missing permission means for the numbers above rather than
    /// against them: without it these APIs return the desktop plus the CALLING
    /// process's own windows — and the window under test is ours, so it is
    /// precisely the content that is allowed through, and it still comes back
    /// empty at `.none`.
    ///
    /// The shield is the second line. The first is `harden`: given the same
    /// keystrokes and the same request for completion, the shipped note field
    /// creates ZERO new windows and leaves the text byte-for-byte as typed.
    private func beginShielding() {
        shieldAuxiliaryWindows()
        guard shieldObserver == nil else { return }
        shieldObserver = NotificationCenter.default.addObserver(
            forName: NSWindow.didUpdateNotification, object: nil, queue: .main) { [weak self] _ in
                self?.shieldAuxiliaryWindows()
            }
    }

    private func endShielding() {
        if let shieldObserver { NotificationCenter.default.removeObserver(shieldObserver) }
        shieldObserver = nil
        for entry in sealedWindows { entry.window.sharingType = entry.was }
        sealedWindows = []
    }

    /// True when `window` is one of the ephemeral helpers AppKit puts on screen
    /// around a text field — a completion list, a correction bubble, a tooltip.
    ///
    /// The discriminator is `canBecomeKey`. Every window the owner can actually
    /// work in answers true (the main window, the setup window, an alert, both
    /// of these panels, which override it); the helpers answer false because
    /// they are decoration over whatever already has the keyboard.
    static func isAuxiliary(_ window: NSWindow) -> Bool {
        !window.canBecomeKey && !(window is FloatingPanel)
    }

    /// - Returns: the windows this call had to seal, so a test can assert on it
    ///   rather than trusting the reasoning above.
    @discardableResult
    func shieldAuxiliaryWindows() -> [NSWindow] {
        var sealed: [NSWindow] = []
        for window in NSApp.windows
        where Self.isAuxiliary(window) && window.sharingType != .none {
            sealedWindows.append((window, window.sharingType))
            window.sharingType = .none
            sealed.append(window)
        }
        return sealed
    }

    // MARK: - Committing a note

    /// Fires when focus leaves the field by any route we did not drive
    /// ourselves — clicking back into the meeting saves the note rather than
    /// losing it.
    func controlTextDidEndEditing(_ note: Notification) {
        setInputFocused(false)
        endShielding()
        guard !yielding else { return }
        commit(thenFocus: false)
    }

    deinit {
        if let shieldObserver { NotificationCenter.default.removeObserver(shieldObserver) }
        if let terminateObserver { NotificationCenter.default.removeObserver(terminateObserver) }
    }

    /// Publish the note in the field to the meeting it belongs to.
    ///
    /// The body is the contract at the top of this file. `meeting_id` is the
    /// binding, not the current recording: it is what makes a note typed in A
    /// land in A even when the request only reaches the backend after A stopped
    /// and B started. `t` is the moment the note was STARTED (captured the
    /// instant the field stopped being empty), which is a real time in the
    /// note's own meeting — so it is sound even after the clock has stopped or
    /// moved on to the next meeting, the two cases the old commit-time stamp had
    /// to drop `t` for.
    private func commit(thenFocus keepFocus: Bool, failureNote: String? = nil) {
        let text = fieldText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        // A second Return while the first POST is still in flight. Never
        // silently dropped: the owner pressed Return, and a keystroke that
        // looks like it did nothing is how somebody walks away from an unsaved
        // note. Queue it, say so, and run it the moment the request lands.
        if committing {
            commitQueued = true
            queuedCommitKeepsFocus = queuedCommitKeepsFocus || keepFocus
            setStatus("Still saving the previous note — this one is queued.", warning: true)
            return
        }
        committing = true
        let target = fieldMeetingID ?? meetingID
        setStatus(nil)

        // Prefer the moment the note was STARTED — captured live, it is a real
        // time in the note's own meeting and stays correct after the clock has
        // stopped or moved on. Fall back to the live clock only if a start was
        // somehow never captured, and then only under the old guard: a stopped
        // clock is a frozen reading (N5), and a running clock measuring the NEXT
        // meeting would put a note from a 40-minute call five seconds in. With
        // no `t`, the backend uses the meeting's own duration, a time that
        // actually happened.
        let stamp: TimeInterval?
        if fieldStartedLive, let started = fieldStartedElapsed {
            stamp = started
        } else if (clockIsLive?() ?? false), target == meetingID {
            stamp = currentElapsed?()
        } else {
            stamp = nil
        }
        let listT = stamp ?? 0

        var body: [String: Any] = ["text": text]
        if let target { body["meeting_id"] = target }
        if let stamp { body["t"] = stamp }

        API.request("POST", baseURL.appendingPathComponent("api/record/note"),
                    body: body, timeout: 5) { [weak self] obj, status in
            guard let self else { return }
            self.committing = false
            if status == 200 {
                // Clear the field only if it still holds the note we posted; if
                // the owner has since typed something else, that is a different
                // note and it is not ours to throw away.
                let current = self.fieldText.trimmingCharacters(in: .whitespacesAndNewlines)
                if current == text {
                    self.setFieldText("")
                    self.forgetDraftState()
                    self.drafts.clear(for: self.fieldMeetingID)
                    self.fieldMeetingID = nil
                }
                // If the text has changed, nothing else needs doing: the draft
                // file for that meeting already holds the NEWER note (the
                // debounce rewrote it as it was typed), so the note just
                // published has no copy left anywhere.
                // "Notes this meeting" means this meeting: a note that has just
                // been filed to the previous one does not belong in the list.
                if target == self.meetingID {
                    self.notes.insert((t: listT, text: text), at: 0)
                    self.renderNotes()
                }
                self.recoverStrandedNotes()
            } else {
                // Never lose it. Put it back in the field if there is room, and
                // if there is not (the owner typed a different note while this
                // one was in flight) stash it for the next sweep rather than
                // letting the field decide which of the two survives.
                if self.fieldText.isEmpty {
                    self.setFieldText(text)
                    self.fieldMeetingID = target
                    self.flushDraft()
                } else if self.fieldText.trimmingCharacters(in: .whitespacesAndNewlines) != text {
                    self.drafts.stashUnsent(text, t: stamp, for: target)
                } else {
                    self.flushDraft()
                }
                let message = (obj?["error"] as? String).map { $0 }
                self.setStatus(failureNote ?? (status == 409
                    ? (message ?? "No recording is running — this note is still here.")
                    : "Couldn’t save — press Return to try again."),
                    warning: status == 409)
            }
            if keepFocus { self.focusInput() }
            // Whatever the outcome, a Return that arrived mid-flight is owed a
            // POST. It runs against whatever the field holds NOW: after a
            // success the field is empty and this is a no-op, and after a
            // second note was typed over the top it is that second note. One
            // shot — the flag is consumed here, so a backend that is down costs
            // one extra attempt and not a retry loop.
            if self.commitQueued {
                self.commitQueued = false
                let focus = self.queuedCommitKeepsFocus
                self.queuedCommitKeepsFocus = false
                self.commit(thenFocus: focus)
            }
        }
        if keepFocus { focusInput() }
    }

    private func setStatus(_ message: String?, warning: Bool = false) {
        statusLabel.stringValue = message ?? ""
        statusLabel.textColor = warning ? MS.warn : MS.danger
        statusLabel.isHidden = (message == nil)
    }

    // MARK: - The text in the field

    /// What is actually in the field right now.
    ///
    /// Not `stringValue`: while a field is being edited the live text lives in
    /// the shared field editor, and the cell only takes a copy when the editing
    /// session ends. Reading `stringValue` mid-edit is exactly how a draft ends
    /// up one keystroke — or one whole note — behind what the owner can see.
    private var fieldText: String {
        input.currentEditor()?.string ?? input.stringValue
    }

    /// Set the field's text through both halves for the same reason.
    private func setFieldText(_ text: String) {
        input.stringValue = text
        if let editor = input.currentEditor() {
            editor.string = text
            editor.selectedRange = NSRange(location: (text as NSString).length, length: 0)
        }
    }

    // MARK: - Drafts (an unconfirmed note survives hiding, quitting and kill -9)

    /// Write what is in the field to disk now, cancelling any armed write.
    ///
    /// Synchronous by contract: when this returns the bytes are on the device
    /// (NoteDraftStore.writeAtomically fsyncs), which is what lets hide(),
    /// Escape, quit and the end of a meeting all promise "it is saved" rather
    /// than "it will be saved".
    private func flushDraft() {
        draftTimer?.invalidate(); draftTimer = nil
        let text = fieldText
        if text.isEmpty {
            drafts.clear(for: fieldMeetingID)
            // Nothing of the field is on disk, so the next keystroke has no
            // budget to spend and is written the instant it lands.
            draftSavedAt = .distantPast
        } else {
            bindFieldIfNeeded()
            // Persist the note's start offset alongside its text, but only when
            // it was captured while the clock ran — a start taken with nothing
            // recording is not a real meeting time. A crash-recovered note then
            // keeps the timestamp it would have been filed with.
            drafts.save(text, t: fieldStartedLive ? fieldStartedElapsed : nil,
                        for: fieldMeetingID)
            draftSavedAt = Date()
        }
        draftDirty = false
    }

    /// There is nothing of the field left on disk: disarm the pending write and
    /// reset the budget so the very next keystroke is written on the spot. The
    /// field is empty or just committed, so the captured start is dropped too —
    /// the next note gets its own.
    private func forgetDraftState() {
        draftTimer?.invalidate(); draftTimer = nil
        draftDirty = false
        draftSavedAt = .distantPast
        fieldStartedElapsed = nil
        fieldStartedLive = false
    }

    /// Bounded staleness, not a debounce.
    ///
    /// The rule is one line: **the disk is never more than
    /// `draftMaxStaleness` behind the field.** A keystroke is written
    /// immediately if that budget has already elapsed since the last write;
    /// otherwise a write is armed for the instant it does elapse, and — this is
    /// the part the old debounce got wrong — a later keystroke does NOT push
    /// that deadline back. Somebody who types without ever pausing therefore
    /// still gets a write every `draftMaxStaleness`, where before they got one
    /// leading-edge character and nothing else until they stopped.
    private func scheduleDraftSave() {
        bindFieldIfNeeded()
        draftDirty = true
        let due = draftSavedAt.addingTimeInterval(Self.draftMaxStaleness)
        let remaining = due.timeIntervalSinceNow
        guard remaining > 0 else {
            flushDraft()          // budget spent (and the first key of a burst)
            return
        }
        guard draftTimer == nil else { return }   // already armed — leave it be
        let timer = Timer(timeInterval: remaining, repeats: false) { [weak self] _ in
            guard let self else { return }
            self.draftTimer = nil
            if self.draftDirty { self.flushDraft() }
        }
        // .common so a draft is still written while the panel is being dragged.
        RunLoop.main.add(timer, forMode: .common)
        draftTimer = timer
    }

    /// Tie the text in the field to a meeting the first time anything is typed,
    /// and stamp the note's start at the same moment — the two things that must
    /// happen the instant the field stops being empty. Both are idempotent.
    private func bindFieldIfNeeded() {
        if fieldMeetingID == nil, !fieldText.isEmpty { fieldMeetingID = meetingID }
        captureStartIfNeeded()
    }

    /// Bring back an unconfirmed note for THIS meeting — never for another one.
    /// A draft from a different meeting is not offered here; it is filed to its
    /// own meeting by `recoverStrandedNotes`.
    private func restoreDraft() {
        guard fieldText.isEmpty else { return }
        if let draft = drafts.load(for: meetingID), !draft.text.isEmpty {
            setFieldText(draft.text)
            fieldMeetingID = meetingID
            adoptCapturedStart(draft.t)
        } else if meetingID != nil, let loose = drafts.load(for: nil), !loose.text.isEmpty {
            // Typed before any meeting existed — a thought jotted a moment
            // before hitting record, or a draft inherited from the build that
            // kept one global one. It is tied to no meeting, so it belongs to
            // whichever one is in front of the owner now; bind it so it is
            // recoverable from here on.
            //
            // WRITE THE NEW HOME BEFORE DROPPING THE OLD ONE. These two calls
            // were the other way round, which left a window — however short —
            // in which the note existed in neither file, and a crash inside it
            // lost the note outright. This order can only ever leave it in
            // BOTH, and a duplicate draft is picked up and filed on the next
            // pass. Losing it is not recoverable; having it twice is.
            setFieldText(loose.text)
            fieldMeetingID = meetingID
            drafts.save(loose.text, t: loose.t, for: meetingID)
            drafts.clear(for: nil)
            adoptCapturedStart(loose.t)
        } else {
            return
        }
        setStatus("This note wasn’t saved yet — press Return to save it.", warning: true)
    }

    /// A recovered draft carries the offset it was started at (present only when
    /// it was captured while the clock ran). Adopt it so the note keeps the
    /// timestamp it would have been filed with, rather than being re-stamped at
    /// recovery time.
    private func adoptCapturedStart(_ t: TimeInterval?) {
        fieldStartedElapsed = t
        fieldStartedLive = (t != nil)
    }

    /// File every note left over from a meeting that is no longer in front of
    /// us: drafts whose meeting has ended, and notes whose POST failed while a
    /// different note was in the field.
    ///
    /// This is the crash path made whole. A draft that survived a `kill -9`
    /// belongs to the meeting it was typed in, and that meeting is named in the
    /// draft itself — so it is filed there, not against whatever happens to be
    /// recording when the app comes back. Nothing is deleted until the backend
    /// confirms it stored it, and a failure simply means the next sweep tries
    /// again.
    private func recoverStrandedNotes() {
        // The current meeting's draft is the owner's live text, and the field's
        // own note is theirs to confirm — neither is stranded.
        var mine = Set<String>()
        if let meetingID { mine.insert(meetingID) }
        if let fieldMeetingID { mine.insert(fieldMeetingID) }
        for item in drafts.stranded(excludingDraftsFor: mine) {
            var body: [String: Any] = ["text": item.text]
            if let id = item.meetingID { body["meeting_id"] = id }
            // The moment it was started, when that was captured (and persisted)
            // during the note's own meeting. Absent, the backend stamps it with
            // the meeting's real duration.
            if let t = item.t { body["t"] = t }
            API.request("POST", baseURL.appendingPathComponent("api/record/note"),
                        body: body, timeout: 5) { [weak self] _, status in
                // Only a stored note is a forgotten note. 404 (the meeting was
                // deleted) and 400 leave the file alone too: there is nowhere
                // to put the words, and dropping them is the one outcome that
                // is not allowed.
                guard let self, status == 200 else { return }
                self.drafts.forget(item)
            }
        }
    }

    // MARK: - Placement

    private var positioned = false
    private func positionIfNeeded() {
        if positioned { return }
        positioned = true
        // setFrameAutosaveName already restored a remembered frame if there is
        // one; only place the panel when there isn't.
        if UserDefaults.standard.string(forKey: "NSWindow Frame MeetingScribeNotes") != nil { return }
        guard let screen = NSScreen.main else { return }
        let visible = screen.visibleFrame
        let size = panel.frame.size
        // Default: just above the HUD, tucked to the right of centre.
        let x = min(visible.maxX - size.width - 20, visible.midX + 60)
        let y = visible.minY + 118
        panel.setFrameOrigin(NSPoint(x: max(visible.minX + 20, x), y: y))
    }
}

// MARK: - Where an unconfirmed note lives

/// The on-disk home of notes that have been typed but not yet stored by the
/// backend. Two kinds, one small file each, under ~/.meetingscribe/note-drafts:
///
///   <meeting>.draft   what is in the note field right now, rewritten as the
///                     owner types. Exactly one per meeting, replaced whole.
///   <meeting>.unsent  notes whose POST failed and which are no longer in the
///                     field, one JSON-encoded string per line, retried later.
///
/// WHY A FILE AND NOT UserDefaults, which is what this used to be. Two reasons,
/// and the second is the one that made a note land in the wrong meeting.
///
///   * Durability. `UserDefaults.set` hands the value to cfprefsd and returns;
///     when it reaches disk is cfprefsd's business, not ours. A note the owner
///     typed is the one thing in this app that cannot be recreated from the
///     audio, so it gets the same treatment notes.jsonl gets on the Python
///     side: write, fsync, rename. After that, only the machine losing power
///     can lose it — and the process being killed cannot.
///   * Identity. There was ONE key for every meeting, so an unconfirmed note
///     from meeting A was handed straight to meeting B the next time the panel
///     opened. A draft is per-meeting here, and the meeting is in the filename,
///     which is what lets a draft that outlived a crash be posted back to the
///     meeting it was actually typed in.
///
/// The directory is settable so a test can drive this without going anywhere
/// near the owner's real state directory.
final class NoteDraftStore {

    /// A note that is on disk and not in the field: it belongs to a meeting
    /// that has moved on, and the only thing left to do with it is file it.
    /// `meetingID` is nil for a note that was never tied to one, in which case
    /// the backend files it against whatever is live.
    struct Stranded: Equatable {
        let meetingID: String?
        let text: String
        /// The offset the note was started at, when it was captured during the
        /// note's own meeting. nil means "stamp it with the meeting's duration".
        let t: TimeInterval?
        fileprivate let source: Source
    }

    fileprivate enum Source: Equatable { case draft, unsent }

    /// The slot for a note typed with no meeting in view. Cannot collide with a
    /// meeting id, which is always `\d{8}-\d{6}` (app.py's MEETING_ID_RE).
    private static let unattached = "unattached"
    private static let legacyDefaultsKey = "MeetingScribeNoteDraft"

    let directory: URL

    init(directory: URL? = nil) {
        self.directory = directory ?? Self.defaultDirectory()
    }

    static func defaultDirectory() -> URL {
        let env = ProcessInfo.processInfo.environment["MEETINGSCRIBE_DATA"]
        let base = env.map { URL(fileURLWithPath: NSString(string: $0).expandingTildeInPath) }
            ?? FileManager.default.homeDirectoryForCurrentUser
                .appendingPathComponent(".meetingscribe")
        return base.appendingPathComponent("note-drafts")
    }

    /// app.py's MEETING_ID_RE, so nothing else can ever become a filename.
    static func isMeetingID(_ id: String) -> Bool {
        let parts = id.split(separator: "-", omittingEmptySubsequences: false)
        guard parts.count == 2, parts[0].count == 8, parts[1].count == 6 else { return false }
        return parts.allSatisfy { $0.allSatisfy { $0.isASCII && $0.isNumber } }
    }

    private func slot(_ meetingID: String?) -> String {
        guard let meetingID, Self.isMeetingID(meetingID) else { return Self.unattached }
        return meetingID
    }

    private func draftURL(_ meetingID: String?) -> URL {
        directory.appendingPathComponent(slot(meetingID) + ".draft")
    }

    private func unsentURL(_ meetingID: String?) -> URL {
        directory.appendingPathComponent(slot(meetingID) + ".unsent")
    }

    // MARK: The live draft

    /// `t` is the offset the note was started at, folded into the file so a
    /// crash-recovered note keeps its timestamp. Defaulted so the callers that
    /// have no timestamp (a legacy adoption) need not pass one.
    func save(_ text: String, t: TimeInterval? = nil, for meetingID: String?) {
        if text.isEmpty { clear(for: meetingID); return }
        do {
            try Self.writeAtomically(Self.encodeDraft(text, t: t), to: draftURL(meetingID))
        } catch {
            NSLog("MeetingScribe: could not persist the note draft: \(error)")
        }
    }

    func load(for meetingID: String?) -> (text: String, t: TimeInterval?)? {
        guard let data = try? Data(contentsOf: draftURL(meetingID)) else { return nil }
        return Self.decodeDraft(data)
    }

    func clear(for meetingID: String?) {
        try? FileManager.default.removeItem(at: draftURL(meetingID))
    }

    // MARK: Notes whose POST failed and which the field no longer holds

    func stashUnsent(_ text: String, t: TimeInterval? = nil, for meetingID: String?) {
        guard !text.isEmpty, let line = Self.encode(text, t: t) else { return }
        do {
            try Self.ensureDirectory(directory)
            let url = unsentURL(meetingID)
            let fd = open(url.path, O_WRONLY | O_CREAT | O_APPEND, 0o600)
            guard fd >= 0 else { throw Self.posixError() }
            defer { close(fd) }
            try Self.writeAll(Data((line + "\n").utf8), to: fd)
            fsync(fd)
        } catch {
            NSLog("MeetingScribe: could not stash an unsent note: \(error)")
        }
    }

    private func unsent(for meetingID: String?) -> [(text: String, t: TimeInterval?)] {
        guard let raw = try? String(contentsOf: unsentURL(meetingID), encoding: .utf8) else {
            return []
        }
        // split("\n") only — the encoder escapes every newline it writes, so
        // nothing else in the file is ever a record separator.
        return raw.split(separator: "\n", omittingEmptySubsequences: true)
            .compactMap { Self.decode(String($0)) }
    }

    // MARK: The sweep

    /// Everything that is not the owner's live text: drafts belonging to
    /// meetings that have moved on, and every unsent note there is.
    ///
    /// `excludingDraftsFor` is the set of meetings whose draft is currently the
    /// field's business — the meeting being recorded and the one the field's
    /// text is bound to. Unsent notes are never excluded: by definition they
    /// are no longer in anybody's field.
    func stranded(excludingDraftsFor live: Set<String>) -> [Stranded] {
        let names = (try? FileManager.default.contentsOfDirectory(atPath: directory.path)) ?? []
        var out: [Stranded] = []
        for name in names.sorted() {
            if name.hasSuffix(".draft") {
                // Only real meetings. The unattached draft is the field's own
                // text-in-waiting and `restoreDraft` is what picks it up.
                let id = String(name.dropLast(".draft".count))
                guard Self.isMeetingID(id), !live.contains(id),
                      let entry = load(for: id) else { continue }
                out.append(Stranded(meetingID: id, text: entry.text, t: entry.t, source: .draft))
            } else if name.hasSuffix(".unsent") {
                let slot = String(name.dropLast(".unsent".count))
                guard Self.isMeetingID(slot) || slot == Self.unattached else { continue }
                let id: String? = slot == Self.unattached ? nil : slot
                for entry in unsent(for: id) {
                    out.append(Stranded(meetingID: id, text: entry.text, t: entry.t, source: .unsent))
                }
            }
        }
        return out
    }

    /// Drop one stranded note — only ever called once the backend has confirmed
    /// it stored it.
    func forget(_ item: Stranded) {
        switch item.source {
        case .draft:
            // Only if it still says what we posted: the owner may have started
            // typing into that meeting again in the meantime.
            if load(for: item.meetingID)?.text == item.text { clear(for: item.meetingID) }
        case .unsent:
            var lines = unsent(for: item.meetingID)
            guard let index = lines.firstIndex(where: { $0.text == item.text }) else { return }
            lines.remove(at: index)
            let url = unsentURL(item.meetingID)
            if lines.isEmpty {
                try? FileManager.default.removeItem(at: url)
            } else {
                let body = lines.compactMap { Self.encode($0.text, t: $0.t) }
                    .joined(separator: "\n") + "\n"
                try? Self.writeAtomically(Data(body.utf8), to: url)
            }
        }
    }

    // MARK: Migration

    /// Take over a draft an earlier build left in UserDefaults. It has no
    /// meeting attached to it — that was the bug — so it goes to the unattached
    /// slot, where the backend will file it against whatever is live.
    func adoptLegacyUserDefaultsDraft() {
        let defaults = UserDefaults.standard
        guard let legacy = defaults.string(forKey: Self.legacyDefaultsKey),
              !legacy.isEmpty else { return }
        if load(for: nil) == nil { save(legacy, for: nil) }
        defaults.removeObject(forKey: Self.legacyDefaultsKey)
    }

    // MARK: Bytes on disk

    /// Replace a file's contents so that a reader only ever sees the whole old
    /// version or the whole new one: write a temp file, flush it to the device,
    /// then rename over the target. The same shape as pipeline._atomic_write on
    /// the Python side, for the same reason.
    ///
    /// Measured on this machine (macOS 26.5.2, APFS, internal SSD), 200
    /// rewrites of a ~40-byte draft, the whole sequence per call: mean 0.278 ms,
    /// worst 1.004 ms (a second run: mean 0.227 ms, worst 1.155 ms). Small
    /// enough to run on the main thread, which is what lets `flushDraft()`
    /// promise the bytes are on disk when it returns rather than "soon" — and
    /// at one write per keystroke burst it is nowhere near a frame budget.
    static func writeAtomically(_ data: Data, to url: URL) throws {
        try ensureDirectory(url.deletingLastPathComponent())
        let tmp = url.deletingLastPathComponent()
            .appendingPathComponent(".\(url.lastPathComponent).\(getpid()).tmp")
        let fd = open(tmp.path, O_WRONLY | O_CREAT | O_TRUNC, 0o600)
        guard fd >= 0 else { throw posixError() }
        do {
            try writeAll(data, to: fd)
            fsync(fd)
            close(fd)
        } catch {
            close(fd)
            unlink(tmp.path)
            throw error
        }
        guard rename(tmp.path, url.path) == 0 else {
            let failure = posixError()
            unlink(tmp.path)
            throw failure
        }
        // The rename itself is metadata; flush the directory so it survives too.
        let dir = open(url.deletingLastPathComponent().path, O_RDONLY)
        if dir >= 0 { fsync(dir); close(dir) }
    }

    private static func ensureDirectory(_ url: URL) throws {
        try FileManager.default.createDirectory(
            at: url, withIntermediateDirectories: true,
            attributes: [.posixPermissions: 0o700])   // half-typed private notes
    }

    private static func writeAll(_ data: Data, to fd: Int32) throws {
        var written = 0
        while written < data.count {
            let n: Int = try data.withUnsafeBytes { raw in
                guard let base = raw.bindMemory(to: UInt8.self).baseAddress else {
                    throw posixError()
                }
                return write(fd, base + written, data.count - written)
            }
            guard n > 0 else { throw posixError() }
            written += n
        }
    }

    private static func posixError() -> Error {
        POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
    }

    /// One note per line, JSON-encoded as `[text]` or `[text, t]`, so a newline
    /// that rode in on a paste cannot split one note into two unreadable halves
    /// and the note's start offset rides along with it.
    static func encode(_ text: String, t: TimeInterval? = nil) -> String? {
        let obj: [Any] = t == nil ? [text] : [text, t!]
        guard let data = try? JSONSerialization.data(withJSONObject: obj),
              let line = String(data: data, encoding: .utf8) else { return nil }
        return line
    }

    static func decode(_ line: String) -> (text: String, t: TimeInterval?)? {
        guard let data = line.data(using: .utf8),
              let array = try? JSONSerialization.jsonObject(with: data) as? [Any],
              let text = array.first as? String, !text.isEmpty else { return nil }
        // A one-element line ([text]) predates the start offset; read it as no t.
        let t = array.count > 1 ? (array[1] as? TimeInterval) : nil
        return (text, t)
    }

    /// A draft on disk is a small JSON object, `{"text": …, "t": …}`, so it can
    /// carry the note's start offset alongside its text. A plain-text draft left
    /// by an earlier build is still read — as the text, with no start time.
    static func encodeDraft(_ text: String, t: TimeInterval?) -> Data {
        var obj: [String: Any] = ["text": text]
        if let t { obj["t"] = t }
        return (try? JSONSerialization.data(withJSONObject: obj)) ?? Data(text.utf8)
    }

    static func decodeDraft(_ data: Data) -> (text: String, t: TimeInterval?)? {
        if let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           let text = obj["text"] as? String, !text.isEmpty {
            return (text, obj["t"] as? TimeInterval)
        }
        // Legacy: the whole file is the note text.
        guard let text = String(data: data, encoding: .utf8), !text.isEmpty else { return nil }
        return (text, nil)
    }
}

// MARK: - The panel body

/// Everything behind the controls. A click here means "let me type", so it both
/// asks the panel to become key (`needsPanelToBecomeKey`) and puts the caret in
/// the note field. Before this, clicking the panel anywhere but the 34pt field
/// left the keyboard pointed at a read-only text view and the panel looked
/// broken.
final class PanelBodyView: NSView {
    var onClick: (() -> Void)?

    override var needsPanelToBecomeKey: Bool { true }
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }

    override func mouseDown(with event: NSEvent) {
        onClick?()
        super.mouseDown(with: event)
    }
}

// MARK: - The reading panes

/// The talking points and the notes-so-far list: selectable so they can be
/// copied, but they must never eat a keystroke. Typing into one means the owner
/// wants to write a note, so the event is handed to the field instead of being
/// swallowed with a beep.
///
/// `acceptsFirstMouse` is not decoration here. MeetingScribe is never the
/// frontmost app during a meeting, so without it the click on a reading pane is
/// swallowed to focus the window, the pane never becomes first responder, the
/// *window* does — and every subsequent keystroke is dropped on the floor.
/// Measured: hit test returned ReadingTextView, panel became key, first
/// responder was FloatingPanel, and typing produced "".
final class ReadingTextView: NSTextView {
    var onTypedInto: ((NSEvent) -> Void)?

    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }

    override func keyDown(with event: NSEvent) {
        let flags = event.modifierFlags.intersection(.deviceIndependentFlagsMask)
        let isTyping = !(event.charactersIgnoringModifiers ?? "").isEmpty
            && !flags.contains(.command)
            && !flags.contains(.control)
        if isTyping, let onTypedInto {
            onTypedInto(event)
            return
        }
        super.keyDown(with: event)
    }
}

// MARK: - The note field

/// The one view in the panel that is allowed to take the keyboard.
///
/// `becomesKeyOnlyIfNeeded` asks the clicked view whether the panel must become
/// key, and `NSView.needsPanelToBecomeKey` is false by default — including on
/// NSTextField, because the field editor that would answer "yes" is not
/// installed until editing has already begun. Without this override the click
/// lands, the panel stays non-key, and every keystroke goes on to the meeting
/// app: verified by typing at it with Zoom-like focus elsewhere and watching
/// the text arrive in the *other* app.
///
/// `acceptsFirstMouse` matters for the same reason it does on the HUD: the
/// panel's app is never active, so the first click must do real work.
final class NoteField: NSTextField {
    override var needsPanelToBecomeKey: Bool { true }
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }
}

// MARK: - Padded text field

/// NSTextField draws its text hard against the left edge; the note field needs
/// breathing room inside its rounded fill.
final class PaddedTextFieldCell: NSTextFieldCell {
    private let padding = NSSize(width: 12, height: 0)

    override func drawingRect(forBounds rect: NSRect) -> NSRect {
        super.drawingRect(forBounds: rect.insetBy(dx: padding.width, dy: padding.height))
    }

    override func edit(withFrame rect: NSRect, in view: NSView, editor: NSText,
                       delegate: Any?, event: NSEvent?) {
        super.edit(withFrame: rect.insetBy(dx: padding.width, dy: padding.height),
                   in: view, editor: editor, delegate: delegate, event: event)
    }

    override func select(withFrame rect: NSRect, in view: NSView, editor: NSText,
                         delegate: Any?, start: Int, length: Int) {
        super.select(withFrame: rect.insetBy(dx: padding.width, dy: padding.height),
                     in: view, editor: editor, delegate: delegate,
                     start: start, length: length)
    }
}

// MARK: - Global hotkey

/// A system-wide shortcut via Carbon's RegisterEventHotKey — it reaches the app
/// while Zoom is frontmost and, unlike an event tap, needs no Accessibility
/// permission. Returns nil if the combination is already taken, in which case
/// the caller falls back to a menu key equivalent.
final class GlobalHotkey {
    private static var registry: [UInt32: GlobalHotkey] = [:]
    private static var nextID: UInt32 = 1
    private static var handlerInstalled = false

    private let id: UInt32
    private let action: () -> Void
    private var ref: EventHotKeyRef?

    init?(keyCode: UInt32, modifiers: UInt32, action: @escaping () -> Void) {
        self.action = action
        self.id = Self.nextID
        Self.nextID += 1
        Self.installHandlerIfNeeded()

        let hotKeyID = EventHotKeyID(signature: OSType(0x4D53_4E54), id: id)  // 'MSNT'
        var newRef: EventHotKeyRef?
        let status = RegisterEventHotKey(keyCode, modifiers, hotKeyID,
                                         GetApplicationEventTarget(), 0, &newRef)
        guard status == noErr, let newRef else { return nil }
        ref = newRef
        Self.registry[id] = self
    }

    deinit {
        if let ref { UnregisterEventHotKey(ref) }
        Self.registry[id] = nil
    }

    private static func installHandlerIfNeeded() {
        guard !handlerInstalled else { return }
        handlerInstalled = true
        var spec = EventTypeSpec(eventClass: OSType(kEventClassKeyboard),
                                 eventKind: UInt32(kEventHotKeyPressed))
        InstallEventHandler(GetApplicationEventTarget(), { _, event, _ -> OSStatus in
            var hkID = EventHotKeyID()
            GetEventParameter(event, EventParamName(kEventParamDirectObject),
                              EventParamType(typeEventHotKeyID), nil,
                              MemoryLayout<EventHotKeyID>.size, nil, &hkID)
            DispatchQueue.main.async { GlobalHotkey.registry[hkID.id]?.action() }
            return noErr
        }, 1, &spec, nil, nil)
    }
}
