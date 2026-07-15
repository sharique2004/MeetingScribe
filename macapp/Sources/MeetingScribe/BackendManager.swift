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

    /// Where app.py lives. Baked into Info.plist by the build script.
    let projectDir: URL
    let pythonPath: String

    private(set) var healthy = false
    var onHealthChange: ((Bool) -> Void)?

    private var process: Process?
    private var restartDelay: TimeInterval = 2
    private var spawnedAt = Date.distantPast
    private var quitting = false

    init() {
        let home = FileManager.default.homeDirectoryForCurrentUser
        let configured = Bundle.main.object(forInfoDictionaryKey: "MSProjectDir") as? String
        let dir = configured.map { NSString(string: $0).expandingTildeInPath }
            ?? home.appendingPathComponent("MeetingScribe").path
        projectDir = URL(fileURLWithPath: dir)
        pythonPath = home.appendingPathComponent(".meetingscribe/venv/bin/python").path
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
        guard Date().timeIntervalSince(spawnedAt) >= restartDelay else { return }
        guard FileManager.default.isExecutableFile(atPath: pythonPath),
              FileManager.default.fileExists(atPath: projectDir.appendingPathComponent("app.py").path) else {
            presentMissingInstall()
            return
        }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: pythonPath)
        p.arguments = [projectDir.appendingPathComponent("app.py").path]
        p.currentDirectoryURL = projectDir
        var env = ProcessInfo.processInfo.environment
        env["MEETINGSCRIBE_NO_BROWSER"] = "1"
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
