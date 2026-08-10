// The talk-time ribbon: one quiet lane per speaker showing WHEN they spoke
// across the meeting, with share and minutes at the right. This popover is
// the entire replacement for the old Speakers tab — so it is also the one
// place a speaker gets a real name, and naming someone is what teaches this
// Mac their voice. The lane admits that in the same breath.
import SwiftUI

private struct SpeakerLane: Identifiable {
    let id: String
    let name: String
    let color: Color
    let spans: [(Double, Double)]   // fraction of meeting: (start, width)
    let share: Double
    let seconds: Double
    /// Pace, questions, words, longest monologue — everything stats.py works
    /// out about one person, which nothing in this app read until now.
    let stats: SpeakerStats?

    /// The second line: how they talked, not just how much.
    var insight: String {
        guard let stats else { return "" }
        var parts: [String] = []
        if let words = stats.words, words > 0 { parts.append("\(words.formatted()) words") }
        if let wpm = stats.wpm, wpm > 0 { parts.append("\(Int(wpm)) wpm") }
        if let questions = stats.questions, questions > 0 {
            parts.append("\(questions) question\(questions == 1 ? "" : "s")")
        }
        if let longest = stats.longest_turn_seconds, longest >= 30 {
            parts.append("longest \(clock(longest))")
        }
        return parts.joined(separator: " · ")
    }
}

struct SpeakerRibbon: View {
    let detail: MeetingDetail
    @ObservedObject var model: MeetingModel

    @State private var hovering = false

    private var lanes: [SpeakerLane] {
        let duration = max(detail.duration ?? 1, 1)
        let turns = detail.turns ?? []
        let stats = detail.stats?.per_speaker ?? [:]
        let keys = (detail.speakers ?? [:]).keys.sorted { a, b in
            (stats[a]?.seconds ?? 0) > (stats[b]?.seconds ?? 0)
        }
        return keys.map { key in
            let spans = turns.filter { $0.speaker == key }.map { turn -> (Double, Double) in
                let start = turn.start / duration
                let width = max(((turn.end ?? turn.start + 2) - turn.start) / duration, 0.003)
                return (start, width)
            }
            return SpeakerLane(
                id: key,
                name: detail.speakers?[key] ?? key,
                color: MS.speaker(key),
                spans: spans,
                share: stats[key]?.share ?? 0,
                seconds: stats[key]?.seconds ?? 0,
                stats: stats[key])
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 8) {
                Text("WHO SPOKE WHEN")
                    .font(MSFont.kicker)
                    .kerning(0.55)
                    .foregroundStyle(MS.ink3)
                Spacer(minLength: 0)
                if hovering {
                    Text("Click a name to rename")
                        .font(MSFont.kicker.weight(.regular))
                        .foregroundStyle(MS.ink4)
                        .transition(.opacity)
                }
            }
            ForEach(lanes) { lane in
                SpeakerLaneRow(lane: lane, meetingID: detail.id, model: model)
            }
        }
        .onHover { hovering = $0 }
        .animation(Motion.micro, value: hovering)
    }
}

/// One lane. The name is an editable field — renaming here is the only way
/// a speaker gets a real name, and it is also what enrolls their voice, so
/// the confirmation of that enrollment lands directly under the field the
/// user just typed in, with a way back out.
private struct SpeakerLaneRow: View {
    let lane: SpeakerLane
    let meetingID: String
    @ObservedObject var model: MeetingModel

    @State private var name = ""
    @State private var saved: VoiceProfile?  // the profile this rename enrolled
    @State private var notice = 0            // which commit owns the dismiss timer
    @State private var committing = false
    @FocusState private var editing: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 10) {
                HStack(spacing: 5) {
                    Circle().fill(lane.color).frame(width: 6, height: 6)
                    TextField("Speaker", text: $name)
                        .textFieldStyle(.plain)
                        .font(MSFont.meta.weight(.medium))
                        .foregroundStyle(MS.ink)
                        .lineLimit(1)
                        .focused($editing)
                        .onSubmit(commit)
                        .help("Name this speaker and they're labelled everywhere in the meeting")
                }
                .frame(width: 92, alignment: .leading)

                GeometryReader { geo in
                    ZStack(alignment: .leading) {
                        Capsule().fill(MS.ink4.opacity(0.25))
                        ForEach(Array(lane.spans.enumerated()), id: \.offset) { _, span in
                            Capsule()
                                .fill(lane.color)
                                .frame(width: max(geo.size.width * span.1, 2))
                                .offset(x: geo.size.width * span.0)
                        }
                    }
                }
                .frame(height: 6)

                Text("\(Int(lane.share * 100))% · \(Int(lane.seconds / 60)) min")
                    .clockFont(11)
                    .foregroundStyle(MS.ink2)
                    .frame(width: 78, alignment: .trailing)
            }

            if !lane.insight.isEmpty {
                Text(lane.insight)
                    .font(MSFont.kicker.weight(.regular))
                    .foregroundStyle(MS.ink3)
                    .padding(.leading, 11)   // under the name, past the identity dot
            }

            if let saved {
                enrollmentNotice(saved)
            }
        }
        .onAppear { name = lane.name }
        .onChange(of: lane.name) { name = lane.name }
        // Naming someone also enrolls their voice, so the edit commits on
        // Return and on nothing else. That is only honest if abandoning the
        // field visibly puts the real name back — otherwise the lane keeps
        // showing a name nobody saved.
        .onChange(of: editing) { _, focused in
            if !focused, !committing, name != lane.name { name = lane.name }
        }
    }

    /// The disclosure. Something was saved about a person; say so in plain
    /// words, where it happened, with one click to take it back and one to
    /// see everything this Mac has learned. The engine names the profile it
    /// enrolled into, so the notice and its way out arrive together, complete,
    /// in one animation.
    private func enrollmentNotice(_ profile: VoiceProfile) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(noticeText(profile))
                .font(MSFont.meta)
                .foregroundStyle(MS.ink2)
                .fixedSize(horizontal: false, vertical: true)
            HStack(spacing: 14) {
                Button {
                    forget(profile)
                } label: {
                    Text("Forget this voice")
                        .font(MSFont.meta)
                        .foregroundStyle(MS.playhead)
                }
                .buttonStyle(PressStyle())
                .help(forgetHelp(profile))

                SettingsLink {
                    Text("Manage voices")
                        .font(MSFont.meta)
                        .foregroundStyle(MS.ink3)
                }
                .buttonStyle(PressStyle())
            }
        }
        .padding(.leading, 11)   // under the name, past the identity dot
        .transition(.asymmetric(
            insertion: .offset(y: -4).combined(with: .opacity),
            removal: .opacity.animation(Motion.exit)))
    }

    /// The engine's own name for the profile, not the string typed into the
    /// field: it decided where this voice belongs, and it may have trimmed the
    /// name on the way in.
    private func noticeText(_ profile: VoiceProfile) -> String {
        profile.n_samples > 1
            ? "Added this meeting to \(profile.name)'s saved voice, so future meetings label them automatically. No audio is kept."
            : "Saved \(profile.name)'s voice fingerprint on this Mac, so future meetings label them automatically. No audio is kept."
    }

    private func forgetHelp(_ profile: VoiceProfile) -> String {
        let scope = profile.n_samples > 1
            ? "Forgets this voice everywhere, including the \(profile.n_samples) meetings it was learned from."
            : "Forgets this voice."
        return scope + " The name you typed stays on this meeting."
    }

    // MARK: - Commit

    private func commit() {
        // Trim only to answer "did anything actually change" — the engine owns
        // what gets stored, and it hands the resulting profile straight back,
        // so nothing here has to predict its stripping or its 60-character cap.
        let typed = name.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !typed.isEmpty, typed != lane.name else {
            name = lane.name        // an empty or unchanged edit is not an edit
            return
        }
        notice &+= 1
        let token = notice
        committing = true
        Task {
            let result = try? await API.renameSpeaker(
                meetingID, key: lane.id, name: typed)
            committing = false
            guard let result else {
                // The engine refused the name or never answered. Put the real
                // one back rather than leave the lane showing an edit that
                // reached nothing.
                name = lane.name
                return
            }
            await model.reload()
            // One answer, one notice: the engine says WHICH profile it enrolled
            // into, so the disclosure and its way out appear together and never
            // depend on a second call. Nothing is guessed by name here, which
            // matters now that two people are allowed to share one.
            guard let profile = result.voice_profile else { return }
            withAnimation(Motion.enter) { saved = profile }
            try? await Task.sleep(nanoseconds: 10_000_000_000)
            // Still needed, and NOT because of any lookup: this timer outlives
            // its own notice. Renaming again within the 10 s starts a second
            // notice, and without the token this older sleep would dismiss it
            // early. Identity, not the name — renaming away and back counts as
            // a new notice too.
            guard token == notice else { return }
            withAnimation(Motion.exit) { saved = nil }
        }
    }

    private func forget(_ profile: VoiceProfile) {
        Task {
            // Only claim the voice is forgotten once the engine says it is.
            // Dismissing first would leave the user believing a fingerprint
            // was deleted while it is still sitting on disk.
            guard (try? await API.deleteVoiceProfile(profile.id)) != nil else { return }
            notice &+= 1   // retire the pending timer with the notice it owned
            withAnimation(Motion.exit) { saved = nil }
        }
    }
}
