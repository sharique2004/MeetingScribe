// The meeting screen: loads one meeting's detail, notes and waveform, then
// renders the document or its transcript mode with the single floating
// glass bar (Ask ↔ transport) over the bottom.
import SwiftUI
import AVFoundation

@MainActor
final class MeetingModel: ObservableObject {
    @Published var detail: MeetingDetail?
    @Published var notes: [MeetingNote] = []
    @Published var waveform: WaveformData?
    @Published var error: String?

    func load(_ id: String) async {
        do {
            detail = try await API.meeting(id)
            error = nil
        } catch {
            self.error = "Couldn't load this meeting."
            return
        }
        if let bundled = detail?.notes, !bundled.isEmpty {
            notes = bundled
        } else {
            notes = (try? await API.notes(id)) ?? []
        }
        waveform = try? await API.waveform(id)
    }
}

struct MeetingScreen: View {
    let meetingID: String
    @Binding var mode: PageMode
    @StateObject private var model = MeetingModel()
    @StateObject private var player = Playback()

    var body: some View {
        Group {
            if let detail = model.detail {
                ZStack {
                    if mode == .document {
                        MeetingPage(detail: detail, notes: model.notes, player: player,
                                    mode: $mode) { seekTo in
                            withAnimation(Motion.enter) { mode = .transcript }
                            if let t = seekTo {
                                player.prepare(meetingID: detail.id)
                                player.seek(t)
                                player.play()
                            }
                        }
                        .transition(.opacity)
                    } else {
                        TranscriptPage(detail: detail, player: player, mode: $mode)
                            .transition(.opacity)
                    }
                }
                .overlay(alignment: .bottom) {
                    FloatingBar(detail: detail, waveform: model.waveform, player: player)
                }
            } else if let err = model.error {
                ContentUnavailableView(err, systemImage: "bolt.slash")
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                    .background(MS.content)
            } else {
                ProgressView().controlSize(.large)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                    .background(MS.content)
            }
        }
        .navigationTitle("")
        .task {
            await model.load(meetingID)
            if let id = model.detail?.id { player.prepare(meetingID: id) }
        }
        .onDisappear { player.pause() }
    }
}

// MARK: - Playback

@MainActor
final class Playback: ObservableObject {
    @Published var playing = false
    @Published var time: Double = 0
    @Published var track = "mic"
    private var player: AVPlayer?
    private var meetingID: String?
    private var observer: Any?

    func prepare(meetingID: String) {
        guard self.meetingID != meetingID || player == nil else { return }
        self.meetingID = meetingID
        rebuild(seekTo: 0)
    }

    func toggle(meetingID: String) {
        prepare(meetingID: meetingID)
        playing ? pause() : play()
    }

    func play() {
        player?.play()
        playing = true
    }

    func pause() {
        player?.pause()
        playing = false
    }

    func seek(_ t: Double) {
        time = t
        player?.seek(to: CMTime(seconds: t, preferredTimescale: 600))
    }

    func switchTrack(_ newTrack: String) {
        guard newTrack != track else { return }
        track = newTrack
        rebuild(seekTo: time)
    }

    private func rebuild(seekTo t: Double) {
        guard let id = meetingID else { return }
        if let observer, let player { player.removeTimeObserver(observer) }
        let wasPlaying = playing
        let p = AVPlayer(url: API.audioURL(id, track: track))
        p.automaticallyWaitsToMinimizeStalling = false
        observer = p.addPeriodicTimeObserver(
            forInterval: CMTime(seconds: 0.25, preferredTimescale: 600), queue: .main
        ) { [weak self] t in
            let seconds = t.seconds
            Task { @MainActor in self?.time = seconds }
        }
        player = p
        if t > 0 { p.seek(to: CMTime(seconds: t, preferredTimescale: 600)) }
        if wasPlaying { p.play() }
    }
}
