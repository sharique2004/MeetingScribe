// One meeting, in the shape the Mac engine already writes.
//
// The field names are meeting.json's, not new ones: `id`, `title`, `created`,
// `duration`, `turns`, `summary`. That is not tidiness, it is the migration
// path — sync.py already uploads exactly this shape (see sync._payload), so a
// phone that speaks it can read meetings recorded on the Mac without either
// side learning a second vocabulary.
import Foundation

struct Turn: Codable, Identifiable, Hashable {
    var speaker: String
    var start: Double
    var end: Double
    var text: String

    var id: String { "\(speaker)-\(start)-\(end)" }
}

struct MeetingSummary: Codable, Hashable {
    var headline: String?
    var tldr: String?
    var keyPoints: [String]
    var actionItems: [String]

    enum CodingKeys: String, CodingKey {
        case headline, tldr
        case keyPoints = "key_points"
        case actionItems = "action_items"
    }

    init(headline: String? = nil, tldr: String? = nil,
         keyPoints: [String] = [], actionItems: [String] = []) {
        self.headline = headline
        self.tldr = tldr
        self.keyPoints = keyPoints
        self.actionItems = actionItems
    }
}

struct Meeting: Codable, Identifiable, Hashable {
    var id: String
    var title: String
    var created: Date
    var duration: Double
    /// Where the audio lives, relative to the store's directory. Relative on
    /// purpose: iOS moves the container between launches and installs, so an
    /// absolute URL saved today is a dead path tomorrow.
    var audioFilename: String?
    var turns: [Turn]
    var summary: MeetingSummary?
    /// What the engine could not do, in words the reader can act on.
    var warning: String?

    var hasTranscript: Bool { !turns.isEmpty }

    var wordCount: Int {
        turns.reduce(0) { $0 + $1.text.split(separator: " ").count }
    }

    /// A stable id in the Mac engine's format: yyyyMMdd-HHmmss.
    static func makeID(at date: Date) -> String {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "yyyyMMdd-HHmmss"
        return f.string(from: date)
    }

    static func defaultTitle(at date: Date) -> String {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "d MMM yyyy, HH:mm"
        return "Meeting " + f.string(from: date)
    }
}
