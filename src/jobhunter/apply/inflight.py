"""In-flight guards for drafting runs (specs/017 "Spend cap").

A run takes 40-180 s. Without a guard a double click, a second tab, or ``jobhunter apply
draft`` beside the console would start a second run (twice the subscription quota, or two API
runs that both pass the cap check because neither has logged its spend yet).

- :func:`claim` is a non-blocking per-packet-and-kind lock: a second Generate for the same
  document while one runs is refused, not queued.
- :func:`spend_section` is a blocking lock around every "check the apply cap, call, log the
  spend" section on the API runner, so the cap check and the logged spend are atomic
  together.

Both are ``flock`` locks on files under ``<data dir>/locks/``, so they hold across the console
and the CLI command; the kernel drops them if a process dies.
"""

from __future__ import annotations

import fcntl
import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class Busy(RuntimeError):
    """Another run of the same document is in progress."""


def _lock_file(data_dir: Path, name: str) -> Path:
    d = Path(data_dir).expanduser() / "locks"
    d.mkdir(parents=True, exist_ok=True)
    return d / (re.sub(r"[^A-Za-z0-9_.-]", "_", name)[:120] + ".lock")


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
