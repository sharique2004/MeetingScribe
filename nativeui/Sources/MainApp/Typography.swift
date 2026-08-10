// The type system. One rule with meaning: serif (New York) is human speech
// and the meeting's own name; SF is the software talking; SF Mono is every
// numeral that changes; SF Rounded appears exactly once in the app (the
// Today stat number). Hierarchy inside a reading column is weight only —
// never a size jump, never a colour change.
import SwiftUI

enum MSFont {
    /// 11pt tracked ALL-CAPS kickers and speaker labels. Apply .kerning(0.55).
    static let kicker = Font.system(size: 11, weight: .medium)
    /// 12pt metadata, datelines, second lines.
    static let meta = Font.system(size: 12)
    /// 13.5pt sidebar rows, chrome, buttons.
    static let chrome = Font.system(size: 13.5)
    static let chromeMedium = Font.system(size: 13.5, weight: .medium)
    /// 15pt document body.
    static let body = Font.system(size: 15)
    /// 15pt section headings — same size, weight is the hierarchy.
    static let sectionHeading = Font.system(size: 15, weight: .semibold)
    /// 20pt regular lead under a display line (the Today stat sentence).
    static let displayLead = Font.system(size: 20)
    /// 26pt semibold serif page title.
    static let pageTitle = Font.system(size: 26, weight: .semibold, design: .serif)
    /// 40pt expanded display — the greeting, and nothing else at this size.
    /// The width axis is the drama: almost nobody ships SF Expanded, so it
    /// reads as a custom cut while staying native. Pair with tracking(-0.8).
    static let display = Font.system(size: 40, weight: .semibold).width(.expanded)
    /// Hero numerals (Today stat, talk share, HUD elapsed): SF Rounded, the
    /// one-off that used to live inline in TodayPage promoted to a token.
    static let numeral = Font.system(size: 20, weight: .semibold, design: .rounded)
    // The HUD pill's own three voices — it had fourteen raw font calls and
    // zero tokens, which is how a surface drifts off-system.
    static let hudLabel = Font.system(size: 12, weight: .semibold)
    static let hudBody = Font.system(size: 12.5)
    static let hudCaption = Font.system(size: 11)
    /// Spoken words: New York 15/25.
    static let spoken = Font.system(size: 15, design: .serif)
    /// Unfolded evidence: New York 14/22.
    static let evidence = Font.system(size: 14, design: .serif)
}

extension View {
    /// Every reading column: 680pt measure, centred, comfortable gutters.
    func documentMeasure() -> some View {
        self.frame(maxWidth: 680, alignment: .leading)
            .padding(.horizontal, 40)
            .frame(maxWidth: .infinity)
    }
}
