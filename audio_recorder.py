"""Dual-track meeting audio recorder (Windows + macOS).

Captures the default microphone and the system audio (everything you hear
through your speakers/headphones) as two separate WAV files. Keeping the
tracks separate is what lets the pipeline tell "you" apart from the other
meeting participants with certainty.

Windows: system audio comes from WASAPI loopback (PyAudioWPatch); a
silence-keeper output stream keeps the loopback flowing during quiet moments.

macOS: CoreAudio has no built-in loopback, so the recorder looks for a
virtual loopback input device such as BlackHole (free,
https://existential.audio/blackhole/). When BlackHole is present, the
recorder routes audio to it automatically: macos_audio.ensure_routing()
creates a "MeetingScribe Output" Multi-Output Device (real speakers +
BlackHole), makes it the default output for the duration of the recording,
and restores the previous output afterwards. The mic is recorded normally
via sounddevice.
"""

import logging
import os
import sys
import threading
import time
import wave

import numpy as np

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

class MeetingRecorder:
    """Singleton-style recorder driven by the Flask app."""

    def __init__(self):
        self._lock = threading.Lock()
        self._pa = None
        self._tracks = {}
        self._silence = None
        self._stop_event = None
        self._route = None  # macOS: result of ensure_routing() while recording
        self._preflight_cache = None  # (timestamp, info)
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
            if system is None:
                warnings.append(
                    "BlackHole is not installed, so other participants will not be "
                    "recorded — only your own mic. Fix once with: brew install blackhole-2ch "
                    "(or re-run setup.sh), then restart the recording."
                )
        if mic is None:
            warnings.append("No microphone found — your own voice will not be recorded.")
        return mic, system, warnings

    def preflight(self):
        """Device snapshot for the UI — no streams are opened.

        Cached for a few seconds so the UI can poll freely.
        """
        with self._lock:
            if self._tracks:
                return {"recording": True}
            now = time.time()
            if self._preflight_cache and now - self._preflight_cache[0] < 3.0:
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
            info = {
                "recording": False,
                "backend": BACKEND,
                "platform": sys.platform,
                "mic": {"name": str(mic["name"])} if mic else None,
                "system": {"name": str(system["name"])} if system else None,
            }
            if system is not None and macos_audio is not None:
                try:
                    info["system"]["routing"] = macos_audio.routing_status(str(system["name"]))
                    info["system"]["auto_route"] = True
                except Exception as exc:
                    log.debug("routing status failed: %s", exc)
                    info["system"]["routing"] = "unknown"
            self._preflight_cache = (now, info)
            return info

    def start(self, out_dir, meeting_id, auto_route=True):
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
            self._route = None
            if system is not None and auto_route and macos_audio is not None:
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
                else:
                    rec = _SounddeviceTrackRecorder(dev, path, self._stop_event, self.levels, key)
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
                    key: {"alive": t.is_alive(), "error": t.error}
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
