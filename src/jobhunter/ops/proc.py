"""External commands for host operations (``jobhunter upgrade``, ``jobhunter-ctr``; spec 018 C15).

Every ``podman``, ``systemctl`` and ``git`` call these commands make goes through a
:class:`Runner`, so tests can swap in a fake that records the exact argv sequence and scripts
failures, and no test ever starts a real container or touches the user's systemd units.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Result:
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def tail(self, limit: int = 300) -> str:
        """The last line of stderr (else stdout), trimmed, for one-line error messages."""
        for text in (self.stderr, self.stdout):
            lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
            if lines:
                return lines[-1][:limit]
        return ""


class Runner(Protocol):
    def run(
        self,
        argv: Sequence[str],
        *,
        capture: bool = True,
        timeout: float | None = None,
    ) -> Result:
        """Run ``argv``. ``capture=False`` streams output to the terminal (long builds)."""
        ...

    def which(self, name: str) -> str | None: ...


class SubprocessRunner:
    """The real runner. A missing binary or a timeout comes back as a failed :class:`Result`
    (127 or 124), never an exception, so callers handle one failure shape."""

    def run(
        self,
        argv: Sequence[str],
        *,
        capture: bool = True,
        timeout: float | None = None,
    ) -> Result:
        try:
            done = subprocess.run(
                list(argv),
                capture_output=capture,
                text=True,
                errors="replace",
                stdin=subprocess.DEVNULL,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError:
            return Result(127, "", f"{argv[0]}: command not found")
        except subprocess.TimeoutExpired:
            return Result(124, "", f"{argv[0]}: timed out after {timeout:g} s")
        except OSError as exc:
            return Result(126, "", f"{argv[0]}: {type(exc).__name__}")
        return Result(done.returncode, done.stdout or "", done.stderr or "")

    def which(self, name: str) -> str | None:
        return shutil.which(name)
