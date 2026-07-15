// First-launch setup window: shows the bootstrap's live progress while the
// downloaded app builds its local environment (venv + dependencies + Speech
// helpers). Plain, honest, and non-blocking — a scrolling log the user can
// watch, then it closes itself and the app continues.

import AppKit

final class SetupWindow {
    private let window: NSWindow
    private let textView = NSTextView()
    private let spinner = NSProgressIndicator()
    private let headline = NSTextField(labelWithString: "Setting up MeetingScribe…")
    private let subhead = NSTextField(labelWithString:
        "One-time setup — installing everything locally on this Mac. A few minutes.")

    init() {
        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 560, height: 380),
            styleMask: [.titled, .fullSizeContentView],
            backing: .buffered, defer: false)
        window.title = "MeetingScribe"
        window.titlebarAppearsTransparent = true
        window.center()
        window.isReleasedWhenClosed = false

        headline.font = .systemFont(ofSize: 16, weight: .semibold)
        subhead.font = .systemFont(ofSize: 12)
        subhead.textColor = .secondaryLabelColor
        spinner.style = .spinning
        spinner.controlSize = .small
        spinner.startAnimation(nil)

        let header = NSStackView(views: [spinner, headline])
        header.spacing = 9
        header.alignment = .centerY

        let scroll = NSScrollView()
        scroll.hasVerticalScroller = true
        scroll.borderType = .noBorder
        scroll.drawsBackground = false
        textView.isEditable = false
        textView.drawsBackground = false
        textView.font = .monospacedSystemFont(ofSize: 11, weight: .regular)
        textView.textColor = .secondaryLabelColor
        scroll.documentView = textView

        let column = NSStackView(views: [header, subhead, scroll])
        column.orientation = .vertical
        column.alignment = .leading
        column.spacing = 8
        column.edgeInsets = NSEdgeInsets(top: 24, left: 24, bottom: 20, right: 24)
        column.translatesAutoresizingMaskIntoConstraints = false
        scroll.translatesAutoresizingMaskIntoConstraints = false
        let content = NSView()
        content.addSubview(column)
        NSLayoutConstraint.activate([
            column.leadingAnchor.constraint(equalTo: content.leadingAnchor),
            column.trailingAnchor.constraint(equalTo: content.trailingAnchor),
            column.topAnchor.constraint(equalTo: content.topAnchor),
            column.bottomAnchor.constraint(equalTo: content.bottomAnchor),
            scroll.widthAnchor.constraint(equalTo: column.widthAnchor, constant: -48),
        ])
        window.contentView = content
    }

    func show() {
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    func append(_ line: String) {
        textView.string += line + "\n"
        textView.scrollToEndOfDocument(nil)
    }

    func finish(success: Bool, message: String? = nil) {
        spinner.stopAnimation(nil)
        spinner.isHidden = true
        if success {
            window.close()
        } else {
            headline.stringValue = "Setup couldn't finish"
            subhead.stringValue = message ?? "See the details below."
            subhead.textColor = .systemRed
            let quit = NSButton(title: "Quit", target: NSApp,
                                action: #selector(NSApplication.terminate(_:)))
            quit.bezelStyle = .rounded
            if let column = (window.contentView?.subviews.first as? NSStackView) {
                column.addArrangedSubview(quit)
            }
        }
    }
}
