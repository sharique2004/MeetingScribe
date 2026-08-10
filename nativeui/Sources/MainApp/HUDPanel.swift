// The floating pill: ONE morphing object, four sizes — nudge, recording
// compact, expanded (hover), stopping. Corner radius tightens as it widens;
// the control row is pinned so nothing reflows; content grows downward.
// The most emotionally loaded element in the app moves the least.
import AppKit
import SwiftUI
import Combine

@MainActor
final class HUDController {
    /// One pill for the life of the app, attached from
    /// applicationDidFinishLaunching.
    ///
    /// It used to be `@State` on ContentView, which tied the app's only
    /// always-visible surface to the main window: closing that window took the
    /// controller down with it, and the subscription that decides whether the
    /// pill is on screen went with the controller. The pill exists precisely
    /// for the moments the window is not there.
    static let shared = HUDController()

    private var panel: NSPanel?
    private var cancellable: AnyCancellable?
    // The last nudge id this controller centred the pill for. A nudge is an
    // interruption, not a status readout, so each NEW nudge plants the pill
    // bottom-centre — the spot the eye already scans for meeting controls —
    // while the id check keeps one nudge to one placement: dragging the pill
    // away mid-nudge must not fight the user's hand on the next poll tick.
    private var centeredForNudgeID: String?

    private init() {}

    func attach(center: RecorderCenter) {
        guard panel == nil else { return }
        let panel = HUDWindow(
            contentRect: NSRect(x: 0, y: 0, width: 176, height: 72),
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered, defer: false)
        panel.isFloatingPanel = true
        panel.level = .floating
        panel.backgroundColor = .clear
        panel.isOpaque = false
        panel.hasShadow = false
        panel.isMovableByWindowBackground = true
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        panel.becomesKeyOnlyIfNeeded = true
        panel.hidesOnDeactivate = false
        // Invisible to screen recordings and screen shares — the pill is for
        // the person recording, never for the people they're presenting to.
        panel.sharingType = .none

        let root = HUDRoot(center: center) { [weak panel] size in
            guard let panel, panel.frame.size != size else { return }
            let f = panel.frame
            panel.setFrame(
                NSRect(x: f.maxX - size.width, y: f.maxY - size.height,
                       width: size.width, height: size.height),
                display: true)
        }
        panel.contentView = NSHostingView(rootView: root)

        let restored = panel.setFrameUsingName("MSNativePill")
        panel.setFrameAutosaveName("MSNativePill")
        if !restored, let screen = NSScreen.main {
            let v = screen.visibleFrame
            panel.setFrameTopLeftPoint(NSPoint(x: v.maxX - panel.frame.width - 16,
                                               y: v.maxY - 10))
        }
        self.panel = panel

        // The pill is also the app's one always-visible surface, so it is
        // where a refused Record or Stop has to appear: the toolbar button
        // that raised it may be behind a meeting page, or behind nothing at
        // all if the window is closed.
        cancellable = center.$phase
            .combineLatest(center.$nudge, center.$alert)
            .receive(on: RunLoop.main)
            .sink { [weak self, weak panel] phase, nudge, alert in
                let visible = phase == .recording
                    || (phase != .recording && alert != nil)
                    || (phase == .idle && nudge != nil)
                if visible {
                    if phase == .idle, let nudge, nudge.id != self?.centeredForNudgeID {
                        self?.centeredForNudgeID = nudge.id
                        self?.moveToBottomCenter()
                    }
                    panel?.orderFrontRegardless()
                } else {
                    panel?.orderOut(nil)
                }
            }
    }

    /// The nudge landing spot: just right of bottom-centre, on whichever
    /// screen the pill lives (falling back to the main screen). The offsets
    /// are taste, dialled in by the owner — right of centre so it clears
    /// whatever sits mid-screen in a call, and high enough off the visible
    /// edge (which already excludes the Dock) to read as "above the Dock",
    /// not "on it".
    private func moveToBottomCenter() {
        guard let panel else { return }
        guard let screen = panel.screen ?? NSScreen.main else { return }
        let v = screen.visibleFrame
        panel.setFrameOrigin(NSPoint(x: v.midX - panel.frame.width / 2 + 160,
                                     y: v.minY + 96))
    }
}

final class HUDWindow: NSPanel {
    override var canBecomeKey: Bool { true }
}

// MARK: - Pill

private struct HUDRoot: View {
    @ObservedObject var center: RecorderCenter
    var onSize: (CGSize) -> Void

    var body: some View {
        HUDPill(center: center)
            .fixedSize()
            .onGeometryChange(for: CGSize.self) { $0.size } action: { onSize($0) }
            .tint(MS.interactive)
    }
}

struct HUDPill: View {
    @ObservedObject var center: RecorderCenter
    @State private var hoverExpanded = false
    /// Opened by a click, and STICKY: hover opens the card while the pointer
    /// is on the pill, this is what keeps it open once the pointer leaves so
    /// there is a note field to walk to.
    @State private var clickExpanded = false
    @State private var hoverTask: Task<Void, Never>?
    @FocusState private var noteFocused: Bool

    private var expanded: Bool {
        (hoverExpanded || clickExpanded) && center.phase == .recording
    }

    /// The control row is the pill's whole height until something unfolds
    /// under it, which is what makes the collapsed shape a capsule rather
    /// than a rounded rect that happens to look like one.
    static let rowHeight: CGFloat = 44

    /// One radius for the glass and the rim, so the contact line can never
    /// disagree with the surface it traces. Collapsed it is half the row —
    /// the capsule; carrying a card it tightens to the container token, and
    /// because both states are the same shape TYPE the morph still animates
    /// rather than snapping.
    private var pillRadius: CGFloat {
        expanded || center.alert != nil ? MS.radius.xl : Self.rowHeight / 2
    }

    var body: some View {
        VStack(alignment: .trailing, spacing: 0) {
            content
                .glassEffect(.regular, in: .rect(cornerRadius: pillRadius))
                // The uniform contact rim, not the lit edge: this pill sits
                // over an arbitrary desktop, where a top-lit gradient would
                // be claiming a light source the backdrop doesn't have.
                .msRim(MS.rr(pillRadius))
                .msElevation(.overlay)
        }
        // The padding is the ramp's room. This panel is exactly as large as
        // SwiftUI reports, a shadow is not part of that measurement, and
        // anything reaching past the padding is cut off at the window edge —
        // a hard rectangle in the penumbra, over whatever the user is
        // presenting. The bottom carries the most because every layer is
        // offset downward, and growing it is free where the others aren't:
        // the panel is pinned by its top-right corner, so the pill does not
        // move when the empty box below it does.
        //
        // Empty padding is not free in the other sense: it is window
        // background, so it drags the pill rather than passing the click
        // down (see the drag handoff in controlRow). This is the smallest
        // halo that keeps the ramp's falloff under ~7% at the cut.
        .padding(.horizontal, 22)
        .padding(.top, 16)
        .padding(.bottom, 40)
        .msAnimation(Motion.enter, value: center.phase)
        .msAnimation(Motion.enter, value: center.nudge)
        .onHover { h in
            // Debounced: a graze doesn't open it, a twitch doesn't close it,
            // and the size change never re-triggers itself mid-flight.
            hoverTask?.cancel()
            hoverTask = Task {
                try? await Task.sleep(for: h ? .milliseconds(100) : .milliseconds(260))
                guard !Task.isCancelled else { return }
                msWithAnimation(Motion.springy) { hoverExpanded = h }
            }
        }
        // The note field is the reason the card exists, and it used to open
        // with the cursor nowhere: every note began with a second click nobody
        // was told to make.
        //
        // Focus follows the CLICK and not the hover. Clicking makes this panel
        // key (see HUDWindow.canBecomeKey), so the cursor lands somewhere that
        // can actually receive what gets typed next; hovering deliberately does
        // not — the panel is non-activating so that Zoom keeps the keyboard —
        // and taking first responder there would put a cursor in a window
        // nothing can be typed into.
        .onChange(of: expanded) { _, open in
            noteFocused = open && clickExpanded
        }
        // A card pinned open must not outlive the recording it belongs to, or
        // the next meeting starts with a note field sitting over the screen.
        .onChange(of: center.phase) { _, phase in
            if phase != .recording { clickExpanded = false }
        }
    }

    /// Click to open, click to close, hover unchanged.
    ///
    /// `hoverExpanded` is cleared on the way down because the pointer is still
    /// sitting on the pill as the card collapses under it: left set, it would
    /// re-open the card on the same frame and the click would look like it did
    /// nothing. Hover comes back the next time the pointer leaves and returns,
    /// which is the only thing onHover reports anyway.
    private func toggleExpanded() {
        guard center.phase == .recording else { return }
        let next = !expanded
        hoverTask?.cancel()      // a debounce in flight must not overrule this
        msWithAnimation(Motion.springy) {
            clickExpanded = next
            hoverExpanded = next
        }
    }

    @ViewBuilder
    private var content: some View {
        // Read once. `captureAlerts` builds its list from scratch on every
        // access, and this body ran it three times for one answer — four
        // times a second, for the length of a meeting.
        let alerts = center.captureAlerts
        if center.phase == .recording {
            // Trailing-aligned: the panel is pinned by its top-right corner,
            // so the dot, clock and bars stay planted while the notes card
            // unfolds beneath and leftward.
            VStack(alignment: .trailing, spacing: 0) {
                controlRow
                    .frame(height: Self.rowHeight)
                // A Stop the engine refused: the recording is still running,
                // which is the one thing the person who pressed it does not
                // believe.
                if let alert = center.alert {
                    refusalRow(alert)
                        .frame(width: 292)
                        .padding(.bottom, 4)
                        .transition(.offset(y: -4).combined(with: .opacity))
                }
                if let alert = alerts.first {
                    captureRow(alert)
                        .frame(width: expanded ? 292 : 220)
                        .transition(.offset(y: -4).combined(with: .opacity))
                }
                if expanded {
                    expandedContent
                        .frame(width: 292)
                        .transition(.asymmetric(
                            insertion: .offset(y: -6).combined(with: .opacity),
                            removal: .opacity))
                }
            }
            .padding(.horizontal, 12)
            .padding(.bottom, expanded || center.alert != nil
                     || !alerts.isEmpty ? 12 : 0)
            .msAnimation(Motion.enter, value: alerts)
            .msAnimation(Motion.enter, value: center.alert)
        } else if let alert = center.alert {
            refusalRow(alert)
                .frame(width: 292)
                .padding(.horizontal, 12)
                .padding(.vertical, 11)
        } else if let nudge = center.nudge {
            nudgeRow(nudge)
                .frame(height: Self.rowHeight)
                .padding(.horizontal, 12)
        }
    }

    // MARK: Rows

    /// The engine said no. Nothing else in the app would have said so: the
    /// record button fired and returned nothing.
    private func refusalRow(_ alert: RecorderAlert) -> some View {
        HStack(alignment: .top, spacing: 9) {
            Image(systemName: "exclamationmark.triangle")
                .font(MSFont.hudLabel)
                .foregroundStyle(MS.ink2)
                .offset(y: 1)
            VStack(alignment: .leading, spacing: 3) {
                Text(alert.headline)
                    .font(MSFont.hudLabel)
                    .foregroundStyle(MS.ink)
                Text(alert.message)
                    .font(MSFont.hudCaption)
                    .foregroundStyle(MS.ink2)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 4)
            Button {
                center.alert = nil
            } label: {
                Image(systemName: "xmark")
                    .font(MSFont.hudCaption.weight(.bold))
                    .foregroundStyle(MS.ink3)
            }
            .buttonStyle(PressStyle())
            .help("Dismiss")
            .accessibilityLabel("Dismiss")
        }
    }

    /// A track that has stopped carrying sound, said DURING the meeting. The
    /// timer used to keep running and the meter used to keep dancing.
    private func captureRow(_ alert: CaptureAlert) -> some View {
        HStack(alignment: .top, spacing: 7) {
            Image(systemName: "exclamationmark.triangle.fill")
                .font(MSFont.hudCaption)
                .foregroundStyle(MS.ink2)
                .offset(y: 1.5)
            Text(alert.title)
                .font(MSFont.hudCaption)
                .foregroundStyle(MS.ink)
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 0)
        }
        .padding(.top, 2)
        .help(alert.detail)
    }

    private func nudgeRow(_ nudge: Nudge) -> some View {
        HStack(spacing: 8) {
            RecordStateCircle(state: .ready)
            Text(nudge.meeting_title.flatMap { $0.isEmpty ? nil : $0 } ?? nudge.title)
                .font(MSFont.hudLabel.weight(.medium))
                .foregroundStyle(MS.ink)
                .lineLimit(1)
                .frame(maxWidth: 150)
            Button("Record") { center.accept(nudge) }
                .buttonStyle(PressStyle())
                .font(MSFont.hudLabel)
                .foregroundStyle(MS.recordRed)
            Button {
                center.dismiss(nudge)
            } label: {
                Image(systemName: "xmark")
                    .font(MSFont.hudCaption.weight(.bold))
                    .foregroundStyle(MS.ink3)
            }
            .buttonStyle(PressStyle())
            .help("Not this meeting")
            .accessibilityLabel("Not this meeting")
        }
    }

    private var controlRow: some View {
        HStack(spacing: 9) {
            Button {
                center.stopRecording()
            } label: {
                // The same level the mic meter below is drawing, spent twice:
                // as a bar that can be read, and as light around the record
                // dot that can be seen from across a desk.
                RecordStateCircle(state: center.stopping ? .processing : .recording,
                                  level: center.micTrack.level)
            }
            .buttonStyle(PressStyle())
            .help("Stop recording")
            // The label is a shape, so without these the one control that
            // ends a meeting is announced as "button" and answers to nothing.
            .accessibilityLabel("Stop recording")
            .accessibilityInputLabels(["Stop", "Stop recording"])

            Text(clock(center.elapsed))
                .clockFont(13)
                .foregroundStyle(MS.ink)

            TrackMeters(mic: center.micTrack, system: center.systemTrack)

            expandChevron
        }
        // The whole collapsed pill opens the card, not just the chevron: the
        // card used to answer to hover and nothing else, which is unreachable
        // by keyboard, invisible to anyone who does not happen to park the
        // pointer here, and gone the moment the pointer moves. The child
        // buttons keep their own clicks — a Button inside this row wins the
        // hit test — so Stop still stops.
        .contentShape(Rectangle())
        .onTapGesture { toggleExpanded() }
        // Handling clicks over these pixels is what stops AppKit moving the
        // window from them (isMovableByWindowBackground only drags from parts
        // no view is taking clicks on), so the drag is handed back explicitly.
        // The pill floats over whatever the user is presenting; it has to stay
        // movable. No event to drag with, no drag — which is exactly where
        // this started.
        .simultaneousGesture(
            DragGesture(minimumDistance: 4).onChanged { _ in
                if let event = NSApp.currentEvent {
                    event.window?.performDrag(with: event)
                }
            })
    }

    /// The hint that the pill opens at all.
    ///
    /// DESIGN.md put a chevron on the collapsed capsule from the first draft
    /// and the native pill shipped without one, so the note field — the whole
    /// reason for the card underneath — was a secret kept by the pointer.
    ///
    /// It is a real Button rather than a trait on the row above: the row also
    /// holds Stop and the two track meters, each of which already says what it
    /// is, and a button trait on their container relabels all three at once.
    private var expandChevron: some View {
        Button {
            toggleExpanded()
        } label: {
            Image(systemName: "chevron.down")
                .font(MSFont.hudCaption.weight(.bold))
                .foregroundStyle(MS.ink3)
                .rotationEffect(.degrees(expanded ? 180 : 0))
        }
        .buttonStyle(PressStyle())
        .msAnimation(Motion.enter, value: expanded)
        .help(expanded ? "Hide the note field" : "Take a note")
        .accessibilityAddTraits(.isButton)
        .accessibilityLabel(expanded ? "Hide the note field" : "Take a note")
        .accessibilityValue(expanded ? "Open" : "Closed")
    }

    private var expandedContent: some View {
        VStack(alignment: .leading, spacing: 8) {
            if let last = center.sentNotes.last {
                HStack(spacing: 6) {
                    Text(last.t.map(clock) ?? "")
                        .clockFont(10)
                        .foregroundStyle(MS.ink4)
                    Text(last.text)
                        .font(MSFont.hudCaption)
                        .foregroundStyle(MS.ink2)
                        .lineLimit(1)
                }
                .transition(.offset(y: 4).combined(with: .opacity))
            }

            TextField("Note this moment…", text: $center.noteDraft)
                .textFieldStyle(.plain)
                .font(MSFont.hudBody)
                .focused($noteFocused)
                .onSubmit { center.sendNote() }
                .padding(.horizontal, 10)
                .padding(.vertical, 7)
                .background(MS.raised.opacity(0.6), in: .rect(cornerRadius: MS.radius.sm))

            if let caption = latestCaption {
                Text(caption)
                    .font(MSFont.hudBody)
                    .foregroundStyle(MS.ink2)
                    .lineLimit(2)
                    .mask {
                        LinearGradient(
                            stops: [.init(color: .clear, location: 0),
                                    .init(color: .black, location: 0.18)],
                            startPoint: .leading, endPoint: .trailing)
                    }
                    .msAnimation(Motion.enter, value: caption)
            }
        }
    }

    private var latestCaption: String? {
        for key in center.livePartials.keys.sorted() {
            if let t = center.livePartials[key]?.text, !t.isEmpty { return t }
        }
        return center.liveTurns.last?.text
    }
}

// MARK: - Pieces

/// One circle, three states, nothing changes position.
struct RecordStateCircle: View {
    enum RecState { case ready, recording, processing }
    let state: RecState
    /// The mic's live level, 0–1. Only the recording state reads it, and it
    /// defaults to silence so the nudge's ready dot stays a one-liner.
    var level: Double = 0
    @State private var spin = false
    /// The release envelope, held as two numbers rather than a per-frame
    /// accumulator: the loudest sample not yet decayed away, and when it
    /// landed. Everything the ring draws is a pure function of those and the
    /// timeline's clock, so the body never writes state while it renders.
    @State private var peak: Double = 0
    @State private var peakAt = Date.distantPast
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    /// Release: 1.6 e-foldings a second, so a shout is most of the way gone
    /// ~600ms after it ends. Attack has no rate — a louder sample IS the new
    /// peak, on the frame it arrives, because the ear hears the transient and
    /// a ring that ramps up to it would be reporting the past.
    private static let release: Double = 1.6
    /// Silence is not a dead ring: a 1.8s swell between 0.05 and 0.25 that
    /// every level rides on top of. "Recording" and "recording and hearing
    /// nothing" have to look different without either looking broken.
    private static let breathPeriod: Double = 1.8

    var body: some View {
        ZStack {
            switch state {
            case .ready:
                Circle().fill(MS.ink.opacity(0.2))
                RoundedRectangle(cornerRadius: 2.5)
                    .fill(MS.recordRed)
                    .frame(width: 8, height: 8)
            case .recording:
                levelRing
                Circle().fill(MS.recordRed)
                RoundedRectangle(cornerRadius: 1.5)
                    .fill(MS.inkOnRecord.opacity(0.9))
                    .frame(width: 7, height: 7)
            case .processing:
                Circle()
                    .trim(from: 0.1, to: 0.9)
                    .stroke(MS.ink2, style: StrokeStyle(lineWidth: 2, lineCap: .round))
                    .frame(width: 12, height: 12)
                    .rotationEffect(.degrees(spin ? 360 : 0))
                    // A spinner that never stops is exactly what Reduce Motion
                    // is for. Without the guard it turns for the life of the
                    // panel whatever the user asked the system for.
                    .animation(reduceMotion ? nil
                               : .linear(duration: 1).repeatForever(autoreverses: false),
                               value: spin)
                    .onAppear { spin = !reduceMotion }
            }
        }
        .frame(width: 21, height: 21)
        .animation(.easeOut(duration: 0.16), value: state)
        // Instant attack, uninterrupted release: the curve restarts from
        // whichever is higher — the sample that just landed, or what is left
        // of the last one — so a quieter sample can never lift the ring, and
        // never cuts a fall short either. The engine sends levels every
        // 350ms; the timeline below fills in between them.
        .onChange(of: level) { _, new in
            let now = Date()
            peak = max(new, released(at: now))
            peakAt = now
        }
    }

    /// The level, as light around the dot. The meter beside it says the same
    /// thing in a bar you can read; this says it in the record light itself,
    /// which is the part of the pill a glance actually lands on.
    @ViewBuilder
    private var levelRing: some View {
        let ring = Circle().stroke(MS.recordRed, lineWidth: 2)
        if reduceMotion {
            // A still ring, not a deleted one: the shape stays, the movement
            // goes, and the meter next to it is already saying "recording" in
            // words for anyone who needs them.
            ring.opacity(0.2)
        } else {
            TimelineView(.animation(minimumInterval: 1.0 / 20)) { context in
                let d = displayed(at: context.date)
                ring
                    .scaleEffect(1 + 0.35 * d)
                    .opacity(0.25 + 0.5 * d)
            }
        }
    }

    /// What the ring is showing at `date`: whatever is loudest of the breath,
    /// the level standing right now, and what is left of the last peak. The
    /// first two are the target the release falls toward — a steady tone
    /// holds the ring up for as long as it lasts, and silence lands it on the
    /// breath rather than on nothing.
    private func displayed(at date: Date) -> Double {
        let t = date.timeIntervalSinceReferenceDate
        let breath = 0.15 + 0.1 * sin(t * 2 * .pi / Self.breathPeriod)
        return min(1, max(max(breath, level), released(at: date)))
    }

    /// What is left of the last peak, `release` e-foldings a second later.
    private func released(at date: Date) -> Double {
        let dt = date.timeIntervalSince(peakAt)
        guard dt > 0 else { return peak }
        return peak * exp(-Self.release * dt)
    }
}

/// The two tracks, side by side and never merged.
///
/// This used to be one bar fed by max(mic, system), which is the one
/// arrangement that CANNOT show the failure it exists to show: while you talk
/// your own mic pins the bar, so a system track recording pure silence looks
/// exactly like a healthy meeting until the transcript comes back with half
/// the conversation missing. Two meters, each fed by its own track, and a
/// glyph that names which is which.
struct TrackMeters: View {
    let mic: RecorderTrack
    let system: RecorderTrack

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            TrackMeter(glyph: "mic.fill", track: mic, label: "You")
            TrackMeter(glyph: "speaker.wave.2.fill", track: system, label: "The meeting")
        }
    }
}

/// One track: its own glyph, its own level, and a bar that goes hollow the
/// moment nothing is reaching it.
struct TrackMeter: View {
    let glyph: String
    let track: RecorderTrack
    let label: String
    var width: CGFloat = 30

    private var lost: Bool { !track.present || !track.alive }
    private var silent: Bool { track.level <= RecorderTrack.silenceFloor }

    var body: some View {
        HStack(spacing: 4) {
            Image(systemName: lost ? "exclamationmark.triangle.fill" : glyph)
                .font(MSFont.hudCaption)
                .foregroundStyle(lost ? MS.ink : MS.ink3)
                .frame(width: 11)
            ZStack(alignment: .leading) {
                Capsule()
                    .fill(MS.ink4.opacity(0.3))
                if !lost {
                    Capsule()
                        .fill(MS.playheadFill)
                        .frame(width: max(1.5, width * min(1, track.level * 1.7)))
                }
            }
            .frame(width: width, height: 3)
            .msAnimation(Motion.micro, value: track.level)
        }
        .help(lost ? "\(label): not being recorded"
                   : silent ? "\(label): silent" : label)
        // The glyph and the bar say it in shapes; this says it in words, so
        // "is this being recorded" is answerable without seeing the meter.
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(label)
        .accessibilityValue(lost ? "Not being recorded"
                            : silent ? "Silent" : "Recording")
    }
}
