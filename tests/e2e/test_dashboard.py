"""Today dashboard: map, toggles, localStorage restore, theme, and console cleanliness."""

from __future__ import annotations

import re

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


SANKEY_NODES = "#chart-sankey svg.sankey .snode-rect"


def test_sankey_renders_nodes_links_and_table(dash):
    dash.wait_for_selector(SANKEY_NODES)
    assert dash.locator(SANKEY_NODES).count() >= 5
    assert dash.locator("#chart-sankey path.slink").count() >= 4
    expect(dash.locator("#chart-sankey .slabel").first).to_be_visible()
    dash.locator("#chart-sankey .sankey-table summary").click()
    cell = dash.locator("#chart-sankey .sankey-table td", has_text="Passed prefilter").first
    expect(cell).to_be_visible()


def test_sankey_node_hover_and_click_navigates(dash):
    dash.wait_for_selector(SANKEY_NODES)
    hit = dash.locator("#chart-sankey .snode a[href^='/pipeline?node='] .hit").first
    hit.hover()
    expect(dash.locator("#chart-sankey .chart-tip")).to_be_visible()
    hit.click()
    dash.wait_for_url("**/pipeline?node=*")
    expect(dash.locator("#node-filter")).to_be_visible()


def test_every_sankey_link_loads_a_page(dash, server):
    # Request each href the chart draws: the Sankey once linked to a page that did not exist.
    dash.wait_for_selector(SANKEY_NODES)
    hrefs = dash.eval_on_selector_all(
        "#chart-sankey .snode a", "as => [...new Set(as.map(a => a.getAttribute('href')))]"
    )
    assert any(h.startswith("/pipeline?node=") for h in hrefs)
    assert any(h.startswith("/inbox?bucket=") for h in hrefs)
    for href in hrefs:
        resp = dash.request.get(f"{server.url}{href}")
        assert resp.status == 200, href


def test_sankey_dark_mode_keeps_marks_and_label_contrast(dash):
    dash.wait_for_selector(SANKEY_NODES)
    n = dash.locator(SANKEY_NODES).count()
    dash.click("#theme-toggle")
    expect(dash.locator("html")).to_have_attribute("data-theme", "dark")
    assert dash.locator(SANKEY_NODES).count() == n
    label = dash.locator("#chart-sankey .slabel").first
    fill = label.evaluate("el => getComputedStyle(el).fill")
    halo = label.evaluate("el => getComputedStyle(el).stroke")
    assert fill != halo
    assert dash.errors == []


def test_sankey_phone_width_has_no_horizontal_scroll(dash):
    dash.set_viewport_size({"width": 360, "height": 800})
    dash.wait_for_function(
        "() => { const s = document.querySelector('#chart-sankey svg.sankey');"
        " return s && s.viewBox.baseVal.width <= 360; }"
    )
    assert dash.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    assert dash.locator(SANKEY_NODES).count() >= 5


# ─── bucket filter ─────────────────────────────────────────────────────────

CHIPS = "#bucket-filter button.chip"


def chip(page, name):
    return page.locator(CHIPS, has_text=name).first


def wa_fill(page):
    return page.get_attribute('#map g.targets path[data-state="WA"]', "fill")


def wa_tip(page) -> dict[str, str]:
    page.focus('#map g.targets path[data-state="WA"]')
    expect(page.locator("#map-tooltip")).to_be_visible()
    return page.evaluate(
        "() => { const o = {}; const dl = document.querySelector('#map-tooltip dl');"
        "dl.querySelectorAll('dt').forEach(dt => { o[dt.textContent.trim()] = "
        "dt.nextElementSibling.textContent; }); return o; }"
    )


def toggle(page, name):
    with page.expect_response(lambda r: "/api/dash/map" in r.url):
        chip(page, name).click()
    page.wait_for_function("() => !document.querySelector('.map-loading')")


def test_bucket_chips_default_to_bullseye_and_strong(dash):
    expect(chip(dash, "Bullseye")).to_have_attribute("aria-pressed", "true")
    expect(chip(dash, "Strong")).to_have_attribute("aria-pressed", "true")
    expect(chip(dash, "Stale Match")).to_have_attribute("aria-pressed", "false")
    expect(chip(dash, "Bullseye")).to_contain_text("3")  # counts from the payload
    assert wa_tip(dash)["New: Bullseye + Strong"] == "3"


def test_toggling_buckets_recolors_map_and_updates_tooltip_url_and_table(dash):
    before = wa_fill(dash)
    toggle(dash, "Bullseye")  # WA: Strong only
    expect(chip(dash, "Bullseye")).to_have_attribute("aria-pressed", "false")
    assert wa_tip(dash)["New: Strong"] == "2"
    assert "buckets=B" in dash.url
    expect(dash.locator('#state-table tr[data-state="WA"] td.num').first).to_have_text("2")
    expect(dash.locator("#map-legend")).to_contain_text("New: Strong")
    toggle(dash, "Stale Match")
    tip = wa_tip(dash)
    assert tip["New: Strong + Stale Match"] == "3"
    assert tip["Stale Match"] == "1" and tip["Strong"] == "2"
    assert "buckets=B%2CF" in dash.url or "buckets=B,F" in dash.url
    # Only Mismatch: WA has none, so its fill drops to the zero step.
    toggle(dash, "Mismatch")
    toggle(dash, "Strong")
    toggle(dash, "Stale Match")
    assert wa_tip(dash)["New: Mismatch"] == "0"
    assert wa_fill(dash) != before


def test_bucket_selection_persists_and_clicking_a_state_filters_the_inbox(dash):
    toggle(dash, "Strong")
    assert dash.evaluate("localStorage.getItem('jh-dash-buckets')") == "A"
    dash.goto(dash.url.split("?")[0])  # no URL param: restored from localStorage
    dash.wait_for_selector(PATHS)
    expect(chip(dash, "Strong")).to_have_attribute("aria-pressed", "false")
    expect(chip(dash, "Bullseye")).to_have_attribute("aria-pressed", "true")
    dash.locator('#map g.targets path[data-state="WA"]').focus()
    dash.keyboard.press("Enter")
    dash.wait_for_url("**/inbox?state=WA&bucket=A")
    expect(dash.locator("section.bucket")).to_have_count(1)


def test_bucket_chips_keyboard_reset_and_all_fits(dash):
    chip(dash, "Stale Match").focus()
    with dash.expect_response(lambda r: "/api/dash/map" in r.url):
        dash.keyboard.press("Space")
    expect(chip(dash, "Stale Match")).to_have_attribute("aria-pressed", "true")
    toggle(dash, "Reset")
    expect(chip(dash, "Stale Match")).to_have_attribute("aria-pressed", "false")
    toggle(dash, "All fits")
    expect(chip(dash, "Lateral")).to_have_attribute("aria-pressed", "true")
    expect(chip(dash, "Mismatch")).to_have_attribute("aria-pressed", "false")
    # The last selected bucket cannot be switched off.
    toggle(dash, "Reset")
    toggle(dash, "Bullseye")
    chip(dash, "Strong").click()
    expect(chip(dash, "Strong")).to_have_attribute("aria-pressed", "true")


def test_bucket_filter_fits_a_phone_in_dark_mode(page, server):
    page.set_viewport_size({"width": 360, "height": 800})
    page.goto(f"{server.url}/?buckets=A,C")
    page.wait_for_selector(PATHS)
    page.click("#theme-toggle")
    expect(page.locator("html")).to_have_attribute("data-theme", "dark")
    expect(chip(page, "Lateral")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


def test_bucket_selection_drives_kpi_and_trend_chart(dash):
    kpi = dash.locator('[data-kpi="new_ab"]')
    both = int(kpi.inner_text())
    expect(dash.locator("#kpis")).to_contain_text("New Bullseye + Strong")
    with dash.expect_response(lambda r: "/dash/kpis" in r.url and "buckets=B" in r.url):
        toggle(dash, "Bullseye")
    expect(dash.locator("#kpis")).to_contain_text("New Strong")
    assert int(kpi.inner_text()) < both
    expect(dash.locator("#chart-line-title")).to_have_text("New Strong per day")
    expect(dash.locator("#chart-funnel")).to_contain_text("Strong")


def test_state_statistics_collapsed_by_default_and_remembered(dash):
    details = dash.locator("#state-details")
    expect(details).not_to_have_attribute("open", "")
    expect(dash.locator("#state-details summary")).to_contain_text(
        re.compile(r"State statistics \(\d+ rows")
    )
    expect(dash.locator("#state-table table")).not_to_be_visible()
    dash.locator("#state-details summary").click()
    expect(dash.locator("#state-table table")).to_be_visible()
    assert dash.evaluate("localStorage.getItem('jh-dash-states')") == "open"
    dash.reload()
    dash.wait_for_selector(PATHS)
    expect(dash.locator("#state-table table")).to_be_visible()


# ─── Sankey labels on real-data shapes ─────────────────────────────────────

# Roughly the shape of the real database: one huge Untriaged node next to tiny Dismissed and
# Shortlisted ones, and a long list of small outcomes.
_REAL_LINKS = {
    ("fetched", "prefiltered_out"): 48,
    ("fetched", "passed"): 2061,
    ("passed", "unscored"): 2,
    ("passed", "bucket_A"): 40,
    ("passed", "bucket_B"): 381,
    ("passed", "bucket_C"): 100,
    ("passed", "bucket_D"): 233,
    ("passed", "bucket_E"): 75,
    ("passed", "bucket_F"): 6,
    ("passed", "bucket_G"): 1224,
    ("bucket_A", "untriaged"): 40,
    ("bucket_B", "untriaged"): 378,
    ("bucket_B", "dismissed"): 1,
    ("bucket_B", "shortlisted"): 2,
    ("bucket_C", "untriaged"): 100,
    ("bucket_D", "untriaged"): 233,
    ("bucket_E", "untriaged"): 75,
    ("bucket_F", "untriaged"): 6,
    ("bucket_G", "untriaged"): 1224,
    ("shortlisted", "not_applied"): 1,
    ("shortlisted", "applied"): 1,
    ("elsewhere", "applied"): 24,
    ("applied", "awaiting"): 18,
    ("applied", "interview"): 1,
    ("applied", "rejected"): 6,
}


def _real_payload() -> str:
    import json

    from jobhunter.console.dashboard import _SANKEY_NODES

    value: dict[str, int] = {}
    for (a, b), n in _REAL_LINKS.items():
        value[b] = value.get(b, 0) + n
        if a in ("fetched", "elsewhere"):
            value[a] = value.get(a, 0) + n
    nodes = [
        {"id": i, "label": label, "col": col, "kind": kind, "href": href, "value": value[i]}
        for i, label, col, kind, href in _SANKEY_NODES
        if value.get(i)
    ]
    links = [{"source": a, "target": b, "value": n} for (a, b), n in _REAL_LINKS.items()]
    return json.dumps({"nodes": nodes, "links": links, "total": 2109 + 24})


OVERLAPS_JS = """
() => {
  const out = [];
  document.querySelectorAll('#chart-sankey svg.sankey').forEach((svg, k) => {
    const box = svg.getBoundingClientRect();
    const labels = [...svg.querySelectorAll('text.slabel')].map(t => ({
      text: t.textContent, r: t.getBoundingClientRect()
    }));
    labels.forEach((a, i) => {
      if (a.r.left < box.left - 1 || a.r.right > box.right + 1) out.push(['outside', k, a.text]);
      labels.slice(i + 1).forEach(b => {
        const w = Math.min(a.r.right, b.r.right) - Math.max(a.r.left, b.r.left);
        const h = Math.min(a.r.bottom, b.r.bottom) - Math.max(a.r.top, b.r.top);
        if (w > 1 && h > 1) out.push([k, a.text, b.text, Math.round(w), Math.round(h)]);
      });
    });
  });
  return out;
}
"""


@pytest.mark.parametrize("width", [360, 390, 700, 1280])
def test_sankey_labels_never_overlap_on_real_data_shapes(page, server, width):
    payload = _real_payload()

    def inject(route):
        resp = route.fetch()
        body = re.sub(
            r'(id="sankey-data">).*?(</script>)',
            lambda m: m.group(1) + payload + m.group(2),
            resp.text(),
            count=1,
            flags=re.S,
        )
        route.fulfill(response=resp, body=body)

    page.route(f"{server.url}/", inject)
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(f"{server.url}/")
    page.wait_for_selector(SANKEY_NODES)
    assert page.locator("#chart-sankey text.slabel", has_text="Untriaged").count() == 1
    assert page.evaluate(OVERLAPS_JS) == []
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
