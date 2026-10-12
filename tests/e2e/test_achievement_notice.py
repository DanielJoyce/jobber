"""The achievement notice in the activity band, in a real browser (specs/021 ticket A4)."""

from __future__ import annotations

import sqlite3

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.e2e

NOTICE = "#activity .ach-notice"


def write(server, sql, *args):
    c = sqlite3.connect(server.db_path)
    c.execute(sql, args)
    c.commit()
    c.close()


def capture(server, n=0):
    write(
        server,
        "INSERT INTO capture_log (action_id, route, captured_at, host, outcome) "
        "VALUES (?, '/ext/capture', '2026-10-10T19:00:00+00:00', 'example.com', 'added')",
        f"e2e{n}",
    )


def refresh_band(page):
    """What a write route's HX-Trigger does: ask the band to reload now."""
    page.evaluate("htmx.trigger(document.body, 'jh-activity')")


def settle(page, server):
    """Load a page and let the first poll do the silent backfill."""
    page.goto(f"{server.url}/help")
    page.wait_for_function(
        "document.querySelector('#activity') && "
        "document.querySelector('#activity').getAttribute('hx-trigger').includes('every')"
    )
    expect(page.locator(NOTICE)).to_have_count(0)


def test_notice_shows_announces_once_and_dismisses(page, server):
    settle(page, server)
    capture(server)
    refresh_band(page)
    notice = page.locator(NOTICE)
    expect(notice).to_be_visible()
    expect(notice).to_contain_text("Achievement: Found it yourself")
    announce = page.locator("#ach-announce")
    expect(announce).to_have_text("Achievement: Found it yourself")
    # Further polls re-render the same notice; it is not announced again.
    page.evaluate("document.getElementById('ach-announce').textContent = ''")
    refresh_band(page)
    page.wait_for_timeout(400)
    refresh_band(page)
    page.wait_for_timeout(400)
    expect(notice).to_be_visible()
    expect(announce).to_have_text("")
    # Nor after navigating in the same tab.
    page.goto(f"{server.url}/help")
    expect(page.locator(NOTICE)).to_be_visible()
    page.wait_for_timeout(300)
    expect(page.locator("#ach-announce")).to_have_text("")

    page.evaluate("sessionStorage.setItem('jh-activity-dismissed', 'keep')")
    with page.expect_response(lambda r: r.url.endswith("/achievements/seen")) as resp:
        page.locator(".ach-dismiss").click()
    assert resp.value.status == 204
    expect(page.locator(NOTICE)).to_have_count(0)  # the band refreshed through /activity
    expect(page.locator("#activity")).to_be_hidden()  # nothing else to show: band hidden
    assert page.evaluate("sessionStorage.getItem('jh-activity-dismissed')") == "keep"
    rows = server.rows("SELECT seen_at FROM achievement_unlock WHERE seen_at IS NULL")
    assert rows == []
    page.reload()
    page.wait_for_timeout(500)
    expect(page.locator(NOTICE)).to_have_count(0)
    assert page.errors == []


def test_dismiss_in_one_tab_clears_the_other(page, server):
    settle(page, server)
    capture(server)
    refresh_band(page)
    expect(page.locator(NOTICE)).to_be_visible()
    other = page.context.new_page()
    other.goto(f"{server.url}/help")
    expect(other.locator(NOTICE)).to_be_visible()
    page.locator(".ach-dismiss").click()
    expect(page.locator(NOTICE)).to_have_count(0)
    refresh_band(other)  # its next poll
    expect(other.locator(NOTICE)).to_have_count(0)
    other.close()


def test_notice_on_inbox_keeps_the_selection(page, server):
    page.goto(f"{server.url}/inbox")
    page.wait_for_selector("article.row.selected")
    page.wait_for_timeout(300)  # backfill
    capture(server)
    refresh_band(page)
    expect(page.locator(NOTICE)).to_be_visible()
    page.keyboard.press("j")
    selected = page.locator("article.row.selected").get_attribute("data-gid")
    page.locator(".ach-dismiss").click()
    expect(page.locator(NOTICE)).to_have_count(0)
    expect(page.locator("article.row.selected")).to_have_count(1)
    assert page.locator("article.row.selected").get_attribute("data-gid") == selected
    page.keyboard.press("j")  # keys still work
    assert page.locator("article.row.selected").get_attribute("data-gid") != selected
    assert page.errors == []


def test_notice_at_phone_width_in_dark_mode(page, server):
    page.set_viewport_size({"width": 375, "height": 700})
    page.emulate_media(color_scheme="dark", reduced_motion="reduce")
    settle(page, server)
    capture(server)
    refresh_band(page)
    expect(page.locator(NOTICE)).to_be_visible()
    width = page.evaluate("document.documentElement.scrollWidth")
    assert width <= 375
    animation = page.evaluate(
        "getComputedStyle(document.querySelector('.ach-notice')).animationName"
    )
    assert animation == "none"
