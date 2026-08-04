// The window during a meeting: notes-first, document-shaped. The human
// types at full ink; the machine's live captions run underneath in serif
// at secondary ink, dissolving in through a top mask. Timer, levels and
// stop all live in the pill — the window is for the human.
import SwiftUI

struct LivePage: View {
    @ObservedObject var center: RecorderCenter
    @FocusState private var noteFocused: Bool

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                VStack(alignment: .leading, spacing: 0) {
                    // Dateline: the only red in the window. The two meters
                    // ride with it, because "is this being recorded" is the
                    // one question the recording screen must always answer.
                    HStack(spacing: 10) {
                        HStack(spacing: 7) {
                            Circle().fill(MS.recordRed).frame(width: 7, height: 7)
                            Text("Recording")
                                .font(MSFont.meta)
                                .foregroundStyle(MS.ink3)
                            Text(clock(center.elapsed))
                                .clockFont(12)
                                .foregroundStyle(MS.ink2)
                        }
                        TrackMeters(mic: center.micTrack, system: center.systemTrack)
                    }
                    .frame(maxWidth: .infinity)
                    .padding(.top, 48)

                    Text(Date().formatted(.dateTime.weekday(.wide).day().month(.wide)))
                        .font(MSFont.pageTitle)
                        .foregroundStyle(MS.ink)
                        .padding(.top, 14)

                    // Everything wrong with the capture, while it can still be
                    // fixed. Until this, a track that died mid-meeting was
                    // invisible until Stop: the timer kept running and the
                    // meter simply went flat.
                    captureNotices

                    Rectangle().fill(MS.hairline).frame(height: 1)
                        .padding(.top, 24)
                        .padding(.bottom, 28)

                    // The human's notes, full ink, timestamped.
                    VStack(alignment: .leading, spacing: 12) {
                        ForEach(center.sentNotes) { note in
                            HStack(alignment: .firstTextBaseline, spacing: 14) {
                                Text(note.t.map(clock) ?? "")
                                    .clockFont(11)
                                    .foregroundStyle(MS.ink4)
                                    .frame(width: 44, alignment: .trailing)
                                Text(note.text)
                                    .font(MSFont.body)
                                    .lineSpacing(8)
                                    .foregroundStyle(MS.ink)
                            }
                            .id(note.id)
                        }

                        HStack(alignment: .firstTextBaseline, spacing: 14) {
                            Text(clock(center.elapsed))
                                .clockFont(11)
                                .foregroundStyle(MS.ink4.opacity(0.6))
                                .frame(width: 44, alignment: .trailing)
                            TextField("Type a note, ⏎ stamps this moment", text: $center.noteDraft, axis: .vertical)
                                .textFieldStyle(.plain)
                                .font(MSFont.body)
                                .foregroundStyle(MS.ink)
                                .lineLimit(1...6)
                                .focused($noteFocused)
                                .onSubmit { center.sendNote() }
                        }
                        .id("live-composer")

                        if let refusal = center.noteRefusal {
                            Text("That note wasn't saved: \(refusal)")
                                .font(MSFont.meta)
                                .foregroundStyle(MS.ink2)
                                .padding(.leading, 58)
                        } else if center.noteSendFailed {
                            Text("The engine hasn't taken that note yet. It's saved on this Mac and will be filed as soon as the engine answers.")
                                .font(MSFont.meta)
                                .foregroundStyle(MS.ink3)
                                .padding(.leading, 58)
                        }
                    }

                    // The machine, one ink down, in serif: the last few
                    // caption lines dissolving in.
                    if !captionTail.isEmpty {
                        VStack(alignment: .leading, spacing: 9) {
                            ForEach(Array(captionTail.enumerated()), id: \.offset) { i, line in
                                Text(line)
                                    .font(MSFont.evidence)
                                    .lineSpacing(7)
                                    .foregroundStyle(MS.ink2.opacity(opacityFor(i)))
                                    .transition(.offset(y: 6).combined(with: .opacity))
                            }
                        }
                        .padding(.leading, 58)
                        .padding(.top, 34)
                        .animation(Motion.enter, value: captionTail)
                    } else {
                        HStack(spacing: 6) {
                            Text("Listening")
                                .font(MSFont.evidence)
                                .foregroundStyle(MS.ink3)
                            ListeningEllipsis()
                        }
                        .padding(.leading, 58)
                        .padding(.top, 34)
                    }
                }
                .documentMeasure()
                .padding(.bottom, 112)
            }
            .background(MS.content)
            .onChange(of: center.sentNotes.count) {
                withAnimation(Motion.seek) { proxy.scrollTo("live-composer", anchor: .bottom) }
            }
            .onAppear { noteFocused = true }
        }
    }

    /// A dead track, a track that never carried anything, whatever the engine
    /// warned about as the recording started, and the disk running out. All of
    /// it was previously discoverable only after the meeting had ended.
    @ViewBuilder
    private var captureNotices: some View {
        let alerts = center.captureAlerts
        let notes = center.startWarnings + [center.diskNote].compactMap { $0 }
        if center.alert != nil || !alerts.isEmpty || !notes.isEmpty {
            VStack(alignment: .leading, spacing: 10) {
                // A Stop that was refused. The recording is still running and
                // the person who pressed it thinks it isn't.
                if let refusal = center.alert {
                    NoticeBand(icon: "exclamationmark.triangle",
                               text: refusal.headline + ". " + refusal.message) {
                        Button("Dismiss") { center.alert = nil }
                            .buttonStyle(.plain)
                            .font(MSFont.meta)
                            .foregroundStyle(MS.ink3)
                    }
                }
                ForEach(alerts) { alert in
                    NoticeBand(icon: "exclamationmark.triangle",
                               text: alert.title + ". " + alert.detail)
                }
                ForEach(Array(notes.enumerated()), id: \.offset) { _, note in
                    HStack(alignment: .firstTextBaseline, spacing: 7) {
                        Image(systemName: "info.circle")
                            .font(.system(size: 10))
                            .foregroundStyle(MS.ink4)
                        Text(note)
                            .font(MSFont.meta)
                            .foregroundStyle(MS.ink3)
                            .fixedSize(horizontal: false, vertical: true)
                            .textSelection(.enabled)
                    }
                }
            }
            // Inside the branch, so a healthy meeting leaves no gap where a
            // warning would have been.
            .padding(.top, 22)
            .animation(Motion.enter, value: alerts)
        }
    }

    /// The last three caption lines (finals), plus the growing partial.
    private var captionTail: [String] {
        var lines = center.liveTurns.suffix(3).map { turn in
            "\(turn.who ?? (turn.track == "mic" ? "You" : "Meeting")): \(turn.text)"
        }
        for key in center.livePartials.keys.sorted() {
            if let p = center.livePartials[key], let t = p.text, !t.isEmpty {
                lines.append("\(p.who ?? (key == "mic" ? "You" : "Meeting")): \(t)…")
            }
        }
        return Array(lines.suffix(4))
    }

    private func opacityFor(_ index: Int) -> Double {
        let n = captionTail.count
        return 0.45 + 0.55 * Double(index + 1) / Double(max(n, 1))
    }
}

struct ListeningEllipsis: View {
    @State private var step = 0
    private let timer = Timer.publish(every: 0.45, on: .main, in: .common).autoconnect()

    var body: some View {
        HStack(spacing: 2.5) {
            ForEach(0..<3, id: \.self) { i in
                Circle()
                    .fill(MS.ink3)
                    .frame(width: 3, height: 3)
                    .opacity(step == i ? 1 : 0.3)
            }
        }
        .onReceive(timer) { _ in
            withAnimation(Motion.micro) { step = (step + 1) % 3 }
        }
    }
}
