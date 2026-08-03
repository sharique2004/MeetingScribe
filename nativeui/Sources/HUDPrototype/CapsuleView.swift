// The recording capsule: a tiny Liquid Glass pill with three states —
// offline (backend down), idle (aqua Record button), recording (pulsing dot
// · live timer · stop on hover · notes dropdown on click). The dropdown is
// the real note-taker: every line submitted files a timestamped note on the
// live meeting.
import SwiftUI

private let aqua = Color(red: 0.37, green: 0.92, blue: 0.83)

struct CapsuleRoot: View {
    @ObservedObject var model: CapsuleModel
    var onSizeChange: (CGSize) -> Void

    var body: some View {
        CapsuleView(model: model)
            .fixedSize()
            .onGeometryChange(for: CGSize.self) { $0.size } action: { onSizeChange($0) }
            .tint(aqua)
    }
}

struct CapsuleView: View {
    @ObservedObject var model: CapsuleModel
    @State private var hovering = false
    @Namespace private var glassNS
    @FocusState private var draftFocused: Bool

    var body: some View {
        GlassEffectContainer(spacing: 10) {
            VStack(alignment: .center, spacing: 10) {
                pill
                    .glassEffect(.regular.interactive(), in: .capsule)
                    .glassEffectID("pill", in: glassNS)

                if model.notesOpen && model.phase == .recording {
                    notesCard
                        .glassEffect(.regular.tint(.black.opacity(0.12)), in: .rect(cornerRadius: 16))
                        .glassEffectID("notes", in: glassNS)
                        .transition(.scale(scale: 0.95, anchor: .top).combined(with: .opacity))
                }
            }
        }
        .padding(14)
        .animation(.spring(response: 0.32, dampingFraction: 0.82), value: model.notesOpen)
        .animation(.spring(response: 0.25, dampingFraction: 0.85), value: hovering)
        .animation(.spring(response: 0.3, dampingFraction: 0.85), value: model.phase)
        .contextMenu {
            Button("Quit HUD Prototype") { NSApp.terminate(nil) }
        }
    }

    // MARK: - Pill

    @ViewBuilder
    private var pill: some View {
        switch model.phase {
        case .offline:
            HStack(spacing: 6) {
                Circle().fill(.secondary).frame(width: 7, height: 7).opacity(0.5)
                Text("offline")
                    .font(.system(size: 11, weight: .medium))
                    .foregroundStyle(.secondary)
            }
            .padding(.horizontal, 11)
            .frame(height: 28)
            .help("MeetingScribe isn't running")

        case .idle:
            Button {
                model.startRecording()
            } label: {
                HStack(spacing: 5) {
                    Image(systemName: "record.circle")
                        .font(.system(size: 11, weight: .semibold))
                        .foregroundStyle(aqua)
                    Text("Record")
                        .font(.system(size: 11, weight: .semibold))
                        .foregroundStyle(.primary)
                }
                .padding(.horizontal, 12)
                .frame(height: 28)
                .contentShape(.capsule)
            }
            .buttonStyle(.plain)
            .help("Start recording this meeting")

        case .recording:
            HStack(spacing: 7) {
                RecordingDot()

                Text(model.timeString)
                    .font(.system(size: 12, weight: .semibold, design: .rounded))
                    .monospacedDigit()
                    .foregroundStyle(.primary)
                    .contentTransition(.numericText())

                if hovering {
                    PillButton(symbol: "stop.fill", help: "Stop recording") {
                        model.stopRecording()
                    }
                    .transition(.scale(scale: 0.8).combined(with: .opacity))
                }

                Image(systemName: "chevron.down")
                    .font(.system(size: 8, weight: .bold))
                    .foregroundStyle(.secondary)
                    .rotationEffect(.degrees(model.notesOpen ? 180 : 0))
            }
            .padding(.horizontal, 11)
            .frame(height: 28)
            .contentShape(.capsule)
            .onHover { hovering = $0 }
            .onTapGesture { toggleNotes() }
        }
    }

    private func toggleNotes() {
        model.notesOpen.toggle()
        if model.notesOpen {
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.05) { draftFocused = true }
        }
    }

    // MARK: - Notes dropdown

    private var notesCard: some View {
        VStack(spacing: 0) {
            ScrollViewReader { proxy in
                ScrollView {
                    VStack(alignment: .leading, spacing: 7) {
                        if model.sentNotes.isEmpty {
                            Text("Notes land on the meeting, stamped at the moment you hit ⏎")
                                .font(.system(size: 11))
                                .foregroundStyle(.tertiary)
                                .padding(.top, 2)
                        }
                        ForEach(model.sentNotes) { note in
                            HStack(alignment: .firstTextBaseline, spacing: 7) {
                                Text(stamp(note.t))
                                    .font(.system(size: 10, weight: .medium, design: .monospaced))
                                    .foregroundStyle(.tertiary)
                                Text(note.text)
                                    .font(.system(size: 12))
                                    .foregroundStyle(.primary.opacity(0.85))
                                    .textSelection(.enabled)
                            }
                            .id(note.id)
                        }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.horizontal, 12)
                    .padding(.top, 10)
                }
                .onChange(of: model.sentNotes.count) {
                    if let last = model.sentNotes.last {
                        withAnimation { proxy.scrollTo(last.id, anchor: .bottom) }
                    }
                }
            }

            Divider().opacity(0.4)

            TextField("Jot a note…", text: $model.draft, axis: .vertical)
                .textFieldStyle(.plain)
                .font(.system(size: 12.5))
                .lineLimit(1...4)
                .padding(.horizontal, 12)
                .padding(.vertical, 9)
                .focused($draftFocused)
                .onSubmit { model.sendNote() }
                .overlay(alignment: .trailing) {
                    if model.noteSendFailed {
                        Image(systemName: "exclamationmark.circle.fill")
                            .font(.system(size: 11))
                            .foregroundStyle(.orange)
                            .padding(.trailing, 10)
                            .help("Couldn't save — press ⏎ to retry")
                    }
                }
        }
        .frame(width: 264, height: 190)
    }

    private func stamp(_ t: Double?) -> String {
        guard let t else { return "  –  " }
        let s = Int(t)
        return String(format: "%d:%02d", s / 60, s % 60)
    }
}

// MARK: - Pieces

private struct RecordingDot: View {
    @State private var dim = false

    var body: some View {
        Circle()
            .fill(Color.red)
            .frame(width: 7, height: 7)
            .opacity(dim ? 0.35 : 1)
            .animation(.easeInOut(duration: 0.9).repeatForever(autoreverses: true), value: dim)
            .onAppear { dim = true }
    }
}

private struct PillButton: View {
    let symbol: String
    let help: String
    let action: () -> Void
    @State private var over = false

    var body: some View {
        Button(action: action) {
            Image(systemName: symbol)
                .font(.system(size: 9, weight: .bold))
                .foregroundStyle(over ? .primary : .secondary)
                .frame(width: 18, height: 18)
                .background(.primary.opacity(over ? 0.12 : 0), in: .circle)
        }
        .buttonStyle(.plain)
        .onHover { over = $0 }
        .help(help)
    }
}
