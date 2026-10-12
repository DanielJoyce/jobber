"""Process-wide logging configuration (bug 85e102e).

Called once, from the CLI callback, so the console, ``jobhunter run``, the timers and every
subcommand log the same way. Records go to stderr, which systemd and podman capture in the
journal. Nothing here, and no log call in the code base, may carry secrets, contact details,
resume text or posting text: log ids, counts, durations and source names only.
"""

from __future__ import annotations

import logging
import os
import sys

LEVEL_ENV_VAR = "JOBHUNTER_LOG_LEVEL"
FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
NOISY = ("httpx", "httpcore", "anthropic", "urllib3", "hpack", "asyncio", "PIL", "markdown_it")
HANDLER_MARK = "_jobhunter_handler"


class _StderrHandler(logging.StreamHandler):
    """Writes to whatever ``sys.stderr`` is at emit time, so a swapped stream (tests, a
    redirect) never leaves the handler holding a closed file."""

    def __init__(self) -> None:
        super().__init__()

    @property
    def stream(self):  # type: ignore[override]
        return sys.stderr

    @stream.setter
    def stream(self, value) -> None:
        pass


def parse_level(value: object) -> int | None:
    """A level name (any case) as a logging level, else None."""
    if not isinstance(value, str) or not value.strip():
        return None
    name = value.strip().upper()
    if name == "WARN":
        name = "WARNING"
    return getattr(logging, name) if name in LEVELS else None


# What overrides ``[logging] level`` in this process ("" when nothing does); set by the CLI so
# /config/changes can say an edit to it has no effect.
OVERRIDE = ""


def override_source(verbose: int = 0, quiet: int = 0) -> str:
    """Words for what overrides ``[logging] level``, or "" when nothing does."""
    if verbose or quiet:
        return "the command line (-v/-q)"
    if os.environ.get(LEVEL_ENV_VAR, "").strip():
        return LEVEL_ENV_VAR
    return ""


def resolve_level(*, verbose: int = 0, quiet: int = 0, config_level: str | None = None) -> int:
    """``JOBHUNTER_LOG_LEVEL``, then ``[logging] level``, else INFO; -v steps that toward
    DEBUG and -q toward ERROR."""
    base = parse_level(os.environ.get(LEVEL_ENV_VAR)) or parse_level(config_level)
    base = base or logging.INFO
    if not (verbose or quiet):
        return base
    steps = [logging.DEBUG, logging.INFO, logging.WARNING, logging.ERROR]
    nearest = min(range(len(steps)), key=lambda i: abs(steps[i] - base))
    return steps[max(0, min(len(steps) - 1, nearest - verbose + quiet))]


def configure(level: int = logging.INFO, stream=None) -> None:
    """Install one stderr handler on the root logger (replacing ours from an earlier call),
    quiet the HTTP libraries, and let uvicorn's loggers use that handler. Access logs stay at
    INFO whatever ``level`` is."""
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, HANDLER_MARK, False):
            root.removeHandler(h)
    handler: logging.StreamHandler = logging.StreamHandler(stream) if stream else _StderrHandler()
    handler.setFormatter(logging.Formatter(FORMAT, DATE_FORMAT))
    setattr(handler, HANDLER_MARK, True)
    root.addHandler(handler)
    root.setLevel(level)
    for name in NOISY:
        logging.getLogger(name).setLevel(max(level, logging.WARNING))
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True
    logging.getLogger("uvicorn.access").setLevel(logging.INFO)
    logging.getLogger("uvicorn.error").setLevel(level)


def uvicorn_log_config() -> dict:
    """A ``log_config`` for ``uvicorn.Config`` that leaves handlers to the root logger, so the
    console's request lines share the format and stream of everything else."""
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "loggers": {
            "uvicorn": {"handlers": [], "propagate": True},
            "uvicorn.error": {"handlers": [], "propagate": True},
            "uvicorn.access": {"handlers": [], "propagate": True, "level": "INFO"},
        },
    }


def spec_label(spec: object) -> str:
    """A scorer spec or model id for a log line: itself when it is safe to show, else a short
    stand-in (``config.hidden_label``). A spec can carry a host or a key-shaped segment."""
    from jobhunter.config import hidden_label, shown_value

    text = str(spec)
    return shown_value(text) or hidden_label(text)


class Throttle:
    """Progress lines for a big loop: ``tick`` logs at INFO at most once per ``seconds`` (or
    every ``every`` calls), and at DEBUG otherwise, so a long run shows life without one
    INFO line per item."""

    def __init__(
        self, logger: logging.Logger, *, seconds: float = 30.0, every: int | None = None
    ) -> None:
        import time

        self._time = time.monotonic
        self.logger = logger
        self.seconds = seconds
        self.every = every
        self.n = 0
        self.last = self._time()

    def tick(self, msg: str, *args: object) -> bool:
        self.n += 1
        now = self._time()
        due = now - self.last >= self.seconds or bool(self.every and self.n % self.every == 0)
        if due:
            self.last = now
        self.logger.log(logging.INFO if due else logging.DEBUG, msg, *args)
        return due
