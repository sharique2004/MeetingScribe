// The talk-time ribbon: one quiet lane per speaker showing WHEN they spoke
// across the meeting, with share and minutes at the right. This popover is
// the entire replacement for the old Speakers tab.
import SwiftUI

struct SpeakerRibbon: View {
    let detail: MeetingDetail

    private struct Lane: Identifiable {
        let id: String
        let name: String
        let color: Color
        let spans: [(Double, Double)]   // fraction of meeting: (start, width)
        let share: Double
        let seconds: Double
    }

    private var lanes: [Lane] {
        let duration = max(detail.duration ?? 1, 1)
        let turns = detail.turns ?? []
        let stats = detail.stats?.per_speaker ?? [:]
        let keys = (detail.speakers ?? [:]).keys.sorted { a, b in
            (stats[a]?.seconds ?? 0) > (stats[b]?.seconds ?? 0)
        }
        return keys.map { key in
            let spans = turns.filter { $0.speaker == key }.map { turn -> (Double, Double) in
                let start = turn.start / duration
                let width = max(((turn.end ?? turn.start + 2) - turn.start) / duration, 0.003)
                return (start, width)
            }
            return Lane(
                id: key,
                name: detail.speakers?[key] ?? key,
                color: MS.speaker(key),
                spans: spans,
                share: stats[key]?.share ?? 0,
                seconds: stats[key]?.seconds ?? 0)
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("WHO SPOKE WHEN")
                .font(MSFont.kicker)
                .kerning(0.55)
                .foregroundStyle(MS.ink3)
            ForEach(lanes) { lane in
                HStack(spacing: 10) {
                    HStack(spacing: 5) {
                        Circle().fill(lane.color).frame(width: 6, height: 6)
                        Text(lane.name)
                            .font(.system(size: 12, weight: .medium))
                            .foregroundStyle(MS.ink)
                            .lineLimit(1)
                    }
                    .frame(width: 92, alignment: .leading)

                    GeometryReader { geo in
                        ZStack(alignment: .leading) {
                            Capsule().fill(MS.ink4.opacity(0.25))
                            ForEach(Array(lane.spans.enumerated()), id: \.offset) { _, span in
                                Capsule()
                                    .fill(lane.color)
                                    .frame(width: max(geo.size.width * span.1, 2))
                                    .offset(x: geo.size.width * span.0)
                            }
                        }
                    }
                    .frame(height: 6)

                    Text("\(Int(lane.share * 100))% · \(Int(lane.seconds / 60)) min")
                        .clockFont(11)
                        .foregroundStyle(MS.ink2)
                        .frame(width: 78, alignment: .trailing)
                }
            }
        }
    }
}
