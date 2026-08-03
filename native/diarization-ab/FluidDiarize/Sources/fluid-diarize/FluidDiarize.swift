// fluid-diarize — Candidate A adapter for the MeetingScribe diarization A/B.
//
// Takes one WAV path, runs FluidAudio's offline diarization pipeline
// (pyannote-style segmentation + embedding CoreML models, auto-downloaded from
// HuggingFace FluidInference repos on first run), and emits ONE JSON object.
//
// Output contract (consumed by ../score_ab.py — keep in sync):
//   {
//     "engine": "fluidaudio",
//     "engine_revision": "<git rev this wrapper pins>",
//     "wav": "<input path>",
//     "audio_seconds": <float>,
//     "speaker_count": <int>,        // distinct speakers in emitted segments
//     "reported_speaker_count": <int>,  // engine's own speakerDatabase count
//     "speakers": ["S1", ...],       // in order of first appearance
//     "segments": [ {"start": s, "end": s, "speaker": "S1", "speaker_idx": 0}, ... ],
//     "elapsed_seconds": <float>     // inference wall time, model load excluded
//   }
// speaker_idx is renumbered by order of first appearance (first voice heard
// = 0), matching diarization.cluster()'s label contract in the Python app.
// JSON goes to --out FILE (preferred; stdout can carry engine log noise).

import FluidAudio
import Foundation

let PINNED_REVISION = "5390df9752c8fc583596018360c5fd70d6fa6c75"

func die(_ msg: String) -> Never {
    FileHandle.standardError.write(("fluid-diarize: " + msg + "\n").data(using: .utf8)!)
    exit(1)
}

@main
struct Main {
    static func main() async {
        var wavPath: String?
        var numSpeakers: Int?
        var threshold: Float?
        var modelsDir: String?
        var outPath: String?

        var it = CommandLine.arguments.dropFirst().makeIterator()
        func next(_ flag: String) -> String {
            guard let v = it.next() else { die("\(flag) needs a value") }
            return v
        }
        while let a = it.next() {
            switch a {
            case "--num-speakers": numSpeakers = Int(next(a))
            case "--threshold": threshold = Float(next(a))
            case "--models-dir": modelsDir = next(a)
            case "--out": outPath = next(a)
            case "--help", "-h":
                print("""
                usage: fluid-diarize <audio.wav> [--out results.json] [--num-speakers N]
                                     [--threshold F] [--models-dir DIR]
                Auto speaker count is the default (--num-speakers omitted).
                """)
                exit(0)
            default:
                if wavPath == nil, !a.hasPrefix("--") { wavPath = a } else { die("unknown argument \(a)") }
            }
        }
        guard let wavPath, FileManager.default.fileExists(atPath: wavPath) else {
            die("audio file missing — usage: fluid-diarize <audio.wav> [--out results.json]")
        }

        var config = DiarizerConfig()
        if let numSpeakers { config.numClusters = numSpeakers }
        if let threshold { config.clusteringThreshold = threshold }

        do {
            let manager = DiarizerManager(config: config)
            let dir = modelsDir.map { URL(fileURLWithPath: $0) }
            let models = try await DiarizerModels.downloadIfNeeded(to: dir)
            manager.initialize(models: models)

            let samples = try AudioConverter().resampleAudioFile(path: wavPath)
            let audioSeconds = Double(samples.count) / 16000.0

            let t0 = Date()
            let result = try manager.performCompleteDiarization(samples, sampleRate: 16000)
            let elapsed = Date().timeIntervalSince(t0)

            // Renumber by first appearance, in time order.
            let ordered = result.segments.sorted { $0.startTimeSeconds < $1.startTimeSeconds }
            var idxOf: [String: Int] = [:]
            var speakers: [String] = []
            var segs: [[String: Any]] = []
            for s in ordered {
                if idxOf[s.speakerId] == nil {
                    idxOf[s.speakerId] = speakers.count
                    speakers.append(s.speakerId)
                }
                segs.append([
                    "start": Double(s.startTimeSeconds),
                    "end": Double(s.endTimeSeconds),
                    "speaker": s.speakerId,
                    "speaker_idx": idxOf[s.speakerId]!,
                ])
            }

            let payload: [String: Any] = [
                "engine": "fluidaudio",
                "engine_revision": PINNED_REVISION,
                "wav": wavPath,
                "audio_seconds": audioSeconds,
                "speaker_count": speakers.count,
                "reported_speaker_count": result.speakerDatabase?.count ?? speakers.count,
                "speakers": speakers,
                "segments": segs,
                "elapsed_seconds": elapsed,
            ]
            let data = try JSONSerialization.data(
                withJSONObject: payload, options: [.sortedKeys, .prettyPrinted])
            if let outPath {
                try data.write(to: URL(fileURLWithPath: outPath))
                FileHandle.standardError.write(
                    "fluid-diarize: \(speakers.count) speakers, \(segs.count) segments -> \(outPath)\n"
                        .data(using: .utf8)!)
            } else {
                print(String(data: data, encoding: .utf8)!)
            }
        } catch {
            die("failed: \(error)")
        }
    }
}
