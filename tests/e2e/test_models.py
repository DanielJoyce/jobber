"""/models in a browser (specs/020 M4): phone width, dark mode, and two saves in a row from
one page view (the digest swap)."""

from __future__ import annotations

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.e2e


def test_models_has_no_horizontal_scroll_at_375px(page, server):
    page.set_viewport_size({"width": 375, "height": 800})
    page.goto(f"{server.url}/models")
    expect(page.locator("#models-row-screen")).to_be_visible()
    page.wait_for_timeout(500)  # let the activity bar load
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


def test_models_dark_mode_rows_use_dark_surface(page, server):
    page.emulate_media(color_scheme="dark")
    page.goto(f"{server.url}/models")
    for sel in ("#models-row-deep", "#models-access-anthropic"):
        el = page.locator(sel)
        expect(el).to_be_visible()
        bg = el.evaluate("el => getComputedStyle(el).backgroundColor")
        assert bg == "rgb(26, 26, 25)"  # --surface-1 in dark
        color = el.evaluate("el => getComputedStyle(el).color")
        assert color != "rgb(11, 11, 11)"  # not light-mode text on a dark surface


def test_two_saves_in_a_row_then_revert(page, server):
    page.goto(f"{server.url}/models")
    page.select_option("#models-effort", "high")
    page.locator("#models-row-drafting button.mo-save").click()
    expect(page.locator("#models-row-drafting")).to_contain_text("Saved at")
    # A second row from the same page view: the swapped digest means no 409.
    page.select_option("#models-deep", "anthropic:claude-sonnet-5-5")
    expect(page.locator("#models-deep-preview")).to_contain_text("a job")
    page.locator("#models-row-deep button.mo-save").click()
    expect(page.locator("#models-row-deep")).to_contain_text("Saved at")
    settings = server.app.state.settings
    assert settings.apply.effort == "high"
    assert settings.scoring.deep_scorer == "anthropic:claude-sonnet-5-5"
    expect(page.locator("#models-history li")).to_have_count(2)
    page.locator("#models-history li").first.get_by_role("button", name="Revert").click()
    expect(page.locator("#models-history")).to_contain_text("Reverted at")
    assert server.app.state.settings.scoring.deep_scorer == "anthropic:claude-opus-5-5"
