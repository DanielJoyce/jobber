"""The Claude Code CLI runner for packet drafting (specs/017 "CLI runner").

One ``claude -p`` subprocess per drafting call, on the user's Claude subscription::

    claude -p --output-format stream-json --verbose
           --model opus --effort medium            (no --effort for the Haiku call)
           --system-prompt <prompt> --json-schema <schema>
           --tools '' --strict-mcp-config --setting-sources '' --safe-mode
           --disable-slash-commands --no-session-persistence --max-budget-usd 1.00

Safety properties, each enforced here and tested with a fake ``claude`` on ``PATH``:

- **Environment built, not inherited** (:func:`child_env`): only an allowlist passes, so an
  ``ANTHROPIC_API_KEY`` loaded from ``.env`` can never reach the child and bill the API while
  we log $0. Non-essential traffic (telemetry, error reporting, auto-update) is turned off.
- **One fixed, empty working directory** (``<cache dir>/apply-cli/``), checked before each call.
- **The ``system``/``init`` line is checked before the model is called**: ``apiKeySource`` must
  be ``none``, ``tools`` exactly ``["StructuredOutput"]`` and ``mcp_servers`` empty. On a
  mismatch the child is killed at once and the runner is turned off (:class:`CliFailure`
  with ``turn_off``).
- **Paid extra usage** (``isUsingOverage`` / ``overageInUse`` on a ``rate_limit_event``) asks
  the caller (``on_overage``); when the caller says no (the apply cap is reached) the child is
  killed.
- ``claude auth status`` must report a claude.ai first-party login before the first call of the
  process (:func:`check_auth`).

The only process launch is :func:`_spawn` and the only binary lookup is :func:`_lookup`; the
test suite's autouse guard replaces both, so no test can execute the real CLI.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import shutil
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

BINARY = "claude"
TIMEOUT_S = 180.0
AUTH_TIMEOUT_S = 30.0
MAX_BUDGET_USD = "1.00"
CWD_NAME = "apply-cli"

# Passed through to the child unchanged when set. Everything else is dropped.
PASS_THROUGH = (
    "PATH",
    "HOME",
    "LANG",
    "TMPDIR",
    "CLAUDE_CONFIG_DIR",  # where the login lives, if moved
    "CLAUDE_CODE_OAUTH_TOKEN",  # a subscription token from `claude setup-token`
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "NO_PROXY",
    "https_proxy",
    "http_proxy",
    "no_proxy",
    "NODE_EXTRA_CA_CERTS",
    "SSL_CERT_FILE",
)
PASS_PREFIXES = ("XDG_",)
FORCED_ENV = {
    # Telemetry, error reporting and auto-update off for the call.
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    # Never attach files the prompt @-mentions: posting text is untrusted, and an
    # "@~/.ssh/id_rsa" in it would otherwise be read and sent before any tool is involved.
    "CLAUDE_CODE_DISABLE_ATTACHMENTS": "1",
    # Belt and braces with --safe-mode: no CLAUDE.md files and no auto-memory in the request.
    "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
}
# Named only for the tests and the docs: these never reach the child (they are not allowlisted).
NEVER_PASSED = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDECODE",
)
EXPECTED_TOOLS = ["StructuredOutput"]


class CliFailure(Exception):
    """A CLI run that produced no usable result.

    ``turn_off``: the runner must be turned off until the user looks (init check, auth).
    ``killed``: the child was killed by us. ``result`` carries any usage the CLI reported,
    so the caller can still log what was spent.
    """

    def __init__(
        self,
        reason: str,
        *,
        turn_off: bool = False,
        killed: bool = False,
        result: CliResult | None = None,
        overage: bool = False,
        started: bool = False,
        partial: CliResult | None = None,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.turn_off = turn_off
        self.killed = killed
        self.result = result
        # The stream reported paid extra usage before the failure (the request was served).
        self.overage = overage
        # The init line passed, so the request may have reached the model.
        self.started = started
        # Usage seen on assistant lines before a kill or timeout, when there is no result.
        self.partial = partial


@dataclass
class CliResult:
    """The final ``result`` line, plus what the stream said on the way."""

    structured: Any = None
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    total_cost_usd: float = 0.0
    overage: bool = False
    is_error: bool = False
    subtype: str = ""
    raw_usage: dict[str, Any] = field(default_factory=dict)

    @property
    def all_input_tokens(self) -> int:
        return self.input_tokens + self.cache_read_tokens + self.cache_creation_tokens


# ─── lookup, environment, argv ──────────────────────────────────────────────


def _lookup(name: str) -> str | None:
    """Where ``claude`` is. Replaced by the test guard (see tests/conftest.py)."""
    return shutil.which(name)


def find_binary() -> str | None:
    return _lookup(BINARY)


def child_env(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The child's whole environment: the allowlist, plus non-essential traffic off."""
    source = os.environ if environ is None else environ
    env = {
        k: v
        for k, v in source.items()
        if k in PASS_THROUGH or any(k.startswith(p) for p in PASS_PREFIXES)
    }
    env.update(FORCED_ENV)
    return env


def build_argv(
    binary: str,
    *,
    model: str,
    system_prompt: str,
    schema: Mapping[str, Any],
    effort: str | None,
    max_budget_usd: str = MAX_BUDGET_USD,
) -> list[str]:
    """The exact command line. ``effort`` None for Haiku, which rejects an effort setting.
    ``max_budget_usd`` is lowered below $1.00 for a run the user confirmed as paid."""
    argv = [binary, "-p", "--output-format", "stream-json", "--verbose", "--model", model]
    if effort is not None:
        argv += ["--effort", effort]
    argv += [
        "--system-prompt",
        system_prompt,
        "--json-schema",
        json.dumps(schema, separators=(",", ":")),
        "--tools",
        "",
        "--strict-mcp-config",
        "--setting-sources",
        "",
        "--safe-mode",
        "--disable-slash-commands",
        "--no-session-persistence",
        "--restricted",
        "--max-budget-usd",
        max_budget_usd,
    ]
    return argv


# Claude Code processes the prompt before the model sees it: "@path" attaches a file, a leading
# "!" is a shell escape and a leading "/" or "#" is a command or memory shortcut in some modes.
# The message is data, so every token that starts with "@" is rewritten to "(at)" and the
# message starts with a plain sentence. Email addresses ("<you>@example.com") are untouched:
# their "@" is not at a token start.
_AT_TOKEN = re.compile(r"(?<!\S)@")
PREAMBLE = "The candidate's materials follow, as plain data.\n\n"


def neutralize(message: str) -> str:
    return PREAMBLE + _AT_TOKEN.sub("(at)", message)


def work_dir(cache_dir: Path) -> Path:
    """``<cache dir>/apply-cli/``: created once, and it must be empty before every call."""
    path = Path(cache_dir).expanduser() / CWD_NAME
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def _spawn(argv: Sequence[str], **kwargs: Any) -> subprocess.Popen[bytes]:
    """The only process launch in this module. Replaced by the test guard."""
    return subprocess.Popen(list(argv), **kwargs)


def _kill(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)  # the CLI may have children of its own
    except (ProcessLookupError, PermissionError, OSError):
        proc.kill()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        logger.warning("claude child %d did not exit after SIGKILL", proc.pid)


# ─── auth status ────────────────────────────────────────────────────────────

_AUTH_OK: set[str] = set()


def reset_auth_cache() -> None:
    _AUTH_OK.clear()


def check_auth(binary: str, cwd: Path, environ: Mapping[str, str] | None = None) -> None:
    """``claude auth status`` must show a claude.ai first-party login. Cached per process
    (success only, so a failure is re-checked after **Turn back on**)."""
    if binary in _AUTH_OK:
        return
    try:
        proc = _spawn(
            [binary, "auth", "status", "--json"],
            cwd=str(cwd),
            env=child_env(environ),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        raise CliFailure("the claude command is not installed") from exc
    try:
        out, _err = proc.communicate(timeout=AUTH_TIMEOUT_S)
    except subprocess.TimeoutExpired as exc:
        _kill(proc)
        raise CliFailure("claude auth status timed out") from exc
    try:
        data = json.loads(out.decode("utf-8", "replace") or "{}")
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    method, provider = data.get("authMethod"), data.get("apiProvider")
    if method != "claude.ai" or provider != "firstParty" or data.get("loggedIn") is False:
        raise CliFailure(
            "claude auth status does not show a claude.ai subscription login "
            f"(authMethod {method!r}, apiProvider {provider!r}); the CLI runner is off so "
            "nothing can bill an API key. Run `claude` and log in with your subscription, "
            "then turn the runner back on",
            turn_off=True,
        )
    _AUTH_OK.add(binary)


# ─── the run ────────────────────────────────────────────────────────────────


def _int(d: Mapping[str, Any], key: str) -> int:
    try:
        return int(d.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def check_init(line: Mapping[str, Any]) -> str | None:
    """Why the ``system``/``init`` line is unacceptable, or None."""
    source = line.get("apiKeySource")
    if source != "none":
        return f"the CLI would authenticate with an API key (apiKeySource {source!r})"
    tools = line.get("tools")
    if tools != EXPECTED_TOOLS:
        return f"the CLI offered tools {tools!r}; only {EXPECTED_TOOLS!r} is allowed"
    mcp = line.get("mcp_servers")
    if mcp != []:
        return f"the CLI loaded MCP servers ({mcp!r}); none are allowed"
    return None


def _overage(info: Any) -> bool:
    if not isinstance(info, Mapping):
        return False
    return bool(info.get("isUsingOverage") or info.get("overageInUse"))


def _served_model(result: Mapping[str, Any], init_model: str) -> str:
    usage = result.get("modelUsage")
    if isinstance(usage, Mapping) and usage:
        # The model that wrote the most output tokens answered the request.
        best = max(
            usage.items(),
            key=lambda kv: _int(kv[1], "outputTokens") if isinstance(kv[1], Mapping) else 0,
        )
        return str(best[0])
    return init_model


def _parse_result(line: Mapping[str, Any], init_model: str, overage: bool) -> CliResult:
    usage = line.get("usage") if isinstance(line.get("usage"), Mapping) else {}
    try:
        cost = float(line.get("total_cost_usd") or 0.0)
    except (TypeError, ValueError):
        cost = 0.0
    return CliResult(
        structured=line.get("structured_output"),
        model=_served_model(line, init_model),
        input_tokens=_int(usage, "input_tokens"),
        output_tokens=_int(usage, "output_tokens"),
        cache_read_tokens=_int(usage, "cache_read_input_tokens"),
        cache_creation_tokens=_int(usage, "cache_creation_input_tokens"),
        total_cost_usd=cost,
        overage=overage,
        is_error=bool(line.get("is_error")),
        subtype=str(line.get("subtype") or ""),
        raw_usage=dict(usage),
    )


def run(
    *,
    binary: str,
    cwd: Path,
    model: str,
    system_prompt: str,
    schema: Mapping[str, Any],
    user_message: str,
    effort: str | None,
    on_overage: Callable[[], bool],
    timeout: float = TIMEOUT_S,
    environ: Mapping[str, str] | None = None,
    max_budget_usd: str = MAX_BUDGET_USD,
) -> CliResult:
    """Run one call and return its result. Raises :class:`CliFailure` on anything else.

    ``on_overage`` is called once, when the stream first reports paid extra usage; returning
    False kills the child (the apply cap is reached). ``user_message`` is neutralized first
    (:func:`neutralize`), so nothing in it can make the CLI attach a file or run a command.
    """
    user_message = neutralize(user_message)
    cwd = Path(cwd)
    leftovers = sorted(p.name for p in cwd.iterdir()) if cwd.is_dir() else None
    if leftovers is None:
        raise CliFailure(f"the CLI working directory {cwd} does not exist")
    if leftovers:
        raise CliFailure(
            f"the CLI working directory {cwd} is not empty ({', '.join(leftovers[:3])}); "
            "empty it and try again"
        )
    argv = build_argv(
        binary,
        model=model,
        system_prompt=system_prompt,
        schema=schema,
        effort=effort,
        max_budget_usd=max_budget_usd,
    )
    try:
        proc = _spawn(
            argv,
            cwd=str(cwd),
            env=child_env(environ),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        raise CliFailure("the claude command is not installed") from exc

    lines: queue.Queue[bytes | None] = queue.Queue()
    err_tail: list[bytes] = []

    def feed() -> None:
        try:
            assert proc.stdin is not None
            proc.stdin.write(user_message.encode("utf-8"))
            proc.stdin.close()
        except (BrokenPipeError, OSError, ValueError):
            pass  # the child exited (or was killed) before reading everything

    def read_out() -> None:
        assert proc.stdout is not None
        try:
            for raw in proc.stdout:
                lines.put(raw)
        except (OSError, ValueError):
            pass
        lines.put(None)

    def read_err() -> None:
        assert proc.stderr is not None
        try:
            for raw in proc.stderr:
                err_tail.append(raw)
                del err_tail[:-20]
        except (OSError, ValueError):
            pass

    threads = [threading.Thread(target=f, daemon=True) for f in (feed, read_out, read_err)]
    for t in threads:
        t.start()

    def stderr_text() -> str:
        text = b"".join(err_tail).decode("utf-8", "replace").strip()
        return text[-400:]

    deadline = time.monotonic() + timeout
    init_model = ""
    seen_init = False
    overage = False
    result: CliResult | None = None
    partial = CliResult(model="")

    def fail(reason: str, **kw: Any) -> CliFailure:
        return CliFailure(
            reason,
            overage=overage,
            started=seen_init,
            partial=partial if seen_init else None,
            **kw,
        )

    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _kill(proc)
                raise fail(f"the CLI timed out after {int(timeout)} s", killed=True)
            try:
                raw = lines.get(timeout=min(remaining, 1.0))
            except queue.Empty:
                continue
            if raw is None:
                break
            try:
                msg = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                continue  # not a stream-json line (a stray warning); ignore it
            if not isinstance(msg, dict):
                continue
            kind = msg.get("type")
            if kind == "system" and msg.get("subtype") == "init":
                reason = check_init(msg)
                if reason is not None:
                    _kill(proc)
                    raise CliFailure(reason, turn_off=True, killed=True)
                seen_init = True
                init_model = str(msg.get("model") or "")
                partial.model = init_model
                continue
            if kind == "system":
                continue
            if not seen_init:
                # Anything before the init line means we could not check it first.
                _kill(proc)
                raise CliFailure(
                    "the CLI did not start with an init line; nothing was checked",
                    turn_off=True,
                    killed=True,
                )
            if kind == "rate_limit_event":
                if not overage and _overage(msg.get("rate_limit_info")):
                    overage = True
                    if not on_overage():
                        _kill(proc)
                        raise fail(
                            "the subscription is on paid extra usage and this run was not "
                            "confirmed as paid, or the apply daily cap is reached; the call "
                            "was stopped",
                            killed=True,
                        )
                continue
            if kind == "assistant":
                usage = (msg.get("message") or {}).get("usage")
                if isinstance(usage, Mapping):
                    partial.input_tokens = max(partial.input_tokens, _int(usage, "input_tokens"))
                    partial.output_tokens += _int(usage, "output_tokens")
                    partial.cache_read_tokens = max(
                        partial.cache_read_tokens, _int(usage, "cache_read_input_tokens")
                    )
                continue
            if kind == "result":
                result = _parse_result(msg, init_model, overage)
                continue
        try:
            code = proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            _kill(proc)
            code = proc.returncode
    except BaseException:
        _kill(proc)
        raise
    finally:
        for t in threads:
            t.join(timeout=2)

    if result is None:
        detail = stderr_text()
        raise fail(
            f"the CLI exited with code {code} and no result" + (f": {detail}" if detail else "")
        )
    if code != 0:
        raise CliFailure(f"the CLI exited with code {code}", result=result)
    if result.is_error:
        raise CliFailure(f"the CLI reported an error ({result.subtype or 'error'})", result=result)
    if result.structured is None:
        raise CliFailure("the CLI returned no structured output", result=result)
    return result
