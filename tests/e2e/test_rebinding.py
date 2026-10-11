"""DNS rebinding in a real Chromium (specs/017 phase 1d, 018 C1).

``rebind.test`` resolves to 127.0.0.1 inside this browser only (--host-resolver-rules; every
other name fails to resolve, so nothing leaves the machine). A page at
http://rebind.test:PORT is a different origin from the console but reaches the same socket:
its GETs carry ``Host: rebind.test:PORT`` and no Fetch Metadata, and must be refused.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e

RULES = "--host-resolver-rules=MAP rebind.test 127.0.0.1, MAP * ~NOTFOUND, EXCLUDE 127.0.0.1"


@pytest.fixture
def rebinding_browser(browser):
    b = browser.browser_type.launch(args=[RULES])
    yield b
    b.close()


def test_a_rebinding_page_cannot_read_the_console(rebinding_browser, server):
    port = server.url.rsplit(":", 1)[1]
    pg = rebinding_browser.new_page()
    r = pg.goto(f"http://rebind.test:{port}/prefs")
    assert r is not None and r.status == 403
    assert "not a loopback address" in pg.content() and "Preferences" not in pg.content()
    # the same page's script fetches are refused too
    status = pg.evaluate("fetch('/inbox').then(r => r.status)")
    assert status == 403
    # and the console itself, under its own name, still answers
    ok = pg.goto(f"{server.url}/prefs")
    assert ok is not None and ok.status == 200
