// The floating pill: ONE morphing object, four sizes — nudge, recording
// compact, expanded (hover), stopping. Corner radius tightens as it widens;
// the control row is pinned so nothing reflows; content grows downward.
// The most emotionally loaded element in the app moves the least.
import AppKit
import SwiftUI
import Combine

@MainActor
final class HUDController {
    private var panel: NSPanel?
    private var cancellable: AnyCancellable?

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
            .sink { [weak panel] phase, nudge, alert in
                let visible = phase == .recording
                    || (phase != .recording && alert != nil)
                    || (phase == .idle && nudge != nil)
                if visible {
                    panel?.orderFrontRegardless()
                } else {
                    panel?.orderOut(nil)
                }
            }
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
    @State private var hoverTask: Task<Void, Never>?
    @FocusState private var noteFocused: Bool

    private var expanded: Bool {
        hoverExpanded && center.phase == .recording
    }

    var body: some View {
        VStack(alignment: .trailing, spacing: 0) {
            content
                .glassEffect(.regular, in: .rect(
                    cornerRadius: expanded || center.alert != nil ? 18 : 22))
        }
        .padding(14)
        .animation(.easeInOut(duration: 0.3), value: center.phase)
        .animation(.easeInOut(duration: 0.3), value: center.nudge)
        .onHover { h in
            // Debounced: a graze doesn't open it, a twitch doesn't close it,
            // and the size change never re-triggers itself mid-flight.
            hoverTask?.cancel()
            hoverTask = Task {
                try? await Task.sleep(nanoseconds: h ? 100_000_000 : 260_000_000)
                guard !Task.isCancelled else { return }
                withAnimation(.spring(response: 0.32, dampingFraction: 0.85)) {
                    hoverExpanded = h
                }
            }
        }
    }

    @ViewBuilder
    private var content: some View {
        if center.phase == .recording {
            // Trailing-aligned: the panel is pinned by its top-right corner,
            // so the dot, clock and bars stay planted while the notes card
            // unfolds beneath and leftward.
            VStack(alignment: .trailing, spacing: 0) {
                controlRow
                    .frame(height: 44)
                // A Stop the engine refused: the recording is still running,
                // which is the one thing the person who pressed it does not
                // believe.
                if let alert = center.alert {
                    refusalRow(alert)
                        .frame(width: 292)
                        .padding(.bottom, 4)
                        .transition(.offset(y: -4).combined(with: .opacity))
                }
                if let alert = center.captureAlerts.first {
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
                     || !center.captureAlerts.isEmpty ? 12 : 0)
            .animation(Motion.enter, value: center.captureAlerts)
            .animation(Motion.enter, value: center.alert)
        } else if let alert = center.alert {
            refusalRow(alert)
                .frame(width: 292)
                .padding(.horizontal, 12)
                .padding(.vertical, 11)
        } else if let nudge = center.nudge {
            nudgeRow(nudge)
                .frame(height: 44)
                .padding(.horizontal, 12)
        }
    }

    // MARK: Rows

    /// The engine said no. Nothing else in the app would have said so: the
    /// record button fired and returned nothing.
    private func refusalRow(_ alert: RecorderAlert) -> some View {
        HStack(alignment: .top, spacing: 9) {
            Image(systemName: "exclamationmark.triangle")
                .font(.system(size: 12, weight: .semibold))
                .foregroundStyle(MS.ink2)
                .offset(y: 1)
            VStack(alignment: .leading, spacing: 3) {
                Text(alert.headline)
                    .font(.system(size: 12, weight: .semibold))
                    .foregroundStyle(MS.ink)
                Text(alert.message)
                    .font(.system(size: 11.5))
                    .foregroundStyle(MS.ink2)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 4)
            Button {
                center.alert = nil
            } label: {
                Image(systemName: "xmark")
                    .font(.system(size: 9, weight: .bold))
                    .foregroundStyle(MS.ink3)
            }
            .buttonStyle(PressStyle())
            .help("Dismiss")
        }
    }

    /// A track that has stopped carrying sound, said DURING the meeting. The
    /// timer used to keep running and the meter used to keep dancing.
    private func captureRow(_ alert: CaptureAlert) -> some View {
        HStack(alignment: .top, spacing: 7) {
            Image(systemName: "exclamationmark.triangle.fill")
                .font(.system(size: 10))
                .foregroundStyle(MS.ink2)
                .offset(y: 1.5)
            Text(alert.title)
                .font(.system(size: 11))
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
            Text(nudge.meeting_title?.isEmpty == false ? nudge.meeting_title! : nudge.title)
                .font(.system(size: 12, weight: .medium))
                .foregroundStyle(MS.ink)
                .lineLimit(1)
                .frame(maxWidth: 150)
            Button("Record") { center.accept(nudge) }
                .buttonStyle(PressStyle())
                .font(.system(size: 11.5, weight: .semibold))
                .foregroundStyle(MS.recordRed)
            Button {
                center.dismiss(nudge)
            } label: {
                Image(systemName: "xmark")
                    .font(.system(size: 9, weight: .bold))
                    .foregroundStyle(MS.ink3)
            }
            .buttonStyle(PressStyle())
            .help("Not this meeting")
        }
    }

    private var controlRow: some View {
        HStack(spacing: 9) {
            Button {
                center.stopRecording()
            } label: {
                RecordStateCircle(state: center.stopping ? .processing : .recording)
            }
            .buttonStyle(PressStyle())
            .help("Stop recording")

            Text(clock(center.elapsed))
                .clockFont(13)
                .foregroundStyle(MS.ink)

            TrackMeters(mic: center.micTrack, system: center.systemTrack)
        }
    }

    private var expandedContent: some View {
        VStack(alignment: .leading, spacing: 8) {
            if let last = center.sentNotes.last {
                HStack(spacing: 6) {
                    Text(last.t.map(clock) ?? "")
                        .clockFont(10)
                        .foregroundStyle(MS.ink4)
                    Text(last.text)
                        .font(.system(size: 11))
                        .foregroundStyle(MS.ink2)
                        .lineLimit(1)
                }
                .transition(.offset(y: 4).combined(with: .opacity))
            }

            TextField("Note this moment…", text: $center.noteDraft)
                .textFieldStyle(.plain)
                .font(.system(size: 12.5))
                .focused($noteFocused)
                .onSubmit { center.sendNote() }
                .padding(.horizontal, 10)
                .padding(.vertical, 7)
                .background(MS.raised.opacity(0.6), in: .rect(cornerRadius: 9))

            if let caption = latestCaption {
                Text(caption)
                    .font(.system(size: 12.5, design: .serif))
                    .foregroundStyle(MS.ink2)
                    .lineLimit(2)
                    .mask {
                        LinearGradient(
                            stops: [.init(color: .clear, location: 0),
                                    .init(color: .black, location: 0.18)],
                            startPoint: .leading, endPoint: .trailing)
                    }
                    .animation(Motion.enter, value: caption)
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
    @State private var spin = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        ZStack {
            switch state {
            case .ready:
                Circle().fill(MS.ink.opacity(0.2))
                RoundedRectangle(cornerRadius: 2.5)
                    .fill(MS.recordRed)
                    .frame(width: 8, height: 8)
            case .recording:
                Circle().fill(MS.recordRed)
                RoundedRectangle(cornerRadius: 1.5)
                    .fill(.white.opacity(0.9))
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
                .font(.system(size: 7.5))
                .foregroundStyle(lost ? MS.ink : MS.ink3)
                .frame(width: 9)
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
            .animation(.easeOut(duration: 0.12), value: track.level)
        }
        .help(lost ? "\(label): not being recorded"
                   : silent ? "\(label): silent" : label)
    }
}
