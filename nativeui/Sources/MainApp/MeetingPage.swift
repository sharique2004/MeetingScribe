// The meeting as one document: centred dateline, serif editable title,
// people line, hairline, 17pt lead, then the two-ink body — the user's
// notes at full ink structuring the page, the machine's writing one step
// down. Every AI line can unfold its transcript evidence in place. No
// cards, no borders; the page sits directly on the content surface.
import SwiftUI

struct MeetingPage: View {
    let detail: MeetingDetail
    let notes: [MeetingNote]
    @ObservedObject var player: Playback
    @ObservedObject var model: MeetingModel
    @Binding var mode: PageMode
    var onOpenTranscript: (Double?) -> Void

    @State private var title: String = ""
    @State private var peoplePopover = false
    @State private var exportError: String?
    @FocusState private var titleFocused: Bool

    private var built: (sections: [DocumentSection], nextSteps: [DocumentBlock]) {
        DocumentBuilder.build(detail: detail, notes: notes)
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 0) {
                dateline
                    .padding(.top, 26)
                titleField
                    .padding(.top, 6)
                HStack(spacing: 14) {
                    peopleLine
                    talkShareStrip
                    // How long, how it was captured, what language it was in:
                    // facts about the recording the document never carried.
                    Text(headMeta)
                        .font(.system(size: 13))
                        .foregroundStyle(MS.ink3)
                }
                .padding(.top, 10)
                PageTabs(mode: $mode)
                    .padding(.top, 18)
                Rectangle().fill(MS.hairline).frame(height: 1)
                    .padding(.bottom, 26)

                engineNotices

                if let leadText = detail.summary?.headline
                    ?? detail.summary?.tldr.map({ String($0.split(separator: ".").first.map(String.init) ?? $0) }) {
                    Text(leadText)
                        .font(MSFont.lead)
                        .lineSpacing(9)
                        .foregroundStyle(MS.ink)
                        .textSelection(.enabled)
                        .padding(.bottom, 28)
                }

                let doc = built
                ForEach(doc.sections) { section in
                    sectionView(section)
                        .padding(.bottom, 28)
                }

                if !doc.nextSteps.isEmpty {
                    nextStepsView(doc.nextSteps)
                        .padding(.bottom, 28)
                }

                if let email = detail.summary?.follow_up_email, email.isUseful {
                    FollowUpEmailCard(email: email)
                        .padding(.bottom, 28)
                }

                insights

                if let omitted = detail.summary?.notes_omitted, omitted > 0 {
                    Text("\(omitted) of your notes didn't fit in the summary pass, they're all still on the transcript.")
                        .font(MSFont.meta)
                        .foregroundStyle(MS.ink3)
                        .padding(.bottom, 22)
                }

                // A failed meeting has its own notice above, with the button
                // that fixes it. Anything else here would be a promise about
                // a transcript that is never coming.
                if doc.sections.isEmpty && doc.nextSteps.isEmpty
                    && detail.summary == nil && !detail.failed {
                    emptyBody
                }

                footerRail
                    .padding(.top, 12)
            }
            .documentMeasure()
            .padding(.bottom, 112)   // the floating capsule never covers the last line
        }
        .background(MS.content)
        .onAppear { title = detail.title }
    }

    // MARK: - Head

    private var dateline: some View {
        Text(datelineText)
            .font(MSFont.meta)
            .foregroundStyle(MS.ink3)
    }

    private var datelineText: String {
        guard let date = createdParser.date(from: detail.created) else { return "" }
        return date.formatted(.dateTime.weekday(.wide).day().month(.wide))
            + " · " + date.formatted(.dateTime.hour().minute())
    }

    /// The meeting's name, edited in place.
    ///
    /// It commits on Return AND on losing focus, because clicking away is how
    /// people leave a title field and the edit used to be thrown away without
    /// a word. That is the opposite of the speaker ribbon on purpose: naming a
    /// speaker enrols a voiceprint, so it asks for an explicit Return; naming
    /// a meeting writes a string, and quietly discarding it is the only
    /// surprising thing it could do. Escape abandons the edit, which is the
    /// way back out.
    private var titleField: some View {
        TextField("Untitled meeting", text: $title, axis: .vertical)
            .textFieldStyle(.plain)
            .font(MSFont.pageTitle)
            .foregroundStyle(MS.ink)
            .lineLimit(1...3)
            .focused($titleFocused)
            .onSubmit {
                commitTitle()
                titleFocused = false
            }
            .onChange(of: titleFocused) { _, focused in
                if !focused { commitTitle() }
            }
            .onExitCommand {
                title = detail.title
                titleFocused = false
            }
            // A rename that landed, a reprocess, a fresh poll: whatever
            // rewrites the document, the field follows it — unless the user
            // is mid-edit, whose typing outranks any refresh.
            .onChange(of: detail.title) {
                if !titleFocused { title = detail.title }
            }
    }

    private func commitTitle() {
        let typed = title.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !typed.isEmpty, typed != detail.title else {
            title = detail.title      // an empty or unchanged edit is not an edit
            return
        }
        Task {
            if await model.rename(to: typed) == false {
                title = detail.title  // the engine refused: show what is saved
            }
        }
    }

    private var peopleLine: some View {
        Button {
            peoplePopover.toggle()
        } label: {
            HStack(spacing: 7) {
                ForEach(speakerEntries, id: \.key) { entry in
                    HStack(spacing: 5) {
                        Circle().fill(MS.speaker(entry.key)).frame(width: 6, height: 6)
                        Text(entry.name)
                            .font(.system(size: 13))
                            .foregroundStyle(MS.ink2)
                    }
                }
            }
        }
        .buttonStyle(.plain)
        .popover(isPresented: $peoplePopover, arrowEdge: .bottom) {
            SpeakerRibbon(detail: detail, model: model)
                .padding(16)
                .frame(width: 380)
        }
    }

    /// Duration · how it was captured · the language it was spoken in.
    private var headMeta: String {
        var parts: [String] = []
        if let d = detail.duration, d > 0 { parts.append("\(max(1, Int(d / 60))) min") }
        if let mode = detail.modeLabel { parts.append(mode) }
        if let language = detail.languageLabel { parts.append(language) }
        return parts.joined(separator: " · ")
    }

    private var speakerEntries: [(key: String, name: String)] {
        let dict = detail.speakers ?? [:]
        return dict.sorted { a, b in
            if a.key == "you" { return true }
            if b.key == "you" { return false }
            return a.key < b.key
        }.map { (key: $0.key, name: $0.value) }
    }

    /// One stacked bar: who owned the meeting, at a glance. Click the
    /// people line above it for the full who-spoke-when ribbon.
    @ViewBuilder
    private var talkShareStrip: some View {
        let shares = speakerEntries.compactMap { entry -> (Color, Double)? in
            guard let share = detail.stats?.per_speaker?[entry.key]?.share, share > 0.005
            else { return nil }
            return (MS.speaker(entry.key), share)
        }
        if !shares.isEmpty {
            GeometryReader { geo in
                HStack(spacing: 2) {
                    ForEach(Array(shares.enumerated()), id: \.offset) { _, s in
                        Capsule().fill(s.0.opacity(0.85))
                            .frame(width: max((geo.size.width - CGFloat(shares.count - 1) * 2) * s.1, 3))
                    }
                }
            }
            .frame(width: 180, height: 5)
        }
    }

    // MARK: - What the engine had to say

    /// The engine's own reporting, at the top of the document where it cannot
    /// be scrolled past: the failure that stopped the run and the button that
    /// retries it, then the warnings that cost audio, then the quieter ones.
    ///
    /// All of this was written by the engine into meeting.json and decoded by
    /// nobody. The consequence was not cosmetic: a meeting recorded with
    /// system audio blocked by macOS looks exactly like a normal one until
    /// you read the transcript and find half the conversation missing.
    @ViewBuilder
    private var engineNotices: some View {
        let capture = detail.captureWarnings
        let minor = detail.minorWarnings
        if detail.failed || !capture.isEmpty || !minor.isEmpty {
            VStack(alignment: .leading, spacing: 12) {
                if detail.failed { failureNotice }
                ForEach(capture, id: \.self) { warning in
                    NoticeBand(icon: "exclamationmark.triangle", text: warning)
                }
                ForEach(minor, id: \.self) { warning in
                    HStack(alignment: .firstTextBaseline, spacing: 7) {
                        Image(systemName: "info.circle")
                            .font(.system(size: 10))
                            .foregroundStyle(MS.ink4)
                        Text(warning)
                            .font(MSFont.meta)
                            .foregroundStyle(MS.ink3)
                            .fixedSize(horizontal: false, vertical: true)
                            .textSelection(.enabled)
                    }
                }
            }
            .padding(.bottom, 26)
        }
    }

    private var failureNotice: some View {
        NoticeBand(icon: "exclamationmark.triangle", text: detail.failureText) {
            if model.reprocessing {
                HStack(spacing: 7) {
                    ProgressView().controlSize(.small)
                    Text("Starting…")
                        .font(MSFont.meta)
                        .foregroundStyle(MS.ink2)
                }
            } else {
                Button {
                    model.reprocess()
                } label: {
                    Label("Reprocess audio", systemImage: "arrow.clockwise")
                        .font(.system(size: 13, weight: .semibold))
                        .foregroundStyle(.black.opacity(0.85))
                        .padding(.horizontal, 14)
                        .padding(.vertical, 6)
                        .background(MS.playheadFill, in: .capsule)
                }
                .buttonStyle(PressStyle())
                .help("Transcribe the saved audio again")
            }
        }
        .alert("Couldn't reprocess",
               isPresented: Binding(get: { model.reprocessError != nil },
                                    set: { if !$0 { model.reprocessError = nil } })) {
            Button("OK") { model.reprocessError = nil }
        } message: {
            Text(model.reprocessError ?? "")
        }
    }

    // MARK: - Body

    /// AI-authored sections (Overview, Decisions, Open questions) wear a
    /// tracked kicker so the page has visible bones; the user's own note
    /// headings stay sentence-weight — their words, not chrome.
    private func sectionView(_ section: DocumentSection) -> some View {
        let aiSection = ["overview", "decisions", "questions"].contains(section.id)
        return VStack(alignment: .leading, spacing: 12) {
            if let title = section.title {
                if aiSection {
                    Text(title.uppercased())
                        .font(MSFont.kicker)
                        .kerning(0.55)
                        .foregroundStyle(MS.ink3)
                } else {
                    Text(title)
                        .font(MSFont.sectionHeading)
                        .foregroundStyle(MS.ink)
                }
            }
            // Said once for the whole section rather than stamped on every
            // line: these notes have no moment to be shown at, and inventing
            // one — putting them at 0:00 — would be a quiet lie about where
            // in the conversation they were written.
            if section.id == "untimed-notes" {
                Text("Saved without a position in the meeting.")
                    .font(MSFont.meta)
                    .foregroundStyle(MS.ink3)
            }
            VStack(alignment: .leading, spacing: 10) {
                ForEach(section.blocks) { block in
                    blockView(block)
                }
            }
        }
    }

    @ViewBuilder
    private func blockView(_ block: DocumentBlock) -> some View {
        switch block {
        case .heading(let text):
            yourNote { Text(text).font(MSFont.sectionHeading).foregroundStyle(MS.ink) }
        case .userParagraph(let text):
            yourNote {
                Text(text)
                    .font(MSFont.body)
                    .lineSpacing(9)
                    .foregroundStyle(MS.ink)
                    .textSelection(.enabled)
            }
        case .untimedNote(_, let text):
            yourNote {
                Text(text)
                    .font(MSFont.body)
                    .lineSpacing(9)
                    .foregroundStyle(MS.ink)
                    .textSelection(.enabled)
            }
        case .bullet(_, let text, let evidence):
            EvidenceDisclosure(text: text, evidence: evidence, player: player,
                               onOpenTranscript: onOpenTranscript)
        case .actionItem(_, let item, let evidence):
            ActionRow(item: item, evidence: evidence, player: player,
                      meetingID: detail.id)
        }
    }

    /// Your own words wear a quiet pencil in the gutter — so the page reads
    /// as one document while still showing which lines are yours and which
    /// the machine wrote around them.
    private func yourNote<Content: View>(@ViewBuilder _ content: () -> Content) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 0) {
            Image(systemName: "pencil")
                .font(.system(size: 9, weight: .semibold))
                .foregroundStyle(MS.ink4)
                .frame(width: 20, alignment: .leading)
                .offset(y: -1)
                .help("Your note, taken during the meeting")
            content()
        }
        .padding(.leading, -20)
    }

    private func nextStepsView(_ steps: [DocumentBlock]) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("TO-DOS")
                .font(MSFont.kicker)
                .kerning(0.55)
                .foregroundStyle(MS.ink3)
            VStack(alignment: .leading, spacing: 12) {
                ForEach(steps) { block in
                    blockView(block)
                }
            }
        }
    }

    // MARK: - The numbers

    /// What stats.py has computed for every meeting since it shipped: talk
    /// time and share, pace, questions asked, the longest unbroken monologue.
    /// All of it was sitting in meeting.json, decoded by nobody — the "how did
    /// I do?" half of the product, invisible in the native app.
    @ViewBuilder
    private var insights: some View {
        let per = detail.stats?.per_speaker ?? [:]
        let people = speakerEntries.filter { (per[$0.key]?.seconds ?? 0) > 0 }
        if !people.isEmpty {
            VStack(alignment: .leading, spacing: 14) {
                Text("HOW IT WENT")
                    .font(MSFont.kicker)
                    .kerning(0.55)
                    .foregroundStyle(MS.ink3)

                HStack(alignment: .top, spacing: 26) {
                    ForEach(meetingStats, id: \.label) { stat in
                        VStack(alignment: .leading, spacing: 2) {
                            Text(stat.value)
                                .font(.system(size: 17, weight: .medium))
                                .monospacedDigit()
                                .foregroundStyle(MS.ink)
                            Text(stat.label)
                                .font(MSFont.meta)
                                .foregroundStyle(MS.ink3)
                            if let sub = stat.sub {
                                Text(sub)
                                    .font(MSFont.meta)
                                    .foregroundStyle(MS.ink4)
                            }
                        }
                        .fixedSize()
                    }
                    Spacer(minLength: 0)
                }

                VStack(alignment: .leading, spacing: 9) {
                    ForEach(people, id: \.key) { person in
                        speakerInsightRow(person.key, name: person.name,
                                          stats: per[person.key])
                    }
                }
            }
            .padding(.bottom, 28)
        }
    }

    private var meetingStats: [(label: String, value: String, sub: String?)] {
        let per = detail.stats?.per_speaker ?? [:]
        let words = detail.stats?.total_words ?? per.values.reduce(0) { $0 + ($1.words ?? 0) }
        let questions = per.values.reduce(0) { $0 + ($1.questions ?? 0) }
        let longest = per.values.compactMap(\.longest_turn_seconds).max() ?? 0
        let minutes = (detail.duration ?? 0) / 60
        var out: [(label: String, value: String, sub: String?)] = []
        if let d = detail.duration, d > 0 {
            out.append((label: "Length", value: clock(d), sub: nil))
        }
        if words > 0 {
            out.append((label: "Words spoken", value: words.formatted(),
                        sub: minutes > 0.05 ? "\(Int(Double(words) / minutes)) wpm overall" : nil))
        }
        out.append((label: "Questions asked", value: "\(questions)", sub: nil))
        if longest > 0 {
            out.append((label: "Longest turn", value: clock(longest),
                        sub: "one unbroken stretch"))
        }
        return out
    }

    private func speakerInsightRow(_ key: String, name: String,
                                   stats: SpeakerStats?) -> some View {
        let share = stats?.share ?? 0
        return VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 8) {
                Text(name)
                    .font(MSFont.chromeMedium)
                    .foregroundStyle(MS.speaker(key))
                Spacer(minLength: 8)
                Text(speakerLine(stats))
                    .font(MSFont.meta)
                    .foregroundStyle(MS.ink2)
            }
            GeometryReader { geo in
                ZStack(alignment: .leading) {
                    Capsule().fill(MS.ink4.opacity(0.2))
                    Capsule().fill(MS.speaker(key).opacity(0.85))
                        .frame(width: max(geo.size.width * share, 2))
                }
            }
            .frame(height: 4)
            if let fillers = topFillers(stats) {
                Text(fillers)
                    .font(MSFont.meta)
                    .foregroundStyle(MS.ink3)
            }
        }
    }

    private func speakerLine(_ stats: SpeakerStats?) -> String {
        guard let stats else { return "" }
        var parts = ["\(Int((stats.share ?? 0) * 100))%"]
        if let seconds = stats.seconds, seconds > 0 { parts.append(clock(seconds)) }
        if let wpm = stats.wpm, wpm > 0 { parts.append("\(Int(wpm)) wpm") }
        if let questions = stats.questions, questions > 0 {
            parts.append("\(questions) question\(questions == 1 ? "" : "s")")
        }
        return parts.joined(separator: " · ")
    }

    /// The three commonest fillers, in the engine's own order.
    private func topFillers(_ stats: SpeakerStats?) -> String? {
        guard let fillers = stats?.fillers, !fillers.isEmpty else { return nil }
        let top = fillers.sorted { $0.value > $1.value }.prefix(3)
            .map { "\($0.key) ×\($0.value)" }
        return top.isEmpty ? nil : "Fillers: " + top.joined(separator: " · ")
    }

    private var emptyBody: some View {
        VStack(alignment: .leading, spacing: 12) {
            if model.summarizing {
                summaryProgressRow
            } else if meetingIsWorking(detail.status) {
                transcriptionProgressRow
            } else if detail.turns?.isEmpty == false {
                Text("No summary yet.")
                    .font(MSFont.body)
                    .foregroundStyle(MS.ink2)
                Button {
                    model.summarize()
                } label: {
                    Label("Write the summary", systemImage: "sparkles")
                        .font(.system(size: 13, weight: .semibold))
                        .foregroundStyle(.black.opacity(0.85))
                        .padding(.horizontal, 16)
                        .padding(.vertical, 7)
                        .background(MS.playheadFill, in: .capsule)
                }
                .buttonStyle(PressStyle())
                Text("Takes a minute or two, the whole transcript is read and distilled on this Mac.")
                    .font(MSFont.meta)
                    .foregroundStyle(MS.ink3)
            } else {
                Text("Still audio-only.")
                    .font(MSFont.body)
                    .foregroundStyle(MS.ink2)
                Text("Transcription usually takes about a third of the meeting's length.")
                    .font(MSFont.meta)
                    .foregroundStyle(MS.ink3)
            }
        }
        .padding(.bottom, 28)
    }

    private var summaryProgressRow: some View {
        HStack(spacing: 9) {
            ProgressView().controlSize(.small)
            Text(model.summaryProgress ?? "Writing the summary…")
                .font(MSFont.body)
                .foregroundStyle(MS.ink2)
                .msGenerationShimmer(true)
                .contentTransition(.opacity)
        }
    }

    /// The transcription, in the engine's words. It knows whether it is
    /// loading a model, fetching 2.5 GB of one, or three quarters of the way
    /// through the audio; the page used to answer all three with silence and
    /// an estimate.
    private var transcriptionProgressRow: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 9) {
                ProgressView().controlSize(.small)
                Text(model.processingProgress ?? "Transcribing this meeting…")
                    .font(MSFont.body)
                    .foregroundStyle(MS.ink2)
                    .msGenerationShimmer(true)
                    .contentTransition(.opacity)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Text("Transcription usually takes about a third of the meeting's length, and the first run also downloads the models.")
                .font(MSFont.meta)
                .foregroundStyle(MS.ink3)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    // MARK: - Footer

    private var footerRail: some View {
        VStack(alignment: .leading, spacing: 8) {
            footerActions
            // Its own line: with export in the rail the provenance had
            // nowhere left to sit without truncating one or the other.
            Text(footerStats)
                .font(MSFont.meta)
                .foregroundStyle(MS.ink3)
                .fixedSize(horizontal: false, vertical: true)
        }
        .alert("Couldn't summarise",
               isPresented: Binding(get: { model.summaryError != nil },
                                    set: { if !$0 { model.summaryError = nil } })) {
            Button("OK") { model.summaryError = nil }
        } message: {
            Text(model.summaryError ?? "")
        }
        .alert("Couldn't rename",
               isPresented: Binding(get: { model.titleError != nil },
                                    set: { if !$0 { model.titleError = nil } })) {
            Button("OK") { model.titleError = nil }
        } message: {
            Text(model.titleError ?? "")
        }
        .alert("Couldn't export",
               isPresented: Binding(get: { exportError != nil },
                                    set: { if !$0 { exportError = nil } })) {
            Button("OK") { exportError = nil }
        } message: {
            Text(exportError ?? "")
        }
    }

    private var footerActions: some View {
        HStack(spacing: 14) {
            Button {
                onOpenTranscript(nil)
            } label: {
                Label("Transcript", systemImage: "waveform")
                    .font(MSFont.meta)
                    .foregroundStyle(MS.ink2)
            }
            .buttonStyle(PressStyle())
            .keyboardShortcut("t", modifiers: .command)

            CopyTranscriptButton(detail: detail)

            // Every meeting already has a transcript.md on disk and the
            // engine will build the Markdown or the plain text on request —
            // there was simply no way to ask for it from here.
            ExportMenu(detail: detail, error: $exportError, shortcut: false)
                .menuStyle(.borderlessButton)
                .fixedSize()
                .font(MSFont.meta)
                .foregroundStyle(MS.ink2)

            if detail.summary != nil {
                if model.summarizing {
                    HStack(spacing: 6) {
                        ProgressView().controlSize(.mini)
                        Text(model.summaryProgress ?? "Re-analysing…")
                            .font(MSFont.meta)
                            .foregroundStyle(MS.ink3)
                    }
                } else {
                    Button {
                        model.summarize()
                    } label: {
                        Label("Re-analyse", systemImage: "arrow.clockwise")
                            .font(MSFont.meta)
                            .foregroundStyle(MS.ink2)
                    }
                    .buttonStyle(PressStyle())
                    .help("Rewrite the summary from the transcript")
                }
            }

            Spacer()
        }
    }

    /// The provenance line: how much was said, what transcribed it, what
    /// wrote the summary, and where all of it ran.
    private var footerStats: String {
        var parts: [String] = []
        let words = detail.stats?.total_words
            ?? (detail.turns ?? []).reduce(0) { $0 + $1.text.split(separator: " ").count }
        if words > 0 { parts.append("\(words.formatted()) words") }
        if let backend = detail.processing?.label { parts.append(backend) }
        if let engine = detail.summary?.engine, !engine.isEmpty {
            parts.append("summarised by \(engine)")
        }
        parts.append("local only")
        return parts.joined(separator: " · ")
    }
}

// MARK: - Notice band

/// One thing the engine needs the reader to know, in the engine's words,
/// with the action that answers it when there is one.
///
/// Deliberately colourless: the palette spends red on recording and mint on
/// the present moment, so attention here is bought with a surface, a rule and
/// a glyph instead. It is at the top of a document nobody can start reading
/// without passing, which is enough.
struct NoticeBand<Action: View>: View {
    let icon: String
    let text: String
    @ViewBuilder var action: () -> Action

    var body: some View {
        HStack(alignment: .top, spacing: 11) {
            Image(systemName: icon)
                .font(.system(size: 13, weight: .semibold))
                .foregroundStyle(MS.ink2)
                .offset(y: 1)
            Text(text)
                .font(MSFont.chrome)
                .lineSpacing(4)
                .foregroundStyle(MS.ink)
                .fixedSize(horizontal: false, vertical: true)
                .textSelection(.enabled)
            Spacer(minLength: 0)
            action()
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(MS.raised, in: .rect(cornerRadius: 10))
        .overlay {
            RoundedRectangle(cornerRadius: 10)
                .stroke(MS.hairlineStrong, lineWidth: 1)
        }
    }
}

extension NoticeBand where Action == EmptyView {
    init(icon: String, text: String) {
        self.init(icon: icon, text: text) { EmptyView() }
    }
}

// MARK: - Evidence disclosure (the hover mechanic)

struct EvidenceDisclosure: View {
    let text: String
    let evidence: Turn?
    @ObservedObject var player: Playback
    var onOpenTranscript: (Double?) -> Void

    @State private var hovering = false
    @State private var unfolded = false

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .firstTextBaseline, spacing: 0) {
                ZStack {
                    if hovering && evidence != nil {
                        Image(systemName: "play.fill")
                            .font(.system(size: 9))
                            .foregroundStyle(MS.playhead)
                            .transition(.scale(scale: 0.6).combined(with: .opacity))
                    } else {
                        Circle().fill(MS.ink4).frame(width: 3.5, height: 3.5)
                            .transition(.scale(scale: 0.6).combined(with: .opacity))
                    }
                }
                .frame(width: 20, alignment: .leading)
                .offset(y: -3)
                .animation(Motion.micro, value: hovering)

                Text(text)
                    .font(MSFont.body)
                    .lineSpacing(9)
                    .foregroundStyle(hovering ? MS.ink : MS.ink2)
                    .textSelection(.enabled)
                    .animation(Motion.micro, value: hovering)
            }
            .contentShape(Rectangle())
            .onHover { hovering = $0 }
            .onTapGesture {
                guard evidence != nil else { return }
                if NSEvent.modifierFlags.contains(.option), let t = evidence?.start {
                    onOpenTranscript(t)
                } else {
                    withAnimation(.timingCurve(0.32, 0.72, 0, 1, duration: 0.22)) {
                        unfolded.toggle()
                    }
                }
            }

            if unfolded, let ev = evidence {
                HStack(alignment: .firstTextBaseline, spacing: 10) {
                    Button {
                        player.seek(ev.start)
                        player.play()
                    } label: {
                        Text(clock(ev.start))
                            .clockFont(11)
                            .foregroundStyle(MS.playhead)
                    }
                    .buttonStyle(PressStyle())

                    Text(ev.text)
                        .font(MSFont.evidence)
                        .lineSpacing(7)
                        .foregroundStyle(MS.ink2)
                        .textSelection(.enabled)
                }
                .padding(.leading, 16)
                .overlay(alignment: .leading) {
                    RoundedRectangle(cornerRadius: 1)
                        .fill(MS.playhead.opacity(0.3))
                        .frame(width: 2)
                        .padding(.leading, 2)
                }
                .padding(.leading, 20)
                .transition(.asymmetric(
                    insertion: .offset(y: -4).combined(with: .opacity),
                    removal: .opacity.animation(Motion.exit)))
            }
        }
    }
}

// MARK: - Action row (self-drawing checkbox)

struct ActionRow: View {
    let item: ActionItem
    let evidence: Turn?
    @ObservedObject var player: Playback
    let meetingID: String
    @ObservedObject private var store = ActionItemStore.shared
    @State private var drawn: CGFloat = 0

    /// The tick is read from the store, not from a local flag: it used to be
    /// @State, which meant it survived until the next tab switch and no
    /// longer. See ActionItemStore for where it lives now and why.
    private var checked: Bool {
        store.isDone(meeting: meetingID, task: item.task)
    }

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 10) {
            Button {
                let next = !checked
                store.set(next, meeting: meetingID, task: item.task)
                withAnimation(.easeOut(duration: 0.4)) { drawn = next ? 1 : 0 }
            } label: {
                ZStack {
                    Circle()
                        .strokeBorder(checked ? MS.playheadFill : MS.ink4, lineWidth: 1.3)
                        .background(Circle().fill(checked ? MS.playheadFill : .clear))
                    CheckmarkShape()
                        .trim(from: 0, to: drawn)
                        .stroke(Color.black.opacity(0.8),
                                style: StrokeStyle(lineWidth: 1.8, lineCap: .round, lineJoin: .round))
                        .padding(5)
                }
                .frame(width: 20, height: 20)
                .animation(Motion.micro, value: checked)
            }
            .buttonStyle(PressStyle())

            VStack(alignment: .leading, spacing: 3) {
                Text(item.task)
                    .font(MSFont.body)
                    .lineSpacing(8)
                    .foregroundStyle(checked ? MS.ink3 : MS.ink)
                    .animation(.easeOut(duration: 0.4), value: checked)
                HStack(spacing: 6) {
                    if let owner = item.owner, !owner.isEmpty {
                        Text(owner).font(MSFont.meta).foregroundStyle(MS.ink2)
                    }
                    if let due = item.due, !due.isEmpty {
                        Text(due).font(MSFont.meta).foregroundStyle(MS.ink3)
                    }
                    if let ev = evidence {
                        Button {
                            player.seek(ev.start)
                            player.play()
                        } label: {
                            Text("@" + clock(ev.start))
                                .clockFont(11)
                                .foregroundStyle(MS.playhead)
                        }
                        .buttonStyle(PressStyle())
                    }
                }
            }
        }
        // A tick that outlives the view has to be DRAWN when the view comes
        // back, or a to-do ticked yesterday reappears as an empty circle with
        // a filled background.
        .onAppear { drawn = checked ? 1 : 0 }
    }
}

/// The draft follow-up email, written as the user sending it. Collapsed to
/// its subject until opened; one click puts the whole thing on the clipboard
/// or hands it to Mail.
struct FollowUpEmailCard: View {
    let email: FollowUpEmail
    @State private var open = false
    @State private var copied = false

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Button {
                withAnimation(.timingCurve(0.32, 0.72, 0, 1, duration: 0.22)) { open.toggle() }
            } label: {
                HStack(spacing: 8) {
                    Text("FOLLOW-UP EMAIL")
                        .font(MSFont.kicker)
                        .kerning(0.55)
                        .foregroundStyle(MS.ink3)
                    Image(systemName: "chevron.right")
                        .font(.system(size: 8, weight: .bold))
                        .foregroundStyle(MS.ink4)
                        .rotationEffect(.degrees(open ? 90 : 0))
                    Spacer()
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(PressStyle())

            if let subject = email.subject, !subject.isEmpty {
                Text(subject)
                    .font(MSFont.sectionHeading)
                    .foregroundStyle(MS.ink)
                    .textSelection(.enabled)
            }

            if open {
                Text(email.body ?? "")
                    .font(MSFont.body)
                    .lineSpacing(8)
                    .foregroundStyle(MS.ink2)
                    .textSelection(.enabled)
                    .transition(.offset(y: -4).combined(with: .opacity))

                HStack(spacing: 10) {
                    Button {
                        let pb = NSPasteboard.general
                        pb.clearContents()
                        pb.setString([email.subject, email.body]
                            .compactMap { $0 }.joined(separator: "\n\n"), forType: .string)
                        withAnimation(Motion.micro) { copied = true }
                        Task {
                            try? await Task.sleep(nanoseconds: 1_600_000_000)
                            withAnimation(Motion.exit) { copied = false }
                        }
                    } label: {
                        Label(copied ? "Copied" : "Copy email",
                              systemImage: copied ? "checkmark" : "doc.on.doc")
                            .font(MSFont.meta)
                            .foregroundStyle(copied ? AnyShapeStyle(MS.playhead)
                                                    : AnyShapeStyle(MS.ink2))
                            .padding(.horizontal, 10)
                            .padding(.vertical, 5)
                            .background(MS.raised, in: .capsule)
                            .contentTransition(.symbolEffect(.replace))
                    }
                    .buttonStyle(PressStyle())

                    Button {
                        var c = URLComponents()
                        c.scheme = "mailto"
                        c.path = ""
                        c.queryItems = [
                            URLQueryItem(name: "subject", value: email.subject ?? ""),
                            URLQueryItem(name: "body", value: email.body ?? ""),
                        ]
                        if let url = c.url { NSWorkspace.shared.open(url) }
                    } label: {
                        Label("Open in Mail", systemImage: "envelope")
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
}

/// Quiet, visible tabs: Notes | Transcript. Active tab wears a 2pt mint
/// underline that sits on the page's hairline.
struct PageTabs: View {
    @Binding var mode: PageMode

    var body: some View {
        HStack(spacing: 20) {
            tab("Notes", .document)
            tab("Transcript", .transcript)
            Spacer()
        }
    }

    private func tab(_ label: String, _ target: PageMode) -> some View {
        let active = mode == target
        return Button {
            withAnimation(Motion.enter) { mode = target }
        } label: {
            VStack(spacing: 7) {
                Text(label)
                    .font(MSFont.chromeMedium)
                    .foregroundStyle(active ? MS.ink : MS.ink3)
                Rectangle()
                    .fill(MS.playhead)
                    .frame(height: 2)
                    .opacity(active ? 1 : 0)
            }
            .fixedSize()
            .contentShape(Rectangle())
        }
        .buttonStyle(PressStyle())
    }
}

struct CheckmarkShape: Shape {
    func path(in rect: CGRect) -> Path {
        var p = Path()
        p.move(to: CGPoint(x: rect.minX + rect.width * 0.05, y: rect.midY + rect.height * 0.08))
        p.addLine(to: CGPoint(x: rect.minX + rect.width * 0.38, y: rect.maxY - rect.height * 0.12))
        p.addLine(to: CGPoint(x: rect.maxX - rect.width * 0.02, y: rect.minY + rect.height * 0.14))
        return p
    }
}
