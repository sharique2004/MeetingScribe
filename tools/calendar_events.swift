// Today's calendar events as JSON, via EventKit (fully local, no cloud API).
//
// MeetingScribe uses this to auto-name recordings after the calendar event
// that is happening, and to pre-fill the expected speaker count from the
// attendee list. Reads every calendar the Mac knows about (iCloud, Google,
// Outlook accounts added to macOS Calendar).
//
// Output: [{"title":"Weekly sync","start":1765400400,"end":1765404000,
//           "calendar":"Work","attendees":3,"organizer":"Alex Rivera"}]
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
    let names: [String]   // attendee display names (excludes the current user)
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

// ---- Identifying the account owner among the attendees ----------------------
// Google-synced calendars report isCurrentUser == false for EVERY attendee,
// including the account owner, so EventKit alone can't say which participant is
// "me". We identify that person instead: by e-mail against the addresses macOS
// already knows for this Mac's calendar accounts, or by the local account's
// full name (the same string summarize.py hands the LLM as "the local user").
//
// When no match is found we fall back to dropping the last entry — the historic
// behaviour — so the attendee COUNT is always "everyone but me" either way.
// Only *which* person is dropped (and therefore the names list) improves.

/// A lowercased e-mail address, or nil if the string isn't one.
func emailLike(_ raw: String?) -> String? {
    guard let raw = raw else { return nil }
    let s = raw.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
    guard s.contains("@"), !s.contains(" ") else { return nil }
    return s
}

/// A participant's address: EventKit exposes it as a mailto: URL.
func participantEmail(_ person: EKParticipant) -> String? {
    let url = person.url
    if let scheme = url.scheme, scheme.lowercased() == "mailto" {
        let body = String(url.absoluteString.dropFirst(scheme.count + 1))
        if let mail = emailLike(body.removingPercentEncoding ?? body) { return mail }
    }
    return emailLike(url.absoluteString) ?? emailLike(person.name)
}

// Addresses belonging to this Mac. Account (source) titles are the strongest
// signal — adding a Google/Exchange account names the source after the address.
// Writable, non-subscribed calendar titles are the weaker second pass: a Google
// primary calendar is titled with the owner's address, whereas a read-only or
// subscribed calendar may be titled with somebody else's.
var accountEmails = Set<String>()
var calendarEmails = Set<String>()
for calendar in store.calendars(for: .event) {
    guard calendar.allowsContentModifications, !calendar.isSubscribed else { continue }
    if let mail = emailLike(calendar.source?.title) { accountEmails.insert(mail) }
    if let mail = emailLike(calendar.title) { calendarEmails.insert(mail) }
}
let selfName = NSFullUserName().trimmingCharacters(in: .whitespacesAndNewlines).lowercased()

/// Index of the account owner in `people`, or nil when identity is unknown.
func currentUserIndex(among people: [EKParticipant], organizer: EKParticipant?) -> Int? {
    let emails = people.map { participantEmail($0) }
    // EventKit sometimes flags the organizer even when it flags no attendee.
    if let organizer = organizer, organizer.isCurrentUser,
       let mine = participantEmail(organizer),
       let i = emails.firstIndex(of: mine) {
        return i
    }
    for (i, mail) in emails.enumerated() where mail != nil && accountEmails.contains(mail!) {
        return i
    }
    for (i, mail) in emails.enumerated() where mail != nil && calendarEmails.contains(mail!) {
        return i
    }
    if !selfName.isEmpty {
        for (i, person) in people.enumerated()
        where person.name?.trimmingCharacters(in: .whitespacesAndNewlines).lowercased() == selfName {
            return i
        }
    }
    return nil
}

let predicate = store.predicateForEvents(withStart: dayStart, end: dayEnd, calendars: nil)
let events = store.events(matching: predicate)

var out: [EventOut] = []
for event in events where !event.isAllDay {
    guard let start = event.startDate, let end = event.endDate else { continue }
    let all = event.attendees ?? []
    var others = all.filter { !$0.isCurrentUser }
    // Nobody was flagged as the current user (the Google-sync case): remove the
    // account owner. Identify them if we can, else drop the last entry — either
    // way exactly one participant goes, so `attendees` stays total-minus-one.
    if !all.isEmpty && others.count == all.count {
        let mine = currentUserIndex(among: others, organizer: event.organizer)
        others.remove(at: mine ?? others.count - 1)
    }
    // Display names for speech-recognition biasing; skip bare emails.
    var names: [String] = []
    for person in others {
        guard let n = person.name?.trimmingCharacters(in: .whitespacesAndNewlines),
              !n.isEmpty, !n.contains("@"), !names.contains(n) else { continue }
        names.append(n)
    }
    out.append(EventOut(
        title: event.title?.trimmingCharacters(in: .whitespacesAndNewlines) ?? "Untitled event",
        start: start.timeIntervalSince1970,
        end: end.timeIntervalSince1970,
        calendar: event.calendar?.title ?? "",
        attendees: others.count,
        organizer: event.organizer?.name,
        names: names
    ))
}
out.sort { $0.start < $1.start }

let data = try JSONEncoder().encode(out)
FileHandle.standardOutput.write(data)
