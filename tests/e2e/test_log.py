"""/log and the Today card at phone width in dark mode (bug be1d232)."""

from __future__ import annotations

import os
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.e2e


def _seed(server) -> None:
    now = datetime.now(UTC)
    c = sqlite3.connect(server.db_path)
    c.execute(
        'INSERT INTO run (started_at, ended_at, stages, exit_status, counts, "trigger") '
        "VALUES (?, ?, 'list,score', 'ok', '{\"list\": {\"stubs\": 9}}', 'timer')",
        (
            (now - timedelta(hours=3)).isoformat(),
            (now - timedelta(hours=3, minutes=-4)).isoformat(),
        ),
    )
    c.execute(
        "INSERT INTO rescore_request (requested_at, scope, scoring_version, job_count, "
        "cost_per_job_usd, estimated_usd, status, scorer, errored, error) "
        "VALUES (?, 'recent', 'v', 3, 0, 0, 'failed', 'openrouter:test/model', 3, "
        "'provider said no for <you>@example.com')",
        ((now - timedelta(hours=2)).isoformat(),),
    )
    c.commit()
    c.close()


def test_log_at_phone_width_in_dark_mode(page, server):
    _seed(server)
    page.set_viewport_size({"width": 375, "height": 800})
    page.emulate_media(color_scheme="dark")
    page.goto(f"{server.url}/log")
    expect(page.get_by_text("Run (timer): list,score")).to_be_visible()
    expect(page.get_by_text("provider said no")).to_be_visible()
    assert "@example.com" not in page.inner_text("body")
    page.wait_for_timeout(500)  # let the activity bar load
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    bg = page.evaluate("getComputedStyle(document.body).backgroundColor")
    assert bg != "rgb(249, 249, 247)"  # not the light page colour
    shot = os.environ.get("JH_SHOT_DIR")
    if shot:
        page.screenshot(path=f"{shot}/log-375-dark.png", full_page=True)
    page.get_by_role("link", name="Re-scores").click()
    expect(page).to_have_url(f"{server.url}/log?kind=rescore")
    expect(page.get_by_text("Run (timer)")).to_have_count(0)
    assert page.errors == []


def test_today_card_at_phone_width_in_dark_mode(page, server):
    _seed(server)
    page.set_viewport_size({"width": 375, "height": 800})
    page.emulate_media(color_scheme="dark")
    page.goto(f"{server.url}/")
    card = page.locator("#last-24h")
    expect(card).to_be_visible()
    expect(card).to_contain_text("Last run")
    expect(card).to_contain_text("Unscored backlog")
    page.wait_for_timeout(500)
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    shot = os.environ.get("JH_SHOT_DIR")
    if shot:
        card.screenshot(path=f"{shot}/today-card-375-dark.png")
    card.get_by_role("link", name="Open the log").click()
    expect(page).to_have_url(f"{server.url}/log")
