// On-device transcription, via the same SpeechAnalyzer flow the Mac uses.
//
// This is a direct port of tools/apple_transcribe.swift, and that is the whole
// point of it: the Mac helper is 116 lines of AVFoundation + Speech with no
// macOS-only API in it, so the phone does not need a second transcription
// strategy, a server round trip, or a bundled Python. The Neural Engine does
// the work on both, and the audio never leaves the device on either.
//
// The one thing that is NOT ported is speaker diarization. The Mac splits
// voices with an ECAPA embedder running under Python, and there is no
// equivalent here yet, so every turn comes back as one speaker. Saying that
// plainly beats shipping a transcript that silently pretends one person spoke.
import AVFoundation
import Foundation
import Speech

enum MeetingTranscriber {
    enum TranscribeError: LocalizedError {
        case unavailable(String)

        var errorDescription: String? {
            if case .unavailable(let why) = self { return why }
            return nil
        }
    }

    /// Transcribe a recorded file into turns.
    ///
    /// `onProgress` reports in the engine's own words rather than a fake
    /// percentage, because the first run downloads a speech model and "63%"
    /// during a 400 MB download is a lie the Mac already learned not to tell.
    static func transcribe(url: URL,
                           localeID: String = "en-US",
                           vocabulary: [String] = [],
                           onProgress: @MainActor @escaping (String) -> Void) async throws -> [Turn] {
        let requested = Locale(identifier: localeID)
        let locale = await SpeechTranscriber.supportedLocale(equivalentTo: requested) ?? requested

        let transcriber = SpeechTranscriber(
            locale: locale,
            transcriptionOptions: [],
            reportingOptions: [],
            attributeOptions: [.audioTimeRange]
        )

        // iOS will not even report the download status of a locale the app has
        // not reserved: the first real run on device failed with "Cannot check
        // the download status, com.meetingscribe.ios is not subscribed to
        // transcription.en". Reserving is the subscription, and it is per-app
        // with a system-imposed limit, so the reservation is released when a
        // different locale is needed rather than accumulating silently.
        // iOS will not report the download status of a locale the app has not
        // reserved: the first run failed with "Cannot check the download
        // status, com.meetingscribe.ios is not subscribed to transcription.en".
        // Reserving is that subscription. It is per-app and capped
        // (maximumReservedLocales is 5 on current systems), so an old
        // reservation is released rather than left to accumulate.
        let reserved = await AssetInventory.reservedLocales
        if !reserved.contains(where: { $0.identifier(.bcp47) == locale.identifier(.bcp47) }) {
            if reserved.count >= AssetInventory.maximumReservedLocales, let oldest = reserved.first {
                await AssetInventory.release(reservedLocale: oldest)
            }
            try await AssetInventory.reserve(locale: locale)
        }

        do {
            if let request = try await AssetInventory.assetInstallationRequest(supporting: [transcriber]) {
                await onProgress("Downloading the speech model, once")
                try await request.downloadAndInstall()
            }
        } catch {
            // The Simulator does not ship the on-device speech models and has
            // no way to fetch them — the same call on this Mac reports nine
            // installed English locales, the Simulator reports none. Say that
            // plainly instead of surfacing an asset-subsystem error that reads
            // like a bug in the app.
            let installed = await SpeechTranscriber.installedLocales
            if installed.isEmpty {
                throw TranscribeError.unavailable(
                    "On-device speech isn't available here. The iOS Simulator doesn't ship the speech models, so transcription needs a real iPhone. The recording is saved and will transcribe when you open it on a device.")
            }
            throw error
        }

        await onProgress("Transcribing on this iPhone")
        let analyzer = SpeechAnalyzer(modules: [transcriber])

        // Bias toward names and jargon, the same hook the Mac exposes as
        // "Your words" in Settings.
        if !vocabulary.isEmpty {
            let context = AnalysisContext()
            context.contextualStrings[.general] = vocabulary
            try await analyzer.setContext(context)
        }

        let audioFile = try AVAudioFile(forReading: url)

        let collector = Task { () -> [Turn] in
            var turns: [Turn] = []
            for try await result in transcriber.results {
                let text = String(result.text.characters)
                    .trimmingCharacters(in: .whitespacesAndNewlines)
                guard !text.isEmpty else { continue }
                turns.append(Turn(speaker: "you",
                                  start: result.range.start.seconds,
                                  end: result.range.end.seconds,
                                  text: text))
            }
            return turns
        }

        if let lastTime = try await analyzer.analyzeSequence(from: audioFile) {
            try await analyzer.finalize(through: lastTime)
        }
        try await analyzer.finalizeAndFinishThroughEndOfInput()

        return try await collector.value
    }
}
