// Accessibility: the one place that answers "does the person using this Mac
// want less motion", plus the small vocabulary every view uses so an
// icon-only control still has a name.
//
// THE MOTION CONTRACT, for every file in this target:
//
//   * Never call `.generationShimmer(_:)` directly. Call
//     `.msGenerationShimmer(_:)`. It is the same shimmer with Reduce Motion
//     honoured, and the shimmer is the app's worst offender: a chroma sweep
//     that runs at 30 fps for as long as the engine works, which on a first
//     run is a 2.4 GB download.
//   * Never call `.animation(_:value:)` on something that repeats, or
//     `withAnimation` from a timer. Call `.msAnimation(_:value:)` and
//     `msWithAnimation(_:)`, which return no animation at all when the
//     system asks for less motion.
//   * A repeating animation that carries meaning (a spinner saying "still
//     working") must keep its meaning without the motion: leave the shape,
//     drop the movement, and make sure a word nearby says the same thing.
//
// Reduce Motion is not a preference about taste. It is how people who get
// motion sickness, migraines or vestibular symptoms from moving interfaces
// use a Mac at all.
import SwiftUI
import AppKit

enum MSMotion {
    /// The system's answer, readable from anywhere, including outside a view.
    @MainActor
    static var reduced: Bool {
        NSWorkspace.shared.accessibilityDisplayShouldReduceMotion
    }

    /// `animation`, or nothing at all when the system asks for less motion.
    @MainActor
    static func safe(_ animation: Animation?) -> Animation? {
        reduced ? nil : animation
    }
}

/// `withAnimation`, with Reduce Motion honoured.
@MainActor
@discardableResult
func msWithAnimation<Result>(_ animation: Animation?,
                             _ body: () throws -> Result) rethrows -> Result {
    try withAnimation(MSMotion.safe(animation), body)
}

private struct MotionSafeAnimation<V: Equatable>: ViewModifier {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    let animation: Animation?
    let value: V

    func body(content: Content) -> some View {
        content.animation(reduceMotion ? nil : animation, value: value)
    }
}

/// The generation shimmer, silenced when the system asks for less motion.
///
/// Nothing is lost by silencing it: everywhere the shimmer appears, the
/// engine's own progress line or a status word is already sitting next to it
/// saying the same thing in words.
private struct MotionSafeShimmer: ViewModifier {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    let active: Bool

    func body(content: Content) -> some View {
        content.generationShimmer(active && !reduceMotion)
    }
}

extension View {
    func msAnimation<V: Equatable>(_ animation: Animation?, value: V) -> some View {
        modifier(MotionSafeAnimation(animation: animation, value: value))
    }

    func msGenerationShimmer(_ active: Bool) -> some View {
        modifier(MotionSafeShimmer(active: active))
    }

    /// An image that is pure decoration: it repeats something the text beside
    /// it already says, so VoiceOver should walk straight past it.
    func msDecorative() -> some View {
        accessibilityHidden(true)
    }
}
