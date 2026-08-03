// swift-tools-version: 6.0
// Candidate A wrapper: FluidAudio (Apache-2.0) offline speaker diarization.
// Pinned to the exact revision whose API was inspected on 2026-08-02.
// NOTE: tools-version must be >= 6.0 here — this machine's Command Line Tools
// (Swift 6.3.3, no Xcode) ships a ManifestAPI that no longer links 5.x
// manifests ("Undefined symbols ... PackageDescription.Package.__allocating_init").
import PackageDescription

let package = Package(
    name: "FluidDiarize",
    platforms: [.macOS(.v14)],
    dependencies: [
        .package(
            url: "https://github.com/FluidInference/FluidAudio.git",
            revision: "5390df9752c8fc583596018360c5fd70d6fa6c75"
        )
    ],
    targets: [
        .executableTarget(
            name: "fluid-diarize",
            dependencies: [
                .product(name: "FluidAudio", package: "FluidAudio")
            ]
        )
    ],
    swiftLanguageVersions: [.v5]
)
