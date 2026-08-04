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
    @Published var summarizing = false
    @Published var summaryProgress: String?
    @Published var summaryError: String?
    private var meetingID: String?

    func load(_ id: String) async {
        meetingID = id
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
        // Opened while the engine is still transcribing? Watch the work, so
        // the transcript appears the moment it lands instead of the next
        // time the user happens to navigate here.
        if isWorking(detail?.status) {
            watchProcessing(id)
        }
        // A summary may already be writing (auto-run after transcription, or
        // kicked off elsewhere) — pick the job up so progress is visible.
        if let job = await API.summaryJob(id), job.state == "processing" {
            summarizing = true
            summaryProgress = job.message
            watchSummaryJob(id)
        }
    }

    private func isWorking(_ status: String?) -> Bool {
        ["recording", "processing"].contains(status ?? "done")
    }

    /// Poll the document while the engine works on it. Nothing else refreshes
    /// an OPEN meeting page: the sidebar's watcher updates the LIST rows, and
    /// this model used to fetch exactly once — so a meeting opened mid-
    /// transcription showed "Processing" until the user clicked away and
    /// back. Ends on its own when the work does, or when the page moves to
    /// another meeting.
    private func watchProcessing(_ id: String) {
        Task {
            while meetingID == id {
                try? await Task.sleep(nanoseconds: 2_000_000_000)
                guard meetingID == id else { return }
                guard let fresh = try? await API.meeting(id) else { continue }
                guard meetingID == id else { return }
                detail = fresh
                if isWorking(fresh.status) { continue }
                // The work landed: bring the sidecars the first load found
                // empty, and pick up the auto-run summary job if one started.
                if let bundled = fresh.notes, !bundled.isEmpty {
                    notes = bundled
                }
                waveform = (try? await API.waveform(id)) ?? waveform
                if !summarizing, let job = await API.summaryJob(id),
                   job.state == "processing" {
                    summarizing = true
                    summaryProgress = job.message
                    watchSummaryJob(id)
                }
                return
            }
        }
    }

    /// Re-fetch the document after an edit the engine made for us — a
    /// speaker rename rewrites the speaker map that the page, the ribbon and
    /// the transcript all read from.
    func reload() async {
        guard let id = meetingID, let fresh = try? await API.meeting(id) else { return }
        detail = fresh
    }

    func summarize() {
        guard let id = meetingID, !summarizing else { return }
        summaryError = nil
        Task {
            let (ok, err) = await API.summarize(id)
            if ok {
                summarizing = true
                summaryProgress = "Summarizing…"
                watchSummaryJob(id)
            } else {
                summaryError = err
            }
        }
    }

    private func watchSummaryJob(_ id: String) {
        Task {
            while summarizing, meetingID == id {
                try? await Task.sleep(nanoseconds: 2_000_000_000)
                guard let job = await API.summaryJob(id) else { continue }
                summaryProgress = job.message
                if job.state == "done" {
                    detail = try? await API.meeting(id)
                    summarizing = false
                    summaryProgress = nil
                } else if job.state == "error" {
                    summarizing = false
                    summaryProgress = nil
                    summaryError = job.message ?? "The summary failed."
                }
            }
        }
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
                                    model: model, mode: $mode) { seekTo in
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
