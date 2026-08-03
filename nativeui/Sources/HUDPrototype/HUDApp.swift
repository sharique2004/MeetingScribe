// Prototype entry point: a borderless non-activating floating panel hosting
// the SwiftUI capsule. The panel resizes to follow the capsule's measured
// size with its TOP edge pinned, so the notes dropdown grows downward and
// only the visible glass ever intercepts clicks.
import AppKit
import SwiftUI

@main
struct HUDPrototypeMain {
    static func main() {
        let app = NSApplication.shared
        let delegate = AppDelegate()
        app.delegate = delegate
        app.setActivationPolicy(.accessory)
        app.run()
    }
}

final class HUDPanel: NSPanel {
    // Borderless panels refuse key status by default; the notes editor needs it.
    override var canBecomeKey: Bool { true }
}

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    private var panel: HUDPanel!
    private let model = CapsuleModel()

    func applicationDidFinishLaunching(_ note: Notification) {
        let panel = HUDPanel(
            contentRect: NSRect(x: 0, y: 0, width: 140, height: 56),
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered, defer: false)
        panel.isFloatingPanel = true
        panel.level = .floating
        panel.backgroundColor = .clear
        panel.isOpaque = false
        panel.hasShadow = false
        panel.isMovableByWindowBackground = true
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        panel.becomesKeyOnlyIfNeeded = true
        panel.hidesOnDeactivate = false

        let root = CapsuleRoot(model: model) { [weak panel] size in
            guard let panel, panel.frame.size != size else { return }
            let f = panel.frame
            panel.setFrame(
                NSRect(x: f.origin.x, y: f.maxY - size.height,
                       width: size.width, height: size.height),
                display: true)
        }
        let hosting = NSHostingView(rootView: root)
        hosting.sizingOptions = []
        panel.contentView = hosting

        if let screen = NSScreen.main {
            let v = screen.visibleFrame
            panel.setFrameTopLeftPoint(
                NSPoint(x: v.midX - panel.frame.width / 2, y: v.maxY - 8))
        }
        panel.orderFrontRegardless()
        self.panel = panel
    }
}
