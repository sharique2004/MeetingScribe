// Summaries, on device, via Apple Intelligence.
//
// Ported from tools/apple_llm.swift, and carrying its hardest-won line: the
// model is built with `.permissiveContentTransformations` guardrails. The
// default guardrails REJECT genuine meeting transcripts — ordinary talk about
// medical, legal, security or personnel matters trips them — and Apple
// provides this mode precisely for the case where the app is transforming the
// user's own content rather than generating new claims. Without it, real
// meetings fail to summarise and the failure looks like a bug in the app.
import FoundationModels
import Foundation

enum MeetingSummariser {
    enum SummariseError: LocalizedError {
        case unavailable(String)

        var errorDescription: String? {
            if case .unavailable(let why) = self { return why }
            return nil
        }
    }

    @Generable
    struct Draft {
        @Guide(description: "One sentence naming what this meeting was actually about. No preamble.")
        var headline: String

        @Guide(description: "Three to six things that were said or decided, each a full sentence.", .count(3...6))
        var keyPoints: [String]

        @Guide(description: "Concrete things someone agreed to do. Empty if nobody committed to anything.", .count(0...5))
        var actionItems: [String]
    }

    static var availability: String? {
        let model = SystemLanguageModel(guardrails: .permissiveContentTransformations)
        switch model.availability {
        case .available:
            return nil
        case .unavailable(let reason):
            switch reason {
            case .appleIntelligenceNotEnabled:
                return "Turn on Apple Intelligence in Settings to summarise meetings on this iPhone."
            case .deviceNotEligible:
                return "This iPhone can't run Apple Intelligence, so summaries aren't available."
            case .modelNotReady:
                return "Apple Intelligence is still downloading its model. Try again shortly."
            @unknown default:
                return "Apple Intelligence isn't available right now."
            }
        @unknown default:
            return "Apple Intelligence isn't available right now."
        }
    }

    static func summarise(turns: [Turn]) async throws -> MeetingSummary {
        if let why = availability { throw SummariseError.unavailable(why) }

        let transcript = turns
            .map { "[\(clock($0.start))] \($0.text)" }
            .joined(separator: "\n")

        let session = LanguageModelSession(
            model: SystemLanguageModel(guardrails: .permissiveContentTransformations),
            instructions: """
            You summarise meeting transcripts. Report only what the transcript \
            actually says. Never invent a number, a name, a date or a \
            commitment that is not there. If the transcript is too thin to \
            summarise, say so in the headline rather than padding it.
            """)

        let response = try await session.respond(
            to: "Summarise this meeting transcript.\n\n\(transcript)",
            generating: Draft.self)

        let draft = response.content
        return MeetingSummary(headline: draft.headline,
                              tldr: draft.headline,
                              keyPoints: draft.keyPoints,
                              actionItems: draft.actionItems)
    }

    private static func clock(_ seconds: Double) -> String {
        let total = Int(seconds.rounded())
        return String(format: "%d:%02d", total / 60, total % 60)
    }
}
