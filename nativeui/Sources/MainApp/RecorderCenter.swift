// The one owner of recorder truth for the whole app: polls the engine for
// recorder state, meeting-start nudges, and live captions; exposes actions
// (start/stop/accept/dismiss/note). The window and the floating pill both
// read from here, so they can never disagree.
import Foundation
import Combine

struct HUDNote: Identifiable {
    let id = UUID()
    let t: Double?
    let text: String
}

@MainActor
final class RecorderCenter: ObservableObject {
    enum Phase: Equatable { case offline, idle, recording }

    @Published private(set) var phase: Phase = .offline
    @Published private(set) var elapsed: Double = 0
    @Published private(set) var meetingId: String?
    @Published private(set) var micLevel: Double = 0
    @Published private(set) var systemLevel: Double = 0
    @Published private(set) var nudge: Nudge?
    @Published private(set) var stopping = false
    @Published private(set) var liveTurns: [LiveTurn] = []
    @Published private(set) var livePartials: [String: LivePartial] = [:]
    @Published var sentNotes: [HUDNote] = []
    @Published var noteDraft = ""
    @Published var noteSendFailed = false

    /// Fired on the idle→recording transition (minimize to the pill).
    var onRecordingStarted: (() -> Void)?
    /// Fired on the recording→idle transition.
    var onRecordingStopped: (() -> Void)?

    private var liveSeq = 0
    private var pollTask: Task<Void, Never>?
    private var nudgeSeenAt: Date?
    private var expiredNudgeIds = Set<String>()

    init() {
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                await self?.tick()
                let recording = self?.phase == .recording
                try? await Task.sleep(nanoseconds: recording ? 350_000_000 : 1_200_000_000)
            }
        }
    }

    private func tick() async {
        guard let st = try? await API.recorderStatus() else {
            phase = .offline
            nudge = nil
            return
        }
        if st.recording {
            if phase != .recording {   // just started (by us, the nudge, or elsewhere)
                liveSeq = 0
                liveTurns = []
                livePartials = [:]
                sentNotes = []
                nudge = nil
                phase = .recording
                onRecordingStarted?()
            }
            elapsed = st.elapsed ?? elapsed
            micLevel = st.levels?["mic"] ?? 0
            systemLevel = st.levels?["system"] ?? 0
            if let id = st.meeting_id, id != meetingId { meetingId = id }
            if let snap = try? await API.live(since: liveSeq) {
                if !snap.turns.isEmpty { liveTurns.append(contentsOf: snap.turns) }
                livePartials = snap.partials
                liveSeq = max(liveSeq, snap.seq)
            }
        } else {
            if phase == .recording {
                phase = .idle
                meetingId = nil
                onRecordingStopped?()
            } else {
                phase = .idle
            }
            elapsed = 0
            // Nudges auto-expire after 90s rather than nagging.
            let fresh = (try? await API.nudge()) ?? nil
            if let fresh {
                if expiredNudgeIds.contains(fresh.id) {
                    nudge = nil
                } else if nudge?.id != fresh.id {
                    nudge = fresh
                    nudgeSeenAt = Date()
                } else if let seen = nudgeSeenAt, Date().timeIntervalSince(seen) > 90 {
                    expiredNudgeIds.insert(fresh.id)
                    nudge = nil
                }
            } else {
                nudge = nil
            }
        }
    }

    // MARK: - Actions

    func startRecording(title: String? = nil) {
        Task {
            var body: [String: Any] = [:]
            if let title, !title.isEmpty { body["title"] = title }
            await API.post("api/record/start", body: body)
            await tick()
        }
    }

    func stopRecording() {
        stopping = true
        Task {
            await API.post("api/record/stop")
            await tick()
            stopping = false
        }
    }

    func accept(_ n: Nudge) {
        Task {
            await API.post("api/nudges/\(n.id)/accept")
            nudge = nil
            await tick()
        }
    }

    func dismiss(_ n: Nudge) {
        Task {
            await API.post("api/nudges/\(n.id)/ack")
            nudge = nil
        }
    }

    func sendNote() {
        let text = noteDraft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, let meetingId else { return }
        let t = elapsed
        Task {
            let ok = await API.post("api/record/note",
                                    body: ["text": text, "t": t, "meeting_id": meetingId])
            if ok {
                sentNotes.append(HUDNote(t: t, text: text))
                noteDraft = ""
                noteSendFailed = false
            } else {
                noteSendFailed = true   // the draft stays — the user still holds the text
            }
        }
    }
}
