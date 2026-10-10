"""Assisted apply phase 1a in a browser: p and n keys, New packet, the packet page (specs/017)."""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import expect

from .test_links import CONTRAST_JS

pytestmark = pytest.mark.e2e

TEXT = "Run the synthetic fleet.\n\nWrite Terraform modules and Go tooling."


def test_p_in_inbox_prepares_the_selected_job(page, server):
    page.goto(f"{server.url}/inbox")
    row = page.locator("article.row.selected")
    expect(row).to_have_count(1)
    title = row.locator(".row-title").inner_text()
    page.keyboard.press("p")
    page.wait_for_url(re.compile(r"/packet/\d+$"))
    expect(page.locator("h1")).to_have_text(title)
    expect(page.locator("#open-application")).to_be_visible()
    assert not page.errors


def test_p_on_job_page_prepares_then_opens_the_same_packet(page, server):
    gid = server.ids["b2"]
    page.goto(f"{server.url}/job/{gid}")
    page.keyboard.press("p")
    page.wait_for_url(re.compile(r"/packet/\d+$"))
    first = page.url
    page.goto(f"{server.url}/job/{gid}")
    expect(page.locator("#prepare-link")).to_be_visible()
    page.keyboard.press("p")
    page.wait_for_url(first)
    status = server.rows("SELECT status FROM application WHERE job_group_id = ?", (gid,))
    assert [r[0] for r in status] == ["preparing"]


def test_n_then_new_packet_from_pasted_text(page, server):
    page.goto(f"{server.url}/inbox")
    page.keyboard.press("n")
    page.wait_for_url(f"{server.url}/apply/new")
    page.fill("#new-text", TEXT)
    page.fill("#new-employer", "Synthetic Co")
    page.fill("#new-title", "Fleet Engineer")
    page.click("#create-btn")
    page.wait_for_url(re.compile(r"/packet/\d+$"))
    expect(page.locator("h1")).to_have_text("Fleet Engineer")
    expect(page.locator("#no-url")).to_be_visible()
    expect(page.locator("#score-now")).to_be_visible()  # shown, never pressed: it would spend
    rows = server.rows("SELECT stage, source_key FROM job WHERE title = 'Fleet Engineer'")
    assert [tuple(r) for r in rows] == [("normalized", "paste-manual")]
    assert not page.errors


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_packet_page_fits_a_phone_and_links_are_readable(browser, server, scheme):
    ctx = browser.new_context(viewport={"width": 375, "height": 800}, color_scheme=scheme)
    ctx.route(
        "**/*",
        lambda r: r.continue_() if "127.0.0.1" in r.request.url else r.abort(),
    )
    pg = ctx.new_page()
    try:
        for path in ("/apply/new", None):
            if path is None:
                pg.goto(f"{server.url}/job/{server.ids['b1']}")
                pg.click("#prepare-btn")
                pg.wait_for_url(re.compile(r"/packet/\d+$"))
            else:
                pg.goto(f"{server.url}{path}")
            overflow = pg.evaluate(
                "document.documentElement.scrollWidth - document.documentElement.clientWidth"
            )
            assert overflow <= 1, f"{path or 'packet'} scrolls sideways by {overflow}px"
        link = pg.locator(".crumbs a").nth(1)
        assert link.evaluate(CONTRAST_JS) >= 4.5
        btn = pg.locator("#open-application")
        assert btn.evaluate(CONTRAST_JS) >= 4.5
    finally:
        ctx.close()
