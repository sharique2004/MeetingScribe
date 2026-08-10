// The one owner of recorder truth for the whole app: polls the engine for
// recorder state, meeting-start nudges, and live captions; exposes actions
// (start/stop/accept/dismiss/note). The window and the floating pill both
// read from here, so they can never disagree.
//
// It also owns the two things a recording can be silently wrong about:
// whether the engine agreed to start at all, and whether both tracks are
// still carrying sound. Both used to be discovered after the meeting.
import Foundation
import Combine

struct HUDNote: Identifiable {
    let id = UUID()
    let t: Double?
    let text: String
}

/// What the user chose before pressing record. Every field has a default that
/// makes "just press record" the right behaviour, so this is only ever an
/// opportunity, never a form to fill in.
struct RecordOptions: Equatable {
    var title = ""
    var inPerson = false
    /// BCP-47-ish code, or "auto" to let the engine decide.
    var language = "auto"
    /// An explicit count the user picked. nil means nobody picked one.
    var expectedSpeakers: Int?
    /// Attendees on the calendar event this recording is for, when the user
    /// chose one. The engine's own auto-pick only ever sees the event
    /// overlapping the current clock time, which is the wrong event when you
    /// join early, join late, or run back-to-back meetings — so a chosen
    /// event has to carry its own count here.
    var attendees: Int?

    /// The count to send. The engine's arithmetic, kept identical: calendar
    /// attendees exclude you, an online call wants the OTHER speakers, and a
    /// room around one microphone wants everybody in it.
    var resolvedSpeakers: Int? {
        if let expectedSpeakers { return expectedSpeakers }
        guard let attendees, attendees > 0 else { return nil }
        return min(8, max(1, inPerson ? attendees + 1 : attendees))
    }

    var requestBody: [String: Any] {
        var body: [String: Any] = ["mode": inPerson ? "inperson" : "online"]
        let trimmed = title.trimmingCharacters(in: .whitespacesAndNewlines)
        if !trimmed.isEmpty { body["title"] = trimmed }
        if language != "auto" { body["language"] = language }
        if let n = resolvedSpeakers { body["expected_speakers"] = n }
        return body
    }
}

/// The engine refused to start or stop a recording, in its own words. The
/// button used to fire and return nothing: a 507 (no disk), a 409 (already
/// recording) and an engine that never answered all looked like a click that
/// did nothing at all.
struct RecorderAlert: Identifiable, Equatable {
    /// `.lost` is not a refused button: it is a meeting that stopped being
    /// recorded because the engine underneath it died. The engine watchdog
    /// puts the engine back on its feet by itself now, so no failure banner
    /// stays on screen to say what happened — and a recording that ends
    /// without a word is the exact silence this app exists to break.
    enum Kind { case start, stop, lost }
    let id = UUID()
    let kind: Kind
    let message: String

    var headline: String {
        switch kind {
        case .start: "The recording didn't start"
        case .stop: "The recording didn't stop"
        case .lost: "The recording stopped"
        }
    }
}

/// One audio track while a meeting runs. `present` is the engine's answer
/// about whether the track was opened at all; `alive` and `error` come
/// straight from the recorder thread, so a track that dies mid-meeting is
/// reported rather than inferred.
struct RecorderTrack: Equatable {
    var present = false
    var alive = true
    var error: String?
    var level: Double = 0
    /// The engine says this track is capturing silence. Believed immediately:
    /// it is the helper's own watchdog, not an inference from meter values.
    var reportedSilent = false
    /// Any sample above digital silence since the recording started.
    var everCarriedSound = false
    /// When the track last carried anything other than digital silence.
    var lastSound: Date?

    /// A live stream is never EXACTLY zero for long — even a quiet room has a
    /// noise floor. Digital silence means nothing is reaching this track.
    static let silenceFloor = 0.001
}

/// Something is wrong with the capture, said while the meeting is still
/// running and can still be saved.
struct CaptureAlert: Identifiable, Equatable {
    let id: String
    let title: String
    let detail: String
}

// MARK: - Engine shapes

struct RecorderDisk: Decodable, Equatable {
    let state: String?      // "ok" | "low" | "full"
    let message: String?
}

struct RecorderTrackState: Decodable, Equatable {
    let alive: Bool?
    let error: String?
    /// The capture helper's OWN verdict that it is writing nothing but zeros.
    /// Authoritative where the level meters are only evidence: the tap knows
    /// inside ten seconds, and it used to travel no further than a log line.
    let silent: Bool?
}

/// /api/record/status in full. The app's older shape dropped `tracks` and
/// `disk` on the floor, which is where "the other side was never recorded"
/// went to hide for the length of a meeting.
struct RecorderSnapshot: Decodable {
    let recording: Bool
    let meeting_id: String?
    let elapsed: Double?
    let levels: [String: Double]?
    let tracks: [String: RecorderTrackState]?
    let disk: RecorderDisk?
}

/// GET /api/devices — what this Mac will record with, asked BEFORE the
/// recording rather than discovered after it.
struct DevicePreflight: Decodable, Equatable {
    struct Device: Decodable, Equatable {
        let name: String?
        let routing: String?
        /// "silent" | "no_loopback" | "not_routed", or nil when there is
        /// nothing to warn about.
        let routing_alert: String?
        let routing_note: String?
        let auto_route: Bool?
    }

    let recording: Bool?
    let mic: Device?
    let system: Device?
    let disk: RecorderDisk?

    /// Problems worth showing before a recording starts, most serious first.
    var alerts: [CaptureAlert] {
        var out: [CaptureAlert] = []
        if disk?.state == "full", let message = disk?.message {
            out.append(CaptureAlert(id: "disk", title: "There is no room to record",
                                    detail: message))
        }
        if mic == nil {
            out.append(CaptureAlert(
                id: "mic", title: "No microphone found",
                detail: "Your own voice will not be recorded. Connect a microphone, or check System Settings → Privacy & Security → Microphone."))
        }
        if system == nil {
            out.append(CaptureAlert(
                id: "system", title: "The other participants will not be recorded",
                detail: "This Mac has no system-audio capture available, so only your microphone will be saved."))
        } else if let alert = system?.routing_alert {
            out.append(CaptureAlert(
                id: "system",
                title: alert == "silent" ? "The other participants will not be recorded"
                                         : "The other participants may not be recorded",
                detail: system?.routing_note
                    ?? "System audio is not routed to MeetingScribe."))
        }
        if disk?.state == "low", let message = disk?.message {
            out.append(CaptureAlert(id: "disk-low", title: "Disk space is running low",
                                    detail: message))
        }
        return out
    }
}

/// One event on today's calendar. The engine reports epoch seconds and an
/// attendee count; both are needed to record "this one" rather than whatever
/// happens to overlap the clock.
struct DayEvent: Decodable, Hashable, Identifiable {
    let title: String?
    let start: Double?
    let end: Double?
    let attendees: Int?
    let organizer: String?

    var id: String { "\(start ?? 0)|\(title ?? "")" }
    var name: String { (title?.isEmpty == false ? title : nil) ?? "Untitled event" }
    var startDate: Date? { start.map { Date(timeIntervalSince1970: $0) } }

    /// Happening right now, as far as the clock is concerned.
    var isNow: Bool {
        guard let start, let end else { return false }
        let now = Date().timeIntervalSince1970
        return start <= now && now < end
    }

    /// Over, and long enough ago that offering to record it is noise.
    var isPast: Bool {
        guard let end else { return false }
        return end < Date().timeIntervalSince1970 - 300
    }
}

/// GET /api/calendar/today. `available` is the only honest basis for saying
/// the calendar is connected: the helper reports denial and failure here, and
/// an empty event list is a perfectly normal successful answer.
struct CalendarStatus: Decodable {
    let available: Bool?
    let events: [DayEvent]?
    let error: String?

    static let unreachable = CalendarStatus(
        available: false, events: nil,
        error: "The engine isn't answering, so the calendar can't be checked.")
}

@MainActor
final class RecorderCenter: ObservableObject {
    /// One recorder for the life of the app, like EngineManager beside it.
    ///
    /// It used to be a `@StateObject` on ContentView, which made the whole of
    /// recording a possession of the main window: closing the window (⌘W —
    /// an ordinary thing to do to a Mac app you are not currently reading)
    /// destroyed the centre, and with it the poll that notices a meeting
    /// starting, the poll that notices a recording running, and the pill that
    /// is the app's only surface once the window is gone. The nudge could not
    /// arrive because nothing was left to ask for it.
    static let shared = RecorderCenter()

    enum Phase: Equatable { case offline, idle, recording }

    @Published private(set) var phase: Phase = .offline
    @Published private(set) var elapsed: Double = 0
    @Published private(set) var meetingId: String?
    @Published private(set) var micLevel: Double = 0
    @Published private(set) var systemLevel: Double = 0
    @Published private(set) var nudge: Nudge?
    @Published private(set) var stopping = false
    @Published private(set) var starting = false
    /// The engine's refusal of the last Record or Stop. Nothing else reports
    /// it: both used to be fire-and-forget, so a refusal was a button that
    /// visibly did nothing.
    @Published var alert: RecorderAlert?
    /// Per-track truth for the meeting in progress.
    @Published private(set) var micTrack = RecorderTrack()
    @Published private(set) var systemTrack = RecorderTrack()
    /// What the engine said as the recording started (routing it could not
    /// fix, a track it could not open, space running out). Saved onto the
    /// meeting too, but that is hours too late to act on.
    @Published private(set) var startWarnings: [String] = []
    /// Free space, while the meeting that will fill it is still running.
    @Published private(set) var diskNote: String?
    /// The devices this Mac would record with right now — asked before the
    /// recording, so "the other side wasn't recorded" is answerable in
    /// advance instead of at Stop.
    @Published private(set) var preflight: DevicePreflight?
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
    /// Nudges this run has already put on screen as a system notification.
    /// Posting is idempotent (same identifier, same banner), but posting again
    /// re-alerts — and the engine goes on offering the same nudge for as long
    /// as its own TTL says to, once every 1.2 s.
    private var announcedNudges = Set<String>()
    private var recordingStartedAt: Date?
    private var preflightAskedAt = Date.distantPast
    private var discardSweptAt = Date.distantPast
    private var sweeping = false

    /// How long a present track may carry nothing at all before it is worth
    /// saying so. Generous on purpose: a meeting where nobody speaks for a
    /// while is ordinary, a track that has been digitally silent for minutes
    /// is not.
    private let silenceGrace: TimeInterval = 45
    private let goneQuietGrace: TimeInterval = 120

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

    private init() {
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

        // `guard let self` rather than `self?.tick()`: optional-chaining a
        // dead owner is a no-op the loop never notices, so the timer went on
        // ticking — forever, at a poll a second — after the centre itself was
        // gone.
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                guard let self else { return }
                await self.tick()
                let recording = self.phase == .recording
                try? await Task.sleep(for: recording ? .milliseconds(350)
                                                     : .milliseconds(1200))
            }
        }
    }

    private func tick() async {
        guard let st = await API.recorderSnapshot() else {
            phase = .offline
            nudge = nil
            return
        }
        flushPending()   // the engine is answering: hand over anything waiting
        sweepDiscards()
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
                // A meeting is being recorded now, which answers every
                // "shall I record this?" still sitting in Notification
                // Center — including one the pill gave up on at 90 s.
                withdrawNudgeNotifications()
                micTrack = RecorderTrack()
                systemTrack = RecorderTrack()
                recordingStartedAt = Date()
                phase = .recording
                onRecordingStarted?()
            }
            lastRecordingSeen = Date()
            elapsed = st.elapsed ?? elapsed
            micLevel = st.levels?["mic"] ?? 0
            systemLevel = st.levels?["system"] ?? 0
            readTracks(st)
            diskNote = st.disk?.state == "ok" ? nil : st.disk?.message
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
                micTrack = RecorderTrack()
                systemTrack = RecorderTrack()
                startWarnings = []
                diskNote = nil
                recordingStartedAt = nil
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
                    // The pill is only half of this. It lives in a floating
                    // panel that the user is not looking at while they join a
                    // call — behind the meeting window, on another Space, or
                    // not on screen at all until this very moment. The banner
                    // is how the question reaches somebody. Once per nudge:
                    // re-posting replaces the banner but re-alerts with it.
                    if !announcedNudges.contains(fresh.id) {
                        announcedNudges.insert(fresh.id)
                        MSNotifications.postNudge(id: fresh.id, title: fresh.title,
                                                  body: fresh.body ?? "Start recording?")
                    }
                } else if let seen = nudgeSeenAt, Date().timeIntervalSince(seen) > 90 {
                    expiredNudgeIds.insert(fresh.id)
                    nudge = nil
                    // The BANNER is deliberately left standing. 90 s is how
                    // long the pill is willing to sit in front of someone's
                    // work, not how long the meeting is worth recording: the
                    // user who comes back from the kitchen four minutes in
                    // still wants that button, and the engine's own TTL is
                    // what decides when the offer really has expired.
                }
            } else {
                nudge = nil
            }
            // Not awaited: device enumeration is the one idle call that can
            // take a moment, and the nudge poll is not waiting behind it.
            Task { await refreshPreflight() }
        }
    }

    /// Was a recording running in the last `seconds`, with no clean stop
    /// since? Asked by the engine watchdog once it has decided the engine is
    /// gone, which is several seconds after the recorder went quiet.
    func wasRecording(within seconds: TimeInterval) -> Bool {
        guard let lastRecordingSeen else { return false }
        return Date().timeIntervalSince(lastRecordingSeen) <= seconds
    }

    // MARK: - What the tracks are doing

    private func readTracks(_ st: RecorderSnapshot) {
        micTrack = merge(micTrack, key: "mic", st)
        systemTrack = merge(systemTrack, key: "system", st)
    }

    private func merge(_ old: RecorderTrack, key: String, _ st: RecorderSnapshot) -> RecorderTrack {
        var t = old
        let reported = st.tracks?[key]
        // A recorder that reports no track map at all (an older engine) is not
        // evidence that the track is missing; the level key is.
        t.present = st.tracks.map { $0[key] != nil } ?? (st.levels?[key] != nil)
        t.alive = reported?.alive ?? true
        t.error = reported?.error
        t.reportedSilent = reported?.silent ?? false
        t.level = st.levels?[key] ?? 0
        if t.level > RecorderTrack.silenceFloor {
            t.everCarriedSound = true
            t.lastSound = Date()
        }
        return t
    }

    /// Everything wrong with the capture right now, in the order it matters.
    /// Empty is the normal case and the whole point: this says nothing at all
    /// while both tracks are healthy.
    var captureAlerts: [CaptureAlert] {
        guard phase == .recording else { return [] }
        var out: [CaptureAlert] = []
        for (key, track, who, fix) in [
            ("mic", micTrack, "Your microphone",
             "Check that the right input is selected in System Settings → Sound, then stop and start the recording again."),
            ("system", systemTrack, "The other participants",
             "Check that the meeting is playing through this Mac's output. Stopping and starting the recording again re-runs the routing."),
        ] as [(String, RecorderTrack, String, String)] {
            if !track.present {
                out.append(CaptureAlert(
                    id: key,
                    title: key == "mic" ? "Your microphone is not being recorded"
                                        : "The other participants are not being recorded",
                    detail: "This meeting was opened without a \(key == "mic" ? "microphone" : "system audio") track, so that side of it will be missing."))
                continue
            }
            if !track.alive {
                out.append(CaptureAlert(
                    id: key, title: "\(who) stopped being recorded",
                    detail: (track.error.map { $0 + ". " } ?? "")
                        + "Everything up to that moment is saved. " + fix))
                continue
            }
            guard let quiet = quietFor(track) else { continue }
            out.append(CaptureAlert(
                id: key,
                title: track.everCarriedSound
                    ? "\(who): no sound for \(minutesPhrase(quiet))"
                    : "\(who): nothing recorded yet",
                detail: track.everCarriedSound
                    ? "The track is running but completely silent. " + fix
                    : "Not one sound has reached this track since the recording started. " + fix))
        }
        return out
    }

    /// How long a present, live track has been digitally silent, or nil while
    /// that is still within the ordinary run of a meeting.
    private func quietFor(_ track: RecorderTrack) -> TimeInterval? {
        guard let started = recordingStartedAt else { return nil }
        let since = track.lastSound ?? started
        let quiet = Date().timeIntervalSince(since)
        // The helper's own watchdog outranks the grace period. It reports
        // sustained digital zeros in ten seconds, and waiting another
        // thirty-five to say so only spends the part of the meeting the user
        // could still have rescued.
        if track.reportedSilent, !track.everCarriedSound { return quiet }
        let grace = track.everCarriedSound ? goneQuietGrace : silenceGrace
        return quiet >= grace ? quiet : nil
    }

    private func minutesPhrase(_ seconds: TimeInterval) -> String {
        let minutes = Int(seconds / 60)
        if minutes < 1 { return "\(Int(seconds)) seconds" }
        return minutes == 1 ? "a minute" : "\(minutes) minutes"
    }

    // MARK: - Before the recording

    /// Ask the engine what it would record with. Cheap, cached by the engine
    /// for a few seconds, and only asked while idle.
    func refreshPreflight(force: Bool = false) async {
        guard phase != .recording else { return }
        guard force || Date().timeIntervalSince(preflightAskedAt) > 8 else { return }
        preflightAskedAt = Date()
        if let fresh = await API.devicePreflight() { preflight = fresh }
    }

    // MARK: - Actions

    func startRecording(title: String? = nil) {
        var options = RecordOptions()
        options.title = title ?? ""
        start(options)
    }

    /// Start a meeting with everything the user chose. The engine's answer is
    /// read: it refuses a start with no disk (507), one already running (409)
    /// and a device it could not open (500), and each refusal says why.
    func start(_ options: RecordOptions) {
        guard !starting, phase != .recording else { return }
        starting = true
        Task {
            switch await API.recordStart(options.requestBody) {
            case .started(let meta):
                alert = nil
                startWarnings = meta.warnings ?? []
            case .refused(let why):
                alert = RecorderAlert(kind: .start, message: why)
            }
            await tick()
            starting = false
        }
    }

    func stopRecording() {
        stopping = true
        Task {
            let (ok, why) = await API.recordStop()
            await tick()
            stopping = false
            if ok { alert = nil }   // a refusal the user has now got past
            // "Not recording" is not a failure worth a word — the recording
            // the user wanted stopped is stopped. Anything else leaves a
            // meeting running that the user believes has ended.
            if !ok, phase != .idle {
                alert = RecorderAlert(
                    kind: .stop,
                    message: why ?? "The engine didn't answer, so the recording may still be running.")
            }
        }
    }

    /// Remove a meeting the app created for its own purposes (the setup
    /// test), retrying until the engine actually lets go of it.
    ///
    /// The engine refuses to delete a meeting while it is being transcribed,
    /// and on a fresh Mac that transcription can sit behind a multi-gigabyte
    /// model download — far longer than the sheet that made it stays open. So
    /// the id is written down and retried on every poll, across launches: a
    /// phantom "Setup test" meeting in someone's library is the one trace
    /// first-run must not leave.
    func discardLater(_ id: String) {
        var ids = Self.pendingDiscards
        guard !ids.contains(id) else { return }
        ids.append(id)
        Self.pendingDiscards = ids
        discardSweptAt = .distantPast
    }

    private static var pendingDiscards: [String] {
        get { UserDefaults.standard.stringArray(forKey: "ms.pendingDiscards") ?? [] }
        set { UserDefaults.standard.set(newValue, forKey: "ms.pendingDiscards") }
    }

    /// Never awaited by the poll: a delete the engine is sitting on must not
    /// hold up the timer, the levels or the notes outbox.
    private func sweepDiscards() {
        guard !sweeping, !Self.pendingDiscards.isEmpty,
              Date().timeIntervalSince(discardSweptAt) > 20 else { return }
        sweeping = true
        discardSweptAt = Date()
        Task { [weak self] in
            defer { self?.sweeping = false }
            guard let self else { return }
            for id in Self.pendingDiscards where id != meetingId {
                if await API.discardMeeting(id) {
                    Self.pendingDiscards.removeAll { $0 == id }
                }
            }
        }
    }

    /// "Record" on a meeting reminder. It is a start like any other, so a
    /// refusal (no disk, the reminder already answered) is shown like any
    /// other rather than dismissing the nudge and doing nothing.
    func accept(_ n: Nudge) { acceptNudge(id: n.id) }

    func dismiss(_ n: Nudge) { dismissNudge(id: n.id) }

    /// The same two answers, reachable with nothing but the id — which is all
    /// the notification's buttons carry (see MSNotifications.handleNudge).
    /// One path, so a banner press and a pill press cannot drift apart.
    func acceptNudge(id: String) {
        MSNotifications.removeNudge(id: id)
        Task {
            let (ok, why) = await API.acceptNudge(id)
            if !ok { alert = RecorderAlert(kind: .start, message: why ?? "The engine didn't answer.") }
            if nudge?.id == id { nudge = nil }
            await tick()
        }
    }

    func dismissNudge(id: String) {
        MSNotifications.removeNudge(id: id)
        Task {
            await API.ackNudge(id)
            if nudge?.id == id { nudge = nil }
        }
    }

    /// Take down every meeting nudge this run has posted. Called when a
    /// recording starts: the question they all ask has been answered by the
    /// fact of the recording, whoever answered it.
    private func withdrawNudgeNotifications() {
        for id in announcedNudges { MSNotifications.removeNudge(id: id) }
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

// MARK: - The engine calls this file owns
//
// Everything here reads the engine's ANSWER rather than the fact that it was
// asked. The plain post() helper returns a bare Bool, which is what let a
// refused start, a refused stop and a denied calendar all pass for success.

extension API {
    enum StartResult {
        case started(StartedMeeting)
        case refused(String)
    }

    /// The meeting the engine just created. `warnings` is the recorder's own
    /// account of what it could not do (routing it failed to set up, a track
    /// it could not open) and it is written at the moment of starting.
    struct StartedMeeting: Decodable {
        let id: String?
        let title: String?
        let warnings: [String]?
    }

    static func recordStart(_ body: [String: Any]) async -> StartResult {
        let (code, data) = await send("api/record/start", method: "POST", body: body, timeout: 20)
        guard let code else {
            return .refused("The engine didn't answer. Check that it is running, then try again.")
        }
        if (200..<300).contains(code) {
            let meta = data.flatMap { try? JSONDecoder().decode(StartedMeeting.self, from: $0) }
            return .started(meta ?? StartedMeeting(id: nil, title: nil, warnings: nil))
        }
        return .refused(errorText(data) ?? "The engine wouldn't start a recording (\(code)).")
    }

    /// (ok, why). A 409 means the recorder was already stopped, which is the
    /// state the caller wanted, so it is reported honestly and the caller
    /// decides whether it is worth a word.
    static func recordStop() async -> (Bool, String?) {
        let (code, data) = await send("api/record/stop", method: "POST", body: [:], timeout: 30)
        guard let code else { return (false, "The engine didn't answer.") }
        if (200..<300).contains(code) { return (true, nil) }
        return (false, errorText(data) ?? "The engine wouldn't stop the recording (\(code)).")
    }

    /// Accepting a reminder starts a recording, so it refuses in the same
    /// shape a start does, and is owed the same sentence.
    static func acceptNudge(_ id: String) async -> (Bool, String?) {
        let (code, data) = await send("api/nudges/\(id)/accept", method: "POST",
                                      body: [:], timeout: 20)
        guard let code else { return (false, "The engine didn't answer.") }
        if (200..<300).contains(code) { return (true, nil) }
        return (false, errorText(data) ?? "The engine wouldn't start that recording (\(code)).")
    }

    /// "Not this meeting". Nothing is owed to the caller beyond the fact that
    /// it was said: the engine drops the nudge, and an ack that never arrives
    /// costs the user one repeat at worst.
    @discardableResult
    static func ackNudge(_ id: String) async -> Bool {
        await post("api/nudges/\(id)/ack")
    }

    static func recorderSnapshot() async -> RecorderSnapshot? {
        try? await get("api/record/status", as: RecorderSnapshot.self)
    }

    static func devicePreflight() async -> DevicePreflight? {
        try? await get("api/devices", as: DevicePreflight.self)
    }

    /// Today's events with everything a recording needs from them: which one
    /// it is, and how many people are on it.
    static func calendarStatus() async -> CalendarStatus {
        (try? await get("api/calendar/today", as: CalendarStatus.self)) ?? .unreachable
    }

    /// Delete a meeting the app made for itself. A 404 is success (it is
    /// already gone, which is the state asked for) and so is a 400 (the id is
    /// malformed, so no amount of retrying will ever remove it). Only a 409,
    /// "still being processed", is worth coming back for.
    static func discardMeeting(_ id: String) async -> Bool {
        let (code, _) = await send("api/meetings/\(id)", method: "DELETE", body: nil, timeout: 15)
        guard let code else { return false }
        return (200..<300).contains(code) || code == 404 || code == 400
    }

    private struct CuesEnvelope: Decodable { let cues: [String]? }

    /// The talking points carried into the next meeting. nil means the engine
    /// didn't answer, which is different from "there are none".
    static func cues() async -> [String]? {
        (try? await get("api/cues", as: CuesEnvelope.self))?.cues
    }

    @discardableResult
    static func saveCues(_ cues: [String]) async -> Bool {
        let (code, _) = await send("api/cues", method: "PUT", body: ["cues": cues], timeout: 10)
        return code.map { (200..<300).contains($0) } ?? false
    }

    /// One request, one honest answer: (status code or nil when the engine
    /// never replied, body).
    private static func send(_ path: String, method: String, body: [String: Any]?,
                             timeout: TimeInterval) async -> (Int?, Data?) {
        var req = URLRequest(url: engineBase.appendingPathComponent(path))
        req.httpMethod = method
        if let body {
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = try? JSONSerialization.data(withJSONObject: body)
        }
        req.timeoutInterval = timeout
        guard let (data, resp) = try? await URLSession.shared.data(for: req) else {
            return (nil, nil)
        }
        return ((resp as? HTTPURLResponse)?.statusCode, data)
    }

    private static func errorText(_ data: Data?) -> String? {
        guard let data,
              let object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              let message = object["error"] as? String, !message.isEmpty else { return nil }
        return message
    }
}
