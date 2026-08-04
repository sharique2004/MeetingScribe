// First run: seven quiet steps from a fresh download to full capacity —
// welcome, the engine (with one-time setup if the Python environment is
// missing), the speech models (the 2.4 GB download that used to ambush the
// first real meeting), microphone, system audio (proven with a real 3-second
// test), calendar, and AI summaries (Claude if present, Apple Intelligence
// otherwise). Every permission is asked in context, never in a wall.
//
// EVERY STEP HAS A WAY OUT. This is a modal sheet over an app that cannot be
// used behind it, so a step that can only be satisfied by something outside
// the app — a permission the user denied, an engine that will not start — is
// a trap unless it also offers a door. The door is always the same one: leave
// setup, keep whatever was achieved, and let the app say what is missing when
// it matters.
import SwiftUI
import AVFoundation

struct OnboardingFlow: View {
    @Binding var presented: Bool
    @EnvironmentObject var center: RecorderCenter
    @StateObject private var engine = EngineManager.shared
    @State private var step = 0

    private let stepCount = 7

    var body: some View {
        VStack(spacing: 0) {
            Group {
                switch step {
                case 0: WelcomeStep(next: advance)
                case 1: EngineStep(engine: engine, next: advance)
                case 2: ModelsStep(next: advance)
                case 3: MicrophoneStep(next: advance)
                case 4: SystemAudioStep(center: center, next: advance)
                case 5: CalendarStep(next: advance)
                default: IntelligenceStep(finish: { finish() })
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .transition(.asymmetric(
                insertion: .offset(x: 24).combined(with: .opacity),
                removal: .offset(x: -24).combined(with: .opacity)))
            .id(step)

            // The dots, and the door. Nothing in setup is compulsory, and
            // "Set up later" is the honest name for it: the app works with
            // whatever was granted and asks again where it needs to.
            ZStack {
                HStack(spacing: 6) {
                    ForEach(0..<stepCount, id: \.self) { i in
                        Circle()
                            .fill(i == step ? MS.interactive : MS.ink4.opacity(0.5))
                            .frame(width: 6, height: 6)
                    }
                }
                HStack {
                    Spacer()
                    if step < stepCount - 1 {
                        Button("Set up later") { finish(completed: false) }
                            .buttonStyle(.plain)
                            .font(MSFont.meta)
                            .foregroundStyle(MS.ink3)
                            .keyboardShortcut(.cancelAction)
                    }
                }
                .padding(.trailing, 24)
            }
            .padding(.bottom, 22)
        }
        .frame(width: 600, height: 560)
        .background(MS.content)
        .animation(Motion.enter, value: step)
    }

    private func advance() {
        withAnimation(Motion.enter) { step += 1 }
    }

    /// Leave setup. The flag is written either way: an onboarding the user
    /// walked out of must not reappear on every launch, and the engine is
    /// only told the flow was seen when it was actually seen through.
    private func finish(completed: Bool = true) {
        if completed { Task { await API.post("api/onboarding/done") } }
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
            subtitle: "MeetingScribe records, transcribes and summarises your meetings, entirely on this Mac. No bots join your calls, and nothing you say ever leaves this machine."
        ) {
            VStack(alignment: .leading, spacing: 12) {
                CheckRow(done: true, text: "Bot-free capture of every call app")
                CheckRow(done: true, text: "Live captions while you meet")
                CheckRow(done: true, text: "Summaries, action items and answers, private by architecture")
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
            kicker: "Step 1 of 6",
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
                        Text(bootstrapping ? "Setting up, a few minutes, once ever…"
                                           : "Starting…")
                            .font(MSFont.body)
                            .foregroundStyle(MS.ink2)
                    }
                case .failed(let message):
                    if !engine.venvExists && !bootstrapping {
                        // Named honestly: bootstrap.sh installs Python
                        // libraries and no models at all. Promising "speech
                        // models" here is what let the real 2.4 GB model
                        // download turn up unannounced during a meeting.
                        Text("One-time setup is needed. It installs about 2 GB of libraries on this Mac. The speech models come next.")
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
                        // A setup that cannot start the engine is not a setup
                        // the user can finish, and this used to be the end of
                        // the road: one button that had already failed, and a
                        // sheet with no way past it. The rest of the flow
                        // copes with an engine that isn't answering.
                        HStack(spacing: 12) {
                            ContinueButton(label: "Try again") {
                                Task { await engine.ensureRunning() }
                            }
                            SkipLink(label: "Carry on without it", action: next)
                        }
                        Text("The app will keep trying, and offers to restart the engine from the main window.")
                            .font(MSFont.meta)
                            .foregroundStyle(MS.ink3)
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
                log.append("Setup failed, scroll up for the reason, then try again.")
            }
        }
    }
}

/// The 2.4 GB that used to arrive uninvited.
///
/// A fresh install carries no speech models: Parakeet, the ECAPA speaker
/// embedder and the neural turn-placer were all fetched LAZILY, blocking and
/// silent, in the middle of the user's first real meeting. One static line of
/// text for a multi-gigabyte transfer is indistinguishable from a hang, which
/// is exactly how it was reported: "it just kept processing and never stopped".
///
/// This step moves that wait to the one place a wait is expected, shows it in
/// bytes, and is skippable in every state. Skipping changes nothing except
/// when the download happens: the lazy path is untouched and still works.
private struct ModelsStep: View {
    let next: () -> Void
    @State private var status: ModelStatus?
    @State private var starting = false
    /// Consecutive polls the engine didn't answer. The engine step can be
    /// skipped, and a step that can't reach the engine must say so and step
    /// aside rather than spin.
    @State private var silentProbes = 0

    var body: some View {
        StepPage(
            kicker: "Step 2 of 6",
            title: "The models, once.",
            subtitle: "Transcription happens on this Mac, so the speech models live here too. Fetching them now means your first meeting starts the moment you press record, instead of waiting on a download."
        ) {
            VStack(alignment: .leading, spacing: 14) {
                if let status {
                    body(for: status)
                } else if silentProbes >= 3 {
                    unreachable
                } else {
                    spinner("Checking what this Mac already has…")
                }
            }
        }
        .task { await watch() }
    }

    // MARK: States

    @ViewBuilder
    private func body(for status: ModelStatus) -> some View {
        switch status.state {
        case "checking":
            spinner(status.message ?? "Checking what this Mac already has…")
        case "downloading":
            downloading(status)
        case "ready", "done":
            CheckRow(done: true, text: status.state == "done"
                     ? "Models ready on this Mac"
                     : "Models already on this Mac, nothing to download")
            componentRows(status)
            ContinueButton(action: next).padding(.top, 12)
        case "error":
            Text(status.message ?? "The download didn't finish.")
                .font(MSFont.body)
                .foregroundStyle(MS.ink2)
            if let detail = status.error {
                Text(detail)
                    .font(MSFont.meta)
                    .foregroundStyle(MS.ink3)
                    .lineLimit(2)
            }
            HStack(spacing: 12) {
                ContinueButton(label: "Try again", enabled: !starting) { start() }
                SkipLink(label: "Skip, download on my first meeting", action: next)
            }
        default:  // "missing"
            componentRows(status)
            Text("It downloads once and stays on this Mac. You can skip and let your first meeting fetch it instead.")
                .font(MSFont.meta)
                .foregroundStyle(MS.ink3)
            HStack(spacing: 12) {
                ContinueButton(label: "Download \(modelSize(status.total_bytes)) now",
                               enabled: !starting) { start() }
                SkipLink(label: "Skip for now", action: next)
            }
        }
    }

    @ViewBuilder
    private func downloading(_ status: ModelStatus) -> some View {
        DownloadBar(fraction: status.fraction)
        HStack(alignment: .firstTextBaseline) {
            Text(activeLine(status))
                .font(MSFont.body)
                .foregroundStyle(MS.ink2)
            Spacer(minLength: 12)
            Text("\(Int(status.fraction * 100))%")
                .clockFont(11)
                .foregroundStyle(MS.ink3)
        }
        componentRows(status)
        if status.stalled == true {
            Text("No new data for a while. Check the connection, or carry on and let it finish in the background.")
                .font(MSFont.meta)
                .foregroundStyle(MS.ink3)
        }
        SkipLink(label: "Continue, this keeps downloading", action: next)
            .padding(.top, 6)
    }

    @ViewBuilder
    private var unreachable: some View {
        Text("The engine isn't answering, so there's nothing to fetch from here. The models download on their own the first time you record.")
            .font(MSFont.body)
            .foregroundStyle(MS.ink2)
        ContinueButton(action: next).padding(.top, 12)
    }

    @ViewBuilder
    private func spinner(_ text: String) -> some View {
        HStack(spacing: 8) {
            ProgressView().controlSize(.small)
            Text(text).font(MSFont.body).foregroundStyle(MS.ink2)
        }
    }

    @ViewBuilder
    private func componentRows(_ status: ModelStatus) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            ForEach(status.components ?? []) { c in
                CheckRow(done: c.done, text: line(for: c))
            }
        }
    }

    private func line(for c: ModelComponent) -> String {
        c.state == "skipped" ? "\(c.label), left for later"
                             : "\(c.label), \(modelSize(c.total_bytes))"
    }

    /// The one line carrying live numbers, composed HERE rather than taken
    /// from the engine's own message: two renderings of one byte count on one
    /// screen ("0 KB of 78 MB" beside "78.2 MB") is how a progress display
    /// stops being believed. The engine's wording is the fallback.
    private func activeLine(_ status: ModelStatus) -> String {
        guard let c = (status.components ?? []).first(where: { $0.state == "downloading" })
        else { return status.message ?? "Downloading…" }
        return "Downloading the \(c.label.lowercased()), "
            + "\(modelSize(c.bytes)) of \(modelSize(c.total_bytes))"
    }

    // MARK: Engine

    private func start() {
        starting = true
        Task {
            await API.prefetchModels()
            status = await API.modelStatus() ?? status
            starting = false
        }
    }

    /// Poll while this step is on screen. Cancelled the moment it isn't, so
    /// skipping costs nothing and the download itself keeps going in the
    /// engine, where it belongs.
    private func watch() async {
        while !Task.isCancelled {
            let fresh = await API.modelStatus()
            silentProbes = fresh == nil ? silentProbes + 1 : 0
            if let fresh {
                withAnimation(Motion.seek) { status = fresh }
            }
            let busy = fresh?.busy ?? (status == nil)
            try? await Task.sleep(nanoseconds: busy ? 700_000_000 : 2_000_000_000)
        }
    }
}

private struct DownloadBar: View {
    let fraction: Double

    var body: some View {
        GeometryReader { geo in
            ZStack(alignment: .leading) {
                Capsule().fill(MS.ink4.opacity(0.25))
                Capsule()
                    .fill(MS.playheadFill)
                    .frame(width: max(geo.size.width * min(1, max(0, fraction)), 3))
            }
        }
        .frame(height: 6)
        .animation(Motion.seek, value: fraction)
    }
}

private struct SkipLink: View {
    let label: String
    let action: () -> Void

    var body: some View {
        Button(label, action: action)
            .buttonStyle(.plain)
            .font(MSFont.meta)
            .foregroundStyle(MS.ink3)
    }
}

/// Decimal units, matching the engine's own arithmetic, so the app and the
/// progress line it renders never quote two different numbers for one file.
private let modelByteFormatter: ByteCountFormatter = {
    let f = ByteCountFormatter()
    f.countStyle = .file
    f.allowedUnits = [.useMB, .useGB]
    // Off, or a download that has not moved yet reads "Zero KB of 78.2 MB".
    f.allowsNonnumericFormatting = false
    return f
}()

private func modelSize(_ bytes: Int64?) -> String {
    modelByteFormatter.string(fromByteCount: max(0, bytes ?? 0))
}

private struct MicrophoneStep: View {
    let next: () -> Void
    @State private var status = AVCaptureDevice.authorizationStatus(for: .audio)

    var body: some View {
        StepPage(
            kicker: "Step 3 of 6",
            title: "Your microphone.",
            subtitle: "Your side of every meeting comes from the mic. Audio is written straight to disk on this Mac, nowhere else."
        ) {
            VStack(alignment: .leading, spacing: 14) {
                switch status {
                case .authorized:
                    CheckRow(done: true, text: "Microphone access granted")
                    ContinueButton(action: next).padding(.top, 12)
                case .denied, .restricted:
                    // Denying used to end here: no way to grant it from
                    // inside the app, and no way past the step either.
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
                    SkipLink(label: "Carry on without my microphone", action: next)
                    Text("Meetings will still be recorded, with everyone but you: your own voice needs the microphone.")
                        .font(MSFont.meta)
                        .foregroundStyle(MS.ink3)
                default:
                    HStack(spacing: 12) {
                        ContinueButton(label: "Allow microphone") {
                            AVCaptureDevice.requestAccess(for: .audio) { _ in
                                DispatchQueue.main.async {
                                    status = AVCaptureDevice.authorizationStatus(for: .audio)
                                }
                            }
                        }
                        SkipLink(label: "Not now", action: next)
                    }
                }
            }
        }
    }
}

/// The one step that can prove itself, and used to prove nothing.
///
/// It recorded three seconds, threw the result away unread, and announced
/// "System audio captured — you're fully wired" whatever had happened: with
/// the permission denied, with no loopback device, with the engine refusing
/// to start at all. The reassurance was worth less than nothing, because the
/// person who got it stopped looking. And the throwaway meeting it left
/// behind was deleted on a best-effort loop that gives up after 80 seconds —
/// on a fresh Mac, where the test recording sits behind a 2.4 GB model
/// download before it can be deleted, that loop always loses and a phantom
/// "Setup test" meeting is the first thing in the library.
///
/// Now: a quiet two-note tone is played through this Mac's normal output
/// while the test records, and the claim is whether the system track heard
/// it. That is the whole question — a system-audio path with nothing playing
/// through it is untestable, which is why a silent three seconds could never
/// have meant anything. Cleanup is handed to RecorderCenter, which keeps
/// retrying across launches until the engine really lets the meeting go.
private struct SystemAudioStep: View {
    @ObservedObject var center: RecorderCenter
    let next: () -> Void

    private enum TestPhase: Equatable {
        case idle
        case running
        case heard
        case unheard(String)
        case refused(String)
    }

    @State private var phase: TestPhase = .idle
    @State private var tone = TonePlayer()

    var body: some View {
        StepPage(
            kicker: "Step 4 of 6",
            title: "The other side of the call.",
            subtitle: "To hear everyone else, macOS asks once for system-audio access. The test below plays a short tone through this Mac and records for three seconds. Approve the prompt when it appears, and it never asks again."
        ) {
            VStack(alignment: .leading, spacing: 14) {
                switch phase {
                case .heard:
                    CheckRow(done: true, text: "System audio captured, tone and all")
                    Text("The test recording is removed as soon as the engine finishes with it.")
                        .font(MSFont.meta)
                        .foregroundStyle(MS.ink3)
                    ContinueButton(action: next).padding(.top, 12)

                case .running:
                    HStack(spacing: 8) {
                        ProgressView().controlSize(.small)
                        Text("Playing a tone and recording three seconds. Approve the system prompt if it appears…")
                            .font(MSFont.body)
                            .foregroundStyle(MS.ink2)
                            .fixedSize(horizontal: false, vertical: true)
                    }

                case .unheard(let why):
                    CheckRow(done: false, text: "Nothing reached the system-audio track")
                    Text(why)
                        .font(MSFont.body)
                        .foregroundStyle(MS.ink2)
                        .fixedSize(horizontal: false, vertical: true)
                    HStack(spacing: 12) {
                        ContinueButton(label: "Test again") { runTest() }
                        Button("Open System Settings") {
                            NSWorkspace.shared.open(URL(string:
                                "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture")!)
                        }
                        .buttonStyle(.plain)
                        .font(MSFont.chrome)
                        .foregroundStyle(MS.ink2)
                    }
                    SkipLink(label: "Carry on anyway", action: next)

                case .refused(let why):
                    CheckRow(done: false, text: "The test recording didn't start")
                    Text(why)
                        .font(MSFont.body)
                        .foregroundStyle(MS.ink2)
                        .fixedSize(horizontal: false, vertical: true)
                    HStack(spacing: 12) {
                        ContinueButton(label: "Try again") { runTest() }
                        SkipLink(label: "Carry on anyway", action: next)
                    }

                case .idle:
                    ContinueButton(label: "Run the test") { runTest() }
                    SkipLink(label: "Skip, ask me on my first real recording", action: next)
                }
            }
        }
        .onDisappear { tone.stop() }
    }

    private func runTest() {
        phase = .running
        Task {
            switch await API.recordStart(["title": "System audio check"]) {
            case .refused(let why):
                phase = .refused(why)
            case .started(let meta):
                let result = await listen()
                var id = meta.id
                if id == nil { id = await API.recorderSnapshot()?.meeting_id }
                _ = await API.recordStop()
                // Durable: the engine refuses to delete a meeting it is still
                // transcribing, and on a fresh Mac that can be a long wait.
                if let id { center.discardLater(id) }
                phase = result
            }
        }
    }

    /// Play the tone, watch the system track's level, and report what was
    /// heard. Every verdict here is about a number that was actually read.
    private func listen() async -> TestPhase {
        guard tone.play() else {
            return .unheard("MeetingScribe couldn't play the test tone through this Mac's output, so there was nothing for the recording to hear. Check the output device in System Settings → Sound, then test again.")
        }
        defer { tone.stop() }
        var peak = 0.0
        var trackPresent: Bool?
        var trackError: String?
        for _ in 0..<14 {
            try? await Task.sleep(nanoseconds: 250_000_000)
            guard let st = await API.recorderSnapshot(), st.recording else { continue }
            peak = max(peak, st.levels?["system"] ?? 0)
            if let tracks = st.tracks {
                trackPresent = tracks["system"] != nil
                trackError = tracks["system"]?.error ?? trackError
            }
            // Ten times the floor that separates a live stream from digital
            // silence, and a tenth of what the tone measures at any ordinary
            // volume: nothing else in a quiet room reaches it.
            if peak > 0.01 { return .heard }
        }
        if trackPresent == false {
            return .unheard("This Mac opened the recording without a system-audio track at all, so only your microphone would be captured. The engine's log has the reason.")
        }
        if let trackError {
            return .unheard("The system-audio track stopped with: \(trackError)")
        }
        return .unheard("The track was recording, but the tone never arrived. macOS usually blocks this the first time: allow MeetingScribe under System Settings → Privacy & Security → Screen & System Audio Recording, then test again. If your Mac's output is muted or set to a device this app can't reach, that would do it too.")
    }
}

/// A short, quiet two-note tone through this Mac's ordinary output — the only
/// way to test the far side of a call when there is no far side yet.
@MainActor
private final class TonePlayer {
    private let engine = AVAudioEngine()
    private let node = AVAudioPlayerNode()
    private var running = false

    /// False when this Mac would not play it at all — which is a different
    /// answer from "the recording heard nothing", and has to be said as one.
    @discardableResult
    func play() -> Bool {
        guard !running else { return true }
        let rate = engine.outputNode.outputFormat(forBus: 0).sampleRate
        guard let format = AVAudioFormat(standardFormatWithSampleRate: rate > 0 ? rate : 48_000,
                                         channels: 2),
              let buffer = Self.tone(format: format) else { return false }
        engine.attach(node)
        engine.connect(node, to: engine.mainMixerNode, format: format)
        guard (try? engine.start()) != nil else { return false }
        running = true
        node.scheduleBuffer(buffer, at: nil, options: .loops)
        node.play()
        return true
    }

    func stop() {
        guard running else { return }
        node.stop()
        engine.stop()
        running = false
    }

    /// 1.3 seconds, looped: a note, a gap, a second note, a gap. Loud enough
    /// to measure at 12% of full scale, and faded at both ends so it reads as
    /// a test tone rather than a click.
    private static func tone(format: AVAudioFormat) -> AVAudioPCMBuffer? {
        let seconds = 1.3
        let frames = AVAudioFrameCount(format.sampleRate * seconds)
        guard let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: frames) else {
            return nil
        }
        buffer.frameLength = frames
        for frame in 0..<Int(frames) {
            let t = Double(frame) / format.sampleRate
            let note = t < 0.35 ? (t, 660.0) : (t >= 0.55 && t < 0.9) ? (t - 0.55, 880.0) : (0, 0)
            var value = 0.0
            if note.1 > 0 {
                let fade = min(1, min(note.0, 0.35 - note.0) / 0.03)
                value = sin(2 * .pi * note.1 * t) * 0.12 * max(0, fade)
            }
            for channel in 0..<Int(format.channelCount) {
                buffer.floatChannelData?[channel][frame] = Float(value)
            }
        }
        return buffer
    }
}

/// Calendar access, reported as it actually went.
///
/// This step used to declare "Calendar connected" after two fire-and-forget
/// requests whose answers were discarded — the same green tick appeared when
/// the user pressed Deny, when the EventKit helper failed to build, and when
/// the engine was not running at all. The endpoint has always said which of
/// those happened: `available` is false and `error` carries the reason.
private struct CalendarStep: View {
    let next: () -> Void
    @State private var status: CalendarStatus?
    @State private var asking = false

    var body: some View {
        StepPage(
            kicker: "Step 5 of 6",
            title: "Meetings, by name.",
            subtitle: "With calendar access, recordings name themselves after the event you're in, and MeetingScribe nudges you to record when a meeting starts. Optional, everything works without it."
        ) {
            VStack(alignment: .leading, spacing: 14) {
                if asking {
                    HStack(spacing: 8) {
                        ProgressView().controlSize(.small)
                        Text("Waiting for macOS to ask you about Calendars…")
                            .font(MSFont.body)
                            .foregroundStyle(MS.ink2)
                    }
                    SkipLink(label: "Carry on without it", action: next)
                } else if status?.available == true {
                    CheckRow(done: true, text: connectedLine)
                    ContinueButton(action: next).padding(.top, 12)
                } else if let status {
                    CheckRow(done: false, text: "Calendar not connected")
                    Text(reason(status))
                        .font(MSFont.body)
                        .foregroundStyle(MS.ink2)
                        .fixedSize(horizontal: false, vertical: true)
                    HStack(spacing: 12) {
                        ContinueButton(label: "Try again") { connect() }
                        Button("Open System Settings") {
                            NSWorkspace.shared.open(URL(string:
                                "x-apple.systempreferences:com.apple.preference.security?Privacy_Calendars")!)
                        }
                        .buttonStyle(.plain)
                        .font(MSFont.chrome)
                        .foregroundStyle(MS.ink2)
                    }
                    SkipLink(label: "Carry on without it", action: next)
                    Text("Recordings will be named by date and time, and you can rename any of them.")
                        .font(MSFont.meta)
                        .foregroundStyle(MS.ink3)
                } else {
                    HStack(spacing: 12) {
                        ContinueButton(label: "Connect calendar") { connect() }
                        SkipLink(label: "Skip for now", action: next)
                    }
                }
            }
        }
    }

    private var connectedLine: String {
        let count = status?.events?.filter { !$0.isPast }.count ?? 0
        if count == 0 { return "Calendar connected, nothing left on today" }
        return "Calendar connected, \(count) meeting\(count == 1 ? "" : "s") still to come today"
    }

    private func reason(_ status: CalendarStatus) -> String {
        let detail = status.error?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        if detail.lowercased().contains("denied") || detail.lowercased().contains("not granted") {
            return "macOS is not allowing MeetingScribe to read your calendar. Turn it on in System Settings → Privacy & Security → Calendars, then try again."
        }
        return detail.isEmpty
            ? "The calendar helper didn't answer, so nothing can be read from it yet."
            : detail
    }

    /// Ask, and keep asking while the system prompt is on screen. The first
    /// request blocks inside the engine until the user answers the macOS
    /// dialog — far longer than one HTTP timeout — so a single call could
    /// only ever come back "no" no matter what the user pressed.
    private func connect() {
        asking = true
        status = nil
        Task {
            var answer = await API.calendarStatus()
            var waited = 0.0
            while answer.available != true, waited < 90 {
                try? await Task.sleep(nanoseconds: 2_000_000_000)
                waited += 2
                answer = await API.calendarStatus()
                // A definite refusal is an answer; stop waiting for a prompt
                // that has already been dismissed.
                if (answer.error ?? "").lowercased().contains("denied") { break }
            }
            status = answer
            asking = false
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
            kicker: "Step 6 of 6",
            title: "Who writes your summaries.",
            subtitle: "After each meeting, MeetingScribe writes the recap, to-dos and a follow-up email, and answers questions about it. Apple Intelligence does this on-device, so nothing needs installing."
        ) {
            VStack(alignment: .leading, spacing: 14) {
                if checked {
                    CheckRow(done: appleReady,
                             text: appleReady
                             ? "Apple Intelligence ready, nothing to install"
                             : (appleMessage ?? "Apple Intelligence unavailable on this Mac"))
                    CheckRow(done: claudeFound,
                             text: claudeFound
                             ? "Claude also found, switch to it in Settings for richer writing"
                             : "Claude Code not installed, optional, not needed")
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
