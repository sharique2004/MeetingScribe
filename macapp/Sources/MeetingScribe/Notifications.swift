// Native notifications: meeting nudges ("Join & record?") and job results
// ("Transcript ready"). Actions round-trip to the backend.

import AppKit
import UserNotifications

final class Notifications: NSObject, UNUserNotificationCenterDelegate {
    static let nudgeCategory = "MS_NUDGE"
    static let doneCategory = "MS_DONE"

    private let baseURL: URL
    var onOpenApp: (() -> Void)?

    /// UNUserNotificationCenter aborts outside a real .app bundle (swift run).
    private var usable: Bool { Bundle.main.bundleIdentifier != nil }

    init(baseURL: URL) {
        self.baseURL = baseURL
        super.init()
    }

    func setup() {
        guard usable else {
            NSLog("notifications disabled: not running from an app bundle")
            return
        }
        let center = UNUserNotificationCenter.current()
        center.delegate = self

        let record = UNNotificationAction(identifier: "RECORD_NOW", title: "Record now",
                                          options: [.foreground])
        let snooze = UNNotificationAction(identifier: "SNOOZE", title: "Not this meeting")
        let nudge = UNNotificationCategory(identifier: Self.nudgeCategory,
                                           actions: [record, snooze],
                                           intentIdentifiers: [])
        let open = UNNotificationAction(identifier: "OPEN", title: "Open",
                                        options: [.foreground])
        let done = UNNotificationCategory(identifier: Self.doneCategory,
                                          actions: [open],
                                          intentIdentifiers: [])
        center.setNotificationCategories([nudge, done])
        center.requestAuthorization(options: [.alert, .sound]) { granted, _ in
            NSLog("notification permission granted: \(granted)")
        }
    }

    func authorizationStatus(_ completion: @escaping (String) -> Void) {
        guard usable else { return completion("unavailable") }
        UNUserNotificationCenter.current().getNotificationSettings { settings in
            let status: String
            switch settings.authorizationStatus {
            case .authorized, .provisional: status = "granted"
            case .denied: status = "denied"
            default: status = "not_determined"
            }
            completion(status)
        }
    }

    func postJobDone(kind: String, meetingID: String, ok: Bool, message: String) {
        guard usable else { return }
        let content = UNMutableNotificationContent()
        if ok {
            content.title = kind == "summary" ? "Summary ready" : "Transcript ready"
            content.body = "Open MeetingScribe to read it."
        } else {
            content.title = kind == "summary" ? "Summary failed" : "Processing failed"
            content.body = message.isEmpty ? "Open MeetingScribe for details." : message
        }
        content.categoryIdentifier = Self.doneCategory
        content.userInfo = ["meeting_id": meetingID]
        let request = UNNotificationRequest(identifier: "done-\(kind)-\(meetingID)",
                                            content: content, trigger: nil)
        UNUserNotificationCenter.current().add(request)
    }

    func postNudge(id: String, title: String, body: String) {
        guard usable else { return }
        let content = UNMutableNotificationContent()
        content.title = title
        content.body = body
        content.categoryIdentifier = Self.nudgeCategory
        content.userInfo = ["nudge_id": id]
        content.interruptionLevel = .timeSensitive
        let request = UNNotificationRequest(identifier: "nudge-\(id)", content: content, trigger: nil)
        UNUserNotificationCenter.current().add(request)
    }

    // MARK: UNUserNotificationCenterDelegate

    func userNotificationCenter(_ center: UNUserNotificationCenter,
                                willPresent notification: UNNotification,
                                withCompletionHandler completionHandler:
                                    @escaping (UNNotificationPresentationOptions) -> Void) {
        completionHandler([.banner, .sound])  // show even while the app is frontmost
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter,
                                didReceive response: UNNotificationResponse,
                                withCompletionHandler completionHandler: @escaping () -> Void) {
        let info = response.notification.request.content.userInfo
        switch response.actionIdentifier {
        case "RECORD_NOW":
            post("api/nudges/\(info["nudge_id"] as? String ?? "")/accept") { [weak self] in
                self?.onOpenApp?()
            }
        case "SNOOZE":
            post("api/nudges/\(info["nudge_id"] as? String ?? "")/ack")
        default:  // notification body or "Open" clicked
            onOpenApp?()
        }
        completionHandler()
    }

    private func post(_ path: String, then completion: (() -> Void)? = nil) {
        var request = URLRequest(url: baseURL.appendingPathComponent(path))
        request.httpMethod = "POST"
        request.timeoutInterval = 5
        URLSession.shared.dataTask(with: request) { _, _, _ in
            if let completion { DispatchQueue.main.async(execute: completion) }
        }.resume()
    }
}
