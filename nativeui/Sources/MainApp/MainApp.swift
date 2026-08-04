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

        SwiftUI.Settings {
            SettingsView()
        }
    }
}

final class MainAppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        NSApp.activate()
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard let refusal = MainActor.assumeIsolated({ EngineManager.shared.prepareForQuit() })
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
    private var query = ""

    func refresh(query: String = "") async {
        self.query = query
        do {
            meetings = try await API.meetings(query: query)
            loadError = nil
        } catch {
            loadError = "The engine isn't answering. Nothing is lost — it just isn't listening yet."
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
        var next: [String: String] = [:]
        for (id, job) in await API.processingJobs() where job.state == "processing" {
            let message = job.message?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
            if !message.isEmpty { next[id] = message }
        }
        progress = next
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
        watchTask = Task { [weak self] in
            defer { self?.watchTask = nil }
            var pending = Set(meetings.filter { meetingIsWorking($0.status) }.map(\.id))
            // When we are waiting for a meeting that does not exist yet, give
            // the engine a bounded window to produce it rather than polling on
            // forever if a recording produced nothing at all.
            var ticksWaitingForArrival = force ? 30 : 0
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 2_000_000_000)
                guard !Task.isCancelled, let self else { return }
                guard let fresh = try? await API.meetings(query: self.query) else { continue }
                self.meetings = fresh
                await self.refreshProgress()
                let stillWorking = Set(fresh.filter { meetingIsWorking($0.status) }.map(\.id))
                // Only the rows that finished ON THIS TICK: their brief and
                // badges were fetched while the transcript did not exist yet.
                for id in pending.subtracting(stillWorking) {
                    self.metaFetches.remove(id)
                    self.meta[id] = nil
                    self.fetchBrief(for: id)
                }
                pending = stillWorking
                if !stillWorking.isEmpty { ticksWaitingForArrival = 0; continue }
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

    func search(_ query: String) {
        searchTask?.cancel()
        searchTask = Task {
            try? await Task.sleep(nanoseconds: 220_000_000)
            guard !Task.isCancelled else { return }
            await refresh(query: query)
        }
    }

    func fetchBrief(for id: String) {
        guard meta[id] == nil, !metaFetches.contains(id) else { return }
        metaFetches.insert(id)
        Task {
            guard let detail = try? await API.meeting(id) else { return }
            var m = RowMeta()
            m.hasTranscript = detail.turns?.isEmpty == false
            m.hasSummary = detail.summary != nil
            m.hasNotes = detail.notes?.isEmpty == false
            m.hasCaptureWarning = !detail.captureWarnings.isEmpty
            if let s = detail.summary {
                m.brief = s.headline ?? s.tldr.map { String($0.prefix(140)) }
            }
            withAnimation(.easeOut(duration: 0.22)) {
                meta[id] = m
                if let b = m.brief { briefs[id] = b }
            }
        }
    }
}

// MARK: - Root

struct ContentView: View {
    @StateObject private var library = Library()
    @StateObject private var center = RecorderCenter()
    @StateObject private var engine = EngineManager.shared
    @State private var hud = HUDController()
    @State private var route: DetailRoute? = .today
    @State private var mode: PageMode = .document
    @State private var query = ""
    @State private var showOnboarding =
        !UserDefaults.standard.bool(forKey: "ms.onboarded.v1")
        || CommandLine.arguments.contains("--onboarding")

    var body: some View {
        NavigationSplitView {
            SidebarView(library: library, center: center, route: $route)
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
                .animation(Motion.enter, value: engine.state)
        }
        .tint(MS.interactive)
        .environmentObject(library)
        .environmentObject(center)
        .toolbar {
            ToolbarItem(placement: .navigation) {
                HStack(spacing: 7) {
                    Image(systemName: "waveform")
                        .font(.system(size: 13, weight: .bold))
                        .foregroundStyle(MS.playhead)
                    Text("MeetingScribe")
                        .font(.system(size: 14, weight: .semibold))
                        .foregroundStyle(MS.ink)
                }
                .fixedSize()
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
        .searchable(text: $query, placement: .sidebar, prompt: "Search meetings")
        .onChange(of: query) { library.search(query) }
        .onChange(of: engine.state) {
            // The engine just came up (or came back): fetch for real.
            if engine.state == .running {
                Task { await library.refresh() }
            }
        }
        .task {
            hud.attach(center: center)
            center.onRecordingStopped = { [weak library] in
                Task {
                    await library?.refresh()
                    // The meeting is not on disk yet, so that refresh cannot
                    // have seen it. Keep looking until it turns up and lands.
                    library?.expectNewMeeting()
                }
            }
            EngineManager.shared.isRecording = { [weak center] in
                center?.phase == .recording
            }
            // The window the watchdog needs: three misses at four seconds,
            // plus the poll that noticed. A minute covers it with room.
            EngineManager.shared.wasRecordingRecently = { [weak center] in
                center?.wasRecording(within: 60) ?? false
            }
            await EngineManager.shared.ensureRunning()
            await library.refresh()
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
                    .id(id)
            default:
                TodayPage { id in
                    route = .meeting(id)
                    mode = .document
                }
            }
        }
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
                .font(.system(size: 13, weight: .semibold))
                .foregroundStyle(MS.ink2)
                .offset(y: 1)

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
                .font(.system(size: 13, weight: .semibold))
                .foregroundStyle(.black.opacity(0.85))
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
    @State private var pendingDelete: MeetingListItem?
    @State private var deleteError: String?
    @State private var reprocessError: String?

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
                    .font(.caption)
                    .foregroundStyle(MS.ink3)
            }

            ForEach(groups) { group in
                Section {
                    ForEach(group.meetings) { m in
                        MeetingRow(meeting: m,
                                   meta: library.meta[m.id],
                                   progress: library.progress[m.id],
                                   showBrief: group.isToday)
                            .tag(DetailRoute.meeting(m.id))
                            .task { library.fetchBrief(for: m.id) }
                            .contextMenu {
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
                    .font(.system(size: 11))
                    .foregroundStyle(MS.ink2)
                    .textCase(nil)
                }
            }
        }
        .listStyle(.sidebar)
        .onDeleteCommand {
            if case .meeting(let id) = route,
               let m = library.meetings.first(where: { $0.id == id }) {
                pendingDelete = m
            }
        }
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
    }

    /// Hand the meeting's saved audio back to the engine. The refresh is what
    /// starts the library's watcher again, so the row picks up the engine's
    /// progress from the next tick.
    private func reprocess(_ m: MeetingListItem) {
        Task {
            let (ok, err) = await API.reprocess(m.id)
            if ok {
                await library.refresh()
            } else {
                reprocessError = err
            }
        }
    }

    private func deleteMeeting(_ m: MeetingListItem) {
        pendingDelete = nil
        Task {
            let (ok, err) = await API.deleteMeeting(m.id)
            if ok {
                if route == .meeting(m.id) { route = .today }
                await library.refresh()
            } else {
                deleteError = err
            }
        }
    }

    private var liveRow: some View {
        HStack(spacing: 8) {
            Circle().fill(MS.recordRed).frame(width: 7, height: 7)
            Text("Recording…")
                .font(MSFont.chromeMedium)
                .generationShimmer(true)
            Spacer()
            Text(clock(center.elapsed))
                .clockFont(11)
                .foregroundStyle(MS.ink2)
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
                .generationShimmer(processing)

            HStack(spacing: 6) {
                Text(timeText)
                    .font(MSFont.meta)
                    .foregroundStyle(MS.ink2)
                if let meta {
                    HStack(spacing: 4) {
                        if meta.hasTranscript {
                            Image(systemName: "quote.bubble").glyph()
                        }
                        if meta.hasSummary {
                            Image(systemName: "sparkle").glyph()
                        }
                        if meta.hasNotes {
                            Image(systemName: "pencil.line").glyph()
                        }
                        if meta.hasCaptureWarning {
                            Image(systemName: "exclamationmark.triangle")
                                .font(.system(size: 10))
                                .foregroundStyle(MS.ink2)
                                .help("Something about this recording needs a look. Open it to read what.")
                        }
                    }
                }
                Spacer(minLength: 0)
                if failed {
                    Label("Not transcribed", systemImage: "exclamationmark.triangle")
                        .font(.system(size: 11))
                        .foregroundStyle(MS.ink2)
                        .labelStyle(.titleAndIcon)
                } else if processing {
                    Text(meeting.status?.capitalized ?? "Working")
                        .font(.system(size: 11))
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
        self.font(.system(size: 10))
            .foregroundStyle(MS.ink3)
    }
}

// MARK: - Record (toolbar)

/// Recording starts where actions live on a Mac: the toolbar. A quiet
/// capsule — small red dot + "Record" — that becomes the live elapsed
/// clock with a stop square while recording. ⌘R either way.
struct RecordToolbarButton: View {
    @ObservedObject var center: RecorderCenter

    private var recording: Bool { center.phase == .recording }

    var body: some View {
        Button {
            recording ? center.stopRecording() : center.startRecording()
        } label: {
            HStack(spacing: 5.5) {
                if recording {
                    RoundedRectangle(cornerRadius: 2, style: .continuous)
                        .fill(.white)
                        .frame(width: 8, height: 8)
                    Text(clock(center.elapsed))
                        .clockFont(12, weight: .bold)
                } else {
                    Image(systemName: "record.circle")
                        .font(.system(size: 12, weight: .bold))
                    Text("Record")
                        .font(.system(size: 12.5, weight: .bold))
                }
            }
            .foregroundStyle(.white)
            .padding(.horizontal, 3)
            .animation(.easeOut(duration: 0.16), value: recording)
        }
        .buttonStyle(.borderedProminent)
        .tint(MS.recordRed)
        .keyboardShortcut("r", modifiers: .command)
        .disabled(center.phase == .offline)
        .opacity(center.phase == .offline ? 0.4 : 1)
        .help(recording ? "Stop recording (⌘R)" : "Start recording (⌘R)")
    }
}
