// The native app owns the engine now: on launch it adopts a running engine
// if one answers on 5005, otherwise spawns the bundled one exactly the way
// the old shell did (venv python + Resources/app/app.py). On quit it shuts
// the engine down only if it spawned it — and never mid-recording.
import Foundation
import AppKit

@MainActor
final class EngineManager: ObservableObject {
    static let shared = EngineManager()

    enum State: Equatable {
        case checking
        case starting
        case running
        case failed(String)
    }

    @Published private(set) var state: State = .checking
    private var process: Process?
    private(set) var spawnedByUs = false
    /// Set by the UI so the quit path can refuse to kill a live recording.
    var isRecording: () -> Bool = { false }

    private let dataDir = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent(".meetingscribe")

    func ensureRunning() async {
        if await healthy() {
            state = .running
            return
        }
        spawn()
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
            state = .failed("The Python environment is missing — run setup.sh once, then relaunch.")
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
                    return
                }
                if !p.isRunning {
                    state = .failed("The engine exited while starting — check ~/.meetingscribe for logs.")
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

    /// Called from the app's quit path. Returns false when quit must be
    /// blocked (a recording is running).
    func prepareForQuit() -> Bool {
        if isRecording() { return false }
        guard spawnedByUs else { return true }
        // Fire-and-wait briefly: a graceful shutdown keeps the meeting store
        // clean; if it hangs the process is our child and dies with us.
        let sem = DispatchSemaphore(value: 0)
        var req = URLRequest(url: engineBase.appendingPathComponent("api/shutdown"))
        req.httpMethod = "POST"
        req.timeoutInterval = 2
        URLSession.shared.dataTask(with: req) { _, _, _ in sem.signal() }.resume()
        _ = sem.wait(timeout: .now() + 2.5)
        return true
    }
}
