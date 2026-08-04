// Client for the local MeetingScribe engine (Flask on 127.0.0.1:5005).
// Read-only endpoints plus /ask. Shapes decoded leniently — every field the
// UI can live without is optional.
import Foundation

let engineBase = URL(string: "http://127.0.0.1:5005")!

// MARK: - Models

struct MeetingListItem: Decodable, Identifiable, Hashable {
    let id: String
    let title: String
    let created: String
    let duration: Double?
    let status: String?
    let speakers: Int?
}

private struct MeetingsPage: Decodable { let items: [MeetingListItem] }

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

struct SpeakerStats: Decodable {
    let seconds: Double?
    let words: Int?
    let share: Double?
    let wpm: Double?
    let questions: Int?
}

struct MeetingStats: Decodable {
    let per_speaker: [String: SpeakerStats]?
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

struct SummaryJob: Decodable {
    let state: String?      // "processing" | "done" | "error"
    let message: String?    // live progress from the writer
}

struct Citation: Decodable, Hashable {
    let t: Double?
    let quote: String?
}

struct AskAnswer: Decodable {
    let answer: String?
    let citations: [Citation]?
    let error: String?
}

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

struct CalendarEvent: Decodable, Hashable {
    let title: String?
    let start: String?
}

private struct CalendarToday: Decodable {
    let available: Bool?
    let events: [CalendarEvent]?
}

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

    static func calendarToday() async -> [CalendarEvent] {
        (try? await get("api/calendar/today", as: CalendarToday.self).events) ?? []
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
        return (false, err ?? "Summarize failed (\(code)).")
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

    /// The live summary job for one meeting, if any.
    static func summaryJob(_ id: String) async -> SummaryJob? {
        struct EngineStatus: Decodable { let summary_jobs: [String: SummaryJob]? }
        return (try? await get("api/status", as: EngineStatus.self))?.summary_jobs?[id]
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

    static func ask(_ id: String, question: String) async throws -> AskAnswer {
        var req = URLRequest(url: engineBase.appendingPathComponent("api/meetings/\(id)/ask"))
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try JSONSerialization.data(withJSONObject: ["question": question])
        req.timeoutInterval = 180
        let (data, _) = try await URLSession.shared.data(for: req)
        return try JSONDecoder().decode(AskAnswer.self, from: data)
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
