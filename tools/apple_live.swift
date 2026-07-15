// apple_live — streaming live captions via Apple's SpeechAnalyzer (macOS 26+).
//
// Reads raw interleaved **int16 PCM** from stdin and emits NDJSON caption
// events on stdout as the on-device model (Neural Engine) recognizes speech:
//
//   {"t":"partial","start":1.2,"end":3.4,"text":"hello wor"}   (volatile)
//   {"t":"final","start":1.2,"end":3.8,"text":"Hello world."}  (won't change)
//   {"t":"done"}                                               (after EOF)
//
// Usage: apple_live <locale> <sample_rate_hz> <channels> [--context file.json]
//   The context file may carry {"strings": ["Priya", "Q3 roadmap", …]} —
//   names/vocabulary the recognizer should be biased toward.
//
// Stdin EOF finalizes pending audio and exits 0. Exit 2 usage, 3 unavailable.
//
// Compiled by swift_helpers.ensure_binary() to ~/.meetingscribe/bin/apple_live.

import AVFoundation
import CoreMedia
import Foundation
import Speech

func die(_ message: String, code: Int32 = 2) -> Never {
    FileHandle.standardError.write(Data((message + "\n").utf8))
    exit(code)
}

let stdoutLock = NSLock()
func emit(_ obj: [String: Any]) {
    guard let data = try? JSONSerialization.data(withJSONObject: obj, options: []) else { return }
    stdoutLock.lock()
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write(Data("\n".utf8))
    stdoutLock.unlock()
}

@main
struct AppleLive {
    static func main() async {
        var args = Array(CommandLine.arguments.dropFirst())
        var contextPath: String? = nil
        if let i = args.firstIndex(of: "--context"), i + 1 < args.count {
            contextPath = args[i + 1]
            args.removeSubrange(i...(i + 1))
        }
        guard args.count >= 3,
              let rate = Double(args[1]), rate > 0,
              let channels = UInt32(args[2]), channels > 0 else {
            die("usage: apple_live <locale> <sample_rate_hz> <channels> [--context file.json]")
        }
        do {
            try await run(localeID: args[0], rate: rate, channels: channels,
                          contextPath: contextPath)
        } catch {
            die("apple_live error: \(error)", code: 3)
        }
    }

    static func run(localeID: String, rate: Double, channels: UInt32,
                    contextPath: String?) async throws {
        let requested = Locale(identifier: localeID)
        let locale = await SpeechTranscriber.supportedLocale(equivalentTo: requested) ?? requested

        let transcriber = SpeechTranscriber(
            locale: locale,
            transcriptionOptions: [],
            reportingOptions: [.volatileResults, .fastResults],
            attributeOptions: []
        )
        if let request = try await AssetInventory.assetInstallationRequest(supporting: [transcriber]) {
            try await request.downloadAndInstall()
        }

        // Bias recognition toward meeting-specific names and vocabulary.
        let context = AnalysisContext()
        if let path = contextPath,
           let data = FileManager.default.contents(atPath: path),
           let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
           let strings = obj["strings"] as? [String], !strings.isEmpty {
            context.contextualStrings[.general] = strings
        }

        // Source: interleaved int16 from stdin. Destination: whatever the
        // analyzer prefers.
        guard let srcFormat = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: rate,
                                            channels: AVAudioChannelCount(channels), interleaved: true) else {
            die("unsupported source format")
        }
        guard let dstFormat = await SpeechAnalyzer.bestAvailableAudioFormat(
                compatibleWith: [transcriber], considering: srcFormat) else {
            die("no compatible analyzer audio format", code: 3)
        }
        guard let converter = AVAudioConverter(from: srcFormat, to: dstFormat) else {
            die("cannot convert \(srcFormat) -> \(dstFormat)", code: 3)
        }

        let (inputStream, continuation) = AsyncStream<AnalyzerInput>.makeStream()
        let analyzer = SpeechAnalyzer(inputSequence: inputStream, modules: [transcriber],
                                      analysisContext: context)

        // Emit caption events as they stream in.
        let emitter = Task {
            for try await result in transcriber.results {
                let text = String(result.text.characters)
                    .trimmingCharacters(in: .whitespacesAndNewlines)
                if text.isEmpty { continue }
                emit([
                    "t": result.isFinal ? "final" : "partial",
                    "start": (result.range.start.seconds * 100).rounded() / 100,
                    "end": (result.range.end.seconds * 100).rounded() / 100,
                    "text": text,
                ])
            }
        }

        // Feed stdin -> converter -> analyzer. ~0.25 s of audio per buffer.
        let feeder = Task.detached { () -> Void in
            let bytesPerFrame = Int(channels) * 2
            let framesPerChunk = Int(rate / 4)
            let chunkBytes = framesPerChunk * bytesPerFrame
            let stdin = FileHandle.standardInput
            var pending = Data()
            while true {
                guard let piece = try? stdin.read(upToCount: chunkBytes), !piece.isEmpty else { break }
                pending.append(piece)
                while pending.count >= chunkBytes {
                    let chunk = pending.prefix(chunkBytes)
                    pending.removeFirst(chunkBytes)
                    if let buf = convert(chunk, frames: framesPerChunk, srcFormat: srcFormat,
                                         dstFormat: dstFormat, converter: converter) {
                        continuation.yield(AnalyzerInput(buffer: buf))
                    }
                }
            }
            let leftoverFrames = pending.count / bytesPerFrame
            if leftoverFrames > 0,
               let buf = convert(pending.prefix(leftoverFrames * bytesPerFrame),
                                 frames: leftoverFrames, srcFormat: srcFormat,
                                 dstFormat: dstFormat, converter: converter) {
                continuation.yield(AnalyzerInput(buffer: buf))
            }
            continuation.finish()
        }

        _ = await feeder.value
        try await analyzer.finalizeAndFinishThroughEndOfInput()
        try? await emitter.value
        emit(["t": "done"])
    }

    static func convert(_ bytes: Data, frames: Int, srcFormat: AVAudioFormat,
                        dstFormat: AVAudioFormat, converter: AVAudioConverter) -> AVAudioPCMBuffer? {
        guard let src = AVAudioPCMBuffer(pcmFormat: srcFormat, frameCapacity: AVAudioFrameCount(frames)) else {
            return nil
        }
        src.frameLength = AVAudioFrameCount(frames)
        bytes.withUnsafeBytes { raw in
            if let base = raw.baseAddress, let dst = src.int16ChannelData {
                memcpy(dst[0], base, bytes.count)
            }
        }
        let ratio = dstFormat.sampleRate / srcFormat.sampleRate
        let outCapacity = AVAudioFrameCount((Double(frames) * ratio).rounded(.up) + 32)
        guard let out = AVAudioPCMBuffer(pcmFormat: dstFormat, frameCapacity: outCapacity) else {
            return nil
        }
        var fed = false
        var convError: NSError?
        converter.convert(to: out, error: &convError) { _, outStatus in
            if fed {
                outStatus.pointee = .noDataNow
                return nil
            }
            fed = true
            outStatus.pointee = .haveData
            return src
        }
        if convError != nil || out.frameLength == 0 { return nil }
        return out
    }
}
