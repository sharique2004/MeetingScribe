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
    private var notesPanel: NotesPanel!
    private var notifications: Notifications!
    private var statusPoller: StatusPoller!
    private var nudgePoller: NudgePoller!
    private var setupWindow: SetupWindow?
    private var notesMenuItem: NSMenuItem?
    private var router: RecorderRouter!

    func applicationDidFinishLaunching(_ notification: Notification) {
        backend = BackendManager()
        mainWindow = MainWindow(baseURL: backend.baseURL)
        statusItem = StatusItem(baseURL: backend.baseURL)
        recordingPanel = RecordingPanel(baseURL: backend.baseURL)
        notesPanel = NotesPanel(baseURL: backend.baseURL)
        notifications = Notifications(baseURL: backend.baseURL)
        statusPoller = StatusPoller(baseURL: backend.baseURL)
        nudgePoller = NudgePoller(baseURL: backend.baseURL, notifications: notifications)

        let openApp: () -> Void = { [weak self] in self?.mainWindow.show() }
        statusItem.onOpenApp = openApp
        recordingPanel.onOpenApp = openApp
        notifications.onOpenApp = openApp

        // The note-taker stamps each note with the HUD's interpolated clock, so
        // both panels agree on how far into the meeting we are — and asks
        // whether that clock is still running, because a reading taken after
        // Stop is a frozen one and must not be sent as a live timestamp.
        notesPanel.currentElapsed = { [weak self] in self?.recordingPanel.currentElapsed ?? 0 }
        notesPanel.clockIsLive = { [weak self] in self?.recordingPanel.isLive ?? false }
        notesPanel.onVisibilityChange = { [weak self] visible in
            self?.statusItem.setNotesVisible(visible)
            self?.notesMenuItem?.state = visible ? .on : .off
        }
        // Asking for the note-taker by name means you want to type in it, so
        // this path takes the keyboard deliberately (Escape hands it back).
        statusItem.onToggleNotes = { [weak self] in self?.notesPanel.toggleFromUserGesture() }

        backend.onHealthChange = { [weak self] healthy in
            if healthy {
                self?.mainWindow.backendBecameHealthy()
            } else {
                self?.mainWindow.backendWentDown()
            }
        }
        // Fresh download: build the local environment once, watching progress.
        backend.onNeedsBootstrap = { [weak self] in
            guard let self, self.setupWindow == nil else { return }
            let win = SetupWindow()
            self.setupWindow = win
            win.show()
            self.backend.runBootstrap(
                onLine: { win.append($0) },
                onDone: { ok, message in
                    if ok {
                        win.finish(success: true)
                        self.setupWindow = nil
                        self.mainWindow.show()  // backend spawns on the next health tick
                    } else {
                        win.finish(success: false, message: message)
                    }
                })
        }

        // Which meeting the note-taker files against, and whether its panel is
        // on screen, are two different decisions taken from one snapshot —
        // RecorderRouter is where that split lives and why it has to exist.
        router = RecorderRouter(sinks: RecorderRouter.Sinks(
            menuBarRecording: { [weak self] on in self?.statusItem.setRecording(on) },
            hudUpdate: { [weak self] state in
                self?.recordingPanel.update(elapsed: state.elapsed, title: nil)
            },
            hudShow: { [weak self] in self?.recordingPanel.show() },
            hudHide: { [weak self] in self?.recordingPanel.hide() },
            meetingChanged: { [weak self] id in self?.notesPanel.meetingChanged(to: id) },
            showNotes: { [weak self] in self?.notesPanel.show() },
            recordingStopped: { [weak self] in self?.notesPanel.recordingStopped() },
            hideMainWindow: { [weak self] in self?.mainWindow.hide() },
            showMainWindow: { [weak self] in self?.mainWindow.show() }))

        statusPoller.onRecorderChange = { [weak self] state in
            self?.router.apply(state)
        }
        statusPoller.onRecorderTick = { [weak self] state, title in
            self?.router.tick(state)
            self?.recordingPanel.update(elapsed: state.elapsed, title: title)
        }
        // The fast identity route: the HUD polls the recorder five times a
        // second for its clock, and that response carries the meeting id. Same
        // sample, so `t` and `meeting_id` can never disagree, and the handover
        // is noticed in ~200 ms rather than up to 2 s.
        recordingPanel.onLiveMeetingID = { [weak self] id in
            self?.router.liveMeetingID(id)
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
        // `router` is built in applicationDidFinishLaunching; a quit that beats
        // it there has nothing to lose, so treat "no router yet" as not
        // recording rather than trapping on the way out.
        if router?.last.recording == true {
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
        let notes = NSMenuItem(title: "Notes Panel", action: #selector(toggleNotesPanel),
                               keyEquivalent: "")
        notes.target = self
        // ⌥⌘N is normally claimed system-wide by the Carbon hotkey so it works
        // mid-call while another app is frontmost. Only put it on the menu item
        // when that registration failed, or both would fire and cancel out.
        if !notesPanel.hotkeyRegistered {
            notes.keyEquivalent = "n"
            notes.keyEquivalentModifierMask = [.command, .option]
        }
        notes.state = notesPanel.isVisible ? .on : .off
        notesMenuItem = notes
        viewMenu.addItem(notes)
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

    @objc private func toggleNotesPanel() {
        notesPanel.toggleFromUserGesture()
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
