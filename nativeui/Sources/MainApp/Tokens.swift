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

    /// Scheme-adaptive color from two hex strings, with an optional pair for
    /// System Settings ▸ Accessibility ▸ Display ▸ Increase contrast.
    ///
    /// Increase contrast is a real macOS switch that this app ignored
    /// completely. Honouring it is one appearance name per scheme, and it is
    /// the difference between "meets the AA floor" and "meets the floor the
    /// person at the keyboard actually needs".
    init(light: String, dark: String,
         lightAlpha: Double = 1, darkAlpha: Double = 1,
         contrastLight: String? = nil, contrastDark: String? = nil) {
        self.init(nsColor: NSColor(name: nil) { appearance in
            let match = appearance.bestMatch(from: [
                .aqua, .darkAqua,
                .accessibilityHighContrastAqua, .accessibilityHighContrastDarkAqua,
            ])
            let isDark = match == .darkAqua || match == .accessibilityHighContrastDarkAqua
            let wantsContrast = match == .accessibilityHighContrastAqua
                || match == .accessibilityHighContrastDarkAqua
            let hex = isDark
                ? ((wantsContrast ? contrastDark : nil) ?? dark)
                : ((wantsContrast ? contrastLight : nil) ?? light)
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
    static let hairline = Color(light: "#0A0F1E", dark: "#FFFFFF",
                                lightAlpha: 0.10, darkAlpha: 0.08)
    static let hairlineStrong = Color(light: "#0A0F1E", dark: "#FFFFFF",
                                      lightAlpha: 0.16, darkAlpha: 0.12)

    // MARK: Inks
    //
    // Every ink below is measured against the surfaces it is allowed to sit
    // on (sunken / content / raised / elevated), not against an idealised
    // white, and the worst of those four is the number quoted. Text inks
    // clear WCAG AA for normal text (4.5:1); ink4 draws controls rather than
    // words, so it clears the non-text minimum (3:1).
    //
    // What changed, and why it had to:
    //   ink3 light was #868D99 — 3.09:1, a fail, on the ink the app uses for
    //     datelines, second lines, section kickers and every piece of "quiet"
    //     copy. Dark was #757C89 — 3.94:1 on an elevated surface, also a fail.
    //   ink4 light was #B3B9C3 — 1.82:1, which is below the floor for the
    //     unselected radio ring in Settings and every chevron and bullet dot
    //     it draws. Dark was #4E5560 — 2.20:1.
    //   The mint below was #0E9C86 in light mode — 3.17:1, and it is the
    //     colour of every clickable timestamp in the app.
    static let ink = Color(light: "#1B1E24", dark: "#E8EAEE",
                           contrastLight: "#0D0F13", contrastDark: "#F7F8FA")
    /// Secondary reading ink. 5.67:1 light, 6.92:1 dark.
    static let ink2 = Color(light: "#5C626D", dark: "#A2A8B4",
                            contrastLight: "#3E434C", contrastDark: "#C6CBD5")
    /// Quiet ink: datelines, kickers, footnotes. 4.80:1 light, 4.58:1 dark.
    static let ink3 = Color(light: "#676D79", dark: "#808795",
                            contrastLight: "#4A4F59", contrastDark: "#A2A8B4")
    /// Non-text ink: unselected rings, bullet dots, chevrons, disclosure
    /// arrows. 3.09:1 light, 3.21:1 dark — the non-text minimum. Nothing that
    /// has to be READ may use it.
    static let ink4 = Color(light: "#868D99", dark: "#666E7B",
                            contrastLight: "#676D79", contrastDark: "#808795")

    // MARK: Chromatic budget
    /// Recording, and nothing else.
    static let recordRed = Color(nsColor: .systemRed)
    /// Where you are in time: playhead, active word, evidence rule, seek
    /// links. 4.85:1 light, 11.66:1 dark.
    static let playhead = Color(light: "#0A7A69", dark: "#64EFD2",
                                contrastLight: "#06584C", contrastDark: "#8FF6E1")
    /// Mint as a FILL (checkboxes, waveform played bars) — same in both
    /// schemes, and never used to carry text.
    static let playheadFill = Color(hex: "#64EFD2")
    /// Interactive accent: focus, send, checked items, the Today stat number.
    static let interactive = Color(light: "#0A7A69", dark: "#64EFD2",
                                   contrastLight: "#06584C", contrastDark: "#8FF6E1")

    // Aurora identity — legal in exactly three places (Today greeting wash,
    // generation shimmer, pill recording edge).
    static let auroraViolet = Color(hex: "#7D5CDF")
    static let auroraBlue = Color(hex: "#2E63D8")

    // MARK: Speaker ramp — channel permutations of one triplet, so no hue is
    // louder than another, and no green permutation for the accent (mint owns
    // green). Speaker names are TEXT, so every entry has to clear AA: the
    // light ramp runs 5.06–8.20:1, the dark 5.87–10.10:1.
    //
    // Moss is the one that isn't a straight permutation. Green carries 71% of
    // the luminance sum, so #608C34 measured 3.66:1 in light mode while its
    // four siblings passed comfortably — the permutation was a nice idea that
    // the luminance formula does not honour. Darkened to sit with sand.
    static let ramp: [Color] = [
        Color(light: "#8C6034", dark: "#D6B08A"),   // you, sand
        Color(light: "#8C3460", dark: "#D68AB0"),   // rose
        Color(light: "#60348C", dark: "#B08AD6"),   // iris
        Color(light: "#34608C", dark: "#8AB0D6"),   // sky
        Color(light: "#4F7329", dark: "#B0D68A"),   // moss
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
