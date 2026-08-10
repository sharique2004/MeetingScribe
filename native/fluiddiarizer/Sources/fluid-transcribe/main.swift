// fluid-transcribe — the CoreML ASR arm of the Parakeet INT8 A/B.
//
// EVAL-ONLY. Nothing in the shipping app calls this: it exists so
// native/asr-ab/score_asr.py can put FluidAudio's CoreML Parakeet (INT8 and
// INT4 encoders, on the Neural Engine) on the same table as the shipping
// MLX/GPU arm. It lives in this package because that is where the FluidAudio
// pin lives — the SAME pin the diarization A/B validated, unchanged.
//
// IT MUST NOT PERTURB fluid-diarizer. This is a second executableTarget with
// its own sources; the diarizer target, its sources, and the dependency pin are
// untouched, so `swift build` reproduces a byte-identical fluid-diarizer. That
// is verified by hash in native/asr-ab/RESULTS.md, not asserted here.
//
// Output contract (consumed by score_asr.py — keep in sync):
//   {
//     "engine": "fluidaudio-parakeet-v3-coreml",
//     "engine_revision": "<the git rev Package.swift pins>",
//     "precision": "int8" | "int4",   // ENCODER precision; decoder/joint are as shipped
//     "audio_seconds": <float>,
//     "elapsed_seconds": <float>,     // inference only, model load and download EXCLUDED
//     "text": "<the whole transcript>",
//     "tokens": [ {"t": "<piece>", "id": <int>, "s": <sec>, "e": <sec>, "c": <conf>}, ... ]
//   }
// "t" is the RAW SentencePiece piece, boundary marker and all ("▁the"). It is
// deliberately not normalized here: the Python side rebuilds parakeet-mlx
// AlignedTokens from these pieces, and it needs the word boundaries the marker
// carries. Normalizing at this end would throw away the only signal that says
// where one word stops and the next starts.
//
// Usage:
//   fluid-transcribe <audio> --out result.json
//                    [--models-dir DIR] [--language en] [--precision int8|int4]
//
// --models-dir is the models ROOT, the same convention fluid-diarizer uses
// (diarization_neural.py passes MODELS_DIR/"fluid-diarization"): the repo's own
// folder is appended under it, so checkpoints land inside the repo's models/
// tree instead of ~/Library/Application Support.

import AVFoundation
import FluidAudio
import Foundation

let PINNED_REVISION = "5390df9752c8fc583596018360c5fd70d6fa6c75"

func die(_ msg: String) -> Never {
    FileHandle.standardError.write(("fluid-transcribe: " + msg + "\n").data(using: .utf8)!)
    exit(1)
}

var audioPath: String?
var outPath: String?
var modelsDir: String?
var languageCode: String?
var precisionRaw = "int8"

var it = CommandLine.arguments.dropFirst().makeIterator()
func next(_ flag: String) -> String {
    guard let v = it.next() else { die("\(flag) needs a value") }
    return v
}
while let a = it.next() {
    switch a {
    case "--out": outPath = next(a)
    case "--models-dir": modelsDir = next(a)
    case "--language": languageCode = next(a)
    case "--precision": precisionRaw = next(a)
    case "--help", "-h":
        print(
            """
            usage: fluid-transcribe <audio> --out result.json
                                    [--models-dir DIR] [--language en]
                                    [--precision int8|int4]
            Offline Parakeet TDT v3 (CoreML, Neural Engine) transcription.
            --precision selects the ENCODER checkpoint; everything else is the
            vendor default at the pinned revision.
            """)
        exit(0)
    default:
        if audioPath == nil, !a.hasPrefix("--") { audioPath = a } else { die("unknown argument \(a)") }
    }
}

guard let audioPath, FileManager.default.fileExists(atPath: audioPath) else {
    die("audio file missing — usage: fluid-transcribe <audio> --out result.json")
}
guard let precision = ParakeetEncoderPrecision(rawValue: precisionRaw) else {
    die("--precision must be int8 or int4, got \(precisionRaw)")
}
// An unknown language code is a typo, not a request for "no hint": silently
// dropping it would run a DIFFERENT decode than the one that was asked for and
// report it under the requested name.
var language: Language?
if let languageCode {
    guard let parsed = Language(rawValue: languageCode) else {
        die("--language \(languageCode) is not a language this build knows")
    }
    language = parsed
}

/// Where this run's checkpoints live, given the models ROOT.
///
/// AsrModels.download/load want the REPO directory (they derive the parent from
/// it and let ModelHub re-append the repo folder). The vendor already knows the
/// mapping from repo to folder, so the folder name is taken from
/// `defaultCacheDirectory` relative to the default root rather than hardcoded —
/// a repo whose folder name changes upstream then still lands in the right
/// place instead of quietly creating a second cache.
func repoDirectory(underRoot root: String?) -> URL? {
    guard let root else { return nil }
    let defaultRoot = MLModelConfigurationUtils.defaultModelsDirectory().standardizedFileURL.path
    let defaultRepo = AsrModels.defaultCacheDirectory(for: .v3).standardizedFileURL.path
    guard defaultRepo.hasPrefix(defaultRoot + "/") else {
        die("cannot locate the repo folder under the models root (vendor layout changed)")
    }
    let relative = String(defaultRepo.dropFirst(defaultRoot.count + 1))
    return URL(fileURLWithPath: root).appendingPathComponent(relative)
}

let semaphore = DispatchSemaphore(value: 0)

Task {
    do {
        let audioURL = URL(fileURLWithPath: audioPath)

        // Duration is read from the file header, NOT by decoding the audio into
        // an array: this binary's peak RSS is one of the numbers the A/B
        // compares, and materialising 111 minutes of float samples just to
        // divide by the sample rate would put ~400 MB of measurement artifact
        // into the thing being measured.
        let file = try AVAudioFile(forReading: audioURL)
        let audioSeconds = Double(file.length) / file.processingFormat.sampleRate

        let directory = repoDirectory(underRoot: modelsDir)
        _ = try await AsrModels.download(
            to: directory, version: .v3, encoderPrecision: precision)
        let models = try await AsrModels.load(
            from: directory ?? AsrModels.defaultCacheDirectory(for: .v3),
            version: .v3, encoderPrecision: precision)

        let manager = AsrManager(config: .default, models: models)
        var decoderState = try TdtDecoderState(decoderLayers: 2)

        // The clock starts AFTER load, so RTF measures transcription and not a
        // cold model compile. Model load is a once-per-process cost the app
        // amortises across meetings; charging it to a 22-second fixture and not
        // to a 111-minute one would rank the arms by fixture length.
        let t0 = Date()
        let result = try await manager.transcribe(
            audioURL, decoderState: &decoderState, language: language)
        let elapsed = Date().timeIntervalSince(t0)

        var tokens: [[String: Any]] = []
        for timing in result.tokenTimings ?? [] {
            tokens.append([
                "t": timing.token,
                "id": timing.tokenId,
                "s": timing.startTime,
                "e": timing.endTime,
                "c": Double(timing.confidence),
            ])
        }

        let payload: [String: Any] = [
            "engine": "fluidaudio-parakeet-v3-coreml",
            "engine_revision": PINNED_REVISION,
            "precision": precision.rawValue,
            "audio_seconds": audioSeconds,
            "elapsed_seconds": elapsed,
            "text": result.text,
            "tokens": tokens,
        ]
        let data = try JSONSerialization.data(withJSONObject: payload, options: [.sortedKeys])
        if let outPath {
            try data.write(to: URL(fileURLWithPath: outPath))
            FileHandle.standardError.write(
                ("fluid-transcribe: \(tokens.count) tokens, \(String(format: "%.1f", audioSeconds))s "
                    + "audio in \(String(format: "%.1f", elapsed))s -> \(outPath)\n")
                    .data(using: .utf8)!)
        } else {
            print(String(data: data, encoding: .utf8)!)
        }
        semaphore.signal()
    } catch {
        FileHandle.standardError.write(
            ("fluid-transcribe: failed: \(error)\n").data(using: .utf8)!)
        exit(1)
    }
}

semaphore.wait()
exit(0)
