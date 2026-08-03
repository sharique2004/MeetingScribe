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
    }
}

final class MainAppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        NSApp.activate()
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if MainActor.assumeIsolated({ !EngineManager.shared.prepareForQuit() }) {
            let alert = NSAlert()
            alert.messageText = "A recording is running"
            alert.informativeText = "Stop the recording before quitting so the meeting is saved."
            alert.runModal()
            return .terminateCancel
        }
        return .terminateNow
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
}

@MainActor
final class Library: ObservableObject {
    @Published var meetings: [MeetingListItem] = []
    @Published var loadError: String?
    @Published var meta: [String: RowMeta] = [:]
    @Published var briefs: [String: String] = [:]
    private var metaFetches = Set<String>()
    private var searchTask: Task<Void, Never>?

    func refresh(query: String = "") async {
        do {
            meetings = try await API.meetings(query: query)
            loadError = nil
        } catch {
            loadError = "The engine isn't answering. Nothing is lost — it just isn't listening yet."
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
        }
        .tint(MS.interactive)
        .environmentObject(library)
        .environmentObject(center)
        .toolbar {
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
                Task { await library?.refresh() }
            }
            EngineManager.shared.isRecording = { [weak center] in
                center?.phase == .recording
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
                                   showBrief: group.isToday)
                            .tag(DetailRoute.meeting(m.id))
                            .task { library.fetchBrief(for: m.id) }
                            .contextMenu {
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
    var showBrief: Bool

    private var processing: Bool {
        (meeting.status ?? "done") != "done"
    }

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
                    }
                }
                Spacer(minLength: 0)
                if processing {
                    Text(meeting.status?.capitalized ?? "Working")
                        .font(.system(size: 11))
                        .foregroundStyle(MS.ink2)
                } else if let d = meeting.duration, d > 0 {
                    Text(clock(d))
                        .clockFont(12)
                        .foregroundStyle(MS.ink2)
                }
            }

            if showBrief, let brief = meta?.brief, !brief.isEmpty {
                Text(brief)
                    .font(MSFont.meta)
                    .foregroundStyle(MS.ink3)
                    .lineLimit(1)
                    .transition(.offset(y: 3).combined(with: .opacity))
            }
        }
        .padding(.vertical, 4)
        .help(!showBrief ? (meta?.brief ?? "") : "")
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
