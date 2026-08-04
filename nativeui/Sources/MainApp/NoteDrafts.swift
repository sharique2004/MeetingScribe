// A note the user has typed and not sent is the one piece of text in this app
// that exists nowhere but a text field: it is not in notes.jsonl, not in
// meeting.json, and no engine has ever seen it. This file is the disk behind
// that field.
//
// Two promises, each with its own mechanism.
//
// NEVER LOST. The field is written to disk on the keystroke, not on a
// debounce: a debounce that restarts on every key never fires for somebody who
// does not stop typing, which loses the text of the FAST typist — the one with
// the most to lose. A draft write is a few hundred microseconds, so the
// keystroke can afford it.
//
// NEVER MISFILED. Every draft carries the id of the meeting it was typed in,
// bound the moment the field stops being empty and never re-pointed at a later
// meeting. A note left in the field when a meeting ends is filed against THAT
// meeting; one that outlived a quit or a crash is filed against the meeting it
// was typed in, never against whatever happens to be recording when the app
// comes back. app.py's POST /api/record/note takes "meeting_id" for exactly
// this and documents the same shape.
import Foundation

/// One unsent note. `t` is the offset the note was started at; nil means the
/// meeting clock had stopped, and the engine stamps it with the meeting's
/// real duration rather than a time that never happened.
struct NoteDraft: Codable, Equatable {
    var meetingID: String?
    var t: Double?
    var text: String
}

/// Where unsent notes live between the keystroke and the engine accepting
/// them. One small JSON file, rewritten atomically, beside the recordings.
struct NoteDraftStore {
    /// What is in the field right now, everything handed over and waiting for
    /// the engine, and the notes the engine refused outright.
    ///
    /// Refused notes are kept and never retried: the engine's 4xx answers
    /// (too long, meeting deleted) mean the same text will be refused again
    /// forever, and leaving one at the head of the queue would block every
    /// note typed after it. They stay in this file, which is plain readable
    /// JSON, rather than being thrown away.
    struct Contents: Codable {
        var live: NoteDraft?
        var pending: [NoteDraft] = []
        var refused: [NoteDraft] = []

        init(live: NoteDraft? = nil, pending: [NoteDraft] = [], refused: [NoteDraft] = []) {
            self.live = live
            self.pending = pending
            self.refused = refused
        }

        /// Hand-written so a file from a build that didn't have one of these
        /// keys still decodes. The alternative is a throw, and a throw here
        /// means every unsent note in the file is lost.
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            live = try? c.decodeIfPresent(NoteDraft.self, forKey: .live)
            pending = (try? c.decodeIfPresent([NoteDraft].self, forKey: .pending)) ?? []
            refused = (try? c.decodeIfPresent([NoteDraft].self, forKey: .refused)) ?? []
        }
    }

    /// MEETINGSCRIBE_DATA when the app set one (an experiment, a test run),
    /// the real data directory otherwise — the same rule the engine follows,
    /// so drafts never end up in a different place than the meetings.
    static func defaultDirectory() -> URL {
        let env = ProcessInfo.processInfo.environment["MEETINGSCRIBE_DATA"]
        let base = env.map { URL(fileURLWithPath: NSString(string: $0).expandingTildeInPath) }
            ?? FileManager.default.homeDirectoryForCurrentUser
                .appendingPathComponent(".meetingscribe")
        return base.appendingPathComponent("note-drafts")
    }

    let directory: URL

    init(directory: URL? = nil) {
        self.directory = directory ?? Self.defaultDirectory()
    }

    private var file: URL { directory.appendingPathComponent("live-notes.json") }

    func load() -> Contents {
        guard let data = try? Data(contentsOf: file),
              let contents = try? JSONDecoder().decode(Contents.self, from: data)
        else { return Contents() }
        return contents
    }

    /// Best effort, and deliberately silent about it: a draft that cannot
    /// reach the disk must never be what stops the user typing. Nothing is
    /// removed on failure, so the previous copy stands.
    func save(_ contents: Contents) {
        if contents.live == nil, contents.pending.isEmpty, contents.refused.isEmpty {
            try? FileManager.default.removeItem(at: file)
            return
        }
        do {
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
            try JSONEncoder().encode(contents).write(to: file, options: .atomic)
        } catch {
            NSLog("MeetingScribe: could not save the note draft: \(error)")
        }
    }
}
