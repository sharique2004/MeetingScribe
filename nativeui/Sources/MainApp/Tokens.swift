// Aurora, as a place. Four tonal surfaces on a violet-cool axis (elevation
// by lightness, never shadow — the shadows that DO exist are tinted, not
// gray), four inks (never pure white), edge light instead of borders, and a
// strict chromatic budget: red = recording, mint = the present moment and
// interaction, sky = the second voice of the accent, a five-hue
// equal-luminance ramp = speaker identity. Light ("paper under a north
// window": violet shadow below, warm light above) and dark ("Aurora Night")
// are both first-class — the same hues at different times of day.
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
    ///
    /// The contrast ALPHAS exist for the edge-light tokens: a rim stroked at
    /// 5% white is structure in the default appearance and invisible under
    /// Increase Contrast, where the answer is the same hex at full opacity —
    /// a hex swap alone cannot express that.
    init(light: String, dark: String,
         lightAlpha: Double = 1, darkAlpha: Double = 1,
         contrastLight: String? = nil, contrastDark: String? = nil,
         contrastLightAlpha: Double? = nil, contrastDarkAlpha: Double? = nil) {
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
            let alpha = isDark
                ? ((wantsContrast ? contrastDarkAlpha : nil) ?? darkAlpha)
                : ((wantsContrast ? contrastLightAlpha : nil) ?? lightAlpha)
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
    //
    // Dark is a hue rotation of the old graphite ladder at matched luminance
    // (each step within 4% relative luminance of what it replaces), so every
    // ink ratio audited below survives the move into the violet. Steps run
    // roughly +3% OKLab L apiece; blue leads green by 8–13 and red by 1–2 —
    // real chroma, never a flat gray, never pure black.
    //
    // Light is the day translation, and it is deliberately not "white": the
    // recessed end sits in violet-cool shadow and the ladder WARMS as it
    // rises toward the light (elevated has R > B). The old ladder also had
    // raised and elevated both at #FFFFFF — three surfaces pretending to be
    // four; that fault ends here.
    static let sunken = Color(light: "#EEECF3", dark: "#0C0B13")
    static let content = Color(light: "#F6F4F9", dark: "#110F19")
    static let raised = Color(light: "#FBFAFC", dark: "#181622")
    static let elevated = Color(light: "#FEFDFC", dark: "#1F1D2B")
    static let hairline = Color(light: "#0A0F1E", dark: "#FFFFFF",
                                lightAlpha: 0.10, darkAlpha: 0.08,
                                contrastLightAlpha: 1, contrastDarkAlpha: 1)
    static let hairlineStrong = Color(light: "#0A0F1E", dark: "#FFFFFF",
                                      lightAlpha: 0.16, darkAlpha: 0.12,
                                      contrastLightAlpha: 1, contrastDarkAlpha: 1)

    // MARK: Edge light
    //
    // Raised chrome is lit, not outlined: a 1px stroke that runs edgeTop →
    // edgeBottom, plus edgeRim as the all-round contact line. In dark the
    // light is white falling from above; in light the top edge is a real
    // specular catch and the BOTTOM carries the violet contact shadow —
    // same physics, inverted sun. Under Increase Contrast every edge token
    // becomes a full-opacity line, which is what the alpha half of the
    // Color initializer above exists for.
    static let edgeTop = Color(light: "#FFFFFF", dark: "#FFFFFF",
                               lightAlpha: 0.85, darkAlpha: 0.10,
                               contrastLightAlpha: 1, contrastDarkAlpha: 1)
    static let edgeBottom = Color(light: "#2A2440", dark: "#FFFFFF",
                                  lightAlpha: 0.07, darkAlpha: 0.03,
                                  contrastLightAlpha: 0.5, contrastDarkAlpha: 0.6)
    static let edgeRim = Color(light: "#2A2440", dark: "#FFFFFF",
                               lightAlpha: 0.06, darkAlpha: 0.055,
                               contrastLightAlpha: 0.9, contrastDarkAlpha: 0.9)
    /// Shadows in a violet world are violet. Every elevation ramp tier tints
    /// with this, never with gray.
    static let shadowTint = Color(light: "#2A2440", dark: "#05040A")

    // MARK: Radius
    //
    // Containers only. Glyph geometry — waveform bars, the record stop
    // square, the active-word underglow capsule — keeps its own literal
    // radii; those are drawings, not surfaces, and the sweep that installed
    // this scale deliberately left them alone.
    enum radius {
        static let xs: CGFloat = 4
        static let sm: CGFloat = 8
        static let md: CGFloat = 11
        static let lg: CGFloat = 14
        static let xl: CGFloat = 18
    }

    static func rr(_ r: CGFloat) -> RoundedRectangle {
        RoundedRectangle(cornerRadius: r, style: .continuous)
    }

    // MARK: Inks
    //
    // Every ink below is measured against the surfaces it is allowed to sit
    // on (sunken / content / raised / elevated), not against an idealised
    // white, and the worst of those four is the number quoted. Text inks
    // clear WCAG AA for normal text (4.5:1); ink4 draws controls rather than
    // words, so it clears the non-text minimum (3:1).
    //
    // The Aurora retune touched LIGHT only (dark luminance was matched, so
    // dark inks carried over untouched): dropping light sunken from #F6F6F4
    // to #EEECF3 cost every dark-on-light ink ~9% of its ratio, so the three
    // that sat on the floor moved down a notch — ink3 #676D79 → #5F6570
    // (5.02:1), ink4 #868D99 → #7E8593 (3.17:1), and the mint text accent
    // #0A7A69 → #097263 (5.05:1, it is every clickable timestamp).
    static let ink = Color(light: "#1B1E24", dark: "#E8EAEE",
                           contrastLight: "#0D0F13", contrastDark: "#F7F8FA")
    /// Secondary reading ink. 5.23:1 light, 6.92:1 dark.
    static let ink2 = Color(light: "#5C626D", dark: "#A2A8B4",
                            contrastLight: "#3E434C", contrastDark: "#C6CBD5")
    /// Quiet ink: datelines, kickers, footnotes. 5.02:1 light, 4.58:1 dark.
    static let ink3 = Color(light: "#5F6570", dark: "#808795",
                            contrastLight: "#4A4F59", contrastDark: "#A2A8B4")
    /// Non-text ink: unselected rings, bullet dots, chevrons, disclosure
    /// arrows. 3.17:1 light, 3.21:1 dark — the non-text minimum. Nothing that
    /// has to be READ may use it.
    static let ink4 = Color(light: "#7E8593", dark: "#666E7B",
                            contrastLight: "#676D79", contrastDark: "#808795")

    // MARK: Chromatic budget
    /// Recording, and nothing else.
    static let recordRed = Color(nsColor: .systemRed)
    /// Where you are in time: playhead, active word, evidence rule, seek
    /// links. 5.05:1 light, 11.66:1 dark.
    static let playhead = Color(light: "#097263", dark: "#64EFD2",
                                contrastLight: "#06584C", contrastDark: "#8FF6E1")
    /// Mint as a FILL (checkboxes, waveform played bars) — same in both
    /// schemes, and never used to carry text.
    static let playheadFill = Color(hex: "#64EFD2")
    /// Interactive accent: focus, send, checked items, the Today stat number.
    static let interactive = Color(light: "#097263", dark: "#64EFD2",
                                   contrastLight: "#06584C", contrastDark: "#8FF6E1")
    /// The accent's second voice — Signal Sky from the web design system.
    /// Beam tail, the live rail's cooler end, never a primary control.
    static let sky = Color(light: "#0A6E96", dark: "#7DD3FC",
                           contrastLight: "#07506D", contrastDark: "#A5E1FD")
    /// Sky as a FILL, scheme-independent, never text.
    static let skyFill = Color(hex: "#7DD3FC")
    /// Text sitting ON the mint fill — the web's own --aqua-ink. 11.96:1 on
    /// playheadFill. Before this token existed the app hand-rolled
    /// .black.opacity(0.85) in five places.
    static let inkOnAccent = Color(hex: "#04211C")
    /// Text sitting ON recordRed.
    static let inkOnRecord = Color(hex: "#FFFFFF")

    // Aurora atmosphere — the sky itself. Legal exactly where MSAuroraField
    // puts it (sidebar, the Today header band, the live page) plus the
    // generation shimmer; never on a reading surface.
    static let auroraViolet = Color(hex: "#7D5CDF")
    static let auroraBlue = Color(hex: "#2E63D8")

    // MARK: Speaker ramp — channel permutations of one triplet, so no hue is
    // louder than another, and no green permutation for the accent (mint owns
    // green). Speaker names are TEXT, so every entry has to clear AA: the
    // light ramp runs 4.69–8.20:1 on the new sunken, the dark 5.87–10.10:1.
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
