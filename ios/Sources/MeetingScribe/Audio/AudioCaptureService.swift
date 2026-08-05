// Recording a meeting on the phone.
//
// WHAT THE PHONE CAN AND CANNOT DO, because it decides the whole product:
// iOS has no equivalent of the Mac's Core Audio process tap
// (AudioHardwareCreateProcessTap, macOS only), so there is no way to capture
// another app's audio. The phone records the MICROPHONE and nothing else.
// That makes it excellent for a meeting in a room and useless for silently
// capturing the far side of a call — and the app says so rather than
// producing a transcript with half the conversation missing.
//
// The session is .playAndRecord + the `audio` background mode, which is what
// keeps the mic alive when the screen locks mid-meeting. Interruptions (a
// call) and route changes (headphones pulled) are handled explicitly, because
// neither resumes on its own and a meeting that silently stopped recording is
// the worst failure this app has.
import AVFoundation
import Observation

@MainActor
@Observable
final class AudioCaptureService {
    enum CaptureError: LocalizedError {
        case micDenied
        case sessionUnavailable(String)

        var errorDescription: String? {
            switch self {
            case .micDenied:
                return "MeetingScribe needs the microphone to record a meeting. Turn it on in Settings ▸ MeetingScribe."
            case .sessionUnavailable(let why):
                return why
            }
        }
    }

    private(set) var isRecording = false
    private(set) var elapsed: TimeInterval = 0
    private(set) var level: Float = 0
    /// The recording is running and has heard nothing but digital zeros.
    private(set) var silent = false
    private(set) var interrupted = false

    private let engine = AVAudioEngine()
    private let box = CaptureBox()
    private var startedAt: Date?
    private var ticker: Task<Void, Never>?
    private var observers: [NSObjectProtocol] = []

    func start(writingTo url: URL) async throws {
        guard !isRecording else { return }
        guard await AVAudioApplication.requestRecordPermission() else {
            throw CaptureError.micDenied
        }

        let session = AVAudioSession.sharedInstance()
        do {
            // .playAndRecord rather than .record: .record silences nearly all
            // other system output, which on a phone means the user's own call
            // audio goes quiet. .spokenAudio tunes for speech and pauses for
            // another app's spoken prompt instead of ducking under it.
            try session.setCategory(.playAndRecord, mode: .spokenAudio,
                                    options: [.allowBluetoothHFP, .defaultToSpeaker])
            // Activated HERE, at capture start, not at launch: activating early
            // interrupts whatever else the phone was playing for no reason.
            try session.setActive(true)
        } catch {
            throw CaptureError.sessionUnavailable(
                "Another app is using the microphone. End that call and try again.")
        }

        let input = engine.inputNode
        // The node's ACTUAL format. Hardcoding a rate here produces garbled
        // writes when the hardware disagrees, which it does on Bluetooth.
        let format = input.outputFormat(forBus: 0)
        box.resetSoundFlag()
        box.setFile(try AVAudioFile(forWriting: url, settings: format.settings))

        // Off the main actor, through a nonisolated function, so the tap
        // closure is not @MainActor-isolated when the render thread calls it.
        // `box` is bound to a local first: capturing `self.box` would pull the
        // main-actor service itself into a closure that runs off-main.
        let box = self.box
        await withCheckedContinuation { continuation in
            DispatchQueue.global(qos: .userInitiated).async {
                installCaptureTap(on: input, format: format, into: box)
                continuation.resume()
            }
        }

        try engine.start()
        observe()
        startedAt = Date()
        isRecording = true
        silent = false
        interrupted = false
        startTicking()
    }

    /// Stops and reports whether the recording actually heard anything.
    @discardableResult
    func stop() -> (duration: TimeInterval, heardSound: Bool) {
        guard isRecording else { return (0, false) }
        let duration = elapsed
        let heard = box.everCarriedSound
        isRecording = false
        ticker?.cancel()
        ticker = nil
        stopObserving()

        let engine = self.engine
        let box = self.box
        // Teardown off the main thread: engine.stop(), removeTap and
        // setActive(false) can each block long enough to freeze the Stop
        // button, and .notifyOthersOnDeactivation adds cross-app calls on top.
        DispatchQueue.global(qos: .userInitiated).async {
            engine.inputNode.removeTap(onBus: 0)
            engine.stop()
            box.setFile(nil)
            try? AVAudioSession.sharedInstance()
                .setActive(false, options: .notifyOthersOnDeactivation)
        }
        return (duration, heard)
    }

    // MARK: - Keeping the recording honest

    private func startTicking() {
        ticker = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(for: .milliseconds(200))
                guard let self, self.isRecording else { return }
                if let startedAt { self.elapsed = Date().timeIntervalSince(startedAt) }
                self.level = self.box.drainPeak()
                // Ten seconds of nothing but zeros is not a quiet room, it is
                // a microphone that is not reaching us. Say so while the
                // meeting can still be saved.
                if self.elapsed > 10, !self.box.everCarriedSound {
                    self.silent = true
                }
            }
        }
    }

    /// A Notification is not Sendable, so the raw values are read out inside
    /// the observer block and only those cross to the main actor. Handing the
    /// notification itself over compiles under Swift 5 and is a data race
    /// under Swift 6.
    private func observe() {
        let centre = NotificationCenter.default
        observers.append(centre.addObserver(
            forName: AVAudioSession.interruptionNotification,
            object: AVAudioSession.sharedInstance(), queue: .main
        ) { [weak self] note in
            let raw = note.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt
            let option = note.userInfo?[AVAudioSessionInterruptionOptionKey] as? UInt
            MainActor.assumeIsolated { self?.handleInterruption(raw: raw, option: option) }
        })
        observers.append(centre.addObserver(
            forName: AVAudioSession.routeChangeNotification,
            object: AVAudioSession.sharedInstance(), queue: .main
        ) { [weak self] note in
            let raw = note.userInfo?[AVAudioSessionRouteChangeReasonKey] as? UInt
            MainActor.assumeIsolated { self?.handleRouteChange(raw: raw) }
        })
    }

    private func stopObserving() {
        observers.forEach(NotificationCenter.default.removeObserver)
        observers.removeAll()
    }

    private func handleInterruption(raw: UInt?, option: UInt?) {
        guard let raw, let type = AVAudioSession.InterruptionType(rawValue: raw) else { return }
        switch type {
        case .began:
            interrupted = true
            engine.pause()
        case .ended:
            // Resume ONLY when the system says so. Resuming unconditionally
            // fights whatever took the mic and loses.
            guard let option,
                  AVAudioSession.InterruptionOptions(rawValue: option).contains(.shouldResume)
            else { return }
            try? AVAudioSession.sharedInstance().setActive(true)
            try? engine.start()
            interrupted = false
        @unknown default:
            break
        }
    }

    private func handleRouteChange(raw: UInt?) {
        guard let raw,
              let reason = AVAudioSession.RouteChangeReason(rawValue: raw) else { return }
        // Losing the input mid-meeting is the one route change that matters
        // here: the engine keeps running against a device that is gone, and
        // every buffer after it is silence.
        if reason == .oldDeviceUnavailable {
            try? engine.start()
        }
    }
}
