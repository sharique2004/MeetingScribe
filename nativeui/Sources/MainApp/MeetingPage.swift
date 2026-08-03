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
            SpeakerRibbon(detail: detail)
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
            Text(text).font(MSFont.sectionHeading).foregroundStyle(MS.ink)
        case .userParagraph(let text):
            Text(text)
                .font(MSFont.body)
                .lineSpacing(9)
                .foregroundStyle(MS.ink)
                .textSelection(.enabled)
        case .bullet(_, let text, let evidence):
            EvidenceDisclosure(text: text, evidence: evidence, player: player,
                               onOpenTranscript: onOpenTranscript)
        case .actionItem(_, let item, let evidence):
            ActionRow(item: item, evidence: evidence, player: player)
        }
    }

    private func nextStepsView(_ steps: [DocumentBlock]) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("NEXT STEPS")
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
        VStack(alignment: .leading, spacing: 6) {
            Text(detail.turns?.isEmpty == false ? "No summary yet." : "Still audio-only.")
                .font(MSFont.body)
                .foregroundStyle(MS.ink2)
            Text(detail.turns?.isEmpty == false
                 ? "Recaps are written after a meeting is summarised."
                 : "Transcription usually takes about a third of the meeting's length.")
                .font(MSFont.meta)
                .foregroundStyle(MS.ink3)
        }
        .padding(.bottom, 28)
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

            Text(footerStats)
                .font(MSFont.meta)
                .foregroundStyle(MS.ink3)
            Spacer()
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
