// The menu bar, and the contract behind it.
//
// Until now the whole app had three keyboard shortcuts and every action lived
// under a hover state or a footer link: nothing could be reached without a
// mouse, nothing could be discovered by opening a menu, and VoiceOver had no
// route to any of it. This file is the Mac answer to that. It is deliberately
// NOT the ⌘K palette from the old web brief, which DESIGN.md defers ("native
// menu bar + .searchable cover it").
//
// ─────────────────────────────────────────────────────────────────────────
// THE CONTRACT, for every other file in this target
// ─────────────────────────────────────────────────────────────────────────
//
// Menus live in a Scene and views live in a window, so the two cannot call
// each other directly. They meet here, in one direction each way:
//
//   MENU → VIEW.  A menu item posts an `MSCommand`. Any view adopts one with
//
//       .onMSCommand(.tidyTranscript) { … }         // exactly this command
//       .onMSCommands { command in … }              // all of them, one switch
//
//     Adoption is optional and additive: ContentView already handles every
//     command below, so a view that adopts one is adding a second, better
//     placed handler, not switching the feature on. If two handlers would
//     both fire, the second one to be written owns the deduplication.
//
//   VIEW → MENU.  A view publishes what the menu is allowed to offer:
//
//       .focusedSceneValue(\.msContext, context)    // an MSContext value
//       CommandBus.shared.publish(context)          // the same value
//
//     Both, on purpose. The focused scene value is the idiomatic macOS route
//     and the one that goes quiet when this window is not the key window; the
//     bus is the fallback the menu reads when no scene value has arrived, so
//     the menu can never be stuck fully disabled because focus went somewhere
//     unexpected. ContentView publishes on every change. Menu items enable,
//     disable and tick themselves off that one value, so a menu never offers
//     an action the current meeting cannot take.
//
// And one broadcast that is not a command: when anything mutates a meeting on
// disk (a re-cluster, a tidy, an undo), tell everyone so the open document can
// reload:
//
//       CommandBus.shared.meetingDidChange(id)
//       .onMSMeetingChange { id in … }
//
import SwiftUI
import Combine

// MARK: - Commands

/// Every action the menu bar can ask for. Adding a case here is the whole
/// cost of adding a menu item that other views can adopt.
enum MSCommand: Equatable {
    case toggleRecording
    case showToday
    case showSettings
    case showNotes
    case showTranscript
    case nextMeeting
    case previousMeeting
    case focusSearch
    case copyTranscript
    case revealInFinder
    case deleteMeeting
    case reanalyse
    case reprocessAudio
    /// Open the speaker-count correction sheet for the current meeting.
    case speakerCount
    /// Re-cluster the current meeting into this many voices; nil hands the
    /// decision back to the engine.
    case setSpeakerCount(Int?)
    case tidyTranscript
    case undoTidy
}

/// What the current window has to offer, so the menu can be honest about it.
struct MSContext: Equatable {
    var meetingID: String?
    var recording = false
    var engineUp = false
    var mode: PageMode = .document
    var hasTranscript = false
    var hasSummary = false
    /// A pre-tidy backup exists, so Undo Tidy can still succeed.
    var canUndoTidy = false
    /// The speaker count in force, in the basis the user is shown; nil is
    /// "Auto". See SpeakerCount.displayed.
    var speakerCount: Int?
    /// What that number counts, in words, because it is not the same question
    /// on every meeting.
    var speakerCountLabel = SpeakerCount.othersLabel
    var canReorderSelection = false
}

struct MSContextKey: FocusedValueKey {
    typealias Value = MSContext
}

extension FocusedValues {
    /// What the key window can currently do. Set it with
    /// `.focusedSceneValue(\.msContext, …)`; read it with
    /// `@FocusedValue(\.msContext)`.
    var msContext: MSContext? {
        get { self[MSContextKey.self] }
        set { self[MSContextKey.self] = newValue }
    }
}

extension Notification.Name {
    /// A menu command fired. `object` is the `MSCommand`.
    static let msCommand = Notification.Name("MeetingScribe.command")
    /// A meeting changed on disk. `object` is the meeting id (a `String`).
    static let msMeetingDidChange = Notification.Name("MeetingScribe.meetingDidChange")
}

@MainActor
final class CommandBus: ObservableObject {
    static let shared = CommandBus()

    @Published private(set) var context = MSContext()

    private init() {}

    func publish(_ context: MSContext) {
        guard context != self.context else { return }
        self.context = context
    }

    func send(_ command: MSCommand) {
        NotificationCenter.default.post(name: .msCommand, object: command)
    }

    func meetingDidChange(_ id: String) {
        NotificationCenter.default.post(name: .msMeetingDidChange, object: id)
    }
}

extension View {
    /// Adopt one command.
    func onMSCommand(_ command: MSCommand, perform: @escaping () -> Void) -> some View {
        onReceive(NotificationCenter.default.publisher(for: .msCommand)) { note in
            guard let sent = note.object as? MSCommand, sent == command else { return }
            perform()
        }
    }

    /// Adopt all of them, and switch.
    func onMSCommands(perform: @escaping (MSCommand) -> Void) -> some View {
        onReceive(NotificationCenter.default.publisher(for: .msCommand)) { note in
            guard let sent = note.object as? MSCommand else { return }
            perform(sent)
        }
    }

    /// React to a meeting having been rewritten on disk by something else.
    func onMSMeetingChange(perform: @escaping (String) -> Void) -> some View {
        onReceive(NotificationCenter.default.publisher(for: .msMeetingDidChange)) { note in
            guard let id = note.object as? String else { return }
            perform(id)
        }
    }
}

// MARK: - The menu bar

struct MeetingScribeCommands: Commands {
    @ObservedObject private var bus = CommandBus.shared
    @FocusedValue(\.msContext) private var focused

    private var ctx: MSContext { focused ?? bus.context }
    private var hasMeeting: Bool { ctx.meetingID != nil }

    var body: some Commands {
        // ── MeetingScribe ───────────────────────────────────────────────
        // Settings is a route in the main window, not a scene, so the app
        // menu's own item has to be replaced: without a `SwiftUI.Settings`
        // scene macOS leaves a "Settings…" that opens nothing.
        CommandGroup(replacing: .appSettings) {
            Button("Settings…") { bus.send(.showSettings) }
                .keyboardShortcut(",", modifiers: .command)
        }

        // ── File ────────────────────────────────────────────────────────
        // "New" on a recorder is a recording. One item, not two: a Mac menu
        // flips its own verb (Play/Pause) rather than greying out a twin.
        CommandGroup(replacing: .newItem) {
            Button(ctx.recording ? "Stop Recording" : "Start Recording") {
                bus.send(.toggleRecording)
            }
            .keyboardShortcut("r", modifiers: .command)
            .disabled(!ctx.engineUp && !ctx.recording)
        }

        CommandGroup(after: .newItem) {
            Divider()
            Button("Reveal Recording in Finder") { bus.send(.revealInFinder) }
                .keyboardShortcut("r", modifiers: [.command, .shift])
                .disabled(!hasMeeting)
            Button("Delete Meeting…") { bus.send(.deleteMeeting) }
                .keyboardShortcut(.delete, modifiers: .command)
                .disabled(!hasMeeting)
        }

        // ── Edit ────────────────────────────────────────────────────────
        CommandGroup(after: .pasteboard) {
            Divider()
            Button("Copy Transcript") { bus.send(.copyTranscript) }
                .keyboardShortcut("c", modifiers: [.command, .shift])
                .disabled(!ctx.hasTranscript)
        }

        CommandGroup(after: .textEditing) {
            Divider()
            // Not plain Cmd-F. A menu key equivalent is dispatched before any
            // view-level .keyboardShortcut, so binding it here would swallow
            // Cmd-F inside the transcript, where a Mac user means "find in
            // this document" and where the find bar is the whole point.
            Button("Find Meeting…") { bus.send(.focusSearch) }
                .keyboardShortcut("f", modifiers: [.command, .shift])
        }

        // ── View ────────────────────────────────────────────────────────
        CommandGroup(after: .sidebar) {
            Divider()
            Button("Today") { bus.send(.showToday) }
                .keyboardShortcut("0", modifiers: .command)
            Button("Notes") { bus.send(.showNotes) }
                .keyboardShortcut("1", modifiers: .command)
                .disabled(!hasMeeting)
            Button("Transcript") { bus.send(.showTranscript) }
                .keyboardShortcut("2", modifiers: .command)
                .disabled(!hasMeeting)
            Divider()
            Button("Next Meeting") { bus.send(.nextMeeting) }
                .keyboardShortcut(.downArrow, modifiers: [.command, .option])
                .disabled(!ctx.canReorderSelection)
            Button("Previous Meeting") { bus.send(.previousMeeting) }
                .keyboardShortcut(.upArrow, modifiers: [.command, .option])
                .disabled(!ctx.canReorderSelection)
        }

        // ── Meeting ─────────────────────────────────────────────────────
        CommandMenu("Meeting") {
            // One item, two verbs. It was always "Re-analyse Summary", which
            // reads as "redo the one you have" — so on a meeting with no
            // summary the single menu route to writing one described itself as
            // something else, and looked like it would do nothing.
            Button(ctx.hasSummary ? "Re-analyse Summary" : "Write the Summary") {
                bus.send(.reanalyse)
            }
                .keyboardShortcut("a", modifiers: [.command, .shift])
                .disabled(!ctx.hasTranscript)
            // No ellipsis: this starts immediately, matching the sidebar's
            // context menu. Ellipses in this menu mean a dialog follows.
            Button("Reprocess Audio") { bus.send(.reprocessAudio) }
                .keyboardShortcut("r", modifiers: [.command, .option])
                .disabled(!hasMeeting)

            Divider()

            // The highest-value correction in the app, and until now the only
            // one with no control at all: the engine's guess at how many
            // people are in the room is the thing a person can always beat.
            // Picking a number re-clusters the saved voice analysis; no
            // re-transcription, usually under a second.
            Picker(selection: Binding(
                get: { ctx.speakerCount ?? 0 },
                set: { bus.send(.setSpeakerCount($0 == 0 ? nil : $0)) })) {
                    Text("Auto").tag(0)
                    ForEach(1...8, id: \.self) { n in
                        Text("\(n)").tag(n)
                    }
                } label: {
                    Text("Speaker Count")
                }
                .disabled(!hasMeeting)

            Button("Correct Speaker Count…") { bus.send(.speakerCount) }
                .keyboardShortcut("k", modifiers: [.command, .shift])
                .disabled(!hasMeeting)

            Divider()

            Button("Tidy Transcript…") { bus.send(.tidyTranscript) }
                .keyboardShortcut("y", modifiers: [.command, .shift])
                .disabled(!ctx.hasTranscript)
            Button("Undo Tidy") { bus.send(.undoTidy) }
                .disabled(!ctx.canUndoTidy)
        }
    }
}

// MARK: - Speaker counting, which is two different questions

/// One control, two questions, and the conversion between them.
///
/// The engine records what a stored count MEANT as `speaker_count_basis`:
/// "others" (the number excludes you, the ordinary online case) or "total"
/// (every voice on the one microphone, you included, which is what the
/// mic-only fallback leaves behind). The question the user should be ASKED
/// depends on the meeting as it stands now, not on how the number was stored,
/// so a count stored under one basis has to be converted before it is shown.
/// This is a faithful port of the old web UI's speakerCountValue().
enum SpeakerCount {
    static let othersLabel = "Speakers besides you"
    static let roomLabel = "People in the room, you included"
    static let micLabel = "Voices on the microphone, you are one of them"

    /// What this meeting's control is asking.
    static func label(_ m: API.Corrections?) -> String {
        guard let m else { return othersLabel }
        if m.diarization_mode == "mic_fallback" { return micLabel }
        return m.mode == "inperson" ? roomLabel : othersLabel
    }

    /// The number to show, in the basis `label` describes. nil is "Auto".
    static func displayed(_ m: API.Corrections?) -> Int? {
        guard let m, let count = m.expected_speakers, count > 0 else { return nil }
        // In-person has only ever asked one question, and app.py's calendar
        // guess for it is already a room total, so its number is already in
        // the displayed basis. Converting it would be acting on a value the
        // engine never reads back.
        if m.mode == "inperson" { return count }
        // Under the fallback a count the engine guessed is not preselected:
        // pipeline.py deliberately ignores a calendar-derived count there, so
        // showing it would claim a setting that is not in force.
        if m.diarization_mode == "mic_fallback", m.speaker_count_source != "user" {
            return nil
        }
        let storedTotal = (m.speaker_count_basis ?? "others") == "total"
        let totalVoices = storedTotal ? count : count + 1
        return m.diarization_mode == "mic_fallback" ? totalVoices : totalVoices - 1
    }

    /// A value the control cannot send back, yet which is the setting in
    /// force: 0 ("just you", what a fallback revert leaves behind) or a
    /// converted total past the end of the 1...8 list.
    static func offList(_ value: Int?) -> Bool {
        guard let value else { return false }
        return value < 1 || value > 8
    }
}

// MARK: - The endpoints the native app never called

extension API {
    /// Everything the correction surfaces need that `MeetingDetail` does not
    /// decode. A second read of the same document rather than an edit to
    /// API.swift, and it is only fetched when a meeting is selected.
    struct Corrections: Decodable, Equatable {
        struct TidyReport: Decodable, Equatable {
            let dropped_turns: Int?
            let trimmed_turns: Int?
            let merged_speakers: [String: String]?
            let renamed_speakers: Int?
        }

        let id: String
        let title: String?
        let status: String?
        /// "online" | "inperson".
        let mode: String?
        /// "mic_fallback" when the call audio was silent and the microphone
        /// carried everyone, the user included. Absent otherwise.
        let diarization_mode: String?
        let expected_speakers: Int?
        /// "user" when a person chose the count rather than the engine or the
        /// calendar guessing it.
        let speaker_count_source: String?
        /// "others" | "total" — what the stored count counted.
        let speaker_count_basis: String?
        /// What the last tidy did. Its presence is also the only signal that
        /// a pre-tidy backup exists, which is what Undo Tidy needs.
        let tidied: TidyReport?
        let turns: [Turn]?

        var hasTranscript: Bool { !(turns ?? []).isEmpty }
    }

    static func corrections(_ id: String) async -> Corrections? {
        try? await get("api/meetings/\(id)", as: Corrections.self)
    }

    /// Re-cluster the saved voice analysis into `speakers` voices, or nil to
    /// hand the decision back to the engine. Synchronous on the engine side:
    /// usually well under a second, but a meeting whose track was never
    /// embedded runs a full pass inside the request, so callers show progress.
    static func recluster(_ id: String, speakers: Int?) async -> (Bool, String?) {
        var body: [String: Any] = [:]
        if let speakers { body["speakers"] = speakers }
        return await postExpectingMessage("api/meetings/\(id)/recluster",
                                          body: body, timeout: 600)
    }

    /// Clean the transcript with the on-device model. Returns immediately;
    /// the work runs as an ordinary processing job, so the meeting's status
    /// becomes "processing" and the usual progress watcher reports it.
    static func tidy(_ id: String) async -> (Bool, String?) {
        await postExpectingMessage("api/meetings/\(id)/tidy")
    }

    /// Put the pre-tidy transcript back. Notes taken since are kept.
    static func undoTidy(_ id: String) async -> (Bool, String?) {
        await postExpectingMessage("api/meetings/\(id)/tidy/undo")
    }

    /// The engine's live line while it re-clusters, if it is saying one.
    static func reclusterProgress(_ id: String) async -> String? {
        struct Status: Decodable { let recluster_jobs: [String: EngineJob]? }
        let status = try? await get("api/status", as: Status.self)
        return status?.recluster_jobs?[id]?.message
    }

    /// POST, and hand back the engine's own refusal when it refuses. Every
    /// one of these endpoints explains itself in `error` — "Meeting is being
    /// summarized", "No transcript to tidy yet", "No pre-tidy backup for this
    /// meeting" — and a UI that swallows that is a UI that makes the user
    /// guess.
    private static func postExpectingMessage(_ path: String,
                                             body: [String: Any] = [:],
                                             timeout: TimeInterval = 30)
        async -> (Bool, String?) {
        var req = URLRequest(url: engineBase.appendingPathComponent(path))
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = (try? JSONSerialization.data(withJSONObject: body)) ?? Data("{}".utf8)
        req.timeoutInterval = timeout
        guard let (data, resp) = try? await URLSession.shared.data(for: req),
              let code = (resp as? HTTPURLResponse)?.statusCode else {
            return (false, "The engine didn't answer.")
        }
        if (200..<300).contains(code) { return (true, nil) }
        let err = ((try? JSONSerialization.jsonObject(with: data)) as? [String: Any])?["error"] as? String
        return (false, err ?? "That didn't work (\(code)).")
    }
}
