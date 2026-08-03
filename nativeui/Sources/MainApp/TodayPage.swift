// The launch surface: a greeting, one stat written as a sentence, what's
// coming up, and today's meetings. The app speaking (SF, not serif), with
// its single rounded-font moment and the only decorative mint in the app.
import SwiftUI

struct TodayPage: View {
    @EnvironmentObject var library: Library
    @EnvironmentObject var center: RecorderCenter
    var onOpen: (String) -> Void

    @State private var events: [CalendarEvent] = []
    @State private var showAllEvents = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 0) {
                Text(greeting)
                    .font(MSFont.greeting)
                    .foregroundStyle(MS.ink)
                    .padding(.top, 48)

                statSentence
                    .padding(.top, 14)

                if !events.isEmpty {
                    kicker("COMING UP")
                        .padding(.top, 40)
                    VStack(alignment: .leading, spacing: 4) {
                        ForEach(Array(shownEvents.enumerated()), id: \.offset) { _, event in
                            EventRow(event: event) {
                                center.startRecording(title: event.title)
                            }
                        }
                        if !showAllEvents && events.count > 3 {
                            Button("\(events.count - 3) more") {
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

                StartRecordingRow(center: center)
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
        .task { events = await API.calendarToday() }
    }

    private var shownEvents: [CalendarEvent] {
        showAllEvents ? events : Array(events.prefix(3))
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
        return (
            Text("You've captured ")
                .font(.system(size: 20))
                .foregroundColor(MS.ink2)
            + Text(amount)
                .font(.system(size: 20, weight: .semibold, design: .rounded))
                .foregroundColor(MS.interactive)
            + Text(" across \(count) meeting\(count == 1 ? "" : "s") this week.")
                .font(.system(size: 20))
                .foregroundColor(MS.ink2)
        )
    }

    private func kicker(_ s: String) -> some View {
        Text(s)
            .font(MSFont.kicker)
            .kerning(0.55)
            .foregroundStyle(MS.ink3)
    }
}

/// The Today page's record affordance: a full sentence, not a button —
/// "In a meeting right now? ⏺ Start recording." Quiet until hovered.
struct StartRecordingRow: View {
    @ObservedObject var center: RecorderCenter
    @State private var hovering = false

    var body: some View {
        if center.phase == .idle {
            Button {
                center.startRecording()
            } label: {
                HStack(spacing: 8) {
                    Circle().fill(MS.recordRed).frame(width: 8, height: 8)
                    Text("In a meeting right now?")
                        .font(MSFont.body)
                        .foregroundStyle(MS.ink2)
                    Text("Start recording")
                        .font(.system(size: 15, weight: .semibold))
                        .foregroundStyle(MS.ink)
                        .underline(hovering, color: MS.ink3)
                }
                .padding(.vertical, 10)
                .padding(.horizontal, 14)
                .background(MS.raised.opacity(hovering ? 1 : 0.65),
                            in: .rect(cornerRadius: 12))
                .contentShape(Rectangle())
            }
            .buttonStyle(PressStyle())
            .onHover { h in withAnimation(Motion.micro) { hovering = h } }
        }
    }
}

private struct EventRow: View {
    let event: CalendarEvent
    let onRecord: () -> Void
    @State private var hovering = false

    var body: some View {
        HStack(spacing: 10) {
            Text(startTime)
                .clockFont(12)
                .foregroundStyle(MS.ink2)
                .frame(width: 56, alignment: .trailing)
            Text(event.title ?? "Untitled event")
                .font(.system(size: 15, weight: .medium))
                .foregroundStyle(MS.ink)
                .lineLimit(1)
            Spacer()
            if hovering {
                Button("Record", action: onRecord)
                    .buttonStyle(PressStyle())
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(MS.interactive)
                    .padding(.horizontal, 10)
                    .padding(.vertical, 4)
                    .background(MS.raised, in: .capsule)
                    .transition(.opacity)
            }
        }
        .padding(.vertical, 6)
        .contentShape(Rectangle())
        .onHover { h in withAnimation(Motion.micro) { hovering = h } }
    }

    private var startTime: String {
        guard let start = event.start else { return "" }
        if let date = ISO8601DateFormatter().date(from: start) ?? createdParser.date(from: start) {
            return date.formatted(.dateTime.hour().minute())
        }
        return String(start.suffix(5))
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
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .onHover { h in withAnimation(Motion.micro) { hovering = h } }
    }
}
