// The one floating glass element: the transport capsule — play, waveform,
// clocks, track — present in both modes, with Ask folded in behind the
// sparkle button (⌘J). Asking expands the same piece of glass upward;
// nothing else ever floats.
import SwiftUI

enum PageMode { case document, transcript }

struct FloatingBar: View {
    let detail: MeetingDetail
    let waveform: WaveformData?
    @ObservedObject var player: Playback

    @State private var askOpen = false
    @State private var draft = ""
    @State private var busy = false
    @State private var answer: AskAnswer?
    @State private var lastQuestion = ""
    @FocusState private var askFocused: Bool

    private var duration: Double { waveform?.duration ?? detail.duration ?? 1 }
    private var peaks: [Double]? {
        guard let w = waveform else { return nil }
        return w.tracks[player.track] ?? w.tracks.values.first
    }

    var body: some View {
        VStack(spacing: 0) {
            if askOpen {
                askPanel
                Divider().opacity(0.35)
            }
            transportRow
        }
        .glassEffect(.regular.interactive(), in: .rect(cornerRadius: 22))
        .frame(maxWidth: 620)
        .shadow(color: .black.opacity(0.35), radius: 10, y: 4)   // the app's one shadow
        .padding(.bottom, 18)
        .animation(Motion.enter, value: askOpen)
    }

    // MARK: - Transport

    private var transportRow: some View {
        HStack(spacing: 11) {
            Button {
                player.toggle(meetingID: detail.id)
            } label: {
                Image(systemName: player.playing ? "pause.fill" : "play.fill")
                    .font(.system(size: 13, weight: .semibold))
                    .contentTransition(.symbolEffect(.replace))
                    .foregroundStyle(MS.ink)
                    .frame(width: 24, height: 24)
                    .contentShape(.circle)
            }
            .buttonStyle(PressStyle())

            Text(clock(player.time))
                .clockFont(11)
                .foregroundStyle(MS.ink2)
                .frame(width: 42, alignment: .trailing)

            if let peaks {
                WaveformScrubber(
                    peaks: peaks, duration: duration, time: player.time,
                    accent: MS.playheadFill
                ) { player.seek($0) }
                .frame(height: 34)
            } else {
                Slider(value: Binding(get: { min(player.time, duration) },
                                      set: { player.seek($0) }),
                       in: 0...max(duration, 1))
                    .controlSize(.small)
            }

            Text("−" + clock(max(0, duration - player.time)))
                .clockFont(11)
                .lineLimit(1)
                .fixedSize()
                .foregroundStyle(MS.ink3)

            Picker("", selection: Binding(get: { player.track },
                                          set: { player.switchTrack($0) })) {
                Text("Your mic").tag("mic")
                Text("Meeting").tag("system")
            }
            .pickerStyle(.menu)
            .controlSize(.small)
            .fixedSize()

            Button {
                withAnimation(Motion.enter) { askOpen.toggle() }
                if askOpen {
                    DispatchQueue.main.asyncAfter(deadline: .now() + 0.05) { askFocused = true }
                }
            } label: {
                Image(systemName: "sparkle")
                    .font(.system(size: 12, weight: .semibold))
                    .foregroundStyle(askOpen ? AnyShapeStyle(MS.interactive) : AnyShapeStyle(MS.ink2))
                    .frame(width: 24, height: 24)
                    .contentShape(.circle)
            }
            .buttonStyle(PressStyle())
            .keyboardShortcut("j", modifiers: .command)
            .help("Ask about this meeting (⌘J)")
        }
        .padding(.horizontal, 13)
        .padding(.vertical, 9)
    }

    // MARK: - Ask

    private var askPanel: some View {
        VStack(alignment: .leading, spacing: 8) {
            if busy || answer != nil {
                answerBlock
            }
            HStack(spacing: 8) {
                TextField(placeholder, text: $draft)
                    .textFieldStyle(.plain)
                    .font(MSFont.chrome)
                    .focused($askFocused)
                    .onSubmit(send)
                Button(action: send) {
                    Image(systemName: "arrow.up.circle.fill")
                        .font(.system(size: 18))
                        .foregroundStyle(canSend ? AnyShapeStyle(MS.interactive)
                                                 : AnyShapeStyle(MS.ink4))
                }
                .buttonStyle(PressStyle())
                .disabled(!canSend)
                Button {
                    closeAsk()
                } label: {
                    Image(systemName: "xmark.circle.fill")
                        .font(.system(size: 15))
                        .foregroundStyle(MS.ink3)
                }
                .buttonStyle(PressStyle())
                .help("Close (esc)")
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 11)
        .onExitCommand { closeAsk() }
        .transition(.asymmetric(
            insertion: .offset(y: 8).combined(with: .opacity).animation(Motion.enter),
            removal: .opacity.animation(Motion.exit)))
    }

    private func closeAsk() {
        withAnimation(Motion.exit) {
            askOpen = false
            answer = nil
        }
    }

    private var placeholder: String {
        let short = detail.title.count > 30 ? String(detail.title.prefix(30)) + "…" : detail.title
        return "Ask about \(short)"
    }

    private var canSend: Bool {
        !draft.trimmingCharacters(in: .whitespaces).isEmpty && !busy
    }

    @ViewBuilder
    private var answerBlock: some View {
        VStack(alignment: .leading, spacing: 7) {
            HStack {
                Text(lastQuestion)
                    .font(.system(size: 11, weight: .medium))
                    .foregroundStyle(MS.ink3)
                    .lineLimit(1)
                Spacer()
                Button("Clear") {
                    withAnimation(Motion.exit) { answer = nil }
                }
                .buttonStyle(.plain)
                .font(.system(size: 10.5))
                .foregroundStyle(MS.ink3)
            }
            if busy {
                HStack(spacing: 6) {
                    ProgressView().controlSize(.small)
                    Text("Reading the transcript…")
                        .font(MSFont.meta)
                        .foregroundStyle(MS.ink2)
                }
            } else if let a = answer {
                ScrollView {
                    Text(a.answer ?? a.error ?? "No answer came back.")
                        .font(.system(size: 12.5))
                        .lineSpacing(5)
                        .foregroundStyle(MS.ink)
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                .frame(maxHeight: 180)
                if let cites = a.citations, !cites.isEmpty {
                    HStack(spacing: 5) {
                        ForEach(cites, id: \.self) { c in
                            if let t = c.t {
                                Button {
                                    player.seek(t)
                                    player.play()
                                } label: {
                                    Text(clock(t))
                                        .clockFont(10)
                                        .foregroundStyle(MS.playhead)
                                        .padding(.horizontal, 6)
                                        .padding(.vertical, 2)
                                        .background(MS.raised, in: .capsule)
                                }
                                .buttonStyle(PressStyle())
                                .help(c.quote ?? "")
                            }
                        }
                    }
                }
            }
        }
    }

    private func send() {
        let q = draft.trimmingCharacters(in: .whitespaces)
        guard !q.isEmpty, !busy else { return }
        lastQuestion = q
        draft = ""
        busy = true
        answer = nil
        let id = detail.id
        Task {
            let a = (try? await API.ask(id, question: q))
                ?? AskAnswer(answer: nil, citations: nil,
                             error: "The engine didn't answer — it may still be transcribing.")
            withAnimation(Motion.enter) {
                answer = a
                busy = false
            }
        }
    }
}
