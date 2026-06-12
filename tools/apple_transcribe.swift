// Fast on-device transcription via Apple's SpeechAnalyzer (macOS 26+).
//
// Runs on the Neural Engine — much faster than Whisper and free. Reads a
// WAV file, prints JSON with segments and word-level timestamps:
//   {"language":"en-US","segments":[
//      {"start":1.2,"end":4.5,"text":"Hello there",
//       "words":[{"w":"Hello","s":1.2,"e":1.6}, …]}]}
//
// Build:  swiftc -O tools/apple_transcribe.swift -o ~/.meetingscribe/bin/apple_transcribe
// Usage:  apple_transcribe <file.wav> [locale]   (locale default en-US)
//
// Exit codes: 0 ok · 2 usage/IO error · 3 backend/model unavailable.

import AVFoundation
import CoreMedia
import Foundation
import Speech

struct WordOut: Codable { let w: String; let s: Double; let e: Double }
struct SegOut: Codable { let start: Double; let end: Double; let text: String; let words: [WordOut] }
struct Output: Codable { let language: String; let segments: [SegOut] }

func die(_ message: String, code: Int32 = 2) -> Never {
    FileHandle.standardError.write(Data((message + "\n").utf8))
    exit(code)
}

func note(_ message: String) {
    FileHandle.standardError.write(Data((message + "\n").utf8))
}

@main
struct AppleTranscribe {
    static func main() async {
        let args = CommandLine.arguments
        guard args.count >= 2 else { die("usage: apple_transcribe <file.wav> [locale]") }
        let url = URL(fileURLWithPath: args[1])
        let localeID = (args.count >= 3 && !args[2].isEmpty) ? args[2] : "en-US"
        do {
            try await transcribe(url: url, localeID: localeID)
        } catch {
            die("apple_transcribe error: \(error)", code: 3)
        }
    }

    static func transcribe(url: URL, localeID: String) async throws {
        let requested = Locale(identifier: localeID)
        let locale = await SpeechTranscriber.supportedLocale(equivalentTo: requested) ?? requested

        let transcriber = SpeechTranscriber(
            locale: locale,
            transcriptionOptions: [],
            reportingOptions: [],
            attributeOptions: [.audioTimeRange]
        )

        // Download the on-device model the first time (needs internet once).
        if let request = try await AssetInventory.assetInstallationRequest(supporting: [transcriber]) {
            note("downloading speech model for \(locale.identifier)…")
            try await request.downloadAndInstall()
        }

        let analyzer = SpeechAnalyzer(modules: [transcriber])
        let audioFile = try AVAudioFile(forReading: url)

        // Collect finalized results as they stream in.
        let collector = Task { () -> [SegOut] in
            var segments: [SegOut] = []
            for try await result in transcriber.results {
                segments.append(makeSegment(result))
            }
            return segments
        }

        if let lastTime = try await analyzer.analyzeSequence(from: audioFile) {
            try await analyzer.finalize(through: lastTime)
        }
        try await analyzer.finalizeAndFinishThroughEndOfInput()

        let segments = try await collector.value
        let output = Output(language: locale.identifier, segments: segments)
        let data = try JSONEncoder().encode(output)
        FileHandle.standardOutput.write(data)
    }

    static func makeSegment(_ result: SpeechTranscriber.Result) -> SegOut {
        let attributed = result.text
        let plain = String(attributed.characters).trimmingCharacters(in: .whitespacesAndNewlines)

        var words: [WordOut] = []
        for run in attributed.runs {
            guard let timeRange = run.audioTimeRange else { continue }
            let token = String(attributed[run.range].characters)
            if token.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { continue }
            words.append(WordOut(w: token, s: timeRange.start.seconds, e: timeRange.end.seconds))
        }

        let start = result.range.start.seconds
        let end = result.range.end.seconds
        return SegOut(start: start, end: end, text: plain, words: words)
    }
}
