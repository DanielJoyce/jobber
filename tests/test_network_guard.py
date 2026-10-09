from __future__ import annotations

import socket

import httpx
import pytest


def test_real_network_is_blocked():
    with pytest.raises(RuntimeError, match="mark it `live`"):
        socket.getaddrinfo("openrouter.ai", 443)
    with pytest.raises((RuntimeError, httpx.ConnectError)):
        httpx.get("https://openrouter.ai/api/v1/models", timeout=2)


def test_loopback_is_allowed():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    cli = socket.socket()
    try:
        cli.connect(srv.getsockname())
    finally:
        cli.close()
        srv.close()


@pytest.mark.live
def test_live_tests_skip_without_opt_in():
    pytest.fail("must never run without -m live and JOBHUNTER_LIVE_TESTS=1")
