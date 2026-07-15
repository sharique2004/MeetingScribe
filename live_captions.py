"""Live captions while recording — streaming SpeechAnalyzer (macOS 26+).

One helper process (tools/apple_live.swift) per recorded track receives the
recorder's PCM chunks over stdin and emits caption events as NDJSON. The
recording callback must never block, so chunks go through a bounded queue
that drops the oldest audio under backpressure (captions may skip; the WAV
on disk — written by the recorder itself — is always complete, and the
batch pass after Stop produces the canonical transcript).

Everything runs on-device; nothing leaves the Mac.
"""

import json
import logging
import queue
import subprocess
import tempfile
import threading
from pathlib import Path

import swift_helpers

log = logging.getLogger("meetingscribe.live")

_SRC = Path(__file__).resolve().parent / "tools" / "apple_live.swift"

QUEUE_CHUNKS = 100     # ~2.3 s of audio at 1024-frame/44.1kHz chunks
MAX_TURNS = 400        # ring buffer of final caption lines

TRACK_LABELS = {"mic": "You", "system": "Them"}


def available():
    return swift_helpers.ensure_binary(_SRC, "apple_live", min_macos=(26, 0)) is not None


class _TrackFeed:
    """One helper process + feeder/reader threads for one audio track."""

    def __init__(self, session, key, channels, rate):
        self.session = session
        self.key = key
        self.queue = queue.Queue(maxsize=QUEUE_CHUNKS)
        self.dropped = 0
        cmd = [session.binary, session.locale, str(rate), str(channels)]
        if session.context_path:
            cmd += ["--context", session.context_path]
        self.proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, bufsize=0,
        )
        threading.Thread(target=self._feeder, daemon=True,
                         name=f"live-feed-{key}").start()
        threading.Thread(target=self._reader, daemon=True,
                         name=f"live-read-{key}").start()

    def push(self, pcm_bytes):
        """Called from the recording thread — never blocks."""
        try:
            self.queue.put_nowait(pcm_bytes)
        except queue.Full:
            try:  # drop the oldest chunk to keep captions near-real-time
                self.queue.get_nowait()
                self.dropped += 1
                self.queue.put_nowait(pcm_bytes)
            except (queue.Empty, queue.Full):
                pass

    def close(self):
        self.queue.put(None)  # feeder sentinel -> stdin EOF -> finalize

    def _feeder(self):
        try:
            while True:
                chunk = self.queue.get()
                if chunk is None:
                    break
                self.proc.stdin.write(chunk)
        except (BrokenPipeError, OSError):
            pass
        finally:
            try:
                self.proc.stdin.close()
            except OSError:
                pass

    def _reader(self):
        for raw in self.proc.stdout:
            try:
                event = json.loads(raw)
            except ValueError:
                continue
            self.session._on_event(self.key, event)
        self.session._on_event(self.key, {"t": "done"})


class LiveSession:
    """Caption state for one recording. Thread-safe snapshots for the UI."""

    def __init__(self, locale="en-US", context_strings=None):
        self.binary = swift_helpers.ensure_binary(_SRC, "apple_live", min_macos=(26, 0))
        self.enabled = self.binary is not None
        self.locale = locale
        self.context_path = None
        self._lock = threading.Lock()
        self._feeds = {}
        self._turns = []          # [{seq, track, who, start, end, text}]
        self._partials = {}       # track -> {start, end, text}
        self._seq = 0
        self._done = set()
        if self.enabled and context_strings:
            try:
                f = tempfile.NamedTemporaryFile(
                    "w", suffix=".json", prefix="ms-live-ctx-", delete=False)
                json.dump({"strings": list(context_strings)[:100]}, f)
                f.close()
                self.context_path = f.name
            except OSError:
                self.context_path = None

    def tap(self, key):
        """A per-track callable for the recorder, or None when disabled."""
        if not self.enabled:
            return None

        def _tap(pcm_bytes, channels, rate):
            feed = self._feeds.get(key)
            if feed is None:
                with self._lock:
                    feed = self._feeds.get(key)
                    if feed is None:
                        try:
                            feed = _TrackFeed(self, key, channels, rate)
                        except OSError as exc:
                            log.warning("live captions for %s failed: %s", key, exc)
                            self.enabled = False
                            return
                        self._feeds[key] = feed
            feed.push(pcm_bytes)

        return _tap

    def _on_event(self, key, event):
        kind = event.get("t")
        with self._lock:
            if kind == "final":
                self._partials.pop(key, None)
                self._seq += 1
                self._turns.append({
                    "seq": self._seq,
                    "track": key,
                    "who": TRACK_LABELS.get(key, key),
                    "start": event.get("start"),
                    "end": event.get("end"),
                    "text": event.get("text", ""),
                })
                if len(self._turns) > MAX_TURNS:
                    del self._turns[: len(self._turns) - MAX_TURNS]
            elif kind == "partial":
                self._partials[key] = {
                    "start": event.get("start"),
                    "end": event.get("end"),
                    "text": event.get("text", ""),
                }
            elif kind == "done":
                self._partials.pop(key, None)
                self._done.add(key)

    def snapshot(self, since=0):
        with self._lock:
            turns = [t for t in self._turns if t["seq"] > since]
            return {
                "enabled": self.enabled,
                "turns": turns,
                "partials": {
                    k: dict(v, who=TRACK_LABELS.get(k, k))
                    for k, v in self._partials.items()
                },
                "seq": self._seq,
                "dropped": sum(f.dropped for f in self._feeds.values()),
            }

    def stop(self):
        """Recorder stopped: close stdins so helpers finalize and exit."""
        with self._lock:
            feeds = list(self._feeds.values())
        for feed in feeds:
            feed.close()

    def discard(self):
        """Free helper processes (called when a new recording replaces us)."""
        self.stop()
        with self._lock:
            feeds = list(self._feeds.values())
            self._feeds.clear()
        for feed in feeds:
            try:
                feed.proc.kill()
            except OSError:
                pass
        if self.context_path:
            Path(self.context_path).unlink(missing_ok=True)
            self.context_path = None
