// The interleaver: turns a meeting's summary + the user's own timestamped
// notes into one document of blocks. The thesis is two inks, one flow —
// the human's notes structure the page at full ink; the machine's writing
// nests beneath at secondary ink. Every AI claim tries to bind to the
// transcript turn that produced it, so evidence can unfold in place.
import Foundation
import SwiftUI

/// Which to-dos are ticked, kept where the app can honour the promise its own
/// checkbox makes.
///
/// The tick used to live in a `@State` inside the row: it survived exactly
/// until the next tab switch, which is the worst of both worlds — a control
/// that looks like a commitment and behaves like a hover state. Persisting is
/// the right answer, and this is where it can be persisted TODAY: the engine
/// has no write path for summary state (its only meeting writes are
/// /title and /speakers), so a tick has nowhere to go in meeting.json without
/// a new endpoint. Kept here, it survives tab switches, meeting switches,
/// relaunches and engine restarts; what it does not do is travel to the phone
/// or into an export, and moving it into meeting.json is the follow-up.
///
/// Items are keyed by their NORMALISED task text, not their position: the
/// summary is regenerated on Re-analyse and the order changes, so an index
/// would tick the wrong line. A re-analysis that REWORDS a task drops its
/// tick, which is honest — it is not the same sentence any more.
@MainActor
final class ActionItemStore: ObservableObject {
    static let shared = ActionItemStore()

    /// The read-through cache of what is on disk. Deliberately NOT
    /// `@Published`: `loaded(_:)` fills it on first read, and the first read
    /// happens inside a row's `body` — publishing from there is a write to
    /// state SwiftUI is in the middle of reading, which is exactly the
    /// "Publishing changes from within view updates" it warns about. Nothing
    /// observes this dictionary; `revision` below is what views watch.
    private var done: [String: Set<String>] = [:]
    /// The one thing this store publishes, bumped when a tick actually
    /// changes. Reading a to-do's state stays silent; changing one redraws.
    @Published private(set) var revision = 0
    private let defaults = UserDefaults.standard

    private func storageKey(_ meetingID: String) -> String { "ms.todos.v1.\(meetingID)" }

    private func loaded(_ meetingID: String) -> Set<String> {
        if let cached = done[meetingID] { return cached }
        let stored = Set(defaults.stringArray(forKey: storageKey(meetingID)) ?? [])
        done[meetingID] = stored
        return stored
    }

    func isDone(meeting: String, task: String) -> Bool {
        loaded(meeting).contains(Self.key(task))
    }

    func set(_ isDone: Bool, meeting: String, task: String) {
        var items = loaded(meeting)
        let key = Self.key(task)
        if isDone { items.insert(key) } else { items.remove(key) }
        done[meeting] = items
        defaults.set(Array(items), forKey: storageKey(meeting))
        revision += 1
    }

    /// The identity of a to-do: its words, with case, whitespace and trailing
    /// punctuation taken out so cosmetic differences don't lose a tick.
    static func key(_ task: String) -> String {
        task.lowercased()
            .components(separatedBy: .whitespacesAndNewlines)
            .filter { !$0.isEmpty }
            .joined(separator: " ")
            .trimmingCharacters(in: CharacterSet(charactersIn: ".!?;:,"))
    }
}

enum DocumentBlock: Identifiable {
    case userParagraph(String)                            // a long user note
    /// A note whose "t" is null: the engine could not vouch for when in the
    /// meeting it was typed. It has no window to sit in, so it gets its own
    /// place at the end rather than being coerced to 0:00 — see the "t"
    /// contract at the top of notes.py.
    case untimedNote(id: String, text: String)
    case bullet(id: String, text: String, evidence: Turn?)
    case actionItem(id: String, item: ActionItem, evidence: Turn?)

    var id: String {
        switch self {
        case .userParagraph(let s): return "p-\(s.prefix(60))"
        case .untimedNote(let id, _): return id
        case .bullet(let id, _, _): return id
        case .actionItem(let id, _, _): return id
        }
    }
}

struct DocumentSection: Identifiable {
    let id: String
    let title: String?        // nil = untitled overview flow
    var blocks: [DocumentBlock]
}

/// One built page: the sections in reading order, and the to-dos that always
/// sit at the end of it.
typealias Document = (sections: [DocumentSection], nextSteps: [DocumentBlock])

enum DocumentBuilder {

    static func build(detail: MeetingDetail, notes: [MeetingNote]) -> Document {
        let turns = detail.turns ?? []
        let summary = detail.summary

        // Every claim on the page is matched against every turn, so the turns
        // are tokenised once here rather than once per claim: the same
        // hundreds of Sets were being rebuilt for each key point, decision,
        // question and to-do (116ms on a document measured on this machine).
        let turnWords = turns.map { contentWords($0.text) }

        // Timed short notes become headings; long ones stay paragraphs.
        let timedNotes = notes.filter { $0.t != nil }.sorted { ($0.t ?? 0) < ($1.t ?? 0) }

        var sections: [DocumentSection] = []

        // Windows: each note owns [its t, next note's t).
        var windows: [(note: MeetingNote, lo: Double, hi: Double)] = []
        for (i, n) in timedNotes.enumerated() {
            let lo = n.t ?? 0
            let hi = i + 1 < timedNotes.count ? (timedNotes[i + 1].t ?? .infinity) : .infinity
            windows.append((n, lo, hi))
        }

        // Bind each AI key point to a turn, then to a note window.
        var overview: [DocumentBlock] = []
        var perWindow: [Int: [DocumentBlock]] = [:]
        for (i, point) in (summary?.key_points ?? []).enumerated() {
            let ev = evidence(for: point, in: turns, words: turnWords)
            let block = DocumentBlock.bullet(id: "kp-\(i)", text: point, evidence: ev)
            if let t = ev?.start, let wi = windows.firstIndex(where: { t >= $0.lo && t < $0.hi }) {
                perWindow[wi, default: []].append(block)
            } else {
                overview.append(block)
            }
        }

        if !overview.isEmpty {
            sections.append(DocumentSection(id: "overview", title: windows.isEmpty ? nil : "Overview",
                                            blocks: overview))
        }
        for (i, w) in windows.enumerated() {
            var blocks: [DocumentBlock] = []
            let text = w.note.text.trimmingCharacters(in: .whitespacesAndNewlines)
            let isHeading = text.count < 60
            if !isHeading { blocks.append(.userParagraph(text)) }
            blocks.append(contentsOf: perWindow[i] ?? [])
            guard !(blocks.isEmpty && !isHeading) else { continue }
            sections.append(DocumentSection(
                id: "note-\(i)",
                title: isHeading ? headingCase(text) : nil,
                blocks: blocks))
        }

        // Notes the engine has no timestamp for. They were dropped from this
        // document entirely — typed by the user, stored on disk, folded into
        // meeting.json, and then filtered out one line above because they had
        // no "t" to place them by. They keep the order they were written in.
        let untimedNotes = notes.filter { $0.t == nil }
            .map { $0.text.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
        if !untimedNotes.isEmpty {
            sections.append(DocumentSection(
                id: "untimed-notes", title: "Your notes",
                blocks: untimedNotes.enumerated().map { i, text in
                    .untimedNote(id: "un-\(i)", text: text)
                }))
        }

        // Decisions and open questions: their own sections, same grammar.
        if let decisions = summary?.decisions, !decisions.isEmpty {
            sections.append(DocumentSection(id: "decisions", title: "Decisions",
                blocks: decisions.enumerated().map { i, d in
                    .bullet(id: "dec-\(i)", text: d,
                            evidence: evidence(for: d, in: turns, words: turnWords))
                }))
        }
        if let questions = summary?.open_questions, !questions.isEmpty {
            sections.append(DocumentSection(id: "questions", title: "Open questions",
                blocks: questions.enumerated().map { i, q in
                    .bullet(id: "q-\(i)", text: q,
                            evidence: evidence(for: q, in: turns, words: turnWords))
                }))
        }

        // To-dos: committed action items first, then things left for later.
        var nextSteps: [DocumentBlock] = (summary?.action_items ?? []).enumerated().map { i, item in
            .actionItem(id: "ai-\(i)", item: item,
                        evidence: evidence(for: item.task, in: turns, words: turnWords))
        }
        for (i, f) in (summary?.follow_ups ?? []).enumerated() {
            nextSteps.append(.actionItem(id: "fu-\(i)",
                                         item: ActionItem(owner: nil, task: f, due: nil),
                                         evidence: evidence(for: f, in: turns, words: turnWords)))
        }

        return (sections, nextSteps)
    }

    /// A short user note becomes a heading: sentence-cased, no trailing period.
    private static func headingCase(_ s: String) -> String {
        var t = s.trimmingCharacters(in: .whitespacesAndNewlines)
        if t.hasSuffix(".") { t.removeLast() }
        guard let first = t.first else { return t }
        return String(first).uppercased() + t.dropFirst()
    }

    /// Nearest transcript turn by content-word overlap; nil when nothing
    /// vouches for the claim (the hover glyph simply doesn't appear).
    ///
    /// `words` is the turns already tokenised, positionally — see `build`.
    static func evidence(for claim: String, in turns: [Turn],
                         words: [Set<String>]) -> Turn? {
        guard !turns.isEmpty else { return nil }
        let claimWords = contentWords(claim)
        guard claimWords.count >= 2 else { return nil }
        var best: (turn: Turn, score: Double)?
        for (i, turn) in turns.enumerated() {
            let turnWords = words[i]
            guard !turnWords.isEmpty else { continue }
            let overlap = Double(claimWords.intersection(turnWords).count)
            let score = overlap / Double(claimWords.count)
            if score > (best?.score ?? 0) { best = (turn, score) }
        }
        guard let best, best.score >= 0.28 else { return nil }
        return best.turn
    }

    private static func contentWords(_ s: String) -> Set<String> {
        Set(s.lowercased()
            .components(separatedBy: CharacterSet.alphanumerics.inverted)
            .filter { $0.count > 3 })
    }
}
