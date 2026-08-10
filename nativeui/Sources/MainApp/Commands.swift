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
//   MENU → TRANSPORT.  Playback rides the same notification under its own
//   payload type, `MSPlaybackCommand`, adopted the same way:
//
//       .onMSPlaybackCommands { command in … }
//
//     A separate type because the two have different audiences. Every
//     MSCommand is answered once, centrally, by ContentView's switch; a
//     playback command means nothing at all unless a transport is on screen,
//     so the bar that owns the AVPlayers is the only sensible handler and
//     ContentView has no business knowing these exist. They also arrive far
//     more often — an arrow key on auto-repeat — and on plain keys, which is
//     a different conversation about focus than ⌘-anything is.
//
//   TRANSPORT → MENU.  MeetingScreen publishes an `MSPlaybackContext` by both
//     of the routes above, so the Playback items know whether there is
//     anything to play, what it is doing, and how fast.
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
import AppKit
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

/// The transport's own verbs, kept apart from `MSCommand` on purpose — see
/// the contract at the top of this file.
enum MSPlaybackCommand: Equatable {
    case togglePlayback
    /// Jump this many seconds along the meeting's timeline; negative is back.
    case seekBy(Double)
    /// Play at this multiple of real time.
    case setSpeed(Double)
    /// The next rung of the speed ladder, wrapping past the top back to 1×.
    case cycleSpeed
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

/// What the transport on screen can currently do.
///
/// Its own value rather than three more fields on MSContext: the transport is
/// owned by the meeting screen, which comes and goes underneath the window
/// that publishes MSContext, and "is anything playing" changes ten times a
/// minute where the rest of MSContext changes once a meeting.
struct MSPlaybackContext: Equatable {
    /// The meeting this transport belongs to; nil when none is on screen. It
    /// is what lets a departing screen tell whether the value on the bus is
    /// still its own — the next meeting's screen is built before this one
    /// goes away, and must not be wiped by its predecessor.
    var meetingID: String?
    /// There is audio to play. False for a meeting whose recording saved no
    /// track at all, which is the whole reason these items can be disabled.
    var available = false
    var playing = false
    var rate = PlaybackSpeed.normal
}

extension FocusedValues {
    /// What the key window can currently do. Set it with
    /// `.focusedSceneValue(\.msContext, …)`; read it with
    /// `@FocusedValue(\.msContext)`.
    @Entry var msContext: MSContext?

    /// What the transport in the key window can currently do.
    @Entry var msPlayback: MSPlaybackContext?
}

extension Notification.Name {
    /// A menu command fired. `userInfo["command"]` is the `MSCommand`.
    static let msCommand = Notification.Name("MeetingScribe.command")
    /// A meeting changed on disk. `object` is the meeting id (a `String`).
    static let msMeetingDidChange = Notification.Name("MeetingScribe.meetingDidChange")
}

@MainActor
final class CommandBus: ObservableObject {
    static let shared = CommandBus()

    @Published private(set) var context = MSContext()
    @Published private(set) var playback = MSPlaybackContext()

    private init() {}

    func publish(_ context: MSContext) {
        guard context != self.context else { return }
        self.context = context
    }

    func publish(playback: MSPlaybackContext) {
        guard playback != self.playback else { return }
        self.playback = playback
    }

    /// The transport for this meeting has left the screen. Only clears when
    /// the value on the bus is still that meeting's: switching meetings builds
    /// the next screen before the last one disappears, and the departing one
    /// must not take its successor's answer down with it.
    func retirePlayback(for meetingID: String) {
        guard playback.meetingID == meetingID else { return }
        playback = MSPlaybackContext()
    }

    func send(_ command: MSCommand) {
        // The payload rides in userInfo, not in `object`: `object` is the
        // SENDER, and NotificationCenter filters observers by it, so a command
        // parked there would be matched against senders nobody registered for.
        NotificationCenter.default.post(name: .msCommand,
                                        object: nil,
                                        userInfo: ["command": command])
    }

    /// The same channel, a different key: a view that adopts one payload type
    /// never has to know the other exists, and both stay one notification.
    func send(_ command: MSPlaybackCommand) {
        NotificationCenter.default.post(name: .msCommand,
                                        object: nil,
                                        userInfo: ["playback": command])
    }

    func meetingDidChange(_ id: String) {
        NotificationCenter.default.post(name: .msMeetingDidChange, object: id)
    }
}

extension View {
    /// Adopt one command.
    func onMSCommand(_ command: MSCommand, perform: @escaping () -> Void) -> some View {
        onReceive(NotificationCenter.default.publisher(for: .msCommand)) { note in
            guard let sent = note.userInfo?["command"] as? MSCommand, sent == command else { return }
            perform()
        }
    }

    /// Adopt all of them, and switch.
    func onMSCommands(perform: @escaping (MSCommand) -> Void) -> some View {
        onReceive(NotificationCenter.default.publisher(for: .msCommand)) { note in
            guard let sent = note.userInfo?["command"] as? MSCommand else { return }
            perform(sent)
        }
    }

    /// Adopt the transport's verbs. The view that owns the players is the one
    /// that should: nothing else can honour them.
    func onMSPlaybackCommands(perform: @escaping (MSPlaybackCommand) -> Void) -> some View {
        onReceive(NotificationCenter.default.publisher(for: .msCommand)) { note in
            guard let sent = note.userInfo?["playback"] as? MSPlaybackCommand else { return }
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
    @FocusedValue(\.msPlayback) private var focusedPlayback

    private var ctx: MSContext { focused ?? bus.context }
    private var hasMeeting: Bool { ctx.meetingID != nil }
    private var transport: MSPlaybackContext { focusedPlayback ?? bus.playback }

    var body: some Commands {
        // ── MeetingScribe ───────────────────────────────────────────────
        // Directly under "About MeetingScribe", where every Mac app that
        // updates itself puts this — and this one has to, because nothing
        // else does: the app arrives as a DMG off GitHub Releases, so a copy
        // that is a year old has never been told and has never been asked.
        // See UpdateChecker for the automatic half; this is the half a user
        // reaches for when they have heard a fix exists.
        CommandGroup(after: .appInfo) {
            Button("Check for Updates…") { checkForUpdates() }
        }

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
            // Playback first, because it is the thing this app is FOR: the
            // core loop is "jump back to the moment it was said", and until
            // now the transport was mouse-only — no play key, no seek keys,
            // no speed at all, on a screen where the same three gestures get
            // made a hundred times in a review.
            //
            // The keys are the ones every Mac media app has trained people to
            // expect (QuickTime, Music, Voice Memos, Podcasts): Space plays,
            // the arrows scrub, ⌥ makes the jump the long one. Plain key
            // equivalents, which is the whole convention: a field editor with
            // the keyboard consumes the key before the menu is ever offered
            // it, so Space still types a space in the search field and ← still
            // moves the caret. The bar guards the two plain verbs against a
            // focused composer as well — see FloatingBar.run — because that is
            // the one failure worth wearing a belt for.
            //
            // One item, two verbs, the way Start/Stop Recording does it.
            Button(transport.playing ? "Pause" : "Play") {
                bus.send(.togglePlayback)
            }
                .keyboardShortcut(.space, modifiers: [])
                .disabled(!transport.available)

            // Five and fifteen: five is "what was that word", fifteen is
            // "what was that sentence". Both directions, because a transport
            // that can only go back is half a transport.
            Button("Back 5 Seconds") { bus.send(.seekBy(-5)) }
                .keyboardShortcut(.leftArrow, modifiers: [])
                .disabled(!transport.available)
            Button("Forward 5 Seconds") { bus.send(.seekBy(5)) }
                .keyboardShortcut(.rightArrow, modifiers: [])
                .disabled(!transport.available)
            Button("Back 15 Seconds") { bus.send(.seekBy(-15)) }
                .keyboardShortcut(.leftArrow, modifiers: .option)
                .disabled(!transport.available)
            Button("Forward 15 Seconds") { bus.send(.seekBy(15)) }
                .keyboardShortcut(.rightArrow, modifiers: .option)
                .disabled(!transport.available)

            // The rate in force is ticked, so the menu answers "how fast am I
            // listening" as well as it sets it.
            Picker(selection: Binding(get: { transport.rate },
                                      set: { bus.send(.setSpeed($0)) })) {
                ForEach(PlaybackSpeed.ladder, id: \.self) { rate in
                    Text(PlaybackSpeed.label(rate)).tag(rate)
                }
            } label: {
                Text("Playback Speed")
            }
                .disabled(!transport.available)

            Button("Cycle Playback Speed") { bus.send(.cycleSpeed) }
                .keyboardShortcut(".", modifiers: [.command, .shift])
                .disabled(!transport.available)

            Divider()

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
            //
            // 0 is a real count here ("just you", what a fallback revert
            // leaves behind), so it cannot double as Auto: nil is Auto, and an
            // off-list count — 0, or a converted total past 8 — gets a row of
            // its own under -1 rather than leaving the menu with nothing
            // ticked and the setting in force invisible. -1 is unsendable by
            // construction; picking it means "leave things alone".
            Picker(selection: Binding(
                get: {
                    guard let n = ctx.speakerCount else { return 0 }
                    return SpeakerCount.offList(n) ? -1 : n
                },
                set: { if $0 >= 0 { bus.send(.setSpeakerCount($0 == 0 ? nil : $0)) } })) {
                    Text("Auto").tag(0)
                    if let n = ctx.speakerCount, SpeakerCount.offList(n) {
                        Text(n == 0 ? "0 (just you)" : "\(n)").tag(-1)
                    }
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

    /// The manual check, answered out loud — every time, including the boring
    /// answer and the failed one.
    ///
    /// A menu item that silently does nothing is indistinguishable from a
    /// broken one, and this item is pressed precisely by someone who wants to
    /// be TOLD. "You're up to date" is the most common outcome and the one it
    /// would be most tempting to swallow; swallowing it is what makes a person
    /// press the item twice and then go and look at the website anyway.
    ///
    /// NSAlert rather than a SwiftUI `.alert`: a Commands body is not a view,
    /// it owns no window to attach a sheet to and has nowhere to keep the
    /// @State a presentation binding needs. MainAppDelegate already makes the
    /// same choice for the quit refusal, for the same reason. The command
    /// itself is not an MSCommand either — it acts on the app, not on whatever
    /// meeting is selected, which is what the contract at the top of this file
    /// says an MSCommand is for.
    @MainActor
    private func checkForUpdates() {
        Task {
            let state = await UpdateChecker.shared.checkNow()
            let alert = NSAlert()
            // A menu item can be picked with the app behind something else,
            // and a modal on an inactive app is a beachball nobody can find.
            NSApp.activate()
            switch state {
            case .available(let version, let notes, _):
                alert.messageText = "MeetingScribe \(version) is available."
                let excerpt = UpdateChecker.notesExcerpt(notes)
                alert.informativeText = excerpt.isEmpty
                    ? "You have \(UpdateChecker.currentVersion). "
                        + "Downloading opens the disk image in your browser."
                    : excerpt
                alert.addButton(withTitle: "Download Update")
                alert.addButton(withTitle: "Later")
                if alert.runModal() == .alertFirstButtonReturn {
                    UpdateChecker.openDownload()
                }
                return
            case .upToDate(let current):
                alert.messageText = "You're up to date"
                alert.informativeText = "MeetingScribe \(current) is the newest version."
            case .failed(let message):
                alert.messageText = "Couldn't check for updates"
                alert.informativeText = message
            case .checking:
                // Unreachable: checkNow only returns once an answer has
                // settled. Saying nothing is still better than a blank alert.
                return
            }
            alert.addButton(withTitle: "OK")
            alert.runModal()
        }
    }
}

// MARK: - How fast the meeting plays

/// The speed ladder, in one place, because three surfaces climb it: the menu's
/// Playback Speed picker, the bar's one-click stepper, and the transport that
/// has to hand the number to AVFoundation.
///
/// It stops at 2×. Past that a meeting recording stops being listenable —
/// speech, not music, and the point of the feature is to hear the sentence
/// again, faster, not to skim past it.
enum PlaybackSpeed {
    static let normal: Double = 1
    static let ladder: [Double] = [1, 1.25, 1.5, 1.75, 2]

    /// "1×", "1.25×", "2×" — no trailing zeros, one form everywhere it shows.
    static func label(_ rate: Double) -> String {
        String(format: "%g", rate) + "×"
    }

    /// The same number for VoiceOver, which reads "×" as "multiplication sign".
    static func spoken(_ rate: Double) -> String {
        String(format: "%g", rate) + " times normal speed"
    }

    /// The next rung up, wrapping back to the bottom past the top. A rate that
    /// is off the ladder entirely climbs onto it rather than being stranded.
    static func next(after rate: Double) -> Double {
        ladder.first(where: { $0 > rate + 0.001 }) ?? normal
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
