"""Assisted apply phase 1a in a browser: p and n keys, New packet, the packet page (specs/017)."""

from __future__ import annotations

import json
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


# ─── phase 1b: documents ────────────────────────────────────────────────────

E2E_OUT = {
    "resume": {
        "header": ["L1"],
        "summary": {"text": "Runs synthetic hosts.", "sources": ["L2"]},
        "sections": [
            {
                "heading": "Experience",
                "entries": [
                    {
                        "source_line": "L1",
                        "employer": "",
                        "title": "",
                        "dates": "",
                        "bullets": [{"text": "Led the synthetic hosts", "sources": ["L2"]}],
                    }
                ],
            }
        ],
        "skills": [],
        "omitted": [],
        "change_notes": ["Kept it short."],
    }
}


def _new_packet(page, server):
    page.goto(f"{server.url}/apply/new")
    page.fill("#new-text", TEXT)
    page.fill("#new-employer", "Synthetic Co")
    page.fill("#new-title", "Fleet Engineer")
    page.click("#create-btn")
    page.wait_for_url(re.compile(r"/packet/\d+$"))


def test_generate_confirm_and_mark_ready_in_a_browser(page, server, fake_claude, claude_stream):
    fake_claude.set([claude_stream(E2E_OUT), claude_stream({"lines": []})])
    _new_packet(page, server)
    page.click("#resume button[data-runner=cli]")
    page.wait_for_url(re.compile(r"/packet/\d+#resume$"))
    expect(page.locator("#resume-editor .chk.bad").first).to_be_visible()
    expect(page.locator("#mark-ready")).to_be_disabled()
    page.click("#resume-editor .confirm-btn")
    page.wait_for_load_state()
    expect(page.locator("#mark-ready")).to_be_enabled()
    page.click("#mark-ready")
    expect(page.locator("#ready .chk.ok")).to_have_text("ready")
    assert [r[0] for r in server.rows("SELECT status FROM application_packet")] == ["ready"]
    assert not page.errors


def test_api_button_asks_first_and_dismissing_runs_nothing(page, server, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-synthetic")
    server.app.state.packet_api_client_factory = lambda: pytest.fail("dismissed: no API call")
    _new_packet(page, server)
    seen = []

    def on_dialog(d):
        seen.append(d.message)
        d.dismiss()

    page.on("dialog", on_dialog)
    page.click("#resume button[data-runner=api]")
    page.wait_for_timeout(300)
    assert seen and "Run on the API? About $" in seen[0]
    assert server.rows("SELECT count(*) FROM packet_document")[0][0] == 0
    assert not page.errors


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_documents_fit_a_phone_and_badges_are_readable(
    browser, server, fake_claude, claude_stream, scheme
):
    fake_claude.set([claude_stream(E2E_OUT), claude_stream({"lines": []})])
    ctx = browser.new_context(viewport={"width": 375, "height": 800}, color_scheme=scheme)
    ctx.route("**/*", lambda r: r.continue_() if "127.0.0.1" in r.request.url else r.abort())
    pg = ctx.new_page()
    try:
        _new_packet(pg, server)
        pg.click("#resume button[data-runner=cli]")
        pg.wait_for_url(re.compile(r"#resume$"))
        overflow = pg.evaluate(
            "document.documentElement.scrollWidth - document.documentElement.clientWidth"
        )
        assert overflow <= 1, f"packet documents scroll sideways by {overflow}px"
        for sel in ("#resume-editor .chk.bad", "#resume-editor .src", ".reasons li", "#use-base"):
            assert pg.locator(sel).first.evaluate(CONTRAST_JS) >= 4.5, sel
    finally:
        ctx.close()


def test_enter_in_an_editor_box_saves_and_never_confirms(page, server, fake_claude, claude_stream):
    out = json.loads(json.dumps(E2E_OUT))
    out["resume"]["sections"][0]["entries"][0]["bullets"].append(
        {"text": "Ran synthetic hosts", "sources": ["L2"]}
    )
    fake_claude.set([claude_stream(out), claude_stream({"lines": []})])
    _new_packet(page, server)
    page.click("#resume button[data-runner=cli]")
    page.wait_for_url(re.compile(r"#resume$"))
    box = page.locator('#resume-editor input[name="b.0.1.sources"]')
    box.fill("L2, L1")
    box.press("Enter")
    page.wait_for_load_state()
    rows = server.rows("SELECT version, origin, check_report FROM packet_document ORDER BY version")
    assert [(r[0], r[1]) for r in rows] == [(1, "generated"), (2, "edited")]
    report = json.loads(rows[-1][2])
    assert all(i["status"] != "confirmed" for i in report["items"])
    assert not page.errors


def test_the_paid_button_sends_its_confirmation_and_a_second_click_runs_nothing(
    page, server, fake_claude, claude_stream
):
    from jobhunter.apply import runner_state

    runner_state.set_overage(server.app.state.settings.paths.data_dir)
    slow = claude_stream(E2E_OUT, overage=True)
    slow["steps"].insert(1, {"sleep": 1.5})
    fake_claude.set([slow, claude_stream({"lines": []})])
    _new_packet(page, server)
    page.on("dialog", lambda d: d.accept())
    btn = page.locator("#resume button[data-runner=cli]")
    assert btn.get_attribute("data-paid") == "1"
    posts = []
    page.on(
        "request",
        lambda r: posts.append(r.post_data) if r.method == "POST" and "generate" in r.url else None,
    )
    # Click, then click again while the run is in flight (the fake takes 1.5 s).
    disabled = page.evaluate(
        """async () => {
          const b = document.querySelector('#resume button[data-runner=cli]');
          b.click();
          await new Promise(r => setTimeout(r, 100));
          const off = b.disabled;
          b.click();
          return off;
        }"""
    )
    assert disabled is True
    page.wait_for_url(re.compile(r"#resume$"), timeout=15000)
    assert len(posts) == 1 and "confirm_paid=1" in posts[0]
    assert len(fake_claude.calls()) == 2  # the run and its confirmed entailment, once
    assert server.rows("SELECT count(*) FROM packet_document")[0][0] == 1
    assert not page.errors
