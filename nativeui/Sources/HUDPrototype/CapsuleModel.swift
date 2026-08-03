// State for the recording capsule, driven by the real MeetingScribe backend
// on 127.0.0.1:5005:
//
//   GET  /api/record/status   {recording, meeting_id, elapsed, levels}
//   POST /api/record/start    {}                       start a meeting
//   POST /api/record/stop     {}                       stop it
//   POST /api/record/note     {text, t, meeting_id}    one timestamped note
//   GET  /api/meetings/<id>/notes                      notes already filed
//
// Notes follow the panel contract in app.py: every note carries the
// meeting_id it belongs to and its own timestamp; a failed POST keeps the
// text in the draft so nothing the user typed is ever lost.
import Foundation

struct SentNote: Identifiable {
    let id = UUID()
    let t: Double?
    let text: String
}

@MainActor
final class CapsuleModel: ObservableObject {
    enum Phase {
        case offline    // backend not reachable
        case idle       // backend up, nothing recording
        case recording
    }

    @Published var phase: Phase = .offline
    @Published var elapsed: TimeInterval = 0
    @Published var notesOpen = false
    @Published var sentNotes: [SentNote] = []
    @Published var draft = ""
    @Published var noteSendFailed = false

    private(set) var meetingId: String?
    private let base = URL(string: "http://127.0.0.1:5005")!
    private var pollTask: Task<Void, Never>?

    init() {
        notesOpen = CommandLine.arguments.contains("--notes-open")
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                await self?.tick()
                let recording = self?.phase == .recording
                try? await Task.sleep(nanoseconds: recording ? 200_000_000 : 1_000_000_000)
            }
        }
    }

    var timeString: String {
        let s = Int(elapsed)
        if s >= 3600 {
            return String(format: "%d:%02d:%02d", s / 3600, (s % 3600) / 60, s % 60)
        }
        return String(format: "%d:%02d", s / 60, s % 60)
    }

    // MARK: - Recorder state

    private func tick() async {
        guard let st = await getJSON("api/record/status") else {
            phase = .offline
            return
        }
        let recording = st["recording"] as? Bool ?? false
        if recording {
            elapsed = st["elapsed"] as? Double ?? elapsed
            let id = st["meeting_id"] as? String
            if id != meetingId, let id {
                meetingId = id
                await loadExistingNotes(id)
            }
            phase = .recording
        } else {
            if phase == .recording {   // just stopped
                notesOpen = false
                sentNotes = []
                meetingId = nil
            }
            elapsed = 0
            phase = .idle
        }
    }

    func startRecording() {
        Task {
            _ = await postJSON("api/record/start", body: [:])
            await tick()
        }
    }

    func stopRecording() {
        Task {
            _ = await postJSON("api/record/stop", body: [:])
            await tick()
        }
    }

    // MARK: - Notes

    func sendNote() {
        let text = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, let meetingId else { return }
        let t = elapsed
        Task {
            let ok = await postJSON("api/record/note",
                                    body: ["text": text, "t": t, "meeting_id": meetingId])
            if ok {
                sentNotes.append(SentNote(t: t, text: text))
                draft = ""
                noteSendFailed = false
            } else {
                noteSendFailed = true   // draft stays put — the user still holds the text
            }
        }
    }

    private func loadExistingNotes(_ id: String) async {
        guard let obj = await getJSON("api/meetings/\(id)/notes"),
              let raw = obj["notes"] as? [[String: Any]] else {
            sentNotes = []
            return
        }
        sentNotes = raw.compactMap { n in
            guard let text = n["text"] as? String else { return nil }
            return SentNote(t: n["t"] as? Double, text: text)
        }
    }

    // MARK: - HTTP

    private func getJSON(_ path: String) async -> [String: Any]? {
        var req = URLRequest(url: base.appendingPathComponent(path))
        req.timeoutInterval = 2
        guard let (data, resp) = try? await URLSession.shared.data(for: req),
              (resp as? HTTPURLResponse)?.statusCode == 200 else { return nil }
        return (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
    }

    private func postJSON(_ path: String, body: [String: Any]) async -> Bool {
        var req = URLRequest(url: base.appendingPathComponent(path))
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: body)
        req.timeoutInterval = 5
        guard let (_, resp) = try? await URLSession.shared.data(for: req),
              let code = (resp as? HTTPURLResponse)?.statusCode else { return false }
        return (200..<300).contains(code)
    }
}
