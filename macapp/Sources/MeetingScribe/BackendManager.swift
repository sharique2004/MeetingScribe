// Owns the Python backend: spawn it, watch it, restart it, stop it.
//
// The backend is the existing Flask app (app.py) run with the project venv.
// If a healthy server is already answering on the port (started from
// Terminal, say), we simply use it instead of spawning a second one.

import AppKit
import Foundation

final class BackendManager {
    static let port = 5005
    let baseURL = URL(string: "http://127.0.0.1:\(BackendManager.port)")!

    /// Where app.py lives — bundled inside the .app for a downloaded copy, or
    /// a source checkout for developer builds.
    let projectDir: URL
    let pythonPath: String
    let dataDir: URL
    /// True when running the shipped self-contained bundle (as opposed to a
    /// developer source checkout). Determines whether we can self-bootstrap.
    let bundled: Bool

    private(set) var healthy = false
    var onHealthChange: ((Bool) -> Void)?
    /// Called when the local environment must be built before the backend can
    /// run (fresh download). The handler drives the setup window + bootstrap.
    var onNeedsBootstrap: (() -> Void)?

    private var process: Process?
    private var restartDelay: TimeInterval = 2
    private var spawnedAt = Date.distantPast
    private var quitting = false
    private(set) var bootstrapping = false

    init() {
        let home = FileManager.default.homeDirectoryForCurrentUser
        dataDir = home.appendingPathComponent(".meetingscribe")
        pythonPath = dataDir.appendingPathComponent("venv/bin/python").path

        // Prefer the Python source bundled inside the app (a downloaded copy);
        // fall back to the Info.plist path or ~/MeetingScribe for dev builds.
        let bundledApp = Bundle.main.resourceURL?.appendingPathComponent("app")
        if let b = bundledApp, FileManager.default.fileExists(atPath: b.appendingPathComponent("app.py").path) {
            projectDir = b
            bundled = true
        } else {
            let configured = Bundle.main.object(forInfoDictionaryKey: "MSProjectDir") as? String
            let dir = configured.map { NSString(string: $0).expandingTildeInPath }
                ?? home.appendingPathComponent("MeetingScribe").path
            projectDir = URL(fileURLWithPath: dir)
            bundled = false
        }
    }

    var needsBootstrap: Bool {
        !FileManager.default.isExecutableFile(atPath: pythonPath)
    }

    func start() {
        checkAndHeal()
        Timer.scheduledTimer(withTimeInterval: 5, repeats: true) { [weak self] _ in
            self?.checkAndHeal()
        }
    }

    private func checkAndHeal() {
        var request = URLRequest(url: baseURL.appendingPathComponent("api/status"))
        request.timeoutInterval = 2.5
        URLSession.shared.dataTask(with: request) { [weak self] _, response, _ in
            guard let self else { return }
            let ok = (response as? HTTPURLResponse)?.statusCode == 200
            DispatchQueue.main.async {
                if ok != self.healthy {
                    self.healthy = ok
                    self.onHealthChange?(ok)
                }
                if ok {
                    self.restartDelay = 2
                } else if !self.quitting {
                    self.spawnIfNeeded()
                }
            }
        }.resume()
    }

    private func spawnIfNeeded() {
        if let p = process, p.isRunning { return }  // starting up — give it time
        if bootstrapping { return }
        guard Date().timeIntervalSince(spawnedAt) >= restartDelay else { return }
        guard FileManager.default.fileExists(atPath: projectDir.appendingPathComponent("app.py").path) else {
            presentMissingInstall()
            return
        }
        if needsBootstrap {
            // Fresh download: build the local environment first.
            if bundled { onNeedsBootstrap?() } else { presentMissingInstall() }
            return
        }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: pythonPath)
        p.arguments = [projectDir.appendingPathComponent("app.py").path]
        p.currentDirectoryURL = projectDir
        var env = ProcessInfo.processInfo.environment
        env["MEETINGSCRIBE_NO_BROWSER"] = "1"
        if bundled { env["MEETINGSCRIBE_DATA"] = dataDir.path }
        p.environment = env
        p.standardOutput = FileHandle.nullDevice
        if let logHandle = try? FileHandle(forWritingTo: logURL()) {
            logHandle.seekToEndOfFile()
            p.standardError = logHandle
        } else {
            p.standardError = FileHandle.nullDevice
        }
        p.terminationHandler = { [weak self] proc in
            DispatchQueue.main.async {
                guard let self, !self.quitting else { return }
                self.process = nil
                if proc.terminationStatus == 0 {
                    // Clean exit = the user pressed Quit inside the web UI.
                    NSApp.terminate(nil)
                } else {
                    self.restartDelay = min(self.restartDelay * 2, 30)
                }
            }
        }
        do {
            try p.run()
            process = p
            spawnedAt = Date()
            NSLog("MeetingScribe backend spawned (pid %d)", p.processIdentifier)
        } catch {
            NSLog("could not start backend: \(error)")
            restartDelay = min(restartDelay * 2, 30)
            spawnedAt = Date()
        }
    }

    /// Run the bundled bootstrap once (fresh download): build the venv, install
    /// dependencies, compile the Speech helpers. Streams progress lines.
    func runBootstrap(onLine: @escaping (String) -> Void,
                      onDone: @escaping (Bool, String?) -> Void) {
        guard let script = Bundle.main.resourceURL?
            .appendingPathComponent("bootstrap.sh"), !bootstrapping else {
            onDone(false, "Setup script is missing from the app bundle.")
            return
        }
        bootstrapping = true
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/bash")
        p.arguments = [script.path, projectDir.path]
        let pipe = Pipe()
        p.standardOutput = pipe
        p.standardError = pipe
        var sawComplete = false
        pipe.fileHandleForReading.readabilityHandler = { handle in
            let data = handle.availableData
            guard !data.isEmpty, let text = String(data: data, encoding: .utf8) else { return }
            for line in text.split(separator: "\n", omittingEmptySubsequences: false) {
                let s = String(line)
                if s.contains("SETUP-COMPLETE") { sawComplete = true; continue }
                if !s.isEmpty { DispatchQueue.main.async { onLine(s) } }
            }
        }
        p.terminationHandler = { [weak self] proc in
            DispatchQueue.main.async {
                self?.bootstrapping = false
                pipe.fileHandleForReading.readabilityHandler = nil
                let ok = proc.terminationStatus == 0 && sawComplete
                onDone(ok, ok ? nil : "Setup exited with an error — see the log above.")
            }
        }
        do {
            try p.run()
        } catch {
            bootstrapping = false
            onDone(false, "Could not start setup: \(error.localizedDescription)")
        }
    }

    private func logURL() -> URL {
        let dir = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".meetingscribe")
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let url = dir.appendingPathComponent("app.log")
        if !FileManager.default.fileExists(atPath: url.path) {
            FileManager.default.createFile(atPath: url.path, contents: nil)
        }
        return url
    }

    private var alertShown = false
    private func presentMissingInstall() {
        guard !alertShown else { return }
        alertShown = true
        let alert = NSAlert()
        alert.messageText = "MeetingScribe's files were not found"
        alert.informativeText = """
        Expected the app files at \(projectDir.path) and the Python environment at \
        \(pythonPath). If you moved the folder, run `bash setup.sh` in the new \
        location to rebuild this app.
        """
        alert.runModal()
        NSApp.terminate(nil)
    }

    /// Ask the backend to exit cleanly. If it refuses because a meeting is
    /// still transcribing/summarizing (HTTP 409), the caller is told so it can
    /// ask the user instead of killing the job. `completion(true)` = safe to
    /// quit; `completion(false)` = backend is busy, don't force-kill yet.
    func shutdown(then completion: @escaping (_ didQuit: Bool) -> Void) {
        var request = URLRequest(url: baseURL.appendingPathComponent("api/shutdown"))
        request.httpMethod = "POST"
        request.timeoutInterval = 1.5
        var replied = false
        let reply: (Bool) -> Void = { didQuit in
            DispatchQueue.main.async {
                guard !replied else { return }
                replied = true
                completion(didQuit)
            }
        }
        URLSession.shared.dataTask(with: request) { [weak self] _, response, _ in
            let status = (response as? HTTPURLResponse)?.statusCode
            if status == 409 {
                reply(false)  // busy — leave the backend running, ask the user
                return
            }
            // Any other outcome (200, or the backend is already gone/unreachable)
            // means it's safe to quit. Terminate our child and confirm.
            DispatchQueue.main.async {
                self?.quitting = true
                if let p = self?.process, p.isRunning { p.terminate() }
            }
            reply(true)
        }.resume()
    }

    /// Force-quit even though the backend is busy (user chose "Quit anyway").
    func forceShutdown(then completion: @escaping () -> Void) {
        quitting = true
        if let p = process, p.isRunning { p.terminate() }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.3, execute: completion)
    }
}
