// MeetingScribe.app — the native shell.
//
// A proper Mac app: Dock icon, menu bar item, native notifications, and a
// floating recording panel — wrapped around the local Python backend and
// its web UI. Everything still runs on this Mac.

import AppKit

final class AppDelegate: NSObject, NSApplicationDelegate {
    private var backend: BackendManager!
    private var mainWindow: MainWindow!
    private var statusItem: StatusItem!
    private var recordingPanel: RecordingPanel!
    private var notifications: Notifications!
    private var statusPoller: StatusPoller!
    private var nudgePoller: NudgePoller!
    private var lastRecorder = RecorderState()

    func applicationDidFinishLaunching(_ notification: Notification) {
        backend = BackendManager()
        mainWindow = MainWindow(baseURL: backend.baseURL)
        statusItem = StatusItem(baseURL: backend.baseURL)
        recordingPanel = RecordingPanel(baseURL: backend.baseURL)
        notifications = Notifications(baseURL: backend.baseURL)
        statusPoller = StatusPoller(baseURL: backend.baseURL)
        nudgePoller = NudgePoller(baseURL: backend.baseURL, notifications: notifications)

        let openApp: () -> Void = { [weak self] in self?.mainWindow.show() }
        statusItem.onOpenApp = openApp
        recordingPanel.onOpenApp = openApp
        notifications.onOpenApp = openApp

        backend.onHealthChange = { [weak self] healthy in
            if healthy {
                self?.mainWindow.backendBecameHealthy()
            } else {
                self?.mainWindow.backendWentDown()
            }
        }

        statusPoller.onRecorderChange = { [weak self] state in
            guard let self else { return }
            self.statusItem.setRecording(state.recording)
            if state.recording {
                self.recordingPanel.update(elapsed: state.elapsed, title: nil)
                self.recordingPanel.show()
            } else {
                self.recordingPanel.hide()
            }
            self.lastRecorder = state
        }
        statusPoller.onRecorderTick = { [weak self] state, title in
            self?.lastRecorder = state
            self?.recordingPanel.update(elapsed: state.elapsed, title: title)
        }
        statusPoller.onJobFinished = { [weak self] kind, meetingID, ok, message in
            self?.notifications.postJobDone(kind: kind, meetingID: meetingID, ok: ok, message: message)
        }

        buildMenuBar()
        notifications.setup()
        backend.start()
        statusPoller.start()
        nudgePoller.start()
        mainWindow.show()
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        mainWindow.show()
        return true
    }

    private var terminationReplied = false

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if lastRecorder.recording {
            let alert = NSAlert()
            alert.messageText = "A recording is still running"
            alert.informativeText = "Quitting now abandons the recording in progress. "
                + "Stop & transcribe first to keep it."
            alert.addButton(withTitle: "Cancel")
            alert.addButton(withTitle: "Quit Anyway")
            if alert.runModal() == .alertFirstButtonReturn {
                return .terminateCancel
            }
        }
        // Reply exactly once, whichever path gets there first.
        terminationReplied = false
        func replyQuit() {
            guard !terminationReplied else { return }
            terminationReplied = true
            NSApp.reply(toApplicationShouldTerminate: true)
        }
        // Hard guarantee: the app ALWAYS quits within a couple of seconds, no
        // matter what the backend does. Quitting must never be able to hang.
        DispatchQueue.main.asyncAfter(deadline: .now() + 3.0) { [weak self] in
            if !(self?.terminationReplied ?? true) {
                self?.backend.forceShutdown { replyQuit() }
            }
        }
        backend.shutdown { [weak self] didQuit in
            if didQuit { return replyQuit() }
            // The backend refused: a transcript or summary is still being
            // built. Ask instead of killing the job mid-flight.
            let alert = NSAlert()
            alert.messageText = "A meeting is still being processed"
            alert.informativeText = "Quitting now interrupts it — you can use "
                + "“Reprocess audio” on that meeting later. Quit anyway?"
            alert.addButton(withTitle: "Cancel")
            alert.addButton(withTitle: "Quit Anyway")
            if alert.runModal() == .alertFirstButtonReturn {
                self?.terminationReplied = true
                NSApp.reply(toApplicationShouldTerminate: false)
            } else {
                self?.backend.forceShutdown { replyQuit() }
            }
        }
        return .terminateLater
    }

    /// Minimal main menu so standard shortcuts (⌘C/⌘V in the web view, ⌘Q,
    /// ⌘R reload, ⌘W close) work like any Mac app.
    private func buildMenuBar() {
        let main = NSMenu()

        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "About MeetingScribe",
                        action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)),
                        keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Quit MeetingScribe",
                        action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        let appItem = NSMenuItem()
        appItem.submenu = appMenu
        main.addItem(appItem)

        let editMenu = NSMenu(title: "Edit")
        editMenu.addItem(withTitle: "Undo", action: Selector(("undo:")), keyEquivalent: "z")
        editMenu.addItem(withTitle: "Redo", action: Selector(("redo:")), keyEquivalent: "Z")
        editMenu.addItem(.separator())
        editMenu.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        editMenu.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        editMenu.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        editMenu.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        let editItem = NSMenuItem()
        editItem.submenu = editMenu
        main.addItem(editItem)

        let viewMenu = NSMenu(title: "View")
        let reload = NSMenuItem(title: "Reload", action: #selector(reloadPage), keyEquivalent: "r")
        reload.target = self
        viewMenu.addItem(reload)
        let viewItem = NSMenuItem()
        viewItem.submenu = viewMenu
        main.addItem(viewItem)

        let windowMenu = NSMenu(title: "Window")
        windowMenu.addItem(withTitle: "Minimize", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
        windowMenu.addItem(withTitle: "Close", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        let windowItem = NSMenuItem()
        windowItem.submenu = windowMenu
        main.addItem(windowItem)
        NSApp.windowsMenu = windowMenu

        NSApp.mainMenu = main
    }

    @objc private func reloadPage() {
        mainWindow.reload()
    }
}

@main
struct Main {
    static func main() {
        let app = NSApplication.shared
        let delegate = AppDelegate()
        app.delegate = delegate
        app.setActivationPolicy(.regular)
        app.run()
    }
}
