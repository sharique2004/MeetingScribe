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

        cancellable = center.$phase.combineLatest(center.$nudge)
            .receive(on: RunLoop.main)
            .sink { [weak panel] phase, nudge in
                let visible = phase == .recording || (phase == .idle && nudge != nil)
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
                .glassEffect(.regular, in: .rect(cornerRadius: expanded ? 18 : 22))
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
                if expanded {
                    expandedContent
                        .frame(width: 292)
                        .transition(.asymmetric(
                            insertion: .offset(y: -6).combined(with: .opacity),
                            removal: .opacity))
                }
            }
            .padding(.horizontal, 12)
            .padding(.bottom, expanded ? 12 : 0)
        } else if let nudge = center.nudge {
            nudgeRow(nudge)
                .frame(height: 44)
                .padding(.horizontal, 12)
        }
    }

    // MARK: Rows

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

            DancingBars(level: max(center.micLevel, center.systemLevel))
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
                    .animation(.linear(duration: 1).repeatForever(autoreverses: false), value: spin)
                    .onAppear { spin = true }
            }
        }
        .frame(width: 21, height: 21)
        .animation(.easeOut(duration: 0.16), value: state)
    }
}

/// Three mirrored bars in mint, moving only a few points from centre —
/// alive, never frantic.
struct DancingBars: View {
    var level: Double

    var body: some View {
        TimelineView(.animation(minimumInterval: 1.0 / 30)) { context in
            let t = context.date.timeIntervalSinceReferenceDate
            let energy = 0.25 + min(level, 1) * 0.75
            HStack(spacing: 2.5) {
                bar(t: t, phase: 0.0, amp: 2.5, energy: energy)
                bar(t: t, phase: 2.1, amp: 4.0, energy: energy)
                bar(t: t, phase: 4.2, amp: 5.0, energy: energy)
            }
        }
        .frame(height: 16)
    }

    private func bar(t: Double, phase: Double, amp: Double, energy: Double) -> some View {
        let h = 5 + abs(sin(t * 3.1 + phase)) * amp * 2 * energy
        return Capsule()
            .fill(MS.playheadFill)
            .frame(width: 2.5, height: h)
    }
}
