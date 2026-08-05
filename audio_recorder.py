"""Dual-track meeting audio recorder (Windows + macOS).

Captures the default microphone and the system audio (everything you hear
through your speakers/headphones) as two separate WAV files. Keeping the
tracks separate is what lets the pipeline tell "you" apart from the other
meeting participants with certainty.

Windows: system audio comes from WASAPI loopback (PyAudioWPatch); a
silence-keeper output stream keeps the loopback flowing during quiet moments.

macOS: the system track comes from a Core Audio process tap via the
tools/apple_syscap.swift helper — driverless, no output rerouting, capture
survives headphones/volume/mute (macOS asks once for "System Audio
Recording Only"). When the helper is unavailable, the recorder falls back
to the legacy path: a virtual loopback input device such as BlackHole
(https://existential.audio/blackhole/), automatically routed via
macos_audio.ensure_routing()'s "MeetingScribe Output" Multi-Output Device
for the duration of the recording. The mic is recorded normally via
sounddevice either way. Override the source with
MEETINGSCRIBE_SYSTEM_SOURCE=tap|blackhole|none (default: auto).
"""

import json
import logging
import os
import subprocess
import sys
import threading
import time
import wave
from pathlib import Path

import numpy as np
from config import DATA_DIR

CHUNK_FRAMES = 1024

log = logging.getLogger("meetingscribe.recorder")

# "wasapi" on Windows, "sounddevice" elsewhere. Override with the
# MEETINGSCRIBE_BACKEND env var (useful for testing the portable backend).
BACKEND = os.environ.get("MEETINGSCRIBE_BACKEND") or (
    "wasapi" if sys.platform == "win32" else "sounddevice"
)

if BACKEND == "wasapi":
    import pyaudiowpatch as pyaudio

    sd = None
else:
    import sounddevice as sd

    pyaudio = None

try:  # macOS only - raises ImportError elsewhere
    import macos_audio
except Exception:
    macos_audio = None

try:  # Swift helper plumbing (macOS); harmless to miss elsewhere
    import swift_helpers
except Exception:
    swift_helpers = None

# --------------------------------------------------- system-audio source --

_SYSCAP_SRC = Path(__file__).resolve().parent / "tools" / "apple_syscap.swift"

# Written after each tap recording so preflight can report the permission
# state ("System Audio Recording Only" has no public query API — the last
# recording's outcome is the best signal available).
TAP_STATE_PATH = DATA_DIR / "tap_state.json"


def _tap_binary(build=True):
    """Path to the apple_syscap helper, or None where unsupported.

    build=False only probes for an existing binary (bundle or a previous
    compile) and never invokes swiftc — for startup and UI-poll paths that
    must not block. Recording start uses build=True.
    """
    if sys.platform != "darwin" or BACKEND == "wasapi" or swift_helpers is None:
        return None
    if not build:
        return swift_helpers.find_binary("apple_syscap")
    return swift_helpers.ensure_binary(
        _SYSCAP_SRC, "apple_syscap", min_macos=(26, 0)
    )


def system_source(build=True):
    """Resolved system-audio source for this run: 'tap', 'blackhole' or 'none'.

    MEETINGSCRIBE_SYSTEM_SOURCE=tap|blackhole|none forces it (testing and
    rollback); 'auto' (default) prefers the driverless tap, then a loopback
    device, then mic-only.
    """
    forced = (os.environ.get("MEETINGSCRIBE_SYSTEM_SOURCE") or "auto").strip().lower()
    if forced == "none":
        return "none"
    if forced == "blackhole":
        return "blackhole"
    if forced == "tap":
        return "tap" if _tap_binary(build) else "none"
    return "tap" if _tap_binary(build) else "blackhole"


def _tap_permission_state():
    """'tap_ready' | 'tap_denied' | 'tap_undetermined' from the last recording."""
    try:
        state = json.loads(TAP_STATE_PATH.read_text(encoding="utf-8")).get("state")
    except (OSError, ValueError):
        return "tap_undetermined"
    if state == "ready":
        return "tap_ready"
    if state == "denied":
        return "tap_denied"
    return "tap_undetermined"


def _write_tap_state(state):
    try:
        TAP_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        TAP_STATE_PATH.write_text(json.dumps({"state": state, "ts": time.time()}),
                                  encoding="utf-8")
    except OSError as exc:
        log.debug("could not persist tap state: %s", exc)


# ------------------------------------------------------------ device lookup --

def _find_default_mic(pa):
    try:
        info = pa.get_default_input_device_info()
    except (OSError, IOError):
        return None
    if int(info.get("maxInputChannels", 0)) < 1 or info.get("isLoopbackDevice"):
        return None
    return info


def _find_default_loopback(pa):
    try:
        return pa.get_default_wasapi_loopback()
    except Exception:
        pass
    try:
        wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        speakers = pa.get_device_info_by_index(wasapi["defaultOutputDevice"])
        if speakers.get("isLoopbackDevice"):
            return speakers
        for dev in pa.get_loopback_device_info_generator():
            if speakers["name"] in dev["name"]:
                return dev
    except Exception:
        pass
    return None


# Names that identify virtual loopback drivers on macOS (and other systems).
VIRTUAL_LOOPBACK_HINTS = ("blackhole", "soundflower", "loopback", "ishowu", "vb-audio", "vb-cable")


def _sd_is_virtual(name):
    lowered = name.lower()
    return any(hint in lowered for hint in VIRTUAL_LOOPBACK_HINTS)


def _sd_refresh_devices():
    """Re-scan the device list (PortAudio caches it at first query), so
    devices plugged in or installed while the app runs are picked up.
    Only safe while no stream is open."""
    try:
        sd._terminate()
        sd._initialize()
    except Exception as exc:
        log.debug("device rescan failed: %s", exc)


def _sd_find_devices():
    """Return (mic_device, system_device) dicts for the sounddevice backend.

    Device dicts from sd.query_devices() carry their authoritative PortAudio
    index in dev["index"] — never derive it from enumeration order.
    """
    devices = sd.query_devices()
    try:
        default_in = sd.default.device[0]
    except Exception:
        default_in = -1

    mic = None
    system = None
    for dev in devices:
        if int(dev.get("max_input_channels", 0)) < 1:
            continue
        if _sd_is_virtual(dev["name"]):
            if system is None:
                system = dict(dev)
        elif dev["index"] == default_in:
            mic = dict(dev)
    if mic is None:  # no usable default input — take the first real microphone
        for dev in devices:
            if int(dev.get("max_input_channels", 0)) >= 1 and not _sd_is_virtual(dev["name"]):
                mic = dict(dev)
                break
    return mic, system


# ----------------------------------------------------------- track recorders --

class _BaseTrackRecorder(threading.Thread):
    """Records one device to one WAV file until the stop event fires.

    Subclasses provide _open_stream / _read_chunk / _close_stream; everything
    else (WAV writing, levels, timing, error capture) is shared.
    """

    def __init__(self, device, path, stop_event, levels, key):
        super().__init__(daemon=True, name=f"record-{key}")
        self.device = device
        self.path = path
        self.stop_event = stop_event
        self.levels = levels
        self.key = key
        self.rate = 0
        self.channels = 1
        self.frames_written = 0
        self.started_at = None  # perf_counter at stream start
        self.error = None
        self.tap = None  # optional fn(pcm_bytes, channels, rate) — must not block
        self.extra_warnings = []  # surfaced verbatim by MeetingRecorder.stop()

    def force_stop(self):
        """Break a blocking read that outlived the stop event (subclass hook)."""

    def _open_stream(self):
        raise NotImplementedError

    def _read_chunk(self, stream):
        """Return one chunk of interleaved int16 PCM bytes."""
        raise NotImplementedError

    def _close_stream(self, stream):
        raise NotImplementedError

    def run(self):
        stream = None
        wf = None
        try:
            stream = self._open_stream()
            wf = wave.open(str(self.path), "wb")
            wf.setnchannels(self.channels)
            wf.setsampwidth(2)
            wf.setframerate(self.rate)
            self.started_at = time.perf_counter()
            while not self.stop_event.is_set():
                data = self._read_chunk(stream)
                wf.writeframes(data)
                self.frames_written += len(data) // (2 * self.channels)
                samples = np.frombuffer(data, dtype=np.int16)
                if samples.size:
                    rms = float(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))
                    self.levels[self.key] = min(1.0, rms / 6000.0)
                if self.tap is not None:
                    try:  # live captions — never allowed to break recording
                        self.tap(data, self.channels, self.rate)
                    except Exception:
                        self.tap = None
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            log.warning("%s track failed: %s", self.key, self.error)
        finally:
            self.levels[self.key] = 0.0
            if stream is not None:
                try:
                    self._close_stream(stream)
                except Exception as exc:
                    log.warning("could not close %s stream cleanly: %s", self.key, exc)
            if wf is not None:
                wf.close()

    @property
    def seconds_recorded(self):
        return self.frames_written / self.rate if self.rate else 0.0


class _WasapiTrackRecorder(_BaseTrackRecorder):
    def __init__(self, pa, device, path, stop_event, levels, key):
        super().__init__(device, path, stop_event, levels, key)
        self.pa = pa
        self.rate = int(device["defaultSampleRate"])

    def _open_stream(self):
        """Prefer the device's native channel count (WASAPI loopback often
        only accepts its mix format), falling back to stereo/mono."""
        native = max(1, min(int(self.device["maxInputChannels"]), 8))
        last_exc = None
        for channels in dict.fromkeys((native, 2, 1)):
            try:
                stream = self.pa.open(
                    format=pyaudio.paInt16,
                    channels=channels,
                    rate=self.rate,
                    input=True,
                    input_device_index=int(self.device["index"]),
                    frames_per_buffer=CHUNK_FRAMES,
                )
                if channels != native:
                    log.warning(
                        "%s: opened with %d channel(s) instead of native %d",
                        self.key, channels, native,
                    )
                self.channels = channels
                return stream
            except Exception as exc:
                last_exc = exc
        raise last_exc

    def _read_chunk(self, stream):
        return stream.read(CHUNK_FRAMES, exception_on_overflow=False)

    def _close_stream(self, stream):
        stream.stop_stream()
        stream.close()


class _SounddeviceTrackRecorder(_BaseTrackRecorder):
    def __init__(self, device, path, stop_event, levels, key):
        super().__init__(device, path, stop_event, levels, key)
        self.rate = int(device["default_samplerate"])
        self.channels = max(1, min(int(device["max_input_channels"]), 2))

    def _open_stream(self):
        stream = sd.InputStream(
            device=int(self.device["index"]),
            channels=self.channels,
            samplerate=self.rate,
            dtype="int16",
            blocksize=CHUNK_FRAMES,
        )
        stream.start()
        return stream

    def _read_chunk(self, stream):
        data, _overflowed = stream.read(CHUNK_FRAMES)
        return bytes(data.tobytes())

    def _close_stream(self, stream):
        stream.stop()
        stream.close()


class _TapTrackRecorder(_BaseTrackRecorder):
    """System track via the apple_syscap Core Audio process-tap helper.

    The helper emits interleaved int16 PCM on stdout, locked at 48 kHz stereo
    for its whole life (it resamples internally across output-device changes),
    and NDJSON events on stderr. No driver, no output rerouting. If the helper
    dies mid-recording it is restarted once, with the gap padded as silence so
    the track keeps tracking wall-clock time.
    """

    CHUNK_MS = 20
    READY_TIMEOUT = 15.0
    # Extra patience on the first-ever run: tap creation MAY sit behind the
    # one-time TCC prompt, and killing the helper mid-prompt would lose the
    # far side of the very first meeting even after the user clicks Allow.
    FIRST_RUN_TIMEOUT = 120.0
    DENIED_WARNING = (
        "macOS blocked system-audio recording, so only your mic was "
        "captured. Enable MeetingScribe under System Settings → Privacy & "
        "Security → Screen & System Audio Recording, then record again."
    )

    def __init__(self, device, path, stop_event, levels, key):
        super().__init__(device, path, stop_event, levels, key)
        self.rate = 48000
        self.channels = 2
        self._binary = device["tap_binary"]
        self._proc = None
        self._stderr_thread = None
        self._ready = threading.Event()
        self._last_event_error = None
        self._silence_warned = False
        # Sticky for the life of the track: a call that goes quiet for a
        # minute is not the problem this reports, and a warning that
        # blinks off the moment somebody coughs is a warning nobody
        # trusts. Cleared only by a restart of the track.
        self.silent = False
        self._restarted = False
        self._saw_signal = False

    @property
    def _chunk_bytes(self):
        return self.rate * self.channels * 2 * self.CHUNK_MS // 1000

    def _spawn(self):
        proc = subprocess.Popen(
            [self._binary, "--rate", str(self.rate), "--channels", str(self.channels),
             "--chunk-ms", str(self.CHUNK_MS), "--exclude-pid", str(os.getpid())],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self._ready.clear()
        self._stderr_thread = threading.Thread(
            target=self._read_events, args=(proc,), daemon=True,
            name=f"tap-events-{self.key}",
        )
        self._stderr_thread.start()
        # Wait for "ready" while also watching the helper's liveness: a
        # helper that dies at startup (permission denied → exit 3) must fail
        # fast and record the denial — not burn the whole timeout.
        timeout = (self.FIRST_RUN_TIMEOUT if not TAP_STATE_PATH.exists()
                   else self.READY_TIMEOUT)
        deadline = time.monotonic() + timeout
        while not self._ready.wait(0.25):
            if proc.poll() is not None:
                if self._stderr_thread is not None:
                    self._stderr_thread.join(timeout=2)
                if proc.returncode == 3:
                    _write_tap_state("denied")
                    self.extra_warnings.append(self.DENIED_WARNING)
                raise RuntimeError(
                    self._last_event_error
                    or f"system-audio tap helper exited (code {proc.returncode})"
                )
            if time.monotonic() > deadline or self.stop_event.is_set():
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                raise RuntimeError(
                    self._last_event_error
                    or "system-audio tap helper did not become ready"
                )
        return proc

    def _read_events(self, proc):
        for raw in proc.stderr:
            try:
                event = json.loads(raw)
            except ValueError:
                continue
            kind = event.get("t")
            if kind == "ready":
                # Trust the helper's locked format over our defaults.
                self.rate = int(event.get("rate") or self.rate)
                self.channels = int(event.get("channels") or self.channels)
                self._ready.set()
            elif kind == "device_changed":
                log.info("system tap follows new output: %s", event.get("output"))
            elif kind == "silence" and not self._silence_warned:
                self._silence_warned = True
                self.silent = True
                log.warning("system tap reports sustained digital silence")
            elif kind == "error":
                self._last_event_error = (
                    f"tap helper {event.get('stage', '?')}: {event.get('message', '')}"
                )
                log.warning("%s", self._last_event_error)

    def _open_stream(self):
        self._proc = self._spawn()
        return self._proc

    def _read_chunk(self, stream):
        want = self._chunk_bytes
        data = b""
        while len(data) < want:
            piece = self._proc.stdout.read(want - len(data))
            if not piece:  # helper exited
                if self.stop_event.is_set():
                    break
                if not self._restarted:
                    self._restarted = True
                    log.warning("system tap helper died — restarting once")
                    self._close_helper()
                    self._proc = self._spawn()
                    self.extra_warnings.append(
                        "The system-audio capture helper restarted once "
                        "mid-recording; a short stretch of the meeting track "
                        "was filled with silence."
                    )
                    # The dying helper's tail goes FIRST (it is audio from
                    # before the gap), trimmed to whole frames, then silence
                    # up to wall-clock.
                    bytes_per_frame = self.channels * 2
                    data = data[: len(data) - len(data) % bytes_per_frame]
                    return data + self._gap_silence(len(data) // bytes_per_frame)
                raise RuntimeError(
                    self._last_event_error or "system-audio tap helper exited"
                )
            data += piece
        if not self._saw_signal and any(data):
            self._saw_signal = True
        return data

    def _gap_silence(self, pending_frames=0):
        """Silence covering the frames lost while the helper was down, so the
        track's length keeps tracking wall-clock time (start_offset only
        aligns the beginning). `pending_frames` = frames already read but not
        yet counted in frames_written."""
        if self.started_at is None:
            return b""
        expected = int((time.perf_counter() - self.started_at) * self.rate)
        deficit = expected - self.frames_written - pending_frames
        deficit = max(0, min(deficit, self.rate * 600))
        return b"\x00" * (deficit * self.channels * 2)

    def _close_helper(self):
        proc, self._proc = self._proc, None
        if proc is None:
            return None
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=2)
            self._stderr_thread = None
        try:
            proc.stdout.close()
            proc.stderr.close()
        except OSError:
            pass
        return proc.returncode

    def force_stop(self):
        """Last-resort unblock for stop(): killing the helper closes its
        stdout, so a blocked _read_chunk sees EOF and the thread can exit."""
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass

    def _close_stream(self, stream):
        returncode = self._close_helper()
        prior_ready = _tap_permission_state() == "tap_ready"
        if self._saw_signal:
            _write_tap_state("ready")
        elif returncode == 3:  # tap creation failed — permission is the usual cause
            _write_tap_state("denied")
            self.extra_warnings.append(self.DENIED_WARNING)
        elif self.seconds_recorded > 15 and not prior_ready:
            # All zeros on a machine where the tap has never proven itself:
            # could be a denied permission (which manifests as silence), or
            # just a quiet meeting. Soft note, neutral state — never the loud
            # denied alarm. Once one recording has captured real audio,
            # quiet recordings (in-person mode) stay entirely silent here.
            _write_tap_state("quiet")
            self.extra_warnings.append(
                "The system track recorded only silence. If nothing was "
                "playing, all is well; if macOS asked to record system audio "
                "and was denied, enable MeetingScribe under System Settings → "
                "Privacy & Security → Screen & System Audio Recording."
            )


class _SilenceKeeper(threading.Thread):
    """(Windows) Plays silence so the WASAPI loopback stream keeps flowing.

    Windows only delivers loopback audio while something is rendering to the
    output device; without this, the system track would pause whenever the
    meeting goes quiet and drift out of sync with the mic track. macOS virtual
    loopback devices (BlackHole etc.) deliver a continuous stream on their
    own, so no keeper is needed there.
    """

    def __init__(self, pa, stop_event):
        super().__init__(daemon=True, name="silence-keeper")
        self.pa = pa
        self.stop_event = stop_event

    def run(self):
        stream = None
        try:
            wasapi = self.pa.get_host_api_info_by_type(pyaudio.paWASAPI)
            dev = self.pa.get_device_info_by_index(wasapi["defaultOutputDevice"])
            rate = int(dev["defaultSampleRate"])
            channels = max(1, min(int(dev.get("maxOutputChannels", 2)), 2))
            stream = self.pa.open(
                format=pyaudio.paInt16,
                channels=channels,
                rate=rate,
                output=True,
                frames_per_buffer=CHUNK_FRAMES,
            )
            silence = b"\x00" * (CHUNK_FRAMES * channels * 2)
            while not self.stop_event.is_set():
                stream.write(silence)
        except Exception as exc:
            # Loopback may then have gaps while nothing plays, but recording works.
            log.warning("silence keeper stopped: %s", exc)
        finally:
            if stream is not None:
                try:
                    stream.stop_stream()
                    stream.close()
                except Exception:
                    pass


# --------------------------------------------------------------- controller --

def _routing_alert(routing, auto_route, loopback_name):
    """Turn a routing_status() value into (alert, human note), or (None, None).

    The alert is only ever raised for a problem the app will NOT fix itself.
    macos_audio.ensure_routing() — called from start() whenever auto_route is
    on — repairs both bad states: it pairs the loopback with a real output in a
    multi-output device and makes that the default. Read it and you will see
    "loopback_only" is handled explicitly (`preferred=None if current is the
    loopback`), which is the whole point: the user does not have to do
    anything. So while auto-routing is on there is nothing to alarm about, and
    a blinking red "you will not hear this meeting" banner would be a false
    alarm telling the user to hand-fix something the app fixes two seconds
    later. What is left is a calm note for the device tooltip.

    Three cases:

    - "loopback_only" + auto-routing ON: the Mac's default output IS the
      loopback right now, so at this instant the user would hear nothing — but
      ensure_routing() repairs exactly this at record time. Informational note,
      NO alert.
    - "loopback_only" + auto-routing OFF: nothing will repair it, so the user
      really will sit through the meeting in silence. Loud alarm.
    - "not_routed": nothing feeds the loopback yet. That is the NORMAL idle
      state when auto-routing is on (routing happens when recording starts),
      so it is only worth flagging when auto-routing is off.

    Tap states (driverless capture — nothing is ever routed):

    - "tap_ready": last recording captured real system audio. Nothing to say.
    - "tap_undetermined": no successful capture yet; macOS will show its
      one-time permission prompt at the first recording. Calm tooltip note.
    - "tap_denied": the permission looks denied — the far side will be lost
      until the user flips it in System Settings. Loud alarm with directions.
    """
    if routing == "tap_ready":
        return None, None
    if routing == "tap_undetermined":
        return None, (
            "System audio is captured directly by macOS — no driver, and your "
            "sound output is never changed. macOS will ask once to allow "
            "system-audio recording when you start recording."
        )
    if routing == "tap_denied":
        return "silent", (
            "macOS is blocking system-audio recording, so the other "
            "participants will not be captured. Enable MeetingScribe under "
            "System Settings → Privacy & Security → Screen & System Audio "
            "Recording, then start the recording again."
        )
    if routing == "loopback_only":
        if auto_route:
            # No alert: the note rides along as the System row's tooltip.
            return None, (
                "Your Mac's sound output is currently set to " + loopback_name
                + ". MeetingScribe switches it to a multi-output device when you "
                "start recording, so you will hear the meeting and it still gets "
                "captured. Your previous output is restored afterwards."
            )
        return "silent", (
            "Your Mac's sound output is set to " + loopback_name + " itself, so "
            "you will not hear the other participants. Automatic audio routing "
            "is turned off (auto_route_macos), so switch the output back to your "
            "speakers or headphones in Sound settings yourself."
        )
    if routing == "not_routed" and not auto_route:
        return "not_routed", (
            "Automatic audio routing is turned off (auto_route_macos), so the "
            "other participants will not be recorded unless you route the "
            "output through " + loopback_name + " yourself."
        )
    return None, None


class MeetingRecorder:
    """Singleton-style recorder driven by the Flask app."""

    def __init__(self):
        self._lock = threading.Lock()
        self._pa = None
        self._tracks = {}
        self._silence = None
        self._stop_event = None
        self._route = None  # macOS: result of ensure_routing() while recording
        self._preflight_cache = None  # (timestamp, info, auto_route)
        self.levels = {}
        self.meeting_id = None
        self.started_at = None  # wall-clock time the meeting started

    @property
    def is_recording(self):
        with self._lock:
            return bool(self._tracks)

    def _discover(self):
        """Find (mic, system) devices and per-platform warnings."""
        warnings = []
        if BACKEND == "wasapi":
            self._pa = pyaudio.PyAudio()
            mic = _find_default_mic(self._pa)
            system = _find_default_loopback(self._pa)
            if system is None:
                warnings.append(
                    "No system-audio (loopback) device found — other participants will not be recorded."
                )
        else:
            _sd_refresh_devices()
            mic, system = _sd_find_devices()
            source = system_source()
            if source == "tap":
                # Driverless Core Audio process tap — replaces the loopback
                # device even when one is installed (override with
                # MEETINGSCRIBE_SYSTEM_SOURCE=blackhole).
                system = {"name": "system-tap", "tap_binary": _tap_binary()}
            elif source == "none":
                system = None
            elif system is None:
                warnings.append(
                    "BlackHole is not installed, so other participants will not be "
                    "recorded — only your own mic. Fix once with: brew install blackhole-2ch "
                    "(or re-run setup.sh), then restart the recording."
                )
        if mic is None:
            warnings.append("No microphone found — your own voice will not be recorded.")
        return mic, system, warnings

    def preflight(self, auto_route=True):
        """Device snapshot for the UI — no streams are opened.

        `auto_route` is the caller's auto_route_macos setting; it is reported
        back verbatim so the UI can tell "routing happens at record time" from
        "nothing will ever route".

        Cached for a few seconds so the UI can poll freely.
        """
        auto_route = bool(auto_route)
        with self._lock:
            if self._tracks:
                return {"recording": True}
            now = time.time()
            if (self._preflight_cache
                    and now - self._preflight_cache[0] < 3.0
                    and self._preflight_cache[2] == auto_route):
                return self._preflight_cache[1]
            if BACKEND == "wasapi":
                pa = pyaudio.PyAudio()
                try:
                    mic = _find_default_mic(pa)
                    system = _find_default_loopback(pa)
                finally:
                    pa.terminate()
            else:
                _sd_refresh_devices()
                mic, system = _sd_find_devices()
                # build=False: preflight is a UI poll and must never sit
                # behind a first-time swiftc run (that happens at record
                # start, in _discover).
                source = system_source(build=False)
                if source == "tap":
                    system = {"name": "System audio (Core Audio tap)", "tap": True}
                elif source == "none":
                    system = None
            info = {
                "recording": False,
                "backend": BACKEND,
                "platform": sys.platform,
                "mic": {"name": str(mic["name"])} if mic else None,
                "system": {"name": str(system["name"])} if system else None,
            }
            if system is not None and macos_audio is not None:
                info["system"]["auto_route"] = auto_route
                if system.get("tap"):
                    info["system"]["routing"] = _tap_permission_state()
                else:
                    try:
                        info["system"]["routing"] = macos_audio.routing_status(str(system["name"]))
                    except Exception as exc:
                        log.debug("routing status failed: %s", exc)
                        info["system"]["routing"] = "unknown"
                alert, note = _routing_alert(
                    info["system"]["routing"], auto_route, str(system["name"])
                )
                info["system"]["routing_alert"] = alert
                info["system"]["routing_note"] = note
            self._preflight_cache = (now, info, auto_route)
            return info

    def start(self, out_dir, meeting_id, auto_route=True, taps=None):
        with self._lock:
            if self._tracks:
                raise RuntimeError("Already recording")
            self._preflight_cache = None
            mic, system, warnings = self._discover()
            if mic is None and system is None:
                if self._pa is not None:
                    self._pa.terminate()
                    self._pa = None
                raise RuntimeError("No microphone or system-audio device available.")

            # macOS: make sure meeting audio actually reaches the loopback
            # device — without this the system track records pure silence.
            # Under the process tap nothing is routed; the one carve-out the
            # record-form tooltip promises is repairing a default output stuck
            # on a loopback device (the user would hear nothing).
            self._route = None
            tap_mode = bool(system is not None and system.get("tap_binary"))
            if tap_mode and auto_route and macos_audio is not None:
                try:
                    repaired = macos_audio.ensure_physical_output()
                    if repaired.get("changed"):
                        log.info(
                            "sound output moved off '%s' to '%s' (loopback-only repair)",
                            repaired.get("was"), repaired.get("hears"),
                        )
                except Exception as exc:
                    log.warning("loopback-only output repair failed: %s", exc)
            elif system is not None and not tap_mode and auto_route and macos_audio is not None:
                try:
                    self._route = macos_audio.ensure_routing(str(system["name"]))
                    if self._route["changed"]:
                        log.info(
                            "sound output switched to '%s' (audible via %s)",
                            self._route["via"], self._route["hears"],
                        )
                except Exception as exc:
                    log.warning("automatic audio routing failed: %s", exc)
                    warnings.append(
                        f"System audio could not be routed automatically ({exc}). "
                        "In Control Centre → Sound, choose 'MeetingScribe Output' as "
                        "the output device, then restart the recording."
                    )

            self._stop_event = threading.Event()
            self.levels.clear()
            for key, dev in (("mic", mic), ("system", system)):
                if dev is None:
                    continue
                path = out_dir / f"{key}.wav"
                if BACKEND == "wasapi":
                    rec = _WasapiTrackRecorder(self._pa, dev, path, self._stop_event, self.levels, key)
                elif dev.get("tap_binary"):
                    rec = _TapTrackRecorder(dev, path, self._stop_event, self.levels, key)
                else:
                    rec = _SounddeviceTrackRecorder(dev, path, self._stop_event, self.levels, key)
                if taps:
                    rec.tap = taps.get(key)
                self._tracks[key] = rec
            if BACKEND == "wasapi" and "system" in self._tracks:
                self._silence = _SilenceKeeper(self._pa, self._stop_event)
                self._silence.start()
            for t in self._tracks.values():
                t.start()
            self.meeting_id = meeting_id
            self.started_at = time.time()
            tracks_meta = {
                key: {"file": f"{key}.wav", "device": str(t.device["name"]), "rate": t.rate}
                for key, t in self._tracks.items()
            }
            return {"tracks": tracks_meta, "warnings": warnings, "routing": self._route}

    def status(self):
        with self._lock:
            if not self._tracks:
                return {"recording": False}
            return {
                "recording": True,
                "meeting_id": self.meeting_id,
                "elapsed": time.time() - self.started_at if self.started_at else 0,
                "levels": dict(self.levels),
                "tracks": {
                    key: {
                        "alive": t.is_alive(),
                        "error": t.error,
                        # The tap tells us within ten seconds when it is
                        # writing nothing but zeros, and until now that
                        # travelled no further than a log line: the user
                        # learned the far side was never recorded AFTER the
                        # meeting, from a diarization warning, when the only
                        # remaining option was to have held it again. It is
                        # in the live status now because every cause is
                        # fixable while the meeting is still running.
                        "silent": bool(getattr(t, "silent", False)),
                    }
                    for key, t in self._tracks.items()
                },
            }

    def stop(self):
        with self._lock:
            if not self._tracks:
                raise RuntimeError("Not recording")
            self._stop_event.set()
            stuck = []
            for key, t in self._tracks.items():
                t.join(timeout=10)
                if t.is_alive():
                    stuck.append(key)
            if stuck:
                # Escalate: force each stuck track to break its blocking read
                # (the tap recorder kills its helper so stdout hits EOF),
                # then give the threads a moment to run their cleanup.
                for key in stuck:
                    self._tracks[key].force_stop()
                still = []
                for key in stuck:
                    self._tracks[key].join(timeout=3)
                    if self._tracks[key].is_alive():
                        still.append(key)
                stuck = still
            if self._silence is not None:
                self._silence.join(timeout=5)

            starts = [t.started_at for t in self._tracks.values() if t.started_at is not None]
            base = min(starts) if starts else 0.0
            result = {"tracks": {}, "warnings": []}
            duration = 0.0
            for key, t in self._tracks.items():
                offset = (t.started_at - base) if t.started_at is not None else 0.0
                result["tracks"][key] = {
                    "file": f"{key}.wav",
                    "device": str(t.device["name"]),
                    "rate": t.rate,
                    "seconds": round(t.seconds_recorded, 2),
                    "start_offset": round(offset, 3),
                }
                duration = max(duration, t.seconds_recorded)
                if t.error:
                    result["warnings"].append(f"{key} track stopped early: {t.error}")
                result["warnings"].extend(t.extra_warnings)
            result["duration"] = round(duration, 2)
            for key in stuck:
                result["warnings"].append(f"{key} track did not shut down cleanly.")

            if self._route is not None and self._route.get("changed") and macos_audio is not None:
                try:
                    macos_audio.restore_routing()
                except Exception as exc:
                    log.warning("could not restore sound output: %s", exc)
                    result["warnings"].append(
                        f"Could not restore the previous sound output ({exc}) — "
                        "you can switch it back in Control Centre → Sound."
                    )
            self._route = None
            self._preflight_cache = None

            self._tracks.clear()
            self._silence = None
            self.meeting_id = None
            self.started_at = None
            self.levels.clear()
            if self._pa is not None:
                if stuck:
                    # A thread may still be blocked inside stream.read(); terminating
                    # PortAudio under it would crash the process. Leak it instead.
                    log.warning("skipping PortAudio terminate; tracks still running: %s", stuck)
                else:
                    try:
                        self._pa.terminate()
                    except Exception as exc:
                        log.warning("PortAudio terminate failed: %s", exc)
                self._pa = None
            return result
