"""Single-writer run lock shared by host and containers (specs/018-containers.md#run-lock, C5).

``flock(LOCK_EX)`` on one file descriptor of ``<db_path>.run.lock``, held for the whole job.
The lock is keyed on the database, not on a data directory, so every process that would
write the same database (host timers, container timers, the console's Re-score now)
contends for the same lock. ``flock`` works across containers that bind-mount the file on one
kernel and a local filesystem, and the kernel drops it when the holder dies, however it dies.

The holder writes its pid, host, command and start time into the lock file **through the same
fd** (opening and closing a second fd would be harmless for ``flock`` but drops a POSIX
``fcntl`` lock, so nothing here relies on that difference). The metadata is advisory only:
the kernel lock decides who holds it; pids from another container's namespace mean little.

Who waits and who reports (the CLI decides; this module only offers both):

- timer-started jobs pass ``wait`` and retry until it runs out (:class:`LockTimeout`);
- interactive jobs pass no wait and get :class:`LockHeld` at once; they exit 0, nothing spent;
- any job that finds a holder older than :data:`STALE_AFTER` gets :class:`StaleHolder`, so a
  hung job is reported (exit 75) instead of every later job queueing behind it forever.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from jobhunter.xdg import mkdir_private

LOCK_SUFFIX = ".run.lock"
STALE_AFTER = timedelta(hours=6)
EXIT_STALE = 75  # EX_TEMPFAIL: a failure, so systemd's OnFailure= fires
EXIT_TIMEOUT = 75
POLL_S = 5.0
_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*$", re.IGNORECASE)
_UNIT_S = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}


def lock_path(db_path: Path | str) -> Path:
    """``<db_path>.run.lock``, beside the database (same mount, same SELinux label)."""
    return Path(f"{db_path}{LOCK_SUFFIX}")


def parse_duration(text: str) -> float:
    """``90``, ``90s``, ``30m``, ``2h``, ``1d`` to seconds. Raises ValueError otherwise."""
    m = _DURATION_RE.match(text or "")
    if not m:
        raise ValueError(f"not a duration: {text!r} (use e.g. 90s, 30m, 2h)")
    return float(m.group(1)) * _UNIT_S[m.group(2).lower()]


@dataclass(frozen=True)
class Holder:
    """What the current holder wrote into the lock file (advisory)."""

    pid: int | None
    host: str | None
    command: str | None
    started_at: datetime | None
    boot_id: str | None = None
    monotonic: float | None = None  # CLOCK_MONOTONIC at start: excludes suspend

    def age(
        self, now: datetime, monotonic_now: float | None = None, boot_id: str | None = None
    ) -> timedelta | None:
        """How long the holder has run. On the same boot (same kernel, so host and its
        containers) this uses CLOCK_MONOTONIC, which stops during suspend: a job frozen by a
        suspend is not reported as hung on resume. Otherwise wall time."""
        if (
            self.monotonic is not None
            and monotonic_now is not None
            and self.boot_id is not None
            and self.boot_id == boot_id
        ):
            return timedelta(seconds=monotonic_now - self.monotonic)
        return None if self.started_at is None else now - self.started_at

    def describe(self) -> str:
        parts = [
            f"pid {self.pid if self.pid is not None else '?'}",
            f"host {self.host or '?'}",
            f"since {self.started_at.isoformat(timespec='seconds') if self.started_at else '?'}",
        ]
        if self.command:
            parts.insert(0, self.command)
        return ", ".join(parts)


def read_holder(path: Path) -> Holder | None:
    """Parse the metadata in ``path``; None when absent, empty or mid-write."""
    try:
        raw = Path(path).read_text(encoding="utf-8")
        data = json.loads(raw) if raw.strip() else None
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    started = None
    if isinstance(data.get("started_at"), str):
        with contextlib.suppress(ValueError):
            started = datetime.fromisoformat(data["started_at"])
            if started.tzinfo is None:
                started = started.replace(tzinfo=UTC)
    pid = data.get("pid")
    mono = data.get("monotonic")
    return Holder(
        pid=pid if isinstance(pid, int) else None,
        host=data.get("host") if isinstance(data.get("host"), str) else None,
        command=data.get("command") if isinstance(data.get("command"), str) else None,
        started_at=started,
        boot_id=data.get("boot_id") if isinstance(data.get("boot_id"), str) else None,
        monotonic=float(mono) if isinstance(mono, int | float) else None,
    )


def current_boot_id() -> str | None:
    """This kernel's boot id (shared by the host and its containers), or None."""
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip() or None
    except OSError:
        return None


class LockHeld(RuntimeError):
    """Another job holds the run lock."""

    def __init__(self, holder: Holder | None, message: str | None = None) -> None:
        self.holder = holder
        who = holder.describe() if holder else "details unknown"
        super().__init__(message or f"another jobhunter job is running ({who})")


class LockTimeout(LockHeld):
    """Waited the whole ``wait`` and the lock is still held."""


class StaleHolder(LockHeld):
    """The holder started more than :data:`STALE_AFTER` ago: probably hung."""


class RunLock:
    """The run lock for one database. Not reentrant: one acquire per instance."""

    def __init__(
        self,
        db_path: Path | str,
        command: str,
        *,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        boot_id: Callable[[], str | None] = current_boot_id,
    ) -> None:
        self.path = lock_path(db_path)
        self.command = command
        self._clock = clock or (lambda: datetime.now(UTC))
        self._sleep = sleep
        self._monotonic = monotonic
        self._boot_id = boot_id
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def try_acquire(self) -> bool:
        """Take the lock if it is free; never waits. Raises :class:`StaleHolder` if not free
        and the holder is older than :data:`STALE_AFTER`."""
        try:
            self.acquire(None)
        except StaleHolder:
            raise
        except LockHeld:
            return False
        return True

    def acquire(
        self,
        wait: float | None = None,
        on_wait: Callable[[Holder | None], object] | None = None,
    ) -> None:
        """Take the lock, waiting up to ``wait`` seconds (None or 0: do not wait).

        ``on_wait(holder)`` is called once, when the lock is found held and waiting begins,
        so a caller can say what it is waiting for.

        Raises :class:`LockHeld` (no wait), :class:`LockTimeout` (waited ``wait``) or
        :class:`StaleHolder` (holder older than 6 h, checked on every attempt).
        """
        if self._fd is not None:
            raise RuntimeError("run lock already held by this instance")
        mkdir_private(self.path.parent)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
        deadline = None if not wait else self._monotonic() + wait
        told = False
        boot = self._boot_id()
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    pass
                holder = read_holder(self.path)
                age = holder.age(self._clock(), self._monotonic(), boot) if holder else None
                if age is not None and age > STALE_AFTER:
                    hours = age.total_seconds() / 3600
                    raise StaleHolder(
                        holder,
                        f"another jobhunter job has held the run lock for {hours:.1f} h "
                        f"({holder.describe() if holder else '?'}); it may be hung. Check it "
                        f"(journalctl --user, podman ps), stop it if so, then retry",
                    )
                if deadline is None:
                    raise LockHeld(holder)
                remaining = deadline - self._monotonic()
                if remaining <= 0:
                    raise LockTimeout(
                        holder,
                        f"gave up after waiting {wait:.0f} s for the run lock: another "
                        f"jobhunter job is running "
                        f"({holder.describe() if holder else 'details unknown'})",
                    )
                if on_wait is not None and not told:
                    told = True
                    on_wait(holder)
                self._sleep(min(POLL_S, remaining))
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd
        self._write_metadata()

    def _write_metadata(self) -> None:
        assert self._fd is not None
        data = {
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "command": self.command,
            "started_at": self._clock().astimezone(UTC).isoformat(timespec="seconds"),
            "boot_id": self._boot_id(),
            "monotonic": self._monotonic(),
        }
        payload = json.dumps(data).encode()
        with contextlib.suppress(OSError):  # metadata is advisory; the flock is what counts
            os.ftruncate(self._fd, 0)
            os.pwrite(self._fd, payload, 0)

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            with contextlib.suppress(OSError):
                os.ftruncate(fd, 0)  # no stale metadata for the next reader
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def __enter__(self) -> RunLock:
        if self._fd is None:
            self.acquire(None)
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()
