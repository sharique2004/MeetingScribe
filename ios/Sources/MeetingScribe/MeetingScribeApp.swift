// MeetingScribe for iPhone.
//
// The Mac app is a two-pane library over a Python engine that captures both
// sides of a call. This is not that, and pretending otherwise would ship a
// lie: iOS has no way to tap another app's audio, so the phone records the
// room through the microphone. That makes it the in-person half of the same
// product — the meeting at a table, the conversation in a corridor — with the
// same transcription (SpeechAnalyzer) and the same summariser (Apple
// Intelligence) the Mac already uses, and no server in the middle.
import SwiftUI

@main
struct MeetingScribeApp: App {
    @State private var store = MeetingStore()

    var body: some Scene {
        WindowGroup {
            LibraryView()
                .environment(store)
                .tint(MS.interactive)
        }
    }
}
