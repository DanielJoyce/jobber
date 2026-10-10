"""/prefs Application answers and the packet answers section in a real browser (specs/017 1d):
phone width, dark mode, the live preview and a save. Network blocked except the console."""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import expect

from .test_links import CONTRAST_JS

pytestmark = pytest.mark.e2e


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_answers_sections_fit_a_phone_and_read_in_both_themes(browser, server, scheme):
    ctx = browser.new_context(viewport={"width": 375, "height": 800}, color_scheme=scheme)
    ctx.route("**/*", lambda r: r.continue_() if "127.0.0.1" in r.request.url else r.abort())
    pg = ctx.new_page()
    try:
        pg.goto(f"{server.url}/prefs#sec-answers")
        section = pg.locator("#sec-answers")
        expect(section).to_be_visible()
        pg.click("button[data-help='ans-notice_period']")
        expect(pg.locator("#help-ans-notice_period")).to_be_visible()
        pg.goto(f"{server.url}/job/{server.ids['b1']}")
        pg.click("#prepare-btn")
        pg.wait_for_url(re.compile(r"/packet/\d+$"))
        for path in ("/prefs", pg.url.removeprefix(server.url)):
            pg.goto(f"{server.url}{path}")
            overflow = pg.evaluate(
                "document.documentElement.scrollWidth - document.documentElement.clientWidth"
            )
            assert overflow <= 1, f"{path} scrolls sideways by {overflow}px"
        expect(pg.locator("#checklist")).to_be_visible()
        assert pg.locator("#checklist li").first.evaluate(CONTRAST_JS) >= 4.5
        assert pg.locator("a[href='/prefs#sec-answers']").evaluate(CONTRAST_JS) >= 4.5
    finally:
        ctx.close()


def test_answer_edit_previews_then_saves_into_the_file(page, server):
    page.goto(f"{server.url}/prefs#sec-answers")
    page.fill("input[name='ans.notice_period']", "two weeks")
    expect(page.locator("#preview-answers")).to_contain_text("1 unsaved change")
    page.locator("#prefs-form button[type=submit].primary").click()
    page.wait_for_url(re.compile(r"/prefs\?saved=1"))
    expect(page.locator("input[name='ans.notice_period']")).to_have_value("two weeks")
    assert not page.errors


def test_a_never_store_answer_is_refused_in_the_browser(page, server):
    page.goto(f"{server.url}/prefs#sec-answers")
    page.fill("#ans-custom-new input[name='ans.custom.question']", "Desired salary")
    page.fill("#ans-custom-new textarea[name='ans.custom.answer']", "a lot")
    page.locator("#prefs-form button[type=submit].primary").click()
    expect(page.locator(".field-error", has_text="never-store list")).to_be_visible()
    page.goto(f"{server.url}/prefs")
    expect(page.locator("input[name='ans.custom.question'][value='Desired salary']")).to_have_count(
        0
    )
