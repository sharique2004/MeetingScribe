// Turns a stream of recorder snapshots into what happens on screen.
//
// ── Why this is its own type ──────────────────────────────────────────────────
// It used to be a closure inside AppDelegate, and it carried a defect that cost
// the owner a note. Two different decisions were being made by one `if`:
//
//     if !lastRecorder.recording {          // "the recording just started"
//         mainWindow.hide()                 //   ← visibility, correctly gated
//         notesPanel.recordingStarted(id)   //   ← IDENTITY, wrongly gated
//     }
//
// The status poll runs every 2 seconds. Stop meeting A and start meeting B
// inside one of those windows and the poller never observes a not-recording
// tick — it goes straight from (recording, A) to (recording, B) — so the guard
// is false on that path and the note-taker was never told the meeting had
// changed. Reproduced off the wire from a live panel:
//
//     recorder is now recording : 20260725-110000   (B)
//     panel was last told about : 20260725-100000   (A)
//     POST /api/record/note  meeting_id='20260725-100000'  t=7
//        text='B-NOTE: decision made in the SECOND meeting'
//
// A note typed in the second meeting, filed to the first, and stamped with the
// second meeting's clock. The backend honours the id it is given and does not
// second-guess it — correct by design, and the reason the client being wrong is
// the whole story. Meanwhile A's notes were still listed under "Notes this
// meeting" while B recorded, and A's own unconfirmed note was never flushed,
// because the stop that would have flushed it was never seen either.
//
// Back-to-back meetings are exactly the handover this panel exists for.
//
// ── The split, which is the fix ───────────────────────────────────────────────
//   IDENTITY   — which meeting the note-taker files against, whose notes are
//                listed, when a pending note is flushed — follows the recorder
//                UNCONDITIONALLY. Every change, by whichever route notices it
//                first. `NotesPanel.meetingChanged(to:)` is idempotent so the
//                routes can overlap freely.
//   VISIBILITY — putting the panel on screen — stays gated to the
//                not-recording -> recording edge, so a panel the owner
//                dismissed mid-meeting stays dismissed.
//
// ── Is a 2 s poll the right transport for identity? ───────────────────────────
// No, and it is no longer the only one. Identity now also rides the HUD's own
// recorder poll (RecordingPanel, 5 Hz, /api/record/status, which carries
// meeting_id), which has two properties the status poll does not:
//
//   * ~200 ms worst case instead of ~2 s.
//   * The id and the clock come from the SAME sample. `t` and `meeting_id` are
//     read off one response, so they can never name different meetings — which
//     is precisely the pairing that went wrong above.
//
// It is still polling, and honesty requires saying so: nothing here can be
// strictly authoritative while the transport is a pull. Making it authoritative
// needs a push from the recorder — an SSE or long-poll `/api/record/events`
// stream that emits on start/stop with the id, which the panel would treat as
// the truth and the polls as a fallback. That endpoint lives in app.py, which
// this task does not own, so it is written down here as the next step rather
// than half-built. What is owned is made correct: the client no longer has a
// blind window at all, only a bounded lag, and the two things that must agree
// are read together.

import Foundation

/// The screen-side effects of a recorder state change, injected so the whole
/// decision table can be exercised directly with the real panels behind it.
final class RecorderRouter {

    struct Sinks {
        /// The menu bar's recording indicator.
        var menuBarRecording: (Bool) -> Void = { _ in }
        /// Move the HUD's clock to this snapshot. Runs before anything is told
        /// the meeting changed, so the clock already belongs to the new meeting
        /// by the time the note-taker hears about it.
        var hudUpdate: (RecorderState) -> Void = { _ in }
        var hudShow: () -> Void = {}
        var hudHide: () -> Void = {}
        /// IDENTITY. Every change, unconditionally.
        var meetingChanged: (String?) -> Void = { _ in }
        /// VISIBILITY. The not-recording -> recording edge only.
        var showNotes: () -> Void = {}
        /// The recording ended: flush whatever is in the note field to the
        /// meeting that just ended, then put the panel away.
        var recordingStopped: () -> Void = {}
        var hideMainWindow: () -> Void = {}
        var showMainWindow: () -> Void = {}
    }

    private let sinks: Sinks

    /// The last snapshot seen, from any route. Read by the quit path to know
    /// whether a recording is still running.
    private(set) var last = RecorderState()

    init(sinks: Sinks) {
        self.sinks = sinks
    }

    /// A snapshot in which something changed — the recorder started, stopped,
    /// or moved to a different meeting.
    func apply(_ state: RecorderState) {
        let previous = last
        // Adopt the new truth before any sink runs: a sink that calls back in
        // (the quit alert, a panel asking what is recording) must not be able
        // to see the state this call is in the middle of leaving behind.
        last = state

        sinks.menuBarRecording(state.recording)

        if state.recording {
            sinks.hudUpdate(state)
            sinks.hudShow()
            // Identity, ungated. This is the line whose absence filed a note
            // typed in B under A: on the A -> B handover `previous.recording`
            // is true, so everything inside the edge guard below is skipped.
            sinks.meetingChanged(state.meetingID)
            if !previous.recording {
                // Stealth: on the start transition only, tuck the main window
                // away so just the small floating panels are on screen. The
                // Dock icon or menu bar can still bring it back mid-recording.
                sinks.hideMainWindow()
                // Only on the transition — so dismissing the note-taker
                // mid-meeting keeps it dismissed.
                sinks.showNotes()
            }
        } else {
            sinks.hudHide()
            sinks.recordingStopped()
            // Recording ended: bring the app back so the user sees the
            // processing state and, soon after, the transcript.
            if previous.recording { sinks.showMainWindow() }
        }
    }

    /// An unchanged snapshot from the status poll's per-tick callback.
    ///
    /// Identity is re-offered here too. It costs nothing (the sink is a no-op
    /// unless the id actually moved) and it means a change that somehow reached
    /// the poller without producing a change event is still delivered on the
    /// very next tick rather than never.
    func tick(_ state: RecorderState) {
        last = state
        if state.recording { sinks.meetingChanged(state.meetingID) }
    }

    /// Identity straight off the HUD's 5 Hz recorder poll — the fast route.
    /// Never used to decide visibility: the HUD seeing a new id is not the same
    /// event as a recording starting.
    func liveMeetingID(_ id: String) {
        sinks.meetingChanged(id)
    }
}
