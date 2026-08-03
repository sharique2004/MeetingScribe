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

    private let path = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent(".meetingscribe/config.json")

    private init() {
        let cfg = Self.read(path)
        // Apple Intelligence is the default: on-device, fast, and it means a
        // fresh install needs nothing installed to write summaries or answer
        // questions. The engine's own default is Claude, so an absent key is
        // written through rather than merely assumed.
        if let existing = cfg["summary_engine"] as? String {
            summaryEngine = existing
        } else {
            summaryEngine = "apple"
            write("summary_engine", "apple")
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
                        : "Built in, on-device, and fast — usually under a minute. Nothing to install, nothing leaves this Mac.",
                    available: appleReady ?? true,
                    recommended: true,
                    selected: settings.summaryEngine == "apple") {
                        settings.summaryEngine = "apple"
                    }
                EngineOption(
                    id: "claude",
                    title: "Claude",
                    detail: claudeFound
                        ? "Richer writing and reads the whole of a long meeting, but takes a minute or two and needs Claude Code installed."
                        : "Optional — install Claude Code to enable this.",
                    available: claudeFound,
                    recommended: false,
                    selected: settings.summaryEngine == "claude") {
                        settings.summaryEngine = "claude"
                    }
            }
            .padding(.top, 18)

            Text("Applies to summaries and to Ask. Re-analyse any meeting to rewrite it with the engine you pick.")
                .font(MSFont.meta)
                .foregroundStyle(MS.ink3)
                .padding(.top, 16)

            Spacer()
        }
        .padding(.horizontal, 34)
        .frame(width: 520, height: 380)
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
