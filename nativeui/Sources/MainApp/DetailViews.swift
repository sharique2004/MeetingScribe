// The meeting screen: loads one meeting's detail, notes and waveform, then
// renders the document or its transcript mode with the single floating
// glass bar (Ask ↔ transport) over the bottom.
import SwiftUI
import AVFoundation
import AppKit
import UniformTypeIdentifiers

@MainActor
final class MeetingModel: ObservableObject {
    @Published var detail: MeetingDetail?
    @Published var notes: [MeetingNote] = []
    @Published var waveform: WaveformData?
    @Published var error: String?
    @Published var summarizing = false
    @Published var summaryProgress: String?
    @Published var summaryError: String?
    /// The engine's line while it transcribes this meeting: "Loading model…",
    /// "Downloading the speech model, 340 MB of 2.5 GB", "Transcribing 4/9".
    @Published var processingProgress: String?
    @Published var reprocessing = false
    @Published var reprocessError: String?
    /// The engine refused a rename, in its words. The field puts the old
    /// title back at the same moment, so the two never disagree.
    @Published var titleError: String?
    /// The sidebar's list, so a rename lands on the row too. Weak: the model
    /// belongs to one screen, the library to the window.
    weak var library: Library?
    private var meetingID: String?
    private var processingWatch: Task<Void, Never>?

    func load(_ id: String) async {
        meetingID = id
        do {
            detail = try await API.meeting(id)
            error = nil
        } catch {
            self.error = "Couldn't load this meeting."
            return
        }
        if let bundled = detail?.notes, !bundled.isEmpty {
            notes = bundled
        } else {
            notes = (try? await API.notes(id)) ?? []
        }
        waveform = try? await API.waveform(id)
        // Opened while the engine is still transcribing? Watch the work, so
        // the transcript appears the moment it lands instead of the next
        // time the user happens to navigate here.
        if meetingIsWorking(detail?.status) {
            watchProcessing(id)
        }
        // A summary may already be writing (auto-run after transcription, or
        // kicked off elsewhere) — pick the job up so progress is visible.
        if let job = await API.summaryJob(id), job.state == "processing" {
            summarizing = true
            summaryProgress = job.message
            watchSummaryJob(id)
        }
    }

    /// Poll the document while the engine works on it. Nothing else refreshes
    /// an OPEN meeting page: the sidebar's watcher updates the LIST rows, and
    /// this model used to fetch exactly once — so a meeting opened mid-
    /// transcription showed "Processing" until the user clicked away and
    /// back. Ends on its own when the work does, or when the page moves to
    /// another meeting.
    ///
    /// The same tick reads the engine's job message, so the page reports the
    /// work in the engine's words rather than the static one it invented.
    private func watchProcessing(_ id: String) {
        guard processingWatch == nil else { return }
        processingWatch = Task { [weak self] in
            defer { self?.processingWatch = nil }
            guard let self else { return }
            processingProgress = await API.processingJobs()[id]?.message
            while meetingID == id {
                try? await Task.sleep(nanoseconds: 2_000_000_000)
                guard meetingID == id else { return }
                processingProgress = await API.processingJobs()[id]?.message
                guard let fresh = try? await API.meeting(id) else { continue }
                guard meetingID == id else { return }
                detail = fresh
                if meetingIsWorking(fresh.status) { continue }
                processingProgress = nil
                // The work landed: bring the sidecars the first load found
                // empty, and pick up the auto-run summary job if one started.
                if let bundled = fresh.notes, !bundled.isEmpty {
                    notes = bundled
                }
                waveform = (try? await API.waveform(id)) ?? waveform
                if !summarizing, let job = await API.summaryJob(id),
                   job.state == "processing" {
                    summarizing = true
                    summaryProgress = job.message
                    watchSummaryJob(id)
                }
                return
            }
        }
    }

    /// Re-fetch the document after an edit the engine made for us — a
    /// speaker rename rewrites the speaker map that the page, the ribbon and
    /// the transcript all read from.
    func reload() async {
        guard let id = meetingID, let fresh = try? await API.meeting(id) else { return }
        detail = fresh
    }

    /// Rename this meeting.
    ///
    /// Two failures used to hide here. The edit was only committed on Return,
    /// so clicking away — the ordinary way anyone leaves a title field —
    /// silently threw the new name away; and a rename that DID reach the
    /// engine never reached the sidebar, so the row kept the old title until
    /// something else refetched the list. Both are fixed at the same point:
    /// this is the only path a title changes on.
    ///
    /// Committing on blur is right HERE and deliberately not in the speaker
    /// ribbon: naming a speaker enrols a voiceprint, which is a side effect
    /// worth an explicit Return. Naming a meeting writes a string.
    ///
    /// -> true when the engine took it.
    @discardableResult
    func rename(to raw: String) async -> Bool {
        let title = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let id = meetingID, !title.isEmpty, title != detail?.title else { return false }
        guard await API.rename(id, title: title) else {
            titleError = "The engine wouldn't take that title. The old one is still saved."
            return false
        }
        // The list row, now, from the answer we already have — not on the next
        // refetch, and without a second request.
        if let i = library?.meetings.firstIndex(where: { $0.id == id }) {
            library?.meetings[i].title = title
        }
        await reload()
        return true
    }

    /// Hand the saved audio back to the engine.
    ///
    /// This is the whole recovery path for a meeting that failed: the WAVs
    /// are untouched by a failed run, so a reprocess is a second attempt with
    /// today's settings, not a salvage job. The page picks the work up the
    /// same way it does for a fresh recording.
    func reprocess() {
        guard let id = meetingID, !reprocessing else { return }
        reprocessing = true
        reprocessError = nil
        Task {
            let (ok, err) = await API.reprocess(id)
            reprocessing = false
            guard ok else {
                reprocessError = err
                return
            }
            processingProgress = "Loading model…"
            if let fresh = try? await API.meeting(id) { detail = fresh }
            watchProcessing(id)
        }
    }

    func summarize() {
        guard let id = meetingID, !summarizing else { return }
        summaryError = nil
        Task {
            let (ok, err) = await API.summarize(id)
            if ok {
                summarizing = true
                summaryProgress = "Summarising…"
                watchSummaryJob(id)
            } else {
                summaryError = err
            }
        }
    }

    private func watchSummaryJob(_ id: String) {
        Task {
            while summarizing, meetingID == id {
                try? await Task.sleep(nanoseconds: 2_000_000_000)
                guard let job = await API.summaryJob(id) else { continue }
                summaryProgress = job.message
                if job.state == "done" {
                    detail = try? await API.meeting(id)
                    summarizing = false
                    summaryProgress = nil
                } else if job.state == "error" {
                    summarizing = false
                    summaryProgress = nil
                    summaryError = job.message ?? "The summary failed."
                }
            }
        }
    }
}

struct MeetingScreen: View {
    let meetingID: String
    @Binding var mode: PageMode
    @StateObject private var model = MeetingModel()
    @StateObject private var player = Playback()
    @EnvironmentObject private var library: Library
    @State private var exportError: String?

    var body: some View {
        Group {
            if let detail = model.detail {
                ZStack {
                    if mode == .document {
                        MeetingPage(detail: detail, notes: model.notes, player: player,
                                    model: model, mode: $mode) { seekTo in
                            withAnimation(Motion.enter) { mode = .transcript }
                            if let t = seekTo {
                                player.prepare(detail)
                                player.seek(t)
                                player.play()
                            }
                        }
                        .transition(.opacity)
                    } else {
                        TranscriptPage(detail: detail, player: player, mode: $mode)
                            .transition(.opacity)
                    }
                }
                .overlay(alignment: .bottom) {
                    FloatingBar(detail: detail, waveform: model.waveform, player: player)
                }
                // Export where a Mac user looks for it: the window's toolbar,
                // alongside the transcript's own copy button on the page.
                .toolbar {
                    ToolbarItem(placement: .primaryAction) {
                        ExportMenu(detail: detail, error: $exportError)
                    }
                }
            } else if let err = model.error {
                ContentUnavailableView(err, systemImage: "bolt.slash")
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                    .background(MS.content)
            } else {
                ProgressView().controlSize(.large)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                    .background(MS.content)
            }
        }
        .navigationTitle("")
        .task {
            model.library = library
            await model.load(meetingID)
            if let detail = model.detail { player.prepare(detail) }
        }
        // The tracks arrive with the document — and again when a reprocess
        // rewrites it — so the transport is built from what this meeting
        // actually has rather than from a guess made before it loaded.
        .onChange(of: model.detail?.tracks) {
            if let detail = model.detail { player.prepare(detail) }
        }
        .alert("Couldn't export",
               isPresented: Binding(get: { exportError != nil },
                                    set: { if !$0 { exportError = nil } })) {
            Button("OK") { exportError = nil }
        } message: {
            Text(exportError ?? "")
        }
        .onDisappear { player.pause() }
    }
}

// MARK: - Export

/// Copy, Markdown, plain text — the three things the web UI offered and the
/// native app dropped, though every meeting has had a transcript.md on disk
/// the whole time. The two file formats come from the engine's own /export,
/// so they carry the summary and the speaking stats, and land wherever the
/// user says through a real save panel.
struct ExportMenu: View {
    let detail: MeetingDetail
    @Binding var error: String?
    /// ⌘S belongs to exactly one of these per window — the toolbar's. The
    /// same menu also sits in the document footer, where a second identical
    /// shortcut would just be ambiguous.
    var shortcut = true
    @State private var copied = false
    @State private var saving = false

    private var empty: Bool { (detail.turns ?? []).isEmpty }

    var body: some View {
        Menu {
            Button {
                copyTranscript()
            } label: {
                Label(copied ? "Copied" : "Copy transcript", systemImage: "doc.on.doc")
            }
            .disabled(empty)
            Divider()
            Button {
                save(.markdown)
            } label: {
                Label("Save as Markdown…", systemImage: "doc.richtext")
            }
            .modifier(SaveShortcut(enabled: shortcut))
            Button {
                save(.plainText)
            } label: {
                Label("Save as plain text…", systemImage: "doc.plaintext")
            }
            Divider()
            Button {
                Task { await API.post("api/meetings/\(detail.id)/reveal") }
            } label: {
                Label("Reveal transcript in Finder", systemImage: "folder")
            }
        } label: {
            Label("Export", systemImage: "square.and.arrow.up")
        }
        .menuIndicator(.hidden)
        .disabled(saving || empty)
        .help(empty ? "There's nothing to export until this meeting has a transcript"
                    : "Copy this meeting, or save it as Markdown or plain text")
    }

    private func copyTranscript() {
        let pb = NSPasteboard.general
        pb.clearContents()
        pb.setString(transcriptText(detail), forType: .string)
        copied = true
        Task {
            try? await Task.sleep(nanoseconds: 1_600_000_000)
            copied = false
        }
    }

    private func save(_ kind: MeetingExportFormat) {
        guard !saving else { return }
        saving = true
        Task {
            error = await MeetingExporter.save(detail, as: kind)
            saving = false
        }
    }
}

/// ⌘S, but only on the copy of the menu that owns it.
private struct SaveShortcut: ViewModifier {
    let enabled: Bool

    func body(content: Content) -> some View {
        if enabled {
            content.keyboardShortcut("s", modifiers: .command)
        } else {
            content
        }
    }
}

enum MeetingExportFormat {
    case markdown, plainText

    /// The engine's own name for the format.
    var fmt: String { self == .markdown ? "md" : "txt" }
    var contentType: UTType {
        UTType(filenameExtension: fmt) ?? .plainText
    }
}

@MainActor
enum MeetingExporter {
    /// -> nil when the file was written or the user cancelled; a sentence to
    /// show when it genuinely failed.
    ///
    /// The bytes are fetched BEFORE the panel opens, so an engine that isn't
    /// answering is reported instead of taken as far as choosing a folder and
    /// then failing on the last click.
    static func save(_ detail: MeetingDetail, as kind: MeetingExportFormat) async -> String? {
        guard let data = await API.export(detail.id, fmt: kind.fmt) else {
            return "The engine didn't hand over the export, so nothing was saved."
        }
        let panel = NSSavePanel()
        panel.nameFieldStringValue = filename(detail.title) + "." + kind.fmt
        panel.allowedContentTypes = [kind.contentType]
        panel.canCreateDirectories = true
        panel.isExtensionHidden = false
        panel.title = "Save meeting"
        panel.message = "The whole meeting: summary, speaking stats and transcript."
        guard panel.runModal() == .OK, let url = panel.url else { return nil }
        do {
            try data.write(to: url, options: .atomic)
        } catch {
            return "Couldn't write that file: \(error.localizedDescription)"
        }
        return nil
    }

    /// A file name from a meeting title: the same characters the engine keeps
    /// in its own Content-Disposition, so the two agree.
    private static func filename(_ title: String) -> String {
        let kept = title.unicodeScalars.filter {
            CharacterSet.alphanumerics.contains($0) || $0 == " " || $0 == "-" || $0 == "_"
        }
        let name = String(String.UnicodeScalarView(kept))
            .trimmingCharacters(in: .whitespaces)
        return name.isEmpty ? "Meeting" : name
    }
}

// MARK: - Playback

/// One of the meeting's tracks, playing on the meeting's timeline.
///
/// `offset` is the track's start_offset: the seconds it began after the
/// earliest one. The transcript, the waveform and the notes are all written
/// against that shared timeline, so this is the number that keeps the two
/// decks — and the words on screen — in step.
@MainActor
private final class Deck {
    let key: String
    let player: AVPlayer
    let offset: Double
    /// The timeline hasn't reached this track's first sample yet, so it is
    /// held at zero rather than played early and out of step.
    var waiting = false
    var ended = false

    init(key: String, url: URL, offset: Double) {
        self.key = key
        self.offset = offset
        let item = AVPlayerItem(url: url)
        player = AVPlayer(playerItem: item)
        player.automaticallyWaitsToMinimizeStalling = false
    }
}

struct TrackChoice: Identifiable, Hashable {
    let id: String
    let label: String
}

/// The transport.
///
/// It plays every track the meeting HAS, together, on one timeline, and the
/// picker only mutes — which is how the old web UI worked and why "Mix" was
/// possible there. Four things were wrong here before: it played the user's
/// own microphone alone by default (so the other participants were missing);
/// Mix didn't exist; the picker offered "Meeting" to meetings that never
/// recorded a system track, which is a 404; and it ignored start_offset, so
/// the two tracks drifted against each other and against the transcript.
@MainActor
final class Playback: ObservableObject {
    @Published private(set) var playing = false
    @Published var time: Double = 0
    /// "mix" | "mic" | "system" — what you HEAR. Every deck plays either way.
    @Published private(set) var track = "mix"
    /// What this meeting can actually offer. Empty when there is nothing to
    /// choose between, so no picker is drawn.
    @Published private(set) var choices: [TrackChoice] = []
    /// Why playback can't happen, or why it stopped — one sentence, on the
    /// bar. Silence used to be the whole story for a missing or broken file.
    @Published private(set) var failure: String?

    private var decks: [Deck] = []
    private var fingerprint: String?
    private var duration: Double = 0
    /// The earliest moment any audio exists. Zero in every ordinary meeting.
    private var origin: Double = 0
    private var timeObservers: [(AVPlayer, Any)] = []
    private var statusWatch: [NSKeyValueObservation] = []
    private var endWatch: [NSObjectProtocol] = []

    var hasAudio: Bool { !decks.isEmpty }

    private static let label = ["mic": "Your mic", "system": "Meeting"]

    /// Build the transport for one meeting from the tracks it really has.
    /// Idempotent: called again with the same tracks, it does nothing, so it
    /// is safe on every document refresh.
    func prepare(_ detail: MeetingDetail) {
        let tracks = detail.playableTracks
        let mark = ([detail.id] + tracks.map { "\($0.key)@\($0.info.start_offset ?? 0)" })
            .joined(separator: "|")
        guard mark != fingerprint else { return }
        teardown()
        fingerprint = mark
        duration = detail.duration ?? 0
        guard !tracks.isEmpty else {
            failure = "No audio was saved for this meeting."
            return
        }

        origin = tracks.map { $0.info.start_offset ?? 0 }.min() ?? 0
        decks = tracks.map { entry in
            Deck(key: entry.key,
                 url: API.audioURL(detail.id, track: entry.key),
                 offset: entry.info.start_offset ?? 0)
        }
        choices = decks.count > 1
            ? [TrackChoice(id: "mix", label: "Mix")]
                + decks.map { TrackChoice(id: $0.key, label: Self.label[$0.key] ?? $0.key) }
            : []
        // Mix is the default and, when there is one track, the only mode:
        // it unmutes everything, which is what "play this meeting" means.
        track = "mix"
        time = origin
        for deck in decks {
            watch(deck)
            deck.waiting = deck.offset > origin + 0.001
            if deck.waiting { deck.player.seek(to: .zero) }
        }
        applyMuting()
    }

    func toggle() {
        playing ? pause() : play()
    }

    func play() {
        guard hasAudio else { return }
        // Replaying from the end starts over rather than doing nothing.
        if decks.allSatisfy(\.ended) { seek(origin) }
        playing = true
        for deck in decks where !deck.waiting {
            deck.ended = false
            deck.player.play()
        }
    }

    func pause() {
        playing = false
        for deck in decks { deck.player.pause() }
    }

    /// Seek the whole meeting, not one file: each deck goes to the same
    /// MOMENT, which is `t` minus its own start offset.
    func seek(_ t: Double) {
        guard hasAudio else { return }
        let clamped = duration > 0 ? min(max(t, origin), duration) : max(t, origin)
        time = clamped
        for deck in decks {
            deck.ended = false
            let local = clamped - deck.offset
            if local < 0 {
                deck.waiting = true
                deck.player.pause()
                deck.player.seek(to: .zero)
            } else {
                deck.waiting = false
                deck.player.seek(to: CMTime(seconds: local, preferredTimescale: 600),
                                 toleranceBefore: .zero, toleranceAfter: .zero)
                if playing { deck.player.play() }
            }
        }
    }

    func switchTrack(_ newTrack: String) {
        guard newTrack != track else { return }
        track = newTrack
        applyMuting()
    }

    /// The picker chooses what is AUDIBLE. Both decks keep running, so
    /// switching mid-playback never re-seeks, never re-buffers, and never
    /// loses your place.
    private func applyMuting() {
        for deck in decks {
            deck.player.isMuted = !(track == "mix" || track == deck.key)
        }
    }

    private func watch(_ deck: Deck) {
        let observer = deck.player.addPeriodicTimeObserver(
            forInterval: CMTime(seconds: 0.1, preferredTimescale: 600), queue: .main
        ) { [weak self] _ in
            Task { @MainActor in self?.tick() }
        }
        timeObservers.append((deck.player, observer))

        let key = deck.key
        if let item = deck.player.currentItem {
            // A missing or unreadable file used to be perfectly silent: the
            // play button toggled, the clock stayed at 0:00, and nothing said
            // why.
            statusWatch.append(item.observe(\.status, options: [.new]) { [weak self] item, _ in
                guard item.status == .failed else { return }
                let reason = item.error?.localizedDescription
                Task { @MainActor in self?.noteFailure(track: key, reason: reason) }
            })
            endWatch.append(NotificationCenter.default.addObserver(
                forName: AVPlayerItem.didPlayToEndTimeNotification,
                object: item, queue: .main
            ) { [weak self] _ in
                Task { @MainActor in self?.noteEnded(deck) }
            })
        }
    }

    /// One tick of the meeting clock: the furthest any playing track has got,
    /// expressed on the meeting's timeline, plus the moment a late track is
    /// due to join.
    private func tick() {
        guard !decks.isEmpty else { return }
        var furthest = origin
        var sawOne = false
        for deck in decks where !deck.waiting {
            let seconds = deck.player.currentTime().seconds
            guard seconds.isFinite else { continue }
            sawOne = true
            furthest = max(furthest, seconds + deck.offset)
        }
        if sawOne {
            time = duration > 0 ? min(furthest, duration) : furthest
        }
        for deck in decks where deck.waiting && time + 0.02 >= deck.offset {
            deck.waiting = false
            deck.player.seek(to: .zero)
            if playing { deck.player.play() }
        }
    }

    private func noteEnded(_ deck: Deck) {
        deck.ended = true
        guard decks.allSatisfy({ $0.ended || $0.waiting }) else { return }
        playing = false
        if duration > 0 { time = duration }
    }

    private func noteFailure(track key: String, reason: String?) {
        let name = Self.label[key] ?? key
        let why = reason.map { " (\($0))" } ?? ""
        failure = "The \(name.lowercased()) track wouldn't play\(why)."
        // A meeting whose only track is broken isn't playing anything.
        if decks.count == 1 { pause() }
    }

    /// An AVPlayer deallocated with a live periodic observer still attached
    /// raises, so the screen going away has to leave the players clean —
    /// .onDisappear only pauses them.
    deinit {
        for (player, observer) in timeObservers { player.removeTimeObserver(observer) }
        for token in endWatch { NotificationCenter.default.removeObserver(token) }
    }

    private func teardown() {
        pause()
        for (player, observer) in timeObservers { player.removeTimeObserver(observer) }
        timeObservers = []
        statusWatch = []
        for token in endWatch { NotificationCenter.default.removeObserver(token) }
        endWatch = []
        decks = []
        choices = []
        failure = nil
        time = 0
        origin = 0
    }
}
