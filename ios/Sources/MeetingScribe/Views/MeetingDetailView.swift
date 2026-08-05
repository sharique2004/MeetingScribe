// One meeting, read on a phone: the summary first, the transcript under it.
//
// Deliberately the Mac's reading order — headline, key points, next steps,
// then the words — because the two apps are one product and the shape of a
// meeting should not change with the screen it is read on.
import SwiftUI

struct MeetingDetailView: View {
    @Environment(MeetingStore.self) private var store
    let meeting: Meeting

    /// Read back from the store so a summary written after this view opened
    /// appears without the reader having to leave and come back.
    private var current: Meeting {
        store.meetings.first { $0.id == meeting.id } ?? meeting
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 22) {
                head
                if let warning = current.warning {
                    noticeBand(warning)
                }
                if let summary = current.summary {
                    summaryBody(summary)
                }
                if current.hasTranscript {
                    transcript
                } else if current.warning == nil {
                    Text("No transcript for this meeting.")
                        .font(MSFont.body)
                        .foregroundStyle(MS.ink2)
                }
            }
            .padding(20)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .background(MS.content)
        .navigationTitle(current.title)
        .navigationBarTitleDisplayMode(.inline)
    }

    private var head: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(current.created, format: .dateTime.weekday(.wide).day().month(.wide).hour().minute())
                .font(MSFont.meta)
                .foregroundStyle(MS.ink3)
            Text(current.title)
                .font(MSFont.pageTitle)
                .foregroundStyle(MS.ink)
            Text("\(clock(current.duration)) · \(current.wordCount.formatted()) words · on this iPhone")
                .font(MSFont.meta)
                .foregroundStyle(MS.ink3)
        }
    }

    private func noticeBand(_ text: String) -> some View {
        HStack(alignment: .top, spacing: 9) {
            Image(systemName: "exclamationmark.triangle")
                .foregroundStyle(MS.ink2)
            Text(text)
                .font(MSFont.meta)
                .foregroundStyle(MS.ink2)
        }
        .padding(13)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(MS.raised, in: .rect(cornerRadius: 12))
    }

    private func summaryBody(_ summary: MeetingSummary) -> some View {
        VStack(alignment: .leading, spacing: 18) {
            if let headline = summary.headline, !headline.isEmpty {
                Text(headline)
                    .font(MSFont.lead)
                    .foregroundStyle(MS.ink)
            }
            if !summary.keyPoints.isEmpty {
                section("OVERVIEW", items: summary.keyPoints)
            }
            if !summary.actionItems.isEmpty {
                section("NEXT STEPS", items: summary.actionItems)
            }
        }
    }

    private func section(_ title: String, items: [String]) -> some View {
        VStack(alignment: .leading, spacing: 9) {
            Text(title)
                .font(MSFont.kicker)
                .kerning(0.55)
                .foregroundStyle(MS.ink3)
                .accessibilityAddTraits(.isHeader)
            ForEach(Array(items.enumerated()), id: \.offset) { _, item in
                HStack(alignment: .top, spacing: 8) {
                    Circle().fill(MS.ink4).frame(width: 4, height: 4)
                        .padding(.top, 8)
                    Text(item)
                        .font(MSFont.body)
                        .foregroundStyle(MS.ink2)
                }
            }
        }
    }

    private var transcript: some View {
        VStack(alignment: .leading, spacing: 9) {
            Text("TRANSCRIPT")
                .font(MSFont.kicker)
                .kerning(0.55)
                .foregroundStyle(MS.ink3)
                .accessibilityAddTraits(.isHeader)
            ForEach(current.turns) { turn in
                VStack(alignment: .leading, spacing: 2) {
                    Text(clock(turn.start))
                        .font(MSFont.meta)
                        .monospacedDigit()
                        .foregroundStyle(MS.playhead)
                    Text(turn.text)
                        .font(MSFont.body)
                        .foregroundStyle(MS.ink)
                        .textSelection(.enabled)
                }
            }
        }
    }
}
