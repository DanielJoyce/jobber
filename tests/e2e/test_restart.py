"""Console Restart in a browser (specs/019 Addendum B, S12b): the bar's button, the GET waiting
page (no old bar, no re-post on reload), the return once the "new process" answers, and the
give-up text. Nothing restarts: the shutdown stub swaps the boot id, as a new process would."""

from __future__ import annotations

import threading

import pytest
from playwright.sync_api import expect

from jobhunter import config_watch as cw
from jobhunter.console import restart as rs

pytestmark = pytest.mark.e2e


def arm(server, tmp_path, *, comes_back: bool):
    app = server.app
    cfg = tmp_path / "watched-config.toml"
    cfg.write_text("[fetch]\nglobal_concurrency = 3\n")  # a restart-class change
    app.state.config_watch = cw.ConfigWatcher(
        config_path=lambda: cfg, env_candidates=lambda: [], container_mode=False, start_env={}
    )
    app.state.restart_preflight = lambda **kw: []
    app.state.restart_delay = 0
    stops: list[bool] = []

    def shutdown() -> None:
        stops.append(True)
        if comes_back:

            def new_process() -> None:  # what a re-exec'd console looks like from outside
                app.state.boot_id = "feedfacefeedface"
                app.state.work.cancel_restart()

            threading.Timer(1.5, new_process).start()

    app.state.restart_shutdown = shutdown
    return stops


def test_restart_from_the_bar_waits_on_a_get_page_and_comes_back(page, server, tmp_path):
    stops = arm(server, tmp_path, comes_back=True)
    page.goto(f"{server.url}/inbox")
    button = page.locator('#activity form[action="/config/restart"] button')
    expect(button).to_be_visible()
    button.click()
    page.wait_for_url("**/config/restarting?**")
    assert "next=%2Finbox" in page.url
    expect(page.locator(".restart-waiting")).to_be_visible()
    assert page.locator("#activity").count() == 0  # no old Apply/Restart buttons
    page.reload()  # a GET: no second restart
    assert stops == [True]
    page.wait_for_url(f"{server.url}/inbox", timeout=15000)
    assert not page.errors, page.errors


def test_the_waiting_page_gives_up_without_the_old_bar(page, server, tmp_path, monkeypatch):
    monkeypatch.setattr(rs, "GIVE_UP_S", 2)
    arm(server, tmp_path, comes_back=False)
    page.goto(f"{server.url}/config/changes")
    page.locator("#cfg-restart button").click()
    page.wait_for_url("**/config/restarting?**")
    gave_up = page.locator(".restart-gave-up")
    expect(gave_up).to_be_visible(timeout=10000)
    expect(gave_up).to_contain_text("did not come back")
    expect(page.locator(".restart-waiting")).to_be_hidden()
    assert page.locator("#activity").count() == 0
    assert page.locator('form[action="/config/restart"], form[action="/config/apply"]').count() == 0
