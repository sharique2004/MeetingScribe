// Settings: who writes the summaries, and what the app does around a
// meeting. The engine reads ~/.meetingscribe/config.json fresh on every
// summarize call, so a change here applies to the very next summary — no
// restart, no engine bounce.
import SwiftUI

@MainActor
final class Settings: ObservableObject {
    static let shared = Settings()

    @Published var summaryEngine: String {
        didSet { write("summary_engine", summaryEngine) }
    }

    /// Whether naming a speaker also teaches this Mac their voice. Same
    /// config.json, read fresh by the engine on every rename, so the switch
    /// applies to the very next one.
    @Published var voiceProfiles: Bool {
        didSet { write("voice_profiles", voiceProfiles) }
    }

    private let path = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent(".meetingscribe/config.json")

    /// Claude if this Mac has it, Apple Intelligence otherwise.
    ///
    /// Both halves are load-bearing. Nothing has to be installed for the app
    /// to summarise and answer questions — that is the point of the on-device
    /// path, and it is what a fresh download gets. But an audit of both
    /// engines against real transcripts is unambiguous that the on-device
    /// model is the weaker writer on long meetings: it paraphrases loosely,
    /// and it invents specifics (durations, amounts) that a reader would act
    /// on. Where the better engine is already present, defaulting to it is
    /// the honest choice; Settings switches either way in one click.
    static var recommendedEngine: String {
        let home = NSHomeDirectory()
        let claude = ["/opt/homebrew/bin/claude", "/usr/local/bin/claude",
                      "\(home)/.claude/local/claude", "\(home)/.local/bin/claude"]
        return claude.contains { FileManager.default.isExecutableFile(atPath: $0) }
            ? "claude" : "apple"
    }

    private init() {
        let cfg = Self.read(path)
        // Matches config.py's default: on, unless this Mac says otherwise.
        voiceProfiles = (cfg["voice_profiles"] as? Bool) ?? true
        if let existing = cfg["summary_engine"] as? String {
            summaryEngine = existing
        } else {
            let picked = Self.recommendedEngine
            summaryEngine = picked
            write("summary_engine", picked)
        }
    }

    private static func read(_ url: URL) -> [String: Any] {
        guard let data = try? Data(contentsOf: url),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return [:] }
        return obj
    }

    /// Read-modify-write so keys the engine owns are never clobbered.
    private func write(_ key: String, _ value: Any) {
        var cfg = Self.read(path)
        cfg[key] = value
        guard let data = try? JSONSerialization.data(withJSONObject: cfg,
                                                     options: [.prettyPrinted, .sortedKeys])
        else { return }
        try? FileManager.default.createDirectory(
            at: path.deletingLastPathComponent(), withIntermediateDirectories: true)
        try? data.write(to: path, options: .atomic)
    }
}

struct SettingsView: View {
    @StateObject private var settings = Settings.shared
    @State private var appleReady: Bool?
    @State private var appleMessage: String?
    @State private var claudeFound = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 0) {
                intelligence
                Rectangle().fill(MS.hairline).frame(height: 1)
                    .padding(.vertical, 26)
                VoicesSection(settings: settings)
            }
            .padding(.horizontal, 34)
            .padding(.bottom, 30)
        }
        .frame(width: 520, height: 560)
        .background(MS.content)
        .task {
            let home = NSHomeDirectory()
            claudeFound = [
                "/opt/homebrew/bin/claude", "/usr/local/bin/claude",
                "\(home)/.claude/local/claude", "\(home)/.local/bin/claude",
            ].contains { FileManager.default.isExecutableFile(atPath: $0) }
            if let status = try? await API.get("api/llm/status", as: LLMStatus.self) {
                appleReady = status.available ?? false
                appleMessage = status.message
            }
        }
    }

    private var intelligence: some View {
        VStack(alignment: .leading, spacing: 0) {
            Text("INTELLIGENCE")
                .font(MSFont.kicker)
                .kerning(0.55)
                .foregroundStyle(MS.ink3)
                .padding(.top, 26)

            Text("Who writes your summaries and answers")
                .font(.system(size: 20, weight: .semibold, design: .serif))
                .foregroundStyle(MS.ink)
                .padding(.top, 8)

            VStack(spacing: 10) {
                EngineOption(
                    id: "apple",
                    title: "Apple Intelligence",
                    detail: appleReady == false
                        ? (appleMessage ?? "Not available on this Mac")
                        : "Built in, on-device and fast — usually under a minute, with nothing to install. Paraphrases more loosely than Claude on long meetings.",
                    available: appleReady ?? true,
                    recommended: !claudeFound,
                    selected: settings.summaryEngine == "apple") {
                        settings.summaryEngine = "apple"
                    }
                EngineOption(
                    id: "claude",
                    title: "Claude",
                    detail: claudeFound
                        ? "The most accurate summaries and answers, especially on long meetings. Takes a minute or two."
                        : "Optional — install Claude Code to enable this.",
                    available: claudeFound,
                    recommended: claudeFound,
                    selected: settings.summaryEngine == "claude") {
                        settings.summaryEngine = "claude"
                    }
            }
            .padding(.top, 18)

            Text("Applies to summaries and to Ask. Re-analyse any meeting to rewrite it with the engine you pick.")
                .font(MSFont.meta)
                .foregroundStyle(MS.ink3)
                .padding(.top, 16)
        }
    }
}

// MARK: - Voices

/// What this Mac has learned about people, and the switch that decides
/// whether it learns any more. The list is the receipt: every person the
/// app can recognize, how much of them it holds, and one click to forget.
private struct VoicesSection: View {
    @ObservedObject var settings: Settings

    @State private var profiles: [VoiceProfile] = []
    @State private var loaded = false
    @State private var confirmForgetAll = false

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Text("VOICES")
                .font(MSFont.kicker)
                .kerning(0.55)
                .foregroundStyle(MS.ink3)

            Text("Who this Mac recognises across meetings")
                .font(.system(size: 20, weight: .semibold, design: .serif))
                .foregroundStyle(MS.ink)
                .padding(.top, 8)

            SettingToggleRow(
                title: "Recognise people by voice",
                detail: "Naming a speaker saves a mathematical fingerprint of their voice on this Mac, never the audio, so later meetings can label them for you.",
                on: $settings.voiceProfiles)
                .padding(.top, 18)

            if !profiles.isEmpty {
                VStack(spacing: 8) {
                    ForEach(Array(profiles.enumerated()), id: \.element.id) { i, profile in
                        VoiceProfileRow(profile: profile,
                                        sharingName: sharingName(profile),
                                        position: position(of: i)) { forget(profile) }
                    }
                }
                .padding(.top, 12)

                HStack(spacing: 12) {
                    Button {
                        confirmForgetAll = true
                    } label: {
                        Text("Forget all voices")
                            .font(MSFont.meta)
                            .foregroundStyle(MS.ink2)
                    }
                    .buttonStyle(PressStyle())

                    if !settings.voiceProfiles {
                        Text("Kept on this Mac, but unused while recognition is off.")
                            .font(MSFont.meta)
                            .foregroundStyle(MS.ink3)
                    }
                }
                .padding(.top, 14)
            } else if loaded {
                Text(settings.voiceProfiles
                     ? "Nobody saved yet. Name a speaker in any meeting and this Mac will know them in the next one."
                     : "Nobody saved. While this is off, naming a speaker changes that meeting and nothing else.")
                    .font(MSFont.meta)
                    .foregroundStyle(MS.ink3)
                    .padding(.top, 14)
            }
        }
        .task { await reload() }
        .alert("Forget all voices?", isPresented: $confirmForgetAll) {
            Button("Forget All", role: .destructive) { forgetAll() }
            Button("Cancel", role: .cancel) { }
        } message: {
            Text("This Mac stops recognising anyone until you name a speaker again. The names already on your meetings don't change.")
        }
    }

    private func reload() async {
        profiles = ((try? await API.voiceProfiles()) ?? []).sorted {
            let byName = $0.name.localizedStandardCompare($1.name)
            // Two people can genuinely be called Jess, so a name is not a
            // sort key on its own. Oldest first inside a shared name, so the
            // numbering below stays put when a newer one is forgotten.
            return byName == .orderedSame
                ? ($0.updated ?? 0) < ($1.updated ?? 0)
                : byName == .orderedAscending
        }
        loaded = true
    }

    /// How many saved voices answer to this profile's name. More than one is
    /// normal, not a bug: the engine files people by voice and lets the label
    /// collide rather than blend two humans into one fingerprint.
    private func sharingName(_ profile: VoiceProfile) -> Int {
        profiles.filter {
            $0.name.localizedCaseInsensitiveCompare(profile.name) == .orderedSame
        }.count
    }

    /// This profile's place among the ones sharing its name (1-based).
    private func position(of index: Int) -> Int {
        let name = profiles[index].name
        return profiles[..<index].filter {
            $0.name.localizedCaseInsensitiveCompare(name) == .orderedSame
        }.count + 1
    }

    private func forget(_ profile: VoiceProfile) {
        withAnimation(Motion.exit) { profiles.removeAll { $0.id == profile.id } }
        Task {
            try? await API.deleteVoiceProfile(profile.id)
            await reload()
        }
    }

    private func forgetAll() {
        let doomed = profiles
        withAnimation(Motion.exit) { profiles = [] }
        Task {
            for profile in doomed {
                try? await API.deleteVoiceProfile(profile.id)
            }
            await reload()
        }
    }
}

/// EngineOption's anatomy with a switch where the radio was: same card,
/// same title-over-detail, for a setting that is on or off.
private struct SettingToggleRow: View {
    let title: String
    let detail: String
    @Binding var on: Bool

    var body: some View {
        HStack(alignment: .top, spacing: 11) {
            VStack(alignment: .leading, spacing: 3) {
                Text(title)
                    .font(.system(size: 14, weight: .semibold))
                    .foregroundStyle(MS.ink)
                Text(detail)
                    .font(MSFont.meta)
                    .foregroundStyle(MS.ink2)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 8)
            Toggle("", isOn: $on)
                .labelsHidden()
                .toggleStyle(.switch)
                .tint(MS.interactive)
        }
        .padding(13)
        .background {
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .fill(on ? MS.raised : MS.raised.opacity(0.5))
                .overlay(
                    RoundedRectangle(cornerRadius: 12, style: .continuous)
                        .strokeBorder(on ? MS.interactive.opacity(0.45) : MS.hairline,
                                      lineWidth: 1))
        }
    }
}

/// A tooltip that is simply absent when there is nothing to say — `.help("")`
/// still arms an empty one.
private struct OptionalHelp: ViewModifier {
    let text: String?

    func body(content: Content) -> some View {
        if let text { content.help(text) } else { content }
    }
}

/// One saved person: their name, how much of them is held, and the way out.
///
/// A name can appear twice, because two people can be called Jess and the
/// engine keeps them as two voices rather than average them into one. When it
/// does, the row has to say which of them it is — otherwise "Forget" is a
/// coin toss between two humans.
private struct VoiceProfileRow: View {
    let profile: VoiceProfile
    let sharingName: Int      // how many saved voices answer to this name
    let position: Int         // this one's place among them, 1-based
    let forget: () -> Void

    private var shared: Bool { sharingName > 1 }

    private var held: String {
        var parts: [String] = []
        if shared { parts.append("voice \(position) of \(sharingName)") }
        parts.append(profile.n_samples == 1 ? "1 meeting" : "\(profile.n_samples) meetings")
        // Wall-clock speech, straight from the engine. An older engine that
        // does not send it gets no speech figure at all rather than the
        // window-time number, which counts every second about twice.
        if let speech = profile.speech_seconds {
            let seconds = Int(speech.rounded())
            parts.append(seconds >= 60 ? "\(seconds / 60) min of speech"
                                       : "\(seconds)s of speech")
        }
        if shared, let updated = profile.updated {
            parts.append("named \(Self.day.string(from: Date(timeIntervalSince1970: updated)))")
        }
        return parts.joined(separator: " · ")
    }

    private static let day: DateFormatter = {
        let f = DateFormatter()
        f.setLocalizedDateFormatFromTemplate("d MMM")
        return f
    }()

    private var forgetHelp: String {
        shared
            ? "Forget this voice only. The other \(sharingName - 1) saved as \(profile.name) stay, and the names already on your meetings don't change."
            : "Forget \(profile.name)'s voice. The names already on your meetings don't change."
    }

    var body: some View {
        HStack(spacing: 11) {
            Image(systemName: "waveform.circle")
                .font(.system(size: 15))
                .foregroundStyle(MS.ink3)
            VStack(alignment: .leading, spacing: 2) {
                Text(profile.name)
                    .font(.system(size: 13.5, weight: .medium))
                    .foregroundStyle(MS.ink)
                Text(held)
                    .font(MSFont.meta)
                    .foregroundStyle(MS.ink3)
            }
            .modifier(OptionalHelp(text: shared
                ? "Two different people are saved as \(profile.name). Each keeps their own voice."
                : nil))
            Spacer(minLength: 8)
            Button(action: forget) {
                Text("Forget")
                    .font(MSFont.meta)
                    .foregroundStyle(MS.ink2)
                    .padding(.horizontal, 10)
                    .padding(.vertical, 4)
                    .background(MS.raised, in: .capsule)
            }
            .buttonStyle(PressStyle())
            .help(forgetHelp)
        }
        .padding(.horizontal, 13)
        .padding(.vertical, 10)
        .background {
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .fill(MS.raised.opacity(0.5))
                .overlay(
                    RoundedRectangle(cornerRadius: 12, style: .continuous)
                        .strokeBorder(MS.hairline, lineWidth: 1))
        }
    }
}

private struct EngineOption: View {
    let id: String
    let title: String
    let detail: String
    let available: Bool
    var recommended = false
    let selected: Bool
    let choose: () -> Void

    var body: some View {
        Button(action: choose) {
            HStack(alignment: .top, spacing: 11) {
                Image(systemName: selected ? "largecircle.fill.circle" : "circle")
                    .font(.system(size: 15))
                    .foregroundStyle(selected ? AnyShapeStyle(MS.interactive)
                                              : AnyShapeStyle(MS.ink4))
                VStack(alignment: .leading, spacing: 3) {
                    HStack(spacing: 7) {
                        Text(title)
                            .font(.system(size: 14, weight: .semibold))
                            .foregroundStyle(MS.ink)
                        if !available {
                            Text("unavailable")
                                .font(.system(size: 10, weight: .medium))
                                .foregroundStyle(MS.ink3)
                                .padding(.horizontal, 6)
                                .padding(.vertical, 2)
                                .background(MS.raised, in: .capsule)
                        } else if recommended {
                            Text("recommended")
                                .font(.system(size: 10, weight: .medium))
                                .foregroundStyle(MS.playhead)
                                .padding(.horizontal, 6)
                                .padding(.vertical, 2)
                                .background(MS.playheadFill.opacity(0.14), in: .capsule)
                        }
                    }
                    Text(detail)
                        .font(MSFont.meta)
                        .foregroundStyle(MS.ink2)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer()
            }
            .padding(13)
            .background {
                RoundedRectangle(cornerRadius: 12, style: .continuous)
                    .fill(selected ? MS.raised : MS.raised.opacity(0.5))
                    .overlay(
                        RoundedRectangle(cornerRadius: 12, style: .continuous)
                            .strokeBorder(selected ? MS.interactive.opacity(0.45) : MS.hairline,
                                          lineWidth: 1))
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(PressStyle())
        .opacity(available ? 1 : 0.55)
    }
}
