// The app's voice and motion, written down so they stay consistent.
//
// VOICE — attentive, precise, quietly warm: a stenographer in the corner,
// not a cheerleader. Terse around record start/stop; one warm moment when a
// transcript is ready; maximum helpfulness and zero whimsy when something
// is wrong.
//
// MOTION — fast, transform/opacity only, exits faster than entries, nothing
// longer than 300ms. The single springy moment in the app is the record
// pill morph; everything else is quick and quiet.
import SwiftUI

enum Motion {
    static let enter = Animation.spring(response: 0.28, dampingFraction: 0.86)
    /// Reserved for the record pill morph — the one springy moment.
    static let springy = Animation.spring(response: 0.34, dampingFraction: 0.8)
    static let exit = Animation.easeOut(duration: 0.15)
    static let micro = Animation.easeOut(duration: 0.12)
    static let seek = Animation.easeOut(duration: 0.18)
    /// The press curve: the web system's one easing token
    /// (cubic-bezier(0.22, 1, 0.32, 1)) at the researched 160ms. Used by
    /// PressStyle and nothing else — presses are the one interaction fast
    /// enough to deserve a sharper start than the springs give.
    static let press = Animation.timingCurve(0.22, 1, 0.32, 1, duration: 0.16)
}

/// Every button presses down 3%: physical, instant, uniform.
struct PressStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .scaleEffect(configuration.isPressed ? 0.97 : 1)
            .animation(Motion.press, value: configuration.isPressed)
    }
}

extension View {
    /// One clock voice for every numeral in the app.
    func clockFont(_ size: CGFloat = 11, weight: Font.Weight = .medium) -> some View {
        self.font(.system(size: size, weight: weight, design: .monospaced))
            .monospacedDigit()
            .contentTransition(.numericText())
    }
}
