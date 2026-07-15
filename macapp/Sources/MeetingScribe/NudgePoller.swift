// Polls the backend for meeting nudges ("your 10:00 is starting", "you look
// like you're in a call") and turns them into native notifications. The
// decision logic lives server-side in nudge.py; this is just the messenger.

import Foundation

final class NudgePoller {
    private let baseURL: URL
    private let notifications: Notifications
    private var announced = Set<String>()

    init(baseURL: URL, notifications: Notifications) {
        self.baseURL = baseURL
        self.notifications = notifications
    }

    func start() {
        Timer.scheduledTimer(withTimeInterval: 20, repeats: true) { [weak self] _ in
            self?.poll()
        }
    }

    private func poll() {
        var request = URLRequest(url: baseURL.appendingPathComponent("api/nudges"))
        request.timeoutInterval = 5
        URLSession.shared.dataTask(with: request) { [weak self] data, response, _ in
            guard let self, let data,
                  (response as? HTTPURLResponse)?.statusCode == 200,
                  let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
                  let nudge = obj["nudge"] as? [String: Any],
                  let id = nudge["id"] as? String else { return }
            DispatchQueue.main.async {
                guard !self.announced.contains(id) else { return }
                self.announced.insert(id)
                self.notifications.postNudge(
                    id: id,
                    title: nudge["title"] as? String ?? "Meeting happening?",
                    body: nudge["body"] as? String ?? "Start recording?")
            }
        }.resume()
    }
}
