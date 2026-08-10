// Ask, surviving the page. The conversation used to be owned by the floating
// bar, so navigating to Today — or to any other meeting — destroyed it, and
// an answer still being written was thrown away with it. AskCenter owns one
// AskConversation per meeting for the life of the app: the bar borrows it,
// the answer keeps streaming wherever the user goes, and the engine persists
// every settled exchange to the meeting's qa.json so the thread is still
// there after a relaunch. It also remembers which meetings finished an
// answer while their page was off screen, which is what the sidebar's
// "answer ready" mark and the bar's open-on-return read.
import SwiftUI

@MainActor
final class AskCenter: ObservableObject {
    static let shared = AskCenter()

    /// Meetings whose answer settled while nobody was looking at the thread —
    /// the page was off screen, or on screen with the panel closed.
    @Published private(set) var unread: Set<String> = []

    /// The meeting page currently on screen; nil on Today and Settings.
    private(set) var visibleMeeting: String?
    /// How many live instances of each meeting page exist right now. A page
    /// reload under a new SwiftUI identity runs the new `.task` and the old
    /// `.onDisappear` in no guaranteed order, so a plain nil-out would let
    /// the outgoing copy blank a meeting the incoming copy just installed.
    private var livePages: [String: Int] = [:]
    /// Meetings whose ask thread is open on screen.
    private var openThreads: Set<String> = []

    private var conversations: [String: AskConversation] = [:]

    /// The one conversation for this meeting, created on first use and kept
    /// for the life of the app. Stable identity is the entire point: the bar
    /// observes whatever is already here, including an answer that has been
    /// streaming since three pages ago.
    func conversation(for id: String) -> AskConversation {
        if let existing = conversations[id] { return existing }
        let fresh = AskConversation(meetingID: id, center: self)
        conversations[id] = fresh
        return fresh
    }

    func pageAppeared(_ id: String) {
        livePages[id, default: 0] += 1
        visibleMeeting = id
    }

    func pageDisappeared(_ id: String) {
        let remaining = max(0, (livePages[id] ?? 1) - 1)
        livePages[id] = remaining
        if remaining == 0, visibleMeeting == id { visibleMeeting = nil }
    }

    /// The ask panel for this meeting opened or closed on screen.
    func threadOpened(_ id: String) {
        openThreads.insert(id)
        unread.remove(id)
    }

    func threadClosed(_ id: String) {
        openThreads.remove(id)
    }

    /// True exactly once per unread answer: the caller opens the thread, and
    /// the sidebar's mark goes out in the same breath.
    func consumeUnread(_ id: String) -> Bool {
        unread.remove(id) != nil
    }

    /// An answer (or its failure) landed. Called by the conversation itself.
    /// The mark keys on whether the THREAD is on screen, not just the page: an
    /// answer settling behind a closed panel is exactly as unseen as one
    /// settling three pages away.
    func noteSettled(_ id: String) {
        if visibleMeeting != id || !openThreads.contains(id) {
            unread.insert(id)
        }
    }
}
