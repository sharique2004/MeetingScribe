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

    @StateObject private var ask = AskConversation()
    @State private var askOpen = false
    @State private var draft = ""
    @FocusState private var askFocused: Bool

    private var duration: Double { waveform?.duration ?? detail.duration ?? 1 }
    private var peaks: [Double]? {
        guard let w = waveform else { return nil }
        return w.tracks[player.track] ?? w.tracks["mix"] ?? w.tracks.values.first
    }

    var body: some View {
        VStack(spacing: 0) {
            if askOpen {
                askPanel
                Divider().opacity(0.35)
            }
            if let failure = player.failure {
                transportNotice(failure)
                Divider().opacity(0.35)
            }
            transportRow
        }
        .glassEffect(.regular.interactive(), in: .rect(cornerRadius: 22))
        .frame(maxWidth: 620)
        .shadow(color: .black.opacity(0.35), radius: 10, y: 4)   // the app's one shadow
        .padding(.bottom, 18)
        .animation(Motion.enter, value: askOpen)
        .animation(Motion.enter, value: player.failure)
    }

    // MARK: - Transport

    /// What went wrong with the audio, in one line, above the controls that
    /// can't do anything about it.
    private func transportNotice(_ text: String) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 7) {
            Image(systemName: "exclamationmark.triangle")
                .font(.system(size: 10))
                .foregroundStyle(MS.ink3)
            Text(text)
                .font(MSFont.meta)
                .foregroundStyle(MS.ink2)
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 8)
    }

    private var transportRow: some View {
        HStack(spacing: 11) {
            Button {
                player.toggle()
            } label: {
                Image(systemName: player.playing ? "pause.fill" : "play.fill")
                    .font(.system(size: 13, weight: .semibold))
                    .contentTransition(.symbolEffect(.replace))
                    .foregroundStyle(player.hasAudio ? MS.ink : MS.ink4)
                    .frame(width: 24, height: 24)
                    .contentShape(.circle)
            }
            .buttonStyle(PressStyle())
            .disabled(!player.hasAudio)
            .help(player.playing ? "Pause" : "Play this meeting")

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
                    .disabled(!player.hasAudio)
            }

            Text("−" + clock(max(0, duration - player.time)))
                .clockFont(11)
                .lineLimit(1)
                .fixedSize()
                .foregroundStyle(MS.ink3)

            // Only what this meeting has. A recording made in person on one
            // device has no meeting track, and offering it was offering a 404.
            if !player.choices.isEmpty {
                Picker("", selection: Binding(get: { player.track },
                                              set: { player.switchTrack($0) })) {
                    ForEach(player.choices) { choice in
                        Text(choice.label).tag(choice.id)
                    }
                }
                .pickerStyle(.menu)
                .controlSize(.small)
                .fixedSize()
                .help("Which track you hear. Everything stays in step either way")
            }

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
        VStack(alignment: .leading, spacing: 10) {
            if ask.messages.isEmpty {
                suggestions
            } else {
                thread
            }
            composer
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 11)
        // A question whose answer never landed comes back to the box rather
        // than being retyped.
        .onChange(of: ask.retry) {
            guard let question = ask.retry else { return }
            if draft.trimmingCharacters(in: .whitespaces).isEmpty { draft = question }
            ask.retry = nil
        }
        .onExitCommand { closeAsk() }
        .transition(.asymmetric(
            insertion: .offset(y: 8).combined(with: .opacity).animation(Motion.enter),
            removal: .opacity.animation(Motion.exit)))
    }

    /// Somewhere to start. Three that ask themselves, and one that hands the
    /// composer a half-written question for the user to finish.
    private var suggestions: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Answered from this transcript alone, with the moment it was said.")
                .font(MSFont.meta)
                .foregroundStyle(MS.ink3)
            HStack(spacing: 7) {
                ForEach(AskConversation.suggestions, id: \.prompt) { suggestion in
                    Button {
                        if suggestion.send {
                            send(suggestion.prompt)
                        } else {
                            draft = suggestion.prompt
                            askFocused = true
                        }
                    } label: {
                        Text(suggestion.label)
                            .font(MSFont.meta)
                            .foregroundStyle(MS.ink2)
                            .padding(.horizontal, 10)
                            .padding(.vertical, 5)
                            .background(MS.raised, in: .capsule)
                    }
                    .buttonStyle(PressStyle())
                }
            }
        }
    }

    private var thread: some View {
        ScrollViewReader { proxy in
            ScrollView {
                VStack(alignment: .leading, spacing: 12) {
                    ForEach(ask.messages) { message in
                        messageView(message).id(message.id)
                    }
                    // Once words are arriving they ARE the progress; the
                    // spinner has done its job.
                    if ask.busy, ask.messages.last?.streaming != true {
                        waitingRow.id("waiting")
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .frame(maxHeight: 260)
            .onChange(of: ask.messages.last?.text) {
                guard let last = ask.messages.last else { return }
                withAnimation(Motion.seek) { proxy.scrollTo(last.id, anchor: .bottom) }
            }
        }
    }

    @ViewBuilder
    private func messageView(_ message: AskMessage) -> some View {
        if message.role == .user {
            HStack(alignment: .top, spacing: 8) {
                Text("YOU")
                    .font(.system(size: 9, weight: .semibold))
                    .kerning(0.5)
                    .foregroundStyle(MS.ink4)
                    .frame(width: 26, alignment: .leading)
                    .offset(y: 2)
                Text(message.text)
                    .font(.system(size: 12.5, weight: .medium))
                    .foregroundStyle(MS.ink)
                    .textSelection(.enabled)
            }
        } else {
            VStack(alignment: .leading, spacing: 6) {
                Text(message.text.isEmpty ? "No answer came back. Try asking again."
                                          : message.text)
                    .font(.system(size: 12.5))
                    .lineSpacing(5)
                    .foregroundStyle(MS.ink)
                    .textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
                if !message.citations.isEmpty {
                    HStack(spacing: 5) {
                        ForEach(message.citations, id: \.self) { c in
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
                                .disabled(!player.hasAudio)
                                .help(c.quote ?? "")
                            }
                        }
                    }
                }
            }
            .padding(.leading, 34)
        }
    }

    /// Up to three minutes can pass before the first word on the on-device
    /// path. Saying nothing for that long is indistinguishable from a hang,
    /// so the wait counts itself out loud and the answer streams in as it is
    /// written.
    private var waitingRow: some View {
        HStack(spacing: 7) {
            ProgressView().controlSize(.small)
            Text(ask.progressLine)
                .font(MSFont.meta)
                .foregroundStyle(MS.ink2)
                .msGenerationShimmer(true)
                .contentTransition(.opacity)
            Button("Stop") { ask.cancel() }
                .buttonStyle(.plain)
                .font(.system(size: 10.5))
                .foregroundStyle(MS.ink3)
        }
        .padding(.leading, 34)
    }

    private var composer: some View {
        VStack(alignment: .leading, spacing: 7) {
            if let error = ask.error {
                Text(error)
                    .font(MSFont.meta)
                    .foregroundStyle(MS.ink2)
                    .fixedSize(horizontal: false, vertical: true)
                    .textSelection(.enabled)
            }
            HStack(spacing: 8) {
                TextField(placeholder, text: $draft)
                    .textFieldStyle(.plain)
                    .font(MSFont.chrome)
                    .focused($askFocused)
                    .onSubmit { send(draft) }
                Button {
                    send(draft)
                } label: {
                    Image(systemName: "arrow.up.circle.fill")
                        .font(.system(size: 18))
                        .foregroundStyle(canSend ? AnyShapeStyle(MS.interactive)
                                                 : AnyShapeStyle(MS.ink4))
                }
                .buttonStyle(PressStyle())
                .disabled(!canSend)
                if !ask.messages.isEmpty {
                    Button {
                        ask.clear()
                    } label: {
                        Text("New")
                            .font(.system(size: 10.5))
                            .foregroundStyle(MS.ink3)
                    }
                    .buttonStyle(PressStyle())
                    .help("Start a fresh conversation about this meeting")
                }
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
    }

    private func closeAsk() {
        withAnimation(Motion.exit) { askOpen = false }
    }

    private var placeholder: String {
        if !ask.messages.isEmpty { return "Ask a follow-up" }
        let short = detail.title.count > 30 ? String(detail.title.prefix(30)) + "…" : detail.title
        return "Ask about \(short)"
    }

    private var canSend: Bool {
        !draft.trimmingCharacters(in: .whitespaces).isEmpty && !ask.busy
    }

    private func send(_ text: String) {
        let question = text.trimmingCharacters(in: .whitespaces)
        guard !question.isEmpty, !ask.busy else { return }
        draft = ""
        ask.send(question, meetingID: detail.id)
    }
}

// MARK: - The conversation

/// Ask, as a conversation rather than a single shot.
///
/// The engine has always taken `history` and always been able to stream the
/// answer as it is written; this side asked one isolated question at a time
/// and then showed a spinner for up to three minutes. The thread lives for as
/// long as the meeting is open, which is exactly the span over which a
/// follow-up like "and what did she say about the timeline?" makes sense.
@MainActor
final class AskConversation: ObservableObject {
    struct Suggestion {
        let label: String
        let prompt: String
        /// A complete question asks itself; an open-ended one is prefilled so
        /// the user can name the topic.
        let send: Bool
    }

    static let suggestions = [
        Suggestion(label: "What did we decide?", prompt: "What did we decide?", send: true),
        Suggestion(label: "What did I commit to?", prompt: "What did I commit to?", send: true),
        Suggestion(label: "Where did I ramble?", prompt: "Where did I ramble?", send: true),
        Suggestion(label: "Every mention of…", prompt: "Every mention of ", send: false),
    ]

    @Published private(set) var messages: [AskMessage] = []
    @Published private(set) var busy = false
    @Published private(set) var error: String?
    @Published private(set) var waited = 0
    /// A question whose answer never landed, handed back so the composer can
    /// offer it again instead of making the user retype it.
    @Published var retry: String?

    private var work: Task<Void, Never>?

    var progressLine: String {
        let stage = waited < 8 ? "Reading the transcript"
                  : waited < 30 ? "Working through the meeting"
                  : "Still writing. Long meetings take a while"
        return waited < 3 ? stage + "…" : "\(stage)… \(clock(Double(waited)))"
    }

    func send(_ question: String, meetingID: String) {
        guard !busy else { return }
        error = nil
        retry = nil
        // History is the exchange BEFORE this question: an answer still being
        // written is a preview the engine has not settled, and it has no
        // business being quoted back as something it said.
        let history = messages.filter { !$0.streaming && !$0.text.isEmpty }
        messages.append(AskMessage(role: .user, text: question))
        busy = true
        waited = 0
        work = Task { [weak self] in
            guard let self else { return }
            let ticker = Task { [weak self] in
                while !Task.isCancelled {
                    try? await Task.sleep(nanoseconds: 1_000_000_000)
                    guard !Task.isCancelled, let self else { return }
                    waited += 1
                }
            }
            defer {
                ticker.cancel()
                busy = false
            }
            do {
                let answer = try await API.askStream(
                    meetingID, question: question, history: history
                ) { [weak self] delta in
                    guard let self, !delta.isEmpty else { return }
                    if let last = messages.last, last.role == .assistant, last.streaming {
                        messages[messages.count - 1].text += delta
                    } else {
                        messages.append(AskMessage(role: .assistant, text: delta,
                                                   citations: [], streaming: true))
                    }
                }
                // The deltas were a preview: only this answer carries
                // citations validated against the turns the model was shown.
                guard let text = answer.answer, !text.isEmpty else {
                    rollBack()
                    error = answer.error ?? "No answer came back. Try asking again."
                    return
                }
                let settled = AskMessage(role: .assistant, text: text,
                                         citations: answer.citations ?? [],
                                         streaming: false)
                if let last = messages.last, last.role == .assistant, last.streaming {
                    messages[messages.count - 1] = settled
                } else {
                    messages.append(settled)
                }
            } catch {
                // A cancelled ask is the user's own doing: roll the exchange
                // back and leave cancel()'s own line on screen.
                rollBack()
                guard !isCancellation(error) else { return }
                if let failure = error as? AskFailure {
                    self.error = failure.needsClaude
                        ? failure.message + " Sign in from Settings to use it."
                        : failure.message
                } else {
                    self.error = "The engine didn't answer. It may still be transcribing."
                }
            }
        }
    }

    func cancel() {
        error = "Stopped."
        work?.cancel()
        work = nil
    }

    func clear() {
        work?.cancel()
        work = nil
        messages = []
        busy = false
        error = nil
    }

    private func isCancellation(_ error: Error) -> Bool {
        error is CancellationError || (error as? URLError)?.code == .cancelled
    }

    /// Take the failed exchange back out of the thread — a half-written
    /// answer with no citations is not something the model said, and leaving
    /// it in would feed it to the next question as history. The question
    /// itself comes back to the composer.
    private func rollBack() {
        if let last = messages.last, last.role == .assistant {
            messages.removeLast()
        }
        if let last = messages.last, last.role == .user {
            retry = last.text
            messages.removeLast()
        }
    }
}
