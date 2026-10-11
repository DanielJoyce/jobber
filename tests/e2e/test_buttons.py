"""Primary buttons keep >= 4.5:1 text contrast at rest, on hover and on focus, in both themes."""

from __future__ import annotations

import re

import pytest

from .test_links import CONTRAST_JS

pytestmark = pytest.mark.e2e

PRIMARY = ".primary:not(:disabled):not([disabled])"


def _packet_url(page, server) -> str:
    page.goto(f"{server.url}/job/{server.ids['b2']}")
    page.keyboard.press("p")
    page.wait_for_url(re.compile(r"/packet/\d+$"))
    return page.url


# "toggle-X": the OS prefers the opposite scheme and the theme button switches to X, so the
# [data-theme=X] token block (not the media query) supplies the colors.
@pytest.mark.parametrize("scheme", ["light", "dark", "toggle-dark", "toggle-light"])
@pytest.mark.parametrize("which", ["prefs", "new_packet", "packet"])
def test_primary_buttons_meet_text_contrast_in_every_state(
    page, server, fake_claude, scheme, which
):
    toggled = scheme.startswith("toggle-")
    target = scheme.removeprefix("toggle-")
    os_scheme = {"dark": "light", "light": "dark"}[target] if toggled else scheme
    page.emulate_media(color_scheme=os_scheme)
    url = {
        "prefs": f"{server.url}/prefs",
        "new_packet": f"{server.url}/apply/new",
        "packet": None,
    }[which]
    page.goto(url or _packet_url(page, server))
    if toggled:
        page.click("#theme-toggle")
        assert page.get_attribute("html", "data-theme") == target
    buttons = page.locator(f"{PRIMARY}, .tile-ranked")
    n = buttons.count()
    assert n > 0, f"no primary buttons found on {which}"
    measured = 0
    for i in range(n):
        b = buttons.nth(i)
        if not b.is_visible():
            continue
        measured += 1
        label = b.inner_text().strip() or b.get_attribute("id")
        b.scroll_into_view_if_needed()
        assert b.evaluate(CONTRAST_JS) >= 4.5, (which, scheme, "rest", label)
        b.hover()
        assert b.evaluate(CONTRAST_JS) >= 4.5, (which, scheme, "hover", label)
        page.mouse.move(0, 0)
        b.focus()
        assert b.evaluate(CONTRAST_JS) >= 4.5, (which, scheme, "focus", label)
        b.blur()
    assert measured > 0
    assert not page.errors
