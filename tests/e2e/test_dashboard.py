"""Today dashboard: map, toggles, localStorage restore, theme, and console cleanliness."""

from __future__ import annotations

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.e2e

PATHS = "#map svg g.targets path"


def fills(page) -> list[str]:
    return page.eval_on_selector_all(PATHS, "ns => ns.map(n => n.getAttribute('fill'))")


@pytest.fixture
def dash(page, server):
    page.goto(f"{server.url}/")
    page.wait_for_selector(PATHS)
    return page


def test_map_renders_51_state_paths(dash):
    expect(dash.locator(PATHS)).to_have_count(51)


def test_metric_switch_updates_fills(dash):
    before = fills(dash)
    with dash.expect_response(lambda r: "/api/dash/map" in r.url and "median_salary" in r.url):
        dash.select_option("#map-metric", "median_salary")
    dash.wait_for_function(
        "before => Array.from(document.querySelectorAll('#map svg g.targets path'))"
        ".map(n => n.getAttribute('fill')).join() !== before.join()",
        arg=before,
    )
    assert fills(dash) != before
    assert "metric=median_salary" in dash.url


def test_shape_grid_toggle_persists_across_reload(dash):
    expect(dash.locator(PATHS)).to_have_count(51)
    dash.locator("#map-view label", has_text="Grid").click()
    expect(dash.locator("#map svg g.targets rect.cell").first).to_be_visible()
    expect(dash.locator(PATHS)).to_have_count(0)
    assert dash.evaluate("localStorage.getItem('jh-dash-view')") == "grid"
    dash.reload()
    dash.wait_for_selector("#map svg g.targets rect.cell")
    expect(dash.locator('input[name="map-view"][value="grid"]')).to_be_checked()
    dash.locator("#map-view label", has_text="Shape").click()
    expect(dash.locator(PATHS)).to_have_count(51)


def test_range_buttons_swap_kpi_fragment(dash):
    kpis = dash.locator("#kpis")
    expect(kpis).to_contain_text("vs previous 7d")
    with dash.expect_response(lambda r: "/dash/kpis" in r.url and "range=30" in r.url):
        dash.locator("#dash-controls label", has_text="30d").click()
    expect(kpis).to_contain_text("vs previous 30d")
    expect(kpis).not_to_contain_text("vs previous 7d")


def test_theme_toggle_sets_attribute_and_persists(dash):
    html = dash.locator("html")
    dash.click("#theme-toggle")
    expect(html).to_have_attribute("data-theme", "dark")
    assert dash.evaluate("localStorage.getItem('jh-theme')") == "dark"
    dash.reload()
    expect(html).to_have_attribute("data-theme", "dark")
    dash.click("#theme-toggle")
    expect(html).to_have_attribute("data-theme", "light")


def test_no_console_errors_on_any_page(page, server):
    paths = [
        "/",
        "/inbox",
        "/pipeline",
        "/followups",
        "/sources",
        "/rejected",
        "/search",
        "/costs",
        "/prefs",
        "/alerts",
        f"/job/{server.ids['live']}",
        f"/job/{server.ids['a1']}",
        f"/pipeline/{server.rows('SELECT id FROM application LIMIT 1')[0][0]}",
    ]
    for path in paths:
        page.goto(f"{server.url}{path}", wait_until="networkidle")
        assert page.errors == [], f"console errors on {path}: {page.errors}"
