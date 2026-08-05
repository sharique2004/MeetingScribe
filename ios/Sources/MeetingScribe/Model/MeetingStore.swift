// Where meetings live on the phone.
//
// One folder per meeting under Application Support, exactly like the Mac's
// ~/.meetingscribe/recordings: audio beside its meeting.json, so a recording
// is one self-contained directory that can be deleted, exported or synced as a
// unit. The Mac learned this the hard way and the phone should not relearn it.
//
// Writes are atomic. A phone is killed mid-write far more often than a Mac is,
// and a half-written meeting.json loses a meeting that was already recorded.
import Foundation
import Observation

@MainActor
@Observable
final class MeetingStore {
    private(set) var meetings: [Meeting] = []

    private let root: URL
    private let decoder: JSONDecoder = {
        let d = JSONDecoder()
        d.dateDecodingStrategy = .iso8601
        return d
    }()
    private let encoder: JSONEncoder = {
        let e = JSONEncoder()
        e.dateEncodingStrategy = .iso8601
        e.outputFormatting = [.prettyPrinted, .sortedKeys]
        return e
    }()

    init() {
        let base = URL.applicationSupportDirectory.appending(path: "MeetingScribe/recordings")
        try? FileManager.default.createDirectory(at: base, withIntermediateDirectories: true)
        root = base
        load()
    }

    /// The folder a meeting owns. Resolved fresh every time rather than
    /// stored: iOS rewrites the container path between installs, so a URL
    /// captured at record time is a dead path after the next update.
    func directory(for meeting: Meeting) -> URL {
        root.appending(path: meeting.id)
    }

    func audioURL(for meeting: Meeting) -> URL? {
        guard let name = meeting.audioFilename else { return nil }
        return directory(for: meeting).appending(path: name)
    }

    /// A folder for a recording that has not finished yet.
    func prepareDirectory(id: String) throws -> URL {
        let dir = root.appending(path: id)
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir
    }

    func load() {
        let fm = FileManager.default
        let folders = (try? fm.contentsOfDirectory(at: root,
                                                   includingPropertiesForKeys: nil)) ?? []
        var found: [Meeting] = []
        for folder in folders {
            let json = folder.appending(path: "meeting.json")
            guard let data = try? Data(contentsOf: json),
                  let meeting = try? decoder.decode(Meeting.self, from: data) else { continue }
            found.append(meeting)
        }
        meetings = found.sorted { $0.created > $1.created }
    }

    func save(_ meeting: Meeting) throws {
        let dir = try prepareDirectory(id: meeting.id)
        let data = try encoder.encode(meeting)
        // .atomic: a phone is killed mid-write often enough that a torn
        // meeting.json is a real way to lose a meeting that was recorded fine.
        try data.write(to: dir.appending(path: "meeting.json"), options: .atomic)
        if let index = meetings.firstIndex(where: { $0.id == meeting.id }) {
            meetings[index] = meeting
        } else {
            meetings.append(meeting)
            meetings.sort { $0.created > $1.created }
        }
    }

    func delete(_ meeting: Meeting) {
        try? FileManager.default.removeItem(at: directory(for: meeting))
        meetings.removeAll { $0.id == meeting.id }
    }
}
