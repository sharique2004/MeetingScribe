// speakerkit-diarize — Candidate B adapter for the MeetingScribe diarization A/B.
//
// Takes one WAV path, runs Argmax OSS SpeakerKit's Pyannote diarization
// pipeline (segmenter + embedder + VBx clustering CoreML models, auto-
// downloaded from HuggingFace argmaxinc/speakerkit-coreml on first run), and
// emits ONE JSON object in the SAME contract as fluid-diarize — see the
// header of ../../FluidDiarize/Sources/fluid-diarize/FluidDiarize.swift.
//
// speaker_idx is renumbered by order of first appearance (first voice heard
// = 0), matching diarization.cluster()'s label contract in the Python app.

import Foundation
import SpeakerKit
import WhisperKit

let PINNED_REVISION = "97d09fd9790393579d2834e2bc098deb3e26bc06"

func die(_ msg: String) -> Never {
    FileHandle.standardError.write(("speakerkit-diarize: " + msg + "\n").data(using: .utf8)!)
    exit(1)
}

@main
struct Main {
    static func main() async {
        var wavPath: String?
        var numSpeakers: Int?
        var threshold: Float?
        var modelsDir: String?
        var downloadBase: String?
        var outPath: String?
        var verbose = false

        var it = CommandLine.arguments.dropFirst().makeIterator()
        func next(_ flag: String) -> String {
            guard let v = it.next() else { die("\(flag) needs a value") }
            return v
        }
        while let a = it.next() {
            switch a {
            case "--num-speakers": numSpeakers = Int(next(a))
            case "--threshold": threshold = Float(next(a))
            case "--models-dir": modelsDir = next(a)      // local models, skips download
            case "--download-base": downloadBase = next(a)  // where downloads land
            case "--out": outPath = next(a)
            case "--verbose": verbose = true
            case "--help", "-h":
                print("""
                usage: speakerkit-diarize <audio.wav> [--out results.json] [--num-speakers N]
                                          [--threshold F] [--models-dir DIR] [--download-base DIR]
                Auto speaker count is the default (--num-speakers omitted).
                """)
                exit(0)
            default:
                if wavPath == nil, !a.hasPrefix("--") { wavPath = a } else { die("unknown argument \(a)") }
            }
        }
        guard let wavPath, FileManager.default.fileExists(atPath: wavPath) else {
            die("audio file missing — usage: speakerkit-diarize <audio.wav> [--out results.json]")
        }

        do {
            // 16 kHz mono Float samples (WhisperKit's loader resamples).
            let frames = try AudioProcessor.loadAudioAsFloatArray(fromPath: wavPath)
            let audioSeconds = Double(frames.count) / 16000.0

            // Mirrors argmax-cli's `diarize` subcommand setup (DiarizeCLI.swift).
            let config = PyannoteConfig(
                downloadBase: modelsDir == nil ? downloadBase : nil,
                modelRepo: "argmaxinc/speakerkit-coreml",
                modelToken: nil,
                modelFolder: modelsDir,
                download: modelsDir == nil,
                verbose: verbose,
                logLevel: .debug
            )
            let speakerKit = try await SpeakerKit(config)

            let options = PyannoteDiarizationOptions(
                numberOfSpeakers: numSpeakers,
                clusterDistanceThreshold: threshold ?? 0.6  // DiarizeCLI default
            )

            let t0 = Date()
            var result = try await speakerKit.diarize(audioArray: frames, options: options)
            let elapsed = Date().timeIntervalSince(t0)

            // Same normalisation SpeakerKit.generateRTTM applies before output.
            result.updateSegments(minActiveOffset: 0.0)

            let ordered = result.segments.sorted { $0.startTime < $1.startTime }
            var idxOf: [String: Int] = [:]
            var speakers: [String] = []
            var segs: [[String: Any]] = []
            for s in ordered {
                let label: String
                switch s.speaker {
                case .speakerId(let id): label = "S\(id)"
                case .multiple(let ids): label = "multi(" + ids.map(String.init).joined(separator: ",") + ")"
                case .noMatch: label = "noMatch"
                }
                if idxOf[label] == nil {
                    idxOf[label] = speakers.count
                    speakers.append(label)
                }
                segs.append([
                    "start": Double(s.startTime),
                    "end": Double(s.endTime),
                    "speaker": label,
                    "speaker_idx": idxOf[label]!,
                ])
            }
            // Count only real speaker identities, not noMatch/multi buckets —
            // that is what a "how many voices" answer means.
            let realSpeakers = Set(ordered.compactMap { $0.speaker.speakerId })

            let payload: [String: Any] = [
                "engine": "speakerkit",
                "engine_revision": PINNED_REVISION,
                "wav": wavPath,
                "audio_seconds": audioSeconds,
                "speaker_count": realSpeakers.count,
                "reported_speaker_count": result.speakerCount,
                "speakers": speakers,
                "segments": segs,
                "elapsed_seconds": elapsed,
            ]
            let data = try JSONSerialization.data(
                withJSONObject: payload, options: [.sortedKeys, .prettyPrinted])
            if let outPath {
                try data.write(to: URL(fileURLWithPath: outPath))
                FileHandle.standardError.write(
                    "speakerkit-diarize: \(realSpeakers.count) speakers, \(segs.count) segments -> \(outPath)\n"
                        .data(using: .utf8)!)
            } else {
                print(String(data: data, encoding: .utf8)!)
            }
        } catch {
            die("failed: \(error)")
        }
    }
}
