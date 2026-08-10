// The Voice Memos-style waveform scrubber: the engine's precomputed peak
// bins rendered as rounded bars in a Canvas, played portion tinted with the
// theme accent, draggable anywhere to scrub. Bars re-bucket to the view's
// width so the drawing stays crisp at any size.
import SwiftUI

struct WaveformScrubber: View {
    let peaks: [Double]
    let duration: Double
    let time: Double
    let accent: Color
    let onScrub: (Double) -> Void

    @State private var dragTime: Double?

    private let barWidth: CGFloat = 2
    private let gap: CGFloat = 1.5

    var body: some View {
        GeometryReader { geo in
            let bars = rebucket(to: Int(geo.size.width / (barWidth + gap)))
            let shownTime = dragTime ?? time
            let fraction = duration > 0 ? min(max(shownTime / duration, 0), 1) : 0

            Canvas { ctx, size in
                let midY = size.height / 2
                let playedX = size.width * fraction
                for (i, peak) in bars.enumerated() {
                    let x = CGFloat(i) * (barWidth + gap)
                    let h = max(2.5, CGFloat(peak) * (size.height - 4))
                    let rect = CGRect(x: x, y: midY - h / 2, width: barWidth, height: h)
                    let path = Path(roundedRect: rect, cornerRadius: barWidth / 2)
                    let played = x + barWidth <= playedX
                    let partial = !played && x < playedX
                    if played {
                        ctx.fill(path, with: .color(accent))
                    } else if partial {
                        // The bar under the playhead: split fill for a crisp edge.
                        ctx.fill(path, with: .color(accent.opacity(0.55)))
                    } else {
                        // The unplayed track: palette ink rather than
                        // `.primary`, so Increase Contrast reaches it too.
                        ctx.fill(path, with: .color(MS.ink4.opacity(0.55)))
                    }
                }
                // Playhead needle.
                if fraction > 0 {
                    let needle = Path(CGRect(x: playedX - 0.5, y: 2, width: 1, height: size.height - 4))
                    ctx.fill(needle, with: .color(accent.opacity(0.9)))
                }
            }
            // The bars melt into the shelf instead of ending in a box: a
            // scrim in the surface the transport's glass sits over, plus the
            // end-fade that keeps the first and last bar from reading as a
            // hard edge. Applied to the drawing alone — `.contentShape`
            // below re-declares the full rectangle, so the scrub still
            // answers to a click on the faded ends.
            .msVignette(in: MS.elevated)
            .contentShape(.rect)
            .gesture(
                DragGesture(minimumDistance: 0)
                    .onChanged { v in
                        let f = min(max(v.location.x / geo.size.width, 0), 1)
                        dragTime = f * duration
                    }
                    .onEnded { v in
                        let f = min(max(v.location.x / geo.size.width, 0), 1)
                        onScrub(f * duration)
                        dragTime = nil
                    })
            .msAnimation(Motion.micro, value: fraction)
            .overlay(alignment: .top) {
                if let t = dragTime {
                    Text(clock(t))
                        .clockFont(10, weight: .semibold)
                        .padding(.horizontal, 6)
                        .padding(.vertical, 2)
                        .background(.thinMaterial, in: .capsule)
                        .offset(x: scrubBubbleOffset(geo: geo, t: t), y: -22)
                        .allowsHitTesting(false)
                }
            }
            // A Canvas is one opaque rectangle to VoiceOver, and a drag is
            // not a gesture a keyboard has. This is the whole scrubber for
            // anyone not holding a mouse: where you are, in words, and
            // twentieths of the recording per arrow key.
            .accessibilityElement()
            .accessibilityLabel("Playback position")
            .accessibilityValue(clock(shownTime))
            .accessibilityAdjustableAction { direction in
                let step = max(1, duration / 20)
                switch direction {
                case .increment: onScrub(min(duration, shownTime + step))
                case .decrement: onScrub(max(0, shownTime - step))
                @unknown default: break
                }
            }
        }
    }

    private func scrubBubbleOffset(geo: GeometryProxy, t: Double) -> CGFloat {
        guard duration > 0 else { return 0 }
        let x = geo.size.width * (t / duration)
        return min(max(x - geo.size.width / 2, -geo.size.width / 2 + 30), geo.size.width / 2 - 30)
    }

    /// Downsample (or upsample) the engine's fixed bins to what fits on screen,
    /// taking the max of each bucket so speech bursts stay visible.
    private func rebucket(to count: Int) -> [Double] {
        guard count > 0, !peaks.isEmpty else { return [] }
        if count >= peaks.count { return peaks }
        var out = [Double]()
        out.reserveCapacity(count)
        let stride = Double(peaks.count) / Double(count)
        for i in 0..<count {
            let lo = Int(Double(i) * stride)
            let hi = min(peaks.count, max(lo + 1, Int(Double(i + 1) * stride)))
            out.append(peaks[lo..<hi].max() ?? 0)
        }
        // Normalize to the track's own loudest moment (floored so silence
        // isn't amplified into noise), then a sqrt lift so quiet speech still
        // reads as texture. A mostly-quiet mic track stays visible this way.
        let ceiling = max(out.max() ?? 1, 0.25)
        return out.map { $0 <= 0 ? 0 : ($0 / ceiling).squareRoot() }
    }
}
