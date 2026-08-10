// The one floating glass element: the transport SHELF — play, waveform,
// clocks, track — present in both modes, with Ask folded in behind the
// sparkle button (⌘J). Asking expands the same piece of glass upward;
// nothing else ever floats.
//
// A shelf, not a pill: it spans the pane with a 12pt margin either side and
// carries the document's own radius, so the controls read as the bottom edge
// of the page rather than an object hovering over it.
import SwiftUI

enum PageMode { case document, transcript }

struct FloatingBar: View {
    let detail: MeetingDetail
    let waveform: WaveformData?
    @ObservedObject var player: Playback
    /// Borrowed from AskCenter, never owned: the conversation lives at app
    /// scope, so an answer keeps streaming into it while the user is on
    /// Today, in Settings, or three meetings away — and is still on screen
    /// when they come back.
    @ObservedObject var ask: AskConversation
    /// For the unread tint on the Ask label: an answer landed while this
    /// panel was closed, and the label is where that shows.
    @ObservedObject private var center = AskCenter.shared

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
        // Full pane width, inset 12 either side: the shelf is as wide as the
        // thing it controls. The old 620 cap left it floating mid-air over a
        // wider column, which is the one place a transport should never be.
        .frame(maxWidth: .infinity)
        .glassEffect(.regular.interactive(), in: .rect(cornerRadius: MS.radius.lg))
        .msEdgeLit(radius: MS.radius.lg)
        .overlay(alignment: .top) { liveRail }
        // The app's one shadow became the app's one RAMP: four tinted layers,
        // blur doubling and opacity decaying, violet rather than black.
        .msElevation(.floating)
        .padding(.horizontal, 12)
        .padding(.bottom, 18)
        .animation(Motion.enter, value: askOpen)
        .animation(Motion.enter, value: player.failure)
        // An answer that finished while this page was off screen opens the
        // thread on arrival: the sidebar's mark said "ready", and the reply
        // is on screen before the user has to remember where Ask lives.
        .onAppear {
            if AskCenter.shared.consumeUnread(detail.id) {
                askOpen = true
            }
        }
        // The keyboard's transport. This bar is the right listener: it is the
        // one view that exists exactly when there is something to play, and it
        // already holds the players. See the contract in Commands.swift.
        .onMSPlaybackCommands(perform: run)
    }

    /// A key equivalent, or a click in the Meeting menu.
    private func run(_ command: MSPlaybackCommand) {
        switch command {
        // Space and the arrows are plain key equivalents, and a first
        // responder that wants the key consumes it before the menu is ever
        // asked — so while the composer has the keyboard this guard should
        // never fire. It is here because "should never" is carrying a lot in
        // that sentence, and the failure it covers is the one that would
        // matter: a space typed mid-question playing the meeting instead. The
        // speed items are unguarded; they are on ⌘, which no field wants.
        case .togglePlayback:
            guard !typing else { return }
            player.toggle()
        case .seekBy(let seconds):
            guard !typing else { return }
            player.skip(seconds)
        case .setSpeed(let rate):
            msWithAnimation(Motion.micro) { player.setRate(rate) }
        case .cycleSpeed:
            msWithAnimation(Motion.micro) { player.cycleRate() }
        }
    }

    // MARK: - Transport

    /// The live rail: while sound is moving, one hairline along the shelf's
    /// top edge, mint at the leading end and sky at the other — the accent's
    /// two voices at the dose chrome is allowed. It says only "this meeting
    /// is running": the waveform already says where in it you are, so the
    /// rail never moves, and there is nothing here to watch.
    ///
    /// Drawn on a full-size clear layer clipped to the shelf's own radius, so
    /// the line stops exactly where the corners begin instead of running out
    /// past them into nothing.
    private var liveRail: some View {
        Color.clear
            .overlay(alignment: .top) {
                LinearGradient(colors: [MS.playhead.opacity(0.14),
                                        MS.skyFill.opacity(0.10)],
                               startPoint: .leading, endPoint: .trailing)
                    .frame(height: 1)
            }
            .clipShape(MS.rr(MS.radius.lg))
            .opacity(player.playing ? 1 : 0)
            .msAnimation(Motion.exit, value: player.playing)
            .allowsHitTesting(false)
    }

    /// What went wrong with the audio, in one line, above the controls that
    /// can't do anything about it.
    private func transportNotice(_ text: String) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 7) {
            Image(systemName: "exclamationmark.triangle")
                .font(MSFont.kicker)
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
                    .font(MSFont.chrome.weight(.semibold))
                    .contentTransition(.symbolEffect(.replace))
                    .foregroundStyle(player.hasAudio ? MS.ink : MS.ink4)
                    .frame(width: 24, height: 24)
                    .contentShape(.circle)
            }
            .buttonStyle(PressStyle())
            .disabled(!player.hasAudio)
            .help(player.playing ? "Pause (space)" : "Play this meeting (space)")
            .accessibilityLabel(player.playing ? "Pause" : "Play this meeting")

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
                    .accessibilityLabel("Playback position")
                    .accessibilityValue(clock(player.time))
            }

            Text("−" + clock(max(0, duration - player.time)))
                .clockFont(11)
                .lineLimit(1)
                .fixedSize()
                .foregroundStyle(MS.ink3)

            // Only what this meeting has. A recording made in person on one
            // device has no meeting track, and offering it was offering a 404.
            if !player.choices.isEmpty {
                Picker("Audio track", selection: Binding(get: { player.track },
                                                         set: { player.switchTrack($0) })) {
                    ForEach(player.choices) { choice in
                        Text(choice.label).tag(choice.id)
                    }
                }
                .pickerStyle(.menu)
                .controlSize(.small)
                .fixedSize()
                .labelsHidden()
                .help("Which track you hear. Everything stays in step either way")
            }

            // Speed as a numeral, not a control: it reads as a third clock,
            // and one click steps to the next rung. The full ladder — and the
            // keys — are in the Meeting menu; this is the one-handed version
            // for the person already scrubbing with the mouse.
            if player.hasAudio {
                Button {
                    msWithAnimation(Motion.micro) { player.cycleRate() }
                } label: {
                    Text(PlaybackSpeed.label(player.rate))
                        .clockFont(11)
                        .foregroundStyle(MS.ink2)
                        // Wide enough for "1.25×" so the bar never reflows as
                        // the speed steps: the numerals beside it don't move.
                        .frame(width: 38, height: 24)
                        .contentShape(.capsule)
                }
                .buttonStyle(PressStyle())
                .help("Playback speed")
                .accessibilityLabel("Playback speed")
                .accessibilityValue(PlaybackSpeed.spoken(player.rate))
            }

            Button {
                withAnimation(Motion.enter) { askOpen.toggle() }
            } label: {
                // A word, not a glyph: "you can talk to this meeting" is the
                // feature, and an unlabelled sparkle was keeping it a secret.
                // While an answer is being written and the panel is closed,
                // the label shimmers — the generation treatment every other
                // in-flight model call in the app already wears.
                HStack(spacing: 5) {
                    Image(systemName: "sparkle")
                        .font(MSFont.kicker.weight(.semibold))
                    Text("Ask")
                        .font(MSFont.chromeMedium)
                }
                .foregroundStyle(askOpen || ask.busy || hasUnread
                                 ? AnyShapeStyle(MS.interactive)
                                 : AnyShapeStyle(MS.ink2))
                .msGenerationShimmer(ask.busy && !askOpen)
                .padding(.horizontal, 9)
                .frame(height: 24)
                .contentShape(.capsule)
            }
            .buttonStyle(PressStyle())
            .keyboardShortcut("j", modifiers: .command)
            .help("Ask this meeting anything (⌘J)")
            .accessibilityLabel(ask.busy ? "Ask. An answer is being written."
                                : hasUnread ? "Ask. An answer is ready."
                                : "Ask about this meeting")
        }
        .padding(.horizontal, 13)
        .padding(.vertical, 9)
        // Whether the thread is on screen decides whether a settling answer
        // needs a mark at all — see AskCenter.noteSettled.
        .onChange(of: askOpen, initial: true) {
            askOpen ? AskCenter.shared.threadOpened(detail.id)
                    : AskCenter.shared.threadClosed(detail.id)
        }
        .onDisappear { AskCenter.shared.threadClosed(detail.id) }
    }

    private var hasUnread: Bool { center.unread.contains(detail.id) }

    /// The composer has the keyboard, so a plain key is a character rather
    /// than a transport verb. Both halves, not just the focus flag: a panel
    /// that isn't on screen has no field to type into, and a stale focus
    /// state must never be able to take Space away for the rest of the visit.
    private var typing: Bool { askOpen && askFocused }

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
        // than being retyped. `initial: true` because the failure may have
        // happened while this panel did not exist — the whole point of the
        // app-scoped conversation — and an onChange that only watches from
        // now on would leave that question stranded in `retry` forever.
        .onChange(of: ask.retry, initial: true) {
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
                    .font(MSFont.kicker.weight(.semibold))
                    .kerning(0.5)
                    .foregroundStyle(MS.ink4)
                    .frame(width: 26, alignment: .leading)
                    .offset(y: 2)
                Text(message.text)
                    .font(MSFont.meta.weight(.medium))
                    .foregroundStyle(MS.ink)
                    .textSelection(.enabled)
            }
        } else {
            VStack(alignment: .leading, spacing: 6) {
                Text(message.text.isEmpty ? "No answer came back. Try asking again."
                                          : message.text)
                    .font(MSFont.meta)
                    .lineSpacing(5)
                    .foregroundStyle(MS.ink)
                    .textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
                if !message.citations.isEmpty {
                    HStack(spacing: 5) {
                        // Indices, not \.self: the engine can cite the same
                        // moment twice in one answer, and two identical
                        // citations must not share an identity.
                        ForEach(message.citations.indices, id: \.self) { i in
                            let c = message.citations[i]
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
                .font(MSFont.kicker.weight(.regular))
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
                    // The field exists only while the panel is open, so its
                    // arrival IS the moment to focus — no timers guessing at
                    // when the panel's entrance has settled.
                    .onAppear { askFocused = true }
                Button {
                    send(draft)
                } label: {
                    Image(systemName: "arrow.up.circle.fill")
                        .font(MSFont.displayLead)
                        .foregroundStyle(canSend ? AnyShapeStyle(MS.interactive)
                                                 : AnyShapeStyle(MS.ink4))
                }
                .buttonStyle(PressStyle())
                .disabled(!canSend)
                .accessibilityLabel("Send question")
                if !ask.messages.isEmpty {
                    Button {
                        ask.clearAll()
                    } label: {
                        Text("Clear")
                            .font(MSFont.kicker.weight(.regular))
                            .foregroundStyle(MS.ink3)
                    }
                    .buttonStyle(PressStyle())
                    .disabled(ask.busy)
                    .help("Forget this conversation, here and on disk")
                }
                Button {
                    closeAsk()
                } label: {
                    Image(systemName: "xmark.circle.fill")
                        .font(MSFont.body)
                        .foregroundStyle(MS.ink3)
                }
                .buttonStyle(PressStyle())
                .help("Close (esc)")
                .accessibilityLabel("Close Ask")
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
        ask.send(question)
    }
}

// MARK: - The conversation

/// Ask, as a conversation rather than a single shot.
///
/// The engine has always taken `history` and always been able to stream the
/// answer as it is written; this side asked one isolated question at a time
/// and then showed a spinner for up to three minutes. The thread now lives at
/// app scope (AskCenter) and on disk (the engine's qa.json): it hydrates from
/// the store on first touch, keeps streaming while the user is on any other
/// page, and picks an in-flight answer back up after a relaunch by polling
/// the store — which is exactly the span over which a follow-up like "and
/// what did she say about the timeline?" makes sense.
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
    /// The engine's own progress line, when this side is following a resumed
    /// job rather than a stream it started itself.
    @Published private(set) var engineLine: String?

    let meetingID: String
    private weak var center: AskCenter?
    private var work: Task<Void, Never>?
    private var hydration: Task<Void, Never>?

    init(meetingID: String, center: AskCenter? = nil) {
        self.meetingID = meetingID
        self.center = center
        hydration = Task { [weak self] in await self?.hydrate() }
    }

    var progressLine: String {
        if let engineLine, !engineLine.isEmpty { return engineLine }
        let stage = waited < 8 ? "Reading the transcript"
                  : waited < 30 ? "Working through the meeting"
                  : "Still writing. Long meetings take a while"
        return waited < 3 ? stage + "…" : "\(stage)… \(clock(Double(waited)))"
    }

    /// One-time, on creation: the persisted thread, and — when the engine is
    /// mid-answer with no live stream in this process, which is what a
    /// relaunch during a long question leaves behind — the resumption of
    /// that answer.
    private func hydrate() async {
        guard let env = try? await API.qa(meetingID) else { return }
        // Cancelled means the user cleared the thread while this fetch was in
        // flight — appending now would resurrect what they just deleted.
        guard !Task.isCancelled, messages.isEmpty, !busy else { return }
        for exchange in env.exchanges {
            guard let q = exchange.question, !q.isEmpty,
                  let a = exchange.answer, !a.isEmpty else { continue }
            messages.append(AskMessage(role: .user, text: q))
            messages.append(AskMessage(role: .assistant, text: a,
                                       citations: exchange.citations ?? []))
        }
        if let job = env.job, job.state == "processing" {
            resume(job: job, partial: env.partial ?? "")
        }
    }

    /// Follow a running job this process didn't start. The store's `partial`
    /// grows exactly like a stream, just on a one-second clock, and the
    /// settled exchange arrives with its citations when the job lands.
    private func resume(job: QAJob, partial: String) {
        guard !busy else { return }
        if let q = job.question, !q.isEmpty {
            messages.append(AskMessage(role: .user, text: q))
        }
        messages.append(AskMessage(role: .assistant, text: partial,
                                   citations: [], streaming: true))
        busy = true
        waited = 0
        engineLine = job.message
        work = Task { [weak self] in
            guard let self else { return }
            defer {
                busy = false
                engineLine = nil
            }
            // A bounded number of misses, not forever: an engine that stops
            // answering would otherwise pin `busy` for the life of the app,
            // which disables send, Clear, and every way out but Stop —
            // inside the panel the user may never open.
            var misses = 0
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(1))
                guard !Task.isCancelled else { return }
                guard let env = try? await API.qa(meetingID) else {
                    misses += 1
                    if misses >= 20 {
                        rollBack()
                        error = "Lost touch with the engine while the answer was being written."
                        return
                    }
                    continue
                }
                misses = 0
                waited += 1
                engineLine = env.job?.message
                if env.job?.state == "processing" {
                    if let last = messages.last, last.role == .assistant, last.streaming,
                       let partial = env.partial, partial.count > last.text.count {
                        messages[messages.count - 1].text = partial
                    }
                    continue
                }
                let qaID = env.job?.qa_id
                if env.job?.state == "done",
                   let settledX = env.exchanges.last(where: { $0.id == qaID })
                                    ?? env.exchanges.last,
                   let text = settledX.answer, !text.isEmpty {
                    // Mutate the streaming row rather than replacing it: the
                    // message keeps its identity, so the row the user may be
                    // selecting text in is updated, not torn down and rebuilt.
                    if let last = messages.last, last.role == .assistant, last.streaming {
                        messages[messages.count - 1].text = text
                        messages[messages.count - 1].citations = settledX.citations ?? []
                        messages[messages.count - 1].streaming = false
                    } else {
                        messages.append(AskMessage(role: .assistant, text: text,
                                                   citations: settledX.citations ?? []))
                    }
                } else {
                    rollBack()
                    let line = env.job?.message ?? "No answer came back. Try asking again."
                    error = env.job?.needs_claude == true
                        ? line + " Sign in from Settings to use it."
                        : line
                }
                center?.noteSettled(meetingID)
                return
            }
        }
    }

    func send(_ question: String) {
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
        engineLine = nil
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
                // Mutate the streaming row rather than replacing it: the
                // message keeps its ForEach identity, so the row is updated
                // in place instead of torn down at the moment it settles.
                if let last = messages.last, last.role == .assistant, last.streaming {
                    messages[messages.count - 1].text = text
                    messages[messages.count - 1].citations = answer.citations ?? []
                    messages[messages.count - 1].streaming = false
                } else {
                    messages.append(AskMessage(role: .assistant, text: text,
                                               citations: answer.citations ?? []))
                }
                center?.noteSettled(meetingID)
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
                center?.noteSettled(meetingID)
            }
        }
    }

    /// Stop READING. The engine finishes on its own schedule and still writes
    /// the answer to the store, where the next hydration finds it — this
    /// cancels the watching, not the work.
    func cancel() {
        error = "Stopped. If the engine finishes anyway, the answer is kept."
        work?.cancel()
        work = nil
    }

    /// Forget the conversation, on screen and on disk. Disabled while an
    /// answer is being written: the engine refuses the delete then, because
    /// its own append would resurrect half of it a moment later.
    func clearAll() {
        guard !busy else { return }
        hydration?.cancel()   // a late hydrate must not resurrect the thread
        messages = []
        error = nil
        retry = nil
        Task { _ = await API.clearQA(meetingID) }
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
