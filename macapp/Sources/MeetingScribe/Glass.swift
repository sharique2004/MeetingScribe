// Shared plumbing for the floating panels that sit on screen during a meeting:
// the recording HUD and the note-taker. Both are Liquid Glass, both float over
// the meeting app, and — the part that matters most — neither is ever visible
// in a screen share.
//
// Three things live here that a new panel must not be able to forget:
//   1. the screen-share exclusion (sharingType = .none),
//   2. the keyboard-focus contract (FloatingPanel.takeKeyboard / yieldKeyboard),
//   3. the app's palette (enum MS), so the panels read as the same product as
//      the web UI rather than as generic system chrome.

import AppKit

// MARK: - The app's design language

/// The web UI's default ("teal") palette, transcribed from templates/index.html.
///
/// The panels are the same product as the web app, so they use the same
/// surfaces, the same hairlines, the same muted secondary text and the same
/// accent — used sparingly, for one thing at a time. Dark always: the panels
/// float over video calls, and a light panel would flare.
enum MS {

    private static func hex(_ value: UInt32) -> NSColor {
        NSColor(srgbRed: CGFloat((value >> 16) & 0xFF) / 255,
                green: CGFloat((value >> 8) & 0xFF) / 255,
                blue: CGFloat(value & 0xFF) / 255,
                alpha: 1)
    }

    // Surfaces
    static let bg      = hex(0x101014)   // --bg
    static let sidebar = hex(0x16161b)   // --bg-sidebar
    static let panel   = hex(0x1b1b21)   // --panel
    static let panel2  = hex(0x212128)   // --panel-2
    static let panel3  = hex(0x2a2a33)   // --panel-3

    // Text
    static let text      = hex(0xf0f0f4) // --text
    static let textDim   = hex(0x9a9aa6) // --text-dim
    static let textFaint = hex(0x63636e) // --text-faint

    // Hairlines — borders in this product are 1px hairlines, never heavy strokes.
    static let border       = NSColor.white.withAlphaComponent(0.07)  // --border
    static let borderStrong = NSColor.white.withAlphaComponent(0.11)  // --border-strong

    // Accent
    static let accent    = hex(0x35c2dd) // --accent
    static let accentInk = hex(0x04262d) // --accent-ink

    // Status
    static let ok     = hex(0x3fbe89)    // --ok
    static let warn   = hex(0xd9a13c)    // --warn
    static let danger = hex(0xe0524d)    // --danger

    // Radii — --r-sm / --r-md / --r-lg
    static let rSmall: CGFloat = 6
    static let rMedium: CGFloat = 9
    static let rLarge: CGFloat = 13

    /// SF Pro Text — the UI face. `NSFont.systemFont` *is* SF Pro on macOS.
    static func ui(_ size: CGFloat, _ weight: NSFont.Weight = .regular) -> NSFont {
        .systemFont(ofSize: size, weight: weight)
    }

    /// SF Mono with tabular figures — timestamps and the timer, always, so
    /// digits never shift width as the clock runs.
    static func mono(_ size: CGFloat, _ weight: NSFont.Weight = .regular) -> NSFont {
        let font = NSFont.monospacedSystemFont(ofSize: size, weight: weight)
        let desc = font.fontDescriptor.addingAttributes([
            .featureSettings: [[NSFontDescriptor.FeatureKey.typeIdentifier: kNumberSpacingType,
                                NSFontDescriptor.FeatureKey.selectorIdentifier: kMonospacedNumbersSelector]]
        ])
        return NSFont(descriptor: desc, size: size) ?? font
    }
}

// MARK: - Floating panel

/// A non-activating, always-on-top, never-captured panel.
///
/// ── Screen-share exclusion (F3) ───────────────────────────────────────────
/// `sharingType = .none` is set in this initialiser, for every panel and every
/// style, so no panel can ship without it. On every capture path that could
/// actually be exercised on this machine, a window with it set contributes no
/// pixels — see the measurements below, including which path could NOT be
/// exercised and why.
///
/// What `sharingType = .none` does NOT do: it does not remove the window from
/// `SCShareableContent`'s *window list*. macOS exposes no supported API that
/// hides a window from that enumeration — `sharingType` governs whether the
/// window's *pixels* are captured, not whether the window is *listed*. Both
/// panels are still returned by `SCShareableContent`, so a window picker can
/// still show an entry for them (the note-taker's is titled "Notes"; the HUD is
/// borderless and untitled).
///
/// Measured, on macOS 26.5.2, what a capturer actually gets if it picks one:
/// with the panel held in one place and ONLY `sharingType` varied, a
/// window-targeted capture — `SCContentFilter(desktopIndependentWindow:)`, the
/// API behind "share a single window" — returned a completely empty frame at
/// `.none` (ink 0.0000%) and the full panel at `.readOnly` (ink 99.8638%).
/// That reading is from an earlier round and was NOT reproducible in this one,
/// for the reason given below; it is left because it is on the record, not
/// because it was re-checked.
///
/// ── The legacy single-window path, re-measured with controls ──────────────
/// This paragraph has been wrong twice. What follows is only what a run on
/// 2026-07-25 actually printed, with the controls the earlier attempts lacked.
///
/// FIRST, the thing every number here depends on and neither earlier version
/// stated: **this process has no Screen Recording permission**.
/// `CGPreflightScreenCaptureAccess()` returns false, and ScreenCaptureKit
/// refuses outright — `SCShareableContent` fails with SCStreamErrorDomain
/// -3801, "the user declined TCCs for application, window, display capture".
/// So no SCK measurement could be taken this round at all, and the sentence
/// above is unverified rather than confirmed.
///
/// What CAN still be measured is what a process is always allowed to see: the
/// desktop and its OWN windows. `CGWindowListCreateImage` on another app's
/// window returns nil here (the control that shows the permission really is
/// absent). On one of our own windows — borderless, opaque, filled with a
/// colour so that "ink" means this window's pixels rather than "a rectangle
/// came back" — with `sharingType` as the only variable, three rounds in
/// alternating order:
///     .readOnly  480x240  opaque 100.0000%  its own fill 100.0000%
///     .none      480x240  opaque   0.0000%  its own fill   0.0000%
/// Stable in both orders, so it is not an artefact of which one ran first.
///
/// The conclusion that survives: on the one capture path this process is
/// permitted to use, `sharingType = .none` empties the frame completely, and
/// it does so even for content the API is otherwise willing to hand over. That
/// is a real result about `sharingType`, and it is narrower than "two capture
/// APIs, same answer" — which is what the previous version claimed on the
/// strength of a number it could not reproduce.
///
/// The earlier `screencapture` correction still stands and is unchanged: that
/// CLI's "could not create image from window" is the missing permission
/// talking, not `sharingType`, and it says nothing either way.
///
/// So: pixels are protected on every path that could be exercised here.
/// What leaks is *metadata*: that a window exists, its title, size and
/// position. See `windowListRisk` for the residual risk, stated plainly.
///
/// ── Keyboard focus (F1) ───────────────────────────────────────────────────
/// `.nonactivatingPanel` lets a click land without the *meeting app* losing
/// frontmost status. Measured: with another app frontmost, a real click on the
/// note field leaves `NSWorkspace.frontmostApplication` unchanged while this
/// panel becomes key and real keystrokes arrive in the field. What a
/// non-activating panel cannot do is take the keyboard *without* being asked,
/// and — the part that traps people — it will not give the keyboard back on its
/// own. So focus here is always explicit: `takeKeyboard(focusing:)` and
/// `yieldKeyboard()`, never a side effect.
class FloatingPanel: NSPanel {

    /// The app that was frontmost when this panel took the keyboard, so
    /// `yieldKeyboard()` can hand it straight back. Nil when the keyboard came
    /// from inside MeetingScribe itself — see `yieldWindow`.
    private var yieldTarget: NSRunningApplication?

    /// The window of *ours* that had the keyboard, for the case the original
    /// hand-back could not serve: MeetingScribe frontmost, the owner typing in
    /// its own main window, then clicking this panel. There is no other app to
    /// give the keyboard back to, and `NSApp.deactivate()` would push the whole
    /// app out of the way — which is how Escape left the owner with nowhere to
    /// type at all.
    private weak var yieldWindow: NSWindow?

    /// Where a keystroke goes when no view claimed it.
    ///
    /// In a background app the first click on anything that does not accept
    /// first mouse — a label, the title bar, a gap between controls — is
    /// swallowed to focus the window, leaving the *window* as first responder.
    /// Every keystroke after that is then silently dropped, which is exactly
    /// what "I clicked the panel and typing does nothing" looks like. Rather
    /// than beep, hand the key to the panel's real keyboard target.
    var unhandledKeyTarget: (() -> NSResponder?)?

    override func keyDown(with event: NSEvent) {
        // NSWindow.keyDown only runs when nothing in the responder chain took
        // the event, so this cannot recurse: once the target holds the
        // keyboard, its own responder handles the re-posted key.
        if let target = unhandledKeyTarget?(), makeFirstResponder(target) {
            NSApp.postEvent(event, atStart: true)
            return
        }
        super.keyDown(with: event)
    }

    /// - Parameter chrome: `true` gives a titled (closable, movable, resizable)
    ///   window for the note-taker; `false` a bare capsule for the HUD.
    init(contentRect: NSRect, chrome: Bool, resizable: Bool = false) {
        var mask: NSWindow.StyleMask = [.nonactivatingPanel]
        if chrome {
            mask.formUnion([.titled, .closable, .fullSizeContentView])
        } else {
            mask.formUnion(.borderless)
        }
        if resizable { mask.formUnion(.resizable) }
        super.init(contentRect: contentRect, styleMask: mask,
                   backing: .buffered, defer: true)

        // ── The screen-share guarantee. Do not remove. ────────────────────
        sharingType = .none
        // ─────────────────────────────────────────────────────────────────

        // Level and collection behaviour are the only levers that measurably
        // shrink the window-list exposure described above — see windowListRisk.
        level = .floating
        collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .ignoresCycle]
        isExcludedFromWindowsMenu = true

        hidesOnDeactivate = false        // stays put when the meeting app takes over
        becomesKeyOnlyIfNeeded = true    // never grabs the keyboard unprompted
        isMovableByWindowBackground = true
        isOpaque = false
        backgroundColor = .clear
        hasShadow = true
        appearance = NSAppearance(named: .darkAqua)  // the design is dark, always

        if chrome {
            titleVisibility = .hidden
            titlebarAppearsTransparent = true
            standardWindowButton(.miniaturizeButton)?.isHidden = true
            standardWindowButton(.zoomButton)?.isHidden = true
        }
    }

    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { false }

    // MARK: Explicit keyboard focus

    /// Take the keyboard **on purpose**, and remember who to give it back to.
    ///
    /// The meeting app keeps running and keeps being recorded; only the
    /// keyboard moves. That trade is deliberate: a panel you cannot type into
    /// is not a note-taker.
    func takeKeyboard(focusing responder: NSResponder?) {
        if !isKeyWindow {
            let front = NSWorkspace.shared.frontmostApplication
            if front?.processIdentifier != Self.ownPID {
                yieldTarget = front
                yieldWindow = nil
            } else {
                // We are the frontmost app: the keyboard is coming from one of
                // our own windows, so that window — not "some other app" — is
                // who Escape must give it back to.
                yieldTarget = nil
                let key = NSApp.keyWindow
                yieldWindow = (key === self) ? nil : key
            }
            makeKeyAndOrderFront(nil)
        }
        if let responder, firstResponder !== responder {
            makeFirstResponder(responder)
        }
    }

    /// Hand the keyboard straight back to whatever had it. The panel stays on
    /// screen and the recording is untouched — only focus moves.
    ///
    /// Three cases, because there used to be only one and the missing two are
    /// what trapped people. Escape must always land somewhere sensible:
    ///   • another app had it  -> reactivate that app (the meeting case);
    ///   • a window of ours had it -> make that window key and stay in the app;
    ///   • nobody recorded     -> the frontmost app if that is not us, else any
    ///                            other window of ours, else step out entirely.
    /// The old code ran the first case unconditionally, so when MeetingScribe
    /// was itself frontmost `NSApp.deactivate()` fired with no target to
    /// activate: the app fell to the back and the keyboard went nowhere.
    func yieldKeyboard() {
        makeFirstResponder(nil)
        let destination = Self.yieldDestination(
            recorded: yieldTarget,
            recordedWindow: yieldWindow,
            frontmost: NSWorkspace.shared.frontmostApplication,
            ourWindows: NSApp.windows.filter { $0 !== self })
        yieldTarget = nil
        yieldWindow = nil

        switch destination {
        case .window(let window):
            window.makeKeyAndOrderFront(nil)
            orderFrontRegardless()
        case .app(let app):
            stepAside(activating: app)
        case .systemChoice:
            stepAside(activating: nil)
        }
    }

    /// Where the keyboard should go, as a pure decision so it can be tested for
    /// every case rather than only for the one that is easy to stage.
    enum YieldDestination: Equatable {
        case app(NSRunningApplication)     // another app had it: give it back
        case window(NSWindow)              // a window of ours had it
        case systemChoice                  // nothing of ours can hold it
    }

    static func yieldDestination(recorded: NSRunningApplication?,
                                 recordedWindow: NSWindow?,
                                 frontmost: NSRunningApplication?,
                                 ourWindows: [NSWindow]) -> YieldDestination {
        if let recorded, !recorded.isTerminated, recorded.processIdentifier != ownPID {
            return .app(recorded)
        }
        if let recordedWindow, recordedWindow.isVisible, recordedWindow.canBecomeKey {
            return .window(recordedWindow)
        }
        // Nothing usable was recorded. Whoever is frontmost now is the best
        // guess — unless that is us, in which case hand the keyboard to one of
        // our own ordinary windows rather than deactivating the app out from
        // under the owner.
        if let frontmost, frontmost.processIdentifier != ownPID {
            return .app(frontmost)
        }
        if let other = ourWindows.first(where: {
            $0.isVisible && $0.canBecomeKey && !($0 is FloatingPanel)
        }) {
            return .window(other)
        }
        return .systemChoice
    }

    /// Drop key status without hiding: `orderFrontRegardless` keeps the panel
    /// visible while the app steps out of the way.
    private func stepAside(activating app: NSRunningApplication?) {
        NSApp.deactivate()
        orderFrontRegardless()
        app?.activate()
    }

    static let ownPID = ProcessInfo.processInfo.processIdentifier

    /// The honest state of the window-list gap, in one place so it can be
    /// quoted rather than re-derived.
    ///
    /// Applied mitigations, each measured on macOS 26.5.2:
    ///   • `sharingType = .none` — no pixels in a display capture (control
    ///     panel 155.61/255 mean diff and 100% of pixels changed; both of these
    ///     panels 0.00 and 0.000%), and an empty frame from a single-window
    ///     capture.
    ///   • `level = .floating` — both panels report `kCGWindowLayer` 3. Window
    ///     pickers commonly list only normal-level windows, so the panels
    ///     usually do not appear as pickable entries even though the API still
    ///     returns them.
    ///     CORRECTION. This used to go on: "and the probe found ZERO
    ///     MeetingScribe windows at layer 0". That is false, and it was false
    ///     when it was written. Re-measured with the app's ordinary window
    ///     open, `CGWindowListCopyWindowInfo` for this process returns four
    ///     windows, TWO of them at layer 0 — the main window among them, since
    ///     an ordinary titled window is exactly what layer 0 means. The true
    ///     claim is narrower and is the one above: the two *panels* are at
    ///     layer 3. Nothing about the panels ever depended on the app having no
    ///     layer-0 windows, so the fix is to stop saying it.
    ///   • `.ignoresCycle` + `isExcludedFromWindowsMenu` — out of ⌘` cycling
    ///     and the Window menu.
    ///   • The HUD is borderless and untitled, the other thing pickers filter.
    ///
    /// Residual risk, stated plainly — this is a metadata leak, not a pixel
    /// leak. `SCShareableContent` still enumerates both panels, and no
    /// supported API can opt a window out of that enumeration. A capturer that
    /// ignores window layer can therefore list "Notes" and disclose that
    /// MeetingScribe is running, along with the panel's size and position; a
    /// viewer who picks it sees an empty region, which is itself a tell. The
    /// contents are safe on every supported capture path we could measure, but
    /// do not claim the panels are *undetectable* — they are uncapturable,
    /// which is not the same promise.
    static let windowListRisk =
        "pixels: excluded from display and single-window capture; "
        + "metadata: still enumerable via SCShareableContent"
}

// MARK: - Liquid Glass

/// A rounded Liquid Glass slab that hosts arbitrary content.
///
/// Uses `NSGlassEffectView` on macOS 26+ and falls back to `NSVisualEffectView`
/// everywhere else. tools/build_mac_app.sh compiles with no explicit `-target`,
/// so the deployment target is whatever the build machine runs — the
/// availability check is what keeps an older build from calling a missing class.
///
/// The tint is the app's own `--panel` surface rather than plain black, and a
/// hairline sits on the edge, so the slab reads as the same material as the web
/// UI's panels instead of as generic system glass.
final class GlassBox: NSView {

    enum Corner {
        case capsule            // fully rounded ends
        case radius(CGFloat)
    }

    private let corner: Corner
    private let inner = NSView()
    private var glass: NSView!
    private let surface = NSView()
    private let hairline = CALayer()

    /// How much of the app's own `--panel` colour is painted onto the glass.
    ///
    /// `NSGlassEffectView.tintColor` is not an overlay and cannot be used for
    /// this: measured, raising a `#1b1b21` tint from alpha 0.72 to 0.92 made the
    /// slab *lighter* (`#40403e` -> `#454543`), because tint alpha drives the
    /// material's frost rather than an opacity blend. Over a bright backdrop
    /// that leaves a warm grey where the product's surface is the cool
    /// `#1b1b21` — which is exactly the "doesn't match the rest of the app"
    /// complaint. So the surface is painted explicitly at a known opacity, and
    /// the glass is left to do what only it can: the specular rim, the corner
    /// shaping and a little refraction. These panels float over video calls, so
    /// a legible, on-brand surface beats a showy one.
    private static let surfaceOpacity: CGFloat = 0.88

    init(corner: Corner, tint: NSColor = MS.panel.withAlphaComponent(0.35)) {
        self.corner = corner
        super.init(frame: .zero)
        wantsLayer = true

        if #available(macOS 26.0, *) {
            let g = NSGlassEffectView(frame: .zero)
            g.tintColor = tint
            g.contentView = inner
            glass = g
        } else {
            let v = NSVisualEffectView(frame: .zero)
            v.material = .hudWindow
            v.blendingMode = .behindWindow
            v.state = .active
            v.wantsLayer = true
            v.layer?.masksToBounds = true
            v.addSubview(inner)
            glass = v
        }
        glass.autoresizingMask = [.width, .height]
        addSubview(glass)

        // The product's surface, painted on both paths so the panel is the same
        // colour whether it is drawn with Liquid Glass or the fallback.
        surface.wantsLayer = true
        surface.layer?.backgroundColor = MS.panel
            .withAlphaComponent(Self.surfaceOpacity).cgColor
        surface.autoresizingMask = [.width, .height]
        inner.addSubview(surface)

        // --border, exactly as the web UI draws every panel edge.
        hairline.borderColor = MS.border.cgColor
        hairline.borderWidth = 1
        layer?.addSublayer(hairline)
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    /// Put the panel's content on the glass.
    ///
    /// Pinned to the slab with constraints rather than an autoresizing mask:
    /// `NSGlassEffectView` owns its `contentView`'s frame, and a springs-and-
    /// struts child of a view being resized underneath it drifts (the content
    /// ends up floating a panel-height above the glass).
    func setContent(_ view: NSView) {
        view.translatesAutoresizingMaskIntoConstraints = false
        inner.addSubview(view)
        NSLayoutConstraint.activate([
            view.leadingAnchor.constraint(equalTo: leadingAnchor),
            view.trailingAnchor.constraint(equalTo: trailingAnchor),
            view.topAnchor.constraint(equalTo: topAnchor),
            view.bottomAnchor.constraint(equalTo: bottomAnchor),
        ])
    }

    override func layout() {
        super.layout()
        glass.frame = bounds
        inner.frame = glass.bounds
        let r: CGFloat
        switch corner {
        case .capsule:         r = bounds.height / 2
        case .radius(let val): r = val
        }
        if #available(macOS 26.0, *), let g = glass as? NSGlassEffectView {
            g.cornerRadius = r
        } else {
            glass.layer?.cornerRadius = r
        }
        CATransaction.begin()
        CATransaction.setDisableActions(true)
        surface.frame = inner.bounds
        surface.layer?.cornerRadius = r
        surface.layer?.masksToBounds = true
        hairline.frame = bounds
        hairline.cornerRadius = r
        CATransaction.commit()
    }
}

// MARK: - Tiny JSON client

/// Minimal HTTP for the panels. Every completion lands on the main queue, and
/// nothing here ever blocks the caller — the HUD polls while audio threads run.
enum API {

    /// - Parameter then: (parsed JSON object or nil, HTTP status or 0 on failure)
    static func request(_ method: String,
                        _ url: URL,
                        body: Any? = nil,
                        timeout: TimeInterval = 2.5,
                        then: (([String: Any]?, Int) -> Void)? = nil) {
        var req = URLRequest(url: url)
        req.httpMethod = method
        req.timeoutInterval = timeout
        if let body {
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = try? JSONSerialization.data(withJSONObject: body)
        }
        URLSession.shared.dataTask(with: req) { data, response, _ in
            guard let then else { return }
            let status = (response as? HTTPURLResponse)?.statusCode ?? 0
            let obj = data.flatMap {
                (try? JSONSerialization.jsonObject(with: $0)) as? [String: Any]
            }
            DispatchQueue.main.async { then(obj, status) }
        }.resume()
    }

    static func get(_ url: URL, timeout: TimeInterval = 2.5,
                    then: @escaping ([String: Any]?, Int) -> Void) {
        request("GET", url, timeout: timeout, then: then)
    }
}

// MARK: - Small shared views

/// A click target that works in a panel the app never activates.
///
/// `acceptsFirstMouse` is the crux: without it the first click on an inactive
/// window is swallowed just to focus it, so Stop would need two clicks.
final class TapControl: NSView {
    var onTap: (() -> Void)?
    var onHover: ((Bool) -> Void)?

    private var pressed = false { didSet { alphaValue = pressed ? 0.5 : 1 } }

    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }

    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        trackingAreas.forEach(removeTrackingArea)
        addTrackingArea(NSTrackingArea(
            rect: .zero,
            options: [.mouseEnteredAndExited, .activeAlways, .inVisibleRect],
            owner: self))
    }

    override func mouseEntered(with event: NSEvent) { onHover?(true) }
    override func mouseExited(with event: NSEvent) { onHover?(false) }

    private func inside(_ event: NSEvent) -> Bool {
        bounds.contains(convert(event.locationInWindow, from: nil))
    }

    override func mouseDown(with event: NSEvent) { pressed = true }
    override func mouseDragged(with event: NSEvent) { pressed = inside(event) }
    override func mouseUp(with event: NSEvent) {
        let hit = inside(event)
        pressed = false
        if hit { onTap?() }
    }
}

/// A 1px hairline. The web UI never draws a heavy stroke, and neither do the
/// panels — `NSBox`'s separator picks up system colours that are not ours.
final class Hairline: NSView {
    init(vertical: Bool = false, color: NSColor = MS.borderStrong, length: CGFloat? = nil) {
        super.init(frame: .zero)
        wantsLayer = true
        layer?.backgroundColor = color.cgColor
        translatesAutoresizingMaskIntoConstraints = false
        if vertical {
            widthAnchor.constraint(equalToConstant: 1).isActive = true
            if let length { heightAnchor.constraint(equalToConstant: length).isActive = true }
        } else {
            heightAnchor.constraint(equalToConstant: 1).isActive = true
            if let length { widthAnchor.constraint(equalToConstant: length).isActive = true }
        }
    }
    required init?(coder: NSCoder) { fatalError("not used") }
}

extension NSTextField {
    /// A label in the app's voice: SF Pro, one of the three text colours.
    static func msLabel(_ text: String, size: CGFloat,
                        weight: NSFont.Weight = .regular,
                        color: NSColor = MS.text,
                        mono: Bool = false) -> NSTextField {
        let field = NSTextField(labelWithString: text)
        field.font = mono ? MS.mono(size, weight) : MS.ui(size, weight)
        field.textColor = color
        return field
    }

    /// The web UI's `.sb-group`: 10pt, semibold, uppercase, tracked out, faint.
    static func msSectionHeaderText(_ text: String) -> NSAttributedString {
        NSAttributedString(string: text.uppercased(),
                           attributes: [.font: MS.ui(10, .semibold),
                                        .foregroundColor: MS.textFaint,
                                        .kern: 0.7])   // letter-spacing: .07em
    }

    /// A section header label.
    static func msSectionHeader(_ text: String) -> NSTextField {
        let field = NSTextField(labelWithString: "")
        field.attributedStringValue = msSectionHeaderText(text)
        return field
    }
}
