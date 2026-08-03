// Aurora as a palette, not a scheme. Four tonal surfaces (elevation by
// lightness, never shadow), four inks (never pure white), one hairline, and
// a strict chromatic budget: red = recording, mint = the present moment and
// interaction, a five-hue equal-luminance ramp = speaker identity. Light
// ("Aurora Day") and dark ("Aurora Night") both first-class.
import SwiftUI
import AppKit

extension Color {
    init(hex: String) {
        var h = hex.trimmingCharacters(in: .whitespaces)
        if h.hasPrefix("#") { h.removeFirst() }
        var v: UInt64 = 0
        Scanner(string: h).scanHexInt64(&v)
        self.init(
            red: Double((v >> 16) & 0xFF) / 255,
            green: Double((v >> 8) & 0xFF) / 255,
            blue: Double(v & 0xFF) / 255)
    }

    /// Scheme-adaptive color from two hex strings.
    init(light: String, dark: String, lightAlpha: Double = 1, darkAlpha: Double = 1) {
        self.init(nsColor: NSColor(name: nil) { appearance in
            let isDark = appearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua
            let hex = isDark ? dark : light
            let alpha = isDark ? darkAlpha : lightAlpha
            var h = hex; if h.hasPrefix("#") { h.removeFirst() }
            var v: UInt64 = 0
            Scanner(string: h).scanHexInt64(&v)
            return NSColor(
                red: CGFloat((v >> 16) & 0xFF) / 255,
                green: CGFloat((v >> 8) & 0xFF) / 255,
                blue: CGFloat(v & 0xFF) / 255,
                alpha: alpha)
        })
    }
}

enum MS {
    // MARK: Surfaces
    static let sunken = Color(light: "#F6F6F4", dark: "#0B0D12")
    static let content = Color(light: "#FCFCFB", dark: "#0E1116")
    static let raised = Color(light: "#FFFFFF", dark: "#15181E")
    static let elevated = Color(light: "#FFFFFF", dark: "#1B1F26")
    static let hairline = Color(light: "#0A0F1E", dark: "#FFFFFF", lightAlpha: 0.10, darkAlpha: 0.08)
    static let hairlineStrong = Color(light: "#0A0F1E", dark: "#FFFFFF", lightAlpha: 0.16, darkAlpha: 0.12)

    // MARK: Inks
    static let ink = Color(light: "#1B1E24", dark: "#E8EAEE")
    static let ink2 = Color(light: "#5C626D", dark: "#A2A8B4")
    static let ink3 = Color(light: "#868D99", dark: "#757C89")
    static let ink4 = Color(light: "#B3B9C3", dark: "#4E5560")

    // MARK: Chromatic budget
    /// Recording, and nothing else.
    static let recordRed = Color(nsColor: .systemRed)
    /// Where you are in time: playhead, active word, evidence rule, seek links.
    static let playhead = Color(light: "#0E9C86", dark: "#64EFD2")
    /// Mint as a FILL (checkboxes, waveform played bars) — same in both schemes.
    static let playheadFill = Color(hex: "#64EFD2")
    /// Interactive accent: focus, send, checked items, the Today stat number.
    static let interactive = Color(light: "#0E9C86", dark: "#64EFD2")

    // Aurora identity — legal in exactly three places (Today greeting wash,
    // generation shimmer, pill recording edge).
    static let auroraViolet = Color(hex: "#7D5CDF")
    static let auroraBlue = Color(hex: "#2E63D8")

    // MARK: Speaker ramp — channel permutations of one triplet; equal
    // luminance, no green permutation (mint owns green).
    static let ramp: [Color] = [
        Color(light: "#8C6034", dark: "#D6B08A"),   // you — sand
        Color(light: "#8C3460", dark: "#D68AB0"),   // rose
        Color(light: "#60348C", dark: "#B08AD6"),   // iris
        Color(light: "#34608C", dark: "#8AB0D6"),   // sky
        Color(light: "#608C34", dark: "#B0D68A"),   // moss
    ]

    static func speaker(_ key: String?) -> Color {
        guard let key, key != "you" else { return ramp[0] }
        let n = Int(key.dropFirst()) ?? 1            // "s2" → 2
        return ramp[1 + (max(1, n) - 1) % (ramp.count - 1)]
    }

    static func speaker(index: Int) -> Color {
        index == 0 ? ramp[0] : ramp[1 + (index - 1) % (ramp.count - 1)]
    }
}
