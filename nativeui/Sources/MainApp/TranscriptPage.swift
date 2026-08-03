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
    @State private var following = true

    private var turns: [(id: Int, turn: Turn)] {
        let all = (detail.turns ?? []).enumerated().map { (id: $0.offset, turn: $0.element) }
        guard !query.isEmpty else { return all }
        return all.filter { $0.turn.text.localizedCaseInsensitiveContains(query) }
    }

    private var activeIndex: Int? {
        guard player.playing || player.time > 0 else { return nil }
        let all = detail.turns ?? []
        return all.lastIndex(where: { $0.start <= player.time })
    }

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                VStack(alignment: .leading, spacing: 0) {
                    header
                        .padding(.top, 48)
                        .padding(.bottom, 24)
                    LazyVStack(alignment: .leading, spacing: 22) {
                        ForEach(turns, id: \.id) { item in
                            TranscriptTurn(
                                turn: item.turn,
                                name: name(item.turn.speaker),
                                color: MS.speaker(item.turn.speaker),
                                active: item.id == activeIndex,
                                time: player.time
                            ) {
                                player.seek(item.turn.start)
                                player.play()
                            }
                            .id(item.id)
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
                withAnimation(Motion.seek) {
                    proxy.scrollTo(idx, anchor: UnitPoint(x: 0.5, y: 0.4))
                }
            }
            .overlay(alignment: .bottomTrailing) {
                if !following, player.playing {
                    Button {
                        following = true
                        if let idx = activeIndex {
                            withAnimation(Motion.seek) { proxy.scrollTo(idx, anchor: .center) }
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

    private var header: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(alignment: .bottom, spacing: 12) {
                PageTabs(mode: $mode)
                Spacer()
                CopyTranscriptButton(detail: detail)
                    .padding(.bottom, 6)
                HStack(spacing: 5) {
                    Image(systemName: "magnifyingglass")
                        .font(.system(size: 10))
                        .foregroundStyle(MS.ink3)
                    TextField("Find in transcript", text: $query)
                        .textFieldStyle(.plain)
                        .font(MSFont.meta)
                        .frame(width: 150)
                }
                .padding(.horizontal, 9)
                .padding(.vertical, 5)
                .background(MS.raised, in: .capsule)
                .padding(.bottom, 6)
            }
            Rectangle().fill(MS.hairline).frame(height: 1)
        }
    }

    private func name(_ key: String?) -> String {
        guard let key else { return "Speaker" }
        return detail.speakers?[key] ?? key.capitalized
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
                try? await Task.sleep(nanoseconds: 1_600_000_000)
                withAnimation(Motion.exit) { copied = false }
            }
        } label: {
            Label(copied ? "Copied" : "Copy", systemImage: copied ? "checkmark" : "doc.on.doc")
                .font(MSFont.meta)
                .foregroundStyle(copied ? AnyShapeStyle(MS.playhead) : AnyShapeStyle(MS.ink2))
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
    let onSeek: () -> Void

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 16) {
            Button(action: onSeek) {
                Text(clock(turn.start))
                    .clockFont(11)
                    .foregroundStyle(active ? AnyShapeStyle(MS.playhead) : AnyShapeStyle(MS.ink4))
            }
            .buttonStyle(PressStyle())
            .frame(width: 48, alignment: .trailing)

            VStack(alignment: .leading, spacing: 4) {
                Text(name.uppercased())
                    .font(MSFont.kicker)
                    .kerning(0.55)
                    .foregroundStyle(color)
                Group {
                    if active {
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
        .animation(.spring(response: 0.28, dampingFraction: 0.9), value: active)
    }

    /// The travelling word: interpolate the spoken word within the active
    /// turn proportionally to character count — indistinguishable from real
    /// word stamps at conversational pace.
    private var spokenText: Text {
        guard active, let end = turn.end, end > turn.start else {
            return Text(turn.text)
                .font(MSFont.spoken)
                .foregroundColor(active ? MS.ink : MS.ink2)
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
        var result = Text(verbatim: "")
        for (i, w) in words.enumerated() {
            var piece = Text(String(w)).font(MSFont.spoken)
            if i == activeWord {
                piece = piece.foregroundColor(MS.ink)
                    .customAttribute(ActiveWordAttribute())
            } else if i < activeWord {
                piece = piece.foregroundColor(MS.ink)
            } else {
                piece = piece.foregroundColor(MS.ink2)
            }
            result = i == 0 ? piece : result + Text(verbatim: " ") + piece
        }
        return result
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
