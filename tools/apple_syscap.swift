// apple_syscap — driverless system-audio capture via Core Audio process taps
// (macOS 14.2+ API; MeetingScribe gates it at macOS 26 like the other helpers).
//
// Replaces the BlackHole loopback driver: a global process tap (excluding this
// helper and any --exclude-pid) is wrapped in a private aggregate device whose
// main sub-device is the user's real output. Nothing about the user's output
// routing is touched — no Multi-Output Device, no default-output switcheroo.
//
//   stdout: raw interleaved int16 PCM, locked at --rate/--channels for the
//           whole life of the process (resampled internally across device and
//           format changes so the WAV downstream keeps a single rate).
//   stderr: NDJSON events, one per line:
//     {"t":"ready","rate":48000,"channels":2,"chunk_ms":20,
//      "source":{"rate":48000,"channels":2},"output":"MacBook Pro Speakers"}
//     {"t":"device_changed","output":"AirPods Pro"}   (aggregate rebuilt)
//     {"t":"rebuilt","reason":"silence"}              (zero-buffer watchdog)
//     {"t":"silence","seconds":10}                    (sustained digital zeros)
//     {"t":"error","stage":"tap_create","message":"…"}
//
// Usage: apple_syscap [--rate 48000] [--channels 2] [--chunk-ms 20]
//                     [--exclude-pid N]...
// Exit:  0 clean stop (SIGTERM/SIGINT/parent exit), 1 runtime error,
//        3 tap creation failed (permission denied is the usual cause), 64 usage.
//
// Permission: creating the first tap fires the one-time "System Audio
// Recording Only" TCC prompt. There is no public API to query the grant;
// denial usually manifests as tap-creation failure or silent zero samples —
// the Python side pairs the exit code / silence warnings with user guidance.
//
// Call sequences adapted from insidegui/AudioCap and makeusabrew/audiotee
// (both MIT) and Apple's "Capturing system audio with Core Audio taps"
// sample, hardened for the traps documented in docs/COREAUDIO_TAP_CAPTURE_PRD.md:
//   1. isExclusive inversion — never mutate .isExclusive after init.
//   2. The tap must ride the tap LIST of an aggregate whose main sub-device is
//      a REAL output device; tap-as-main delivers zero samples.
//   3. AudioDeviceCreateIOProcIDWithBlock silently registers nothing when
//      given a nil dispatch queue on macOS 26 — always pass a real queue.
//   4. Aggregates cannot nest: when the default output is itself an aggregate
//      (a legacy "MeetingScribe Output" multi-output), unwrap it to its first
//      real sub-device.
//
// Compiled by swift_helpers.ensure_binary(); in the packaged app it runs
// IN PLACE from Contents/Resources/bin (never copied out) so the TCC identity
// is the app bundle — see swift_helpers.BUNDLE_ONLY.

import AVFoundation
import AudioToolbox
import CoreAudio
import Foundation

// MARK: - OSStatus / FourCC pretty-printing

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

// MARK: - Core Audio property helpers (AudioCap CoreAudioUtils.swift patterns, MIT)

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

func readPropertyArray<T>(_ objectID: AudioObjectID,
                          _ selector: AudioObjectPropertySelector,
                          scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal,
                          element: T) throws -> [T] {
    var address = propertyAddress(selector, scope: scope)
    var dataSize: UInt32 = 0
    var err = AudioObjectGetPropertyDataSize(objectID, &address, 0, nil, &dataSize)
    guard err == noErr else {
        throw CAError(message: "GetPropertyDataSize \(fourCCString(selector)) failed: \(osStatusString(err))")
    }
    let count = Int(dataSize) / MemoryLayout<T>.size
    guard count > 0 else { return [] }
    var values = [T](repeating: element, count: count)
    err = values.withUnsafeMutableBufferPointer { buf in
        AudioObjectGetPropertyData(objectID, &address, 0, nil, &dataSize, buf.baseAddress!)
    }
    guard err == noErr else {
        throw CAError(message: "GetPropertyData \(fourCCString(selector)) failed: \(osStatusString(err))")
    }
    return values
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

func readAggregateSubDeviceUIDs(_ deviceID: AudioObjectID) -> [String] {
    guard let cfArray = try? readProperty(deviceID, kAudioAggregateDevicePropertyFullSubDeviceList,
                                          defaultValue: [] as CFArray) else { return [] }
    return (cfArray as? [String]) ?? []
}

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

/// Input-scope stream IDs of a device, in ABL buffer order.
func readInputStreams(_ deviceID: AudioObjectID) -> [AudioObjectID] {
    (try? readPropertyArray(deviceID, kAudioDevicePropertyStreams,
                            scope: kAudioObjectPropertyScopeInput,
                            element: AudioObjectID(kAudioObjectUnknown))) ?? []
}

func readOutputStreams(_ deviceID: AudioObjectID) -> [AudioObjectID] {
    (try? readPropertyArray(deviceID, kAudioDevicePropertyStreams,
                            scope: kAudioObjectPropertyScopeOutput,
                            element: AudioObjectID(kAudioObjectUnknown))) ?? []
}

func readStreamVirtualFormat(_ streamID: AudioObjectID) -> AudioStreamBasicDescription? {
    try? readProperty(streamID, kAudioStreamPropertyVirtualFormat,
                      defaultValue: AudioStreamBasicDescription())
}

/// Restrict which of the device's streams our IOProc drives. Disabling the
/// output side and every input stream except the tap's keeps a headphone
/// sub-device's microphone out of the capture (and keeps Bluetooth devices
/// out of the degraded HFP headset profile).
/// struct AudioHardwareIOProcStreamUsage { void *mIOProc; UInt32 mNumberStreams; UInt32 mStreamIsOn[…]; }
func setIOProcStreamUsage(device: AudioObjectID, ioProcID: AudioDeviceIOProcID,
                          scope: AudioObjectPropertyScope, streamIsOn: [Bool]) {
    guard !streamIsOn.isEmpty else { return }
    let headerSize = MemoryLayout<UnsafeMutableRawPointer?>.size + MemoryLayout<UInt32>.size
    let size = headerSize + MemoryLayout<UInt32>.size * streamIsOn.count
    let raw = UnsafeMutableRawPointer.allocate(byteCount: size,
                                               alignment: MemoryLayout<UnsafeMutableRawPointer?>.alignment)
    defer { raw.deallocate() }
    raw.storeBytes(of: unsafeBitCast(ioProcID, to: UnsafeMutableRawPointer.self), toByteOffset: 0,
                   as: UnsafeMutableRawPointer.self)
    raw.storeBytes(of: UInt32(streamIsOn.count),
                   toByteOffset: MemoryLayout<UnsafeMutableRawPointer?>.size, as: UInt32.self)
    for (i, on) in streamIsOn.enumerated() {
        raw.storeBytes(of: UInt32(on ? 1 : 0),
                       toByteOffset: headerSize + i * MemoryLayout<UInt32>.size, as: UInt32.self)
    }
    var address = propertyAddress(kAudioDevicePropertyIOProcStreamUsage, scope: scope)
    let err = AudioObjectSetPropertyData(device, &address, 0, nil, UInt32(size), raw)
    if err != noErr {
        // Non-fatal: capture still works, at worst the sub-device's own input
        // stream rides along (the ingest path picks the tap buffer by index).
        Events.emit(["t": "warn", "stage": "stream_usage",
                     "message": "IOProcStreamUsage failed: \(osStatusString(err))"])
    }
}

// MARK: - NDJSON events on stderr

enum Events {
    static let lock = NSLock()
    static func emit(_ obj: [String: Any]) {
        guard JSONSerialization.isValidJSONObject(obj),
              let data = try? JSONSerialization.data(withJSONObject: obj) else { return }
        lock.lock()
        defer { lock.unlock() }
        var line = data
        line.append(0x0A)
        line.withUnsafeBytes { buf in
            var off = 0
            while off < buf.count {
                let n = write(2, buf.baseAddress!.advanced(by: off), buf.count - off)
                if n <= 0 {
                    if errno == EINTR { continue }
                    return
                }
                off += n
            }
        }
    }
}

// MARK: - stdout PCM sink (locked format, chunked, frame-accounted)

/// Serializes converted int16 PCM to stdout in fixed-size chunks and keeps the
/// emitted-frame count so rebuild gaps can be back-filled with silence,
/// preserving the WAV's realtime timeline. All calls happen on the IO queue
/// (engine callbacks) or the control queue (gap fill) — guarded by a lock.
final class PCMSink {
    let bytesPerFrame: Int
    let chunkBytes: Int
    private let lock = NSLock()
    private var pending = Data()
    private(set) var framesOut: Int = 0
    var onWriteFailure: (() -> Void)?

    init(bytesPerFrame: Int, chunkBytes: Int) {
        self.bytesPerFrame = bytesPerFrame
        self.chunkBytes = chunkBytes
    }

    /// Frames delivered to the sink (written out or still buffered).
    var framesAccepted: Int {
        lock.lock(); defer { lock.unlock() }
        return framesOut + pending.count / bytesPerFrame
    }

    func append(_ data: Data) {
        guard !data.isEmpty else { return }
        // The write happens INSIDE the lock: appenders live on two queues
        // (IO queue for audio, control queue for rebuild gap-fill), and a
        // write issued after unlocking could land on stdout out of order —
        // or interleave mid-frame when a large silence pad blocks on the
        // pipe. Holding the lock across write(2) keeps byte order exactly
        // equal to extraction order; the recorder reads eagerly, so the
        // stall window is bounded.
        lock.lock()
        defer { lock.unlock() }
        pending.append(data)
        let complete = (pending.count / chunkBytes) * chunkBytes
        if complete > 0 {
            let toWrite = pending.prefix(complete)
            pending.removeFirst(complete)
            framesOut += complete / bytesPerFrame
            writeAll(toWrite)
        }
    }

    func padSilence(frames: Int) {
        guard frames > 0 else { return }
        append(Data(count: frames * bytesPerFrame))
    }

    /// Flush any sub-chunk remainder (shutdown only).
    func flush() {
        lock.lock()
        defer { lock.unlock() }
        let rest = pending
        pending.removeAll()
        framesOut += rest.count / bytesPerFrame
        if !rest.isEmpty { writeAll(rest) }
    }

    private func writeAll(_ data: Data) {
        data.withUnsafeBytes { buf in
            var off = 0
            while off < buf.count {
                let n = write(1, buf.baseAddress!.advanced(by: off), buf.count - off)
                if n <= 0 {
                    if errno == EINTR { continue }
                    onWriteFailure?()  // reader (the recorder) is gone
                    return
                }
                off += n
            }
        }
    }
}

// MARK: - One build of tap + aggregate + IOProc

final class TapEngine {
    let tapID: AudioObjectID
    let aggregateID: AudioObjectID
    let ioProcID: AudioDeviceIOProcID
    let outputDeviceID: AudioDeviceID
    let outputName: String
    let sourceFormat: AudioStreamBasicDescription
    private let srcAVFormat: AVAudioFormat
    private let dstAVFormat: AVAudioFormat
    private let converter: AVAudioConverter?
    private let tapBufferIndex: Int
    private var stopped = false

    /// Called on the IO queue with converted int16 interleaved PCM.
    /// `zeroInput` is true when every source sample this cycle was digital zero
    /// (measured before conversion, so dither can't mask a dead tap).
    var onPCM: ((Data, _ zeroInput: Bool) -> Void)?

    struct BuildError: Error {
        let stage: String
        let message: String
        let permissionSuspect: Bool
    }

    private init(tapID: AudioObjectID, aggregateID: AudioObjectID, ioProcID: AudioDeviceIOProcID,
                 outputDeviceID: AudioDeviceID, outputName: String,
                 sourceFormat: AudioStreamBasicDescription,
                 srcAVFormat: AVAudioFormat, dstAVFormat: AVAudioFormat,
                 converter: AVAudioConverter?, tapBufferIndex: Int) {
        self.tapID = tapID
        self.aggregateID = aggregateID
        self.ioProcID = ioProcID
        self.outputDeviceID = outputDeviceID
        self.outputName = outputName
        self.sourceFormat = sourceFormat
        self.srcAVFormat = srcAVFormat
        self.dstAVFormat = dstAVFormat
        self.converter = converter
        self.tapBufferIndex = tapBufferIndex
    }

    /// Resolve the real (non-aggregate) default output device, unwrapping a
    /// legacy multi-output if needed (aggregates cannot nest).
    static func resolveRealOutput() throws -> AudioDeviceID {
        var outputDeviceID = try readDefaultOutputDevice()
        guard outputDeviceID != kAudioObjectUnknown else {
            throw CAError(message: "no default output device")
        }
        if readTransportType(outputDeviceID) == kAudioDeviceTransportTypeAggregate {
            var unwrapped = AudioObjectID(kAudioObjectUnknown)
            for uid in readAggregateSubDeviceUIDs(outputDeviceID) {
                let dev = translateUIDToDevice(uid)
                guard dev != kAudioObjectUnknown,
                      readTransportType(dev) != kAudioDeviceTransportTypeAggregate else { continue }
                let virtual = readTransportType(dev) == kAudioDeviceTransportTypeVirtual
                if !virtual {
                    unwrapped = dev
                    break
                }
                if unwrapped == kAudioObjectUnknown { unwrapped = dev }
            }
            guard unwrapped != kAudioObjectUnknown else {
                throw CAError(message: "default output is an aggregate with no usable sub-device")
            }
            outputDeviceID = unwrapped
        }
        return outputDeviceID
    }

    static func build(excludePIDs: [pid_t], dstFormat: AVAudioFormat,
                      ioQueue: DispatchQueue) throws -> TapEngine {
        // 1. Process objects to exclude (self + requested); missing ones are
        //    normal — a process that has never played audio has no object.
        var excluded: [AudioObjectID] = []
        for pid in excludePIDs {
            if let obj = try? translatePIDToProcessObject(pid) { excluded.append(obj) }
        }

        // 2. Global stereo mixdown tap. The initializer sets the inverted
        //    isExclusive semantics — never mutate it afterwards (trap 1).
        let tapDescription = CATapDescription(stereoGlobalTapButExcludeProcesses: excluded)
        tapDescription.uuid = UUID()
        tapDescription.name = "MeetingScribe system tap"
        tapDescription.muteBehavior = .unmuted
        tapDescription.isPrivate = true

        var tapID = AudioObjectID(kAudioObjectUnknown)
        var err = AudioHardwareCreateProcessTap(tapDescription, &tapID)
        guard err == noErr, tapID != kAudioObjectUnknown else {
            throw BuildError(stage: "tap_create",
                             message: "AudioHardwareCreateProcessTap failed: \(osStatusString(err))",
                             permissionSuspect: true)
        }

        func fail(_ stage: String, _ message: String,
                  aggregate: AudioObjectID? = nil) -> BuildError {
            if let agg = aggregate { AudioHardwareDestroyAggregateDevice(agg) }
            AudioHardwareDestroyProcessTap(tapID)
            return BuildError(stage: stage, message: message, permissionSuspect: false)
        }

        let sourceFormat: AudioStreamBasicDescription
        do {
            sourceFormat = try readTapStreamDescription(tapID)
        } catch {
            throw fail("tap_format", "\(error)")
        }

        // 3. Real output device as the aggregate's main sub-device (traps 2+4).
        let outputDeviceID: AudioDeviceID
        let outputUID: String
        do {
            outputDeviceID = try resolveRealOutput()
            outputUID = try readDeviceUID(outputDeviceID)
        } catch {
            throw fail("output_resolve", "\(error)")
        }
        let outputName = readObjectName(outputDeviceID)

        // 4. Private aggregate: output as main sub-device, tap in the tap list
        //    with drift compensation (AudioCap ProcessTap.prepare pattern).
        let description: [String: Any] = [
            kAudioAggregateDeviceNameKey: "MeetingScribe Tap",
            kAudioAggregateDeviceUIDKey: UUID().uuidString,
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
            throw fail("aggregate_create",
                       "AudioHardwareCreateAggregateDevice failed: \(osStatusString(err))")
        }

        // 5. Locate the tap's stream among the aggregate's input streams — a
        //    headphone/AirPods output sub-device can contribute its own mic
        //    stream, and the ABL orders buffers by stream. The tap stream is
        //    the one matching the tap format; sub-device streams come first,
        //    so on ambiguity take the last match.
        let inputStreams = readInputStreams(aggregateID)
        var tapBufferIndex = max(0, inputStreams.count - 1)
        for (i, stream) in inputStreams.enumerated() {
            guard let fmt = readStreamVirtualFormat(stream) else { continue }
            if fmt.mSampleRate == sourceFormat.mSampleRate
                && fmt.mChannelsPerFrame == sourceFormat.mChannelsPerFrame {
                tapBufferIndex = i
            }
        }

        // Source/destination conversion (rate, channels, int16, interleaving).
        var srcASBD = sourceFormat
        guard let srcAVFormat = AVAudioFormat(streamDescription: &srcASBD) else {
            throw fail("source_format", "unsupported tap format", aggregate: aggregateID)
        }
        let converter: AVAudioConverter?
        if srcAVFormat == dstFormat {
            converter = nil
        } else {
            guard let c = AVAudioConverter(from: srcAVFormat, to: dstFormat) else {
                throw fail("converter", "AVAudioConverter init failed "
                           + "(\(srcAVFormat) → \(dstFormat))", aggregate: aggregateID)
            }
            converter = c
        }

        // 6. IOProc on a real dispatch queue (trap 3), engine wired after.
        let box = EngineBox()
        var ioProcID: AudioDeviceIOProcID?
        err = AudioDeviceCreateIOProcIDWithBlock(&ioProcID, aggregateID, ioQueue) {
            _, inInputData, _, _, _ in
            box.engine?.ingest(inInputData)
        }
        guard err == noErr, let procID = ioProcID else {
            throw fail("ioproc_create",
                       "AudioDeviceCreateIOProcIDWithBlock failed: \(osStatusString(err))",
                       aggregate: aggregateID)
        }

        // Drive only the tap's input stream; never the output side and never a
        // sub-device microphone (Bluetooth would drop into HFP otherwise).
        setIOProcStreamUsage(device: aggregateID, ioProcID: procID,
                             scope: kAudioObjectPropertyScopeInput,
                             streamIsOn: inputStreams.indices.map { $0 == tapBufferIndex })
        let outputStreams = readOutputStreams(aggregateID)
        setIOProcStreamUsage(device: aggregateID, ioProcID: procID,
                             scope: kAudioObjectPropertyScopeOutput,
                             streamIsOn: outputStreams.map { _ in false })

        err = AudioDeviceStart(aggregateID, procID)
        guard err == noErr else {
            AudioDeviceDestroyIOProcID(aggregateID, procID)
            throw fail("device_start", "AudioDeviceStart failed: \(osStatusString(err))",
                       aggregate: aggregateID)
        }

        let engine = TapEngine(tapID: tapID, aggregateID: aggregateID, ioProcID: procID,
                               outputDeviceID: outputDeviceID, outputName: outputName,
                               sourceFormat: sourceFormat, srcAVFormat: srcAVFormat,
                               dstAVFormat: dstFormat, converter: converter,
                               tapBufferIndex: tapBufferIndex)
        box.engine = engine
        return engine
    }

    /// Retains the engine for the IO block without a chicken-and-egg problem
    /// (the block is created before the engine exists).
    private final class EngineBox {
        var engine: TapEngine?
    }

    private func ingest(_ inInputData: UnsafePointer<AudioBufferList>) {
        let buffers = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: inInputData))
        guard buffers.count > 0 else { return }

        // Pick the tap's buffer. Index-based first; if that slot is empty
        // (stream-usage quirks), fall back to the last non-empty buffer.
        var index = min(tapBufferIndex, buffers.count - 1)
        if buffers[index].mData == nil || buffers[index].mDataByteSize == 0 {
            for i in stride(from: buffers.count - 1, through: 0, by: -1)
            where buffers[i].mData != nil && buffers[i].mDataByteSize > 0 {
                index = i
                break
            }
        }
        let buffer = buffers[index]
        guard let base = buffer.mData, buffer.mDataByteSize > 0 else { return }
        let bytesPerFrame = max(1, Int(sourceFormat.mBytesPerFrame))
        let frames = Int(buffer.mDataByteSize) / bytesPerFrame
        guard frames > 0 else { return }

        // Zero-input detection on the RAW source bytes (denial = digital zeros).
        let rawBytes = UnsafeRawBufferPointer(start: base, count: Int(buffer.mDataByteSize))
        let zeroInput = !rawBytes.contains { $0 != 0 }

        var abl = AudioBufferList(mNumberBuffers: 1, mBuffers: buffer)
        let outData: Data? = withUnsafeMutablePointer(to: &abl) { ablPtr -> Data? in
            guard let inBuf = AVAudioPCMBuffer(pcmFormat: srcAVFormat,
                                               bufferListNoCopy: ablPtr, deallocator: nil) else {
                return nil
            }
            inBuf.frameLength = AVAudioFrameCount(frames)
            if let converter {
                let ratio = dstAVFormat.sampleRate / srcAVFormat.sampleRate
                let capacity = AVAudioFrameCount(Double(frames) * ratio) + 64
                guard let outBuf = AVAudioPCMBuffer(pcmFormat: dstAVFormat,
                                                    frameCapacity: capacity) else { return nil }
                var consumed = false
                var convErr: NSError?
                let status = converter.convert(to: outBuf, error: &convErr) { _, outStatus in
                    if consumed {
                        outStatus.pointee = .noDataNow
                        return nil
                    }
                    consumed = true
                    outStatus.pointee = .haveData
                    return inBuf
                }
                guard status != .error else { return nil }
                return Self.dataFrom(outBuf)
            }
            return Self.dataFrom(inBuf)
        }
        if let outData, !outData.isEmpty {
            onPCM?(outData, zeroInput)
        }
    }

    private static func dataFrom(_ buffer: AVAudioPCMBuffer) -> Data {
        let abl = buffer.audioBufferList.pointee.mBuffers
        guard let base = abl.mData, buffer.frameLength > 0 else { return Data() }
        let bytes = Int(buffer.frameLength) * Int(buffer.format.streamDescription.pointee.mBytesPerFrame)
        return Data(bytes: base, count: min(bytes, Int(abl.mDataByteSize)))
    }

    /// Strict teardown order: Stop → DestroyIOProcID → DestroyAggregate → DestroyTap.
    func stop() {
        guard !stopped else { return }
        stopped = true
        onPCM = nil
        AudioDeviceStop(aggregateID, ioProcID)
        AudioDeviceDestroyIOProcID(aggregateID, ioProcID)
        AudioHardwareDestroyAggregateDevice(aggregateID)
        AudioHardwareDestroyProcessTap(tapID)
    }
}

// MARK: - Controller: lifecycle, rebuilds, watchdog, signals

final class Controller {
    let rate: Int
    let channels: Int
    let chunkMs: Int
    let excludePIDs: [pid_t]

    private let control = DispatchQueue(label: "com.meetingscribe.syscap.control")
    private let ioQueue = DispatchQueue(label: "com.meetingscribe.syscap.io", qos: .userInitiated)
    private let sink: PCMSink
    private let dstFormat: AVAudioFormat
    private var engine: TapEngine?
    private var startNanos: UInt64 = 0
    private var shuttingDown = false

    // Silence watchdog: sustained digital zeros → one rebuild per episode
    // (macOS 26 zero-buffer bug), then a surfaced warning. Legitimate quiet
    // (nothing playing) also produces zeros, so the recorder pairs these
    // events with its own context before alarming the user.
    private var zeroFrameRun = 0
    private var silenceEpisodeHandled = false
    private let silenceRebuildFrames: Int
    private var pendingDeviceChange: DispatchWorkItem?
    private var deviceListenerBlock: AudioObjectPropertyListenerBlock?
    private var rateListenerBlock: AudioObjectPropertyListenerBlock?
    private var listenedRateDevice: AudioDeviceID = AudioObjectID(kAudioObjectUnknown)

    init(rate: Int, channels: Int, chunkMs: Int, excludePIDs: [pid_t]) {
        self.rate = rate
        self.channels = channels
        self.chunkMs = chunkMs
        self.excludePIDs = excludePIDs
        let bytesPerFrame = channels * 2
        self.sink = PCMSink(bytesPerFrame: bytesPerFrame,
                            chunkBytes: max(1, rate * chunkMs / 1000) * bytesPerFrame)
        self.silenceRebuildFrames = rate * 10
        guard let dst = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: Double(rate),
                                      channels: AVAudioChannelCount(channels), interleaved: true) else {
            fatalError("cannot build destination format \(rate)/\(channels)")
        }
        self.dstFormat = dst
        sink.onWriteFailure = { [weak self] in
            // stdout reader (the recorder) went away — nothing left to do.
            self?.control.async { self?.shutdown(code: 0) }
        }
    }

    func run() -> Never {
        control.sync {
            do {
                try startEngine()
            } catch let e as TapEngine.BuildError {
                Events.emit(["t": "error", "stage": e.stage, "message": e.message])
                exit(e.permissionSuspect ? 3 : 1)
            } catch {
                Events.emit(["t": "error", "stage": "build", "message": "\(error)"])
                exit(1)
            }
            startNanos = DispatchTime.now().uptimeNanoseconds
            installDeviceListener()
            installSignalHandlers()
            installParentWatch()
            Events.emit([
                "t": "ready", "rate": rate, "channels": channels, "chunk_ms": chunkMs,
                "source": ["rate": engine!.sourceFormat.mSampleRate,
                           "channels": Int(engine!.sourceFormat.mChannelsPerFrame)],
                "output": engine!.outputName,
            ])
        }
        dispatchMain()
    }

    // ---- engine lifecycle (control queue only) ----

    private func startEngine() throws {
        let e = try TapEngine.build(excludePIDs: excludePIDs, dstFormat: dstFormat, ioQueue: ioQueue)
        e.onPCM = { [weak self] data, zeroInput in
            self?.handlePCM(data, zeroInput: zeroInput)
        }
        engine = e
        installRateListener(for: e.outputDeviceID)
    }

    private func rebuild(reason: String) {
        guard !shuttingDown else { return }
        engine?.stop()
        engine = nil
        var delayMs: UInt32 = 250
        for attempt in 1...4 {
            do {
                try startEngine()
                fillTimelineGap()
                if reason == "device_changed" {
                    Events.emit(["t": "device_changed", "output": engine!.outputName])
                } else {
                    Events.emit(["t": "rebuilt", "reason": reason])
                }
                return
            } catch {
                if attempt == 4 {
                    Events.emit(["t": "error", "stage": "rebuild",
                                 "message": "rebuild after \(reason) failed: \(error)"])
                    shutdown(code: 1)
                }
                usleep(delayMs * 1000)
                delayMs *= 2
            }
        }
    }

    /// After a rebuild, pad the output with silence so the WAV's length keeps
    /// tracking wall-clock time (start_offset only aligns the beginning).
    private func fillTimelineGap() {
        let elapsed = Double(DispatchTime.now().uptimeNanoseconds - startNanos) / 1_000_000_000
        let expected = Int(elapsed * Double(rate))
        let deficit = expected - sink.framesAccepted
        // Only fill genuine gaps; cap at 10 minutes as a runaway guard.
        if deficit > rate / 20 && deficit < rate * 600 {
            sink.padSilence(frames: deficit)
        }
    }

    // ---- IO-queue path ----

    private func handlePCM(_ data: Data, zeroInput: Bool) {
        sink.append(data)
        let frames = data.count / (channels * 2)
        if zeroInput {
            let before = zeroFrameRun
            zeroFrameRun += frames
            if before < silenceRebuildFrames && zeroFrameRun >= silenceRebuildFrames
                && !silenceEpisodeHandled {
                silenceEpisodeHandled = true
                Events.emit(["t": "silence", "seconds": zeroFrameRun / rate])
                control.async { [weak self] in self?.rebuild(reason: "silence") }
            }
        } else {
            zeroFrameRun = 0
            silenceEpisodeHandled = false
        }
    }

    // ---- notifications ----

    private func installDeviceListener() {
        let block: AudioObjectPropertyListenerBlock = { [weak self] _, _ in
            guard let self else { return }
            // Debounce: device switches fire bursts of notifications.
            self.pendingDeviceChange?.cancel()
            let work = DispatchWorkItem { [weak self] in
                guard let self, !self.shuttingDown else { return }
                let current = (try? TapEngine.resolveRealOutput()) ?? AudioObjectID(kAudioObjectUnknown)
                if current != self.engine?.outputDeviceID {
                    self.rebuild(reason: "device_changed")
                }
            }
            self.pendingDeviceChange = work
            self.control.asyncAfter(deadline: .now() + .milliseconds(400), execute: work)
        }
        var address = propertyAddress(kAudioHardwarePropertyDefaultOutputDevice)
        let err = AudioObjectAddPropertyListenerBlock(kSystemObject, &address, control, block)
        if err == noErr { deviceListenerBlock = block }
    }

    /// The same physical device can change nominal rate mid-session (e.g. a
    /// display's audio switching 48 kHz ↔ 44.1 kHz); the tap format follows it.
    private func installRateListener(for device: AudioDeviceID) {
        removeRateListener()
        let block: AudioObjectPropertyListenerBlock = { [weak self] _, _ in
            guard let self, !self.shuttingDown else { return }
            self.pendingDeviceChange?.cancel()
            let work = DispatchWorkItem { [weak self] in
                self?.rebuild(reason: "device_changed")
            }
            self.pendingDeviceChange = work
            self.control.asyncAfter(deadline: .now() + .milliseconds(400), execute: work)
        }
        var address = propertyAddress(kAudioDevicePropertyNominalSampleRate)
        if AudioObjectAddPropertyListenerBlock(device, &address, control, block) == noErr {
            rateListenerBlock = block
            listenedRateDevice = device
        }
    }

    private func removeRateListener() {
        if let block = rateListenerBlock, listenedRateDevice != kAudioObjectUnknown {
            var address = propertyAddress(kAudioDevicePropertyNominalSampleRate)
            AudioObjectRemovePropertyListenerBlock(listenedRateDevice, &address, control, block)
        }
        rateListenerBlock = nil
        listenedRateDevice = AudioObjectID(kAudioObjectUnknown)
    }

    // ---- shutdown ----

    private func installSignalHandlers() {
        signal(SIGPIPE, SIG_IGN)
        for sig in [SIGTERM, SIGINT] {
            signal(sig, SIG_IGN)
            let source = DispatchSource.makeSignalSource(signal: sig, queue: control)
            source.setEventHandler { [weak self] in self?.shutdown(code: 0) }
            source.resume()
            signalSources.append(source)
        }
    }

    private var signalSources: [DispatchSourceSignal] = []

    /// If the recorder process dies without stopping us, don't keep a tap
    /// running as an orphan of launchd.
    private func installParentWatch() {
        let timer = DispatchSource.makeTimerSource(queue: control)
        timer.schedule(deadline: .now() + 2, repeating: 2)
        timer.setEventHandler { [weak self] in
            if getppid() == 1 { self?.shutdown(code: 0) }
        }
        timer.resume()
        parentWatch = timer
    }

    private var parentWatch: DispatchSourceTimer?

    private func shutdown(code: Int32) {
        guard !shuttingDown else { return }
        shuttingDown = true
        pendingDeviceChange?.cancel()
        removeRateListener()
        if let block = deviceListenerBlock {
            var address = propertyAddress(kAudioHardwarePropertyDefaultOutputDevice)
            AudioObjectRemovePropertyListenerBlock(kSystemObject, &address, control, block)
        }
        engine?.stop()
        engine = nil
        ioQueue.sync {}  // drain in-flight IO callbacks before the final flush
        sink.flush()
        exit(code)
    }
}

// MARK: - Main

@main
struct AppleSyscap {
    static func main() {
        var rate = 48000
        var channels = 2
        var chunkMs = 20
        var excludePIDs: [pid_t] = [ProcessInfo.processInfo.processIdentifier]

        var args = Array(CommandLine.arguments.dropFirst())
        func intArg(_ name: String) -> Int? {
            guard let v = args.first.flatMap({ Int($0) }) else {
                FileHandle.standardError.write(Data("\(name) needs an integer\n".utf8))
                exit(64)
            }
            args.removeFirst()
            return v
        }
        while !args.isEmpty {
            let a = args.removeFirst()
            switch a {
            case "--rate": rate = intArg(a) ?? rate
            case "--channels": channels = intArg(a) ?? channels
            case "--chunk-ms": chunkMs = intArg(a) ?? chunkMs
            case "--exclude-pid":
                if let v = intArg(a) { excludePIDs.append(pid_t(v)) }
            case "--help", "-h":
                print("usage: apple_syscap [--rate HZ] [--channels N] [--chunk-ms MS] [--exclude-pid PID]...")
                return
            default:
                FileHandle.standardError.write(Data("unknown argument: \(a)\n".utf8))
                exit(64)
            }
        }
        guard (8000...192_000).contains(rate), (1...2).contains(channels),
              (5...50).contains(chunkMs) else {
            FileHandle.standardError.write(Data("invalid --rate/--channels/--chunk-ms\n".utf8))
            exit(64)
        }

        Controller(rate: rate, channels: channels, chunkMs: chunkMs,
                   excludePIDs: excludePIDs).run()
    }
}
