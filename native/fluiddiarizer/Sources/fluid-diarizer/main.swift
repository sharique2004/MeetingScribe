// fluid-diarizer — MeetingScribe's speaker-turn engine.
//
// Takes one WAV path, runs FluidAudio's OFFLINE diarization pipeline (the
// CoreML port of pyannote community-1: powerset segmentation + WeSpeaker
// embeddings + PLDA/VBx clustering) at the vendor's `.community` presets,
// and emits ONE JSON object. The streaming pipeline is deliberately not
// reachable from here — it was measured 3.6x worse on this exact corpus
// (native/diarization-ab/CORRECTION.md).
//
// Output contract (consumed by diarization_neural.py — keep in sync):
//   {
//     "engine": "fluidaudio-offline",
//     "engine_revision": "<git rev Package.swift pins>",
//     "threshold": <float>,
//     "num_speakers": <int or null>,     // the forced count, when given
//     "audio_seconds": <float>,
//     "speaker_count": <int>,            // distinct speakers in emitted segments
//     "speakers": ["S1", ...],           // in order of first appearance
//     "speaker_seconds": {"S1": <float>, ...},
//     "segments": [ {"start": s, "end": s, "speaker_idx": 0}, ... ],
//     "elapsed_seconds": <float>         // inference wall time, model load excluded
//   }
// speaker_idx is renumbered by order of first appearance (first voice heard
// = 0), matching diarization.cluster()'s label contract in the Python app.
// JSON goes to --out FILE (stdout carries engine log noise).

import FluidAudio
import Foundation

let PINNED_REVISION = "5390df9752c8fc583596018360c5fd70d6fa6c75"

func die(_ msg: String) -> Never {
    FileHandle.standardError.write(("fluid-diarizer: " + msg + "\n").data(using: .utf8)!)
    exit(1)
}

var wavPath: String?
var numSpeakers: Int?
var threshold = 0.7
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
    case "--threshold": threshold = Double(next(a)) ?? threshold
    case "--models-dir": modelsDir = next(a)
    case "--out": outPath = next(a)
    case "--help", "-h":
        print("""
        usage: fluid-diarizer <audio.wav> --out results.json
                              [--num-speakers N] [--threshold F] [--models-dir DIR]
        Offline community-1 diarization. Auto speaker count unless --num-speakers.
        """)
        exit(0)
    default:
        if wavPath == nil, !a.hasPrefix("--") { wavPath = a } else { die("unknown argument \(a)") }
    }
}
guard let wavPath, FileManager.default.fileExists(atPath: wavPath) else {
    die("audio file missing — usage: fluid-diarizer <audio.wav> --out results.json")
}

let semaphore = DispatchSemaphore(value: 0)

Task {
    do {
        var clustering = OfflineDiarizerConfig.Clustering.community
        clustering.threshold = threshold
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

        let samples = try AudioConverter().resampleAudioFile(path: wavPath)
        let audioSeconds = Double(samples.count) / 16000.0

        let t0 = Date()
        let result = try await manager.process(audio: samples)
        let elapsed = Date().timeIntervalSince(t0)

        // Renumber by first appearance, in time order.
        let ordered = result.segments.sorted { $0.startTimeSeconds < $1.startTimeSeconds }
        var idxOf: [String: Int] = [:]
        var speakers: [String] = []
        var seconds: [String: Double] = [:]
        var segs: [[String: Any]] = []
        for s in ordered {
            if idxOf[s.speakerId] == nil {
                idxOf[s.speakerId] = speakers.count
                speakers.append(s.speakerId)
            }
            seconds[s.speakerId, default: 0] += Double(s.endTimeSeconds - s.startTimeSeconds)
            segs.append([
                "start": Double(s.startTimeSeconds),
                "end": Double(s.endTimeSeconds),
                "speaker_idx": idxOf[s.speakerId]!,
            ])
        }

        let payload: [String: Any] = [
            "engine": "fluidaudio-offline",
            "engine_revision": PINNED_REVISION,
            "threshold": threshold,
            "num_speakers": numSpeakers as Any,
            "audio_seconds": audioSeconds,
            "speaker_count": speakers.count,
            "speakers": speakers,
            "speaker_seconds": seconds.mapValues { (($0 * 100).rounded()) / 100 },
            "segments": segs,
            "elapsed_seconds": elapsed,
        ]
        let data = try JSONSerialization.data(
            withJSONObject: payload, options: [.sortedKeys])
        if let outPath {
            try data.write(to: URL(fileURLWithPath: outPath))
            FileHandle.standardError.write(
                "fluid-diarizer: \(speakers.count) speakers, \(segs.count) segments -> \(outPath)\n"
                    .data(using: .utf8)!)
        } else {
            print(String(data: data, encoding: .utf8)!)
        }
        semaphore.signal()
    } catch {
        FileHandle.standardError.write(
            ("fluid-diarizer: failed: \(error)\n").data(using: .utf8)!)
        exit(1)
    }
}

semaphore.wait()
exit(0)
