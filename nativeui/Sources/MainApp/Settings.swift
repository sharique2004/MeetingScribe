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

    /// Which speech recogniser writes the transcript. "auto" resolves to the
    /// most accurate engine this Mac can run (Parakeet, then Apple Speech,
    /// then Whisper): see pipeline.pick_backend.
    ///
    /// APPLIES FROM THE NEXT RECORDING, or from Reprocess audio on a meeting
    /// that already exists. NOT from Re-analyse, which only rewrites the
    /// summary from the transcript already on disk and never touches the
    /// recogniser. Settings said "the next recording or Re-analyse" for
    /// months, which is why the copy below now names Reprocess.
    @Published var whisperBackend: String {
        didSet { write("whisper_backend", whisperBackend) }
    }

    /// The language the recognisers are told to expect. "auto" is stored as
    /// JSON null, which is config.py's default.
    ///
    /// This existed in config.json from the beginning, the old web UI asked
    /// for it on every recording, and the native app never surfaced it at
    /// all — so every fresh install has transcribed every meeting as English,
    /// silently, whatever was actually spoken.
    @Published var language: String {
        didSet { write("language", language == "auto" ? NSNull() : language) }
    }

    /// The languages the pipeline is actually wired for. Deliberately the
    /// same list the web recorder offered rather than everything Whisper can
    /// attempt, because these are the ones the engine routes correctly:
    /// pipeline.pick_backend sends anything outside Parakeet's coverage
    /// (hi/ja/ko/zh/ar and the rest) to Apple Speech or Whisper instead.
    static let languages: [(code: String, name: String)] = [
        ("auto", "Auto-detect"), ("en", "English"), ("hi", "Hindi"),
        ("es", "Spanish"), ("fr", "French"), ("de", "German"),
        ("it", "Italian"), ("pt", "Portuguese"), ("ja", "Japanese"),
        ("ko", "Korean"), ("zh", "Chinese"), ("ar", "Arabic"),
        ("ru", "Russian"),
    ]

    /// Whether naming a speaker also teaches this Mac their voice. Same
    /// config.json, read fresh by the engine on every rename, so the switch
    /// applies to the very next one.
    @Published var voiceProfiles: Bool {
        didSet { write("voice_profiles", voiceProfiles) }
    }

    /// Whether a meeting summarises itself the moment it finishes
    /// transcribing. Read fresh by app._auto_summarize at the end of every
    /// transcription, so the switch applies to the very next meeting.
    @Published var autoSummarize: Bool {
        didSet { write("auto_summarize", autoSummarize) }
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
        // Matches config.py's defaults: on, unless this Mac says otherwise.
        voiceProfiles = (cfg["voice_profiles"] as? Bool) ?? true
        autoSummarize = (cfg["auto_summarize"] as? Bool) ?? true
        whisperBackend = (cfg["whisper_backend"] as? String) ?? "auto"
        // null, absent, or a code this build doesn't offer all mean the same
        // thing to the user: nothing is being forced.
        let stored = (cfg["language"] as? String)?
            .trimmingCharacters(in: .whitespaces).lowercased() ?? ""
        language = Self.languages.contains { $0.code == stored } ? stored : "auto"
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

/// One AI CLI as the engine probe reports it (`GET /api/cli-engines`).
struct CLIEngine: Decodable, Identifiable {
    let id: String
    let label: String
    let installed: Bool
}

private struct CLIEngines: Decodable {
    let engines: [CLIEngine]
}

struct SettingsView: View {
    @StateObject private var settings = Settings.shared
    @State private var appleReady: Bool?
    @State private var appleMessage: String?
    // The engine probe is the backend's (ai_cli.detect_all) so the path list
    // can't drift per front end; until it answers, Claude is assumed present
    // so the card the user most likely wants isn't greyed out by a race.
    @State private var clis: [CLIEngine] = [
        CLIEngine(id: "claude", label: "Claude", installed: true)
    ]

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 0) {
                intelligence
                Rectangle().fill(MS.hairline).frame(height: 1)
                    .padding(.vertical, 26)
                transcription
                Rectangle().fill(MS.hairline).frame(height: 1)
                    .padding(.vertical, 26)
                VoicesSection(settings: settings)
            }
            .padding(.horizontal, 34)
            .padding(.bottom, 30)
        }
        // Was a hard 520×680. Every string in this window wraps, and at a
        // larger text size a fixed height clips the last section off the
        // bottom with no way to reach it. Let the window grow instead.
        .frame(minWidth: 520, idealWidth: 520, maxWidth: 640,
               minHeight: 420, idealHeight: 680, maxHeight: 900)
        .background(MS.content)
        .task {
            if let probe = try? await API.get("api/cli-engines", as: CLIEngines.self) {
                clis = probe.engines
            }
            if let status = try? await API.get("api/llm/status", as: LLMStatus.self) {
                appleReady = status.available ?? false
                appleMessage = status.message
            }
        }
    }

    private var claudeFound: Bool {
        clis.first(where: { $0.id == "claude" })?.installed ?? false
    }

    private func cliDetail(_ cli: CLIEngine) -> String {
        if !cli.installed {
            return "Not on this Mac. Install the \(cli.label) CLI and sign in, then come back."
        }
        switch cli.id {
        case "claude":
            return "The most accurate summaries and answers, especially on long meetings. Takes a minute or two."
        case "gemini":
            return "Uses your Gemini CLI account. Note: Google's free tier may use prompts to improve their models."
        case "copilot":
            return "Uses your GitHub Copilot plan, and each summary or answer spends premium requests."
        default:
            return "Uses your \(cli.label) account and its default model."
        }
    }

    /// The engine the config names is not on this Mac.
    ///
    /// The old picker let it be chosen anyway and said nothing afterwards, so
    /// the first sign of trouble was a summary that quietly came out of a
    /// different model. The engine does fall back on its own; the point of
    /// this line is that the user finds out here rather than there.
    private var chosenEngineMissing: String? {
        let id = settings.summaryEngine
        if id == "apple" {
            guard appleReady == false else { return nil }
            return appleMessage ?? "Apple Intelligence isn't available on this Mac."
        }
        guard let cli = clis.first(where: { $0.id == id }) else { return nil }
        guard !cli.installed else { return nil }
        return "\(cli.label) isn't installed on this Mac, so summaries and Ask use Apple Intelligence until it is."
    }

    private var transcription: some View {
        VStack(alignment: .leading, spacing: 0) {
            Text("TRANSCRIPTION")
                .font(MSFont.kicker)
                .kerning(0.55)
                .foregroundStyle(MS.ink3)
                .accessibilityAddTraits(.isHeader)

            Text("Which recogniser writes the words")
                .font(.system(size: 20, weight: .semibold, design: .serif))
                .foregroundStyle(MS.ink)
                .padding(.top, 8)

            VStack(spacing: 10) {
                EngineOption(
                    id: "auto",
                    title: "Best available",
                    detail: "Picks the most accurate engine this Mac can run: Parakeet for meeting audio, falling back to Apple Speech, then Whisper. Fully on-device either way.",
                    available: true,
                    recommended: true,
                    selected: settings.whisperBackend == "auto") {
                        settings.whisperBackend = "auto"
                    }
                EngineOption(
                    id: "parakeet",
                    title: "Parakeet",
                    detail: "The most accurate on natural, overlapping meeting speech, and it almost never invents words over silence. English and 24 European languages.",
                    available: true,
                    selected: settings.whisperBackend == "parakeet") {
                        settings.whisperBackend = "parakeet"
                    }
                EngineOption(
                    id: "apple",
                    title: "Apple Speech",
                    detail: "The macOS recogniser on the Neural Engine: fastest and coolest, but tuned for clean dictation, so it mishears more on meeting audio.",
                    available: true,
                    selected: settings.whisperBackend == "apple") {
                        settings.whisperBackend = "apple"
                    }
                EngineOption(
                    id: "mlx",
                    title: "Whisper",
                    detail: "OpenAI's large-v3-turbo on the Apple GPU. Nearly 100 languages, and the one engine that can be biased towards your vocabulary and attendee names.",
                    available: true,
                    selected: settings.whisperBackend == "mlx") {
                        settings.whisperBackend = "mlx"
                    }
            }
            .padding(.top, 18)

            // The setting that has been in config.json since the first
            // release and in no user interface at all. Without it every
            // recogniser is told to expect English, so a meeting held in Hindi
            // came back as English-shaped nonsense and nothing in the app
            // explained why.
            SettingPickerRow(
                title: "Language",
                detail: languageDetail,
                options: Settings.languages,
                selection: $settings.language)
                .padding(.top, 10)

            Text("Everything here runs on this Mac, and the audio never leaves it. A change applies to your next recording. To apply it to a meeting you already have, open that meeting and use Reprocess audio (⌥⌘R), which transcribes the saved audio again with today's settings. Re-analyse only rewrites the summary, so it will not change the words or the language.")
                .font(MSFont.meta)
                .foregroundStyle(MS.ink3)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.top, 16)
        }
    }

    /// Auto-detect is only genuinely automatic on one of the four engines, and
    /// saying otherwise is how a Hindi meeting comes back in English.
    private var languageDetail: String {
        settings.language == "auto"
            ? "Only Whisper truly detects the language on its own. Parakeet and Apple Speech read an unset language as English, so pick yours here if your meetings are not in English."
            : "Locks speech recognition to this language, which is more accurate than letting the engine guess. Live captions need a chosen language too."
    }

    /// What auto-summarising actually costs, which is not the same sentence on
    /// every engine: on-device it is a minute of this Mac, on a cloud CLI it is
    /// a call per meeting, and a user turning it on deserves to know which.
    private var autoSummarizeDetail: String {
        let base = "As soon as a meeting finishes transcribing it writes its own summary, "
        if settings.summaryEngine == "apple" {
            return base + "on this Mac, costing nothing but a minute of it. Off, the summary waits for you to ask."
        }
        let name = clis.first { $0.id == settings.summaryEngine }?.label
            ?? settings.summaryEngine.capitalized
        return base + "which is one \(name) call per meeting. Off, the summary waits for you to ask."
    }

    private var intelligence: some View {
        VStack(alignment: .leading, spacing: 0) {
            Text("INTELLIGENCE")
                .font(MSFont.kicker)
                .kerning(0.55)
                .foregroundStyle(MS.ink3)
                .padding(.top, 26)
                .accessibilityAddTraits(.isHeader)

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
                        : "Built in, on-device and fast, usually under a minute, with nothing to install. Paraphrases more loosely than a frontier model on long meetings.",
                    available: appleReady ?? true,
                    recommended: !claudeFound,
                    selected: settings.summaryEngine == "apple") {
                        settings.summaryEngine = "apple"
                    }
                ForEach(clis) { cli in
                    EngineOption(
                        id: cli.id,
                        title: cli.label,
                        detail: cliDetail(cli),
                        available: cli.installed,
                        recommended: cli.id == "claude" && cli.installed,
                        selected: settings.summaryEngine == cli.id) {
                            settings.summaryEngine = cli.id
                        }
                }
            }
            .padding(.top, 18)

            if let missing = chosenEngineMissing {
                NoticeBand(icon: "exclamationmark.triangle", text: missing)
                    .padding(.top, 12)
            }

            SettingToggleRow(
                title: "Summarise every meeting automatically",
                detail: autoSummarizeDetail,
                on: $settings.autoSummarize)
                .padding(.top, 14)

            Text("Applies to summaries and to Ask. Cloud engines receive the transcript text, never audio, which stays on this Mac. If the engine you pick is ever unavailable, summaries fall back to Apple Intelligence automatically. Re-analyse any meeting to rewrite its summary with the engine you pick.")
                .font(MSFont.meta)
                .foregroundStyle(MS.ink3)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.top, 16)
        }
    }
}

/// SettingToggleRow's anatomy with a menu where the switch was: same card,
/// same title-over-detail, for a setting that is one of a list.
private struct SettingPickerRow: View {
    let title: String
    let detail: String
    let options: [(code: String, name: String)]
    @Binding var selection: String

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
            // The label is hidden, not absent: labelsHidden() keeps it for
            // VoiceOver, which is the whole difference between "Language,
            // pop-up button" and an unnamed control.
            Picker(title, selection: $selection) {
                ForEach(options, id: \.code) { option in
                    Text(option.name).tag(option.code)
                }
            }
            .labelsHidden()
            .pickerStyle(.menu)
            .tint(MS.interactive)
            .fixedSize()
        }
        .padding(13)
        .background {
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .fill(MS.raised)
                .overlay(
                    RoundedRectangle(cornerRadius: 12, style: .continuous)
                        .strokeBorder(MS.hairline, lineWidth: 1))
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
                .accessibilityAddTraits(.isHeader)

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
        msWithAnimation(Motion.exit) { profiles.removeAll { $0.id == profile.id } }
        Task {
            try? await API.deleteVoiceProfile(profile.id)
            await reload()
        }
    }

    private func forgetAll() {
        let doomed = profiles
        msWithAnimation(Motion.exit) { profiles = [] }
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
            // The title, not "": labelsHidden() hides it visually and keeps
            // it for VoiceOver, which otherwise reads this as a bare switch
            // with no idea what it switches.
            Toggle(title, isOn: $on)
                .labelsHidden()
                .toggleStyle(.switch)
                .tint(MS.interactive)
                .accessibilityHint(detail)
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
                .msDecorative()
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
            // Six rows all saying "Forget" is six identical VoiceOver stops,
            // and the button that deletes the wrong person's fingerprint
            // sounds exactly like the right one.
            .accessibilityLabel(shared
                ? "Forget \(profile.name), voice \(position) of \(sharingName)"
                : "Forget \(profile.name)'s voice")
            .accessibilityHint(forgetHelp)
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
        // A greyed-out card that still takes the click is worse than no card
        // at all: the app accepted an engine it did not have, wrote it to
        // config.json, and the user found out at the end of the next meeting.
        // The badge said "unavailable" the whole time.
        .disabled(!available)
        .modifier(OptionalHelp(text: available
            ? nil : "\(title) isn't available on this Mac yet."))
        .accessibilityLabel(title)
        .accessibilityValue(available ? "" : "Unavailable")
        .accessibilityHint(detail)
        .accessibilityAddTraits(selected ? [.isSelected] : [])
    }
}
