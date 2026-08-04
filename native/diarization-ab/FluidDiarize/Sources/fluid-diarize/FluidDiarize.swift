// fluid-diarize — Candidate A adapter for the MeetingScribe diarization A/B.
//
// Takes one WAV path and emits ONE JSON object. Two modes:
//
//   --mode offline (DEFAULT)  FluidAudio's OfflineDiarizerManager — the CoreML
//       port of pyannote community-1 (powerset segmentation + WeSpeaker
//       embeddings + PLDA/VBx clustering), at the vendor's `.community`
//       presets. This is the pipeline the vendor benchmarks at 10.6% DER on
//       AMI-SDM and the one CORRECTION.md's "fair re-test" requires.
//   --mode streaming          The chunked real-time DiarizerManager the
//       ORIGINAL A/B (commit 417ef04) mistakenly benchmarked as "offline"
//       (vendor's own AMI-SDM number for that path at these defaults: 38.2%
//       DER). Kept callable so the correction is reproducible, never the
//       default again.
//
// Output contract (consumed by ../score_ab.py — keep in sync):
//   {
//     "engine": "fluidaudio",
//     "engine_revision": "<git rev this wrapper pins>",
//     "mode": "offline" | "streaming",
//     "threshold": <float or null>,     // clustering threshold actually used
//     "wav": "<input path>",
//     "audio_seconds": <float>,
//     "speaker_count": <int>,           // distinct speakers in emitted segments
//     "reported_speaker_count": <int>,  // engine's own speakerDatabase count
//     "speakers": ["S1", ...],          // in order of first appearance
//     "speaker_seconds": {"S1": <float>, ...},  // summed segment time per label
//     "segments": [ {"start": s, "end": s, "speaker": "S1", "speaker_idx": 0}, ... ],
//     "elapsed_seconds": <float>        // inference wall time, model load excluded
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

struct Emitted {
    let segments: [(start: Double, end: Double, speaker: String)]
    let reportedCount: Int
    let elapsed: Double
}

@main
struct Main {
    static func main() async {
        var wavPath: String?
        var numSpeakers: Int?
        var threshold: Double?
        var modelsDir: String?
        var outPath: String?
        var mode = "offline"

        var it = CommandLine.arguments.dropFirst().makeIterator()
        func next(_ flag: String) -> String {
            guard let v = it.next() else { die("\(flag) needs a value") }
            return v
        }
        while let a = it.next() {
            switch a {
            case "--num-speakers": numSpeakers = Int(next(a))
            case "--threshold": threshold = Double(next(a))
            case "--models-dir": modelsDir = next(a)
            case "--out": outPath = next(a)
            case "--mode":
                mode = next(a)
                guard ["offline", "streaming"].contains(mode) else { die("--mode offline|streaming") }
            case "--help", "-h":
                print("""
                usage: fluid-diarize <audio.wav> [--out results.json] [--mode offline|streaming]
                                     [--num-speakers N] [--threshold F] [--models-dir DIR]
                Default mode is offline (community-1 CoreML: powerset segmentation +
                WeSpeaker + PLDA/VBx at the vendor's .community presets).
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

        do {
            let emitted: Emitted
            if mode == "offline" {
                emitted = try await runOffline(
                    wavPath: wavPath, numSpeakers: numSpeakers,
                    threshold: threshold, modelsDir: modelsDir)
            } else {
                emitted = try await runStreaming(
                    wavPath: wavPath, numSpeakers: numSpeakers,
                    threshold: threshold.map { Float($0) }, modelsDir: modelsDir)
            }

            let samples = try AudioConverter().resampleAudioFile(path: wavPath)
            let audioSeconds = Double(samples.count) / 16000.0

            // Renumber by first appearance, in time order.
            let ordered = emitted.segments.sorted { $0.start < $1.start }
            var idxOf: [String: Int] = [:]
            var speakers: [String] = []
            var seconds: [String: Double] = [:]
            var segs: [[String: Any]] = []
            for s in ordered {
                if idxOf[s.speaker] == nil {
                    idxOf[s.speaker] = speakers.count
                    speakers.append(s.speaker)
                }
                seconds[s.speaker, default: 0] += s.end - s.start
                segs.append([
                    "start": s.start,
                    "end": s.end,
                    "speaker": s.speaker,
                    "speaker_idx": idxOf[s.speaker]!,
                ])
            }

            let payload: [String: Any] = [
                "engine": "fluidaudio",
                "engine_revision": PINNED_REVISION,
                "mode": mode,
                "threshold": threshold as Any,
                "wav": wavPath,
                "audio_seconds": audioSeconds,
                "speaker_count": speakers.count,
                "reported_speaker_count": emitted.reportedCount,
                "speakers": speakers,
                "speaker_seconds": seconds.mapValues { (($0 * 100).rounded()) / 100 },
                "segments": segs,
                "elapsed_seconds": emitted.elapsed,
            ]
            let data = try JSONSerialization.data(
                withJSONObject: payload, options: [.sortedKeys, .prettyPrinted])
            if let outPath {
                try data.write(to: URL(fileURLWithPath: outPath))
                FileHandle.standardError.write(
                    "fluid-diarize[\(mode)]: \(speakers.count) speakers, \(segs.count) segments -> \(outPath)\n"
                        .data(using: .utf8)!)
            } else {
                print(String(data: data, encoding: .utf8)!)
            }
        } catch {
            die("failed: \(error)")
        }
    }

    // The pipeline CORRECTION.md's "fair re-test" requires: community-1 CoreML
    // offline (powerset segmentation + WeSpeaker + PLDA/VBx), vendor presets.
    static func runOffline(
        wavPath: String, numSpeakers: Int?, threshold: Double?, modelsDir: String?
    ) async throws -> Emitted {
        var clustering = OfflineDiarizerConfig.Clustering.community
        if let threshold { clustering.threshold = threshold }
        if let numSpeakers { clustering.numSpeakers = numSpeakers }
        let config = OfflineDiarizerConfig(
            segmentation: .community,
            embedding: .community,
            clustering: clustering,
            vbx: .community,
            postProcessing: .community
        )
        let manager = OfflineDiarizerManager(config: config)
        try await manager.prepareModels(directory: modelsDir.map { URL(fileURLWithPath: $0) })

        let t0 = Date()
        let result = try await manager.process(URL(fileURLWithPath: wavPath))
        let elapsed = Date().timeIntervalSince(t0)
        return Emitted(
            segments: result.segments.map {
                (Double($0.startTimeSeconds), Double($0.endTimeSeconds), $0.speakerId)
            },
            reportedCount: result.speakerDatabase?.count ?? Set(result.segments.map(\.speakerId)).count,
            elapsed: elapsed
        )
    }

    // The chunked real-time path the original A/B mistakenly ran. Kept only so
    // the correction is reproducible.
    static func runStreaming(
        wavPath: String, numSpeakers: Int?, threshold: Float?, modelsDir: String?
    ) async throws -> Emitted {
        var config = DiarizerConfig()
        if let numSpeakers { config.numClusters = numSpeakers }
        if let threshold { config.clusteringThreshold = threshold }

        let manager = DiarizerManager(config: config)
        let dir = modelsDir.map { URL(fileURLWithPath: $0) }
        let models = try await DiarizerModels.downloadIfNeeded(to: dir)
        manager.initialize(models: models)

        let samples = try AudioConverter().resampleAudioFile(path: wavPath)
        let t0 = Date()
        let result = try manager.performCompleteDiarization(samples, sampleRate: 16000)
        let elapsed = Date().timeIntervalSince(t0)
        return Emitted(
            segments: result.segments.map {
                (Double($0.startTimeSeconds), Double($0.endTimeSeconds), $0.speakerId)
            },
            reportedCount: result.speakerDatabase?.count ?? Set(result.segments.map(\.speakerId)).count,
            elapsed: elapsed
        )
    }
}
