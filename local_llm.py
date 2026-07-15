"""On-device language model via Apple Intelligence (Foundation Models).

All of MeetingScribe's AI features (summary, tidy, practice interviewer) run
through this module. The heavy lifting happens in a small Swift helper
(tools/apple_llm.swift -> ~/.meetingscribe/bin/apple_llm) that talks to the
~3B on-device model on the Neural Engine with guided generation, so replies
are always schema-valid JSON. Nothing ever leaves this Mac.

The helper is kept alive between calls ("serve" mode, NDJSON over pipes) so
repeated calls — e.g. the map-reduce passes of a long summary — skip process
and framework start-up. It is killed after two minutes of inactivity.
"""

import atexit
import json
import logging
import queue
import subprocess
import threading
import time
from pathlib import Path

from swift_helpers import ensure_binary

log = logging.getLogger("meetingscribe.llm")

_SRC = Path(__file__).resolve().parent / "tools" / "apple_llm.swift"
IDLE_KILL_S = 120
CHECK_CACHE_S = 60

# Rough char budget per request: the model has a 4096-token context; with
# ~4 chars/token, instructions (~700 tokens) and output (~1000 tokens)
# reserved, ~9000 chars of prompt is a safe ceiling.
MAX_PROMPT_CHARS = 9000

REASON_MESSAGES = {
    "apple_intelligence_disabled": (
        "Apple Intelligence is turned off. Enable it in "
        "System Settings → Apple Intelligence & Siri, then try again."
    ),
    "device_not_eligible": "This Mac doesn't support Apple Intelligence.",
    "model_not_ready": (
        "Apple Intelligence is still downloading its model — "
        "try again in a few minutes."
    ),
    "not_supported": (
        "On-device AI needs an Apple Silicon Mac on macOS 26 or newer."
    ),
    "unavailable": "The on-device model is unavailable right now.",
}

ERROR_MESSAGES = {
    "context_overflow": "The request was too long for the on-device model.",
    "guardrail": "The on-device model declined this content.",
    "refusal": "The on-device model declined this content.",
    "unsupported_language": "The on-device model doesn't support this language yet.",
    "rate_limited": "The on-device model is rate-limiting — try again in a moment.",
    "busy": "The on-device model is busy — try again in a moment.",
}


class LocalLLMError(RuntimeError):
    """Generation failed. `code` carries the machine-readable cause."""

    def __init__(self, message, code="error"):
        super().__init__(message)
        self.code = code


def reason_message(reason):
    return REASON_MESSAGES.get(reason, REASON_MESSAGES["unavailable"])


def _binary():
    return ensure_binary(_SRC, "apple_llm", min_macos=(26, 0))


# ------------------------------------------------------------ availability --

_check_lock = threading.Lock()
_check_cache = {"at": 0.0, "result": (False, "unavailable")}


def available(force=False):
    """-> (ok, reason). Cached for a minute; `reason` is None when ok."""
    with _check_lock:
        now = time.time()
        if not force and now - _check_cache["at"] < CHECK_CACHE_S:
            return _check_cache["result"]
        result = _probe()
        _check_cache.update(at=now, result=result)
        return result


def _probe():
    exe = _binary()
    if exe is None:
        return (False, "not_supported")
    try:
        proc = subprocess.run([exe, "check"], capture_output=True, text=True, timeout=30)
        info = json.loads(proc.stdout or "{}")
    except (subprocess.SubprocessError, OSError, ValueError) as exc:
        log.warning("apple_llm check failed: %s", exc)
        return (False, "unavailable")
    if info.get("available"):
        return (True, None)
    return (False, str(info.get("reason") or "unavailable"))


# ------------------------------------------------------------ serve process --
#
# The helper handles requests concurrently (one task per line), so several
# callers can be in flight at once — that's what makes long summaries fast
# (the map phase fans out). _proc_lock guards process lifecycle, stdin
# writes and the pending table; each caller then waits on its own event.

MAX_INFLIGHT = 3          # concurrent on-device generations (ANE saturates fast)

_proc_lock = threading.Lock()
_proc = None
_pending = {}             # id -> {"event": Event, "resp": dict|None}
_last_used = 0.0
_next_id = 1
_reaper_started = False
_inflight = threading.BoundedSemaphore(MAX_INFLIGHT)


def _reader(proc):
    """Route responses to their waiting callers; fail everything on EOF."""
    for line in proc.stdout:
        line = line.strip()
        if not line:
            continue
        try:
            resp = json.loads(line)
        except ValueError:
            continue
        with _proc_lock:
            slot = _pending.pop(resp.get("id"), None)
        if slot is not None:
            slot["resp"] = resp
            slot["event"].set()
    with _proc_lock:
        if _proc is proc:  # died on its own (not replaced by a restart)
            _stop_proc_locked()


def _start_proc_locked():
    global _proc
    exe = _binary()
    if exe is None:
        raise LocalLLMError(reason_message("not_supported"), code="unavailable")
    _proc = subprocess.Popen(
        [exe, "serve"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, bufsize=1,
    )
    threading.Thread(target=_reader, args=(_proc,), daemon=True).start()
    log.info("apple_llm serve started (pid %s)", _proc.pid)


def _stop_proc_locked():
    global _proc
    if _proc is not None:
        try:
            _proc.kill()
        except OSError:
            pass
        _proc = None
    # Anyone still waiting gets a crash answer instead of a timeout.
    for slot in _pending.values():
        slot["resp"] = {"ok": False, "error": "crashed"}
        slot["event"].set()
    _pending.clear()


def _reaper():
    while True:
        time.sleep(30)
        with _proc_lock:
            if (_proc is not None and not _pending
                    and time.time() - _last_used > IDLE_KILL_S):
                log.info("apple_llm idle — stopping")
                _stop_proc_locked()


def _submit(request):
    """Register + write one request under the lock. -> its pending slot."""
    global _next_id, _last_used, _reaper_started
    with _proc_lock:
        if _proc is None or _proc.poll() is not None:
            _stop_proc_locked()
            _start_proc_locked()
        if not _reaper_started:
            threading.Thread(target=_reaper, daemon=True).start()
            _reaper_started = True
        _next_id += 1
        request = dict(request, id=_next_id)
        slot = {"event": threading.Event(), "resp": None}
        _pending[_next_id] = slot
        _last_used = time.time()
        try:
            _proc.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
            _proc.stdin.flush()
        except (BrokenPipeError, OSError):
            _pending.pop(request["id"], None)
            _stop_proc_locked()
            raise LocalLLMError("The on-device model stopped unexpectedly.",
                                code="crashed")
    return request["id"], slot


def _roundtrip(request, timeout):
    """Send one request, wait for its response. Concurrent-safe."""
    global _last_used
    request_id, slot = _submit(request)
    if not slot["event"].wait(timeout):
        with _proc_lock:
            _pending.pop(request_id, None)
            _stop_proc_locked()  # the model is stuck; fail fast for everyone
        raise LocalLLMError("The on-device model took too long — try again.",
                            code="timeout")
    with _proc_lock:
        _last_used = time.time()
    resp = slot["resp"] or {}
    if resp.get("error") == "crashed":
        raise LocalLLMError("The on-device model stopped unexpectedly.",
                            code="crashed")
    return resp


def _stop_proc():
    with _proc_lock:
        _stop_proc_locked()


atexit.register(_stop_proc)


def generate(instructions, prompt, schema, *, temperature=0.2, max_tokens=1500,
             timeout=180):
    """One guided generation -> parsed dict/list per `schema`. Thread-safe;
    up to MAX_INFLIGHT callers run concurrently on the Neural Engine."""
    request = {
        "instructions": instructions,
        "prompt": prompt,
        "schema": schema,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    with _inflight:
        resp = None
        grew_budget = False
        for attempt in range(5):
            try:
                resp = _roundtrip(request, timeout)
            except LocalLLMError as exc:
                if exc.code != "crashed" or attempt:
                    raise
                # One retry after a crash — the OS daemon may have restarted.
                resp = _roundtrip(request, timeout)
            if resp.get("ok"):
                break
            code = str(resp.get("error") or "")
            if code == "decoding_failure" and not grew_budget:
                # max_tokens cut the constrained output mid-structure; give
                # the same request one shot with double the budget.
                grew_budget = True
                request = dict(request, max_tokens=min(2000, max_tokens * 2))
                continue
            if code not in ("busy", "rate_limited"):
                break
            # The system model rejected a concurrent request — back off and
            # retry; with the semaphore this resolves quickly.
            time.sleep(0.4 * (attempt + 1))
    if resp.get("ok"):
        return resp.get("result")
    code = str(resp.get("error") or "error")
    if code == "unavailable":
        raise LocalLLMError(reason_message(resp.get("detail") or "unavailable"),
                            code="unavailable")
    message = ERROR_MESSAGES.get(
        code, f"On-device generation failed ({resp.get('detail') or code}).")
    raise LocalLLMError(message, code=code)
