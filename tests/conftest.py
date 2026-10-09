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
"""

from __future__ import annotations

import ipaddress
import os
import socket

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
