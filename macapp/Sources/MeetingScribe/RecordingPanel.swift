// The floating "recording" side panel: a small non-activating window pinned
// to the top-right corner showing the live timer, meeting title, and a Stop
// button. Appears whenever a recording is running (including ones started
// from a notification nudge) without stealing focus from the meeting app.

import AppKit

final class RecordingPanel {
    private let panel: NSPanel
    private let timerLabel = NSTextField(labelWithString: "0:00")
    private let titleLabel = NSTextField(labelWithString: "")
    private let baseURL: URL
    var onOpenApp: (() -> Void)?

    init(baseURL: URL) {
        self.baseURL = baseURL
        panel = NSPanel(
            contentRect: NSRect(x: 0, y: 0, width: 260, height: 96),
            styleMask: [.nonactivatingPanel, .titled, .fullSizeContentView],
            backing: .buffered, defer: true)
        panel.titleVisibility = .hidden
        panel.titlebarAppearsTransparent = true
        panel.standardWindowButton(.closeButton)?.isHidden = true
        panel.standardWindowButton(.miniaturizeButton)?.isHidden = true
        panel.standardWindowButton(.zoomButton)?.isHidden = true
        panel.level = .floating
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        panel.isMovableByWindowBackground = true
        panel.hidesOnDeactivate = false
        panel.becomesKeyOnlyIfNeeded = true

        let dot = NSView()
        dot.wantsLayer = true
        dot.layer?.backgroundColor = NSColor.systemRed.cgColor
        dot.layer?.cornerRadius = 4
        dot.translatesAutoresizingMaskIntoConstraints = false
        dot.widthAnchor.constraint(equalToConstant: 8).isActive = true
        dot.heightAnchor.constraint(equalToConstant: 8).isActive = true

        timerLabel.font = .monospacedDigitSystemFont(ofSize: 15, weight: .semibold)
        let recLabel = NSTextField(labelWithString: "Recording")
        recLabel.font = .systemFont(ofSize: 11, weight: .semibold)
        recLabel.textColor = .secondaryLabelColor

        titleLabel.font = .systemFont(ofSize: 12)
        titleLabel.textColor = .secondaryLabelColor
        titleLabel.lineBreakMode = .byTruncatingTail
        titleLabel.maximumNumberOfLines = 1

        let stop = NSButton(title: "Stop", target: self, action: #selector(stopTapped))
        stop.bezelStyle = .rounded
        stop.controlSize = .small
        let open = NSButton(title: "Open", target: self, action: #selector(openTapped))
        open.bezelStyle = .rounded
        open.controlSize = .small

        let topRow = NSStackView(views: [dot, recLabel, timerLabel])
        topRow.spacing = 6
        let bottomRow = NSStackView(views: [stop, open])
        bottomRow.spacing = 6
        let column = NSStackView(views: [topRow, titleLabel, bottomRow])
        column.orientation = .vertical
        column.alignment = .leading
        column.spacing = 6
        column.edgeInsets = NSEdgeInsets(top: 4, left: 14, bottom: 10, right: 14)

        panel.contentView = column
    }

    @objc private func stopTapped() {
        var request = URLRequest(url: baseURL.appendingPathComponent("api/record/stop"))
        request.httpMethod = "POST"
        URLSession.shared.dataTask(with: request).resume()
    }

    @objc private func openTapped() {
        onOpenApp?()
    }

    func show() {
        position()
        panel.orderFrontRegardless()
    }

    func hide() {
        panel.orderOut(nil)
    }

    func update(elapsed: TimeInterval, title: String?) {
        let s = Int(elapsed)
        timerLabel.stringValue = String(format: "%d:%02d", s / 60, s % 60)
        if let title, !title.isEmpty, titleLabel.stringValue != title {
            titleLabel.stringValue = title
        }
    }

    private func position() {
        guard let screen = NSScreen.main else { return }
        let f = screen.visibleFrame
        let size = panel.frame.size
        panel.setFrameOrigin(NSPoint(x: f.maxX - size.width - 16,
                                     y: f.maxY - size.height - 16))
    }
}
