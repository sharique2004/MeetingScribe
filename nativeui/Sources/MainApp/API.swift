// Client for the local MeetingScribe engine (Flask on 127.0.0.1:5005).
// Read-only endpoints plus /ask. Shapes decoded leniently — every field the
// UI can live without is optional.
import Foundation

let engineBase = URL(string: "http://127.0.0.1:5005")!

// MARK: - Models

struct MeetingListItem: Decodable, Identifiable, Hashable {
    let id: String
    /// `var` so a rename can land on the sidebar row the moment the engine
    /// accepts it, instead of waiting for whatever refetches the list next.
    var title: String
    let created: String
    let duration: Double?
    let status: String?
    let speakers: Int?
    // The row view, served with the list. Drawing a row used to fetch the
    // whole document per row: 43 KB against 191 B on a 400-turn meeting, with
    // every turn crossing the socket, and /api/meetings/<id> also takes
    // JOB_LOCK to persist notes, so drawing a list put a lock-taking write in
    // front of the job the row was reporting on.
    let brief: String?
    let has_transcript: Bool?
    let has_summary: Bool?
    let has_notes: Bool?
    let warnings: [String]?
}

extension MeetingListItem {
    var rowMeta: RowMeta {
        var m = RowMeta()
        m.brief = (brief?.isEmpty == false) ? brief : nil
        m.hasTranscript = has_transcript ?? false
        m.hasSummary = has_summary ?? false
        m.hasNotes = has_notes ?? false
        m.hasCaptureWarning = (warnings ?? []).contains { isCaptureWarning($0) }
        return m
    }
}

private struct MeetingsPage: Decodable { let items: [MeetingListItem] }

/// The row view of one meeting, and nothing else: no turns, no document, and
/// no write. For the tick a meeting finishes, when its list entry was read
/// while the transcript did not exist yet.
private struct BriefRow: Decodable {
    let brief: String?
    let has_transcript: Bool?
    let has_summary: Bool?
    let has_notes: Bool?
    let warnings: [String]?
}

extension API {
    static func brief(_ id: String) async throws -> RowMeta {
        let r = try await get("api/meetings/\(id)/brief", as: BriefRow.self)
        var m = RowMeta()
        m.brief = (r.brief?.isEmpty == false) ? r.brief : nil
        m.hasTranscript = r.has_transcript ?? false
        m.hasSummary = r.has_summary ?? false
        m.hasNotes = r.has_notes ?? false
        m.hasCaptureWarning = (r.warnings ?? []).contains { isCaptureWarning($0) }
        return m
    }
}

struct Turn: Decodable, Hashable {
    let speaker: String?
    let start: Double
    let end: Double?
    let text: String
}

struct ActionItem: Decodable, Hashable {
    let owner: String?
    let task: String
    let due: String?
}

struct FollowUpEmail: Decodable, Hashable {
    let subject: String?
    let body: String?

    var isUseful: Bool {
        (body?.trimmingCharacters(in: .whitespacesAndNewlines).count ?? 0) > 40
    }
}

struct MeetingSummary: Decodable {
    let headline: String?
    let tldr: String?
    let key_points: [String]?
    let decisions: [String]?
    let action_items: [ActionItem]?
    let follow_ups: [String]?
    let open_questions: [String]?
    let follow_up_email: FollowUpEmail?
    /// Set by the engine when the prompt couldn't carry every note.
    let notes_omitted: Int?
    let engine: String?
}

/// What stats.py already computes for one speaker, all of it. Talk time,
/// share, pace, questions, the longest unbroken monologue and the filler
/// words — the "how did I do?" half of the product, which nothing in the
/// native app read until now.
struct SpeakerStats: Decodable {
    let seconds: Double?
    let words: Int?
    let share: Double?
    let wpm: Double?
    let questions: Int?
    let turns: Int?
    let longest_turn_seconds: Double?
    /// Phrase → count, already sorted by the engine (commonest first).
    let fillers: [String: Int]?
    let filler_total: Int?
}

struct MeetingStats: Decodable {
    let per_speaker: [String: SpeakerStats]?
    let total_spoken_seconds: Double?
    let total_words: Int?
    let duration: Double?
}

/// One recorded audio track as the recorder wrote it. `start_offset` is the
/// seconds this track began AFTER the earliest one — the two capture threads
/// do not start on the same sample, and every other reader of this file
/// (the waveform endpoint, the pipeline's transcript offsets) already
/// corrects for it. Playback that ignores it plays the two tracks out of step
/// with each other and with the transcript.
struct TrackInfo: Decodable, Hashable {
    let file: String?
    let device: String?
    let seconds: Double?
    let start_offset: Double?

    /// Did this track actually capture anything? A meeting whose system audio
    /// macOS blocked still carries a `system` entry, with zero seconds in it,
    /// and offering that as a playable track is offering silence.
    ///
    /// `seconds` is only written when the recorder STOPS, so a meeting a crash
    /// caught mid-recording has tracks with no length at all — and its WAVs
    /// are on disk, which is the whole premise of Reprocess. Unknown therefore
    /// means playable; a file that turns out not to be there fails loudly on
    /// the transport instead of being silently withheld.
    var hasAudio: Bool { (seconds ?? 1) > 0.05 }
}

/// How this meeting was transcribed, in the engine's words.
struct ProcessingInfo: Decodable {
    let backend: String?
    let model: String?
    let seconds: Double?

    /// The name the product uses for each speech backend.
    var label: String? {
        switch backend {
        case "parakeet": return "Parakeet"
        case "apple": return "Apple Speech"
        case "mlx": return "Whisper · GPU"
        case "faster": return "Whisper · CPU"
        default: return nil
        }
    }
}

struct MeetingDetail: Decodable {
    let id: String
    let title: String
    let created: String
    let duration: Double?
    let status: String?
    let speakers: [String: String]?
    let turns: [Turn]?
    let stats: MeetingStats?
    let summary: MeetingSummary?
    let notes: [MeetingNote]?
    /// The audio this meeting actually has, keyed "mic" / "system". The
    /// transport picker is built from this and nothing else — a meeting
    /// recorded in person on one device has no `system` track, and a picker
    /// that offers it hands the user a 404.
    let tracks: [String: TrackInfo]?
    /// "online" | "inperson".
    let mode: String?
    /// Per-track detected language, e.g. ["mic": "en_US"].
    let languages: [String: String]?
    let processing: ProcessingInfo?
    /// Why processing stopped, in the engine's own words, e.g. "Interrupted —
    /// press Reprocess to transcribe the saved audio." Set with
    /// status == "error" and nothing else clears it: the audio is still on
    /// disk and Reprocess is the whole recovery path.
    let error: String?
    /// Everything the engine wants said about this recording that isn't a
    /// failure: system audio that macOS blocked, a diarization fallback, echo
    /// it trimmed. Written by the recorder at stop and by the pipeline, and
    /// never seen by anyone until it is on the page.
    let warnings: [String]?
}

extension MeetingDetail {
    /// Processing failed and will not resume on its own.
    var failed: Bool { status == "error" }

    /// The engine's sentence for a failed meeting, or a plain one if the
    /// document somehow carries the status without the reason.
    var failureText: String {
        let text = error?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        return text.isEmpty ? "Processing failed. The audio is saved." : text
    }

    /// Warnings that cost the user audio: the recording is not what they
    /// think it is, and the engine's sentence ends with the fix. Those earn
    /// a band at the top of the page. Everything else the engine reports
    /// (a diarization fallback, echo it trimmed) changed the labels, not the
    /// sound, and reads as a footnote.
    ///
    /// Matched on the engine's words rather than a severity field, because
    /// there isn't one: warnings are free text appended by the recorder and
    /// the pipeline. A phrase that stops matching costs a warning its band,
    /// never its visibility.
    var captureWarnings: [String] { (warnings ?? []).filter { isCaptureWarning($0) } }

    /// The quieter half: true, worth one line, not worth alarm.
    var minorWarnings: [String] { (warnings ?? []).filter { !isCaptureWarning($0) } }

    /// The tracks with audio in them, mic first. Everything about playback —
    /// which decks exist, what the picker offers, whether "Mix" is even a
    /// thing for this meeting — is derived from this one list.
    var playableTracks: [(key: String, info: TrackInfo)] {
        guard let tracks, !tracks.isEmpty else {
            // A meeting.json carrying no tracks block at all. mic.wav is the
            // file that has always existed, so offer it rather than declaring
            // a meeting silent on the strength of a missing key; if it isn't
            // there, the transport says so out loud.
            return [(key: "mic", info: TrackInfo(file: "mic.wav", device: nil,
                                                 seconds: nil, start_offset: nil))]
        }
        return ["mic", "system"].compactMap { key in
            guard let info = tracks[key], info.hasAudio else { return nil }
            return (key, info)
        }
    }

    /// "Online call" or "In person" — a fact about the recording the document
    /// never showed, though it decides how the audio was captured.
    var modeLabel: String? {
        switch mode {
        case "inperson": return "In person"
        case "online": return "Online call"
        default: return nil
        }
    }

    /// The spoken language the engine detected, named in the reader's own
    /// language. Both tracks agree in every ordinary meeting; the mic is the
    /// tie-breaker because it is the track that always exists.
    var languageLabel: String? {
        guard let code = languages?["mic"] ?? languages?["system"] else { return nil }
        return Locale.current.localizedString(forIdentifier: code)
            ?? Locale.current.localizedString(forLanguageCode: String(code.prefix(2)))
    }

}

/// Did this warning cost the user audio, or is it only about labels?
/// One definition, because the sidebar row and the meeting page must agree
/// about which warnings are loud.
func isCaptureWarning(_ w: String) -> Bool {
    let text = w.lowercased()
    return ["blocked", "not be recorded", "no microphone", "was silent",
            "filled with silence", "stopped early", "did not shut down",
            "could not be routed"].contains { text.contains($0) }
}

/// Is the engine still working on this meeting?
///
/// "error" is TERMINAL: the run failed, the audio is saved, and nothing
/// changes until the user presses Reprocess. Anything that treats it as work
/// in flight (a poll loop, a shimmer) waits for a moment that never comes.
func meetingIsWorking(_ status: String?) -> Bool {
    ["recording", "processing"].contains(status ?? "done")
}

struct WaveformData: Decodable {
    let bins: Int
    let duration: Double
    let tracks: [String: [Double]]
}

struct RecorderStatus: Decodable {
    let recording: Bool
    let meeting_id: String?
    let elapsed: Double?
    let levels: [String: Double]?
}

struct Nudge: Decodable, Equatable {
    let id: String
    let kind: String?
    let title: String
    let body: String?
    let meeting_title: String?
}

struct LiveTurn: Decodable, Hashable {
    let seq: Int
    let track: String?
    let who: String?
    let start: Double?
    let end: Double?
    let text: String
}

struct LivePartial: Decodable {
    let start: Double?
    let end: Double?
    let text: String?
    let who: String?
}

struct LiveSnapshot: Decodable {
    let enabled: Bool
    let turns: [LiveTurn]
    let partials: [String: LivePartial]
    let seq: Int
}

/// One piece of engine work in flight, as /api/status reports it —
/// transcription under "jobs", summarizing under "summary_jobs".
/// `message` is the line the engine wants shown and it moves: "Loading
/// model…", "Downloading the speech model, 340 MB of 2.5 GB", "Transcribing
/// 4/9". Showing the static word "Processing" instead is how a ten-minute
/// first-run download looked identical to a hang.
struct EngineJob: Decodable {
    let state: String?      // "processing" | "done" | "error"
    let message: String?    // live progress from the worker
}

/// One model the engine needs on disk before it can transcribe.
/// `state` is "pending" | "downloading" | "done" | "present" | "skipped" |
/// "error"; `present` is the engine's answer about the disk and outranks it.
struct ModelComponent: Decodable, Identifiable, Hashable {
    let key: String
    let label: String
    let detail: String?
    let required: Bool?
    let present: Bool?
    let state: String?
    let bytes: Int64?
    let total_bytes: Int64?
    var id: String { key }

    var done: Bool { present == true || state == "done" }
}

/// What a fresh install still has to download before its first meeting.
///
/// `state` is "checking" (the engine is still working it out) | "ready"
/// (everything is already here) | "missing" | "downloading" | "done" |
/// "error". `ready` is the one field worth branching on for "can this Mac
/// transcribe right now"; the rest is how to say it.
struct ModelStatus: Decodable {
    let state: String
    let ready: Bool?
    let message: String?
    let downloaded_bytes: Int64?
    let total_bytes: Int64?
    /// No new bytes for the best part of a minute. The download is not
    /// cancelled, it is worth saying out loud.
    let stalled: Bool?
    let error: String?
    let components: [ModelComponent]?

    var busy: Bool { state == "checking" || state == "downloading" }
    var fraction: Double {
        guard let total = total_bytes, total > 0 else { return 0 }
        return min(1, max(0, Double(downloaded_bytes ?? 0) / Double(total)))
    }
}

struct Citation: Decodable, Hashable {
    let t: Double?
    let quote: String?
}

struct AskAnswer: Decodable {
    let answer: String?
    let citations: [Citation]?
    let error: String?
    /// The engine has no summarizer signed in. Its message says so; this flag
    /// is how a client knows the fix is Settings, not a retry.
    let needs_claude: Bool?
}

/// One earlier turn of the conversation, sent back so a follow-up like "and
/// what did he say about the deadline?" still has a subject. app.py has taken
/// `history` since the web UI shipped; the native Ask never sent it, which is
/// what made every question the first one.
struct AskMessage: Identifiable, Hashable {
    enum Role: String { case user, assistant }
    let id = UUID()
    let role: Role
    var text: String
    var citations: [Citation] = []
    /// The answer is still being written — a preview, never history.
    var streaming = false

    var wire: [String: String] { ["role": role.rawValue, "text": text] }
}

/// An ask that produced no answer. `needsClaude` carries the engine's own
/// "you have no summarizer signed in" so the UI can point at Settings.
struct AskFailure: Error {
    let message: String
    let needsClaude: Bool
}

/// One line of the engine's NDJSON ask stream:
///   {"type":"delta","text":"…"}   zero or more, plain answer text
///   {"type":"done","answer":"…","citations":[…]}   ends it
///   {"type":"error","error":"…"}  ends it, if it broke after some text
private struct AskStreamEvent: Decodable {
    let type: String?
    let text: String?
    let answer: String?
    let citations: [Citation]?
    let error: String?
    let needs_claude: Bool?
}

let askStreamMediaType = "application/x-ndjson"

// MARK: - Client

struct MeetingNote: Decodable, Identifiable, Hashable {
    let t: Double?
    let text: String
    var id: String { "\(t ?? -1)-\(text)" }
}

private struct NotesEnvelope: Decodable { let notes: [MeetingNote] }

/// One person the engine can recognize by voice across meetings.
/// `speech_seconds` is wall-clock speech the profile was built from;
/// `n_samples` how many meetings contributed. No embedding ever crosses this
/// boundary, and neither does the engine's internal window time — that number
/// counts each second of speech about twice and has no business in a UI.
///
/// `name` is a LABEL, not an identity: two people can both be Jess, so two
/// profiles can share a name and only `id` tells them apart.
struct VoiceProfile: Decodable, Identifiable, Hashable {
    let id: String
    let name: String
    let speech_seconds: Double?
    let n_samples: Int
    let updated: Double?
}

private struct VoiceProfilesEnvelope: Decodable { let profiles: [VoiceProfile] }

/// The rename response. `voice_profile` is the engine handing back the profile
/// this rename enrolled into, or nil when nothing was saved. It has to be the
/// engine's answer: the voice picks the profile, names are allowed to collide,
/// and the name is stripped and truncated on the way in — so a client that
/// went looking for it afterwards would be re-deriving all three rules and
/// could still land on the wrong person.
struct SpeakerRenameResult: Decodable { let voice_profile: VoiceProfile? }



enum API {
    static func meetings(query: String = "") async throws -> [MeetingListItem] {
        var path = "api/meetings?limit=200"
        if !query.isEmpty,
           let q = query.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) {
            path += "&q=\(q)"
        }
        return try await get(path, as: MeetingsPage.self).items
    }

    static func notes(_ id: String) async throws -> [MeetingNote] {
        try await get("api/meetings/\(id)/notes", as: NotesEnvelope.self).notes
    }

    static func rename(_ id: String, title: String) async -> Bool {
        await post("api/meetings/\(id)/title", body: ["title": title])
    }

    /// Kick off (re-)summarization. Returns (ok, engineMessage) — the engine
    /// explains refusals (no transcript yet, no summarizer available, one
    /// already running).
    static func summarize(_ id: String) async -> (Bool, String?) {
        var req = URLRequest(url: engineBase.appendingPathComponent("api/meetings/\(id)/summarize"))
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = Data("{}".utf8)
        req.timeoutInterval = 15
        guard let (data, resp) = try? await URLSession.shared.data(for: req),
              let code = (resp as? HTTPURLResponse)?.statusCode else {
            return (false, "The engine didn't answer.")
        }
        if (200..<300).contains(code) { return (true, nil) }
        let err = ((try? JSONSerialization.jsonObject(with: data)) as? [String: Any])?["error"] as? String
        return (false, err ?? "Summarising failed (\(code)).")
    }

    /// Rename one speaker of a meeting. Returns the profile the engine
    /// enrolled that person's voice into, if it did — renaming is how
    /// enrollment fires, so the caller owns telling the user it happened.
    static func renameSpeaker(_ id: String, key: String,
                              name: String) async throws -> SpeakerRenameResult {
        var req = URLRequest(url: engineBase.appendingPathComponent("api/meetings/\(id)/speakers"))
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try JSONSerialization.data(withJSONObject: ["key": key, "name": name])
        req.timeoutInterval = 15
        let (data, resp) = try await URLSession.shared.data(for: req)
        guard let code = (resp as? HTTPURLResponse)?.statusCode,
              (200..<300).contains(code) else { throw URLError(.badServerResponse) }
        return (try? JSONDecoder().decode(SpeakerRenameResult.self, from: data))
            ?? SpeakerRenameResult(voice_profile: nil)
    }

    /// Everyone this Mac can currently recognize by voice.
    static func voiceProfiles() async throws -> [VoiceProfile] {
        try await get("api/voice-profiles", as: VoiceProfilesEnvelope.self).profiles
    }

    /// Forget one person's voice, every sample of it. A 404 is success: the
    /// profile is already gone, which is the state the caller asked for, and
    /// treating it as failure would strand a "Forget" button that can never
    /// succeed once the same voice was forgotten from Settings.
    static func deleteVoiceProfile(_ id: String) async throws {
        var req = URLRequest(url: engineBase.appendingPathComponent("api/voice-profiles/\(id)"))
        req.httpMethod = "DELETE"
        req.timeoutInterval = 10
        let (_, resp) = try await URLSession.shared.data(for: req)
        guard let code = (resp as? HTTPURLResponse)?.statusCode,
              (200..<300).contains(code) || code == 404
        else { throw URLError(.badServerResponse) }
    }

    /// What the engine still has to download before it can transcribe.
    /// nil only when the engine isn't answering — every other outcome,
    /// including "nothing to do", comes back as a ModelStatus.
    static func modelStatus() async -> ModelStatus? {
        try? await get("api/models/status", as: ModelStatus.self)
    }

    /// Fetch the models now rather than during the user's first meeting.
    /// A 409 (one already running) is success from the caller's point of
    /// view: the download the caller wanted is happening.
    @discardableResult
    static func prefetchModels() async -> Bool {
        var req = URLRequest(url: engineBase.appendingPathComponent("api/models/prefetch"))
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = Data("{}".utf8)
        req.timeoutInterval = 10
        guard let (_, resp) = try? await URLSession.shared.data(for: req),
              let code = (resp as? HTTPURLResponse)?.statusCode else { return false }
        return (200..<300).contains(code) || code == 409
    }

    private struct EngineStatus: Decodable {
        let jobs: [String: EngineJob]?
        let summary_jobs: [String: EngineJob]?
    }

    /// The live summary job for one meeting, if any.
    static func summaryJob(_ id: String) async -> EngineJob? {
        (try? await get("api/status", as: EngineStatus.self))?.summary_jobs?[id]
    }

    /// Every transcription job the engine is running, keyed by meeting id.
    /// One request covers the whole library, so the list poll can carry the
    /// progress rather than each row opening its own.
    static func processingJobs() async -> [String: EngineJob] {
        (try? await get("api/status", as: EngineStatus.self))?.jobs ?? [:]
    }

    /// Transcribe a meeting's saved audio again — the recovery path for one
    /// that failed, and the only one there is. Returns (ok, engineMessage):
    /// the engine refuses while the meeting is recording, while it is being
    /// summarized, and when the audio is gone, and each refusal says which.
    static func reprocess(_ id: String) async -> (Bool, String?) {
        var req = URLRequest(url: engineBase.appendingPathComponent("api/meetings/\(id)/process"))
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = Data("{}".utf8)
        req.timeoutInterval = 15
        guard let (data, resp) = try? await URLSession.shared.data(for: req),
              let code = (resp as? HTTPURLResponse)?.statusCode else {
            return (false, "The engine didn't answer.")
        }
        if (200..<300).contains(code) { return (true, nil) }
        let err = ((try? JSONSerialization.jsonObject(with: data)) as? [String: Any])?["error"] as? String
        return (false, err ?? "Reprocess failed (\(code)).")
    }

    /// Returns (ok, engineMessage). The engine refuses while a meeting is
    /// still transcribing — surface its words, don't guess.
    static func deleteMeeting(_ id: String) async -> (Bool, String?) {
        var req = URLRequest(url: engineBase.appendingPathComponent("api/meetings/\(id)"))
        req.httpMethod = "DELETE"
        req.timeoutInterval = 10
        guard let (data, resp) = try? await URLSession.shared.data(for: req),
              let code = (resp as? HTTPURLResponse)?.statusCode else {
            return (false, "The engine didn't answer.")
        }
        if (200..<300).contains(code) { return (true, nil) }
        let err = ((try? JSONSerialization.jsonObject(with: data)) as? [String: Any])?["error"] as? String
        return (false, err ?? "Delete failed (\(code)).")
    }

    static func meeting(_ id: String) async throws -> MeetingDetail {
        try await get("api/meetings/\(id)", as: MeetingDetail.self)
    }

    /// Ask one question about one meeting, reading the answer as it is
    /// written.
    ///
    /// Three things the one-shot version didn't do, all of them already
    /// supported by app.py: it sends `history`, so a follow-up knows what it
    /// is following up on; it opts into the NDJSON stream, so the words appear
    /// as the model writes them instead of after up to three minutes of
    /// nothing; and it keeps the engine's error text, including the
    /// needs_claude flag that says the fix is signing in rather than asking
    /// again.
    ///
    /// The deltas are a PREVIEW: only the "done" event carries citations
    /// validated against the turns the model was actually shown, so the
    /// returned answer — not the accumulated deltas — is what gets kept.
    @MainActor
    static func askStream(_ id: String, question: String,
                          history: [AskMessage],
                          onDelta: (String) -> Void) async throws -> AskAnswer {
        var req = URLRequest(url: engineBase.appendingPathComponent("api/meetings/\(id)/ask"))
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.setValue(askStreamMediaType, forHTTPHeaderField: "Accept")
        req.httpBody = try JSONSerialization.data(withJSONObject: [
            "question": question,
            "history": history.map(\.wire),
            "stream": true,
        ])
        // The on-device path (Apple Intelligence) writes no deltas at all and
        // can take minutes, so this is the gap between BYTES the request has
        // to tolerate, not just the time to first response.
        req.timeoutInterval = 300

        let (bytes, resp) = try await URLSession.shared.bytes(for: req)
        guard let http = resp as? HTTPURLResponse else {
            throw AskFailure(message: "The engine didn't answer.", needsClaude: false)
        }
        guard (200..<300).contains(http.statusCode) else {
            let body = try? JSONDecoder().decode(AskAnswer.self, from: await collect(bytes))
            throw AskFailure(
                message: body?.error ?? "The engine wouldn't answer that (\(http.statusCode)).",
                needsClaude: body?.needs_claude == true)
        }
        // An engine that doesn't know the flag answers in one object, exactly
        // as it always has. No version check anywhere — the content type is
        // the whole test.
        guard (http.value(forHTTPHeaderField: "Content-Type") ?? "")
            .contains(askStreamMediaType) else {
            guard let answer = try? JSONDecoder()
                .decode(AskAnswer.self, from: await collect(bytes)) else {
                throw AskFailure(message: "The engine's answer couldn't be read.",
                                 needsClaude: false)
            }
            return answer
        }

        var settled: AskAnswer?
        for try await line in bytes.lines {
            let trimmed = line.trimmingCharacters(in: .whitespacesAndNewlines)
            // One unparseable line must not cost an answer that is otherwise
            // arriving: "done" is read on its own line like everything else.
            guard !trimmed.isEmpty, let data = trimmed.data(using: .utf8),
                  let event = try? JSONDecoder().decode(AskStreamEvent.self, from: data)
            else { continue }
            switch event.type {
            case "delta":
                onDelta(event.text ?? "")
            case "done":
                settled = AskAnswer(answer: event.answer, citations: event.citations,
                                    error: nil, needs_claude: nil)
            case "error":
                throw AskFailure(message: event.error ?? "Could not answer that question.",
                                 needsClaude: event.needs_claude == true)
            default:
                continue
            }
        }
        guard let settled else {
            // The connection dropped between the last delta and the "done"
            // that carries the citations. There is no answer to keep.
            throw AskFailure(message: "The answer stopped part-way through. Ask again.",
                             needsClaude: false)
        }
        return settled
    }

    /// Drain a body we are only going to read as one JSON object — the error
    /// path, and the path where the engine answered without streaming. A read
    /// that breaks part-way hands back what arrived; the decode above decides
    /// whether that was enough.
    private static func collect(_ bytes: URLSession.AsyncBytes) async -> Data {
        var data = Data()
        do {
            for try await byte in bytes { data.append(byte) }
        } catch {
            return data
        }
        return data
    }

    /// The engine's own export of one meeting — the same Markdown or plain
    /// text the web UI has always offered, built server-side from
    /// meeting.json, so the file carries the summary and the speaking stats
    /// and not just the turns. `fmt` is "md" or "txt".
    static func export(_ id: String, fmt: String) async -> Data? {
        var comps = URLComponents(
            url: engineBase.appendingPathComponent("api/meetings/\(id)/export"),
            resolvingAgainstBaseURL: false)
        comps?.queryItems = [URLQueryItem(name: "fmt", value: fmt)]
        guard let url = comps?.url else { return nil }
        var req = URLRequest(url: url)
        req.timeoutInterval = 30
        guard let (data, resp) = try? await URLSession.shared.data(for: req),
              let code = (resp as? HTTPURLResponse)?.statusCode,
              (200..<300).contains(code) else { return nil }
        return data
    }

    static func audioURL(_ id: String, track: String) -> URL {
        engineBase.appendingPathComponent("api/meetings/\(id)/audio/\(track)")
    }

    static func waveform(_ id: String) async throws -> WaveformData {
        try await get("api/meetings/\(id)/waveform", as: WaveformData.self)
    }

    static func recorderStatus() async throws -> RecorderStatus {
        try await get("api/record/status", as: RecorderStatus.self)
    }

    private struct NudgeEnvelope: Decodable { let nudge: Nudge? }

    static func nudge() async throws -> Nudge? {
        try await get("api/nudges", as: NudgeEnvelope.self).nudge
    }

    static func live(since: Int) async throws -> LiveSnapshot {
        try await get("api/live?since=\(since)", as: LiveSnapshot.self)
    }

    /// What became of one note. app.py's contract: every non-200 stored
    /// NOTHING, so the caller still owns the text either way. The difference
    /// that matters to a retry is whether the engine will ever accept it —
    /// a 4xx (too long, meeting deleted, malformed id) refuses the same text
    /// forever, while an unreachable engine is worth asking again.
    enum NoteResult {
        case stored
        case refused(String)
        case unreachable
    }

    /// File one note against the meeting it was typed in. `meetingID` is
    /// omitted only for a note typed with nothing in view, which is the one
    /// case where "whatever is live" is what the user means.
    static func note(text: String, t: Double?, meetingID: String?) async -> NoteResult {
        var body: [String: Any] = ["text": text]
        if let meetingID { body["meeting_id"] = meetingID }
        if let t { body["t"] = t }
        var req = URLRequest(url: engineBase.appendingPathComponent("api/record/note"))
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: body)
        req.timeoutInterval = 10
        guard let (data, resp) = try? await URLSession.shared.data(for: req),
              let code = (resp as? HTTPURLResponse)?.statusCode else {
            return .unreachable
        }
        if (200..<300).contains(code) { return .stored }
        guard (400..<500).contains(code) else { return .unreachable }
        let err = ((try? JSONSerialization.jsonObject(with: data)) as? [String: Any])?["error"] as? String
        return .refused(err ?? "The engine wouldn't take that note (\(code)).")
    }

    @discardableResult
    static func post(_ path: String, body: [String: Any] = [:]) async -> Bool {
        var req = URLRequest(url: engineBase.appendingPathComponent(path))
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: body)
        req.timeoutInterval = 8
        guard let (_, resp) = try? await URLSession.shared.data(for: req),
              let code = (resp as? HTTPURLResponse)?.statusCode else { return false }
        return (200..<300).contains(code)
    }

    static func get<T: Decodable>(_ path: String, as type: T.Type) async throws -> T {
        let url = URL(string: path, relativeTo: engineBase)!
        var req = URLRequest(url: url)
        req.timeoutInterval = 10
        let (data, _) = try await URLSession.shared.data(for: req)
        return try JSONDecoder().decode(T.self, from: data)
    }
}

// MARK: - Shared formatting

/// The whole transcript as text: "[3:07] Priya: …" per line.
func transcriptText(_ detail: MeetingDetail) -> String {
    (detail.turns ?? []).map { turn in
        let name = turn.speaker.flatMap { detail.speakers?[$0] }
            ?? (turn.speaker == "you" ? "You" : "Speaker")
        return "[\(clock(turn.start))] \(name): \(turn.text)"
    }.joined(separator: "\n")
}

func clock(_ seconds: Double) -> String {
    let s = Int(seconds)
    if s >= 3600 { return String(format: "%d:%02d:%02d", s / 3600, (s % 3600) / 60, s % 60) }
    return String(format: "%d:%02d", s / 60, s % 60)
}

let createdParser: DateFormatter = {
    let f = DateFormatter()
    f.dateFormat = "yyyy-MM-dd'T'HH:mm:ss"
    f.locale = Locale(identifier: "en_US_POSIX")
    return f
}()
