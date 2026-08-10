// The app had no way of telling anyone it was out of date.
//
// MeetingScribe ships as a DMG off GitHub Releases, which means every copy
// ever downloaded is frozen at the version it was downloaded at: there is no
// App Store to nag, no Sparkle feed, and nothing in the app has ever looked
// outward at all. A fix shipped on Tuesday reaches a user on the day they
// happen to visit the site again, which for most of them is never — and the
// things this app fixes are things like a system-audio tap that silently
// recorded zeros. Sitting on an old build here is not a cosmetic problem.
//
// So: one read of the public releases API, a few seconds after launch and
// once a day after that, compared against this bundle's own version. It
// downloads nothing and installs nothing — the answer is always a link to the
// canonical DMG and a human deciding. Three surfaces carry it, deliberately
// unequal in loudness:
//
//   • a notification, once per version, EVER — the automatic path's one
//     interruption, and it never repeats itself (see notifyOnce)
//   • "Check for Updates…" in the app menu, which answers out loud either way
//   • a line in the Settings footer, which is the quiet one that is always
//     there for a user who dismissed the banner three weeks ago
//
// WHAT IT COSTS THE USER: one unauthenticated GitHub request a day, carrying
// no identifier of any kind, conditional on an ETag so the ordinary answer is
// a 304 with an empty body. Nothing about them or their meetings leaves the
// Mac — which is the promise the whole product is built on, and an update
// checker is exactly the sort of thing that quietly breaks it elsewhere.
import AppKit
import Foundation
import os

/// The canonical download link: our own domain, never the GitHub asset URL.
///
/// It redirects to releases/latest (mobile/vercel.json), so when the file
/// moves to different hosting only that redirect changes and every published
/// link keeps working — this one, the landing page's, the install script's.
/// Same doctrine, and the same reasoning, as mobile/src/Landing.jsx.
///
/// Top level, like `engineBase` in API.swift: it is one constant that several
/// surfaces need and nobody should be spelling out a second time.
let msDownloadURL = URL(string: "https://meetingscribe.shariquekhatri.com/MeetingScribe.dmg")!

/// Where the answer comes from. The repository is public, so this needs no
/// token and must never be given one.
private let releaseURL = URL(
    string: "https://api.github.com/repos/sharique2004/MeetingScribe/releases/latest")!

// File scope rather than members of UpdateChecker, matching Notifications.swift:
// these are storage keys and a logger, not state anyone should reach through
// the singleton.
private let etagKey = "ms.update.etag.v1"
private let cachedTagKey = "ms.update.tag.v1"
private let cachedNotesKey = "ms.update.notes.v1"
private let cachedPublishedKey = "ms.update.published.v1"
/// The version whose banner has already been posted. Written once per version
/// and never cleared, which is what makes the automatic path a single
/// interruption rather than a daily one.
private let lastNotifiedKey = "ms.update.lastNotified.v1"
private let log = Logger(subsystem: "com.meetingscribe.app", category: "updates")

/// Long enough that launch is over. The engine spawn, the model probe, the
/// first library fetch and the notification permission prompt all happen in
/// the first couple of seconds, and none of them may queue behind a request
/// to a server on the other side of the world.
private let launchDelay: Duration = .seconds(6)
private let checkInterval: Duration = .seconds(24 * 60 * 60)

// MARK: - Version numbers, compared the way this project actually numbers things

/// The bundle calls itself "3.1" (CFBundleShortVersionString) and the release
/// is tagged "v3.1.0". Those are the same version, and a string comparison
/// says otherwise — which on its own would have every copy of 3.1 announcing
/// an update to itself, forever, from the day this shipped.
///
/// So: drop a leading "v", split on ".", and compare component by component
/// as NUMBERS with a missing component reading as zero. "3.10" is newer than
/// "3.9" (which is the other trap, and the one a lexical compare gets exactly
/// backwards); "3.1.1" is newer than "3.1"; "3.1" and "3.1.0" are equal.
///
/// Its own enum rather than methods on UpdateChecker: this is arithmetic on
/// two strings with no main actor, no network and no state in it, so it can be
/// read, reasoned about and exercised entirely on its own.
enum MSVersion {
    /// "v3.1.0" → [3, 1, 0]. A component that is not purely numeric
    /// contributes its leading digits and nothing else, so a "v3.2.0-beta1"
    /// tag reads as 3.2.0 rather than as garbage — pre-releases are not
    /// something this project publishes, and if one ever appears it should be
    /// compared as the version it is a pre-release OF rather than ignored.
    static func components(_ raw: String) -> [Int] {
        number(raw).split(separator: ".", omittingEmptySubsequences: false).map {
            Int($0.prefix { $0.isNumber }) ?? 0
        }
    }

    /// The version without its tag decoration: "v3.1.0" → "3.1.0".
    static func number(_ raw: String) -> String {
        var s = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        if s.first == "v" || s.first == "V" { s.removeFirst() }
        return s
    }

    static func compare(_ a: String, _ b: String) -> ComparisonResult {
        let left = components(a), right = components(b)
        for i in 0..<max(left.count, right.count) {
            // The missing-component rule, and the whole reason 3.1 == 3.1.0.
            let l = i < left.count ? left[i] : 0
            let r = i < right.count ? right[i] : 0
            if l != r { return l < r ? .orderedAscending : .orderedDescending }
        }
        return .orderedSame
    }

    static func isNewer(_ candidate: String, than current: String) -> Bool {
        compare(candidate, current) == .orderedDescending
    }

    /// The number a human should be shown.
    ///
    /// Releases are tagged "v3.2.0" and this app calls itself "3.1", so a
    /// sentence carrying both spellings ("MeetingScribe 3.1 — update to
    /// 3.2.0") makes two versions look like three different numbering schemes.
    /// Trailing zero components come off, down to a floor of two, so the tag
    /// speaks the bundle's dialect. It changes nothing about what is COMPARED:
    /// `compare` above is the only thing that decides anything.
    static func display(_ raw: String) -> String {
        var parts = components(raw)
        while parts.count > 2, parts.last == 0 { parts.removeLast() }
        return parts.map(String.init).joined(separator: ".")
    }
}

// MARK: - The checker

@MainActor
final class UpdateChecker: ObservableObject {
    static let shared = UpdateChecker()

    enum State: Equatable {
        /// No settled answer: the state between launch and the first reply,
        /// and the state during every check after it. Every surface reads it
        /// as "nothing to say yet", which is the truth — the footer shows only
        /// the current version, and no banner is posted.
        case checking
        /// The newest release is this copy's own version, or older than it.
        /// Carries the current version because that is the number the
        /// reassuring sentence has to quote.
        case upToDate(String)
        case available(version: String, notes: String, publishedAt: Date?)
        /// Something between here and GitHub did not work. Never surfaced by
        /// the automatic path — a check nobody asked for that fails is not
        /// news — and always surfaced by the manual one, which was pressed by
        /// somebody waiting for an answer.
        case failed(String)
    }

    @Published private(set) var state: State = .checking

    private var autoTask: Task<Void, Never>?
    /// A request already on the wire. A manual check that lands on top of the
    /// daily one joins it rather than firing a second request at an API that
    /// rate-limits by address.
    private var inFlight: Task<State, Never>?

    private init() {}

    /// What this copy calls itself.
    ///
    /// MS_FAKE_VERSION IS THE VERIFICATION HOOK. Nothing about an update
    /// checker can be tried honestly without being older than the release it
    /// checks against, and the alternatives are waiting for a release or
    /// editing Info.plist inside a signed bundle. Launch with it set to a
    /// version below the published tag and every surface — the banner, the
    /// menu alert, the Settings footer — behaves exactly as it will for a real
    /// out-of-date copy:
    ///
    ///     MS_FAKE_VERSION=3.0 /Applications/MeetingScribe.app/Contents/MacOS/MeetingScribe
    ///
    /// It only ever changes what the app claims to BE. It changes nothing
    /// about what is fetched or what is downloaded, which is always the
    /// canonical DMG above.
    ///
    /// The "0" fallback is for a bare swiftc binary, which has no Info.plist
    /// at all (MSNotifications.usable is the other half of that story): such a
    /// build reads as older than everything, which is the same harmless
    /// pretence MS_FAKE_VERSION exists to make, and it cannot post a
    /// notification because it has no bundle identifier either.
    static var currentVersion: String {
        let fake = ProcessInfo.processInfo.environment["MS_FAKE_VERSION"]?
            .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        if !fake.isEmpty { return fake }
        let short = Bundle.main
            .object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String
        let trimmed = short?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        return trimmed.isEmpty ? "0" : trimmed
    }

    // MARK: - The automatic path

    /// One check a few seconds after launch, then one a day for as long as the
    /// app is up. Called from applicationDidFinishLaunching, alongside
    /// everything else that has to outlive the main window.
    func start() {
        guard autoTask == nil else { return }
        autoTask = Task { [weak self] in
            // Cancellation is checked after every sleep rather than trusted to
            // the `try?`, which swallows it: the same shape EngineManager.watch
            // uses, and for the same reason — a cancelled task whose error is
            // discarded otherwise spins the loop instead of leaving it.
            try? await Task.sleep(for: launchDelay)
            while !Task.isCancelled {
                guard let self else { return }
                await self.check(notifying: true)
                try? await Task.sleep(for: checkInterval)
            }
        }
    }

    // MARK: - The manual path

    /// The menu asked. Same request, and deliberately no notification: the
    /// user is looking straight at the answer, and a banner about a thing they
    /// just pressed a menu item for is the app telling them what they are
    /// looking at — the rule Notifications.swift opens with.
    @discardableResult
    func checkNow() async -> State { await check(notifying: false) }

    /// Hand the download to the browser. Every surface that offers an update
    /// goes through here rather than building a URL of its own: one link, one
    /// place to change it when the hosting moves.
    static func openDownload() {
        NSWorkspace.shared.open(msDownloadURL)
    }

    /// The release notes, cut down to something an alert can hold.
    ///
    /// GitHub bodies are markdown of arbitrary length (the 3.1 one is two
    /// thousand characters). An alert is not a release page and must not try
    /// to be one: take the opening paragraphs, turn the handful of markdown
    /// marks that read as punctuation without a renderer into nothing, and
    /// stop at a word boundary.
    static func notesExcerpt(_ notes: String, limit: Int = 420) -> String {
        let lines = notes
            .replacingOccurrences(of: "\r\n", with: "\n")
            .split(separator: "\n", omittingEmptySubsequences: false)
            .map { line -> String in
                var s = line.trimmingCharacters(in: .whitespaces)
                // A heading is a heading in a renderer and a row of hashes in
                // an alert.
                while s.first == "#" { s.removeFirst() }
                s = s.trimmingCharacters(in: .whitespaces)
                // A list marker is a hyphen or a star FOLLOWED BY A SPACE.
                // Without that test "**Signed and notarized by Apple.**" —
                // which is bold, not a list — loses one star and gains a
                // bullet, which is exactly what the first draft of this did to
                // the 3.1 notes.
                if s.hasPrefix("- ") || s.hasPrefix("* ") { s = "• " + s.dropFirst(2) }
                return s.replacingOccurrences(of: "**", with: "")
                        .replacingOccurrences(of: "`", with: "")
            }
        var text = lines.joined(separator: "\n")
            .replacingOccurrences(of: "\n\n\n", with: "\n\n")
            .trimmingCharacters(in: .whitespacesAndNewlines)
        guard text.count > limit else { return text }
        // Cut at a word, not mid-syllable, and say that there is more.
        let cut = text.index(text.startIndex, offsetBy: limit)
        if let space = text[..<cut].lastIndex(of: " ") { text = String(text[..<space]) }
        else { text = String(text[..<cut]) }
        return text.trimmingCharacters(in: .whitespacesAndNewlines) + "…"
    }

    // MARK: - Asking

    @discardableResult
    private func check(notifying: Bool) async -> State {
        // Joining rather than starting: whoever is already asking will publish
        // the answer, and this caller only wants to know what it was.
        if let inFlight { return await inFlight.value }
        state = .checking
        // `self` strongly: the task is short, it is owned by a singleton that
        // outlives the app's windows, and the alternative is a `weak self`
        // dance whose failure case would have to invent a State.
        let task = Task { await self.fetch() }
        inFlight = task
        let result = await task.value
        inFlight = nil
        state = result
        if notifying, case .available(let version, _, _) = result {
            notifyOnce(version)
        }
        return result
    }

    private func fetch() async -> State {
        let current = Self.currentVersion
        var request = URLRequest(url: releaseURL)
        request.setValue("application/vnd.github+json", forHTTPHeaderField: "Accept")
        // The version GitHub asks unauthenticated callers to pin, so a future
        // change to their default shape cannot break this parse.
        request.setValue("2022-11-28", forHTTPHeaderField: "X-GitHub-Api-Version")
        request.timeoutInterval = 15
        // URLSession's own store would answer a repeat request out of cache
        // and hand us a 200 we cannot tell from a fresh one. Do the
        // conditional request ourselves: the cached answer is then OURS, on
        // disk, and survives the relaunch that empties the URL cache.
        request.cachePolicy = .reloadIgnoringLocalCacheData
        let defaults = UserDefaults.standard
        if let etag = defaults.string(forKey: etagKey), cachedRelease() != nil {
            request.setValue(etag, forHTTPHeaderField: "If-None-Match")
        }

        let data: Data
        let http: HTTPURLResponse
        do {
            let (body, response) = try await URLSession.shared.data(for: request)
            guard let asHTTP = response as? HTTPURLResponse else {
                return .failed("GitHub's answer wasn't an HTTP response.")
            }
            data = body
            http = asHTTP
        } catch {
            // Offline is the ordinary case here, not an error worth a red
            // banner: a laptop in a tunnel is not a broken app.
            log.notice("update check could not reach GitHub: \(error.localizedDescription, privacy: .public)")
            return .failed("Couldn't reach GitHub to check for updates. "
                + "Check your connection and try again.")
        }

        switch http.statusCode {
        case 200:
            guard let release = try? JSONDecoder().decode(Release.self, from: data),
                  !MSVersion.number(release.tag_name).isEmpty else {
                log.error("update check could not read GitHub's release JSON")
                return .failed("GitHub's answer wasn't something this app could read.")
            }
            remember(release, etag: http.value(forHTTPHeaderField: "ETag"))
            return verdict(release.cached, current: current)
        case 304:
            // Nothing has changed since we last asked, which is the answer on
            // all but the handful of days a release happens. The cached copy
            // is the reply.
            guard let cached = cachedRelease() else {
                // Cannot happen — the header is only sent when a cache exists
                // — but a 304 with nothing to show is worse than asking again,
                // so drop the tag and let tomorrow's check be unconditional.
                defaults.removeObject(forKey: etagKey)
                return .failed("Couldn't check for updates. Try again.")
            }
            return verdict(cached, current: current)
        case 403, 429:
            // Unauthenticated GitHub allows 60 requests an hour per address,
            // which one check a day cannot exhaust on its own — but a shared
            // office address can, and the honest answer is to say so rather
            // than to claim the app is up to date.
            log.notice("update check was rate-limited by GitHub")
            return .failed("GitHub is rate-limiting update checks from this network. "
                + "Try again later.")
        default:
            log.notice("update check got HTTP \(http.statusCode, privacy: .public)")
            return .failed("GitHub answered \(http.statusCode).")
        }
    }

    private func verdict(_ release: CachedRelease, current: String) -> State {
        guard MSVersion.isNewer(release.tag, than: current) else {
            return .upToDate(MSVersion.display(current))
        }
        return .available(version: MSVersion.display(release.tag),
                          notes: release.notes,
                          publishedAt: release.published.flatMap(Self.date))
    }

    /// Post the banner, at most once for any given version, ever.
    ///
    /// Across launches, across days, and across a 24-hour loop that will find
    /// the same release forty times before the next one ships. A notification
    /// the user has already read and dismissed is not made more useful by
    /// arriving again tomorrow — and this app posts two other kinds of banner
    /// that genuinely matter, which a nagging third would devalue. Once the
    /// banner has been spent, the Settings footer and the menu are where the
    /// answer stays available for as long as it is true.
    private func notifyOnce(_ version: String) {
        let defaults = UserDefaults.standard
        guard defaults.string(forKey: lastNotifiedKey) != version else { return }
        defaults.set(version, forKey: lastNotifiedKey)
        MSNotifications.postUpdateAvailable(version: version)
    }

    // MARK: - What GitHub said, and what we kept of it

    /// Exactly the three fields this app reads, spelled the way GitHub spells
    /// them — API.swift's house style for a wire shape, and the spelling is
    /// the contract. Everything but the tag is optional: a release with no
    /// notes and no date is still a release.
    private struct Release: Decodable {
        let tag_name: String
        let body: String?
        let published_at: String?

        var cached: CachedRelease {
            CachedRelease(tag: tag_name,
                          notes: (body ?? "").trimmingCharacters(in: .whitespacesAndNewlines),
                          published: published_at)
        }
    }

    /// The last answer, as three plain strings in UserDefaults. Plain strings
    /// rather than an archived object so a future field can be added without a
    /// migration, and so a corrupted value is a missing value rather than a
    /// crash on launch.
    private struct CachedRelease {
        let tag: String
        let notes: String
        let published: String?
    }

    private func cachedRelease() -> CachedRelease? {
        let defaults = UserDefaults.standard
        guard let tag = defaults.string(forKey: cachedTagKey),
              !MSVersion.number(tag).isEmpty else { return nil }
        return CachedRelease(tag: tag,
                             notes: defaults.string(forKey: cachedNotesKey) ?? "",
                             published: defaults.string(forKey: cachedPublishedKey))
    }

    private func remember(_ release: Release, etag: String?) {
        let defaults = UserDefaults.standard
        let cached = release.cached
        defaults.set(cached.tag, forKey: cachedTagKey)
        defaults.set(cached.notes, forKey: cachedNotesKey)
        if let published = cached.published {
            defaults.set(published, forKey: cachedPublishedKey)
        } else {
            defaults.removeObject(forKey: cachedPublishedKey)
        }
        // Only alongside the body it describes. An ETag kept without its
        // release would earn a 304 with nothing to answer it.
        if let etag, !etag.isEmpty {
            defaults.set(etag, forKey: etagKey)
        } else {
            defaults.removeObject(forKey: etagKey)
        }
    }

    /// "2026-08-04T05:58:52Z" — GitHub's dates are internet date-time, always.
    private static func date(_ raw: String) -> Date? {
        let parser = ISO8601DateFormatter()
        parser.formatOptions = [.withInternetDateTime]
        return parser.date(from: raw)
    }
}
