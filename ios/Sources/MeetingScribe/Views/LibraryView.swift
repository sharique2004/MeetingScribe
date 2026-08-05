// The library: every meeting this phone has recorded, newest first.
//
// A NavigationStack rather than the Mac's NavigationSplitView. Same content,
// but a phone is one column and forcing a sidebar onto it is how a Mac app
// ends up feeling like a Mac app running on a phone.
import SwiftUI

struct LibraryView: View {
    @Environment(MeetingStore.self) private var store
    @State private var recording = false

    var body: some View {
        NavigationStack {
            Group {
                if store.meetings.isEmpty {
                    emptyState
                } else {
                    list
                }
            }
            .background(MS.content)
            .navigationTitle("Meetings")
            .toolbar {
                ToolbarItem(placement: .primaryAction) {
                    Button("Record a meeting", systemImage: "record.circle") {
                        recording = true
                    }
                    .tint(MS.recordRed)
                }
            }
            .fullScreenCover(isPresented: $recording) {
                RecordingView()
                    .environment(store)
            }
        }
    }

    private var list: some View {
        List {
            ForEach(store.meetings) { meeting in
                NavigationLink(value: meeting) {
                    MeetingRow(meeting: meeting)
                }
            }
            .onDelete { offsets in
                for index in offsets { store.delete(store.meetings[index]) }
            }
        }
        .listStyle(.plain)
        .navigationDestination(for: Meeting.self) { meeting in
            MeetingDetailView(meeting: meeting)
                .environment(store)
        }
    }

    private var emptyState: some View {
        ContentUnavailableView {
            Label("No meetings yet", systemImage: "waveform")
        } description: {
            Text("Record the room and this iPhone will transcribe and summarise it, entirely on device.")
        } actions: {
            Button("Record a meeting", systemImage: "record.circle") {
                recording = true
            }
            .buttonStyle(.borderedProminent)
            .tint(MS.recordRed)
        }
    }
}

private struct MeetingRow: View {
    let meeting: Meeting

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(meeting.title)
                .font(MSFont.chromeMedium)
                .foregroundStyle(MS.ink)
                .lineLimit(1)
            if let brief = meeting.summary?.headline, !brief.isEmpty {
                Text(brief)
                    .font(MSFont.meta)
                    .foregroundStyle(MS.ink2)
                    .lineLimit(2)
            }
            HStack(spacing: 6) {
                Text(meeting.created, format: .dateTime.hour().minute())
                Text("·")
                Text(clock(meeting.duration))
                if meeting.hasTranscript {
                    Image(systemName: "quote.bubble")
                        .accessibilityLabel("Transcribed")
                }
                if meeting.summary != nil {
                    Image(systemName: "sparkle")
                        .accessibilityLabel("Summarised")
                }
                if meeting.warning != nil {
                    Image(systemName: "exclamationmark.triangle")
                        .accessibilityLabel("Needs a look")
                }
            }
            .font(MSFont.meta)
            .foregroundStyle(MS.ink3)
        }
        .padding(.vertical, 3)
    }
}

func clock(_ seconds: Double) -> String {
    let total = Int(seconds.rounded())
    return String(format: "%d:%02d", total / 60, total % 60)
}
