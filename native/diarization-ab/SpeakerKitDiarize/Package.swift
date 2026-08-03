// swift-tools-version: 6.0
// Candidate B wrapper: Argmax OSS SpeakerKit (MIT) speaker diarization.
// The OSS package DOES expose diarization publicly (SpeakerKit.diarize +
// full Pyannote pipeline under Sources/SpeakerKit/Pyannote/) — verified by
// source inspection at the pinned revision; see ../README.md.
// NOTE: tools-version must be >= 6.0 here — this machine's Command Line Tools
// (Swift 6.3.3, no Xcode) ships a ManifestAPI that no longer links 5.x
// manifests ("Undefined symbols ... PackageDescription.Package.__allocating_init").
import PackageDescription

let package = Package(
    name: "SpeakerKitDiarize",
    platforms: [.macOS(.v14)],
    dependencies: [
        .package(
            url: "https://github.com/argmaxinc/argmax-oss-swift.git",
            revision: "97d09fd9790393579d2834e2bc098deb3e26bc06"
        )
    ],
    targets: [
        .executableTarget(
            name: "speakerkit-diarize",
            dependencies: [
                .product(name: "SpeakerKit", package: "argmax-oss-swift"),
                .product(name: "WhisperKit", package: "argmax-oss-swift"),
            ]
        )
    ],
    swiftLanguageVersions: [.v5]
)
