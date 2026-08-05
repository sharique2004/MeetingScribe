// Recording, and everything that happens after Stop.
//
// One screen for the whole arc — record, transcribe, summarise — because on a
// phone the alternative is dumping the user back in a list where a row says
// "Processing" and nothing explains what for. The Mac shipped exactly that bug
// and it took a poller and two follow-up fixes to undo.
import SwiftUI

struct RecordingView: View {
    @Environment(MeetingStore.self) private var store
    @Environment(\.dismiss) private var dismiss

    @State private var capture = AudioCaptureService()
    @State private var phase: Phase = .idle
    @State private var progressLine = ""
    @State private var error: String?
    @State private var showError = false

    /// Decided once, when recording starts, and carried to the end.
    ///
    /// These were recomputed at Stop from `Date()` minus the elapsed time,
    /// which is not the instant recording began — the two drifted by six
    /// seconds on the first real run, so meeting.json was written into one
    /// folder while the audio sat in another and the recording was orphaned
    /// on disk. A recording's identity is fixed the moment it starts.
    @State private var meetingID = ""
    @State private var startedAt = Date()

    private enum Phase: Equatable {
        case idle, recording, working, done
    }

    var body: some View {
        VStack(spacing: 26) {
            header
            Spacer()
            centrepiece
            Spacer()
            controls
        }
        .padding(28)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(MS.content)
        .alert("Couldn't record", isPresented: $showError) {
            Button("OK") { error = nil }
        } message: {
            Text(error ?? "")
        }
        .task { await beginIfNeeded() }
        .interactiveDismissDisabled(phase == .recording || phase == .working)
    }

    private var header: some View {
        HStack {
            Button("Close", systemImage: "xmark") { dismiss() }
                .labelStyle(.iconOnly)
                .foregroundStyle(MS.ink2)
                .disabled(phase == .recording || phase == .working)
            Spacer()
            Text(phase == .recording ? "Recording" : "MeetingScribe")
                .font(MSFont.kicker)
                .kerning(0.55)
                .foregroundStyle(MS.ink3)
            Spacer()
            // Balances the close button so the title sits centred.
            Image(systemName: "xmark").opacity(0).accessibilityHidden(true)
        }
    }

    @ViewBuilder
    private var centrepiece: some View {
        switch phase {
        case .idle:
            ProgressView()
        case .recording:
            VStack(spacing: 18) {
                Text(clock(capture.elapsed))
                    .font(.system(size: 56, weight: .light, design: .rounded))
                    .monospacedDigit()
                    .foregroundStyle(MS.ink)
                    .contentTransition(.numericText())
                LevelMeter(level: capture.level)
                if capture.silent {
                    Notice(icon: "exclamationmark.triangle",
                           text: "This recording is hearing nothing. Check that no other app is holding the microphone.")
                } else if capture.interrupted {
                    Notice(icon: "pause.circle",
                           text: "Something else took the microphone. Recording resumes when it lets go.")
                } else {
                    Text("The microphone is picking up the room. The far side of a phone or video call is not included: iOS gives no app access to another app's audio.")
                        .font(MSFont.meta)
                        .foregroundStyle(MS.ink3)
                        .multilineTextAlignment(.center)
                }
            }
        case .working:
            VStack(spacing: 14) {
                ProgressView()
                Text(progressLine)
                    .font(MSFont.body)
                    .foregroundStyle(MS.ink2)
                    .multilineTextAlignment(.center)
                Text("On this iPhone. Nothing is uploaded.")
                    .font(MSFont.meta)
                    .foregroundStyle(MS.ink3)
            }
        case .done:
            VStack(spacing: 10) {
                Image(systemName: "checkmark.circle")
                    .font(.system(size: 42))
                    .foregroundStyle(MS.interactive)
                Text("Saved to your meetings")
                    .font(MSFont.body)
                    .foregroundStyle(MS.ink2)
            }
        }
    }

    @ViewBuilder
    private var controls: some View {
        switch phase {
        case .recording:
            Button {
                Task { await finish() }
            } label: {
                Label("Stop", systemImage: "stop.fill")
                    .font(.system(size: 17, weight: .semibold))
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 14)
            }
            .buttonStyle(.borderedProminent)
            .tint(MS.recordRed)
        case .done:
            Button("Done") { dismiss() }
                .buttonStyle(.borderedProminent)
                .tint(MS.interactive)
                .frame(maxWidth: .infinity)
        default:
            Color.clear.frame(height: 1)
        }
    }

    // MARK: - The arc

    private func beginIfNeeded() async {
        guard phase == .idle else { return }
        startedAt = Date()
        meetingID = Meeting.makeID(at: startedAt)
        do {
            let dir = try store.prepareDirectory(id: meetingID)
            try await capture.start(writingTo: dir.appending(path: "audio.caf"))
            phase = .recording
        } catch {
            self.error = error.localizedDescription
            showError = true
            phase = .idle
        }
    }

    private func finish() async {
        let (duration, heardSound) = capture.stop()
        phase = .working
        progressLine = "Finishing the recording"

        // The id and the start instant come from where recording BEGAN, not
        // from arithmetic on the stop time. See the note on those properties.
        var meeting = Meeting(id: meetingID,
                              title: Meeting.defaultTitle(at: startedAt),
                              created: startedAt,
                              duration: duration,
                              audioFilename: "audio.caf",
                              turns: [],
                              summary: nil,
                              warning: nil)

        // A recording that heard nothing gets saved anyway, and says why.
        // Throwing it away would be deciding for the user that their meeting
        // did not happen.
        if !heardSound {
            meeting.warning = "This recording is silent: no sound reached the microphone. The audio is kept, but there is nothing to transcribe."
            try? store.save(meeting)
            phase = .done
            return
        }

        let audioURL = store.directory(for: meeting).appending(path: "audio.caf")
        do {
            meeting.turns = try await MeetingTranscriber.transcribe(url: audioURL) { line in
                progressLine = line
            }
            try store.save(meeting)
        } catch {
            meeting.warning = "Couldn't transcribe this recording: \(error.localizedDescription)"
            try? store.save(meeting)
            phase = .done
            return
        }

        guard !meeting.turns.isEmpty else {
            meeting.warning = "Nothing recognisable was said in this recording."
            try? store.save(meeting)
            phase = .done
            return
        }

        progressLine = "Writing the summary"
        do {
            meeting.summary = try await MeetingSummariser.summarise(turns: meeting.turns)
        } catch {
            // A missing summary is not a missing meeting. The transcript is
            // already saved; record why the summary is absent and move on.
            meeting.warning = error.localizedDescription
        }
        try? store.save(meeting)
        phase = .done
    }
}

private struct LevelMeter: View {
    let level: Float

    var body: some View {
        GeometryReader { geo in
            ZStack(alignment: .leading) {
                Capsule().fill(MS.ink4.opacity(0.2))
                Capsule().fill(MS.recordRed)
                    .frame(width: max(4, geo.size.width * CGFloat(min(1, level * 3))))
            }
        }
        .frame(height: 6)
        .animation(.easeOut(duration: 0.15), value: level)
        .accessibilityHidden(true)
    }
}

private struct Notice: View {
    let icon: String
    let text: String

    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: icon)
                .foregroundStyle(MS.ink2)
            Text(text)
                .font(MSFont.meta)
                .foregroundStyle(MS.ink2)
        }
        .padding(12)
        .background(MS.raised, in: .rect(cornerRadius: 12))
    }
}
