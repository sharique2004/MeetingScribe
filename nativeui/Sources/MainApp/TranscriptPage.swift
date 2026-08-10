// Transcript mode: the same 680pt column, re-typeset — New York for the
// spoken words (serif is human speech), SF Mono timestamps in a quiet
// gutter, speaker labels in identity color. During playback the active
// turn lifts to full ink over a 6% speaker wash and the spoken word carries
// a travelling mint underglow, synced-lyrics style.
import SwiftUI

struct TranscriptPage: View {
    let detail: MeetingDetail
    @ObservedObject var player: Playback
    @Binding var mode: PageMode
    @State private var query = ""
    @State private var matches: [TranscriptFind.Match] = []
    @State private var current = 0
    @State private var following = true
    @FocusState private var findFocused: Bool

    /// Every turn, always. Find used to FILTER this list, which answered
    /// "where was that said?" by deleting everything around it — the one
    /// question a transcript search is asked.
    private var turns: [Turn] { detail.turns ?? [] }

    private var activeIndex: Int? {
        guard player.playing || player.time > 0 else { return nil }
        return turns.lastIndex(where: { $0.start <= player.time })
    }

    /// Which occurrence inside a given turn is the one being looked at.
    private func currentOccurrence(in turnIndex: Int) -> Int? {
        guard matches.indices.contains(current) else { return nil }
        let match = matches[current]
        return match.turn == turnIndex ? match.occurrence : nil
    }

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                VStack(alignment: .leading, spacing: 0) {
                    header
                        .padding(.top, 48)
                        .padding(.bottom, 24)
                    // Both worked out once per pass. `activeIndex` used to be
                    // read inside the row closure, which is a scan of the
                    // whole transcript per row, ten times a second.
                    let rows = turns
                    let active = activeIndex
                    if rows.isEmpty {
                        emptyTranscript
                    }
                    LazyVStack(alignment: .leading, spacing: 22) {
                        ForEach(rows.indices, id: \.self) { i in
                            let turn = rows[i]
                            TranscriptTurn(
                                turn: turn,
                                name: name(turn.speaker),
                                color: MS.speaker(turn.speaker),
                                active: i == active,
                                // Only the turn being spoken has a travelling
                                // word to move; handing the clock to the rest
                                // invalidates the whole transcript at 10Hz.
                                time: i == active ? player.time : 0,
                                query: query,
                                currentOccurrence: currentOccurrence(in: i)
                            ) {
                                player.seek(turn.start)
                                player.play()
                            }
                            .id(i)
                        }
                    }
                }
                .documentMeasure()
                .padding(.bottom, 112)
            }
            .background(MS.content)
            .onScrollPhaseChange { _, phase in
                if phase == .interacting { following = false }
            }
            .onChange(of: activeIndex) {
                guard following, player.playing, let idx = activeIndex, query.isEmpty else { return }
                msWithAnimation(Motion.seek) {
                    proxy.scrollTo(idx, anchor: UnitPoint(x: 0.5, y: 0.4))
                }
            }
            .onChange(of: query) {
                matches = TranscriptFind.matches(in: detail.turns ?? [], query: query)
                current = 0
                // Searching means reading, not following the playhead.
                if !query.isEmpty { following = false }
                show(match: current, with: proxy)
            }
            .onChange(of: current) { show(match: current, with: proxy) }
            .overlay(alignment: .bottomTrailing) {
                if !following, player.playing {
                    Button {
                        following = true
                        if let idx = activeIndex {
                            msWithAnimation(Motion.seek) { proxy.scrollTo(idx, anchor: .center) }
                        }
                    } label: {
                        Label("Now", systemImage: "arrow.down.to.line")
                            .font(.system(size: 11, weight: .semibold))
                            .padding(.horizontal, 10)
                            .padding(.vertical, 6)
                    }
                    .buttonStyle(PressStyle())
                    .glassEffect(.regular.interactive(), in: .capsule)
                    .padding(.trailing, 20)
                    .padding(.bottom, 118)
                    .transition(.asymmetric(
                        insertion: .offset(y: 6).combined(with: .opacity).animation(Motion.enter),
                        removal: .opacity.animation(Motion.exit)))
                }
            }
        }
    }

    /// A transcript with nothing in it, said out loud.
    ///
    /// The header used to sit over blank space, which reads as a page that
    /// failed to load rather than as a recording nobody spoke in — and the
    /// difference matters most to the person wondering whether their meeting
    /// was captured at all.
    private var emptyTranscript: some View {
        Text(emptyTranscriptText)
            .font(MSFont.body)
            .lineSpacing(9)
            .foregroundStyle(MS.ink2)
            .multilineTextAlignment(.center)
            .fixedSize(horizontal: false, vertical: true)
            .frame(maxWidth: .infinity, alignment: .center)
            .padding(.top, 96)
            .padding(.horizontal, 20)
    }

    /// Which silence this is. Notes change what it means: a meeting somebody
    /// wrote in is not empty, only unspoken, and the sentence should point
    /// back at the page their words are on. And neither sentence is true while
    /// the engine is still working or after it gave up — nothing was detected
    /// yet because nothing has finished listening.
    private var emptyTranscriptText: String {
        if meetingIsWorking(detail.status) {
            return "The transcript is still being written. It appears here as soon as the engine is done."
        }
        if detail.failed {
            return detail.failureText
        }
        let written = (detail.notes ?? []).contains {
            !$0.text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        }
        return written
            ? "No speech was detected, so this meeting is only what you wrote down."
            : "No speech was detected in this recording, so there is nothing to read here."
    }

    private func show(match index: Int, with proxy: ScrollViewProxy) {
        guard matches.indices.contains(index) else { return }
        msWithAnimation(Motion.seek) {
            proxy.scrollTo(matches[index].turn, anchor: UnitPoint(x: 0.5, y: 0.35))
        }
    }

    private func step(_ delta: Int) {
        guard !matches.isEmpty else { return }
        current = (current + delta + matches.count) % matches.count
    }

    private func closeFind() {
        query = ""
        matches = []
        current = 0
        findFocused = false
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(alignment: .bottom, spacing: 12) {
                PageTabs(mode: $mode)
                Spacer()
                CopyTranscriptButton(detail: detail)
                    .padding(.bottom, 6)
                findBar
                    .padding(.bottom, 6)
            }
            Rectangle().fill(MS.hairline).frame(height: 1)
        }
    }

    /// A find, not a filter: the transcript stays whole, every hit is lit, and
    /// the count says how many there are and which one you are on. ⌘F opens
    /// it, Return and ⌘G walk the hits, esc puts the page back.
    private var findBar: some View {
        HStack(spacing: 5) {
            // Four glyphs and not a word between them. The name is the string;
            // `.iconOnly` keeps the bar looking exactly as it did while giving
            // VoiceOver something better to read than "chevron up, button".
            Button("Find in transcript", systemImage: "magnifyingglass") {
                findFocused = true
            }
            .labelStyle(.iconOnly)
            .font(.system(size: 10))
            .foregroundStyle(MS.ink3)
            .buttonStyle(PressStyle())
            .keyboardShortcut("f", modifiers: .command)
            .help("Find in transcript (⌘F)")

            TextField("Find in transcript", text: $query)
                .textFieldStyle(.plain)
                .font(MSFont.meta)
                .focused($findFocused)
                .frame(width: 132)
                .onSubmit { step(1) }
                .onExitCommand { closeFind() }

            if !query.isEmpty {
                Text(countText)
                    .font(MSFont.kicker)
                    .monospacedDigit()
                    .foregroundStyle(matches.isEmpty ? MS.ink3 : MS.ink2)
                    .fixedSize()

                Button("Previous match", systemImage: "chevron.up") { step(-1) }
                    .labelStyle(.iconOnly)
                    .font(.system(size: 9, weight: .bold))
                    .buttonStyle(PressStyle())
                    .disabled(matches.isEmpty)
                    .keyboardShortcut("g", modifiers: [.command, .shift])
                    .help("Previous match (⇧⌘G)")

                Button("Next match", systemImage: "chevron.down") { step(1) }
                    .labelStyle(.iconOnly)
                    .font(.system(size: 9, weight: .bold))
                    .buttonStyle(PressStyle())
                    .disabled(matches.isEmpty)
                    .keyboardShortcut("g", modifiers: .command)
                    .help("Next match (⌘G)")

                Button("Clear the search", systemImage: "xmark") { closeFind() }
                    .labelStyle(.iconOnly)
                    .font(.system(size: 9, weight: .bold))
                    .buttonStyle(PressStyle())
                    .foregroundStyle(MS.ink3)
                    .help("Clear (esc)")
            }
        }
        .foregroundStyle(MS.ink2)
        .padding(.horizontal, 9)
        .padding(.vertical, 5)
        .background(MS.raised, in: .capsule)
        .animation(Motion.micro, value: query.isEmpty)
    }

    private var countText: String {
        matches.isEmpty ? "No matches" : "\(current + 1) of \(matches.count)"
    }

    private func name(_ key: String?) -> String {
        guard let key else { return "Speaker" }
        return detail.speakers?[key] ?? key.capitalized
    }
}

// MARK: - Find

/// One definition of what "matches" means, used by the counter and by the
/// highlighter, so the number on the bar can never disagree with the marks on
/// the page.
enum TranscriptFind {
    struct Match: Hashable {
        let turn: Int
        /// Which hit within that turn — a turn can contain the word twice.
        let occurrence: Int
    }

    static let options: String.CompareOptions = [.caseInsensitive, .diacriticInsensitive]
    /// A cap per turn, so a one-character query on a two-hour transcript
    /// cannot spend the frame counting.
    private static let perTurnLimit = 100

    static func matches(in turns: [Turn], query: String) -> [Match] {
        let needle = query.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !needle.isEmpty else { return [] }
        var out: [Match] = []
        for (i, turn) in turns.enumerated() {
            var rest = Substring(turn.text)
            var n = 0
            while n < perTurnLimit,
                  let range = rest.range(of: needle, options: options) {
                out.append(Match(turn: i, occurrence: n))
                n += 1
                rest = rest[range.upperBound...]
            }
        }
        return out
    }

    /// The turn's words with every hit washed in mint and the one being
    /// looked at washed harder. Nothing is removed, nothing is reordered.
    static func highlight(_ text: String, query: String, current: Int?) -> AttributedString {
        let needle = query.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !needle.isEmpty else { return AttributedString(text) }
        var out = AttributedString()
        var rest = Substring(text)
        var n = 0
        while n < perTurnLimit, let range = rest.range(of: needle, options: options) {
            out.append(AttributedString(rest[rest.startIndex..<range.lowerBound]))
            var hit = AttributedString(rest[range])
            hit.backgroundColor = MS.playheadFill.opacity(n == current ? 0.55 : 0.2)
            hit.foregroundColor = MS.ink
            out.append(hit)
            rest = rest[range.upperBound...]
            n += 1
        }
        out.append(AttributedString(rest))
        return out
    }
}

/// One click, whole transcript on the clipboard, with a moment of "Copied".
struct CopyTranscriptButton: View {
    let detail: MeetingDetail
    @State private var copied = false

    var body: some View {
        Button {
            let pb = NSPasteboard.general
            pb.clearContents()
            pb.setString(transcriptText(detail), forType: .string)
            withAnimation(Motion.micro) { copied = true }
            Task {
                try? await Task.sleep(for: .milliseconds(1600))
                withAnimation(Motion.exit) { copied = false }
            }
        } label: {
            Label(copied ? "Copied" : "Copy", systemImage: copied ? "checkmark" : "doc.on.doc")
                .font(MSFont.meta)
                .foregroundStyle(copied ? MS.playhead : MS.ink2)
                .padding(.horizontal, 9)
                .padding(.vertical, 5)
                .background(MS.raised, in: .capsule)
                .contentTransition(.symbolEffect(.replace))
        }
        .buttonStyle(PressStyle())
        .help("Copy the whole transcript")
        .disabled((detail.turns ?? []).isEmpty)
    }
}

struct TranscriptTurn: View {
    let turn: Turn
    let name: String
    let color: Color
    let active: Bool
    let time: Double
    /// What the find bar is looking for, "" when it is closed.
    var query: String = ""
    /// The hit inside THIS turn that the find bar is parked on, if any.
    var currentOccurrence: Int?
    let onSeek: () -> Void

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 16) {
            Button(action: onSeek) {
                Text(clock(turn.start))
                    .clockFont(11)
                    .foregroundStyle(active ? MS.playhead : MS.ink4)
            }
            .buttonStyle(PressStyle())
            .frame(width: 48, alignment: .trailing)

            VStack(alignment: .leading, spacing: 4) {
                Text(name.uppercased())
                    .font(MSFont.kicker)
                    .kerning(0.55)
                    .foregroundStyle(color)
                Group {
                    if !query.isEmpty {
                        // Searching and the travelling-word underglow are two
                        // readings of the same line; the one being asked for
                        // wins.
                        foundText
                    } else if active {
                        spokenText.textRenderer(ActiveWordUnderglow())
                    } else {
                        spokenText
                    }
                }
                .lineSpacing(10)
                .textSelection(.enabled)
            }
            .padding(.vertical, active ? 8 : 0)
            .padding(.horizontal, active ? 10 : 0)
            .background {
                if active {
                    RoundedRectangle(cornerRadius: 8, style: .continuous)
                        .fill(color.opacity(0.06))
                }
            }
        }
        .msAnimation(Motion.enter, value: active)
    }

    private var foundText: Text {
        Text(TranscriptFind.highlight(turn.text, query: query, current: currentOccurrence))
            .font(MSFont.spoken)
            .foregroundStyle(active ? MS.ink : MS.ink2)
    }

    /// The travelling word: interpolate the spoken word within the active
    /// turn proportionally to character count — indistinguishable from real
    /// word stamps at conversational pace.
    private var spokenText: Text {
        guard active, let end = turn.end, end > turn.start else {
            return Text(turn.text)
                .font(MSFont.spoken)
                .foregroundStyle(active ? MS.ink : MS.ink2)
        }
        let progress = min(max((time - turn.start) / (end - turn.start), 0), 1)
        let words = turn.text.split(separator: " ", omittingEmptySubsequences: false)
        let totalChars = max(turn.text.count, 1)
        var acc = 0
        var activeWord = words.count - 1
        for (i, w) in words.enumerated() {
            acc += w.count + 1
            if Double(acc) / Double(totalChars) >= progress {
                activeWord = i
                break
            }
        }
        // Three runs, not one per word: said, saying, unsaid. The line used to
        // be built by adding a Text per word, which is deprecated and grows
        // with the turn; interpolation composes the same three styles once.
        // The separating spaces live INSIDE the runs so the word carrying
        // ActiveWordAttribute is exactly one word wide and the underglow
        // capsule hugs it rather than the space beside it.
        let said = words[..<activeWord].joined(separator: " ")
        let unsaid = words[(activeWord + 1)...].joined(separator: " ")
        let head = Text(said.isEmpty ? "" : said + " ")
            .font(MSFont.spoken)
            .foregroundStyle(MS.ink)
        let saying = Text(String(words[activeWord]))
            .font(MSFont.spoken)
            .foregroundStyle(MS.ink)
            .customAttribute(ActiveWordAttribute())
        let tail = Text(unsaid.isEmpty ? "" : " " + unsaid)
            .font(MSFont.spoken)
            .foregroundStyle(MS.ink2)
        return Text("\(head)\(saying)\(tail)")
    }
}

/// Marks the active word so a TextRenderer can draw the mint underglow
/// capsule behind exactly that glyph run.
struct ActiveWordAttribute: TextAttribute {}

/// Draws a soft mint capsule behind the run carrying ActiveWordAttribute —
/// the travelling word underglow.
struct ActiveWordUnderglow: TextRenderer {
    func draw(layout: Text.Layout, in ctx: inout GraphicsContext) {
        for line in layout {
            for run in line {
                if run[ActiveWordAttribute.self] != nil {
                    let r = run.typographicBounds.rect.insetBy(dx: -3.5, dy: -1.5)
                    ctx.fill(
                        Path(roundedRect: r, cornerRadius: r.height / 2),
                        with: .color(MS.playheadFill.opacity(0.22)))
                }
                ctx.draw(run)
            }
        }
    }
}
