"""/models Save + re-score in a browser (specs/020 M5): the re-run intent survives a preview
in flight, the queued re-score question still offers it, and the panel is themed. Nothing is
confirmed, so nothing runs."""

from __future__ import annotations

import sqlite3
import time

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.e2e


def _rescore_rows(server) -> int:
    return server.rows("SELECT count(*) FROM rescore_request WHERE status != 'pending'")[0][0]


def test_rerun_click_while_a_preview_is_in_flight_still_reruns(page, server):
    posts: list[str] = []
    page.on(
        "request",
        lambda r: (
            posts.append(r.post_data or "")
            if r.method == "POST" and r.url.endswith("/models/screen")
            else None
        ),
    )

    def slow_preview(route):
        time.sleep(1.0)  # the preview is still in flight when the save is clicked
        route.continue_()

    page.route("**/models/screen/preview", slow_preview)
    page.goto(f"{server.url}/models")
    row = page.locator("#models-row-screen")
    row.locator("#models-screen-other").fill("openrouter:test/other")
    # Clicking blurs the input: its change starts the preview, and the save queues behind it.
    row.locator(".mo-rerun-button").click()
    expect(row.locator(".mo-msg")).to_be_visible(timeout=10000)
    assert posts and "rerun=1" in posts[0], posts
    # Not cached, so it asks "save anyway" first; the intent is kept for that answer.
    expect(row.locator("input.mo-rerun-intent")).to_have_value("1")
    page.unroute("**/models/screen/preview")
    row.locator(".mo-rerun-button").click()  # Save anyway + re-score
    expect(row.locator("#rescore-panel #rescore-heading")).to_contain_text("openrouter:test/other")
    assert server.app.state.settings.scoring.screen_scorer == "openrouter:test/other"
    assert _rescore_rows(server) == 0
    assert not page.errors


def test_rerun_with_a_queued_rescore_present(page, server):
    c = sqlite3.connect(server.db_path)
    c.execute(
        "INSERT INTO rescore_request (requested_at, scope, scoring_version, job_count, "
        "cost_per_job_usd, estimated_usd) VALUES ('2026-10-10T01:21:00Z', 'recent', 'v', 3, "
        "0.002, 0.006)"
    )
    c.commit()
    c.close()
    page.goto(f"{server.url}/models")
    row = page.locator("#models-row-screen")
    row.locator("#models-screen-other").fill("openrouter:test/other")
    row.locator("#models-screen-other").press("Tab")  # change: the preview with the queued line
    expect(row.locator(".mo-queued")).to_contain_text("queued re-score")
    expect(row.locator(".mo-rerun-button")).to_have_count(2)
    clicked: list[str] = []
    for _ in range(3):  # a "save anyway" question may come first; the queued one follows
        if row.locator("#rescore-form").count():
            break
        btn = row.locator(".mo-rerun-button").first
        clicked.append(btn.inner_text())
        btn.click()
        expect(row.locator(".mo-msg")).to_be_visible()
        page.wait_for_timeout(300)
    expect(row.locator("#rescore-form")).to_be_visible()
    assert any("queued re-scores use the new model" in t for t in clicked), clicked
    expect(row.locator("#rescore-queued-elsewhere")).to_contain_text("Prefs")
    assert _rescore_rows(server) == 0
    assert not page.errors


def test_rerun_panel_is_themed_in_dark_mode_and_cancel_runs_nothing(page, server):
    page.emulate_media(color_scheme="dark")
    dialogs: list[str] = []
    page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
    page.goto(f"{server.url}/models")
    row = page.locator("#models-row-screen")
    row.locator(".mo-rerun-button").click()  # no change: saves nothing, opens the panel
    select = row.locator("#rescore-form select[name=scope]")
    expect(select).to_be_visible()
    assert select.evaluate("e => getComputedStyle(e).backgroundColor") == "rgb(13, 13, 13)"
    assert select.evaluate("e => getComputedStyle(e).color") == "rgb(255, 255, 255)"
    run = row.locator("#rescore-run")
    run.evaluate("e => { e.disabled = true; }")
    assert run.evaluate("e => getComputedStyle(e).cursor") == "not-allowed"
    assert run.evaluate("e => getComputedStyle(e).opacity") == "0.6"
    # A scope change replans without a dialog (hx-disinherit), on the /models route.
    select.select_option("all")
    expect(row.locator("#rescore-form select[name=scope]")).to_have_value("all")
    assert dialogs == []
    run = row.locator("#rescore-run")
    if run.is_enabled():
        run.click()  # the dialog is dismissed: Cancel
        page.wait_for_timeout(300)
        assert dialogs and "This spends credits." in dialogs[0]
    assert _rescore_rows(server) == 0
    assert server.rows("SELECT count(*) FROM rescore_request")[0][0] == 0
    assert not page.errors
