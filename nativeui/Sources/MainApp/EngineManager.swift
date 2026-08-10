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
    /// A meeting was being recorded when the engine died, and the watchdog
    /// brought the engine back by itself — so `state` returns to .running and
    /// the failure banner that would have explained it never appears. Fired
    /// once per such death, for whoever owns a surface that is always visible.
    var onLostRecording: (() -> Void)?

    private let dataDir = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent(".meetingscribe")

    /// How many times the watchdog quietly puts the engine back before it
    /// gives up and hands the failure to the user, and how long it waits in
    /// between. Three attempts is enough to ride out the two deaths that
    /// actually happen — a helper that took the interpreter down with it, and
    /// an OOM under a transcription — without turning a genuinely broken
    /// install into a restart loop the user cannot see or stop.
    private let autoRestartAttempts = 3
    private let autoRestartGap: UInt64 = 5_000_000_000
    /// When the watchdog last brought the engine back by itself. See recover.
    private var recoveredAt: Date?

    func ensureRunning() async {
        if await healthy() {
            state = .running
            watch()
            return
        }
        spawn()
    }

    /// Start again after a death (or after a failed start), because the user
    /// asked. Adopts an engine that came back on its own, spawns one otherwise.
    func restart() async {
        monitorTask?.cancel()
        monitorTask = nil
        // The user has read the banner; the death it described is now theirs
        // to have seen. Only this path clears it — and their restart earns the
        // watchdog its one automatic save back.
        lostRecording = false
        recoveredAt = nil
        await relaunch()
    }

    /// The restart itself, without touching the monitor task — so the watchdog
    /// can use the very same path from inside its own loop without cancelling
    /// itself halfway through the thing it is doing.
    private func relaunch() async {
        if let process, process.isRunning { process.terminate() }
        process = nil
        spawnedByUs = false
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
                // A death used to end here, in a banner asking the user to
                // press a button the app could perfectly well press itself.
                if await self.recover() {
                    misses = 0
                    continue          // back on its feet, and still watched
                }
                guard !Task.isCancelled else { return }
                self.monitorTask = nil
                return
            }
        }
    }

    /// The engine stopped answering. Put it back before telling anyone.
    ///
    /// Returns true when the engine is running again — in which case the
    /// caller keeps its post, because a watchdog that stops watching after the
    /// first save is a watchdog for exactly one death.
    private func recover() async -> Bool {
        // Both readings are taken HERE, at the death: fifteen seconds of
        // retries would push the recording out of the window
        // `wasRecordingRecently` looks back over, and a `process` that gets
        // replaced by the first attempt can no longer say how it exited.
        let lost = wasRecordingRecently()
        let why = deathMessage()
        // An engine that dies again within a minute of being revived is not
        // having a blip, it is failing on startup in slow motion — and quietly
        // restarting it forever would be an invisible loop the user can
        // neither see nor stop. One save per engine, then it is their call.
        if let recoveredAt, Date().timeIntervalSince(recoveredAt) < 60 {
            lostRecording = lost
            state = .failed(why + " It was restarted a moment ago and stopped again.")
            return false
        }
        for attempt in 1...autoRestartAttempts {
            // A user-driven restart cancels this task. It owns the engine from
            // that moment; stop here rather than spawn a second one behind it
            // or overwrite the state it is setting.
            guard !Task.isCancelled else { return true }
            await relaunch()
            if await settled() {
                recoveredAt = Date()
                if lost { onLostRecording?() }
                // Nothing is on screen to carry this any more: the banner it
                // captions is gone with the failure. It was reported above.
                lostRecording = false
                return true
            }
            guard !Task.isCancelled else { return true }
            if attempt < autoRestartAttempts {
                try? await Task.sleep(nanoseconds: autoRestartGap)
            }
        }
        // Three attempts and it is still not answering: this is not a blip,
        // and the user's Mac is the only place it can be fixed now. Say that
        // the app already tried, so the banner's Restart button is offered as
        // one more go rather than as the thing nobody thought to do.
        lostRecording = lost
        state = .failed(why + " Restarting it three times didn't bring it back.")
        return false
    }

    /// Wait for a relaunch to declare itself. `spawn` reports its outcome
    /// through `state`, from a task of its own, so the answer is read there
    /// rather than raced with a second health poll of our own.
    private func settled() async -> Bool {
        for _ in 0..<90 {          // 45s; spawn allows the engine 30 to come up
            if state == .running { return true }
            if case .failed = state { return false }
            if Task.isCancelled { return state == .running }
            try? await Task.sleep(nanoseconds: 500_000_000)
        }
        return state == .running
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
        // Never write .pyc files: the bundled runtime lives inside the signed,
        // read-only .app and its resource seal must survive execution.
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        // A crash report that is still sitting in a 4 KB stdio buffer when the
        // process dies is a crash report nobody ever reads. The last thing the
        // engine says before it goes is the one line worth having.
        env["PYTHONUNBUFFERED"] = "1"
        if let bin = Bundle.main.resourceURL?.appendingPathComponent("bin"),
           FileManager.default.fileExists(atPath: bin.path) {
            env["MEETINGSCRIBE_PREBUILT"] = bin.path
        }
        p.environment = env
        // Both streams to one file, appended. Until now they went to the
        // parent's stdout — which for a double-clicked .app is /dev/null — so
        // "the engine quit unexpectedly (exit code 1)" was the entire account
        // of every failure this app has ever had, and the traceback that would
        // have explained it was discarded as it was written.
        let logHandle = engineLog()
        p.standardOutput = logHandle ?? FileHandle.nullDevice
        p.standardError = logHandle ?? FileHandle.nullDevice
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

    /// ~/.meetingscribe/app.log, open for appending, rolled once it gets big.
    ///
    /// One generation back (app.log.1) and a 20 MB ceiling: enough to still
    /// hold the death that happened this morning, little enough that a chatty
    /// engine left running for a month cannot quietly eat someone's disk —
    /// which on a machine that also stores hours of uncompressed audio is not
    /// a hypothetical.
    private func engineLog() -> FileHandle? {
        let fm = FileManager.default
        try? fm.createDirectory(at: dataDir, withIntermediateDirectories: true)
        let url = dataDir.appendingPathComponent("app.log")
        if let attrs = try? fm.attributesOfItem(atPath: url.path),
           let size = attrs[.size] as? Int, size > 20 * 1024 * 1024 {
            let rolled = dataDir.appendingPathComponent("app.log.1")
            try? fm.removeItem(at: rolled)
            try? fm.moveItem(at: url, to: rolled)
        }
        if !fm.fileExists(atPath: url.path) {
            fm.createFile(atPath: url.path, contents: nil)
        }
        guard let handle = try? FileHandle(forWritingTo: url) else { return nil }
        // Appending, not truncating: the previous run's last words are the
        // most useful thing in the file the moment this run starts.
        handle.seekToEndOfFile()
        return handle
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
