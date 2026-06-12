// Today's calendar events as JSON, via EventKit (fully local, no cloud API).
//
// MeetingScribe uses this to auto-name recordings after the calendar event
// that is happening, and to pre-fill the expected speaker count from the
// attendee list. Reads every calendar the Mac knows about (iCloud, Google,
// Outlook accounts added to macOS Calendar).
//
// Output: [{"title":"Weekly sync","start":1765400400,"end":1765404000,
//           "calendar":"Work","attendees":3,"organizer":"Jess"}]
//   start/end are Unix epoch seconds; attendees excludes the current user;
//   all-day events are skipped (they aren't meetings).
//
// Build:  swiftc -O tools/calendar_events.swift -o ~/.meetingscribe/bin/calendar_events
// Usage:  calendar_events            (start of today .. end of today)
//
// Exit codes: 0 ok · 3 calendar access denied or EventKit failure.
// The first run shows the macOS calendar-permission prompt once.

import EventKit
import Foundation

struct EventOut: Codable {
    let title: String
    let start: Double
    let end: Double
    let calendar: String
    let attendees: Int
    let organizer: String?
}

func die(_ message: String, code: Int32 = 3) -> Never {
    FileHandle.standardError.write(Data((message + "\n").utf8))
    exit(code)
}

let store = EKEventStore()
let semaphore = DispatchSemaphore(value: 0)
var granted = false
var accessError: String?

switch EKEventStore.authorizationStatus(for: .event) {
case .fullAccess:
    granted = true
case .denied, .restricted, .writeOnly:
    die("calendar access denied — allow it in System Settings → Privacy & Security → Calendars")
default:  // .notDetermined — ask (shows the one-time system prompt)
    store.requestFullAccessToEvents { ok, error in
        granted = ok
        accessError = error?.localizedDescription
        semaphore.signal()
    }
    _ = semaphore.wait(timeout: .now() + 120)
}

guard granted else {
    die("calendar access not granted\(accessError.map { " (\($0))" } ?? "")")
}

let cal = Calendar.current
let now = Date()
let dayStart = cal.startOfDay(for: now)
guard let dayEnd = cal.date(byAdding: .day, value: 1, to: dayStart) else {
    die("could not compute day bounds")
}

let predicate = store.predicateForEvents(withStart: dayStart, end: dayEnd, calendars: nil)
let events = store.events(matching: predicate)

var out: [EventOut] = []
for event in events where !event.isAllDay {
    guard let start = event.startDate, let end = event.endDate else { continue }
    let all = event.attendees ?? []
    var others = all.filter { !$0.isCurrentUser }
    // Google-synced calendars often report isCurrentUser=false for the user
    // themselves; if nobody matched, assume one of the attendees is the user.
    if !all.isEmpty && others.count == all.count {
        others.removeLast()
    }
    out.append(EventOut(
        title: event.title?.trimmingCharacters(in: .whitespacesAndNewlines) ?? "Untitled event",
        start: start.timeIntervalSince1970,
        end: end.timeIntervalSince1970,
        calendar: event.calendar?.title ?? "",
        attendees: others.count,
        organizer: event.organizer?.name
    ))
}
out.sort { $0.start < $1.start }

let data = try JSONEncoder().encode(out)
FileHandle.standardOutput.write(data)
