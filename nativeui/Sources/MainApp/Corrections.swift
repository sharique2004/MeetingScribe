// Correcting the machine: the speaker count.
//
// The engine guesses how many people are in a meeting, and when it guesses
// wrong every line of the transcript is attributed to the wrong person, the
// talk-share bars lie, and the summary quotes the wrong speaker. It is the
// single highest-value correction a person can make to a transcript, the
// engine has taken it since forever (POST /api/meetings/<id>/recluster,
// usually well under a second because the voice analysis is already on disk),
// the old web UI had a control for it, and the native app shipped without
// one. This is that control, in the native idiom.
import SwiftUI

struct SpeakerCountSheet: View {
    let meetingID: String
    let corrections: API.Corrections?
    /// Hand back the picked count so the host can run it and refresh: nil is
    /// "Auto". The host owns the request because it also owns the reload.
    let apply: (Int?) async -> (Bool, String?)
    @Binding var presented: Bool

    @State private var current: Int?
    @State private var busy = false
    @State private var progress: String?
    @State private var flash: String?
    @State private var failure: String?
    @State private var progressWatch: Task<Void, Never>?

    private var micFallback: Bool { corrections?.diarization_mode == "mic_fallback" }
    private var label: String { SpeakerCount.label(corrections) }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Text("SPEAKER DETECTION")
                .font(MSFont.kicker)
                .kerning(0.55)
                .foregroundStyle(MS.ink3)
                .accessibilityAddTraits(.isHeader)

            Text("How many voices are in this meeting?")
                .font(MSFont.pageTitle)
                .foregroundStyle(MS.ink)
                .padding(.top, 8)
                .fixedSize(horizontal: false, vertical: true)

            Text(label)
                .font(MSFont.meta)
                .foregroundStyle(MS.ink2)
                .padding(.top, 6)
                .fixedSize(horizontal: false, vertical: true)

            counts
                .padding(.top, 18)

            Text(explanation)
                .font(MSFont.meta)
                .foregroundStyle(MS.ink3)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.top, 16)

            if micFallback {
                NoticeBand(icon: "exclamationmark.triangle", text: micFallbackText)
                    .padding(.top, 14)
            }

            status
                .padding(.top, 14)

            Spacer(minLength: 18)

            HStack {
                Spacer()
                Button("Done") { presented = false }
                    .keyboardShortcut(.defaultAction)
            }
        }
        .padding(28)
        .frame(minWidth: 430, idealWidth: 460, maxWidth: 560,
               minHeight: 360, idealHeight: 420, maxHeight: 620)
        .background(MS.content)
        .onAppear { current = SpeakerCount.displayed(corrections) }
        .onDisappear { progressWatch?.cancel() }
    }

    // MARK: - The picker

    private var counts: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                chip(title: "Auto", value: nil, wide: true)
                if SpeakerCount.offList(current), let value = current {
                    // A number this control cannot send, yet which is in
                    // force. Shown, and disabled, because falling back to
                    // "Auto" would claim a meeting is on auto-detection when
                    // a count a person chose is still being applied to it.
                    Text(value == 0 ? "0 (just you)" : "\(value)")
                        .font(MSFont.chromeMedium)
                        .foregroundStyle(MS.ink2)
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 9)
                        .background(chipBackground(selected: true))
                        .accessibilityLabel(value == 0
                            ? "Currently set to just you. Pick a number to change it."
                            : "Currently set to \(value) voices")
                }
            }
            HStack(spacing: 8) {
                ForEach(1...8, id: \.self) { n in
                    chip(title: "\(n)", value: n, wide: false)
                }
            }
        }
    }

    private func chip(title: String, value: Int?, wide: Bool) -> some View {
        let selected = !SpeakerCount.offList(current) && current == value
        return Button {
            pick(value)
        } label: {
            Text(title)
                .font(MSFont.chromeMedium)
                .foregroundStyle(selected ? MS.ink : MS.ink2)
                .frame(maxWidth: .infinity)
                .padding(.vertical, 9)
                .background(chipBackground(selected: selected))
                .contentShape(Rectangle())
        }
        .buttonStyle(PressStyle())
        .disabled(busy)
        .accessibilityLabel(value == nil
            ? "Automatic, let the engine decide"
            : "\(value ?? 0) voices")
        .accessibilityAddTraits(selected ? [.isSelected] : [])
        .accessibilityHint(label)
    }

    /// The chips keep their well; only the outline changes hands — the edge
    /// rim where the chip is merely sitting there, the mint ring where it is
    /// the count in force.
    private func chipBackground(selected: Bool) -> some View {
        MS.rr(MS.radius.md)
            .fill(selected ? MS.raised : MS.raised.opacity(0.5))
            .overlay(
                MS.rr(MS.radius.md)
                    .strokeBorder(selected ? MS.interactive.opacity(0.55) : MS.edgeRim,
                                  lineWidth: 1))
    }

    // MARK: - What happened

    @ViewBuilder
    private var status: some View {
        if busy {
            HStack(spacing: 9) {
                ProgressView().controlSize(.small)
                Text(progress ?? "Re-clustering the voices…")
                    .font(MSFont.meta)
                    .foregroundStyle(MS.ink2)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .accessibilityElement(children: .combine)
        } else if let failure {
            NoticeBand(icon: "exclamationmark.triangle", text: failure)
        } else if let flash {
            HStack(spacing: 7) {
                Image(systemName: "checkmark.circle")
                    .font(MSFont.meta)
                    .foregroundStyle(MS.playhead)
                    .msDecorative()
                Text(flash)
                    .font(MSFont.meta)
                    .foregroundStyle(MS.ink2)
            }
            .accessibilityElement(children: .combine)
        }
    }

    private var explanation: String {
        if micFallback {
            return "Set how many voices are on the microphone. Pick 1 if it was only you and the \u{201C}You\u{201D} label comes back."
        }
        return "Wrong split? Set the real number. The voice analysis is already saved, so the whole meeting is re-labelled from it in about a second, with no re-transcription and no new recording. Names you have typed are kept where the engine can still match them."
    }

    private var micFallbackText: String {
        "The call audio wasn't captured, so everyone including you was recorded on the microphone. There is no way to tell which of these voices is yours, so rename them on the meeting page. If it was only you, set the count to 1."
    }

    // MARK: - Apply

    private func pick(_ value: Int?) {
        guard !busy else { return }
        busy = true
        failure = nil
        flash = nil
        progress = nil
        watchProgress()
        let started = Date()
        Task {
            let (ok, message) = await apply(value)
            progressWatch?.cancel()
            progressWatch = nil
            busy = false
            progress = nil
            guard ok else {
                failure = message ?? "The engine wouldn't re-cluster this meeting."
                return
            }
            current = value
            let seconds = String(format: "%.2f", Date().timeIntervalSince(started))
            msWithAnimation(Motion.enter) {
                flash = value == nil
                    ? "Back on automatic detection in \(seconds) s."
                    : "Re-clustered into \(value ?? 0) in \(seconds) s."
            }
        }
    }

    /// A re-cluster is normally instant, but a track the engine never embedded
    /// runs a full voice pass inside the request, which is seconds to minutes
    /// on a long meeting. The engine publishes its own line for that on
    /// /api/status under `recluster_jobs`, so a slow run reports its progress
    /// instead of leaving a mute spinner to look like a hang.
    private func watchProgress() {
        progressWatch?.cancel()
        progressWatch = Task {
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 700_000_000)
                guard !Task.isCancelled else { return }
                let message = await API.reclusterProgress(meetingID)
                guard !Task.isCancelled else { return }
                progress = message
            }
        }
    }
}
