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
                    if let d = detail.duration {
                        Text("\(Int(d / 60)) min")
                            .font(.system(size: 13))
                            .foregroundStyle(MS.ink3)
                    }
                }
                .padding(.top, 10)
                PageTabs(mode: $mode)
                    .padding(.top, 18)
                Rectangle().fill(MS.hairline).frame(height: 1)
                    .padding(.bottom, 26)

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

                if let omitted = detail.summary?.notes_omitted, omitted > 0 {
                    Text("\(omitted) of your notes didn't fit in the summary pass — they're all still on the transcript.")
                        .font(MSFont.meta)
                        .foregroundStyle(MS.ink3)
                        .padding(.bottom, 22)
                }

                if doc.sections.isEmpty && doc.nextSteps.isEmpty && detail.summary == nil {
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

    private var titleField: some View {
        TextField("Untitled meeting", text: $title, axis: .vertical)
            .textFieldStyle(.plain)
            .font(MSFont.pageTitle)
            .foregroundStyle(MS.ink)
            .lineLimit(1...3)
            .onSubmit {
                let t = title.trimmingCharacters(in: .whitespaces)
                guard !t.isEmpty, t != detail.title else { return }
                Task { await API.rename(detail.id, title: t) }
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
        case .bullet(_, let text, let evidence):
            EvidenceDisclosure(text: text, evidence: evidence, player: player,
                               onOpenTranscript: onOpenTranscript)
        case .actionItem(_, let item, let evidence):
            ActionRow(item: item, evidence: evidence, player: player)
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

    private var emptyBody: some View {
        VStack(alignment: .leading, spacing: 12) {
            if model.summarizing {
                summaryProgressRow
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
                Text("Takes a minute or two — the whole transcript is read and distilled on this Mac.")
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
                .generationShimmer(true)
                .contentTransition(.opacity)
        }
    }

    // MARK: - Footer

    private var footerRail: some View {
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

            Text(footerStats)
                .font(MSFont.meta)
                .foregroundStyle(MS.ink3)
            Spacer()
        }
        .alert("Couldn't summarise",
               isPresented: Binding(get: { model.summaryError != nil },
                                    set: { if !$0 { model.summaryError = nil } })) {
            Button("OK") { model.summaryError = nil }
        } message: {
            Text(model.summaryError ?? "")
        }
    }

    private var footerStats: String {
        var parts: [String] = []
        let words = (detail.turns ?? []).reduce(0) { $0 + $1.text.split(separator: " ").count }
        if words > 0 { parts.append("\(words.formatted()) words") }
        if let d = detail.duration { parts.append("\(Int(d / 60)) min") }
        parts.append("local only")
        return parts.joined(separator: " · ")
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
    @State private var checked = false
    @State private var drawn: CGFloat = 0

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 10) {
            Button {
                checked.toggle()
                withAnimation(.easeOut(duration: 0.4)) { drawn = checked ? 1 : 0 }
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
