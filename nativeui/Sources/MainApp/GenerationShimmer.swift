// The generation shimmer: while a meeting is being transcribed or
// summarised, its title runs a warm chroma sweep (mint → violet → blue →
// mint) masked to the letters. No spinner, no skeleton — generation feels
// like the text blushing. This is one of Aurora's three legal appearances.
import SwiftUI

struct GenerationShimmer: ViewModifier {
    var active: Bool

    func body(content: Content) -> some View {
        if active {
            content
                .overlay {
                    TimelineView(.animation(minimumInterval: 1.0 / 30)) { context in
                        let t = context.date.timeIntervalSinceReferenceDate
                        let phase = (t.truncatingRemainder(dividingBy: 2.2)) / 2.2
                        GeometryReader { geo in
                            LinearGradient(
                                stops: [
                                    .init(color: MS.playheadFill, location: 0),
                                    .init(color: MS.auroraViolet, location: 0.35),
                                    .init(color: MS.auroraBlue, location: 0.65),
                                    .init(color: MS.playheadFill, location: 1),
                                ],
                                startPoint: .leading, endPoint: .trailing)
                            .frame(width: geo.size.width * 2)
                            .offset(x: -geo.size.width + geo.size.width * 2 * phase)
                        }
                    }
                    .mask(content)
                    .allowsHitTesting(false)
                }
        } else {
            content
        }
    }
}

extension View {
    func generationShimmer(_ active: Bool) -> some View {
        modifier(GenerationShimmer(active: active))
    }
}
