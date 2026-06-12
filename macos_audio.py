"""macOS CoreAudio routing helpers (pure ctypes, no extra dependencies).

The system track on macOS comes from a virtual loopback driver (BlackHole).
Audio only reaches BlackHole if the *default output device* is a
Multi-Output Device that feeds both the real speakers and BlackHole.
Setting that up by hand in Audio MIDI Setup is the step everyone gets
wrong, so this module does it programmatically:

  ensure_routing(name)  - before recording: create (or reuse) a stacked
                          aggregate "MeetingScribe Output" containing the
                          current real output + the loopback device, and
                          make it the default output.
  restore_routing()     - after recording: put the previous output back.

The previous default output is remembered in ~/.meetingscribe/output_route.json
so a crash mid-recording can be repaired on the next app start.
"""

import ctypes
import json
import logging
import struct
import sys
import time
from pathlib import Path

log = logging.getLogger("meetingscribe.macaudio")

AGGREGATE_UID = "com.meetingscribe.multiout"
AGGREGATE_NAME = "MeetingScribe Output"
STATE_PATH = Path.home() / ".meetingscribe" / "output_route.json"

if sys.platform != "darwin":  # pragma: no cover - imported on macOS only
    raise ImportError("macos_audio is only usable on macOS")

_ca = ctypes.CDLL("/System/Library/Frameworks/CoreAudio.framework/CoreAudio")
_cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")


# ------------------------------------------------------------ CoreFoundation --

_kCFStringEncodingUTF8 = 0x08000100
_kCFNumberSInt32Type = 3

_cf.CFStringCreateWithCString.restype = ctypes.c_void_p
_cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
_cf.CFStringGetCString.restype = ctypes.c_bool
_cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
_cf.CFNumberCreate.restype = ctypes.c_void_p
_cf.CFNumberCreate.argtypes = [ctypes.c_void_p, ctypes.c_long, ctypes.c_void_p]
_cf.CFDictionaryCreateMutable.restype = ctypes.c_void_p
_cf.CFDictionaryCreateMutable.argtypes = [ctypes.c_void_p, ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p]
_cf.CFDictionarySetValue.restype = None
_cf.CFDictionarySetValue.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
_cf.CFArrayCreateMutable.restype = ctypes.c_void_p
_cf.CFArrayCreateMutable.argtypes = [ctypes.c_void_p, ctypes.c_long, ctypes.c_void_p]
_cf.CFArrayAppendValue.restype = None
_cf.CFArrayAppendValue.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
_cf.CFArrayGetCount.restype = ctypes.c_long
_cf.CFArrayGetCount.argtypes = [ctypes.c_void_p]
_cf.CFArrayGetValueAtIndex.restype = ctypes.c_void_p
_cf.CFArrayGetValueAtIndex.argtypes = [ctypes.c_void_p, ctypes.c_long]
_cf.CFRelease.restype = None
_cf.CFRelease.argtypes = [ctypes.c_void_p]

_kCFTypeDictKeyCB = ctypes.c_void_p(
    ctypes.addressof(ctypes.c_int.in_dll(_cf, "kCFTypeDictionaryKeyCallBacks"))
)
_kCFTypeDictValueCB = ctypes.c_void_p(
    ctypes.addressof(ctypes.c_int.in_dll(_cf, "kCFTypeDictionaryValueCallBacks"))
)
_kCFTypeArrayCB = ctypes.c_void_p(
    ctypes.addressof(ctypes.c_int.in_dll(_cf, "kCFTypeArrayCallBacks"))
)


def _cfstr(text):
    return _cf.CFStringCreateWithCString(None, text.encode("utf-8"), _kCFStringEncodingUTF8)


def _cfnum(value):
    v = ctypes.c_int32(value)
    return _cf.CFNumberCreate(None, _kCFNumberSInt32Type, ctypes.byref(v))


def _cfstr_to_py(ref):
    if not ref:
        return ""
    buf = ctypes.create_string_buffer(1024)
    if _cf.CFStringGetCString(ref, buf, len(buf), _kCFStringEncodingUTF8):
        return buf.value.decode("utf-8", "replace")
    return ""


def _cf_from_py(value):
    """Convert str/int/list/dict to a new (owned) CF object."""
    if isinstance(value, str):
        return _cfstr(value)
    if isinstance(value, bool) or isinstance(value, int):
        return _cfnum(int(value))
    if isinstance(value, dict):
        d = _cf.CFDictionaryCreateMutable(None, 0, _kCFTypeDictKeyCB, _kCFTypeDictValueCB)
        for k, v in value.items():
            ck, cv = _cfstr(k), _cf_from_py(v)
            _cf.CFDictionarySetValue(d, ck, cv)
            _cf.CFRelease(ck)
            _cf.CFRelease(cv)
        return d
    if isinstance(value, (list, tuple)):
        arr = _cf.CFArrayCreateMutable(None, 0, _kCFTypeArrayCB)
        for item in value:
            ci = _cf_from_py(item)
            _cf.CFArrayAppendValue(arr, ci)
            _cf.CFRelease(ci)
        return arr
    raise TypeError(f"unsupported CF conversion: {type(value)}")


# ---------------------------------------------------------------- CoreAudio --

def _fourcc(code):
    return struct.unpack(">I", code.encode("ascii"))[0]


class _PropAddr(ctypes.Structure):
    _fields_ = [
        ("mSelector", ctypes.c_uint32),
        ("mScope", ctypes.c_uint32),
        ("mElement", ctypes.c_uint32),
    ]


_SYSTEM_OBJECT = 1  # kAudioObjectSystemObject
_SCOPE_GLOBAL = _fourcc("glob")
_SCOPE_OUTPUT = _fourcc("outp")
_SEL_DEVICES = _fourcc("dev#")
_SEL_DEFAULT_OUTPUT = _fourcc("dOut")
_SEL_UID = _fourcc("uid ")
_SEL_NAME = _fourcc("lnam")
_SEL_TRANSPORT = _fourcc("tran")
_SEL_STREAMS = _fourcc("stm#")
_SEL_SUBDEVICES = _fourcc("grup")  # kAudioAggregateDevicePropertyFullSubDeviceList
_TRANSPORT_VIRTUAL = _fourcc("virt")
_TRANSPORT_AGGREGATE = _fourcc("grup")
_TRANSPORT_BUILTIN = _fourcc("bltn")

_ca.AudioObjectGetPropertyDataSize.restype = ctypes.c_int32
_ca.AudioObjectGetPropertyDataSize.argtypes = [
    ctypes.c_uint32, ctypes.POINTER(_PropAddr), ctypes.c_uint32, ctypes.c_void_p,
    ctypes.POINTER(ctypes.c_uint32),
]
_ca.AudioObjectGetPropertyData.restype = ctypes.c_int32
_ca.AudioObjectGetPropertyData.argtypes = [
    ctypes.c_uint32, ctypes.POINTER(_PropAddr), ctypes.c_uint32, ctypes.c_void_p,
    ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p,
]
_ca.AudioObjectSetPropertyData.restype = ctypes.c_int32
_ca.AudioObjectSetPropertyData.argtypes = [
    ctypes.c_uint32, ctypes.POINTER(_PropAddr), ctypes.c_uint32, ctypes.c_void_p,
    ctypes.c_uint32, ctypes.c_void_p,
]
_ca.AudioObjectHasProperty.restype = ctypes.c_bool
_ca.AudioObjectHasProperty.argtypes = [ctypes.c_uint32, ctypes.POINTER(_PropAddr)]
_ca.AudioHardwareCreateAggregateDevice.restype = ctypes.c_int32
_ca.AudioHardwareCreateAggregateDevice.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
_ca.AudioHardwareDestroyAggregateDevice.restype = ctypes.c_int32
_ca.AudioHardwareDestroyAggregateDevice.argtypes = [ctypes.c_uint32]


def _addr(selector, scope=_SCOPE_GLOBAL):
    return _PropAddr(selector, scope, 0)


def _check(status, what):
    if status != 0:
        try:
            code = struct.pack(">i", status).decode("ascii")
        except (UnicodeDecodeError, struct.error):
            code = str(status)
        raise RuntimeError(f"CoreAudio {what} failed ({code})")


def _get_data(obj, selector, scope=_SCOPE_GLOBAL):
    addr = _addr(selector, scope)
    size = ctypes.c_uint32(0)
    _check(_ca.AudioObjectGetPropertyDataSize(obj, ctypes.byref(addr), 0, None, ctypes.byref(size)),
           f"size of '{struct.pack('>I', selector).decode()}'")
    buf = ctypes.create_string_buffer(size.value)
    _check(_ca.AudioObjectGetPropertyData(obj, ctypes.byref(addr), 0, None, ctypes.byref(size), buf),
           f"get '{struct.pack('>I', selector).decode()}'")
    return buf.raw[: size.value]


def _device_ids():
    raw = _get_data(_SYSTEM_OBJECT, _SEL_DEVICES)
    n = len(raw) // 4
    return list(struct.unpack(f"{n}I", raw))


def _device_uid(dev):
    ref = ctypes.c_void_p(struct.unpack("Q", _get_data(dev, _SEL_UID).ljust(8, b"\0"))[0])
    out = _cfstr_to_py(ref)
    if ref:
        _cf.CFRelease(ref)
    return out


def _device_name(dev):
    try:
        ref = ctypes.c_void_p(struct.unpack("Q", _get_data(dev, _SEL_NAME).ljust(8, b"\0"))[0])
    except RuntimeError:
        return ""
    out = _cfstr_to_py(ref)
    if ref:
        _cf.CFRelease(ref)
    return out


def _device_transport(dev):
    try:
        return struct.unpack("I", _get_data(dev, _SEL_TRANSPORT)[:4])[0]
    except RuntimeError:
        return 0


def _has_output(dev):
    try:
        return len(_get_data(dev, _SEL_STREAMS, _SCOPE_OUTPUT)) >= 4
    except RuntimeError:
        return False


def _default_output():
    return struct.unpack("I", _get_data(_SYSTEM_OBJECT, _SEL_DEFAULT_OUTPUT)[:4])[0]


def _set_default_output(dev):
    addr = _addr(_SEL_DEFAULT_OUTPUT)
    val = ctypes.c_uint32(dev)
    _check(
        _ca.AudioObjectSetPropertyData(
            _SYSTEM_OBJECT, ctypes.byref(addr), 0, None, 4, ctypes.byref(val)
        ),
        "set default output",
    )


def _subdevice_uids(dev):
    """UIDs inside an aggregate/multi-output device ([] for plain devices)."""
    addr = _addr(_SEL_SUBDEVICES)
    if not _ca.AudioObjectHasProperty(dev, ctypes.byref(addr)):
        return []
    try:
        arr = ctypes.c_void_p(struct.unpack("Q", _get_data(dev, _SEL_SUBDEVICES).ljust(8, b"\0"))[0])
    except RuntimeError:
        return []
    if not arr:
        return []
    uids = []
    for i in range(_cf.CFArrayGetCount(arr)):
        uids.append(_cfstr_to_py(ctypes.c_void_p(_cf.CFArrayGetValueAtIndex(arr, i))))
    _cf.CFRelease(arr)
    return uids


def _find_by_uid(uid):
    for dev in _device_ids():
        if _device_uid(dev) == uid:
            return dev
    return None


def _find_loopback(name):
    """The loopback device, by exact name first, then by 'blackhole' hint."""
    fallback = None
    for dev in _device_ids():
        dev_name = _device_name(dev)
        if dev_name == name:
            return dev
        if fallback is None and "blackhole" in dev_name.lower():
            fallback = dev
    return fallback


def _find_real_output(preferred=None):
    """A physical output device: the preferred one if usable, else built-in."""
    if preferred is not None:
        transport = _device_transport(preferred)
        if transport not in (_TRANSPORT_VIRTUAL, _TRANSPORT_AGGREGATE) and _has_output(preferred):
            return preferred
    builtin = None
    first = None
    for dev in _device_ids():
        if not _has_output(dev):
            continue
        transport = _device_transport(dev)
        if transport in (_TRANSPORT_VIRTUAL, _TRANSPORT_AGGREGATE):
            continue
        if transport == _TRANSPORT_BUILTIN:
            builtin = dev
        if first is None:
            first = dev
    return builtin or first


def _create_multi_output(real_uid, loopback_uid):
    desc = {
        "name": AGGREGATE_NAME,
        "uid": AGGREGATE_UID,
        "stacked": 1,   # stacked aggregate == Multi-Output Device
        "private": 0,   # visible in Audio MIDI Setup
        "master": real_uid,
        "subdevices": [
            {"uid": real_uid, "drift": 0},
            {"uid": loopback_uid, "drift": 1},
        ],
    }
    cf_desc = _cf_from_py(desc)
    dev = ctypes.c_uint32(0)
    try:
        _check(_ca.AudioHardwareCreateAggregateDevice(cf_desc, ctypes.byref(dev)),
               "create multi-output device")
    finally:
        _cf.CFRelease(cf_desc)
    # The device needs a beat before it accepts being made default.
    for _ in range(20):
        if _find_by_uid(AGGREGATE_UID) is not None:
            return dev.value
        time.sleep(0.05)
    raise RuntimeError("multi-output device did not appear after creation")


# ----------------------------------------------------------------- routing --

def _save_state(previous_uid):
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        if STATE_PATH.exists():  # keep the original pre-switch device across crashes
            return
        STATE_PATH.write_text(json.dumps({"previous_output_uid": previous_uid}), encoding="utf-8")
    except OSError as exc:
        log.warning("could not save output state: %s", exc)


def routing_status(loopback_name):
    """How the default output relates to the loopback device right now.

    Returns "routed" (default output feeds the loopback), "loopback_only"
    (default output IS the loopback - silent for the user), "not_routed",
    or "no_loopback" when the device does not exist at all.
    """
    loop = _find_loopback(loopback_name)
    if loop is None:
        return "no_loopback"
    loop_uid = _device_uid(loop)
    current = _default_output()
    if _device_uid(current) == loop_uid:
        return "loopback_only"
    if loop_uid in _subdevice_uids(current):
        return "routed"
    return "not_routed"


def ensure_routing(loopback_name):
    """Make the default output feed both the real speakers and the loopback.

    Returns {"changed": bool, "via": <device name>, "hears": <real output name>}.
    Raises RuntimeError if CoreAudio refuses.
    """
    loop = _find_loopback(loopback_name)
    if loop is None:
        raise RuntimeError(f"loopback device '{loopback_name}' not found")
    loop_uid = _device_uid(loop)

    current = _default_output()
    current_uid = _device_uid(current)
    if loop_uid in _subdevice_uids(current):
        return {"changed": False, "via": _device_name(current), "hears": _device_name(current)}

    real = _find_real_output(preferred=None if current_uid == loop_uid else current)
    if real is None:
        raise RuntimeError("no physical output device found to pair with the loopback")
    real_uid = _device_uid(real)

    # Reuse our aggregate when it already pairs the right devices, else rebuild
    # it (covers e.g. switching from speakers to AirPods between meetings).
    agg = _find_by_uid(AGGREGATE_UID)
    if agg is not None:
        subs = set(_subdevice_uids(agg))
        if subs != {real_uid, loop_uid}:
            _check(_ca.AudioHardwareDestroyAggregateDevice(agg), "destroy stale multi-output")
            agg = None
    if agg is None:
        _create_multi_output(real_uid, loop_uid)
        agg = _find_by_uid(AGGREGATE_UID)
    if agg is None:
        raise RuntimeError("multi-output device did not appear after creation")

    if current_uid != AGGREGATE_UID:
        _save_state(current_uid if current_uid != loop_uid else real_uid)
    _set_default_output(agg)
    log.info("default output switched to '%s' (was '%s')", AGGREGATE_NAME, _device_name(current))
    return {"changed": True, "via": AGGREGATE_NAME, "hears": _device_name(real)}


def restore_routing():
    """Undo ensure_routing(). Safe to call when there is nothing to undo.

    Only restores when the default output is still our aggregate, so a user
    who picked a different output mid-meeting is left alone.
    """
    if not STATE_PATH.exists():
        return False
    try:
        previous_uid = json.loads(STATE_PATH.read_text(encoding="utf-8")).get("previous_output_uid")
    except (ValueError, OSError):
        previous_uid = None
    restored = False
    try:
        if previous_uid and _device_uid(_default_output()) == AGGREGATE_UID:
            previous = _find_by_uid(previous_uid)
            if previous is not None:
                _set_default_output(previous)
                log.info("default output restored to '%s'", _device_name(previous))
                restored = True
    finally:
        try:
            STATE_PATH.unlink()
        except OSError:
            pass
    return restored
