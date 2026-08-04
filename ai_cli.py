"""The user's own AI CLIs, as interchangeable cloud engines.

Summaries and Ask answers can run on any assistant CLI the user already has
installed and signed into — Claude Code, OpenAI Codex, Gemini CLI, GitHub
Copilot — instead of being hard-wired to `claude`. The registry below is the
single description of every supported CLI: where its binary lives, how to run
it once non-interactively, and how to get the answer text back out.

Division of labour with summarize.py / ask.py, chosen deliberately:

  * `claude` KEEPS its specialised code paths there (the JSON envelope in
    summarize._summarize_claude, the stream-json incremental path in
    ask._stream_claude, and the --tools ""/--strict-mcp-config sandbox).
    Those are battle-tested against real CLI quirks and stream partial
    answers; nothing here replaces them.
  * Every OTHER provider runs through run_one_shot() below: prompt in on
    stdin (never argv — a transcript in `ps` output, and macOS ARG_MAX,
    both say no), whole answer out. No streaming: only Claude's CLI has a
    stable incremental-output contract today, so the others deliver the
    answer in one piece and the caller's on_delta fires once.

Containment for the non-claude CLIs: each runs in an EMPTY working directory
(no project files, no repo, no instructions files to pick up) with no
tool-approval flags passed, so agentic tool use never auto-fires; Codex
additionally runs under its default read-only sandbox. That is weaker than
the claude sandbox's explicit "no tools at all" flags — these CLIs offer no
equivalent switch — and the transcript is still untrusted input, which is why
none of them is granted approvals. The same variadic-flag trap documented at
summarize.claude_sandbox() applies here too: the prompt NEVER rides argv.

The prompt/transcript leaves the machine either way: every provider is a
cloud service. That is already true of the `claude` engine — "voice never
leaves the device" refers to AUDIO, which no engine ever receives — but the
Settings UI states it per provider, and Gemini's free tier gets an extra note
there (its terms allow training on free-tier prompts).
"""

import json
import logging
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
from pathlib import Path

log = logging.getLogger("meetingscribe.ai_cli")

# Where CLIs land on macOS when a GUI app's PATH doesn't include them: the
# packaged app inherits launchd's minimal PATH, so shutil.which() alone would
# miss a perfectly good install in ~/.local/bin or /opt/homebrew/bin.
_HOME = Path.home()
_COMMON_BIN_DIRS = (
    _HOME / ".local" / "bin",
    Path("/opt/homebrew/bin"),
    Path("/usr/local/bin"),
    _HOME / ".bun" / "bin",
    _HOME / ".npm-global" / "bin",
    _HOME / ".volta" / "bin",
)

PROVIDERS = {
    "claude": {
        "label": "Claude",
        "binary": "claude",
        "extra_paths": (_HOME / ".claude" / "local" / "claude",),
        "model_flag": "--model",
        "setup_help": (
            "MeetingScribe uses YOUR Claude account (no API key). One-time "
            "setup: install Claude Code — `npm install -g "
            "@anthropic-ai/claude-code` — then run `claude` in Terminal once "
            "and sign in."),
        "signed_out": r"log ?in|logged out|authent|credential|api key|/login",
    },
    "codex": {
        "label": "Codex",
        "binary": "codex",
        "extra_paths": (),
        "model_flag": "-m",
        "setup_help": (
            "To use OpenAI Codex here: install it — `npm install -g "
            "@openai/codex` or `brew install codex` — then run `codex` in "
            "Terminal once and sign in with your ChatGPT account."),
        "signed_out": r"log ?in|logged out|authent|credential|api key|not signed",
    },
    "gemini": {
        "label": "Gemini",
        "binary": "gemini",
        "extra_paths": (),
        "model_flag": "-m",
        "setup_help": (
            "To use Google's Gemini CLI here: install it — `npm install -g "
            "@google/gemini-cli` or `brew install gemini-cli` — then run "
            "`gemini` in Terminal once and sign in."),
        "signed_out": r"log ?in|logged out|authent|credential|api.?key|oauth",
    },
    "copilot": {
        "label": "Copilot",
        "binary": "copilot",
        "extra_paths": (),
        "model_flag": "--model",
        "setup_help": (
            "To use GitHub Copilot here: install its CLI — `npm install -g "
            "@github/copilot` — then run `copilot` in Terminal once and use "
            "/login. Note each answer spends Copilot premium requests."),
        "signed_out": r"log ?in|logged out|authent|credential|token|/login",
    },
}

# Engines summaries/Ask accept for config "summary_engine" besides "apple".
CLOUD_ENGINES = tuple(PROVIDERS)


def find_cli(engine):
    """Absolute path of the provider's binary, or None if not installed."""
    p = PROVIDERS.get(engine)
    if not p:
        return None
    exe = shutil.which(p["binary"])
    if exe:
        return exe
    for cand in tuple(d / p["binary"] for d in _COMMON_BIN_DIRS) + tuple(p["extra_paths"]):
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    return None


def detect_all():
    """[{id, label, installed, path}] for every provider — the Settings UI's
    source of truth, so the probe list can't drift per front end."""
    out = []
    for engine, p in PROVIDERS.items():
        path = find_cli(engine)
        out.append({"id": engine, "label": p["label"],
                    "installed": path is not None, "path": path})
    return out


class NeedsCLIError(RuntimeError):
    """The chosen CLI is missing or signed out — the UI walks the user through
    setup. Subclassed by summarize.NeedsClaudeError's alias so existing
    app.py/except-clauses keep catching both."""

    def __init__(self, message, engine="claude"):
        super().__init__(message)
        self.engine = engine


def _kill_tree(proc):
    """SIGKILL the process group `proc` leads (start_new_session=True below
    gives it one of its own), falling back to the process. Same rationale as
    ask._kill_tree: killing only the wrapper leaves a child holding our
    stdout pipe and the read loop never sees EOF."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        return
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        proc.kill()
    except ProcessLookupError:
        pass


def _argv(engine, exe, model, workdir):
    """One-shot argv for a non-claude provider. The prompt goes on stdin in
    every case. Returns (argv, answer_file_or_None)."""
    p = PROVIDERS[engine]
    model_args = [p["model_flag"], str(model)] if model else []
    if engine == "codex":
        # `codex exec -` makes stdin the entire prompt; the final answer is
        # written to --output-last-message (stdout carries event noise).
        # --skip-git-repo-check: our empty sandbox cwd is deliberately not a
        # repo. The policy flags are PINNED, never inherited: the user's
        # ~/.codex/config.toml may set a permissive sandbox for their own
        # coding, and an injected transcript must not ride that.
        #   --sandbox read-only       shell tools cannot write or reach the
        #                             network, whatever the user's default is
        #   --ignore-user-config      don't load config.toml at all ("auth
        #                             still uses CODEX_HOME" — verified in
        #                             `codex exec --help`), so trusted MCP
        #                             servers configured there never attach
        #   --ephemeral               no session file: the transcript is not
        #                             persisted into ~/.codex/sessions
        answer = workdir / "answer.md"
        return [exe, "exec", "-", "--output-last-message", str(answer),
                "--skip-git-repo-check", "--sandbox", "read-only",
                "--ignore-user-config", "--ephemeral", *model_args], answer
    if engine == "gemini":
        # Piped stdin engages headless mode; --output-format json gives
        # {"response": ...} which survives any banner noise on stdout.
        # Policy pinned for the same reason as codex: the user's
        # ~/.gemini/settings.json may carry "yolo" approval or trusted MCP
        # servers. --approval-mode plan is the CLI's read-only mode (denies
        # every tool), and it beats an ambient yolo default because the
        # explicit flag wins; -e none loads no extensions.
        return [exe, "--approval-mode", "plan", "-e", "none",
                "--output-format", "json", *model_args], None
    if engine == "copilot":
        # Piped stdin is the prompt (-p would make it ignored); -s silences
        # stats/decoration so stdout is just the answer text. --deny-tool '*'
        # is the documented programmatic-mode switch that refuses every tool
        # (an unsupported flag makes the run fail loudly, which is the right
        # direction for a policy flag).
        return [exe, "-s", "--deny-tool", "*", *model_args], None
    raise ValueError(f"no one-shot recipe for engine {engine!r}")


def _parse(engine, stdout, answer_file):
    """The answer text out of a finished one-shot run, or None."""
    if engine == "codex":
        try:
            text = answer_file.read_text(encoding="utf-8").strip()
            return text or None
        except OSError:
            return None
    if engine == "gemini":
        # The payload is a pretty-printed {"response": ...} object, and the
        # CLI writes banner noise around it ("Loaded cached credentials.").
        # Parse from each "{" that starts a line until one slice balances —
        # raw.decoder gives the object's true extent, noise after and all.
        decoder = json.JSONDecoder()
        for m in re.finditer(r"\{", stdout):
            try:
                obj, _end = decoder.raw_decode(stdout, m.start())
            except ValueError:
                continue
            if isinstance(obj, dict) and "response" in obj:
                return str(obj.get("response") or "").strip() or None
        return None
    return stdout.strip() or None  # copilot: plain text


def run_one_shot(engine, prompt, timeout, model=None, progress_cb=None):
    """Run a non-claude provider once. Returns the answer text.

    Raises NeedsCLIError when the CLI is missing or looks signed out, and
    RuntimeError for everything else. The prompt goes in on a temp FILE wired
    as stdin (a pipe can deadlock against a full buffer while we wait), and
    the whole tree is killed on timeout — see _kill_tree.
    """
    p = PROVIDERS.get(engine)
    if p is None:
        raise ValueError(f"unknown engine {engine!r}")
    exe = find_cli(engine)
    if exe is None:
        raise NeedsCLIError(p["setup_help"], engine=engine)
    if progress_cb:
        progress_cb(f"Asking {p['label']}…")

    with tempfile.TemporaryDirectory(prefix=f"meetingscribe-{engine}-") as tmp:
        root = Path(tmp)
        cwd = root / "cwd"           # empty: no repo, no instruction files
        cwd.mkdir()
        argv, answer_file = _argv(engine, exe, model, root)
        with tempfile.TemporaryFile("w+", encoding="utf-8") as stdin, \
                tempfile.TemporaryFile("w+", encoding="utf-8",
                                       errors="replace") as stdout, \
                tempfile.TemporaryFile("w+", encoding="utf-8",
                                       errors="replace") as stderr:
            stdin.write(prompt)
            stdin.flush()
            stdin.seek(0)
            proc = subprocess.Popen(
                argv, cwd=str(cwd), stdin=stdin, stdout=stdout, stderr=stderr,
                text=True, start_new_session=True)
            timed_out = threading.Event()

            def give_up():
                timed_out.set()
                _kill_tree(proc)

            watchdog = threading.Timer(timeout, give_up)
            watchdog.start()
            try:
                proc.wait()
            finally:
                watchdog.cancel()
            stdout.seek(0)
            out = stdout.read()
            stderr.seek(0)
            err = stderr.read().strip()

        reply = _parse(engine, out, answer_file)
        if reply and proc.returncode == 0:
            # A finished answer is a finished answer, even when the watchdog
            # fired in the gap between the process exiting and the timer
            # being cancelled.
            return reply
        if timed_out.is_set():
            raise RuntimeError(
                f"{p['label']} took too long to answer — please try again.")
        if proc.returncode != 0 or not reply:
            detail = (err or out or "").strip()
            # Classify off the EXTRACTED failure line, not the raw stream:
            # gemini prints "Loaded cached credentials." as a banner on every
            # run, and matching `credential` against the whole blob turned a
            # quota error into "you are signed out".
            line = _failure_line(detail)
            if re.search(p["signed_out"], line, re.I):
                raise NeedsCLIError(
                    f"Your {p['label']} CLI is not signed in. "
                    + p["setup_help"], engine=engine)
            raise RuntimeError(f"{p['label']} could not answer: {line}")
        return reply


def _failure_line(detail):
    """One human-readable line out of a CLI's failure output. Several CLIs
    emit pretty-printed JSON on stderr, whose literal last line is '}' —
    dig the error message out instead of showing the user a brace."""
    if not detail:
        return "no output"
    decoder = json.JSONDecoder()
    for m in re.finditer(r"\{", detail):
        try:
            obj, _end = decoder.raw_decode(detail, m.start())
        except ValueError:
            continue
        if isinstance(obj, dict):
            err = obj.get("error")
            msg = (err or {}).get("message") if isinstance(err, dict) \
                else obj.get("message")
            if msg:
                return str(msg).strip().splitlines()[0]
    return detail.splitlines()[-1]
