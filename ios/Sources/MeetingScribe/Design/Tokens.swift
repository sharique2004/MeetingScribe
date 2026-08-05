// The Mac app's Aurora palette, resolved through UIKit instead of AppKit.
//
// Deliberately the SAME hex values as nativeui/Sources/MainApp/Tokens.swift,
// including the contrast-boosted pairs, because the two apps are one product
// and a user who moves between them should not feel a seam. What changes here
// is only the resolver: macOS asks an NSAppearance which of four appearance
// names it best matches; iOS asks a UITraitCollection for its userInterface
// Style and accessibilityContrast. The inks are the measured ones — every text
// ink clears WCAG AA on all four surfaces, ink4 clears the 3:1 non-text floor
// and is never allowed to carry words.
import SwiftUI
import UIKit

extension Color {
    init(hex: String) {
        self.init(uiColor: UIColor(hex: hex, alpha: 1))
    }

    /// Scheme-adaptive colour, with an optional pair for Settings ▸
    /// Accessibility ▸ Display & Text Size ▸ Increase Contrast.
    init(light: String, dark: String,
         lightAlpha: Double = 1, darkAlpha: Double = 1,
         contrastLight: String? = nil, contrastDark: String? = nil) {
        self.init(uiColor: UIColor { traits in
            let isDark = traits.userInterfaceStyle == .dark
            let wantsContrast = traits.accessibilityContrast == .high
            let hex = isDark
                ? ((wantsContrast ? contrastDark : nil) ?? dark)
                : ((wantsContrast ? contrastLight : nil) ?? light)
            return UIColor(hex: hex, alpha: isDark ? darkAlpha : lightAlpha)
        })
    }
}

extension UIColor {
    fileprivate convenience init(hex: String, alpha: Double) {
        var h = hex.trimmingCharacters(in: .whitespaces)
        if h.hasPrefix("#") { h.removeFirst() }
        var v: UInt64 = 0
        Scanner(string: h).scanHexInt64(&v)
        self.init(red: CGFloat((v >> 16) & 0xFF) / 255,
                  green: CGFloat((v >> 8) & 0xFF) / 255,
                  blue: CGFloat(v & 0xFF) / 255,
                  alpha: alpha)
    }
}

enum MS {
    // MARK: Surfaces — elevation by lightness, never shadow.
    static let sunken = Color(light: "#F6F6F4", dark: "#0B0D12")
    static let content = Color(light: "#FCFCFB", dark: "#0E1116")
    static let raised = Color(light: "#FFFFFF", dark: "#15181E")
    static let elevated = Color(light: "#FFFFFF", dark: "#1B1F26")
    static let hairline = Color(light: "#0A0F1E", dark: "#FFFFFF",
                                lightAlpha: 0.10, darkAlpha: 0.08)

    // MARK: Inks — never pure white, all measured against the four surfaces.
    static let ink = Color(light: "#1B1E24", dark: "#E8EAEE",
                           contrastLight: "#0D0F13", contrastDark: "#F7F8FA")
    static let ink2 = Color(light: "#5C626D", dark: "#A2A8B4",
                            contrastLight: "#3E434C", contrastDark: "#C6CBD5")
    static let ink3 = Color(light: "#676D79", dark: "#808795",
                            contrastLight: "#4A4F59", contrastDark: "#A2A8B4")
    /// Non-text only: rings, dots, chevrons. Nothing that must be READ.
    static let ink4 = Color(light: "#868D99", dark: "#666E7B",
                            contrastLight: "#676D79", contrastDark: "#808795")

    // MARK: Chromatic budget — red is recording and nothing else.
    static let recordRed = Color(uiColor: .systemRed)
    static let playhead = Color(light: "#0A7A69", dark: "#64EFD2",
                                contrastLight: "#06584C", contrastDark: "#8FF6E1")
    static let playheadFill = Color(hex: "#64EFD2")
    static let interactive = Color(light: "#0A7A69", dark: "#64EFD2",
                                   contrastLight: "#06584C", contrastDark: "#8FF6E1")

    /// Speaker identity. Equal-luminance ramp, no green (mint owns green).
    static let speakerRamp: [Color] = [
        Color(light: "#8A6212", dark: "#D8B25A"),
        Color(light: "#A03D6B", dark: "#E58BB4"),
        Color(light: "#3D5FA8", dark: "#8FB2E8"),
        Color(light: "#7A4BA8", dark: "#BFA0E8"),
        Color(light: "#A85138", dark: "#E89B80"),
    ]

    static func speaker(_ key: String) -> Color {
        if key == "you" { return speakerRamp[0] }
        let hash = abs(key.hashValue) % speakerRamp.count
        return speakerRamp[hash]
    }
}

enum MSFont {
    static let pageTitle = Font.system(size: 28, weight: .semibold, design: .serif)
    static let lead = Font.system(size: 17)
    static let body = Font.system(size: 16)
    static let chrome = Font.system(size: 15)
    static let chromeMedium = Font.system(size: 15, weight: .medium)
    static let meta = Font.system(size: 13)
    static let kicker = Font.system(size: 11, weight: .semibold)
}
