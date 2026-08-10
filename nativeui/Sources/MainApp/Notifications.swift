// The one thing this app never said out loud.
//
// A 45-minute meeting takes about fifteen minutes to transcribe. Nothing
// announced the end of that: no notification, no badge, no sound. The user
// pressed stop, went somewhere else, and the only way to learn the transcript
// existed was to come back and look. So: one notification, at the one moment
// worth interrupting for, and only when the user is somewhere else — with the
// app in front of them the sidebar row changing under their eyes has already
// said it, and a banner on top would be the app telling them what they are
// looking at.
//
// The retired AppKit shell (macapp/Sources/MeetingScribe/Notifications.swift)
// had this and the SwiftUI app lost it. What is kept from it: the userInfo
// key, the "open the meeting" action, and the hard-won guard that
// UNUserNotificationCenter aborts the process outside a real .app bundle.
import AppKit
import UserNotifications

// File scope rather than members of MSNotifications: the delegate reads them
// from nonisolated callbacks, and a main-actor member cannot be touched there.
private let meetingIDKey = "meeting_id"
private let readyCategory = "MS_TRANSCRIPT"
private let openActionID = "MS_OPEN"
/// Set once, the first time a recording ends. See requestAuthorizationIfNeeded.
private let askedDefaultsKey = "ms.notifications.asked.v1"

extension Notification.Name {
    /// A notification the user clicked. `object` is the meeting id (a `String`).
    ///
    /// Not an `MSCommand`: every command in Commands.swift acts on whatever is
    /// already selected, and this one carries its own subject. It lands in
    /// ContentView, which owns `route` — the same destination the `--open <id>`
    /// launch argument reaches.
    static let msOpenMeeting = Notification.Name("MeetingScribe.openMeeting")
}

@MainActor
enum MSNotifications {
    /// A meeting a notification asked for before anything was listening: a
    /// click that LAUNCHED the app rather than switching to it runs the
    /// delegate before ContentView exists. Drained once, on first appearance.
    private static var pendingMeetingID: String?

    /// UNUserNotificationCenter traps outside a real .app bundle (`swift run`,
    /// a bare swiftc binary), which is how the app is built during
    /// development. Every entry point checks this first.
    private static var usable: Bool { Bundle.main.bundleIdentifier != nil }

    // MARK: - Wiring

    /// Take delivery of clicks. Called from applicationDidFinishLaunching, so
    /// a notification that launched the app is not dropped on the floor.
    static func install() {
        guard usable else { return }
        let center = UNUserNotificationCenter.current()
        center.delegate = delegate
        // One action, spelled the way the banner's own button should read.
        // Clicking the body does the same thing; this is for the expanded
        // banner and for VoiceOver, which reads actions and not bodies.
        let open = UNNotificationAction(identifier: openActionID,
                                        title: "Open Meeting", options: [.foreground])
        center.setNotificationCategories([
            UNNotificationCategory(identifier: readyCategory, actions: [open],
                                   intentIdentifiers: []),
        ])
    }

    /// Ask, once, and remember that we asked.
    ///
    /// Called the first time a recording STOPS rather than at launch: at
    /// launch the app has nothing to tell anyone about and the question is
    /// unanswerable noise, while at the end of a recording the app is about to
    /// spend fifteen minutes transcribing and "may I tell you when it's done"
    /// answers itself.
    ///
    /// The flag is written before the request, not in its completion: macOS
    /// shows this prompt exactly once per app whatever we do, so a second
    /// request would be silent anyway, and a flag written only on success
    /// would mean re-asking every recording of a user who said no.
    static func requestAuthorizationIfNeeded() {
        guard usable, !UserDefaults.standard.bool(forKey: askedDefaultsKey) else { return }
        UserDefaults.standard.set(true, forKey: askedDefaultsKey)
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound]) { _, _ in }
    }

    // MARK: - Posting

    /// A meeting finished transcribing, or failed trying.
    ///
    /// `message` is the engine's own sentence for a failure ("Interrupted —
    /// press Reprocess to transcribe the saved audio"), which is worth more
    /// than anything this app could invent about a run it did not watch.
    static func postTranscriptReady(meetingID: String, title: String,
                                    ok: Bool, message: String? = nil) {
        guard usable, !meetingID.isEmpty else { return }
        let name = title.trimmingCharacters(in: .whitespacesAndNewlines)
        let reason = (message ?? "").trimmingCharacters(in: .whitespacesAndNewlines)

        let content = UNMutableNotificationContent()
        content.title = ok ? "Transcript ready" : "Transcription failed"
        if ok {
            // The meeting's own name is the body: on a day with three
            // recordings it is the only part of this that says WHICH one.
            content.body = name.isEmpty ? "Your meeting is ready to read." : name
        } else {
            // Which meeting, then why, then what to do about it. Maximum
            // helpfulness and zero whimsy is the rule when something is wrong.
            if !name.isEmpty { content.subtitle = name }
            content.body = reason.isEmpty
                ? "The audio is saved. Open the meeting and press Reprocess."
                : reason
        }
        content.sound = .default
        content.categoryIdentifier = readyCategory
        content.userInfo = [meetingIDKey: meetingID]
        // Everything the app ever says about one meeting stacks under that
        // meeting rather than under the app.
        content.threadIdentifier = meetingID

        // The id is per meeting, so a reprocess replaces the notice it is
        // redoing instead of leaving two contradictory ones in the centre.
        let request = UNNotificationRequest(identifier: "transcript-\(meetingID)",
                                            content: content, trigger: nil)
        UNUserNotificationCenter.current().add(request)
    }

    // MARK: - Clicks

    /// Bring the app forward and ask for this meeting.
    fileprivate static func open(meetingID: String?) {
        NSApp.activate()
        // A click already activates a running app; this is for the window
        // that is behind everything or closed down to the Dock.
        NSApp.windows.first { $0.canBecomeMain }?.makeKeyAndOrderFront(nil)
        guard let meetingID, !meetingID.isEmpty else { return }
        pendingMeetingID = meetingID
        NotificationCenter.default.post(name: .msOpenMeeting, object: meetingID)
    }

    /// The meeting a cold-launch click asked for, once.
    static func takePendingMeeting() -> String? {
        defer { pendingMeetingID = nil }
        return pendingMeetingID
    }

    /// A live view took the click off the broadcast, so there is nothing left
    /// waiting for the launch drain to find.
    static func clearPendingMeeting() {
        pendingMeetingID = nil
    }

    private static let delegate = MSNotificationDelegate()
}

/// The delegate has to be an object macOS can hold weakly, so it cannot be the
/// enum above. It does nothing but translate a click into a main-actor call.
private final class MSNotificationDelegate: NSObject, UNUserNotificationCenterDelegate {
    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification,
        withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void
    ) {
        // Whether the user is looking at the app is decided ONCE, where it can
        // be decided honestly: nothing is posted while the app is active (see
        // Library.watchWorkInFlight). Anything that reaches here has already
        // passed that test, so it is shown.
        completionHandler([.banner, .sound])
    }

    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        didReceive response: UNNotificationResponse,
        withCompletionHandler completionHandler: @escaping () -> Void
    ) {
        // Read on this thread and hop with a plain String: capturing the
        // response, or the completion handler, would carry a non-Sendable
        // value across the isolation boundary for no gain.
        let id = response.notification.request.content.userInfo[meetingIDKey] as? String
        Task { @MainActor in MSNotifications.open(meetingID: id) }
        completionHandler()
    }
}
