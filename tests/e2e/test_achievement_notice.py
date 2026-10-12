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


def test_x_during_a_pending_bulk_keeps_the_next_selection(page, server):
    """A bulk action remembers the row to select once its request finishes (inbox.js
    htmx:afterRequest). The x's request (hx-swap none: htmx fires afterRequest, not afterSwap)
    finishing first must not consume it and jump the selection early."""
    page.goto(f"{server.url}/inbox")
    page.wait_for_selector("article.row.selected")
    page.wait_for_timeout(300)  # backfill
    capture(server)
    refresh_band(page)
    expect(page.locator(NOTICE)).to_be_visible()
    order = page.evaluate(
        "Array.from(document.querySelectorAll('.inbox article.row')).filter(r => {"
        " const d = r.closest('details'); return !r.classList.contains('triaged') &&"
        " (!d || d.open); }).map(r => r.dataset.gid)"
    )
    a, b, after = order[0], order[1], order[2]
    assert page.locator("article.row.selected").get_attribute("data-gid") == a
    page.locator(f"#row-{a} input.sel").check()
    page.locator(f"#row-{b} input.sel").check()
    held = []
    page.route("**/inbox/bulk", lambda route: held.append(route))
    page.locator("#bulk-bar").get_by_role("button", name="Dismiss").click()
    while not held:
        page.wait_for_timeout(50)
    with page.expect_response(lambda r: r.url.endswith("/achievements/seen")):
        page.locator(".ach-dismiss").click()
    page.wait_for_timeout(300)  # the seen response and the band refresh settle
    # Still pending: had the x's afterRequest consumed it, the selection would have moved.
    assert page.locator("article.row.selected").get_attribute("data-gid") == a
    held[0].continue_()
    expect(page.locator(f"#row-{a}")).to_have_class("row triaged")
    expect(page.locator(f"#row-{after}.selected")).to_have_count(1)
    assert page.errors == []


def test_rescore_dismiss_and_achievement_dismiss_leave_each_other(page, server):
    settle(page, server)
    capture(server)
    write(
        server,
        "INSERT INTO rescore_request (requested_at, scope, scoring_version, job_count, "
        "cost_per_job_usd, estimated_usd, status, done_at, scored, cost_usd) "
        "VALUES (strftime('%Y-%m-%dT%H:%M:%S+00:00','now'), 'recent', 'v', 1, 0, 0, 'done', "
        "strftime('%Y-%m-%dT%H:%M:%S+00:00','now'), 1, 0.01)",
    )
    refresh_band(page)
    done = page.locator("#activity .activity-done")
    expect(done).to_be_visible()
    expect(page.locator(NOTICE)).to_be_visible()
    # The achievement x hides the notice at once, even beside another item.
    with page.expect_response(lambda r: r.url.endswith("/achievements/seen")):
        page.locator(".ach-dismiss").click()
    expect(page.locator(NOTICE)).to_have_count(0)
    expect(done).to_be_visible()
    # And the re-score x keeps an achievement notice.
    capture(server, 1)
    write(
        server,
        "INSERT INTO alert_signup (source_key, subscribed_at, updated_at) VALUES "
        "('k', datetime('now') || '+00:00', datetime('now') || '+00:00')",
    )
    refresh_band(page)
    expect(page.locator(NOTICE)).to_be_visible()
    page.locator(".activity-dismiss").click()
    expect(done).to_have_count(0)
    expect(page.locator(NOTICE)).to_be_visible()
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
