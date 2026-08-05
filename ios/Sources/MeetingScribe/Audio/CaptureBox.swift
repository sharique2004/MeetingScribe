// The only thing the realtime audio thread is allowed to touch.
//
// AVAudioEngine calls an input tap on its render thread. A closure created
// inside a @MainActor type INHERITS that isolation, so when the engine calls
// it off-main it trips dispatch_assert_queue_fail and the app dies. The fix is
// structural, not cosmetic: capture state crosses to the audio thread through
// this nonisolated, lock-guarded box, and the tap closure never reads an
// actor-isolated stored property.
//
// Keep the work under the lock small. This runs on a realtime thread, and a
// slow tap is a glitchy recording.
import AVFoundation

final class CaptureBox: @unchecked Sendable {
    private let lock = NSLock()
    private var file: AVAudioFile?
    private var peak: Float = 0

    /// Setting a file starts writing; setting nil discards buffers, which is
    /// what lets the engine stay warm between recordings.
    func setFile(_ file: AVAudioFile?) {
        lock.lock(); defer { lock.unlock() }
        self.file = file
    }

    func write(_ buffer: AVAudioPCMBuffer) {
        let loudest = Self.peak(of: buffer)
        lock.lock(); defer { lock.unlock() }
        guard let file else { return }
        try? file.write(from: buffer)
        peak = max(peak, loudest)
        if loudest > 0 { heardSound = true }
    }

    /// The loudest sample since the last read, then reset. Drives the level
    /// meter without the UI ever reaching into the audio thread.
    func drainPeak() -> Float {
        lock.lock(); defer { lock.unlock() }
        let value = peak
        peak = 0
        return value
    }

    private var heardSound = false

    /// True once any written sample has been non-zero.
    ///
    /// This exists because of a bug the Mac side shipped: a denied capture
    /// permission does not raise, it writes a full-length file of digital
    /// zeros. 26 of 44 recordings on the development Mac were silent that way
    /// and nothing noticed until someone measured the samples. The phone gets
    /// the check for free, so it gets it from the start, and a recording that
    /// heard nothing says so instead of producing an empty transcript.
    var everCarriedSound: Bool {
        lock.lock(); defer { lock.unlock() }
        return heardSound
    }

    func resetSoundFlag() {
        lock.lock(); defer { lock.unlock() }
        heardSound = false
    }

    private static func peak(of buffer: AVAudioPCMBuffer) -> Float {
        guard let channels = buffer.floatChannelData else { return 0 }
        let frames = Int(buffer.frameLength)
        var loudest: Float = 0
        for channel in 0..<Int(buffer.format.channelCount) {
            let samples = channels[channel]
            for frame in 0..<frames {
                loudest = max(loudest, abs(samples[frame]))
            }
        }
        return loudest
    }
}

/// Installed from a nonisolated FREE function so the closure does not inherit
/// @MainActor. Do not "simplify" this by calling installTap inside the
/// AudioCaptureService methods: that reintroduces the isolation and the crash.
nonisolated func installCaptureTap(on input: AVAudioInputNode,
                                   format: AVAudioFormat,
                                   into box: CaptureBox) {
    input.installTap(onBus: 0, bufferSize: 4096, format: format) { buffer, _ in
        box.write(buffer)
    }
}
