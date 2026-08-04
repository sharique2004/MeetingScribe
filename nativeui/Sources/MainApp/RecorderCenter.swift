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
    /// The live note field. Every change reaches the disk (see NoteDrafts) —
    /// this used to be memory only, so Stop, quit and crash each threw away
    /// whatever was half-typed, and the leftover text was inherited by the
    /// NEXT meeting and could be filed as that one's note.
    @Published var noteDraft = "" { didSet { draftChanged(from: oldValue) } }
    /// The engine could not be reached, so notes are queueing on disk.
    @Published var noteSendFailed = false
    /// The engine REFUSED a note and always will (too long, or its meeting is
    /// gone). Nothing is retrying it, so this has to be said out loud rather
    /// than left to look like a slow send.
    @Published var noteRefusal: String?

    /// Fired on the idle→recording transition (minimize to the pill).
    var onRecordingStarted: (() -> Void)?
    /// Fired on the recording→idle transition.
    var onRecordingStopped: (() -> Void)?

    /// When the engine last confirmed a recording was running, cleared on a
    /// clean stop. It survives the jump to .offline, so something asking
    /// after the fact ("did the engine take a meeting down with it?") can
    /// still tell an interruption from an ordinary idle app.
    private(set) var lastRecordingSeen: Date?

    private var liveSeq = 0
    private var pollTask: Task<Void, Never>?
    private var nudgeSeenAt: Date?
    private var expiredNudgeIds = Set<String>()

    // The note draft's own state. `draftMeetingID` is the binding that keeps a
    // note on the meeting it was typed in; `pending` is everything handed over
    // and not yet accepted by the engine. Both are mirrored to disk.
    private let drafts = NoteDraftStore()
    private var draftMeetingID: String?
    private var draftT: Double?
    private var pending: [NoteDraft] = []
    private var refused: [NoteDraft] = []
    private var flushTask: Task<Void, Never>?
    private var retryAfter = Date.distantPast

    init() {
        // Adopt whatever the last run left behind BEFORE the first poll: a
        // draft's meeting is decided by the tick that learns what is
        // recording, and it can only decide correctly if the draft is here.
        // (Property observers don't fire from an initializer, so setting
        // noteDraft here re-binds nothing and re-writes nothing.)
        let restored = drafts.load()
        pending = restored.pending
        refused = restored.refused
        draftMeetingID = restored.live?.meetingID
        draftT = restored.live?.t
        noteDraft = restored.live?.text ?? ""

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
        flushPending()   // the engine is answering: hand over anything waiting
        if st.recording {
            if phase != .recording {   // just started (by us, the nudge, or elsewhere)
                // Text still in the field belongs to an EARLIER meeting (the
                // app was quit mid-note, or a second meeting started without
                // the first's note being sent). File it there; this meeting
                // starts with an empty field.
                if draftMeetingID != nil, draftMeetingID != st.meeting_id { strandDraft() }
                liveSeq = 0
                liveTurns = []
                livePartials = [:]
                sentNotes = []
                nudge = nil
                phase = .recording
                onRecordingStarted?()
            }
            lastRecordingSeen = Date()
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
                // Stop is the one moment a note is filed without the user
                // pressing Return: from here it can only belong to the
                // meeting that just ended, so filing it beats leaving it to
                // rot in a field the next meeting inherits.
                strandDraft()
                meetingId = nil
                lastRecordingSeen = nil   // a clean stop: nothing was interrupted
                onRecordingStopped?()
            } else {
                phase = .idle
                // A draft that outlived a quit or a crash: its meeting ended
                // while the app was away.
                if draftMeetingID != nil { strandDraft() }
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

    /// Was a recording running in the last `seconds`, with no clean stop
    /// since? Asked by the engine watchdog once it has decided the engine is
    /// gone, which is several seconds after the recorder went quiet.
    func wasRecording(within seconds: TimeInterval) -> Bool {
        guard let lastRecordingSeen else { return false }
        return Date().timeIntervalSince(lastRecordingSeen) <= seconds
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
        guard !text.isEmpty else { return }
        pending.append(NoteDraft(meetingID: draftMeetingID ?? meetingId,
                                 t: draftT ?? (phase == .recording ? elapsed : nil),
                                 text: text))
        noteDraft = ""      // the observer clears the binding and saves both
        noteRefusal = nil   // the user is acting again; last time's verdict goes
        retryAfter = .distantPast
        flushPending()
    }

    // MARK: - The note draft

    /// The field changed. Bind it to a meeting on its first character, let it
    /// go on its last, and mirror it to disk either way.
    private func draftChanged(from old: String) {
        guard noteDraft != old else { return }
        if noteDraft.isEmpty {
            draftMeetingID = nil
            draftT = nil
        } else if draftMeetingID == nil {
            draftMeetingID = meetingId
            draftT = phase == .recording ? elapsed : nil
        }
        saveDrafts()
    }

    /// Hand the field's text to the outbox: the meeting it belongs to is over.
    private func strandDraft() {
        let text = noteDraft.trimmingCharacters(in: .whitespacesAndNewlines)
        let target = draftMeetingID ?? meetingId
        guard !text.isEmpty else {
            if !noteDraft.isEmpty { noteDraft = "" }   // whitespace only
            return
        }
        pending.append(NoteDraft(meetingID: target, t: draftT, text: text))
        noteDraft = ""
        retryAfter = .distantPast
        flushPending()
    }

    /// Give the engine everything waiting, oldest first, one at a time.
    ///
    /// Nothing leaves the disk until the engine says it stored it, so a note
    /// survives an engine that is down, restarting or mid-upgrade. A note the
    /// engine REFUSES (too long, meeting deleted) moves aside instead of
    /// staying at the head of the queue: it will be refused identically
    /// forever, and one of those parked in front would silently stop every
    /// note typed after it from being filed.
    private func flushPending() {
        guard flushTask == nil, !pending.isEmpty, Date() >= retryAfter else { return }
        flushTask = Task { [weak self] in
            defer { self?.flushTask = nil }
            guard let self else { return }
            while let next = pending.first {
                let result = await API.note(text: next.text, t: next.t, meetingID: next.meetingID)
                // The queue only ever grows at the tail, so the head is still
                // ours. Stop rather than spin if that ever stops being true.
                guard let i = pending.firstIndex(of: next) else { return }
                switch result {
                case .stored:
                    pending.remove(at: i)
                    saveDrafts()
                    noteSendFailed = false
                    if next.meetingID == meetingId, phase == .recording {
                        sentNotes.append(HUDNote(t: next.t, text: next.text))
                    }
                case .refused(let why):
                    NSLog("MeetingScribe: the engine refused a note (\(why)). "
                          + "It is kept in the note-drafts file.")
                    pending.remove(at: i)
                    refused.append(next)
                    saveDrafts()
                    noteRefusal = why
                case .unreachable:
                    noteSendFailed = true
                    retryAfter = Date().addingTimeInterval(10)
                    return
                }
            }
        }
    }

    private func saveDrafts() {
        let live = noteDraft.isEmpty
            ? nil
            : NoteDraft(meetingID: draftMeetingID, t: draftT, text: noteDraft)
        drafts.save(NoteDraftStore.Contents(live: live, pending: pending, refused: refused))
    }
}
