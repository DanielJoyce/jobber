"""Console Restart (specs/019 Addendum B "Restart", S12b).

``POST /config/restart`` (in ``config_routes``) checks the new files in a child process, takes
the work registry (``console/work.py``), answers 202 and asks uvicorn to shut down. The
``console`` command then calls :func:`after_serve`, which waits for leftover work and either:

- **re-execs** in place (host, run by hand or under any supervisor): the same interpreter and
  arguments with the environment as it was *before* ``.env`` files and ``*_FILE`` were loaded
  (:data:`PRISTINE_ENV`), so the new process reads ``config.toml``, ``models.toml`` and every
  ``.env`` afresh; or
- **exits 42** (container, unit opted in with ``JOBHUNTER_RESTART_EXIT``): the unit's
  ``SuccessExitStatus=42`` and ``RestartForceExitStatus=42`` make systemd start a fresh
  container (and a fresh config mount) without firing ``OnFailure=``.

A container whose unit has not opted in gets no button: exiting 0 would leave it down
(``Restart=on-failure``) and any other code would send a failure notification.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from jobhunter import container

logger = logging.getLogger(__name__)

EXIT_RESTART = 42  # nothing else uses it: uvicorn 3, click 1 and 2, --wait 75
RESTART_EXIT_VAR = "JOBHUNTER_RESTART_EXIT"
# The unit lines (base unit and preflight's drop-in carry exactly these).
UNIT_CONTAINER_LINES = (f"Environment={RESTART_EXIT_VAR}={EXIT_RESTART}",)
UNIT_SERVICE_LINES = (
    f"SuccessExitStatus={EXIT_RESTART}",
    f"RestartForceExitStatus={EXIT_RESTART}",
)
# Host drain: longer than the drafting timeout (cli_runner.TIMEOUT_S, 180 s). Container: the
# health check can kill a container 60 s after the listener closes, so stay well under that.
HOST_DRAIN_S = 200
CONTAINER_DRAIN_S = 30
PREFLIGHT_TIMEOUT_S = 60
GIVE_UP_S = 90  # the 202 page stops polling after this

EXEC, EXIT = "exec", "exit"

# os.environ as it was at the very top of the CLI callback, before .env and *_FILE loading.
PRISTINE_ENV: dict[str, str] | None = None


def snapshot_environ() -> None:
    """Called first thing in the CLI callback, before anything loads a ``.env`` file."""
    global PRISTINE_ENV
    PRISTINE_ENV = dict(os.environ)


def pristine_env() -> dict[str, str]:
    return dict(PRISTINE_ENV) if PRISTINE_ENV is not None else dict(os.environ)


def unit_opted_in(env: Mapping[str, str] | None = None) -> bool:
    env = os.environ if env is None else env
    return bool(env.get(RESTART_EXIT_VAR, "").strip())


def kind(env: Mapping[str, str] | None = None) -> str | None:
    """How this console restarts: :data:`EXEC` on the host, :data:`EXIT` in an opted-in
    container, None when it cannot restart itself."""
    if not container.is_container():
        return EXEC
    return EXIT if unit_opted_in(env) else None


def drain_seconds() -> int:
    return CONTAINER_DRAIN_S if container.is_container() else HOST_DRAIN_S


# --- the child preflight -------------------------------------------------------------------

# Runs in a fresh interpreter with the pristine environment: the new code, the new .env files,
# the new config. It prints one JSON line; its lines are value-free (config_watch.load_errors).
PREFLIGHT_CODE = """\
import json, sys
def say(ok, lines=()):
    sys.stdout.write("\\n" + json.dumps({"ok": ok, "lines": list(lines)}) + "\\n")
    sys.stdout.flush()
try:
    import jobhunter.console.app  # noqa: F401  the code on disk imports
    from jobhunter import config_watch, secrets
    from jobhunter.config import load_env_files, load_settings
except ModuleNotFoundError as exc:
    say(False, ["missing module %s; run uv sync" % (exc.name or "?")])
    sys.exit(1)
except BaseException as exc:
    say(False, ["the console code could not be imported (%s)" % type(exc).__name__])
    sys.exit(1)
def load():
    secrets.startup(load_settings, load_env_files, lambda line: None)
    return load_settings()
settings, lines = config_watch.load_errors(load)
if settings is None:
    say(False, lines)
    sys.exit(1)
say(True)
"""

Runner = Callable[..., Any]


def run_preflight(
    env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
    run: Runner = subprocess.run,
) -> list[str]:
    """Check the files a restarted console would read; ``[]`` when it would start. The lines
    never carry a value: they come from the child's JSON line, never from its stderr."""
    try:
        out = run(
            [sys.executable, "-c", PREFLIGHT_CODE],
            env=dict(pristine_env() if env is None else env),
            cwd=str(cwd or Path.cwd()),
            timeout=PREFLIGHT_TIMEOUT_S,
            capture_output=True,
            text=True,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return [f"the check did not finish in {PREFLIGHT_TIMEOUT_S} s"]
    except OSError as exc:
        return [f"the check could not start ({type(exc).__name__})"]
    data: dict[str, Any] = {}
    for line in reversed((out.stdout or "").splitlines()):
        if line.strip():
            try:
                parsed = json.loads(line)
            except ValueError:
                parsed = None
            if isinstance(parsed, dict):
                data = parsed
            break
    if out.returncode == 0 and data.get("ok") is True:
        return []
    lines = [str(x) for x in data.get("lines") or []][:20]
    return lines or [f"the check failed (exit {out.returncode})"]


# --- after the server stops ----------------------------------------------------------------


def after_serve(
    *,
    how: str,
    leftover: Callable[[float], list[str]],
    drain: float,
    execve: Callable[..., Any] | None = None,
    exit_: Callable[[int], Any] | None = None,
) -> None:
    """Join what is left of the registered work (bounded), then re-exec or exit 42. Looked up
    at call time so tests can stub ``os.execve``, ``os._exit`` and ``logging.shutdown``."""
    execve = execve or os.execve
    exit_ = exit_ or os._exit
    left = leftover(drain)
    if left:
        logger.warning("console restart: still running after %ss: %s", drain, ", ".join(left))
    logger.warning("console restart: %s", "re-exec" if how == EXEC else f"exit {EXIT_RESTART}")
    logging.shutdown()
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(OSError, ValueError):
            stream.flush()
    if how == EXIT:
        # os._exit, not sys.exit: interpreter shutdown would wait on non-daemon workers.
        exit_(EXIT_RESTART)
        return
    argv = [sys.executable, *sys.orig_argv[1:]]
    execve(sys.executable, argv, pristine_env())
