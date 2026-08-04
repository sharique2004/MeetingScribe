// The native app owns the engine now: on launch it adopts a running engine
// if one answers on 5005, otherwise spawns the bundled one exactly the way
// the old shell did (venv python + Resources/app/app.py). On quit it shuts
// the engine down only if it spawned it — and never mid-recording.
import Foundation
import AppKit

/// The shutdown reply, written on URLSession's thread and read on the main
/// one after the semaphore. The two never overlap, so the box needs no lock.
private final class ShutdownReply: @unchecked Sendable {
    var refusal: String?
}

@MainActor
final class EngineManager: ObservableObject {
    static let shared = EngineManager()

    enum State: Equatable {
        case checking
        case starting
        case running
        case failed(String)

        var message: String? {
            if case .failed(let why) = self { return why }
            return nil
        }
    }

    @Published private(set) var state: State = .checking
    /// The engine died while a meeting was being recorded. The audio up to
    /// that moment is on disk, and the engine repairs the meeting into
    /// "error" the next time it starts — so this is the one failure the user
    /// has to be told something extra about.
    @Published private(set) var lostRecording = false
    private var process: Process?
    private(set) var spawnedByUs = false
    private var monitorTask: Task<Void, Never>?
    /// Set by the UI so the quit path can refuse to kill a live recording.
    var isRecording: () -> Bool = { false }
    /// Was a meeting being recorded in the seconds before now, with no clean
    /// stop since? It cannot be `isRecording` here: the recorder's own poll
    /// turns "recording" into "offline" the moment the engine stops
    /// answering, so by the time a death is CONFIRMED nothing is recording
    /// any more. The question a death has to ask is about the recent past.
    var wasRecordingRecently: () -> Bool = { false }

    private let dataDir = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent(".meetingscribe")

    func ensureRunning() async {
        if await healthy() {
            state = .running
            watch()
            return
        }
        spawn()
    }

    /// Start again after a death (or after a failed start). Adopts an engine
    /// that came back on its own, spawns one otherwise.
    func restart() async {
        monitorTask?.cancel()
        monitorTask = nil
        if let process, process.isRunning { process.terminate() }
        process = nil
        spawnedByUs = false
        lostRecording = false
        state = .checking
        await ensureRunning()
    }

    /// Watch the engine for as long as the app is up.
    ///
    /// Nothing did. The engine was launched and then trusted forever: when it
    /// died the recorder poll simply reported "offline", the library said the
    /// engine "isn't answering", a live recording stopped existing without a
    /// word, and the app offered no way back short of quitting it. Three
    /// consecutive misses, so a slow answer under a transcription load is not
    /// mistaken for a death.
    private func watch() {
        guard monitorTask == nil else { return }
        monitorTask = Task { [weak self] in
            var misses = 0
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 4_000_000_000)
                guard !Task.isCancelled, let self, self.state == .running else { return }
                if await self.healthy() {
                    misses = 0
                    continue
                }
                misses += 1
                guard misses >= 3 else { continue }
                guard !Task.isCancelled, self.state == .running else { return }
                self.lostRecording = self.wasRecordingRecently()
                self.state = .failed(self.deathMessage())
                self.monitorTask = nil
                return
            }
        }
    }

    private func deathMessage() -> String {
        if let process, !process.isRunning {
            return "The engine quit unexpectedly (exit code \(process.terminationStatus))."
        }
        return "The engine stopped answering."
    }

    private func healthy() async -> Bool {
        var req = URLRequest(url: engineBase.appendingPathComponent("api/record/status"))
        req.timeoutInterval = 1.5
        guard let (_, resp) = try? await URLSession.shared.data(for: req) else { return false }
        return (resp as? HTTPURLResponse)?.statusCode == 200
    }

    private var engineSourceDir: URL? {
        // Bundled engine first; the repo checkout as the dev fallback.
        let bundled = Bundle.main.resourceURL?.appendingPathComponent("app")
        if let bundled, FileManager.default.fileExists(atPath: bundled.appendingPathComponent("app.py").path) {
            return bundled
        }
        let repo = URL(fileURLWithPath: NSHomeDirectory()).appendingPathComponent("MeetingScribe")
        if FileManager.default.fileExists(atPath: repo.appendingPathComponent("app.py").path) {
            return repo
        }
        return nil
    }

    /// The interpreter to run the engine with.
    ///
    /// A downloaded copy carries its own relocatable CPython at
    /// Contents/Resources/python (tools/build_dmg_bundle.sh puts it there), so
    /// it runs with no install step: no system Python, no pip, no bootstrap.
    /// Only a developer build, which has no such runtime, falls back to the
    /// ~/.meetingscribe venv.
    private var enginePython: String? {
        if let bundled = Bundle.main.resourceURL?
            .appendingPathComponent("python/bin/python3").path,
           FileManager.default.isExecutableFile(atPath: bundled) {
            return bundled
        }
        let venv = dataDir.appendingPathComponent("venv/bin/python").path
        return FileManager.default.isExecutableFile(atPath: venv) ? venv : nil
    }

    private func spawn() {
        guard let python = enginePython else {
            state = .failed("The Python environment is missing. Run setup.sh once, then relaunch.")
            return
        }
        guard let sourceDir = engineSourceDir else {
            state = .failed("The engine files are missing from the app bundle.")
            return
        }

        let p = Process()
        p.executableURL = URL(fileURLWithPath: python)
        p.arguments = [sourceDir.appendingPathComponent("app.py").path]
        p.currentDirectoryURL = sourceDir
        var env = ProcessInfo.processInfo.environment
        env["MEETINGSCRIBE_DATA"] = dataDir.path
        env["MEETINGSCRIBE_NO_BROWSER"] = "1"
        if let bin = Bundle.main.resourceURL?.appendingPathComponent("bin"),
           FileManager.default.fileExists(atPath: bin.path) {
            env["MEETINGSCRIBE_PREBUILT"] = bin.path
        }
        p.environment = env
        do {
            try p.run()
        } catch {
            state = .failed("Couldn't start the engine: \(error.localizedDescription)")
            return
        }
        process = p
        spawnedByUs = true
        state = .starting

        Task {
            for _ in 0..<60 {
                try? await Task.sleep(nanoseconds: 500_000_000)
                if await healthy() {
                    state = .running
                    watch()
                    return
                }
                if !p.isRunning {
                    state = .failed("The engine exited while starting. Check ~/.meetingscribe for logs.")
                    return
                }
            }
            state = .failed("The engine didn't come up in time.")
        }
    }

    /// Whether an interpreter exists at all, so onboarding knows if the
    /// one-time setup is needed. A downloaded copy ships its own runtime and
    /// must never be asked to install a 2 GB environment it already has;
    /// only a developer build without a venv should see that step.
    var venvExists: Bool { enginePython != nil }

    /// First-run setup: run the bundled bootstrap script (creates the venv,
    /// installs the engine's dependencies), streaming its output.
    func runBootstrap(onLine: @escaping (String) -> Void,
                      onDone: @escaping (Bool) -> Void) {
        guard let sourceDir = engineSourceDir else {
            onDone(false)
            return
        }
        let script = sourceDir.appendingPathComponent("tools/bootstrap.sh")
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/bash")
        p.arguments = [script.path, sourceDir.path]
        let pipe = Pipe()
        p.standardOutput = pipe
        p.standardError = pipe
        pipe.fileHandleForReading.readabilityHandler = { handle in
            let data = handle.availableData
            guard !data.isEmpty, let text = String(data: data, encoding: .utf8) else { return }
            for line in text.split(separator: "\n") {
                let s = String(line)
                DispatchQueue.main.async { onLine(s) }
            }
        }
        p.terminationHandler = { proc in
            pipe.fileHandleForReading.readabilityHandler = nil
            let ok = proc.terminationStatus == 0
            DispatchQueue.main.async { onDone(ok) }
        }
        do {
            try p.run()
        } catch {
            onLine("Couldn't run bootstrap: \(error.localizedDescription)")
            onDone(false)
        }
    }

    /// Why quitting now would cost the user something. `overridable` is the
    /// difference between "you can fix this in two seconds" (stop the
    /// recording) and "the engine is mid-write and asked for a minute",
    /// which the user is allowed to overrule on their own machine.
    struct QuitRefusal {
        let title: String
        let body: String
        let overridable: Bool
    }

    /// Called from the app's quit path. Returns nil when it is safe to go,
    /// and the reason to show when it is not.
    ///
    /// The engine answers /api/shutdown with 409 when a meeting is still
    /// being transcribed, summarized or reclustered — it is protecting a
    /// half-written meeting.json and analysis.npz. This used to fire the
    /// request and return true no matter what came back, so the refusal was
    /// read by nobody and the app quit anyway.
    func prepareForQuit() -> QuitRefusal? {
        if isRecording() {
            return QuitRefusal(
                title: "A recording is running",
                body: "Stop the recording before quitting so the meeting is saved.",
                overridable: false)
        }
        guard spawnedByUs else { return nil }
        // Fire-and-wait briefly: a graceful shutdown keeps the meeting store
        // clean; if it hangs the process is our child and dies with us.
        let sem = DispatchSemaphore(value: 0)
        var req = URLRequest(url: engineBase.appendingPathComponent("api/shutdown"))
        req.httpMethod = "POST"
        req.timeoutInterval = 2
        let reply = ShutdownReply()
        URLSession.shared.dataTask(with: req) { data, resp, _ in
            defer { sem.signal() }
            guard let code = (resp as? HTTPURLResponse)?.statusCode,
                  !(200..<300).contains(code), let data else { return }
            reply.refusal =
                ((try? JSONSerialization.jsonObject(with: data)) as? [String: Any])?["error"] as? String
                ?? "The engine is still busy (\(code))."
        }.resume()
        _ = sem.wait(timeout: .now() + 2.5)
        guard let refusal = reply.refusal else { return nil }
        return QuitRefusal(
            title: "MeetingScribe is still working",
            body: refusal + "\n\nQuitting now can leave that meeting half written.",
            overridable: true)
    }
}
