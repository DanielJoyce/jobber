"""The help topic list is visible beside the page on desktop and a <details> on a phone."""

from __future__ import annotations

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.e2e

LINK = '.help-side a[href="/help/getting-started"]'


@pytest.mark.parametrize("path", ["/help", "/help/nightly-runs"])
def test_sidebar_topic_link_is_visible_on_desktop(page, server, path):
    page.set_viewport_size({"width": 1280, "height": 800})
    page.goto(f"{server.url}{path}")
    expect(page.locator(LINK)).to_be_visible()
    expect(page.locator(".help-topics")).to_be_hidden()


def test_topic_details_works_on_a_phone_without_page_scroll(page, server):
    page.set_viewport_size({"width": 375, "height": 700})
    page.goto(f"{server.url}/help/nightly-runs")
    expect(page.locator(".help-side")).to_be_hidden()
    nav_link = page.locator('.help-topics a[href="/help/scorers"]')
    expect(nav_link).to_be_hidden()
    page.locator(".help-topics > summary").click()
    expect(nav_link).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


def test_dark_mode_help_page_renders(page, server):
    page.emulate_media(color_scheme="dark")
    page.goto(f"{server.url}/help/nightly-runs")
    expect(page.locator(LINK)).to_be_visible()
