// syscap_spike.swift — Core Audio process-tap capture spike for MeetingScribe (Gate 1).
//
// Proves that driverless system-audio capture via CATapDescription +
// AudioHardwareCreateProcessTap + aggregate device works on this machine
// (macOS 26 / Apple Silicon), producing real non-zero samples into a WAV.
//
// Call sequences adapted from two MIT-licensed references:
//   - insidegui/AudioCap  (ProcessTap.swift, CoreAudioUtils.swift)
//     https://github.com/insidegui/AudioCap
//   - makeusabrew/audiotee (AudioTapManager.swift, Utils.swift)
//     https://github.com/makeusabrew/audiotee
// and Apple's "Capturing system audio with Core Audio taps" sample.
//
// The four traps this spike deliberately codes around:
//   1. isExclusive inversion — CATapDescription(stereoGlobalTapButExcludeProcesses:)
//      sets the inverted "exclusive" semantics at init. Mutating .isExclusive after
//      init silences delivery. We never touch it.
//   2. Tap-as-main-sub-device — an aggregate whose only content is the tap (empty
//      sub-device list) delivers zero samples. The REAL current default output
//      device must be the main sub-device; the tap goes in the tap *list*
//      (kAudioAggregateDeviceTapListKey) with kAudioAggregateDeviceTapAutoStartKey.
//      (AudioCap pattern, ProcessTap.prepare(for:).)
//   3. nil dispatch queue — AudioDeviceCreateIOProcIDWithBlock(_, _, nil, block)
//      silently registers nothing on macOS 26 (Tahoe regression). We always pass a
//      real DispatchQueue.
//   4. Aggregate default output — if the user's default output is itself an
//      aggregate (e.g. a legacy "MeetingScribe Output" multi-output), aggregates
//      can't nest: unwrap it to its first real (non-aggregate) sub-device.
//
// Permission: the SystemAudioCaptureRequests TCC prompt ("System Audio Recording
// Only") fires on FIRST tap creation. There is NO public API to pre-check the
// grant; denial manifests as either a creation error or silent all-zero samples.
// This spike attempts creation, reports errors honestly, and prints guidance when
// the capture is digitally silent.
//
// Build:  swiftc -O -parse-as-library syscap_spike.swift -o syscap-spike
//         codesign -s - --force syscap-spike
// Run:    ./syscap-spike [--seconds 8] [--out capture.wav]
//         (play some audio while it runs, e.g. afplay a system sound in a loop)

import Foundation
import CoreAudio
import AudioToolbox

// MARK: - FourCC / OSStatus pretty-printing

func fourCCString(_ value: UInt32) -> String {
    let bytes = [UInt8((value >> 24) & 0xFF), UInt8((value >> 16) & 0xFF),
                 UInt8((value >> 8) & 0xFF), UInt8(value & 0xFF)]
    if bytes.allSatisfy({ $0 >= 0x20 && $0 < 0x7F }), let s = String(bytes: bytes, encoding: .ascii) {
        return s
    }
    return String(value)
}

func osStatusString(_ err: OSStatus) -> String {
    let cc = fourCCString(UInt32(bitPattern: err))
    return cc.count == 4 ? "'\(cc)' (\(err))" : "\(err)"
}

// MARK: - Core Audio property helpers (adapted from AudioCap CoreAudioUtils.swift, MIT)

let kSystemObject = AudioObjectID(kAudioObjectSystemObject)

func propertyAddress(_ selector: AudioObjectPropertySelector,
                     scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal,
                     element: AudioObjectPropertyElement = kAudioObjectPropertyElementMain) -> AudioObjectPropertyAddress {
    AudioObjectPropertyAddress(mSelector: selector, mScope: scope, mElement: element)
}

struct CAError: Error, CustomStringConvertible {
    let message: String
    var description: String { message }
}

func readProperty<T>(_ objectID: AudioObjectID,
                     _ selector: AudioObjectPropertySelector,
                     scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal,
                     defaultValue: T,
                     qualifierSize: UInt32 = 0,
                     qualifierData: UnsafeRawPointer? = nil) throws -> T {
    var address = propertyAddress(selector, scope: scope)
    var dataSize: UInt32 = 0
    var err = AudioObjectGetPropertyDataSize(objectID, &address, qualifierSize, qualifierData, &dataSize)
    guard err == noErr else {
        throw CAError(message: "GetPropertyDataSize \(fourCCString(selector)) failed: \(osStatusString(err))")
    }
    var value = defaultValue
    err = withUnsafeMutablePointer(to: &value) { ptr in
        AudioObjectGetPropertyData(objectID, &address, qualifierSize, qualifierData, &dataSize, ptr)
    }
    guard err == noErr else {
        throw CAError(message: "GetPropertyData \(fourCCString(selector)) failed: \(osStatusString(err))")
    }
    return value
}

func readDeviceUID(_ deviceID: AudioObjectID) throws -> String {
    try readProperty(deviceID, kAudioDevicePropertyDeviceUID, defaultValue: "" as CFString) as String
}

func readObjectName(_ objectID: AudioObjectID) -> String {
    (try? readProperty(objectID, kAudioObjectPropertyName, defaultValue: "" as CFString) as String) ?? "<unnamed>"
}

func readDefaultOutputDevice() throws -> AudioDeviceID {
    try readProperty(kSystemObject, kAudioHardwarePropertyDefaultOutputDevice,
                     defaultValue: AudioObjectID(kAudioObjectUnknown))
}

func readTransportType(_ deviceID: AudioObjectID) -> UInt32 {
    (try? readProperty(deviceID, kAudioDevicePropertyTransportType, defaultValue: UInt32(0))) ?? 0
}

/// kAudioAggregateDevicePropertyFullSubDeviceList → [device UID strings]
func readAggregateSubDeviceUIDs(_ deviceID: AudioObjectID) -> [String] {
    guard let cfArray = try? readProperty(deviceID, kAudioAggregateDevicePropertyFullSubDeviceList,
                                          defaultValue: [] as CFArray) else { return [] }
    return (cfArray as? [String]) ?? []
}

/// kAudioHardwarePropertyTranslatePIDToProcessObject — PID → Core Audio process object.
/// (audiotee Utils.swift pattern, MIT.)
func translatePIDToProcessObject(_ pid: pid_t) throws -> AudioObjectID {
    var mutablePID = pid
    let object: AudioObjectID = try withUnsafePointer(to: &mutablePID) { pidPtr in
        try readProperty(kSystemObject, kAudioHardwarePropertyTranslatePIDToProcessObject,
                         defaultValue: AudioObjectID(kAudioObjectUnknown),
                         qualifierSize: UInt32(MemoryLayout<pid_t>.size),
                         qualifierData: pidPtr)
    }
    guard object != kAudioObjectUnknown else {
        throw CAError(message: "PID \(pid) has no Core Audio process object")
    }
    return object
}

func translateUIDToDevice(_ uid: String) -> AudioDeviceID {
    var cfUID = uid as CFString
    let device: AudioDeviceID? = try? withUnsafePointer(to: &cfUID) { uidPtr in
        try readProperty(kSystemObject, kAudioHardwarePropertyTranslateUIDToDevice,
                         defaultValue: AudioObjectID(kAudioObjectUnknown),
                         qualifierSize: UInt32(MemoryLayout<CFString>.size),
                         qualifierData: uidPtr)
    }
    return device ?? AudioObjectID(kAudioObjectUnknown)
}

func readTapStreamDescription(_ tapID: AudioObjectID) throws -> AudioStreamBasicDescription {
    try readProperty(tapID, kAudioTapPropertyFormat, defaultValue: AudioStreamBasicDescription())
}

func describeASBD(_ asbd: AudioStreamBasicDescription) -> String {
    let flags = asbd.mFormatFlags
    var parts: [String] = []
    if flags & kAudioFormatFlagIsFloat != 0 { parts.append("float") }
    if flags & kAudioFormatFlagIsSignedInteger != 0 { parts.append("signed-int") }
    parts.append(flags & kAudioFormatFlagIsNonInterleaved != 0 ? "non-interleaved" : "interleaved")
    return "\(Int(asbd.mSampleRate)) Hz, \(asbd.mChannelsPerFrame) ch, "
        + "\(asbd.mBitsPerChannel)-bit \(parts.joined(separator: " ")), "
        + "fmt='\(fourCCString(asbd.mFormatID))', \(asbd.mBytesPerFrame) B/frame"
}

// MARK: - Capture sink (WAV accumulation + per-second RMS watchdog)

/// Accumulates tap samples as int16 interleaved PCM and prints a per-second RMS
/// line — this is the silence-watchdog logic MeetingScribe's helper will need.
/// IO block runs on a serial DispatchQueue; main thread only reads after
/// AudioDeviceStop, but a lock keeps it honest.
final class CaptureSink {
    private let lock = NSLock()
    private var pcm = Data()

    // per-second accumulators
    private var secFrames = 0
    private var secSumSquares = 0.0
    private var secPeak: Float = 0
    private var secondIndex = 0

    // totals
    private(set) var totalFrames = 0
    private var totalSumSquares = 0.0
    private(set) var overallPeak: Float = 0
    private(set) var silentSeconds = 0
    private(set) var ioCallbacks = 0

    let sampleRate: Int
    let channels: Int
    let sourceIsFloat: Bool
    let sourceIsInterleaved: Bool
    let sourceBitsPerChannel: Int

    init(format: AudioStreamBasicDescription) {
        self.sampleRate = Int(format.mSampleRate)
        self.channels = max(1, Int(format.mChannelsPerFrame))
        self.sourceIsFloat = format.mFormatFlags & kAudioFormatFlagIsFloat != 0
        self.sourceIsInterleaved = format.mFormatFlags & kAudioFormatFlagIsNonInterleaved == 0
        self.sourceBitsPerChannel = Int(format.mBitsPerChannel)
    }

    var overallRMS: Double {
        totalFrames == 0 ? 0 : (totalSumSquares / Double(totalFrames * channels)).squareRoot()
    }

    var pcmData: Data {
        lock.lock(); defer { lock.unlock() }
        return pcm
    }

    /// Called from the IO block. Converts whatever the tap delivers to int16
    /// interleaved and updates RMS accumulators.
    func ingest(_ inInputData: UnsafePointer<AudioBufferList>) {
        let ablPtr = UnsafeMutablePointer(mutating: inInputData)
        let buffers = UnsafeMutableAudioBufferListPointer(ablPtr)
        guard buffers.count > 0 else { return }

        var floats: [Float] = []   // interleaved float samples for this cycle

        if sourceIsFloat && sourceBitsPerChannel == 32 {
            if sourceIsInterleaved || buffers.count == 1 {
                let buf = buffers[0]
                guard let base = buf.mData else { return }
                let count = Int(buf.mDataByteSize) / MemoryLayout<Float>.size
                floats = Array(UnsafeBufferPointer(start: base.assumingMemoryBound(to: Float.self), count: count))
            } else {
                // Non-interleaved: one buffer per channel — interleave manually.
                let chBuffers: [[Float]] = buffers.compactMap { buf in
                    guard let base = buf.mData else { return nil }
                    let count = Int(buf.mDataByteSize) / MemoryLayout<Float>.size
                    return Array(UnsafeBufferPointer(start: base.assumingMemoryBound(to: Float.self), count: count))
                }
                guard let frameCount = chBuffers.map(\.count).min(), frameCount > 0 else { return }
                floats.reserveCapacity(frameCount * chBuffers.count)
                for frame in 0..<frameCount {
                    for ch in chBuffers { floats.append(ch[frame]) }
                }
            }
        } else if !sourceIsFloat && sourceBitsPerChannel == 16 {
            let buf = buffers[0]
            guard let base = buf.mData else { return }
            let count = Int(buf.mDataByteSize) / MemoryLayout<Int16>.size
            let ints = UnsafeBufferPointer(start: base.assumingMemoryBound(to: Int16.self), count: count)
            floats = ints.map { Float($0) / 32768.0 }
        } else {
            return // unexpected format — reported at startup, nothing to do here
        }

        guard !floats.isEmpty else { return }
        let frames = floats.count / channels

        var int16Samples = [Int16](repeating: 0, count: floats.count)
        var sumSq = 0.0
        var peak: Float = 0
        for (i, v) in floats.enumerated() {
            let a = abs(v)
            if a > peak { peak = a }
            sumSq += Double(v) * Double(v)
            let clamped = max(-1.0, min(1.0, v))
            int16Samples[i] = Int16(clamped * 32767.0)
        }

        lock.lock()
        ioCallbacks += 1
        int16Samples.withUnsafeBufferPointer { pcm.append(UnsafeRawBufferPointer($0).bindMemory(to: UInt8.self)) }
        totalFrames += frames
        totalSumSquares += sumSq
        if peak > overallPeak { overallPeak = peak }
        secFrames += frames
        secSumSquares += sumSq
        if peak > secPeak { secPeak = peak }

        while secFrames >= sampleRate {
            // Simplification for the spike: attribute the whole accumulator to this
            // second (cycle sizes are ~512 frames, so spillover is negligible).
            let rms = (secSumSquares / Double(secFrames * channels)).squareRoot()
            let db = rms > 0 ? 20 * log10(rms) : -Double.infinity
            let silent = rms < 1e-6
            if silent { silentSeconds += 1 }
            secondIndex += 1
            let dbText = db.isFinite ? String(format: "%6.1f dBFS", db) : "  -inf dBFS"
            let line = String(format: "[sec %2d] rms=%.6f  %@  peak=%.4f%@",
                              secondIndex, rms, dbText, secPeak, silent ? "  ** SILENT **" : "")
            print(line)
            secFrames = 0
            secSumSquares = 0
            secPeak = 0
        }
        lock.unlock()
    }
}

// MARK: - WAV writer (int16 PCM, canonical 44-byte header)

func writeWAV(url: URL, pcm: Data, channels: Int, sampleRate: Int) throws {
    var d = Data()
    func ascii(_ s: String) { d.append(s.data(using: .ascii)!) }
    func u32(_ v: UInt32) { withUnsafeBytes(of: v.littleEndian) { d.append(contentsOf: $0) } }
    func u16(_ v: UInt16) { withUnsafeBytes(of: v.littleEndian) { d.append(contentsOf: $0) } }
    ascii("RIFF"); u32(UInt32(36 + pcm.count)); ascii("WAVE")
    ascii("fmt "); u32(16); u16(1) // PCM
    u16(UInt16(channels)); u32(UInt32(sampleRate))
    u32(UInt32(sampleRate * channels * 2)) // byte rate
    u16(UInt16(channels * 2)); u16(16)     // block align, bits
    ascii("data"); u32(UInt32(pcm.count)); d.append(pcm)
    try d.write(to: url)
}

// MARK: - Main

@main
struct SyscapSpike {
    static func main() {
        // ---- args ----
        var seconds = 8
        var outPath = "capture.wav"
        var args = Array(CommandLine.arguments.dropFirst())
        while !args.isEmpty {
            let a = args.removeFirst()
            switch a {
            case "--seconds", "-s":
                if let v = args.first.flatMap({ Int($0) }) { seconds = v; args.removeFirst() }
            case "--out", "-o":
                if let v = args.first { outPath = v; args.removeFirst() }
            case "--help", "-h":
                print("usage: syscap-spike [--seconds N] [--out FILE.wav]")
                return
            default:
                FileHandle.standardError.write("unknown argument: \(a)\n".data(using: .utf8)!)
                exit(64)
            }
        }

        print("syscap-spike — Core Audio process-tap capture spike (MeetingScribe Gate 1)")
        print("pid=\(ProcessInfo.processInfo.processIdentifier)  capturing \(seconds)s → \(outPath)")
        print("")

        // ---- 1. Exclude our own PID from the global tap ----
        // Translate PID → process object (kAudioHardwarePropertyTranslatePIDToProcessObject).
        // A CLI that has never played audio often has no process object — that is
        // fine (we emit no audio to leak into the tap); proceed with empty list.
        // (macOS 26 SDK imports the initializer as taking [AudioObjectID] directly.)
        var excludedProcesses: [AudioObjectID] = []
        let ownPID = ProcessInfo.processInfo.processIdentifier
        do {
            let obj = try translatePIDToProcessObject(ownPID)
            excludedProcesses = [obj]
            print("[tap] own PID \(ownPID) → process object #\(obj), excluding it from the tap")
        } catch {
            print("[tap] own PID \(ownPID) has no Core Audio process object (normal for a silent CLI): \(error)")
            print("[tap] proceeding with an empty exclusion list")
        }

        // ---- 2. Tap description: global stereo mixdown, excluding us ----
        // TRAP 1: the initializer sets the inverted isExclusive semantics itself.
        // Do NOT mutate .isExclusive afterwards — that silences delivery.
        let tapDescription = CATapDescription(stereoGlobalTapButExcludeProcesses: excludedProcesses)
        tapDescription.uuid = UUID()
        tapDescription.name = "syscap-spike-tap"
        tapDescription.muteBehavior = .unmuted
        print("[tap] CATapDescription: stereo global tap, exclude=\(excludedProcesses), mute=unmuted")

        // ---- 3. Create the process tap ----
        // FIRST creation fires the one-time TCC prompt ("System Audio Recording
        // Only" / SystemAudioCaptureRequests). No public pre-check API exists.
        print("[tap] AudioHardwareCreateProcessTap … (a system permission dialog may appear NOW)")
        var tapID = AudioObjectID(kAudioObjectUnknown)
        var err = AudioHardwareCreateProcessTap(tapDescription, &tapID)
        guard err == noErr, tapID != kAudioObjectUnknown else {
            print("[tap] FAILED: AudioHardwareCreateProcessTap → \(osStatusString(err))")
            print("")
            print("Likely causes:")
            print("  - System Audio Recording permission denied for this terminal's responsible app.")
            print("    System Settings → Privacy & Security → Screen & System Audio Recording →")
            print("    'System Audio Recording Only' section → enable your terminal, then re-run.")
            print("  - macOS < 14.2 (API unavailable) — not the case on this machine.")
            exit(1)
        }
        print("[tap] created process tap #\(tapID)")

        // ---- 4. Read the tap's stream format (learn the real-world shape) ----
        var tapFormat: AudioStreamBasicDescription
        do {
            tapFormat = try readTapStreamDescription(tapID)
            print("[tap] tap stream format: \(describeASBD(tapFormat))")
        } catch {
            print("[tap] FAILED reading kAudioTapPropertyFormat: \(error)")
            AudioHardwareDestroyProcessTap(tapID)
            exit(1)
        }

        // ---- 5. Resolve the REAL default output device ----
        // TRAP 2 prep: the aggregate needs a real output device as its main
        // sub-device. TRAP 4: if the default output is itself an aggregate
        // (legacy "MeetingScribe Output" multi-output etc.), unwrap it —
        // aggregates cannot nest.
        var outputDeviceID: AudioDeviceID
        do {
            outputDeviceID = try readDefaultOutputDevice()
        } catch {
            print("[agg] FAILED reading default output device: \(error)")
            AudioHardwareDestroyProcessTap(tapID)
            exit(1)
        }
        var outputName = readObjectName(outputDeviceID)
        print("[agg] default output device: #\(outputDeviceID) \"\(outputName)\"")

        if readTransportType(outputDeviceID) == kAudioDeviceTransportTypeAggregate {
            let subUIDs = readAggregateSubDeviceUIDs(outputDeviceID)
            print("[agg] default output is an AGGREGATE (sub-devices: \(subUIDs)) — unwrapping")
            var unwrapped = AudioObjectID(kAudioObjectUnknown)
            // Prefer a non-aggregate, non-loopback sub-device.
            for uid in subUIDs {
                let dev = translateUIDToDevice(uid)
                guard dev != kAudioObjectUnknown else { continue }
                guard readTransportType(dev) != kAudioDeviceTransportTypeAggregate else { continue }
                let name = readObjectName(dev)
                if name.localizedCaseInsensitiveContains("blackhole") && unwrapped != kAudioObjectUnknown {
                    continue
                }
                if unwrapped == kAudioObjectUnknown || !name.localizedCaseInsensitiveContains("blackhole") {
                    unwrapped = dev
                    if !name.localizedCaseInsensitiveContains("blackhole") { break }
                }
            }
            guard unwrapped != kAudioObjectUnknown else {
                print("[agg] FAILED: could not unwrap aggregate default output to a real device")
                AudioHardwareDestroyProcessTap(tapID)
                exit(1)
            }
            outputDeviceID = unwrapped
            outputName = readObjectName(outputDeviceID)
            print("[agg] unwrapped to real output: #\(outputDeviceID) \"\(outputName)\"")
        }

        let outputUID: String
        do {
            outputUID = try readDeviceUID(outputDeviceID)
        } catch {
            print("[agg] FAILED reading output device UID: \(error)")
            AudioHardwareDestroyProcessTap(tapID)
            exit(1)
        }
        print("[agg] output device UID: \(outputUID)")

        // ---- 6. Aggregate device: real output as main sub-device + tap in tap list ----
        // TRAP 2: tap-as-main with an empty sub-device list yields ZERO samples.
        // Dict shape copied from AudioCap ProcessTap.prepare(for:) (MIT).
        let aggregateUID = UUID().uuidString
        let description: [String: Any] = [
            kAudioAggregateDeviceNameKey: "syscap-spike-aggregate",
            kAudioAggregateDeviceUIDKey: aggregateUID,
            kAudioAggregateDeviceMainSubDeviceKey: outputUID,
            kAudioAggregateDeviceIsPrivateKey: true,
            kAudioAggregateDeviceIsStackedKey: false,
            kAudioAggregateDeviceTapAutoStartKey: true,
            kAudioAggregateDeviceSubDeviceListKey: [
                [kAudioSubDeviceUIDKey: outputUID]
            ],
            kAudioAggregateDeviceTapListKey: [
                [
                    kAudioSubTapDriftCompensationKey: true,
                    kAudioSubTapUIDKey: tapDescription.uuid.uuidString
                ]
            ]
        ]

        var aggregateID = AudioObjectID(kAudioObjectUnknown)
        err = AudioHardwareCreateAggregateDevice(description as CFDictionary, &aggregateID)
        guard err == noErr, aggregateID != kAudioObjectUnknown else {
            print("[agg] FAILED: AudioHardwareCreateAggregateDevice → \(osStatusString(err))")
            AudioHardwareDestroyProcessTap(tapID)
            exit(1)
        }
        print("[agg] created aggregate device #\(aggregateID) \"\(readObjectName(aggregateID))\" (private, tap auto-start)")

        // Optional: report the aggregate's first input stream virtual format.
        if let streams = try? readProperty(aggregateID, kAudioDevicePropertyStreams,
                                           scope: kAudioObjectPropertyScopeInput,
                                           defaultValue: AudioObjectID(kAudioObjectUnknown)),
           streams != kAudioObjectUnknown,
           let streamFormat = try? readProperty(streams, kAudioStreamPropertyVirtualFormat,
                                                defaultValue: AudioStreamBasicDescription()) {
            print("[agg] aggregate input stream #\(streams) virtual format: \(describeASBD(streamFormat))")
        }

        // ---- 7. IO proc on a REAL dispatch queue + start ----
        // TRAP 3: passing nil for the queue silently registers nothing on
        // macOS 26 (Tahoe regression). Always pass a real queue.
        let sink = CaptureSink(format: tapFormat)
        if !(sink.sourceIsFloat && sink.sourceBitsPerChannel == 32)
            && !(!sink.sourceIsFloat && sink.sourceBitsPerChannel == 16) {
            print("[io ] WARNING: unexpected tap format (\(describeASBD(tapFormat))) — ingest will drop data")
        }

        let ioQueue = DispatchQueue(label: "com.meetingscribe.syscap-spike.io", qos: .userInitiated)
        var ioProcID: AudioDeviceIOProcID?
        err = AudioDeviceCreateIOProcIDWithBlock(&ioProcID, aggregateID, ioQueue) {
            _, inInputData, _, _, _ in
            sink.ingest(inInputData)
        }
        guard err == noErr, ioProcID != nil else {
            print("[io ] FAILED: AudioDeviceCreateIOProcIDWithBlock → \(osStatusString(err))")
            AudioHardwareDestroyAggregateDevice(aggregateID)
            AudioHardwareDestroyProcessTap(tapID)
            exit(1)
        }

        err = AudioDeviceStart(aggregateID, ioProcID)
        guard err == noErr else {
            print("[io ] FAILED: AudioDeviceStart → \(osStatusString(err))")
            AudioDeviceDestroyIOProcID(aggregateID, ioProcID!)
            AudioHardwareDestroyAggregateDevice(aggregateID)
            AudioHardwareDestroyProcessTap(tapID)
            exit(1)
        }
        print("[io ] capturing for \(seconds)s — play some audio now …")
        print("")

        // ---- 8. Capture window ----
        let deadline = Date().addingTimeInterval(TimeInterval(seconds))
        while Date() < deadline {
            usleep(100_000)
        }

        // ---- 9. Teardown, strictly: Stop → DestroyIOProcID → DestroyAggregate → DestroyTap ----
        print("")
        err = AudioDeviceStop(aggregateID, ioProcID)
        if err != noErr { print("[io ] warning: AudioDeviceStop → \(osStatusString(err))") }
        err = AudioDeviceDestroyIOProcID(aggregateID, ioProcID!)
        if err != noErr { print("[io ] warning: AudioDeviceDestroyIOProcID → \(osStatusString(err))") }
        err = AudioHardwareDestroyAggregateDevice(aggregateID)
        if err != noErr { print("[agg] warning: AudioHardwareDestroyAggregateDevice → \(osStatusString(err))") }
        err = AudioHardwareDestroyProcessTap(tapID)
        if err != noErr { print("[tap] warning: AudioHardwareDestroyProcessTap → \(osStatusString(err))") }

        // ---- 10. Results + WAV ----
        let pcm = sink.pcmData
        let capturedSeconds = Double(sink.totalFrames) / Double(sink.sampleRate)
        print(String(format: "[out] io callbacks=%d  frames=%d (%.2fs)  overall rms=%.6f  peak=%.4f  silent-seconds=%d",
                     sink.ioCallbacks, sink.totalFrames, capturedSeconds,
                     sink.overallRMS, sink.overallPeak, sink.silentSeconds))

        if sink.totalFrames == 0 {
            print("")
            print("PROBLEM: the IOProc never delivered a single frame.")
            print("  - If AudioDeviceCreateIOProcIDWithBlock had been given a nil queue this is the")
            print("    macOS 26 silent-registration trap — this spike passes a real queue, so more")
            print("    likely: aggregate/tap misconfiguration or the device could not start.")
            exit(2)
        }

        let outURL = URL(fileURLWithPath: outPath)
        do {
            try writeWAV(url: outURL, pcm: pcm, channels: sink.channels, sampleRate: sink.sampleRate)
            print("[out] wrote \(outURL.path) — int16 PCM, \(sink.channels) ch, \(sink.sampleRate) Hz, \(pcm.count) bytes")
        } catch {
            print("[out] FAILED writing WAV: \(error)")
            exit(1)
        }

        if sink.overallRMS < 1e-6 {
            print("""

            PROBLEM: capture succeeded but every sample is digital silence.
            Likely causes, in order:
              1. System Audio Recording permission DENIED (this is how denial manifests —
                 tap creation succeeds, samples are zeros; there is no public query API).
                 Fix: System Settings → Privacy & Security → Screen & System Audio Recording
                 → 'System Audio Recording Only' → enable this terminal's app → re-run.
                 If no prompt ever appeared, trigger it again by re-running, or reset with:
                   tccutil reset AudioCapture
              2. Nothing was playing audio during the window (play a song / afplay a sound).
              3. Tap/aggregate misconfiguration (tap as main sub-device, or a mutated
                 isExclusive) — this spike codes around both, see comments.
            """)
            exit(2)
        }

        print("[out] SUCCESS: non-silent system audio captured via Core Audio process tap.")
    }
}
