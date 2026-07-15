// Menu-bar presence: mic icon (red badge while recording) + quick actions.

import AppKit

final class StatusItem {
    private let item: NSStatusItem
    private let baseURL: URL
    private var recording = false
    var onOpenApp: (() -> Void)?

    init(baseURL: URL) {
        self.baseURL = baseURL
        item = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        applyIcon()
        item.menu = buildMenu()
    }

    func setRecording(_ on: Bool) {
        guard on != recording else { return }
        recording = on
        applyIcon()
        item.menu = buildMenu()
    }

    private func applyIcon() {
        guard let button = item.button else { return }
        let name = recording ? "record.circle.fill" : "mic.fill"
        let image = NSImage(systemSymbolName: name, accessibilityDescription: "MeetingScribe")
        if recording {
            let config = NSImage.SymbolConfiguration(paletteColors: [.systemRed])
            button.image = image?.withSymbolConfiguration(config)
            button.image?.isTemplate = false
        } else {
            button.image = image
            button.image?.isTemplate = true
        }
    }

    private func buildMenu() -> NSMenu {
        let menu = NSMenu()
        let open = NSMenuItem(title: "Open MeetingScribe", action: #selector(openApp), keyEquivalent: "o")
        open.target = self
        menu.addItem(open)
        menu.addItem(.separator())
        if recording {
            let stop = NSMenuItem(title: "Stop & Transcribe", action: #selector(stopRecording), keyEquivalent: "")
            stop.target = self
            menu.addItem(stop)
        } else {
            let start = NSMenuItem(title: "Start Recording", action: #selector(startRecording), keyEquivalent: "")
            start.target = self
            menu.addItem(start)
        }
        menu.addItem(.separator())
        let quit = NSMenuItem(title: "Quit MeetingScribe", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        menu.addItem(quit)
        return menu
    }

    @objc private func openApp() { onOpenApp?() }

    @objc private func startRecording() {
        post("api/record/start", body: ["title": "", "mode": "online"])
    }

    @objc private func stopRecording() {
        post("api/record/stop", body: [:])
    }

    private func post(_ path: String, body: [String: Any]) {
        var request = URLRequest(url: baseURL.appendingPathComponent(path))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try? JSONSerialization.data(withJSONObject: body)
        URLSession.shared.dataTask(with: request).resume()
    }
}
