// First run: six quiet steps from a fresh download to full capacity —
// welcome, the engine (with one-time setup if the Python environment is
// missing), microphone, system audio (proven with a real 3-second test),
// calendar, and AI summaries (Claude if present, Apple Intelligence
// otherwise). Every permission is asked in context, never in a wall.
import SwiftUI
import AVFoundation

struct OnboardingFlow: View {
    @Binding var presented: Bool
    @EnvironmentObject var center: RecorderCenter
    @StateObject private var engine = EngineManager.shared
    @State private var step = 0

    private let stepCount = 6

    var body: some View {
        VStack(spacing: 0) {
            Group {
                switch step {
                case 0: WelcomeStep(next: advance)
                case 1: EngineStep(engine: engine, next: advance)
                case 2: MicrophoneStep(next: advance)
                case 3: SystemAudioStep(center: center, next: advance)
                case 4: CalendarStep(next: advance)
                default: IntelligenceStep(finish: finish)
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .transition(.asymmetric(
                insertion: .offset(x: 24).combined(with: .opacity),
                removal: .offset(x: -24).combined(with: .opacity)))
            .id(step)

            HStack(spacing: 6) {
                ForEach(0..<stepCount, id: \.self) { i in
                    Circle()
                        .fill(i == step ? MS.interactive : MS.ink4.opacity(0.5))
                        .frame(width: 6, height: 6)
                }
            }
            .padding(.bottom, 22)
        }
        .frame(width: 600, height: 560)
        .background(MS.content)
        .animation(Motion.enter, value: step)
        .interactiveDismissDisabled()
    }

    private func advance() {
        withAnimation(Motion.enter) { step += 1 }
    }

    private func finish() {
        Task { await API.post("api/onboarding/done") }
        UserDefaults.standard.set(true, forKey: "ms.onboarded.v1")
        presented = false
    }
}

// MARK: - Shared step chrome

private struct StepPage<Content: View>: View {
    let kicker: String
    let title: String
    let subtitle: String
    @ViewBuilder let content: Content

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Text(kicker.uppercased())
                .font(MSFont.kicker)
                .kerning(0.55)
                .foregroundStyle(MS.ink3)
                .padding(.top, 52)
            Text(title)
                .font(.system(size: 24, weight: .semibold, design: .serif))
                .foregroundStyle(MS.ink)
                .padding(.top, 10)
            Text(subtitle)
                .font(MSFont.body)
                .lineSpacing(6)
                .foregroundStyle(MS.ink2)
                .padding(.top, 8)
            content
                .padding(.top, 26)
            Spacer()
        }
        .padding(.horizontal, 56)
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

private struct ContinueButton: View {
    var label = "Continue"
    var enabled = true
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Text(label)
                .font(.system(size: 13, weight: .semibold))
                .foregroundStyle(.black.opacity(0.85))
                .padding(.horizontal, 22)
                .padding(.vertical, 8)
        }
        .buttonStyle(PressStyle())
        .background(enabled ? MS.playheadFill : MS.ink4, in: .capsule)
        .disabled(!enabled)
    }
}

private struct CheckRow: View {
    let done: Bool
    let text: String

    var body: some View {
        HStack(spacing: 9) {
            Image(systemName: done ? "checkmark.circle.fill" : "circle")
                .font(.system(size: 14))
                .foregroundStyle(done ? AnyShapeStyle(MS.playheadFill) : AnyShapeStyle(MS.ink4))
            Text(text)
                .font(MSFont.body)
                .foregroundStyle(done ? MS.ink : MS.ink2)
        }
    }
}

// MARK: - Steps

private struct WelcomeStep: View {
    let next: () -> Void

    var body: some View {
        StepPage(
            kicker: "Welcome",
            title: "Meetings, remembered.",
            subtitle: "MeetingScribe records, transcribes and summarises your meetings — entirely on this Mac. No bots join your calls, and nothing you say ever leaves this machine."
        ) {
            VStack(alignment: .leading, spacing: 12) {
                CheckRow(done: true, text: "Bot-free capture of every call app")
                CheckRow(done: true, text: "Live captions while you meet")
                CheckRow(done: true, text: "Summaries, action items and answers — private by architecture")
                ContinueButton(label: "Set up", action: next)
                    .padding(.top, 22)
            }
        }
    }
}

private struct EngineStep: View {
    @ObservedObject var engine: EngineManager
    let next: () -> Void
    @State private var bootstrapping = false
    @State private var log: [String] = []

    var body: some View {
        StepPage(
            kicker: "Step 1 of 5",
            title: "Starting the engine.",
            subtitle: "The transcription engine runs privately on this Mac. First launch sets up its environment once."
        ) {
            VStack(alignment: .leading, spacing: 14) {
                switch engine.state {
                case .running:
                    CheckRow(done: true, text: "Engine running")
                    ContinueButton(action: next).padding(.top, 12)
                case .checking, .starting:
                    HStack(spacing: 8) {
                        ProgressView().controlSize(.small)
                        Text(bootstrapping ? "Setting up — a few minutes, once ever…"
                                           : "Starting…")
                            .font(MSFont.body)
                            .foregroundStyle(MS.ink2)
                    }
                case .failed(let message):
                    if !engine.venvExists && !bootstrapping {
                        Text("One-time setup is needed (about 2 GB of speech models and libraries).")
                            .font(MSFont.body)
                            .foregroundStyle(MS.ink2)
                        ContinueButton(label: "Run setup") { runBootstrap() }
                    } else if bootstrapping {
                        HStack(spacing: 8) {
                            ProgressView().controlSize(.small)
                            Text("Setting up…").font(MSFont.body).foregroundStyle(MS.ink2)
                        }
                    } else {
                        Text(message)
                            .font(MSFont.body)
                            .foregroundStyle(MS.ink2)
                        ContinueButton(label: "Try again") {
                            Task { await engine.ensureRunning() }
                        }
                    }
                }

                if !log.isEmpty {
                    ScrollViewReader { proxy in
                        ScrollView {
                            VStack(alignment: .leading, spacing: 2) {
                                ForEach(Array(log.suffix(200).enumerated()), id: \.offset) { i, line in
                                    Text(line)
                                        .font(.system(size: 10.5, design: .monospaced))
                                        .foregroundStyle(MS.ink3)
                                        .id(i)
                                }
                            }
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(8)
                        }
                        .frame(height: 160)
                        .background(MS.sunken, in: .rect(cornerRadius: 8))
                        .onChange(of: log.count) {
                            proxy.scrollTo(log.suffix(200).count - 1, anchor: .bottom)
                        }
                    }
                }
            }
        }
        .task {
            if engine.state == .checking { await engine.ensureRunning() }
        }
    }

    private func runBootstrap() {
        bootstrapping = true
        engine.runBootstrap { line in
            log.append(line)
        } onDone: { ok in
            bootstrapping = false
            if ok {
                Task { await engine.ensureRunning() }
            } else {
                log.append("Setup failed — scroll up for the reason, then try again.")
            }
        }
    }
}

private struct MicrophoneStep: View {
    let next: () -> Void
    @State private var status = AVCaptureDevice.authorizationStatus(for: .audio)

    var body: some View {
        StepPage(
            kicker: "Step 2 of 5",
            title: "Your microphone.",
            subtitle: "Your side of every meeting comes from the mic. Audio is written straight to disk on this Mac — nowhere else."
        ) {
            VStack(alignment: .leading, spacing: 14) {
                switch status {
                case .authorized:
                    CheckRow(done: true, text: "Microphone access granted")
                    ContinueButton(action: next).padding(.top, 12)
                case .denied, .restricted:
                    Text("Microphone access is off. Turn it on in System Settings → Privacy & Security → Microphone, then come back.")
                        .font(MSFont.body)
                        .foregroundStyle(MS.ink2)
                    HStack(spacing: 10) {
                        ContinueButton(label: "Open System Settings") {
                            NSWorkspace.shared.open(URL(string:
                                "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone")!)
                        }
                        Button("Check again") {
                            status = AVCaptureDevice.authorizationStatus(for: .audio)
                        }
                        .buttonStyle(.plain)
                        .font(MSFont.chrome)
                        .foregroundStyle(MS.ink2)
                    }
                default:
                    ContinueButton(label: "Allow microphone") {
                        AVCaptureDevice.requestAccess(for: .audio) { _ in
                            DispatchQueue.main.async {
                                status = AVCaptureDevice.authorizationStatus(for: .audio)
                            }
                        }
                    }
                }
            }
        }
    }
}

private struct SystemAudioStep: View {
    @ObservedObject var center: RecorderCenter
    let next: () -> Void
    @State private var phase = 0   // 0 idle, 1 testing, 2 done

    var body: some View {
        StepPage(
            kicker: "Step 3 of 5",
            title: "The other side of the call.",
            subtitle: "To hear everyone else, macOS asks once for System Audio access. We'll run a three-second test recording — approve the prompt when it appears, and it never asks again."
        ) {
            VStack(alignment: .leading, spacing: 14) {
                switch phase {
                case 2:
                    CheckRow(done: true, text: "System audio captured — you're fully wired")
                    ContinueButton(action: next).padding(.top, 12)
                case 1:
                    HStack(spacing: 8) {
                        ProgressView().controlSize(.small)
                        Text("Recording three seconds — approve the system prompt if it appears…")
                            .font(MSFont.body)
                            .foregroundStyle(MS.ink2)
                    }
                default:
                    ContinueButton(label: "Run the test") { runTest() }
                    Button("Skip — ask me on my first real recording") { next() }
                        .buttonStyle(.plain)
                        .font(MSFont.meta)
                        .foregroundStyle(MS.ink3)
                }
            }
        }
    }

    private func runTest() {
        phase = 1
        Task {
            await API.post("api/record/start", body: ["title": "Setup test"])
            try? await Task.sleep(nanoseconds: 3_200_000_000)
            // The id comes from the recorder itself, so the right meeting is
            // removed even if ids drifted.
            let id = (try? await API.recorderStatus())?.meeting_id
            await API.post("api/record/stop")
            phase = 2
            if let id {
                Task.detached {
                    for _ in 0..<20 {
                        let (ok, _) = await API.deleteMeeting(id)
                        if ok { break }
                        try? await Task.sleep(nanoseconds: 4_000_000_000)
                    }
                }
            }
        }
    }
}

private struct CalendarStep: View {
    let next: () -> Void
    @State private var connected = false
    @State private var asked = false

    var body: some View {
        StepPage(
            kicker: "Step 4 of 5",
            title: "Meetings, by name.",
            subtitle: "With calendar access, recordings name themselves after the event you're in, and MeetingScribe nudges you to record when a meeting starts. Optional — everything works without it."
        ) {
            VStack(alignment: .leading, spacing: 14) {
                if connected {
                    CheckRow(done: true, text: "Calendar connected")
                    ContinueButton(action: next).padding(.top, 12)
                } else {
                    HStack(spacing: 12) {
                        ContinueButton(label: asked ? "Checking…" : "Connect calendar",
                                       enabled: !asked) { connect() }
                        Button("Skip for now") { next() }
                            .buttonStyle(.plain)
                            .font(MSFont.meta)
                            .foregroundStyle(MS.ink3)
                    }
                }
            }
        }
    }

    private func connect() {
        asked = true
        Task {
            // Touching the calendar endpoint spins up the calendar helper,
            // which raises the system permission prompt on first use.
            _ = await API.calendarToday()
            try? await Task.sleep(nanoseconds: 1_500_000_000)
            _ = await API.calendarToday()
            connected = true
            asked = false
        }
    }
}

private struct IntelligenceStep: View {
    let finish: () -> Void
    @State private var claudeFound = false
    @State private var appleReady = false
    @State private var appleMessage: String?
    @State private var checked = false

    var body: some View {
        StepPage(
            kicker: "Step 5 of 5",
            title: "Who writes your summaries.",
            subtitle: "After each meeting, MeetingScribe writes the recap, to-dos and a follow-up email — and answers questions about it. Apple Intelligence does this on-device, so nothing needs installing."
        ) {
            VStack(alignment: .leading, spacing: 14) {
                if checked {
                    CheckRow(done: appleReady,
                             text: appleReady
                             ? "Apple Intelligence ready — nothing to install"
                             : (appleMessage ?? "Apple Intelligence unavailable on this Mac"))
                    CheckRow(done: claudeFound,
                             text: claudeFound
                             ? "Claude also found — switch to it in Settings for richer writing"
                             : "Claude Code not installed — optional, not needed")
                    if !appleReady && !claudeFound {
                        Text("Recording and transcription work fully today; summaries switch on the moment either becomes available.")
                            .font(MSFont.meta)
                            .foregroundStyle(MS.ink3)
                    }
                    ContinueButton(label: "Start using MeetingScribe", action: finish)
                        .padding(.top, 16)
                } else {
                    HStack(spacing: 8) {
                        ProgressView().controlSize(.small)
                        Text("Checking what's available…")
                            .font(MSFont.body)
                            .foregroundStyle(MS.ink2)
                    }
                }
            }
        }
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
            checked = true
        }
    }
}

struct LLMStatus: Decodable {
    let available: Bool?
    let message: String?
}
