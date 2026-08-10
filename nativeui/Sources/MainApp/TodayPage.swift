// The launch surface: a greeting, one stat written as a sentence, what's
// coming up, and today's meetings. The app speaking (SF, not serif), with
// its single rounded-font moment and the only decorative mint in the app.
//
// It is also where a recording begins, so it carries the two things that
// used to be missing there: what this Mac will actually record with, and the
// choices the old web form offered (which meeting, what kind, which language,
// how many people, what you meant to raise). Pressing record still takes one
// click and no decisions.
import SwiftUI
import AppKit

struct TodayPage: View {
    @EnvironmentObject var library: Library
    @EnvironmentObject var center: RecorderCenter
    var onOpen: (String) -> Void

    @State private var events: [DayEvent] = []
    @State private var showAllEvents = false
    /// Non-nil while the setup sheet is open, carrying what it started from.
    @State private var setup: RecordOptions?

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 0) {
                Text(greeting)
                    .font(MSFont.greeting)
                    .foregroundStyle(MS.ink)
                    .padding(.top, 48)

                statSentence
                    .padding(.top, 14)

                if !upcoming.isEmpty {
                    kicker("COMING UP")
                        .padding(.top, 40)
                    VStack(alignment: .leading, spacing: 4) {
                        ForEach(shownEvents) { event in
                            EventRow(event: event) {
                                center.start(options(for: event))
                            } onOptions: {
                                setup = options(for: event)
                            }
                        }
                        if !showAllEvents && upcoming.count > 3 {
                            Button("\(upcoming.count - 3) more") {
                                withAnimation(Motion.enter) { showAllEvents = true }
                            }
                            .buttonStyle(.plain)
                            .font(MSFont.meta)
                            .foregroundStyle(MS.ink3)
                            .padding(.leading, 66)
                        }
                    }
                    .padding(.top, 10)
                }

                StartRecordingRow(center: center) {
                    var fresh = RecordOptions()
                    // Whatever the clock says is happening is only ever the
                    // suggestion; the sheet lets it be changed.
                    if let now = events.first(where: { $0.isNow }) {
                        fresh = options(for: now)
                    }
                    setup = fresh
                }
                .padding(.top, 28)

                kicker("TODAY")
                    .padding(.top, 40)
                if todays.isEmpty {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("No meetings yet today.")
                            .font(MSFont.body)
                            .foregroundStyle(MS.ink2)
                        Text("Recordings you start will show up here.")
                            .font(MSFont.meta)
                            .foregroundStyle(MS.ink3)
                    }
                    .padding(.top, 10)
                } else {
                    VStack(alignment: .leading, spacing: 2) {
                        ForEach(todays) { m in
                            TodayMeetingRow(meeting: m, brief: library.briefs[m.id]) {
                                onOpen(m.id)
                            }
                            .task { library.fetchBrief(for: m.id) }
                        }
                    }
                    .padding(.top, 8)
                }
            }
            .documentMeasure()
            .padding(.bottom, 112)
        }
        .background(alignment: .top) {
            // Aurora's one appearance on a reading surface: the top 200pt
            // of the greeting block, at whisper strength.
            LinearGradient(
                colors: [MS.auroraBlue.opacity(0.10), MS.auroraViolet.opacity(0.05), .clear],
                startPoint: .top, endPoint: .bottom)
                .frame(height: 200)
                .ignoresSafeArea(edges: .top)
        }
        .background(MS.content)
        .sheet(item: $setup) { options in
            NewRecordingSheet(center: center, options: options, events: upcoming) { chosen in
                setup = nil
                center.start(chosen)
            } onCancel: {
                setup = nil
            }
        }
        .task {
            events = await API.calendarStatus().events ?? []
            await center.refreshPreflight(force: true)
        }
    }

    /// Recording THIS event rather than whatever overlaps the clock. The
    /// engine's own auto-pick only ever sees the current time, which is the
    /// wrong meeting when you join early, join late, or run two back to back
    /// — so the title and the attendee count both travel with the choice.
    private func options(for event: DayEvent) -> RecordOptions {
        var o = RecordOptions()
        o.title = event.name
        o.attendees = event.attendees
        return o
    }

    private var upcoming: [DayEvent] {
        events.filter { !$0.isPast }
    }

    private var shownEvents: [DayEvent] {
        showAllEvents ? upcoming : Array(upcoming.prefix(3))
    }

    private var todays: [MeetingListItem] {
        library.meetings.filter {
            guard let d = createdParser.date(from: $0.created) else { return false }
            return Calendar.current.isDateInToday(d)
        }
    }

    private var greeting: String {
        let hour = Calendar.current.component(.hour, from: Date())
        let name = NSFullUserName().split(separator: " ").first.map(String.init) ?? ""
        let part = hour < 5 ? "Up late" : hour < 12 ? "Good morning" :
                   hour < 17 ? "Good afternoon" : "Good evening"
        return name.isEmpty ? "\(part)." : "\(part), \(name)."
    }

    private var statSentence: some View {
        let week = library.meetings.filter {
            guard let d = createdParser.date(from: $0.created) else { return false }
            return d > Date().addingTimeInterval(-7 * 86400)
        }
        let seconds = week.compactMap(\.duration).reduce(0, +)
        let count = week.count
        let h = Int(seconds) / 3600, m = (Int(seconds) % 3600) / 60
        let amount = h > 0 ? "\(h) h \(m) m" : "\(m) m"
        // Interpolated rather than concatenated: `Text + Text` is on its way
        // out, and the interpolating form is the one that stays a single
        // sentence for VoiceOver and for a translator.
        let lead = Text("You've captured ")
            .font(.system(size: 20))
            .foregroundStyle(MS.ink2)
        let figure = Text(amount)
            .font(.system(size: 20, weight: .semibold, design: .rounded))
            .foregroundStyle(MS.interactive)
        let tail = Text(" across \(count) meeting\(count == 1 ? "" : "s") this week.")
            .font(.system(size: 20))
            .foregroundStyle(MS.ink2)
        return Text("\(lead)\(figure)\(tail)")
    }

    private func kicker(_ s: String) -> some View {
        Text(s)
            .font(MSFont.kicker)
            .kerning(0.55)
            .foregroundStyle(MS.ink3)
    }
}

extension RecordOptions: Identifiable {
    /// Identity for the sheet only: presenting one is the act of choosing
    /// these options, so the options themselves are the item.
    var id: String { "\(title)|\(attendees ?? -1)" }
}

/// The Today page's record affordance: a full sentence, not a button —
/// "In a meeting right now? ⏺ Start recording." Quiet until hovered.
///
/// Below it, and only when there is something to say: what the engine refused
/// last time, and what this Mac would fail to record if you pressed it now.
struct StartRecordingRow: View {
    @ObservedObject var center: RecorderCenter
    var onOptions: () -> Void = {}
    @State private var hovering = false

    var body: some View {
        if center.phase == .idle {
            VStack(alignment: .leading, spacing: 12) {
                HStack(spacing: 10) {
                    Button {
                        center.start(RecordOptions())
                    } label: {
                        HStack(spacing: 8) {
                            Circle().fill(MS.recordRed).frame(width: 8, height: 8)
                            Text("In a meeting right now?")
                                .font(MSFont.body)
                                .foregroundStyle(MS.ink2)
                            Text(center.starting ? "Starting…" : "Start recording")
                                .font(.system(size: 15, weight: .semibold))
                                .foregroundStyle(MS.ink)
                                .underline(hovering, color: MS.ink3)
                        }
                        .padding(.vertical, 10)
                        .padding(.horizontal, 14)
                        .background(MS.raised.opacity(hovering ? 1 : 0.65),
                                    in: .rect(cornerRadius: 12))
                        .contentShape(.rect)
                    }
                    .buttonStyle(PressStyle())
                    .disabled(center.starting)
                    .onHover { h in withAnimation(Motion.micro) { hovering = h } }

                    Button("Options…", action: onOptions)
                        .buttonStyle(.plain)
                        .font(MSFont.meta)
                        .foregroundStyle(MS.ink3)
                        .help("Title, meeting type, language, how many people, talking points")
                }

                if let alert = center.alert {
                    NoticeBand(icon: "exclamationmark.triangle",
                               text: alert.headline + ". " + alert.message) {
                        Button("Dismiss") { center.alert = nil }
                            .buttonStyle(.plain)
                            .font(MSFont.meta)
                            .foregroundStyle(MS.ink3)
                    }
                }

                // The preflight answer, before the meeting rather than after
                // it. Silent when there is nothing wrong, which is most days.
                ForEach(center.preflight?.alerts ?? []) { problem in
                    HStack(alignment: .firstTextBaseline, spacing: 7) {
                        Image(systemName: "exclamationmark.triangle")
                            .font(.system(size: 10))
                            .foregroundStyle(MS.ink3)
                        Text(problem.title + ". " + problem.detail)
                            .font(MSFont.meta)
                            .foregroundStyle(MS.ink3)
                            .fixedSize(horizontal: false, vertical: true)
                        // The sentence has always ended by naming a pane in
                        // System Settings. Naming it and leaving the user to
                        // find it is most of a fix; this is the rest.
                        if let pane = SystemSettingsPane.answering(problem) {
                            OpenSystemSettingsButton(pane: pane)
                        }
                    }
                }
            }
            .animation(Motion.enter, value: center.alert)
        }
    }
}

// MARK: - The pane that answers a warning

/// Where in System Settings a capture problem is actually fixed.
///
/// Every one of these sentences already ends by naming a pane, and until now
/// that was the whole of the help: the user was told to go to System Settings
/// ▸ Privacy & Security ▸ Microphone and left to find it. Onboarding has
/// opened these panes directly since it was written, so the mechanics existed
/// two files away from the warnings that needed them.
///
/// `CaptureAlert` carries no kind, only an id and two sentences, and the
/// detail for a routing problem is free text written by the engine
/// (audio_recorder.py's `_routing_alert`). So the match is made on both: the
/// id says which side of the capture is in trouble, and the sentence has to
/// genuinely send the user to System Settings before a button appears. An
/// alert that names no pane keeps its sentence and gains no button, which is
/// better than a button that opens the wrong pane — the "sound output is set
/// to the loopback device" warning is fixed in Sound, not in Privacy, and it
/// says so itself without naming System Settings.
enum SystemSettingsPane {
    case microphone
    case screenAndSystemAudio

    /// The same URLs the onboarding flow opens, so there is one answer to
    /// "which pane is that" in this app rather than two that can drift.
    var url: URL {
        switch self {
        case .microphone:
            return URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone")!
        case .screenAndSystemAudio:
            return URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture")!
        }
    }

    /// The path as macOS itself writes it, for the tooltip.
    var written: String {
        switch self {
        case .microphone:
            return "Privacy & Security → Microphone"
        case .screenAndSystemAudio:
            return "Privacy & Security → Screen & System Audio Recording"
        }
    }

    /// The same path spelled out, for VoiceOver, where "→" is not a word and
    /// "&" is read unevenly.
    var spoken: String {
        switch self {
        case .microphone:
            return "Privacy and Security, Microphone"
        case .screenAndSystemAudio:
            return "Privacy and Security, Screen and System Audio Recording"
        }
    }

    static func answering(_ alert: CaptureAlert) -> SystemSettingsPane? {
        guard (alert.title + ". " + alert.detail).contains("System Settings") else { return nil }
        switch alert.id {
        case "mic": return .microphone
        case "system": return .screenAndSystemAudio
        default: return nil          // disk, and anything added later
        }
    }
}

/// The fix, one click away, and quiet about it: this sits beside a warning
/// that is already the loudest thing on the page, so it borrows the mint the
/// app spends on interaction and nothing else.
private struct OpenSystemSettingsButton: View {
    let pane: SystemSettingsPane

    var body: some View {
        Button("Open System Settings") {
            NSWorkspace.shared.open(pane.url)
        }
        .buttonStyle(.plain)
        .font(MSFont.meta)
        .foregroundStyle(MS.interactive)
        .fixedSize()
        .help("Opens System Settings → \(pane.written)")
        .accessibilityLabel("Open System Settings to \(pane.spoken)")
    }
}

private struct EventRow: View {
    let event: DayEvent
    let onRecord: () -> Void
    let onOptions: () -> Void
    @State private var hovering = false
    /// True while either of the row's controls holds keyboard focus, so
    /// tabbing to one reveals both.
    @FocusState private var focused: Bool

    var body: some View {
        HStack(spacing: 10) {
            Text(startTime)
                .clockFont(12)
                .foregroundStyle(event.isNow ? MS.ink : MS.ink2)
                .frame(width: 56, alignment: .trailing)
            Text(event.name)
                .font(.system(size: 15, weight: .medium))
                .foregroundStyle(MS.ink)
                .lineLimit(1)
            if let n = event.attendees, n > 0 {
                HStack(spacing: 3) {
                    Image(systemName: "person.2")
                        .font(.system(size: 9))
                    Text("\(n)")
                        .font(MSFont.meta)
                }
                .foregroundStyle(MS.ink4)
                .help("\(n) guest\(n == 1 ? "" : "s") on the invitation, not counting you")
            }
            Spacer()
            // Faded out rather than removed. `if hovering` took both controls
            // out of the view tree entirely, which means a keyboard could
            // never tab to them and VoiceOver could never find them: the two
            // ways of recording a specific meeting existed only for a mouse.
            HStack(spacing: 8) {
                Button("Record this one", action: onRecord)
                    .buttonStyle(PressStyle())
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(MS.interactive)
                    .padding(.horizontal, 10)
                    .padding(.vertical, 4)
                    .background(MS.raised, in: .capsule)
                    .focused($focused)
                Button("Set up this recording", systemImage: "slider.horizontal.3",
                       action: onOptions)
                    .labelStyle(.iconOnly)
                    .buttonStyle(PressStyle())
                    .font(.system(size: 11))
                    .foregroundStyle(MS.ink3)
                    .help("Set up this recording first")
                    .focused($focused)
            }
            .opacity(hovering || focused ? 1 : 0)
            .msAnimation(Motion.micro, value: focused)
        }
        .padding(.vertical, 6)
        .contentShape(.rect)
        .onHover { h in withAnimation(Motion.micro) { hovering = h } }
    }

    private var startTime: String {
        guard let date = event.startDate else { return "" }
        return event.isNow ? "now" : date.formatted(.dateTime.hour().minute())
    }
}

// MARK: - Setting up a recording

/// Everything the old web form asked, in the native idiom and none of it
/// compulsory: which meeting this is, whether everyone is in the room, the
/// language, how many voices to expect, what you meant to raise, and what
/// this Mac will record with. Opened on request; skipped by everyone who
/// just wants to press record.
struct NewRecordingSheet: View {
    @ObservedObject var center: RecorderCenter
    @State var options: RecordOptions
    let events: [DayEvent]
    let onStart: (RecordOptions) -> Void
    let onCancel: () -> Void

    @State private var cues = ""
    @State private var cuesLoaded = false
    @State private var saving = false

    private static let languages: [(String, String)] = [
        ("auto", "Auto-detect"), ("en", "English"), ("hi", "Hindi"),
        ("es", "Spanish"), ("fr", "French"), ("de", "German"), ("it", "Italian"),
        ("pt", "Portuguese"), ("ja", "Japanese"), ("ko", "Korean"),
        ("zh", "Chinese"), ("ar", "Arabic"), ("ru", "Russian"),
    ]

    var body: some View {
        VStack(spacing: 0) {
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    header
                    titleField
                    typeField
                    languageField
                    speakersField
                    cuesField
                    devicesField
                }
                .padding(.horizontal, 34)
                .padding(.top, 30)
                .padding(.bottom, 26)
            }

            Divider().overlay(MS.hairline)

            HStack(spacing: 12) {
                Button("Cancel", action: onCancel)
                    .buttonStyle(.plain)
                    .font(MSFont.chrome)
                    .foregroundStyle(MS.ink2)
                    .keyboardShortcut(.cancelAction)
                Spacer()
                Button {
                    saving = true
                    Task {
                        // The engine freezes the cue list into the meeting as
                        // it starts, so they have to land first.
                        await API.saveCues(cueList)
                        saving = false
                        onStart(options)
                    }
                } label: {
                    Text(saving ? "Starting…" : "Start recording")
                        .font(.system(size: 13, weight: .semibold))
                        .foregroundStyle(.black.opacity(0.85))
                        .padding(.horizontal, 20)
                        .padding(.vertical, 8)
                }
                .buttonStyle(PressStyle())
                .background(MS.playheadFill, in: .capsule)
                .disabled(saving)
                .keyboardShortcut(.defaultAction)
            }
            .padding(.horizontal, 34)
            .padding(.vertical, 16)
        }
        .frame(width: 560, height: 620)
        .background(MS.content)
        .task {
            await center.refreshPreflight(force: true)
            if !cuesLoaded, let existing = await API.cues() {
                cues = existing.joined(separator: "\n")
                cuesLoaded = true
            }
        }
    }

    // MARK: Fields

    private var header: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("NEW RECORDING")
                .font(MSFont.kicker)
                .kerning(0.55)
                .foregroundStyle(MS.ink3)
            Text("Set up this meeting.")
                .font(.system(size: 24, weight: .semibold, design: .serif))
                .foregroundStyle(MS.ink)
        }
    }

    private var titleField: some View {
        Field("Meeting title") {
            TextField("Named after your calendar", text: $options.title)
                .textFieldStyle(.plain)
                .font(MSFont.body)
                .foregroundStyle(MS.ink)
                .padding(.horizontal, 12)
                .padding(.vertical, 9)
                .background(MS.raised, in: .rect(cornerRadius: 9))
                .overlay {
                    RoundedRectangle(cornerRadius: 9).stroke(MS.hairlineStrong, lineWidth: 1)
                }
            if !events.isEmpty {
                FlowRow(spacing: 6) {
                    ForEach(events.prefix(4)) { event in
                        Button {
                            options.title = event.name
                            options.attendees = event.attendees
                        } label: {
                            HStack(spacing: 5) {
                                Image(systemName: "calendar")
                                    .font(.system(size: 9))
                                Text(event.name)
                                    .font(MSFont.meta)
                                    .lineLimit(1)
                            }
                            .foregroundStyle(options.title == event.name ? MS.ink : MS.ink2)
                            .padding(.horizontal, 9)
                            .padding(.vertical, 4)
                            .background(MS.raised, in: .capsule)
                            .overlay {
                                Capsule().stroke(
                                    options.title == event.name ? MS.hairlineStrong : MS.hairline,
                                    lineWidth: 1)
                            }
                        }
                        .buttonStyle(PressStyle())
                    }
                }
            }
            hint("Left blank, the name comes from your calendar, read on this Mac and never from a cloud API.")
        }
    }

    private var typeField: some View {
        Field("Meeting type") {
            HStack(spacing: 6) {
                segment("Online call", icon: "display", on: !options.inPerson) {
                    options.inPerson = false
                }
                segment("In person", icon: "person.wave.2", on: options.inPerson) {
                    options.inPerson = true
                }
            }
            hint(options.inPerson
                 ? "Everyone shares this Mac's microphone and the voices are told apart automatically. Place the laptop near the centre of the table."
                 : "Your mic is recorded as “You”, and the meeting audio (Zoom, Meet, Teams, anything that makes sound) becomes the other speakers. Headphones recommended.")
        }
    }

    private var languageField: some View {
        Field("Language") {
            Picker("", selection: $options.language) {
                ForEach(Self.languages, id: \.0) { code, name in
                    Text(name).tag(code)
                }
            }
            .labelsHidden()
            .pickerStyle(.menu)
            .frame(width: 200, alignment: .leading)
            hint("Locks speech recognition to one language, which is more accurate than letting it guess.")
        }
    }

    private var speakersField: some View {
        Field("How many voices") {
            Picker("", selection: $options.expectedSpeakers) {
                Text("Work it out").tag(Int?.none)
                ForEach(1...8, id: \.self) { n in
                    Text(n == 1 ? "1 voice" : "\(n) voices").tag(Int?.some(n))
                }
            }
            .labelsHidden()
            .pickerStyle(.menu)
            .frame(width: 200, alignment: .leading)
            hint(speakersHint)
        }
    }

    private var speakersHint: String {
        if let n = options.expectedSpeakers {
            return options.inPerson
                ? "\(n) \(n == 1 ? "person" : "people") around this Mac, you included."
                : "\(n) other \(n == 1 ? "speaker" : "speakers") on the call, not counting you."
        }
        if let attendees = options.attendees, attendees > 0, let resolved = options.resolvedSpeakers {
            return "From the invitation: \(attendees) guest\(attendees == 1 ? "" : "s"), so \(resolved) "
                + (options.inPerson ? "\(resolved == 1 ? "person" : "people") in the room."
                                    : "other \(resolved == 1 ? "speaker" : "speakers") on the call.")
        }
        return "Left alone, the number comes from the calendar invitation, and from the recording itself when there isn't one."
    }

    private var cuesField: some View {
        Field("Talking points and questions") {
            TextEditor(text: $cues)
                .font(MSFont.body)
                .foregroundStyle(MS.ink)
                .scrollContentBackground(.hidden)
                .frame(height: 78)
                .padding(.horizontal, 8)
                .padding(.vertical, 6)
                .background(MS.raised, in: .rect(cornerRadius: 9))
                .overlay {
                    RoundedRectangle(cornerRadius: 9).stroke(MS.hairlineStrong, lineWidth: 1)
                }
                .overlay(alignment: .topLeading) {
                    if cues.isEmpty {
                        Text("One per line. Ask where we landed on pricing. Raise the October timeline.")
                            .font(MSFont.body)
                            .foregroundStyle(MS.ink4)
                            .padding(.horizontal, 13)
                            .padding(.vertical, 12)
                            .allowsHitTesting(false)
                    }
                }
            hint("Kept until you clear them, and copied into the meeting when you start, so the summary can flag anything you never got to.")
        }
    }

    private var devicesField: some View {
        Field("What this Mac will record") {
            VStack(alignment: .leading, spacing: 8) {
                deviceRow(name: center.preflight?.mic?.name ?? "No microphone found",
                          role: "you",
                          ok: center.preflight?.mic != nil)
                deviceRow(name: center.preflight?.system?.name ?? "No system audio available",
                          role: systemRole,
                          ok: center.preflight?.system != nil
                              && center.preflight?.system?.routing_alert == nil)
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 10)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(MS.raised, in: .rect(cornerRadius: 10))
            .overlay {
                RoundedRectangle(cornerRadius: 10).stroke(MS.hairline, lineWidth: 1)
            }
            ForEach(center.preflight?.alerts ?? []) { problem in
                NoticeBand(icon: "exclamationmark.triangle",
                           text: problem.title + ". " + problem.detail) {
                    if let pane = SystemSettingsPane.answering(problem) {
                        OpenSystemSettingsButton(pane: pane)
                    }
                }
            }
            if center.preflight == nil {
                hint("Checking the audio devices…")
            }
        }
    }

    private var systemRole: String {
        guard let system = center.preflight?.system else {
            return "the other participants would be lost"
        }
        switch system.routing {
        case "tap_ready": return "the other participants"
        case "tap_undetermined":
            return "the other participants, macOS asks once when you start"
        case "routed": return "the other participants, routed"
        case "not_routed":
            return system.auto_route == false
                ? "the other participants, routing is off"
                : "the other participants, routes when you start"
        default: return "the other participants"
        }
    }

    private func deviceRow(name: String, role: String, ok: Bool) -> some View {
        HStack(spacing: 9) {
            Circle()
                .fill(ok ? MS.playheadFill : MS.ink4)
                .frame(width: 6, height: 6)
            Text(name)
                .font(MSFont.chrome)
                .foregroundStyle(MS.ink)
                .lineLimit(1)
            Spacer(minLength: 8)
            Text(role)
                .font(MSFont.meta)
                .foregroundStyle(MS.ink3)
                .lineLimit(1)
        }
    }

    // MARK: Pieces

    private var cueList: [String] {
        cues.split(separator: "\n")
            .map { $0.trimmingCharacters(in: .whitespaces) }
            .filter { !$0.isEmpty }
    }

    private func segment(_ label: String, icon: String, on: Bool,
                         action: @escaping () -> Void) -> some View {
        Button(action: action) {
            HStack(spacing: 6) {
                Image(systemName: icon).font(.system(size: 11))
                Text(label).font(MSFont.chromeMedium)
            }
            .foregroundStyle(on ? MS.ink : MS.ink2)
            .padding(.horizontal, 14)
            .padding(.vertical, 7)
            .background(on ? MS.raised : MS.raised.opacity(0.4), in: .capsule)
            .overlay {
                Capsule().stroke(on ? MS.hairlineStrong : MS.hairline, lineWidth: 1)
            }
        }
        .buttonStyle(PressStyle())
    }

    private func hint(_ text: String) -> some View {
        Text(text)
            .font(MSFont.meta)
            .foregroundStyle(MS.ink3)
            .lineSpacing(3)
            .fixedSize(horizontal: false, vertical: true)
    }

    private struct Field<Content: View>: View {
        let label: String
        @ViewBuilder let content: Content

        init(_ label: String, @ViewBuilder content: () -> Content) {
            self.label = label
            self.content = content()
        }

        var body: some View {
            VStack(alignment: .leading, spacing: 8) {
                Text(label.uppercased())
                    .font(MSFont.kicker)
                    .kerning(0.55)
                    .foregroundStyle(MS.ink3)
                content
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }
}

/// Chips that wrap rather than overflow. Native `Layout`, no dependencies.
private struct FlowRow: Layout {
    var spacing: CGFloat = 6

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let maxWidth = proposal.width ?? .infinity
        var x: CGFloat = 0, y: CGFloat = 0, lineHeight: CGFloat = 0
        for view in subviews {
            let size = view.sizeThatFits(.unspecified)
            if x > 0, x + size.width > maxWidth {
                x = 0
                y += lineHeight + spacing
                lineHeight = 0
            }
            x += size.width + spacing
            lineHeight = max(lineHeight, size.height)
        }
        return CGSize(width: maxWidth == .infinity ? x : maxWidth, height: y + lineHeight)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize,
                       subviews: Subviews, cache: inout ()) {
        var x = bounds.minX, y = bounds.minY, lineHeight: CGFloat = 0
        for view in subviews {
            let size = view.sizeThatFits(.unspecified)
            if x > bounds.minX, x + size.width > bounds.maxX {
                x = bounds.minX
                y += lineHeight + spacing
                lineHeight = 0
            }
            view.place(at: CGPoint(x: x, y: y), proposal: ProposedViewSize(size))
            x += size.width + spacing
            lineHeight = max(lineHeight, size.height)
        }
    }
}

private struct TodayMeetingRow: View {
    let meeting: MeetingListItem
    var brief: String?
    let onOpen: () -> Void
    @State private var hovering = false

    var body: some View {
        Button(action: onOpen) {
            VStack(alignment: .leading, spacing: 3) {
                HStack {
                    Text(meeting.title)
                        .font(.system(size: 15, weight: .medium))
                        .foregroundStyle(MS.ink)
                        .lineLimit(1)
                    Spacer()
                    if let d = meeting.duration {
                        Text(clock(d))
                            .clockFont(12)
                            .foregroundStyle(MS.ink3)
                    }
                }
                if let brief, !brief.isEmpty {
                    Text(brief)
                        .font(MSFont.meta)
                        .foregroundStyle(MS.ink3)
                        .lineLimit(1)
                        .transition(.offset(y: 3).combined(with: .opacity))
                }
            }
            .padding(.vertical, 8)
            .padding(.horizontal, 10)
            .background {
                if hovering {
                    RoundedRectangle(cornerRadius: 8).fill(MS.ink.opacity(0.05))
                }
            }
            .contentShape(.rect)
        }
        .buttonStyle(.plain)
        .onHover { h in withAnimation(Motion.micro) { hovering = h } }
    }
}
