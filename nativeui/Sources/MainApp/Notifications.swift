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
// The other moment worth interrupting for is the opposite one: a meeting on
// the calendar is STARTING and nothing is recording it. That question has an
// expiry date measured in minutes — asked late it is worthless — and the app
// window is exactly where the user is not looking while they join a call. The
// pill in RecorderCenter says it on screen; this says it wherever the user
// actually is, with the two buttons that answer it.
//
// The retired AppKit shell (macapp/Sources/MeetingScribe/Notifications.swift)
// had both and the SwiftUI app kept only the first. What is carried over from
// it: the userInfo keys, the action identifiers the backend's nudge contract
// is written against, the "open the meeting" action, and the hard-won guard
// that UNUserNotificationCenter aborts the process outside a real .app bundle.
import AppKit
import UserNotifications
import os

// File scope rather than members of MSNotifications: the delegate reads them
// from nonisolated callbacks, and a main-actor member cannot be touched there.
private let meetingIDKey = "meeting_id"
private let readyCategory = "MS_TRANSCRIPT"
private let openActionID = "MS_OPEN"
private let nudgeIDKey = "nudge_id"
private let nudgeCategory = "MS_NUDGE"
private let updateVersionKey = "update_version"
private let updateCategory = "MS_UPDATE"
// Spelled exactly as the legacy shell spelled them: these strings are the
// contract, and a renamed action is an action macOS silently never delivers.
private let recordNowActionID = "RECORD_NOW"
private let snoozeActionID = "SNOOZE"
/// Set once, after the launch request comes back. See requestAuthorizationIfNeeded.
private let askedDefaultsKey = "ms.notifications.asked.v1"
/// Permission that was refused is invisible from inside the app: everything
/// still "succeeds", the banners simply never appear. The log line is the
/// only way anyone ever finds out.
private let log = Logger(subsystem: "com.meetingscribe.app", category: "notifications")

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
        // The nudge's two buttons are the whole answer to the question it
        // asks, and they have to be answerable without the app: "Record now"
        // brings it forward because the user is about to want the pill and
        // the note field, "Not this meeting" is answered where it stands.
        let record = UNNotificationAction(identifier: recordNowActionID,
                                          title: "Record now", options: [.foreground])
        let snooze = UNNotificationAction(identifier: snoozeActionID,
                                          title: "Not this meeting")
        // The update notice has no actions, and should not: there is exactly
        // one thing to do with it, the banner body already says so, and a
        // "Download" button beside a banner whose whole text is "click to
        // download" is a second way to press the same thing.
        center.setNotificationCategories([
            UNNotificationCategory(identifier: readyCategory, actions: [open],
                                   intentIdentifiers: []),
            UNNotificationCategory(identifier: nudgeCategory, actions: [record, snooze],
                                   intentIdentifiers: []),
            UNNotificationCategory(identifier: updateCategory, actions: [],
                                   intentIdentifiers: []),
        ])
    }

    /// What macOS last told us about permission. Read back rather than
    /// inferred: `requestAuthorization` reports the answer to THIS request,
    /// and a user who turned notifications off in System Settings afterwards
    /// leaves no other trace.
    enum Authorization: String {
        /// Not an app bundle, so there is no such thing as permission here.
        case unavailable
        case notDetermined
        case granted
        case denied
    }

    private(set) static var authorization: Authorization = .notDetermined
    /// A request already in flight. The launch ask and the (harmless) later
    /// ones must not race each other into two prompts.
    private static var asking = false

    /// Ask, once, and remember that we asked.
    ///
    /// Called from applicationDidFinishLaunching, the way the legacy shell
    /// asked. It used to wait for the first recording to END, on the reasoning
    /// that the app had nothing to tell anyone about until then — but the
    /// meeting nudge is precisely a thing worth telling someone about BEFORE
    /// they have ever recorded anything, and permission that is asked for
    /// after the first meeting has already started is permission that arrives
    /// one meeting too late.
    ///
    /// The flag is written in the completion rather than before the request:
    /// a request that never returns an answer (the prompt dismissed by a
    /// crash, an app that quit while it was up) is not an app that has asked.
    static func requestAuthorizationIfNeeded() {
        guard usable, !asking, !UserDefaults.standard.bool(forKey: askedDefaultsKey) else {
            // Already asked at some point: the answer can still have changed
            // in System Settings since, so read it back either way.
            if usable, !asking { refreshAuthorization() }
            return
        }
        asking = true
        UNUserNotificationCenter.current()
            .requestAuthorization(options: [.alert, .sound]) { granted, error in
                // Hop with plain values: neither the settings object nor the
                // error is worth carrying across the isolation boundary.
                let message = error?.localizedDescription
                Task { @MainActor in
                    asking = false
                    UserDefaults.standard.set(true, forKey: askedDefaultsKey)
                    if let message {
                        log.error("notification permission request failed: \(message, privacy: .public)")
                    }
                    authorization = granted ? .granted : .denied
                    if !granted {
                        log.notice("notification permission was not granted; meeting nudges and transcript notices will not appear")
                    }
                    refreshAuthorization()
                }
            }
    }

    /// Ask macOS what the current answer is and remember it.
    static func refreshAuthorization() {
        guard usable else {
            authorization = .unavailable
            return
        }
        UNUserNotificationCenter.current().getNotificationSettings { settings in
            let status: Authorization
            switch settings.authorizationStatus {
            case .authorized, .provisional, .ephemeral: status = .granted
            case .denied: status = .denied
            default: status = .notDetermined
            }
            Task { @MainActor in
                authorization = status
                if status == .denied {
                    log.notice("notifications are denied for MeetingScribe; nudges and transcript notices are being posted into a void")
                }
            }
        }
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

    /// A meeting on the calendar is starting and nothing is recording it.
    ///
    /// The one notification in this app that is allowed to interrupt: it is
    /// time-sensitive in the literal sense macOS means by it — worth breaking
    /// a Focus for, worthless five minutes later — and it is posted while the
    /// user is joining a call, which is to say while they are looking at
    /// anything except this window.
    static func postNudge(id: String, title: String, body: String) {
        guard usable, !id.isEmpty else { return }
        let content = UNMutableNotificationContent()
        content.title = title.isEmpty ? "A meeting is starting" : title
        content.body = body.isEmpty ? "Start recording?" : body
        content.sound = .default
        content.categoryIdentifier = nudgeCategory
        content.userInfo = [nudgeIDKey: id]
        // Everything said about one nudge stacks under that nudge, and the
        // request id is derived from it too — so an engine that re-offers the
        // same meeting REPLACES its own banner instead of stacking a second
        // copy of the same question behind the first.
        content.threadIdentifier = id
        content.interruptionLevel = .timeSensitive
        let request = UNNotificationRequest(identifier: nudgeRequestID(id),
                                            content: content, trigger: nil)
        UNUserNotificationCenter.current().add(request)
    }

    /// The question has been answered — by either of its buttons, by the pill
    /// in the app, or by a recording starting. Take the banner down, and the
    /// copy sitting in Notification Center with it: a "shall I record this?"
    /// the user can still press at four in the afternoon is a trap.
    static func removeNudge(id: String) {
        guard usable, !id.isEmpty else { return }
        let center = UNUserNotificationCenter.current()
        center.removeDeliveredNotifications(withIdentifiers: [nudgeRequestID(id)])
        center.removePendingNotificationRequests(withIdentifiers: [nudgeRequestID(id)])
    }

    /// Namespaced so a nudge id can never collide with a meeting id in the
    /// same identifier space (see postTranscriptReady).
    private static func nudgeRequestID(_ id: String) -> String { "nudge-\(id)" }

    /// A newer MeetingScribe exists.
    ///
    /// The quietest thing this file posts, and the only one that is not about
    /// a meeting: no sound, .passive, so it lands in Notification Center
    /// without taking the screen. The two banners above interrupt because they
    /// are worth minutes; a release is worth a day, and one that shouts over a
    /// call would make the two that matter easier to ignore.
    ///
    /// UpdateChecker posts this at most once per version, ever — see
    /// notifyOnce, which is where that promise is kept, not here.
    static func postUpdateAvailable(version: String) {
        guard usable, !version.isEmpty else { return }
        let content = UNMutableNotificationContent()
        content.title = "MeetingScribe \(version) is available"
        // What the user is actually deciding, which is not "shall I read a
        // changelog" but "is this safe": a local-first recorder's update is
        // frightening in exactly one way, and the answer is one clause long.
        content.body = "Click to download it. Your meetings and settings stay where they are."
        content.categoryIdentifier = updateCategory
        content.userInfo = [updateVersionKey: version]
        content.threadIdentifier = updateCategory
        content.interruptionLevel = .passive
        // One identifier for all versions, not one per version: the only thing
        // worse than an unread update banner is two of them, and if 3.3 ships
        // while the 3.2 notice is still sitting in Notification Center the
        // newer one should REPLACE it rather than queue behind a notice that
        // is now wrong.
        let request = UNNotificationRequest(identifier: "update-available",
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

    /// What the user did with a meeting nudge.
    ///
    /// Both answers go through RecorderCenter rather than straight at the
    /// engine: it is the one owner of nudge state, so pressing "Record now"
    /// on a banner and pressing Record on the pill are the same act — same
    /// request, same pill, and the same sentence shown if the engine refuses.
    /// A refusal fired from a banner used to be nobody's to report.
    fileprivate static func handleNudge(action: String, id: String) {
        switch action {
        case recordNowActionID:
            RecorderCenter.shared.acceptNudge(id: id)
            // The action is .foreground, so macOS is already bringing the app
            // up; this is for the window that is behind everything or closed.
            open(meetingID: nil)
        case snoozeActionID:
            RecorderCenter.shared.dismissNudge(id: id)
        case UNNotificationDismissActionIdentifier:
            // Swept out of Notification Center. Our category does not ask for
            // this action so macOS does not currently send it — but clearing a
            // banner is housekeeping, not an answer, and must never be filed
            // as one on the user's behalf.
            break
        default:
            // The body was clicked. It is not either of the two answers, so
            // the nudge stays standing and the app just comes forward: the
            // user asked to look, not to decide.
            open(meetingID: nil)
        }
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
        // be decided honestly: a transcript notice is not posted while the app
        // is active (see Library.watchWorkInFlight), and a meeting nudge is
        // posted deliberately whatever is frontmost — the window can be open
        // on another Space, behind the call, or closed to the Dock, and a
        // meeting that starts unrecorded is not made less urgent by an app
        // that happens to be running. Anything that reaches here is shown.
        completionHandler([.banner, .sound])
    }

    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        didReceive response: UNNotificationResponse,
        withCompletionHandler completionHandler: @escaping () -> Void
    ) {
        // Read on this thread and hop with plain Strings: capturing the
        // response, or the completion handler, would carry a non-Sendable
        // value across the isolation boundary for no gain.
        let info = response.notification.request.content.userInfo
        let action = response.actionIdentifier
        if info[updateVersionKey] != nil {
            // Straight to the download, not into the app: the app the user
            // would be brought forward into is the old one, and the thing the
            // banner offered lives on the web. Only a real click counts —
            // sweeping the banner out of Notification Center is housekeeping,
            // and housekeeping must not open a browser.
            if action == UNNotificationDefaultActionIdentifier {
                Task { @MainActor in UpdateChecker.openDownload() }
            }
            completionHandler()
            return
        }
        if let nudgeID = info[nudgeIDKey] as? String {
            Task { @MainActor in MSNotifications.handleNudge(action: action, id: nudgeID) }
            completionHandler()
            return
        }
        let id = info[meetingIDKey] as? String
        Task { @MainActor in MSNotifications.open(meetingID: id) }
        completionHandler()
    }
}
