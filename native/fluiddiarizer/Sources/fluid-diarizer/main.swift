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
//     "config_fingerprint": "<16 hex chars>",  // see FINGERPRINT below
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
//
// EVERY FLAG BELOW IS OPTIONAL AND DEFAULTS TO THE `.community` PRESET, so a
// default invocation (`fluid-diarizer a.wav --out o.json [--threshold F]
// [--models-dir D] [--num-speakers N]`) produces exactly the segments it did
// before these knobs existed. Pass-throughs, with the vendor default each one
// keeps when omitted:
//   --min-speakers N          clustering.minSpeakers            (nil = no floor)
//   --max-speakers N          clustering.maxSpeakers            (nil = no ceiling)
//                             both are IGNORED by the vendor when --num-speakers
//                             is also given (numSpeakers wins, pyannote's rule)
//   --zero-vote-reembed       zeroVoteReembed.enabled = true    (default false);
//                             re-embeds timeline spans that got zero cluster
//                             votes instead of tie-breaking them to cluster 0
//   --step-ratio F            segmentation.stepRatio            (0.2)
//   --min-segment-duration F  embedding.minSegmentDurationSeconds (1.0). NOTE:
//                             this is the EMBEDDING stage's minimum, not a
//                             segmentation field — at this pin Segmentation has
//                             only minDurationOn/minDurationOff, which the
//                             powerset community-1 model ignores.
//
// EMBEDDINGS (--embeddings PATH). Turns on `exposeChunkEmbeddings` and writes a
// SECOND JSON — the turns file at --out is unaffected (the vendor builds the
// chunk-embedding array after segments are final; it is a view, not an input):
//   {
//     "chunk_embeddings": [
//       {"speaker": <int>, "start": s, "end": s,
//        "embedding256": [256 floats],   // L2-normalized WeSpeaker embedding
//        "rho128": [128 doubles]}        // PLDA-whitened; [] with no PLDA model
//     ],
//     "speaker_database": {"<speaker int>": [centroid floats]}
//   }
// "speaker" uses the SAME first-appearance renumbering as the turns file, so ids
// join across the two. A cluster that owns chunk embeddings but wins no emitted
// segment cannot get an id from the turns pass; those are numbered after the
// turn speakers, in the order the vendor's chunk array first mentions them (a
// deterministic, time-ordered array), so ids stay stable and collision-free.
//
// FINGERPRINT. "config_fingerprint" is the first 16 hex chars of SHA-256 over
// the pinned revision plus every config field this run actually fed the engine,
// canonicalized as sorted `key=value` lines joined by "\n". It exists so a
// cached turn set can be invalidated when the engine's *inputs* change.
// Deliberately NOT in the digest: `exposeChunkEmbeddings` and
// `export.embeddingsPath`, which change what is REPORTED, never what is
// computed — a --embeddings run must fingerprint the same as the plain run that
// produced the same turns.

import CryptoKit
import FluidAudio
import Foundation

let PINNED_REVISION = "5390df9752c8fc583596018360c5fd70d6fa6c75"

func die(_ msg: String) -> Never {
    FileHandle.standardError.write(("fluid-diarizer: " + msg + "\n").data(using: .utf8)!)
    exit(1)
}

/// Canonical digest of everything that steers the engine's output. See
/// FINGERPRINT in the header for what is in and what is out.
func configFingerprint(_ c: OfflineDiarizerConfig, revision: String) -> String {
    func opt(_ v: Int?) -> String { v.map(String.init) ?? "nil" }
    let skip: String
    switch c.embedding.skipStrategy {
    case .none: skip = "none"
    case .maskSimilarity(let t): skip = "maskSimilarity(\(t))"
    }
    let fields: [String: String] = [
        "revision": revision,
        "segmentation.windowDurationSeconds": String(c.segmentation.windowDurationSeconds),
        "segmentation.sampleRate": String(c.segmentation.sampleRate),
        "segmentation.minDurationOn": String(c.segmentation.minDurationOn),
        "segmentation.minDurationOff": String(c.segmentation.minDurationOff),
        "segmentation.stepRatio": String(c.segmentation.stepRatio),
        "segmentation.speechOnsetThreshold": String(c.segmentation.speechOnsetThreshold),
        "segmentation.speechOffsetThreshold": String(c.segmentation.speechOffsetThreshold),
        "embedding.batchSize": String(c.embedding.batchSize),
        "embedding.excludeOverlap": String(c.embedding.excludeOverlap),
        "embedding.minSegmentDurationSeconds": String(c.embedding.minSegmentDurationSeconds),
        "embedding.skipStrategy": skip,
        "clustering.threshold": String(c.clustering.threshold),
        "clustering.warmStartFa": String(c.clustering.warmStartFa),
        "clustering.warmStartFb": String(c.clustering.warmStartFb),
        "clustering.minSpeakers": opt(c.clustering.minSpeakers),
        "clustering.maxSpeakers": opt(c.clustering.maxSpeakers),
        "clustering.numSpeakers": opt(c.clustering.numSpeakers),
        "vbx.maxIterations": String(c.vbx.maxIterations),
        "vbx.convergenceTolerance": String(c.vbx.convergenceTolerance),
        "postProcessing.minGapDurationSeconds": String(c.postProcessing.minGapDurationSeconds),
        "postProcessing.exclusiveSegments": String(c.postProcessing.exclusiveSegments),
        "zeroVoteReembed.enabled": String(c.zeroVoteReembed.enabled),
        "zeroVoteReembed.minDurationSeconds": String(c.zeroVoteReembed.minDurationSeconds),
    ]
    let canonical = fields.keys.sorted()
        .map { "\($0)=\(fields[$0]!)" }
        .joined(separator: "\n")
    let digest = SHA256.hash(data: Data(canonical.utf8))
    return String(digest.map { String(format: "%02x", $0) }.joined().prefix(16))
}

/// The --embeddings sidecar. Encoded with JSONEncoder (not JSONSerialization)
/// so the 256-wide Float vectors print as short round-trip literals instead of
/// their double-widened expansions.
struct EmbeddingsPayload: Encodable {
    struct Chunk: Encodable {
        let speaker: Int
        let start: Double
        let end: Double
        let embedding256: [Float]
        let rho128: [Double]
    }
    let chunk_embeddings: [Chunk]  // swiftlint:disable:this identifier_name
    let speaker_database: [String: [Float]]  // swiftlint:disable:this identifier_name
}

var wavPath: String?
var numSpeakers: Int?
var threshold = 0.7
var modelsDir: String?
var outPath: String?
var embeddingsPath: String?
var minSpeakers: Int?
var maxSpeakers: Int?
var zeroVoteReembed = false
var stepRatio: Double?
var minSegmentDuration: Double?

var it = CommandLine.arguments.dropFirst().makeIterator()
func next(_ flag: String) -> String {
    guard let v = it.next() else { die("\(flag) needs a value") }
    return v
}
func nextDouble(_ flag: String) -> Double {
    let raw = next(flag)
    guard let v = Double(raw) else { die("\(flag) needs a number, got \(raw)") }
    return v
}
func nextInt(_ flag: String) -> Int {
    let raw = next(flag)
    guard let v = Int(raw) else { die("\(flag) needs an integer, got \(raw)") }
    return v
}
while let a = it.next() {
    switch a {
    case "--num-speakers": numSpeakers = Int(next(a))
    case "--threshold": threshold = Double(next(a)) ?? threshold
    case "--models-dir": modelsDir = next(a)
    case "--out": outPath = next(a)
    case "--embeddings": embeddingsPath = next(a)
    case "--min-speakers": minSpeakers = nextInt(a)
    case "--max-speakers": maxSpeakers = nextInt(a)
    case "--zero-vote-reembed": zeroVoteReembed = true
    case "--step-ratio": stepRatio = nextDouble(a)
    case "--min-segment-duration": minSegmentDuration = nextDouble(a)
    case "--help", "-h":
        print("""
        usage: fluid-diarizer <audio.wav> --out results.json
                              [--num-speakers N] [--threshold F] [--models-dir DIR]
                              [--embeddings FILE]
                              [--min-speakers N] [--max-speakers N]
                              [--zero-vote-reembed]
                              [--step-ratio F] [--min-segment-duration F]
        Offline community-1 diarization. Auto speaker count unless --num-speakers.
        Every optional flag defaults to the vendor's .community preset, so
        omitting them all reproduces the shipped behaviour exactly.
        --embeddings writes per-chunk speaker embeddings + centroids to a second
        JSON, keyed by the same speaker ids the turns file uses.
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
        var segmentation = OfflineDiarizerConfig.Segmentation.community
        if let stepRatio { segmentation.stepRatio = stepRatio }
        var embedding = OfflineDiarizerConfig.Embedding.community
        if let minSegmentDuration { embedding.minSegmentDurationSeconds = minSegmentDuration }
        var clustering = OfflineDiarizerConfig.Clustering.community
        clustering.threshold = threshold
        if let numSpeakers { clustering.numSpeakers = numSpeakers }
        if let minSpeakers { clustering.minSpeakers = minSpeakers }
        if let maxSpeakers { clustering.maxSpeakers = maxSpeakers }
        let config = OfflineDiarizerConfig(
            segmentation: segmentation,
            embedding: embedding,
            clustering: clustering,
            vbx: .community,
            postProcessing: .community,
            zeroVoteReembed: zeroVoteReembed
                ? OfflineDiarizerConfig.ZeroVoteReembed(enabled: true)
                : .disabled,
            exposeChunkEmbeddings: embeddingsPath != nil
        )
        let fingerprint = configFingerprint(config, revision: PINNED_REVISION)
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
            "config_fingerprint": fingerprint,
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

        if let embeddingsPath {
            // Clusters absent from the turns pass keep going where the turns
            // numbering left off, first-mention order (see header).
            var extra = idxOf
            var chunks: [EmbeddingsPayload.Chunk] = []
            for c in result.chunkEmbeddings ?? [] {
                let idx: Int
                if let known = extra[c.speakerId] {
                    idx = known
                } else {
                    idx = extra.count
                    extra[c.speakerId] = idx
                }
                chunks.append(
                    EmbeddingsPayload.Chunk(
                        speaker: idx,
                        start: c.startTimeSeconds,
                        end: c.endTimeSeconds,
                        embedding256: c.embedding256,
                        rho128: c.rho128))
            }
            var database: [String: [Float]] = [:]
            // Sorted so a centroid for a cluster with no turns and no chunk
            // embeddings (belt-and-braces) still numbers deterministically.
            for (speakerId, centroid) in (result.speakerDatabase ?? [:]).sorted(by: { $0.key < $1.key }) {
                let idx: Int
                if let known = extra[speakerId] {
                    idx = known
                } else {
                    idx = extra.count
                    extra[speakerId] = idx
                }
                database[String(idx)] = centroid
            }
            let encoder = JSONEncoder()
            encoder.outputFormatting = [.sortedKeys]
            let embeddingsData = try encoder.encode(
                EmbeddingsPayload(chunk_embeddings: chunks, speaker_database: database))
            try embeddingsData.write(to: URL(fileURLWithPath: embeddingsPath))
            FileHandle.standardError.write(
                ("fluid-diarizer: \(chunks.count) chunk embeddings, \(database.count) centroids "
                    + "-> \(embeddingsPath)\n").data(using: .utf8)!)
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
