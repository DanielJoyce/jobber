"""No-model-access banners: Hide sticks across reloads and swaps, and a new reason re-shows."""

from __future__ import annotations

import pytest

from jobhunter.config import Scoring

pytestmark = pytest.mark.e2e

BANNER = "[data-model-banner]"


@pytest.fixture
def no_keys(server, tmp_path, monkeypatch):
    for var in ("ANTHROPIC_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    server.app.state.setup_mode = "host"
    server.app.state.setup_environ = {}
    server.app.state.setup_home = tmp_path / "home"
    return server


def test_hide_sticks_on_reload_and_a_new_reason_shows_again(page, no_keys):
    url = f"{no_keys.url}/captured"
    page.goto(url)
    banner = page.locator(BANNER)
    assert banner.count() == 1 and banner.is_visible()
    assert "No OpenRouter key found" in banner.inner_text()
    banner.locator("[data-model-banner-hide]").click()
    assert not banner.is_visible()
    page.reload()
    assert not page.locator(BANNER).first.is_visible()
    # A different reason (another provider) is a different banner: it shows.
    no_keys.app.state.settings.scoring = Scoring(screen_scorer="anthropic:claude-haiku-5-5")
    page.reload()
    shown = page.locator(BANNER)
    assert shown.count() == 1 and shown.is_visible()
    assert "No Anthropic API credential found" in shown.inner_text()


def test_hide_survives_an_htmx_swap_of_the_rescore_panel(page, no_keys):
    page.goto(f"{no_keys.url}/prefs")
    banner = page.locator(f"#rescore-panel {BANNER}")
    assert banner.is_visible()
    banner.locator("[data-model-banner-hide]").click()
    assert not banner.is_visible()
    page.select_option("#rescore-panel select[name=scope]", index=1)
    page.wait_for_load_state("networkidle")
    assert page.locator(f"#rescore-panel {BANNER}").count() == 1
    assert not page.locator(f"#rescore-panel {BANNER}").is_visible()


def test_banner_fits_375px_and_dark(page, no_keys):
    page.set_viewport_size({"width": 375, "height": 800})
    page.emulate_media(color_scheme="dark")
    page.goto(f"{no_keys.url}/captured")
    assert page.locator(BANNER).is_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
