// MeetingScribe — two panes: a quiet system-material library rail, and one
// document column that is always exactly one thing (Today, a meeting page,
// its transcript mode, or the live meeting). Recording lives in the sidebar
// record disc and the floating pill.
import SwiftUI
import AppKit

@main
struct MeetingScribeApp: App {
    @NSApplicationDelegateAdaptor(MainAppDelegate.self) var delegate

    var body: some Scene {
        WindowGroup("MeetingScribe") {
            ContentView()
        }
        .defaultSize(width: 1180, height: 780)
        // The Mac half of the app. Before this the whole product had three
        // keyboard shortcuts and no menu of its own: see Commands.swift for
        // the contract other views adopt to hang their own actions off it.
        .commands { MeetingScribeCommands() }
        // No `SwiftUI.Settings` scene: preferences are a route in the main
        // window now (DetailRoute.settings), and ⌘, selects it. A second
        // window showing the same controls would be two places to look and
        // one of them would always be the wrong one.
    }
}

final class MainAppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        NSApp.activate()
        // Before ContentView exists, because a click on "Transcript ready" or
        // on a meeting nudge can be what launched the app: the delegate has to
        // be in place to catch what arrives with it.
        MSNotifications.install()
        // And ask at launch, because the first thing this app has to say is
        // "your 10 o'clock is starting" — a question that cannot wait for the
        // first recording to end, because it is what causes one.
        MSNotifications.requestAuthorizationIfNeeded()

        // Everything that has to outlive the main window is wired here, and
        // nowhere else. ContentView is a view of these; it does not own them.
        // ⌘W used to take the recorder poll, the nudge poll and the floating
        // pill down with the window, which is how a meeting could start with
        // the app running and nothing said about it.
        let center = RecorderCenter.shared
        HUDController.shared.attach(center: center)
        EngineManager.shared.isRecording = { center.phase == .recording }
        // The window the watchdog needs: three misses at four seconds, plus
        // the poll that noticed. A minute covers it with room.
        EngineManager.shared.wasRecordingRecently = { center.wasRecording(within: 60) }
        // The engine died under a live meeting and the watchdog quietly put it
        // back, so no failure banner will be left on screen to say so. The
        // pill is the one surface that is always there to say it instead.
        EngineManager.shared.onLostRecording = {
            center.alert = RecorderAlert(
                kind: .lost,
                message: "The engine stopped while a meeting was recording. The audio up to "
                    + "that moment is saved — open that meeting and press Reprocess.")
        }
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard let refusal = EngineManager.shared.prepareForQuit()
        else { return .terminateNow }
        let alert = NSAlert()
        alert.messageText = refusal.title
        alert.informativeText = refusal.body
        alert.addButton(withTitle: "OK")
        // The engine can be mid-write for minutes. Waiting is the right
        // answer and the default one, but this is the user's Mac: an app
        // whose only remaining exit is Force Quit is a worse trap than the
        // half-written meeting it is guarding.
        if refusal.overridable { alert.addButton(withTitle: "Quit Anyway") }
        let choice = alert.runModal()
        if refusal.overridable, choice == .alertSecondButtonReturn { return .terminateNow }
        return .terminateCancel
    }
}

// MARK: - Routes

enum DetailRoute: Hashable {
    case today
    /// Settings, in the document column like everything else.
    ///
    /// It used to be a `SwiftUI.Settings` scene — a separate floating window,
    /// declared with no size, which opened short enough to clip its own last
    /// section under the title bar. A panel you have to know the shortcut for
    /// and then fight to read is not where a user's preferences should live.
    case settings
    case meeting(String)
}

// MARK: - Library

struct RowMeta {
    var brief: String?
    var hasTranscript = false
    var hasSummary = false
    var hasNotes = false
    /// The engine warned that this recording is not what it looks like —
    /// system audio macOS blocked, a mic that wasn't there. The page carries
    /// the sentence; the row carries the fact that there is one, because a
    /// warning nobody opens the meeting to read is a warning nobody has read.
    var hasCaptureWarning = false
}

@MainActor
final class Library: ObservableObject {
    @Published var meetings: [MeetingListItem] = []
    @Published var loadError: String?
    @Published var meta: [String: RowMeta] = [:]
    @Published var briefs: [String: String] = [:]
    /// What the engine is doing to each meeting it is working on, in its own
    /// words: "Downloading the speech model, 340 MB of 2.5 GB", "Transcribing
    /// 4/9". Keyed by meeting id, empty when nothing is in flight.
    @Published var progress: [String: String] = [:]
    private var metaFetches = Set<String>()
    private var searchTask: Task<Void, Never>?
    private var watchTask: Task<Void, Never>?
    /// Which registration in `watchTask` is the current one. A poller that
    /// resumes after being cancelled and replaced must not clear its
    /// successor's slot — see watchWorkInFlight.
    private var watchGeneration = 0
    private var query = ""

    /// nil means "whatever the user is already searching for". A plain
    /// `refresh()` used to reset the query to "", so a refresh after an edit
    /// silently dropped the user out of their own search results.
    func refresh(query: String? = nil) async {
        if let query { self.query = query }
        do {
            meetings = try await API.meetings(query: self.query)
            loadError = nil
        } catch {
            loadError = "The engine isn't answering. Nothing is lost, it just isn't listening yet."
        }
        if meetings.contains(where: { meetingIsWorking($0.status) }) {
            await refreshProgress()
        }
        watchWorkInFlight()
    }

    /// The engine's line for every meeting it is working on. One request for
    /// the whole library, taken on the same tick as the list rather than by a
    /// second poller racing it.
    private func refreshProgress() async {
        applyProgress(await API.processingJobs())
    }

    /// The engine's job table as the per-row progress lines. Its own function
    /// so the poller, which already has the table in hand, does not fetch the
    /// same document a second time to say the same thing differently.
    private func applyProgress(_ jobs: [String: EngineJob]) {
        progress = jobs.compactMapValues { job in
            guard job.state == "processing" else { return nil }
            let message = job.message?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
            return message.isEmpty ? nil : message
        }
    }

    /// Recording just stopped, so a meeting is on its way that the engine has
    /// probably not written yet.
    ///
    /// onRecordingStopped fires the instant Stop is pressed, and refreshing
    /// then returns a list that does NOT contain the new meeting: the folder
    /// and meeting.json are written a moment later. Nothing in that list looks
    /// unfinished, so watching for in-flight work alone never starts, and the
    /// meeting simply never appears. Watch on the EVENT, not on the evidence.
    func expectNewMeeting() {
        watchTask?.cancel()
        watchTask = nil
        watchWorkInFlight(force: true)
    }

    /// Keep the list honest while the engine is still working on something.
    ///
    /// The list is otherwise only fetched at launch, when the engine comes up,
    /// and from onRecordingStopped, which is before transcription, diarization
    /// and summarizing have run. A row caught at that instant says
    /// "Processing" and, with nothing to refresh it, said so until the app was
    /// restarted. Poll only while there is a reason to, so an idle library
    /// makes no requests at all.
    private func watchWorkInFlight(force: Bool = false) {
        let working = meetings.contains { meetingIsWorking($0.status) }
        guard working || force else { watchTask?.cancel(); watchTask = nil; return }
        guard watchTask == nil else { return }
        let known = Set(meetings.map(\.id))
        let initialPending = Set(meetings.filter { meetingIsWorking($0.status) }.map(\.id))
        // expectNewMeeting cancels this task and starts another one straight
        // away. Cancellation is not immediate — the old task is asleep and
        // resumes later — so an unconditional `watchTask = nil` on the way out
        // erased the NEWER task's registration, and the next caller, seeing an
        // empty slot, started a second poller alongside it. Only clear the slot
        // if it is still ours.
        watchGeneration += 1
        let generation = watchGeneration
        watchTask = Task { [weak self] in
            defer { if self?.watchGeneration == generation { self?.watchTask = nil } }
            var pending = initialPending
            // When we are waiting for a meeting that does not exist yet, give
            // the engine a bounded window to produce it rather than polling on
            // forever if a recording produced nothing at all.
            var ticksWaitingForArrival = force ? 30 : 0
            // Summaries now start on their own at the end of a transcription
            // (app._auto_summarize), and they start AFTER the status goes to
            // "done" — so watching transcription alone stopped this loop at
            // the exact moment the summary began, and the row that was about
            // to gain a headline and a sparkle kept neither until relaunch.
            var summarising = Set<String>()
            // …and the claim lands a beat AFTER that, with a folder rename and
            // a phone push in between, so there is a window where transcription
            // is finished and the summary is not yet registered. Ticking inside
            // it would see nothing in flight and stop. Hold the loop open across
            // the gap instead.
            var ticksAwaitingSummary = 0
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(2))
                guard !Task.isCancelled, let self else { return }
                guard let fresh = try? await API.meetings(query: self.query) else { continue }
                self.meetings = fresh
                let (processing, summaryJobs) = await API.allJobs()
                self.applyProgress(processing)
                let stillWorking = Set(fresh.filter { meetingIsWorking($0.status) }.map(\.id))
                let stillSummarising = Set(summaryJobs.filter { $0.value.state == "processing" }
                                                      .map(\.key))
                // Only the rows that finished ON THIS TICK: their brief and
                // badges were fetched while the transcript, or the summary,
                // did not exist yet.
                let justTranscribed = pending.subtracting(stillWorking)
                self.announce(justTranscribed, in: fresh, jobs: processing)
                for id in justTranscribed.union(summarising.subtracting(stillSummarising)) {
                    self.metaFetches.remove(id)
                    self.meta[id] = nil
                    self.fetchBrief(for: id)
                }
                if !justTranscribed.isEmpty { ticksAwaitingSummary = 5 }
                pending = stillWorking
                summarising = stillSummarising
                if !stillWorking.isEmpty || !stillSummarising.isEmpty {
                    ticksWaitingForArrival = 0
                    ticksAwaitingSummary = 0
                    continue
                }
                if ticksAwaitingSummary > 0 { ticksAwaitingSummary -= 1; continue }
                // Nothing is in flight. Keep waiting only while a meeting we
                // have not seen before is still expected to turn up.
                if ticksWaitingForArrival > 0, Set(fresh.map(\.id)).subtracting(known).isEmpty {
                    ticksWaitingForArrival -= 1
                    continue
                }
                return
            }
        }
    }

    /// Say out loud that these meetings are done, because nothing else does.
    ///
    /// This poll is the only place in the app that knows the exact tick a
    /// transcription finished, and a 45-minute meeting spends about fifteen
    /// minutes here: by the time it lands the user is in another app, in
    /// another window, or away from the Mac entirely. That is also the whole
    /// test — with MeetingScribe in front of them, the row losing its shimmer
    /// and gaining its badges has already said everything a banner would, and
    /// a banner on top of it would be the app narrating what they can see.
    ///
    /// A meeting that left the working state without appearing in the fresh
    /// list was deleted mid-run, and has nothing to announce.
    private func announce(_ finished: Set<String>, in fresh: [MeetingListItem],
                          jobs: [String: EngineJob]) {
        guard !finished.isEmpty, !NSApp.isActive else { return }
        for id in finished {
            guard let row = fresh.first(where: { $0.id == id }) else { continue }
            let failed = row.status == "error"
            MSNotifications.postTranscriptReady(
                meetingID: id,
                title: row.title,
                ok: !failed,
                // The engine writes the reason into the job it just failed, in
                // the same words the meeting page will show. Absent — the
                // engine restarted under us — postTranscriptReady has a plain
                // sentence of its own.
                message: failed ? jobs[id]?.message : nil)
        }
    }

    func search(_ query: String) {
        searchTask?.cancel()
        searchTask = Task {
            try? await Task.sleep(for: .milliseconds(220))
            guard !Task.isCancelled else { return }
            await refresh(query: query)
        }
    }

    /// The row view for one meeting.
    ///
    /// The list already carries it, so the common case costs nothing: this
    /// used to fetch the whole document per visible row (43 KB against 191 B
    /// on a 400-turn meeting, every turn crossing the socket), and
    /// /api/meetings/<id> also takes JOB_LOCK to persist notes, which put a
    /// lock-taking write in front of the very job the row was reporting on.
    /// Only a row that just finished, whose list entry was read while the
    /// transcript did not exist yet, asks the engine again, and then for the
    /// cheap /brief rather than the document.
    func fetchBrief(for id: String) {
        guard meta[id] == nil, !metaFetches.contains(id) else { return }
        if let row = meetings.first(where: { $0.id == id }), row.brief != nil {
            let m = row.rowMeta
            meta[id] = m
            if let b = m.brief { briefs[id] = b }
            return
        }
        metaFetches.insert(id)
        Task {
            // A failed read used to leave the id in metaFetches for the life of
            // the app, and the guard at the top of this function then refused
            // every later attempt: one dropped request meant a row that never
            // got its brief again. Matches what invalidate(_:) already does.
            defer { metaFetches.remove(id) }
            guard let m = try? await API.brief(id) else { return }
            msWithAnimation(Motion.seek) {
                meta[id] = m
                if let b = m.brief { briefs[id] = b }
            }
        }
    }

    /// One row's badges and brief went stale because the meeting was rewritten
    /// under it — a summary was written, a tidy landed.
    ///
    /// Deliberately does NOT go via the list: the cached row is the very thing
    /// that is stale, so fetchBrief's shortcut would put the same stale answer
    /// straight back. /brief is the cheap document-free read.
    ///
    /// Nothing called this for a summary, so writing one left the sidebar
    /// without its sparkle and the Meeting menu still offering to write the
    /// summary that had just been written. Only a status change refreshed a
    /// row, and summarising is not a status change.
    func invalidate(_ id: String) {
        guard !metaFetches.contains(id) else { return }
        metaFetches.insert(id)
        Task {
            defer { metaFetches.remove(id) }
            guard let m = try? await API.brief(id) else { return }
            msWithAnimation(Motion.seek) {
                meta[id] = m
                if let b = m.brief { briefs[id] = b }
            }
        }
    }
}

// MARK: - Root

struct ContentView: View {
    @StateObject private var library = Library()
    // Observed, not owned: the engine and the recorder are singletons that
    // outlive this view, and @StateObject would claim the view creates and
    // owns their lifetime — which is exactly what it used to claim about the
    // recorder, and exactly why closing the window stopped recording working.
    // Both are created and wired in MainAppDelegate; the pill with them.
    @ObservedObject private var center = RecorderCenter.shared
    @ObservedObject private var engine = EngineManager.shared
    @State private var route: DetailRoute? = .today
    @State private var mode: PageMode = .document
    @State private var query = ""
    @State private var showOnboarding =
        !UserDefaults.standard.bool(forKey: "ms.onboarded.v1")
        || CommandLine.arguments.contains("--onboarding")
    /// The fields of the open meeting that MeetingDetail doesn't decode: the
    /// speaker count in force and whether a tidy can still be undone. The
    /// menu bar reads its enablement from these.
    @State private var corrections: API.Corrections?
    @State private var showSpeakerCount = false
    @State private var confirmTidy = false
    @State private var confirmUndoTidy = false
    @State private var actionError: String?
    @State private var busy = false
    /// Bumped when something rewrites a meeting on disk. It is part of the
    /// detail column's identity, so the open document reloads itself: the
    /// same thing clicking away and back would do, without the click.
    @State private var reloadTokens: [String: Int] = [:]
    @FocusState private var searchFocused: Bool

    var body: some View {
        NavigationSplitView {
            SidebarView(library: library, center: center, route: $route, query: query)
                .navigationSplitViewColumnWidth(min: 244, ideal: 272, max: 320)
        } detail: {
            detailColumn
                .navigationTitle("")   // the wordmark in the toolbar is the title
                .safeAreaInset(edge: .top, spacing: 0) {
                    // Above the document, not over it: an engine that is down
                    // takes recording, transcripts and search with it, and the
                    // app used to say nothing at all.
                    if engine.state.message != nil {
                        EngineBanner(engine: engine)
                            .transition(.move(edge: .top).combined(with: .opacity))
                    }
                }
                .msAnimation(Motion.enter, value: engine.state)
        }
        .tint(MS.interactive)
        .environmentObject(library)
        .environmentObject(center)
        .toolbar {
            ToolbarItem(placement: .navigation) {
                HStack(spacing: 7) {
                    Image(systemName: "waveform")
                        .font(MSFont.chromeMedium)
                        .foregroundStyle(MS.playhead)
                        .msDecorative()
                    Text("MeetingScribe")
                        .font(MSFont.sectionHeading)
                        .foregroundStyle(MS.ink)
                }
                .fixedSize()
                .accessibilityElement(children: .combine)
                .accessibilityLabel("MeetingScribe")
            }
            .sharedBackgroundVisibility(.hidden)
            ToolbarItem(placement: .primaryAction) {
                RecordToolbarButton(center: center)
            }
        }
        .sheet(isPresented: $showOnboarding) {
            OnboardingFlow(presented: $showOnboarding)
                .environmentObject(center)
        }
        .sheet(isPresented: $showSpeakerCount) {
            if let id = currentMeetingID {
                SpeakerCountSheet(meetingID: id,
                                  corrections: corrections,
                                  apply: { await recluster(id, speakers: $0) },
                                  presented: $showSpeakerCount)
            }
        }
        .alert("Tidy this transcript?", isPresented: $confirmTidy) {
            Button("Tidy") { tidy() }
            Button("Cancel", role: .cancel) { }
        } message: {
            Text("The on-device model removes echoed duplicate lines, trims stray words and merges speaker labels that are obviously the same person. Nothing leaves this Mac, no words are invented, and the original transcript is backed up so you can undo it.")
        }
        .alert("Undo the tidy?", isPresented: $confirmUndoTidy) {
            Button("Undo Tidy", role: .destructive) { undoTidy() }
            Button("Cancel", role: .cancel) { }
        } message: {
            Text("The transcript goes back to exactly what the recogniser wrote. Notes you have taken since are kept.")
        }
        .alert("That didn't work",
               isPresented: Binding(get: { actionError != nil },
                                    set: { if !$0 { actionError = nil } })) {
            Button("OK") { actionError = nil }
        } message: {
            Text(actionError ?? "")
        }
        .searchable(text: $query, placement: .sidebar, prompt: "Search meetings")
        .searchFocused($searchFocused)
        .onChange(of: query) { library.search(query) }
        .onChange(of: engine.state) {
            // The engine just came up (or came back): fetch for real.
            if engine.state == .running {
                Task { await library.refresh(query: query) }
            }
        }
        // One value, published on every change, is the whole of what the menu
        // bar is allowed to offer. See Commands.swift.
        .focusedSceneValue(\.msContext, menuContext)
        .onChange(of: menuContext, initial: true) {
            CommandBus.shared.publish(menuContext)
        }
        .onMSCommands(perform: run)
        .onMSMeetingChange { id in
            reloadTokens[id, default: 0] += 1
            Task { await refreshAfterChange(id) }
        }
        // A notification was clicked while the app was already running. It is
        // not an MSCommand: every command in Commands.swift acts on whatever
        // is already selected, and this one arrives carrying its own meeting.
        .onReceive(NotificationCenter.default.publisher(for: .msOpenMeeting)) { note in
            guard let id = note.object as? String else { return }
            openMeeting(id)
        }
        .task(id: currentMeetingID) {
            corrections = nil
            guard let id = currentMeetingID else { return }
            corrections = await API.corrections(id)
        }
        .task {
            // The window's own business: the sidebar list is the one thing
            // here that belongs to the window. Everything the recorder needs
            // from the app is wired in MainAppDelegate, where it survives ⌘W.
            center.onRecordingStopped = { [weak library] in
                Task {
                    await library?.refresh()
                    // The meeting is not on disk yet, so that refresh cannot
                    // have seen it. Keep looking until it turns up and lands.
                    library?.expectNewMeeting()
                }
            }
            await EngineManager.shared.ensureRunning()
            await library.refresh()
            // A "Transcript ready" click that LAUNCHED the app rather than
            // switching to it: the delegate ran before this view existed, so
            // the meeting it asked for waited here for someone to collect it.
            if let id = MSNotifications.takePendingMeeting() { openMeeting(id) }
            if let i = CommandLine.arguments.firstIndex(of: "--open"),
               CommandLine.arguments.indices.contains(i + 1) {
                route = .meeting(CommandLine.arguments[i + 1])
                if CommandLine.arguments.contains("--transcript") { mode = .transcript }
            }
        }
    }

    @ViewBuilder
    private var detailColumn: some View {
        if center.phase == .recording {
            LivePage(center: center)
        } else {
            switch route {
            case .meeting(let id):
                MeetingScreen(meetingID: id, mode: $mode)
                    .id("\(id)#\(reloadTokens[id] ?? 0)")
            case .settings:
                SettingsView()
            default:
                TodayPage { id in
                    route = .meeting(id)
                    mode = .document
                }
            }
        }
    }

    /// Show one meeting, from somewhere outside the window: a notification
    /// click, or the id that was waiting when the window opened. The document
    /// mode is reset with it, because arriving from a "Transcript ready" and
    /// landing in whatever mode was left open a week ago is not arriving.
    private func openMeeting(_ id: String) {
        MSNotifications.clearPendingMeeting()
        msWithAnimation(Motion.enter) {
            route = .meeting(id)
            mode = .document
        }
    }

    // MARK: - What the menu bar is allowed to offer

    private var currentMeetingID: String? {
        if case .meeting(let id) = route { return id }
        return nil
    }

    private var menuContext: MSContext {
        var ctx = MSContext()
        ctx.meetingID = currentMeetingID
        ctx.recording = center.phase == .recording
        ctx.engineUp = center.phase != .offline
        ctx.mode = mode
        // The library's own badges answer this before the second read lands,
        // so the menu is right on the first frame rather than one fetch late.
        let known = currentMeetingID.flatMap { library.meta[$0] }
        ctx.hasTranscript = corrections?.hasTranscript ?? (known?.hasTranscript ?? false)
        ctx.hasSummary = known?.hasSummary ?? false
        ctx.canUndoTidy = corrections?.tidied != nil
        ctx.speakerCount = SpeakerCount.displayed(corrections)
        ctx.speakerCountLabel = SpeakerCount.label(corrections)
        ctx.canReorderSelection = library.meetings.count > 1
        return ctx
    }

    // MARK: - Running a command

    private func run(_ command: MSCommand) {
        switch command {
        case .toggleRecording:
            center.phase == .recording ? center.stopRecording() : center.startRecording()
        case .showToday:
            route = .today
        case .showSettings:
            route = .settings
        case .showNotes:
            msWithAnimation(Motion.enter) { mode = .document }
        case .showTranscript:
            msWithAnimation(Motion.enter) { mode = .transcript }
        case .nextMeeting:
            step(by: 1)
        case .previousMeeting:
            step(by: -1)
        case .focusSearch:
            searchFocused = true
        case .copyTranscript:
            copyTranscript()
        case .revealInFinder:
            guard let id = currentMeetingID else { return }
            Task { await API.post("api/meetings/\(id)/reveal") }
        case .deleteMeeting:
            // Handled by SidebarView, which owns the confirmation and the
            // "what do we select afterwards" question. The first adopter of
            // the contract in Commands.swift, and the reason it exists.
            break
        case .reanalyse:
            guard let id = currentMeetingID else { return }
            Task {
                let (ok, err) = await API.summarize(id)
                if ok { bumpReload(id) } else { actionError = err }
            }
        case .reprocessAudio:
            guard let id = currentMeetingID else { return }
            Task {
                let (ok, err) = await API.reprocess(id)
                if ok { bumpReload(id); await refreshAfterChange(id) } else { actionError = err }
            }
        case .speakerCount:
            guard currentMeetingID != nil else { return }
            showSpeakerCount = true
        case .setSpeakerCount(let n):
            guard let id = currentMeetingID else { return }
            Task {
                let (ok, err) = await recluster(id, speakers: n)
                if !ok { actionError = err }
            }
        case .tidyTranscript:
            guard currentMeetingID != nil else { return }
            confirmTidy = true
        case .undoTidy:
            guard currentMeetingID != nil else { return }
            confirmUndoTidy = true
        }
    }

    private func step(by delta: Int) {
        let ids = library.meetings.map(\.id)
        guard !ids.isEmpty else { return }
        guard let id = currentMeetingID, let i = ids.firstIndex(of: id) else {
            route = .meeting(ids[delta > 0 ? 0 : ids.count - 1])
            return
        }
        let next = i + delta
        guard ids.indices.contains(next) else { return }
        route = .meeting(ids[next])
    }

    private func copyTranscript() {
        guard let id = currentMeetingID else { return }
        Task {
            guard let detail = try? await API.meeting(id) else {
                actionError = "The engine didn't answer."
                return
            }
            let text = transcriptText(detail)
            guard !text.isEmpty else {
                actionError = "This meeting has no transcript yet."
                return
            }
            let pb = NSPasteboard.general
            pb.clearContents()
            pb.setString(text, forType: .string)
        }
    }

    // MARK: - Mutations, and the reload that follows one

    /// Re-cluster, and tell the rest of the app the document moved under it.
    /// Returns the engine's own refusal when it refuses, so the sheet can
    /// print it verbatim.
    private func recluster(_ id: String, speakers: Int?) async -> (Bool, String?) {
        guard !busy else { return (false, "Something else is already running on this meeting.") }
        busy = true
        let (ok, err) = await API.recluster(id, speakers: speakers)
        busy = false
        if ok {
            bumpReload(id)
            await refreshAfterChange(id)
        }
        return (ok, err)
    }

    private func tidy() {
        guard let id = currentMeetingID else { return }
        Task {
            let (ok, err) = await API.tidy(id)
            guard ok else { actionError = err; return }
            // Tidy runs as an ordinary processing job, so the reloaded page
            // picks up the engine's own progress line and finishes on its own.
            bumpReload(id)
            await refreshAfterChange(id)
        }
    }

    private func undoTidy() {
        guard let id = currentMeetingID else { return }
        Task {
            let (ok, err) = await API.undoTidy(id)
            guard ok else { actionError = err; return }
            bumpReload(id)
            await refreshAfterChange(id)
        }
    }

    private func bumpReload(_ id: String) {
        reloadTokens[id, default: 0] += 1
    }

    /// A meeting was rewritten. `corrections` describes the OPEN meeting and
    /// nothing else, so it is only re-read when the meeting that changed is
    /// the one on screen: the sidebar can re-cluster a row nobody is looking
    /// at, and loading that row's count into this state would leave the menu
    /// bar ticking a number from the wrong meeting.
    private func refreshAfterChange(_ id: String) async {
        if id == currentMeetingID {
            corrections = await API.corrections(id)
        }
        await library.refresh(query: query)
    }
}

// MARK: - Engine banner

/// The engine is down. Nothing in this app works without it — recording,
/// transcripts, search and Ask are all its answers — so the failure gets a
/// line of its own above the document, the reason in the engine's words, and
/// the one button that fixes it. Before this the app just went quiet.
struct EngineBanner: View {
    @ObservedObject var engine: EngineManager
    @State private var restarting = false

    var body: some View {
        HStack(alignment: .top, spacing: 11) {
            Image(systemName: "bolt.slash")
                .font(MSFont.chromeMedium)
                .foregroundStyle(MS.ink2)
                .offset(y: 1)
                .msDecorative()

            VStack(alignment: .leading, spacing: 3) {
                Text(engine.state.message ?? "")
                    .font(MSFont.chromeMedium)
                    .foregroundStyle(MS.ink)
                Text(engine.lostRecording
                     ? "A recording was running. The audio saved up to that moment is still on this Mac: restart, then open that meeting and press Reprocess."
                     : "Recording, transcripts, search and Ask all need it. Nothing already saved is affected.")
                    .font(MSFont.meta)
                    .foregroundStyle(MS.ink2)
                    .fixedSize(horizontal: false, vertical: true)
            }

            Spacer(minLength: 12)

            Button {
                restarting = true
                Task {
                    await engine.restart()
                    restarting = false
                }
            } label: {
                Group {
                    if restarting {
                        HStack(spacing: 7) {
                            ProgressView().controlSize(.small)
                            Text("Starting…")
                        }
                    } else {
                        Text("Restart the engine")
                    }
                }
                .font(MSFont.chromeMedium)
                .foregroundStyle(MS.inkOnAccent)
                .padding(.horizontal, 14)
                .padding(.vertical, 6)
                .background(MS.playheadFill, in: .capsule)
            }
            .buttonStyle(PressStyle())
            .disabled(restarting)
        }
        .padding(.horizontal, 20)
        .padding(.vertical, 12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(MS.raised)
        .overlay(alignment: .bottom) {
            Rectangle().fill(MS.hairline).frame(height: 1)
        }
    }
}

// MARK: - Sidebar

private struct DayGroup: Identifiable {
    let id: String
    let title: String
    let isToday: Bool
    let meetings: [MeetingListItem]
}

struct SidebarView: View {
    @ObservedObject var library: Library
    @ObservedObject var center: RecorderCenter
    @Binding var route: DetailRoute?
    /// What the user is searching for, so a refresh after an edit doesn't
    /// silently drop them out of their own search results.
    var query: String = ""
    /// So the footer's engine label follows a change made in the pane it opens.
    @ObservedObject private var settings = Settings.shared
    /// For the "answer ready" mark: a question asked three pages ago finished
    /// while the user was elsewhere, and its row is where they find out.
    @ObservedObject private var askCenter = AskCenter.shared
    @State private var pendingDelete: MeetingListItem?
    @State private var deleteError: String?
    @State private var reprocessError: String?
    @State private var actionError: String?

    var body: some View {
        List(selection: $route) {
            Label("Today", systemImage: "sun.horizon")
                .font(MSFont.chrome)
                .tag(DetailRoute.today)

            if center.phase == .recording {
                liveRow
            }

            if let err = library.loadError {
                Text(err)
                    .font(MSFont.meta)
                    .foregroundStyle(MS.ink2)
            }

            ForEach(groups) { group in
                Section {
                    ForEach(group.meetings) { m in
                        MeetingRow(meeting: m,
                                   meta: library.meta[m.id],
                                   progress: library.progress[m.id],
                                   answerReady: askCenter.unread.contains(m.id),
                                   showBrief: group.isToday)
                            .tag(DetailRoute.meeting(m.id))
                            .task { library.fetchBrief(for: m.id) }
                            .contextMenu {
                                // The fast path for the correction that
                                // matters most. The menu bar's version and
                                // the sheet act on the OPEN meeting and can
                                // therefore tick the count in force; this one
                                // acts on the row under the pointer, which is
                                // usually what a right-click means, and asks
                                // the engine nothing until it is used.
                                Menu("Set Speaker Count") {
                                    Button("Auto") { recluster(m, speakers: nil) }
                                    Divider()
                                    ForEach(1...8, id: \.self) { n in
                                        Button("\(n)") { recluster(m, speakers: n) }
                                    }
                                }
                                Divider()
                                // Reprocess is the only way back from a failed
                                // meeting, and the audio it needs is still on
                                // disk. It belongs here even when the row looks
                                // healthy: it is also how a bad transcript is
                                // redone with today's settings.
                                Button("Reprocess Audio") { reprocess(m) }
                                Button("Reveal in Finder") {
                                    Task { await API.post("api/meetings/\(m.id)/reveal") }
                                }
                                Divider()
                                Button("Delete Meeting…", role: .destructive) {
                                    pendingDelete = m
                                }
                            }
                    }
                } header: {
                    HStack {
                        Text(group.title)
                        Spacer()
                        if group.isToday {
                            Text("\(group.meetings.count)")
                                .foregroundStyle(MS.ink3)
                        }
                    }
                    .font(MSFont.kicker)
                    .foregroundStyle(MS.ink2)
                    .textCase(nil)
                }
            }

            // Nothing to list. WHICH of the reasons it is matters, and until
            // now the rail said none of them: an empty library and a search
            // that matched nobody both drew an empty column. An engine that
            // isn't answering has already explained itself in the row above,
            // so it keeps the floor.
            if groups.isEmpty, library.loadError == nil {
                emptyState
                    .selectionDisabled()
            }
        }
        .listStyle(.sidebar)
        // The rail IS the sky. The list's own material is hidden so nothing
        // sits between the rows and the weather: sunken is the floor, the
        // aurora drifts above it at full strength, and the grain gives the
        // whole column its tooth. The rows keep clear backgrounds — a second
        // material over the field would fog it, and every ink in a row is
        // measured against sunken, which is exactly what is underneath.
        .scrollContentBackground(.hidden)
        .background {
            ZStack {
                MS.sunken
                MSAuroraField().msGrain()
            }
        }
        // Pinned to the foot of the rail rather than sitting in the list:
        // preferences are not one of the meetings, and a row that scrolls away
        // under 51 of them is a row nobody finds. Same place a Mac user already
        // looks for the account and the app's own controls.
        .safeAreaInset(edge: .bottom, spacing: 0) { settingsFooter }
        .onDeleteCommand { askToDeleteSelection() }
        // File ▸ Delete Meeting… (⌘⌫). The command is posted by the menu bar
        // and adopted HERE rather than in ContentView, because the
        // confirmation and the "what is selected afterwards" question both
        // live in this view. See the contract in Commands.swift.
        .onMSCommand(.deleteMeeting) { askToDeleteSelection() }
        .alert("Delete \u{201C}\(pendingDelete?.title ?? "")\u{201D}?",
               isPresented: Binding(get: { pendingDelete != nil },
                                    set: { if !$0 { pendingDelete = nil } })) {
            Button("Delete", role: .destructive) {
                if let m = pendingDelete { deleteMeeting(m) }
            }
            Button("Cancel", role: .cancel) { pendingDelete = nil }
        } message: {
            Text("The recording, transcript and summary are removed from this Mac. This can't be undone.")
        }
        .alert("Couldn't delete",
               isPresented: Binding(get: { deleteError != nil },
                                    set: { if !$0 { deleteError = nil } })) {
            Button("OK") { deleteError = nil }
        } message: {
            Text(deleteError ?? "")
        }
        .alert("Couldn't reprocess",
               isPresented: Binding(get: { reprocessError != nil },
                                    set: { if !$0 { reprocessError = nil } })) {
            Button("OK") { reprocessError = nil }
        } message: {
            Text(reprocessError ?? "")
        }
        .alert("Couldn't change the speaker count",
               isPresented: Binding(get: { actionError != nil },
                                    set: { if !$0 { actionError = nil } })) {
            Button("OK") { actionError = nil }
        } message: {
            Text(actionError ?? "")
        }
    }

    /// The rail with nothing in it.
    ///
    /// Two plain rows, in the register the rest of the rail speaks: no icon,
    /// no centred headline, no ContentUnavailableView. That control draws a
    /// page's empty state — a large glyph and a bold line — and a sidebar is
    /// not a page. The first line says what happened, the second says the one
    /// thing worth knowing next.
    private var emptyState: some View {
        VStack(alignment: .leading, spacing: 3) {
            if !searchText.isEmpty {
                // The query is quoted back because a sidebar search that
                // matched nothing is most often a typo, and a typo is only
                // visible when you can see what was typed.
                Text("Nothing matches \u{201C}\(searchText)\u{201D}.")
                    .font(MSFont.chrome)
                    .foregroundStyle(MS.ink2)
                Text("Titles and speaker names are searched.")
                    .font(MSFont.meta)
                    .foregroundStyle(MS.ink3)
            } else if center.phase == .recording {
                // A first-ever recording, in flight. "No meetings yet" is
                // true and useless here, and "press ⌘R" would be an
                // instruction to stop the thing they just started.
                Text("Your first meeting is recording now.")
                    .font(MSFont.chrome)
                    .foregroundStyle(MS.ink2)
                Text("It arrives here when the engine has finished with it.")
                    .font(MSFont.meta)
                    .foregroundStyle(MS.ink3)
            } else {
                Text("No meetings yet.")
                    .font(MSFont.chrome)
                    .foregroundStyle(MS.ink2)
                Text("Press ⌘R when your next call starts. Everything stays on this Mac.")
                    .font(MSFont.meta)
                    .foregroundStyle(MS.ink3)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(.vertical, 8)
        // Two fragments read as two rows nobody can act on. One sentence.
        .accessibilityElement(children: .combine)
    }

    /// What the user is actually searching for. The field is passed straight
    /// down from ContentView, so it is live: it changes on the keystroke,
    /// ahead of the debounced fetch that empties the list.
    private var searchText: String {
        query.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private func askToDeleteSelection() {
        if case .meeting(let id) = route,
           let m = library.meetings.first(where: { $0.id == id }) {
            pendingDelete = m
        }
    }

    /// Hand the meeting's saved audio back to the engine. The refresh is what
    /// starts the library's watcher again, so the row picks up the engine's
    /// progress from the next tick.
    private func reprocess(_ m: MeetingListItem) {
        Task {
            let (ok, err) = await API.reprocess(m.id)
            if ok {
                await library.refresh(query: query)
                CommandBus.shared.meetingDidChange(m.id)
            } else {
                reprocessError = err
            }
        }
    }

    /// Re-cluster this row's meeting into a fixed number of voices. The
    /// broadcast is how the open document finds out it was rewritten: see
    /// the contract in Commands.swift.
    private func recluster(_ m: MeetingListItem, speakers: Int?) {
        Task {
            let (ok, err) = await API.recluster(m.id, speakers: speakers)
            if ok {
                await library.refresh(query: query)
                CommandBus.shared.meetingDidChange(m.id)
            } else {
                actionError = err
            }
        }
    }

    private func deleteMeeting(_ m: MeetingListItem) {
        pendingDelete = nil
        Task {
            let (ok, err) = await API.deleteMeeting(m.id)
            if ok {
                if route == .meeting(m.id) { route = .today }
                await library.refresh(query: query)
            } else {
                deleteError = err
            }
        }
    }

    private var liveRow: some View {
        HStack(spacing: 8) {
            Circle().fill(MS.recordRed).frame(width: 7, height: 7)
                .msDecorative()
            Text("Recording…")
                .font(MSFont.chromeMedium)
                .msGenerationShimmer(true)
            Spacer()
            Text(clock(center.elapsed))
                .clockFont(11)
                .foregroundStyle(MS.ink2)
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("Recording now")
        .accessibilityValue(clock(center.elapsed))
    }

    /// The foot of the sidebar: where this app's own controls live.
    ///
    /// A hairline, then one row that behaves like every other row in the rail
    /// (same selected tint, same hit area) while living outside the scroll, so
    /// it is in the same place whether the library holds two meetings or two
    /// hundred. It carries the one fact worth reading at a glance — who writes
    /// the summaries — because that is the setting most likely to be wrong and
    /// the one people forget they chose.
    private var settingsFooter: some View {
        let selected = route == .settings
        return VStack(spacing: 0) {
            Rectangle().fill(MS.hairline).frame(height: 1)
            Button {
                route = .settings
            } label: {
                HStack(spacing: 8) {
                    Image(systemName: "gearshape")
                        .font(MSFont.chrome)
                        .foregroundStyle(selected ? MS.ink : MS.ink2)
                        .msDecorative()
                    Text("Settings")
                        .font(MSFont.chromeMedium)
                        .foregroundStyle(selected ? MS.ink : MS.ink2)
                    Spacer(minLength: 6)
                    Text(engineLabel)
                        .font(MSFont.meta)
                        .foregroundStyle(MS.ink3)
                        .lineLimit(1)
                }
                .padding(.horizontal, 10)
                .padding(.vertical, 7)
                // Selected, this row is a lit chip rather than a tinted
                // rectangle: less mint (0.16 against the sky reads as loud as
                // 0.20 did against a material) and a 1px edge light, which is
                // how every other raised thing in Aurora says it is raised.
                .background {
                    if selected {
                        MS.rr(MS.radius.sm)
                            .fill(MS.interactive.opacity(0.16))
                            .msEdgeLit(radius: MS.radius.sm)
                    }
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            // On the Button, not on the VStack around it. Collapsing the
            // container hid the Button inside it, so VoiceOver read a label
            // with nothing to activate; here the element IS the control, and
            // isButton comes free with it.
            .accessibilityElement(children: .ignore)
            .accessibilityLabel("Settings")
            .accessibilityValue("Summaries by \(engineLabel)")
            .accessibilityAddTraits(selected ? .isSelected : [])
            .padding(.horizontal, 8)
            .padding(.vertical, 7)
        }
        // Opaque, or the list scrolls THROUGH it: a safe-area inset reserves
        // the space but does not stop the rows passing underneath, so without
        // a surface the last meeting's title reads straight over the word
        // "Settings".
        .background(.bar)
    }

    /// Who writes the summaries, in the name the user picked it by.
    private var engineLabel: String {
        switch settings.summaryEngine {
        case "apple": return "Apple Intelligence"
        case let id: return id.prefix(1).uppercased() + id.dropFirst()
        }
    }

    private var groups: [DayGroup] {
        let cal = Calendar.current
        var buckets: [(key: String, title: String, isToday: Bool, items: [MeetingListItem])] = []
        for m in library.meetings {
            let date = createdParser.date(from: m.created) ?? .distantPast
            let key: String, title: String
            var isToday = false
            if cal.isDateInToday(date) {
                key = "today"; title = "Today"; isToday = true
            } else if cal.isDateInYesterday(date) {
                key = "yesterday"; title = "Yesterday"
            } else if date > Date().addingTimeInterval(-7 * 86400) {
                let day = cal.startOfDay(for: date)
                key = "d\(day.timeIntervalSince1970)"
                title = date.formatted(.dateTime.weekday(.wide).day().month())
            } else {
                key = date.formatted(.dateTime.year().month())
                title = date.formatted(.dateTime.month(.wide))
            }
            if let i = buckets.firstIndex(where: { $0.key == key }) {
                buckets[i].items.append(m)
            } else {
                buckets.append((key, title, isToday, [m]))
            }
        }
        return buckets.map { DayGroup(id: $0.key, title: $0.title, isToday: $0.isToday, meetings: $0.items) }
    }
}

// MARK: - Row (Voice Memos anatomy: zero colour, zero cards)

struct MeetingRow: View {
    let meeting: MeetingListItem
    var meta: RowMeta?
    /// The engine's live line for this meeting while it works on it.
    var progress: String?
    /// An Ask answer finished while this meeting's page was off screen.
    var answerReady = false
    var showBrief: Bool

    /// Work in flight. "error" is NOT work: the row used to shimmer on
    /// anything that wasn't "done", so a failed meeting shimmered "Error" for
    /// the life of the app and the library's poll loop could never exit.
    private var processing: Bool { meetingIsWorking(meeting.status) }
    private var failed: Bool { meeting.status == "error" }

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(meeting.title)
                .font(MSFont.chromeMedium)
                .foregroundStyle(MS.ink)
                .lineLimit(1)
                .msGenerationShimmer(processing)

            HStack(spacing: 6) {
                Text(timeText)
                    .font(MSFont.meta)
                    .foregroundStyle(MS.ink2)
                if let meta {
                    HStack(spacing: 4) {
                        if meta.hasTranscript {
                            Image(systemName: "quote.bubble").glyph()
                                .accessibilityLabel("Transcribed")
                        }
                        if meta.hasSummary {
                            Image(systemName: "sparkle").glyph()
                                .accessibilityLabel("Summarised")
                        }
                        if meta.hasNotes {
                            Image(systemName: "pencil.line").glyph()
                                .accessibilityLabel("Has your notes")
                        }
                        if meta.hasCaptureWarning {
                            Image(systemName: "exclamationmark.triangle")
                                .font(MSFont.kicker)
                                .foregroundStyle(MS.ink2)
                                .help("Something about this recording needs a look. Open it to read what.")
                                .accessibilityLabel("Needs a look")
                        }
                    }
                }
                if answerReady {
                    // Mint, because it is an invitation to click: the answer
                    // to a question asked here finished while the user was on
                    // another page, and opening the row opens the thread.
                    Image(systemName: "text.bubble.fill")
                        .font(MSFont.kicker)
                        .foregroundStyle(MS.interactive)
                        .help("An answer to your question is ready")
                        .accessibilityLabel("Answer ready")
                }
                Spacer(minLength: 0)
                if failed {
                    Label("Not transcribed", systemImage: "exclamationmark.triangle")
                        .font(MSFont.meta)
                        .foregroundStyle(MS.ink2)
                        .labelStyle(.titleAndIcon)
                } else if processing {
                    Text(meeting.status?.capitalized ?? "Working")
                        .font(MSFont.meta)
                        .foregroundStyle(MS.ink2)
                } else if let d = meeting.duration, d > 0 {
                    Text(clock(d))
                        .clockFont(12)
                        .foregroundStyle(MS.ink2)
                }
            }

            // While the engine works, its own words take the third line —
            // "Processing" alone was the whole story for a run that can spend
            // ten minutes downloading a model.
            if let progress, processing, !progress.isEmpty {
                Text(progress)
                    .font(MSFont.meta)
                    .foregroundStyle(MS.ink3)
                    .lineLimit(1)
                    .contentTransition(.opacity)
                    .transition(.offset(y: 3).combined(with: .opacity))
            } else if showBrief, let brief = meta?.brief, !brief.isEmpty {
                Text(brief)
                    .font(MSFont.meta)
                    .foregroundStyle(MS.ink3)
                    .lineLimit(1)
                    .transition(.offset(y: 3).combined(with: .opacity))
            }
        }
        .padding(.vertical, 4)
        .help(helpText)
        // Four lines of glyphs and numerals read as noise one fragment at a
        // time. One row, one sentence.
        .accessibilityElement(children: .combine)
        .accessibilityLabel(voiceOverLabel)
    }

    /// The row as a sentence: what it is, when it was, and what state it is
    /// in. Deliberately not the same string as the tooltip: a tooltip is read
    /// after you have already seen the row, this is instead of seeing it.
    private var voiceOverLabel: String {
        var parts = [meeting.title]
        if !timeText.isEmpty { parts.append(timeText) }
        if failed {
            parts.append("not transcribed")
        } else if processing {
            parts.append(progress?.isEmpty == false
                         ? (progress ?? "")
                         : (meeting.status ?? "working"))
        } else if let d = meeting.duration, d > 0 {
            parts.append("\(Int(d / 60)) min")
        }
        // An explicit label replaces the children, badges included, so every
        // glyph in the row has to be said again here or it is said to nobody.
        if meta?.hasCaptureWarning == true { parts.append("needs a look") }
        if answerReady { parts.append("answer ready") }
        if meta?.hasTranscript == true { parts.append("transcribed") }
        if meta?.hasSummary == true { parts.append("summarised") }
        if meta?.hasNotes == true { parts.append("has your notes") }
        if let brief = meta?.brief, !brief.isEmpty { parts.append(brief) }
        return parts.joined(separator: ", ")
    }

    private var helpText: String {
        if processing, let progress, !progress.isEmpty { return progress }
        if failed { return "Processing failed. Right-click to reprocess the saved audio." }
        return showBrief ? "" : (meta?.brief ?? "")
    }

    private var timeText: String {
        guard let date = createdParser.date(from: meeting.created) else { return "" }
        return date.formatted(.dateTime.hour().minute())
    }
}

private extension Image {
    func glyph() -> some View {
        self.font(MSFont.kicker)
            .foregroundStyle(MS.ink3)
    }
}

// MARK: - Record (toolbar)

/// Recording starts where actions live on a Mac: the toolbar. A quiet
/// capsule, small red dot plus "Record", that becomes the live elapsed clock
/// with a stop square while recording.
///
/// The ⌘R shortcut now belongs to File ▸ Start Recording. A button and a
/// menu item cannot both own one key equivalent without the menu quietly
/// winning, and the menu is the one that works when this window is behind
/// something else.
struct RecordToolbarButton: View {
    @ObservedObject var center: RecorderCenter

    private var recording: Bool { center.phase == .recording }
    /// Armed: the engine is up and nothing is recording, so pressing this
    /// starts a meeting. Offline the button is disabled and idle is the only
    /// other phase there is — the state the beam needs is already read here.
    private var armed: Bool { center.phase == .idle }

    var body: some View {
        Button {
            recording ? center.stopRecording() : center.startRecording()
        } label: {
            HStack(spacing: 5.5) {
                if recording {
                    // Glyph geometry, not a container: the stop square keeps
                    // its own 2pt radius.
                    RoundedRectangle(cornerRadius: 2, style: .continuous)
                        .fill(MS.inkOnRecord)
                        .frame(width: 8, height: 8)
                    Text(clock(center.elapsed))
                        .clockFont(12, weight: .bold)
                } else {
                    Image(systemName: "record.circle")
                        .font(MSFont.chromeMedium)
                    Text("Record")
                        .font(MSFont.chromeMedium)
                }
            }
            .foregroundStyle(MS.inkOnRecord)
            .padding(.horizontal, 3)
            .msAnimation(Motion.exit, value: recording)
        }
        .buttonStyle(.borderedProminent)
        .tint(MS.recordRed)
        // One of the beam's two homes: the button that is ready to record.
        // The prominent style draws a capsule, so half the button's own height
        // IS its corner radius — the arc traces the border the button actually
        // has rather than a guess at it, and the clip keeps the blurred twin
        // off the toolbar's other items. While recording there is nothing to
        // arm and the overlay does not exist at all.
        .overlay {
            if armed {
                GeometryReader { geo in
                    MSBeam(radius: geo.size.height / 2, period: 7)
                }
                .clipShape(.capsule)
                .allowsHitTesting(false)
            }
        }
        .disabled(center.phase == .offline)
        .help(recording ? "Stop recording (⌘R)" : "Start recording (⌘R)")
        .accessibilityLabel(recording ? "Stop recording" : "Start recording")
        .accessibilityValue(recording ? clock(center.elapsed) : "")
    }
}
