// The recording HUD: one calm Liquid Glass capsule floating over the meeting.
//
//     ●  18:42   ▁▄█▃▂  │  ■ Stop
//
// A red dot, the elapsed time in large monospaced digits, a live waveform
// driven by the real microphone level, a hairline divider, and Stop. After
// four idle seconds it fades to a faint pill so it is out of the way; it comes
// back to full strength on hover, or the moment somebody speaks.
//
// The shape is the owner's mockup and is not reinterpreted here; what changed
// is that every colour, weight, radius and gap now comes from the app's own
// palette (enum MS) instead of being invented locally — same surfaces as the
// web UI, hairline rules rather than strokes, muted secondary text, SF Mono
// with tabular figures for the clock, and the accent spent on exactly one
// thing: the live waveform.
//
// It is never visible in a screen share — see FloatingPanel (sharingType).

import AppKit

final class RecordingPanel {

    private static let idleFade: TimeInterval = 4.0
    private static let faintAlpha: CGFloat = 0.26

    private let panel: FloatingPanel
    private let baseURL: URL
    private let box = GlassBox(corner: .capsule)
    /// SF Mono, tabular figures — the web UI's .rec-timer, at HUD size.
    private let timerLabel = NSTextField.msLabel("0:00", size: 30, weight: .regular,
                                                 color: MS.text, mono: true)
    private let waveform = WaveformView()
    private let stopLabel = NSTextField.msLabel("Stop", size: 13.5, weight: .medium,
                                                color: MS.textDim)

    var onOpenApp: (() -> Void)?

    /// The id of the meeting this clock is measuring, straight off the 5 Hz
    /// recorder poll, fired only when it changes.
    ///
    /// This is the app's fast route for meeting IDENTITY. The status poll runs
    /// every 2 seconds, which is long enough for a whole stop-and-start to
    /// happen inside one window and be invisible — see RecorderRouter for the
    /// note that cost. This response carries `meeting_id` alongside `elapsed`,
    /// so the note-taker's `t` and its `meeting_id` are read from one sample
    /// and cannot end up naming different meetings.
    var onLiveMeetingID: ((String) -> Void)?
    private var liveMeetingID: String?

    // Elapsed is interpolated between polls so the clock never stutters.
    private var polledElapsed: TimeInterval = 0
    private var polledAt = Date()
    /// Where the clock stopped, once the recording did. Nil while it is running.
    ///
    /// Interpolating is right while a meeting is live and wrong the moment it
    /// is not. `hide()` also stops the poll timer, so after Stop the getter was
    /// extrapolating wall-clock seconds from a sample that would never be
    /// refreshed again: a note committed in the moments around Stop — exactly
    /// when people write "action: send the deck" — was stamped with a time
    /// later than the meeting ever reached. Freezing the clock at Stop bounds
    /// it; the note-taker additionally drops `t` altogether once `isLive` is
    /// false, so the backend stamps the note with the meeting's real duration.
    private var stoppedAt: TimeInterval?

    /// Seconds into the meeting right now — the note-taker timestamps with this.
    var currentElapsed: TimeInterval {
        if let stoppedAt { return stoppedAt }
        return max(0, polledElapsed + Date().timeIntervalSince(polledAt))
    }

    /// True while this clock is still tracking a running recording. False means
    /// `currentElapsed` is a frozen reading, not a live one.
    var isLive: Bool { stoppedAt == nil && shown }

    private var idleTimer: Timer?
    private var pollTimer: Timer?
    private var animTimer: Timer?
    private var hovering = false
    private var shown = false

    private var speechWake = SpeechWake()
    /// The dedicated endpoint is preferred; older backends only expose the
    /// recorder inside /api/status, so fall back once rather than going dark.
    private var useRecordStatus = true

    init(baseURL: URL) {
        self.baseURL = baseURL
        panel = FloatingPanel(contentRect: NSRect(x: 0, y: 0, width: 300, height: 58),
                              chrome: false)

        let dot = NSView()
        dot.wantsLayer = true
        dot.layer?.backgroundColor = MS.danger.cgColor
        dot.layer?.cornerRadius = 5
        dot.translatesAutoresizingMaskIntoConstraints = false
        dot.widthAnchor.constraint(equalToConstant: 10).isActive = true
        dot.heightAnchor.constraint(equalToConstant: 10).isActive = true

        // --border-strong, 1px: the web UI never draws a heavier rule.
        let divider = Hairline(vertical: true, color: MS.borderStrong, length: 26)

        waveform.translatesAutoresizingMaskIntoConstraints = false
        waveform.widthAnchor.constraint(equalToConstant: 33).isActive = true
        waveform.heightAnchor.constraint(equalToConstant: 22).isActive = true

        let stop = Self.makeStopControl(label: stopLabel)

        let row = NSStackView(views: [dot, timerLabel, waveform, divider, stop])
        row.orientation = .horizontal
        row.alignment = .centerY
        row.spacing = 15
        row.setCustomSpacing(13, after: dot)
        row.setCustomSpacing(16, after: timerLabel)
        row.translatesAutoresizingMaskIntoConstraints = false

        let content = HUDContentView()
        content.addSubview(row)
        NSLayoutConstraint.activate([
            row.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 22),
            row.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -20),
            row.centerYAnchor.constraint(equalTo: content.centerYAnchor),
        ])

        // Size the capsule to its contents, then keep that as the panel size.
        let width = ceil(row.fittingSize.width) + 42
        panel.setContentSize(NSSize(width: width, height: 58))
        box.frame = NSRect(x: 0, y: 0, width: width, height: 58)
        box.autoresizingMask = [.width, .height]
        box.setContent(content)
        panel.contentView = box

        stop.onTap = { [weak self] in self?.stopTapped() }
        stop.onHover = { [weak self] inside in
            self?.stopLabel.textColor = inside ? MS.text : MS.textDim
        }
        content.onHover = { [weak self] inside in self?.setHovering(inside) }
        content.onDoubleClick = { [weak self] in self?.onOpenApp?() }
    }

    /// The Stop control: a small red square glyph plus the word, quieter than
    /// the timer. A TapControl rather than an NSButton so a single click lands
    /// even though the panel never activates the app.
    private static func makeStopControl(label: NSTextField) -> TapControl {
        let square = NSView()
        square.wantsLayer = true
        square.layer?.backgroundColor = MS.danger.cgColor
        square.layer?.cornerRadius = 2.5
        square.translatesAutoresizingMaskIntoConstraints = false
        square.widthAnchor.constraint(equalToConstant: 10).isActive = true
        square.heightAnchor.constraint(equalToConstant: 10).isActive = true

        let inner = NSStackView(views: [square, label])
        inner.orientation = .horizontal
        inner.alignment = .centerY
        inner.spacing = 7
        inner.translatesAutoresizingMaskIntoConstraints = false

        let control = TapControl()
        control.addSubview(inner)
        NSLayoutConstraint.activate([
            inner.leadingAnchor.constraint(equalTo: control.leadingAnchor, constant: 2),
            inner.trailingAnchor.constraint(equalTo: control.trailingAnchor, constant: -2),
            inner.topAnchor.constraint(equalTo: control.topAnchor, constant: 6),
            inner.bottomAnchor.constraint(equalTo: control.bottomAnchor, constant: -6),
        ])
        return control
    }

    // MARK: - Show / hide

    func show() {
        guard !shown else { return }
        shown = true
        stoppedAt = nil            // a new meeting: the clock runs again
        liveMeetingID = nil        // and its id is republished on the next poll
        positionIfNeeded()
        panel.alphaValue = 1
        panel.orderFrontRegardless()
        startTimers()
        scheduleIdleFade()
    }

    func hide() {
        // Read the clock one last time while it is still meaningful, and only
        // on the way down — hide() is idempotent and a second call must not
        // re-freeze it at a later, invented time.
        if shown, stoppedAt == nil { stoppedAt = currentElapsed }
        shown = false
        stopTimers()
        panel.orderOut(nil)
    }

    var isVisible: Bool { panel.isVisible }

    /// Frame of the HUD on screen — the note-taker parks itself near it.
    var frameOnScreen: NSRect { panel.frame }

    // MARK: - State in

    /// Called by the app's status poller. `title` is unused by the capsule
    /// design (the mockup has no title) but kept so callers stay unchanged.
    func update(elapsed: TimeInterval, title: String?) {
        polledElapsed = elapsed
        polledAt = Date()
        stoppedAt = nil            // a live reading: the clock is running
        renderClock()
    }

    // MARK: - Timers

    private func startTimers() {
        stopTimers()
        // ~5 Hz: fast enough for a live waveform and prompt speech-wake,
        // cheap enough to run beside the audio threads.
        let poll = Timer(timeInterval: 0.2, repeats: true) { [weak self] _ in self?.poll() }
        RunLoop.main.add(poll, forMode: .common)
        pollTimer = poll
        // 30 fps for the bars and the clock. .common so it keeps running while
        // the user drags the panel around.
        let anim = Timer(timeInterval: 1.0 / 30.0, repeats: true) { [weak self] _ in
            self?.waveform.tick()
            self?.renderClock()
        }
        RunLoop.main.add(anim, forMode: .common)
        animTimer = anim
        poll.fire()
    }

    private func stopTimers() {
        pollTimer?.invalidate(); pollTimer = nil
        animTimer?.invalidate(); animTimer = nil
        idleTimer?.invalidate(); idleTimer = nil
    }

    private func renderClock() {
        let s = Int(currentElapsed)
        let text = s >= 3600
            ? String(format: "%d:%02d:%02d", s / 3600, (s % 3600) / 60, s % 60)
            : String(format: "%d:%02d", s / 60, s % 60)
        if timerLabel.stringValue != text { timerLabel.stringValue = text }
    }

    // MARK: - Polling

    private func poll() {
        let path = useRecordStatus ? "api/record/status" : "api/status"
        API.get(baseURL.appendingPathComponent(path), timeout: 1.5) { [weak self] obj, status in
            guard let self else { return }
            if status == 404, self.useRecordStatus {
                self.useRecordStatus = false     // older backend: use /api/status
                return
            }
            guard let obj else { return }
            // /api/record/status is flat; /api/status nests it under "recorder".
            let rec = (obj["recorder"] as? [String: Any]) ?? obj
            if let elapsed = rec["elapsed"] as? TimeInterval, rec["recording"] as? Bool == true {
                self.polledElapsed = elapsed
                self.polledAt = Date()
                // Publish identity only AFTER the clock has moved to this
                // sample, so a listener that asks "how far into the meeting are
                // we" the moment it is told the meeting changed gets the new
                // meeting's answer and not the old meeting's.
                if let id = rec["meeting_id"] as? String, id != self.liveMeetingID {
                    self.liveMeetingID = id
                    self.onLiveMeetingID?(id)
                }
            }
            let levels = rec["levels"] as? [String: Any] ?? [:]
            let mic = CGFloat(levels["mic"] as? Double ?? 0)
            let sys = CGFloat(levels["system"] as? Double ?? 0)
            self.feed(level: max(mic, sys))
        }
    }

    /// Drive the waveform and decide whether somebody just started speaking.
    private func feed(level: CGFloat) {
        waveform.level = level
        if speechWake.feed(level) { wake() }
    }

    // MARK: - Fading

    private func setHovering(_ inside: Bool) {
        guard hovering != inside else { return }
        hovering = inside
        if inside {
            idleTimer?.invalidate(); idleTimer = nil
            setFaint(false)
        } else {
            scheduleIdleFade()
        }
    }

    private func wake() {
        setFaint(false)
        if !hovering { scheduleIdleFade() }
    }

    private func scheduleIdleFade() {
        idleTimer?.invalidate()
        guard shown, !hovering else { return }
        let t = Timer(timeInterval: Self.idleFade, repeats: false) { [weak self] _ in
            self?.setFaint(true)
        }
        RunLoop.main.add(t, forMode: .common)
        idleTimer = t
    }

    private func setFaint(_ faint: Bool) {
        let target: CGFloat = faint ? Self.faintAlpha : 1.0
        guard abs(panel.alphaValue - target) > 0.01 else { return }
        NSAnimationContext.runAnimationGroup { ctx in
            ctx.duration = faint ? 0.55 : 0.18      // sink slowly, wake briskly
            ctx.timingFunction = CAMediaTimingFunction(name: .easeInEaseOut)
            panel.animator().alphaValue = target
        }
    }

    // MARK: - Actions

    private func stopTapped() {
        API.request("POST", baseURL.appendingPathComponent("api/record/stop"))
    }

    private static let frameName = "MeetingScribeHUD"

    private var positioned = false
    private func positionIfNeeded() {
        // Remember where the user dragged it; only place it the first time.
        if positioned { return }
        positioned = true
        // Register for autosaving FIRST, and on every path. This used to be the
        // last line of the method, after an early `return` taken whenever a
        // saved frame was restored — so the *second* launch restored the
        // remembered frame, never re-registered, and silently stopped saving:
        // from then on the HUD forgot every drag. setFrameAutosaveName only
        // records where to save, it does not move the window, so restoring
        // first and registering second keeps the restored position.
        let restored = panel.setFrameUsingName(Self.frameName)
        panel.setFrameAutosaveName(Self.frameName)
        if restored { return }
        if let screen = NSScreen.main {
            let f = screen.visibleFrame
            let size = panel.frame.size
            panel.setFrameOrigin(NSPoint(x: f.midX - size.width / 2, y: f.minY + 34))
        }
    }
}

// MARK: - Wake on speech

/// Decides, from the microphone level alone, whether somebody just spoke.
///
/// A value type with no window in it, so the decision can be driven by a
/// recorded level trace in a test instead of being argued about: replay a
/// meeting's mic.wav through audio_recorder.py's own arithmetic and feed the
/// series straight in.
struct SpeechWake {

    // Tuning, measured rather than guessed. Levels are `min(1, rms/6000)` from
    // audio_recorder.py. Sampling real meeting audio and a live quiet room:
    //   room tone / silence   0.015 – 0.042   (quiet-room p99 = 0.042)
    //   speech                p75 = 0.15, p90 = 0.22, loud passages 0.28 – 0.84
    // So 0.10 sits ~2.4x above the noise ceiling and below any real speech burst.
    static let speechWake: CGFloat = 0.10
    /// How far above the *tracked* room floor a sample must sit. Keeps a noisy
    /// room (fan, aircon, street) from holding the HUD awake permanently.
    static let wakeMargin: CGFloat = 0.06
    /// How far back the room estimate looks for its quietest sample: 50 samples
    /// at the HUD's 5 Hz poll = 10 seconds.
    static let floorWindow = 50

    private(set) var noiseFloor: CGFloat = 0.02
    private var loudTicks = 0
    private var recent = [CGFloat]()
    private var next = 0

    /// Feed one poll sample. Returns true when the HUD should wake.
    mutating func feed(_ level: CGFloat) -> Bool {
        let threshold = max(Self.speechWake, noiseFloor + Self.wakeMargin)
        loudTicks = level > threshold ? loudTicks + 1 : 0
        let woke = loudTicks >= 2               // ~0.4 s — a word, not a click
        trackRoom(level)
        return woke
    }

    /// Estimate the room from the *quietest* level heard in the last ten
    /// seconds, rather than from an average of everything heard.
    ///
    /// The average was the bug. It climbs toward whatever it is fed, so about
    /// seven seconds into an unbroken answer the floor had risen to speech
    /// level, the wake threshold had risen with it, and the HUD faded out
    /// mid-sentence — exactly when the person was most likely to want to glance
    /// at the timer. Measured by replaying two real meetings' microphone audio
    /// through the same arithmetic audio_recorder.py uses (1024-frame RMS /
    /// 6000, sampled at the 5 Hz poll): the average left the HUD faint while
    /// somebody was speaking for 22.8 s of one 49-minute call and 112.0 s of a
    /// 45-minute one. With the running minimum both are 0.0 s.
    ///
    /// A minimum survives speech because speech is gappy — between syllables
    /// the level drops back to room tone, and the real traces dip to 0.012
    /// several times a second — while a fan, aircon or street hum has no gaps,
    /// so its minimum *is* its level and it still stops holding the HUD awake.
    /// Re-measured on the same synthetic noise controls: a steady 0.15 fan and
    /// a 0.20 aircon both still fade after the usual four seconds.
    private mutating func trackRoom(_ level: CGFloat) {
        if recent.count < Self.floorWindow {
            recent.append(level)
        } else {
            recent[next] = level
            next = (next + 1) % Self.floorWindow
        }
        let quietest = recent.min() ?? level
        // Climbs slowly when the room gets noisier, falls faster when it
        // quietens, so the next person to speak still wakes the HUD.
        let rate: CGFloat = quietest > noiseFloor ? 0.03 : 0.08
        noiseFloor = min(0.35, max(0, noiseFloor + (quietest - noiseFloor) * rate))
    }
}

// MARK: - Content view (hover + double-click)

/// The capsule's interior. Hover has to work while another app is frontmost,
/// which is what `.activeAlways` on the tracking area buys us — a panel that
/// never activates gets no mouse events otherwise.
private final class HUDContentView: NSView {
    var onHover: ((Bool) -> Void)?
    var onDoubleClick: (() -> Void)?

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

    override func mouseDown(with event: NSEvent) {
        if event.clickCount >= 2 { onDoubleClick?() } else { super.mouseDown(with: event) }
    }
}

// MARK: - Waveform

/// Five slim bars that breathe with the real audio level.
///
/// Each bar has its own speed and phase so the group ripples instead of
/// pumping in unison, and every bar is scaled by the measured level — so it
/// looks alive while somebody talks and settles flat when the room is quiet.
final class WaveformView: NSView {

    private static let barCount = 5
    private static let barWidth: CGFloat = 3
    private static let minHeight: CGFloat = 3
    /// Level 0.45 fills the bars — real speech peaks at 0.3–0.7, so loud
    /// passages hit the ceiling and normal talking sits around two-thirds.
    private static let gain: CGFloat = 1 / 0.45
    /// Room tone measured at 0.015–0.042; subtract it so silence reads as still.
    private static let floorLevel: CGFloat = 0.03

    private let speeds: [CGFloat] = [7.3, 9.4, 8.1, 6.6, 10.2]
    private let phases: [CGFloat] = [0.0, 1.9, 0.7, 2.7, 1.3]
    private var heights = [CGFloat](repeating: minHeight, count: barCount)
    private var bars: [CALayer] = []
    private var t: CGFloat = 0

    var level: CGFloat = 0

    override init(frame: NSRect) {
        super.init(frame: frame)
        wantsLayer = true
        for _ in 0..<Self.barCount {
            let bar = CALayer()
            // The one accent in the HUD: the live level. Everything else is
            // text, hairline or danger-red.
            bar.backgroundColor = MS.accent.cgColor
            bar.cornerRadius = Self.barWidth / 2
            layer?.addSublayer(bar)
            bars.append(bar)
        }
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    /// Called at 30 fps by the panel.
    func tick() {
        t += 1.0 / 30.0
        let maxHeight = max(Self.minHeight, bounds.height)
        let energy = min(1, max(0, (level - Self.floorLevel) * Self.gain))
        for i in 0..<Self.barCount {
            let wobble = 0.55 + 0.45 * sin(t * speeds[i] + phases[i])
            let target = Self.minHeight + (maxHeight - Self.minHeight) * energy * wobble
            heights[i] += (target - heights[i]) * 0.35
        }
        layoutBars()
    }

    override func layout() {
        super.layout()
        layoutBars()
    }

    private func layoutBars() {
        guard bounds.width > 0 else { return }
        let gap = (bounds.width - CGFloat(Self.barCount) * Self.barWidth)
            / CGFloat(Self.barCount - 1)
        CATransaction.begin()
        CATransaction.setDisableActions(true)      // we animate by hand at 30 fps
        for (i, bar) in bars.enumerated() {
            let h = max(Self.minHeight, heights[i])
            bar.frame = NSRect(x: CGFloat(i) * (Self.barWidth + gap),
                               y: (bounds.height - h) / 2,
                               width: Self.barWidth, height: h)
        }
        CATransaction.commit()
    }
}
