"""In-flight guards for drafting runs (specs/017 "Spend cap").

A run takes 40-180 s. Without a guard a double click, a second tab, or ``jobhunter apply
draft`` beside the console would start a second run (twice the subscription quota, or two API
runs that both pass the cap check because neither has logged its spend yet).

- :func:`claim` is a non-blocking per-packet-and-kind lock: a second Generate for the same
  document while one runs is refused, not queued.
- :func:`spend_section` is a blocking lock around every "check the apply cap, call, log the
  spend" section on the API runner, so the cap check and the logged spend are atomic
  together.
- :func:`probe` is a blocking lock (with a deadline) around every CLI call that is not a
  confirmed paid run. While no paid-extra-usage hold is set, such a call may turn out to be on
  paid extra usage, and the spec accepts that **one** call. Two of them at once (two documents
  drafted side by side) would both be charged before either reports it, so they run one at a
  time and each re-reads the hold after the one before it.

All are ``flock`` locks on files under ``<data dir>/locks/``, so they hold across the console
and the CLI command; the kernel drops them if a process dies.
"""

from __future__ import annotations

import fcntl
import hashlib
import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

MAX_NAME = 120
PROBE_WAIT_S = 240.0  # longer than one CLI call's timeout (cli_runner.TIMEOUT_S, 180 s)


class Busy(RuntimeError):
    """Another run of the same document is in progress."""


def _lock_file(data_dir: Path, name: str) -> Path:
    d = Path(data_dir).expanduser() / "locks"
    d.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", name)
    if safe != name or len(safe) > MAX_NAME:
        # Keys that differ only past the cut, or only in replaced characters, stay distinct.
        digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:16]
        safe = f"{safe[: MAX_NAME - 17]}-{digest}"
    return d / (safe + ".lock")


@contextmanager
def claim(data_dir: Path, key: str) -> Iterator[None]:
    with open(_lock_file(data_dir, f"apply-{key}"), "a+") as fh:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Busy("a draft of this document is already running; wait for it") from exc
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


@contextmanager
def spend_section(data_dir: Path) -> Iterator[None]:
    with open(_lock_file(data_dir, "apply-spend"), "a+") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


@contextmanager
def probe(data_dir: Path, wait_s: float | None = None, poll_s: float = 0.25) -> Iterator[None]:
    """One unconfirmed CLI call at a time; waits up to ``wait_s`` for the one running."""
    with open(_lock_file(data_dir, "apply-cli-probe"), "a+") as fh:
        deadline = time.monotonic() + (PROBE_WAIT_S if wait_s is None else wait_s)
        while True:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    raise Busy(
                        "another subscription draft is still running; try again when it finishes"
                    ) from exc
                time.sleep(poll_s)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
