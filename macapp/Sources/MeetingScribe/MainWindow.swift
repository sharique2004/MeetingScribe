// The main window: a WKWebView onto the local web UI, with native handling
// for JS dialogs (the UI uses confirm/alert) and file downloads (exports).

import AppKit
import WebKit

final class MainWindow: NSObject, WKUIDelegate, WKNavigationDelegate, WKDownloadDelegate,
    NSWindowDelegate {
    private let window: NSWindow
    private let webView: WKWebView
    private let baseURL: URL
    private var loadedApp = false

    init(baseURL: URL) {
        self.baseURL = baseURL
        let config = WKWebViewConfiguration()
        webView = WKWebView(frame: .zero, configuration: config)
        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1240, height: 820),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered, defer: false)
        super.init()
        window.title = "MeetingScribe"
        window.minSize = NSSize(width: 880, height: 560)
        window.center()
        window.setFrameAutosaveName("MeetingScribeMain")
        window.contentView = webView
        window.isReleasedWhenClosed = false
        window.delegate = self
        webView.uiDelegate = self
        webView.navigationDelegate = self
        showStartingPage()
    }

    func show() {
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    /// Load the web app once the backend answers; safe to call repeatedly.
    func backendBecameHealthy() {
        guard !loadedApp else { return }
        loadedApp = true
        webView.load(URLRequest(url: baseURL))
    }

    func backendWentDown() {
        loadedApp = false
        showStartingPage()
    }

    func reload() {
        if loadedApp { webView.reload() } else { showStartingPage() }
    }

    private func showStartingPage() {
        let html = """
        <!doctype html><meta charset="utf-8">
        <body style="margin:0;display:grid;place-items:center;height:100vh;\
        background:#101014;color:#8b8b96;font:14px -apple-system">
        <div style="text-align:center"><div style="font-size:15px;color:#ececf1;\
        font-weight:650;margin-bottom:6px">MeetingScribe is starting…</div>
        <div>The on-device engine is warming up.</div></div>
        """
        webView.loadHTMLString(html, baseURL: nil)
    }

    // MARK: JS dialogs (the web UI uses alert/confirm)

    func webView(_ webView: WKWebView,
                 runJavaScriptAlertPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo,
                 completionHandler: @escaping () -> Void) {
        let alert = NSAlert()
        alert.messageText = "MeetingScribe"
        alert.informativeText = message
        alert.runModal()
        completionHandler()
    }

    func webView(_ webView: WKWebView,
                 runJavaScriptConfirmPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo,
                 completionHandler: @escaping (Bool) -> Void) {
        let alert = NSAlert()
        alert.messageText = "MeetingScribe"
        alert.informativeText = message
        alert.addButton(withTitle: "OK")
        alert.addButton(withTitle: "Cancel")
        completionHandler(alert.runModal() == .alertFirstButtonReturn)
    }

    func webView(_ webView: WKWebView,
                 runJavaScriptTextInputPanelWithPrompt prompt: String,
                 defaultText: String?,
                 initiatedByFrame frame: WKFrameInfo,
                 completionHandler: @escaping (String?) -> Void) {
        let alert = NSAlert()
        alert.messageText = prompt
        let field = NSTextField(frame: NSRect(x: 0, y: 0, width: 260, height: 24))
        field.stringValue = defaultText ?? ""
        alert.accessoryView = field
        alert.addButton(withTitle: "OK")
        alert.addButton(withTitle: "Cancel")
        completionHandler(alert.runModal() == .alertFirstButtonReturn ? field.stringValue : nil)
    }

    // MARK: downloads (transcript exports)

    func webView(_ webView: WKWebView,
                 decidePolicyFor navigationResponse: WKNavigationResponse,
                 decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) {
        if !navigationResponse.canShowMIMEType {
            decisionHandler(.download)
        } else {
            decisionHandler(.allow)
        }
    }

    func webView(_ webView: WKWebView, navigationResponse: WKNavigationResponse,
                 didBecome download: WKDownload) {
        download.delegate = self
    }

    func download(_ download: WKDownload, decideDestinationUsing response: URLResponse,
                  suggestedFilename: String,
                  completionHandler: @escaping (URL?) -> Void) {
        let downloads = FileManager.default.urls(for: .downloadsDirectory, in: .userDomainMask).first!
        var target = downloads.appendingPathComponent(suggestedFilename)
        var n = 2
        while FileManager.default.fileExists(atPath: target.path) {
            let stem = (suggestedFilename as NSString).deletingPathExtension
            let ext = (suggestedFilename as NSString).pathExtension
            target = downloads.appendingPathComponent("\(stem) \(n).\(ext)")
            n += 1
        }
        completionHandler(target)
    }

    func downloadDidFinish(_ download: WKDownload) {
        NSSound(named: "Glass")?.play()
    }

    // Closing the window keeps the app (and any recording) alive in the
    // menu bar; clicking the Dock icon brings it back.
    func windowShouldClose(_ sender: NSWindow) -> Bool {
        window.orderOut(nil)
        return false
    }
}
