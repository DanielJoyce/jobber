"""Test-wide guards.

No test may reach the network: only loopback connections (the e2e server, mocked
transports) are allowed. A test that genuinely needs a real service must be marked
``live`` and is skipped unless JOBHUNTER_LIVE_TESTS=1 is set, so paid endpoints are
never called by an ordinary ``pytest`` run.

Why this exists (note for future maintainers, human or model): the scorers call paid
APIs (OpenRouter/TypeSafe Jev, Anthropic) on the user's own prepaid credits, and the
scrapers hit real government sites under polite rate limits. The user explicitly asked
that tests never spend credits unless deliberately run. Agents run `pytest` constantly,
so a single unmocked call in a test would burn money and hammer sites on every run.
Do not weaken, bypass or delete this guard; mock with httpx.MockTransport instead, or
mark a test `live` if it truly needs the real service.

User files: no test may read the developer's real profile, resume, data or config either.
Those files live in the repo (profile/, resume/, data/, config.toml, .env: all gitignored),
in ~/.config/jobhunter and in ~/.env. Code under test resolves its defaults against the
working directory and the home directory, so an unisolated test silently reads them. That
is how console tests passed locally on the developer's real resume and failed in CI, where
no such file exists. Tests that read the real resume or profile both leaked personal data
into test behavior and made CI and local results diverge, which hid that CI failure
locally. The ``_isolate_user_files`` fixture points HOME at a fresh tmp dir, clears
JOBHUNTER_CONFIG and JOBHUNTER_ENV_FILE, and makes any open of those paths raise. Tests
needing settings files or a resume create them under tmp_path. Do not weaken this guard.
"""

from __future__ import annotations

import builtins
import io
import ipaddress
import os
import socket
import subprocess
from pathlib import Path

import pytest

LIVE_ENV = "JOBHUNTER_LIVE_TESTS"
_LOOPBACK_NAMES = {"localhost", "localhost.localdomain", "ip6-localhost"}


def _is_loopback(host: object) -> bool:
    if not isinstance(host, str):
        return False
    if host in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class NetworkBlocked(RuntimeError):
    """Raised when a test tries to reach a non-loopback host."""


@pytest.fixture(autouse=True)
def _block_network(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    if request.node.get_closest_marker("live"):
        if os.environ.get(LIVE_ENV) != "1":
            pytest.skip(f"live test: set {LIVE_ENV}=1 and run with -m live")
        return

    real_connect = socket.socket.connect
    real_getaddrinfo = socket.getaddrinfo

    def guarded_connect(self: socket.socket, address: object) -> object:
        if self.family == socket.AF_UNIX:
            return real_connect(self, address)
        host = address[0] if isinstance(address, tuple) else address
        if not _is_loopback(host):
            raise NetworkBlocked(f"test tried to connect to {host!r}; mark it `live` if intended")
        return real_connect(self, address)

    def guarded_getaddrinfo(host: object, *args: object, **kwargs: object) -> object:
        if host is not None and not _is_loopback(host):
            raise NetworkBlocked(f"test tried to resolve {host!r}; mark it `live` if intended")
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)


# ─── User-file isolation ────────────────────────────────────────────────────

# Resolved at import time, before any test patches HOME.
_REPO = Path(__file__).resolve().parent.parent
_REAL_HOME = Path(os.path.realpath(os.path.expanduser("~")))
_USER_FILE_NAMES = ("profile", "resume", "data", "config.toml", ".env")


def _main_checkout() -> Path:
    """The primary checkout's root. A git worktree keeps its gitignored profile/, resume/
    and data/ in the main checkout, so those are protected as well."""
    try:
        common = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=_REPO,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return _REPO
    return Path(common).resolve().parent


_PROTECTED = tuple(
    sorted(
        {
            os.path.realpath(root / name)
            for root in {_REPO, _main_checkout()}
            for name in _USER_FILE_NAMES
        }
        | {
            os.path.realpath(_REAL_HOME / ".config" / "jobhunter"),
            os.path.realpath(_REAL_HOME / ".env"),
        }
    )
)
_REAL_IO_OPEN = io.open


class UserFileAccess(RuntimeError):
    """Raised when a test opens one of the developer's real user files."""


def _is_protected(target: str) -> bool:
    real = os.path.realpath(target)
    return any(real == p or real.startswith(p + os.sep) for p in _PROTECTED)


def _guarded_open(file: object, *args: object, **kwargs: object) -> object:
    if isinstance(file, str | bytes | os.PathLike):
        target = os.fsdecode(file)
        if _is_protected(target):
            raise UserFileAccess(
                f"test tried to open the developer's real user file {target!r}; "
                "use a tmp_path copy instead (see tests/conftest.py)"
            )
    return _REAL_IO_OPEN(file, *args, **kwargs)  # type: ignore[call-overload]


@pytest.fixture(autouse=True)
def _isolate_user_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    # A fresh dir outside tmp_path, so it cannot collide with a test's own tmp_path/"home".
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("JOBHUNTER_CONFIG", raising=False)
    monkeypatch.delenv("JOBHUNTER_ENV_FILE", raising=False)
    # Path.open, read_text and read_bytes all reach io.open; builtins.open is a separate
    # name for the same function, so both are patched.
    monkeypatch.setattr(io, "open", _guarded_open)
    monkeypatch.setattr(builtins, "open", _guarded_open)
