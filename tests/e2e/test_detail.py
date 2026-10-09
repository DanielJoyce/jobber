"""Job detail page: evidence highlight, apply redirect, did-you-apply banner."""

from __future__ import annotations

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.e2e


def test_verified_quote_is_marked(page, server):
    page.goto(f"{server.url}/job/{server.ids['live']}")
    expect(page.locator("mark")).to_have_text("manage Linux servers daily")


def test_A_goes_through_apply_redirect(page, server):
    gid = server.ids["live"]
    page.goto(f"{server.url}/job/{gid}")
    expect(page.locator("#apply-link")).to_have_attribute("href", f"/apply/{gid}")
    requests: list[str] = []
    page.on("request", lambda r: requests.append(r.url))
    with page.expect_response(f"{server.url}/apply/{gid}") as resp:
        page.keyboard.press("A")
    r = resp.value
    assert r.status == 302
    assert r.headers["location"] == f"{server.url}/__employer"
    page.wait_for_url(f"{server.url}/__employer")
    assert f"{server.url}/__employer" in requests
    clicks = server.rows("SELECT outcome FROM apply_click WHERE job_group_id = ?", (gid,))
    assert [c[0] for c in clicks] == ["redirected"]


def test_expired_link_shows_closed_page(page, server):
    page.goto(f"{server.url}/apply/{server.ids['expired']}")
    expect(page.locator("body")).to_contain_text("Expired Link")
    clicks = server.rows("SELECT outcome FROM apply_click")
    assert [c[0] for c in clicks] == ["expired"]


def test_did_you_apply_banner_yes_writes_applied_event(page, server):
    gid = server.ids["live"]
    url = f"{server.url}/job/{gid}"
    page.goto(url)
    expect(page.get_by_text("Did you apply?")).to_have_count(0)
    page.click("#apply-link")
    page.wait_for_url(f"{server.url}/__employer")
    page.goto(url)
    banner = page.locator("#did-apply")
    expect(banner).to_contain_text("Did you apply?")
    banner.get_by_role("button", name="Yes").click()
    expect(page.locator("#did-apply")).to_contain_text("Marked as applied")
    rows = server.rows(
        "SELECT e.status FROM application_event e JOIN application a ON a.id = e.application_id "
        "WHERE a.job_group_id = ? ORDER BY e.id",
        (gid,),
    )
    statuses = [r[0] for r in rows]
    assert statuses[-1] == "applied"
    assert server.rows("SELECT status FROM application WHERE job_group_id = ?", (gid,))[0][0] == (
        "applied"
    )
    page.reload()
    expect(page.get_by_text("Did you apply?")).to_have_count(0)
