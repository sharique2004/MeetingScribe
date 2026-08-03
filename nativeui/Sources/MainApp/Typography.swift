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
    /// 17pt regular lead — light weight at large size reads as warmth.
    static let lead = Font.system(size: 17)
    /// 26pt semibold serif page title.
    static let pageTitle = Font.system(size: 26, weight: .semibold, design: .serif)
    /// 34pt greeting — the app speaking, so SF, not serif.
    static let greeting = Font.system(size: 34, weight: .semibold)
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
