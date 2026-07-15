// Polls the backend and publishes recording state + finished-job events.
//
// One poller drives everything native: the menu-bar icon, the floating
// recording panel, and "transcript/summary ready" notifications.

import Foundation

struct RecorderState: Equatable {
    var recording = false
    var meetingID: String?
    var elapsed: TimeInterval = 0
}

final class StatusPoller {
    private let baseURL: URL
    private var lastJobs: [String: String] = [:]         // id -> state
    private var lastSummaryJobs: [String: String] = [:]
    private var primedJobs = false
    private var titleCache: [String: String] = [:]

    var onRecorderChange: ((RecorderState) -> Void)?
    var onRecorderTick: ((RecorderState, String?) -> Void)?  // state, meeting title
    var onJobFinished: ((_ kind: String, _ meetingID: String, _ ok: Bool, _ message: String) -> Void)?

    private var state = RecorderState()

    init(baseURL: URL) {
        self.baseURL = baseURL
    }

    func start() {
        poll()
        Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { [weak self] _ in
            self?.poll()
        }
    }

    private func poll() {
        var request = URLRequest(url: baseURL.appendingPathComponent("api/status"))
        request.timeoutInterval = 1.8
        URLSession.shared.dataTask(with: request) { [weak self] data, response, _ in
            guard let self, let data,
                  (response as? HTTPURLResponse)?.statusCode == 200,
                  let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else { return }
            DispatchQueue.main.async { self.apply(obj) }
        }.resume()
    }

    private func apply(_ obj: [String: Any]) {
        let rec = obj["recorder"] as? [String: Any] ?? [:]
        var next = RecorderState()
        next.recording = rec["recording"] as? Bool ?? false
        next.meetingID = rec["meeting_id"] as? String
        next.elapsed = rec["elapsed"] as? TimeInterval ?? 0

        if next.recording != state.recording || next.meetingID != state.meetingID {
            state = next
            onRecorderChange?(next)
        } else {
            state = next
        }
        if next.recording {
            tickWithTitle(next)
        }

        // Job transitions -> notifications. The first poll only primes the
        // baseline so a relaunch doesn't re-announce old finished jobs.
        let jobs = states(of: obj["jobs"])
        let summaryJobs = states(of: obj["summary_jobs"])
        if primedJobs {
            announceTransitions(from: lastJobs, to: jobs, kind: "transcript")
            announceTransitions(from: lastSummaryJobs, to: summaryJobs, kind: "summary")
        }
        lastJobs = jobs
        lastSummaryJobs = summaryJobs
        primedJobs = true
    }

    private func states(of value: Any?) -> [String: String] {
        var out: [String: String] = [:]
        for (id, job) in (value as? [String: [String: Any]]) ?? [:] {
            out[id] = job["state"] as? String ?? ""
        }
        return out
    }

    private func announceTransitions(from old: [String: String], to new: [String: String], kind: String) {
        for (id, s) in new where s != "processing" {
            if old[id] == "processing" {
                onJobFinished?(kind, id, s == "done", s)
            }
        }
    }

    private func tickWithTitle(_ current: RecorderState) {
        guard let id = current.meetingID else {
            onRecorderTick?(current, nil)
            return
        }
        if let title = titleCache[id] {
            onRecorderTick?(current, title)
            return
        }
        onRecorderTick?(current, nil)
        var request = URLRequest(url: baseURL.appendingPathComponent("api/meetings/\(id)"))
        request.timeoutInterval = 1.8
        URLSession.shared.dataTask(with: request) { [weak self] data, _, _ in
            guard let self, let data,
                  let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
                  let title = obj["title"] as? String else { return }
            DispatchQueue.main.async {
                self.titleCache[id] = title
                if self.state.meetingID == id {
                    self.onRecorderTick?(self.state, title)
                }
            }
        }.resume()
    }
}
