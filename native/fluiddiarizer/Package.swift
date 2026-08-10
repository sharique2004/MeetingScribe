// swift-tools-version: 6.0
// Production speaker-turn engine: FluidAudio (Apache-2.0) OFFLINE diarization —
// the CoreML port of pyannote community-1 (powerset segmentation + WeSpeaker
// embeddings + PLDA/VBx clustering), running on the Neural Engine.
//
// Pinned to the same revision the diarization A/B validated
// (native/diarization-ab, offline re-test of 2026-08-03). Bump deliberately,
// re-running that A/B — never as a side effect of `swift package update`.
//
// NOTE: tools-version must be >= 6.0 here — this machine's Command Line Tools
// (Swift 6.3.3, no Xcode) ships a ManifestAPI that no longer links 5.x
// manifests.
import PackageDescription

let package = Package(
    name: "FluidDiarizer",
    platforms: [.macOS(.v14)],
    dependencies: [
        .package(
            url: "https://github.com/FluidInference/FluidAudio.git",
            revision: "5390df9752c8fc583596018360c5fd70d6fa6c75"
        )
    ],
    targets: [
        .executableTarget(
            name: "fluid-diarizer",
            dependencies: [
                .product(name: "FluidAudio", package: "FluidAudio")
            ]
        ),
        // EVAL-ONLY, and deliberately a SEPARATE target rather than a flag on
        // the diarizer: the shipping binary must not grow a code path (or a
        // recompile) because an A/B needed a transcriber. Same package, same
        // FluidAudio pin, own sources — `swift build` still produces a
        // byte-identical fluid-diarizer, which native/asr-ab/RESULTS.md
        // verifies by hash. Nothing in the app invokes it.
        .executableTarget(
            name: "fluid-transcribe",
            dependencies: [
                .product(name: "FluidAudio", package: "FluidAudio")
            ]
        ),
    ],
    swiftLanguageVersions: [.v5]
)
