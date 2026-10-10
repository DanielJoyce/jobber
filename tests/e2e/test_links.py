"""Bare links stay readable in dark mode (the browser's default blue is ~1.9:1 on near-black)."""

from __future__ import annotations

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.e2e

CONTRAST_JS = """
(el) => {
  const parse = (s) => s.match(/[\\d.]+/g).map(Number);
  const lin = (c) => { c /= 255; return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4; };
  const lum = ([r, g, b]) => 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);
  let bg = el;
  while (bg) {
    const c = parse(getComputedStyle(bg).backgroundColor);
    if (c.length === 3 || c[3] > 0.99) { var rgb = c.slice(0, 3); break; }
    bg = bg.parentElement;
  }
  const fg = parse(getComputedStyle(el).color).slice(0, 3);
  const [hi, lo] = [lum(fg), lum(rgb)].sort((a, b) => b - a);
  return (hi + 0.05) / (lo + 0.05);
}
"""


def test_shortlist_toast_pipeline_link_is_readable_in_dark_mode(page, server):
    page.emulate_media(color_scheme="dark")
    page.goto(f"{server.url}/inbox")
    page.wait_for_selector("article.row.selected")
    page.keyboard.press("s")
    link = page.get_by_role("link", name="see it in Pipeline")
    expect(link).to_be_visible()
    assert link.evaluate(CONTRAST_JS) >= 4.5
    assert link.evaluate("el => getComputedStyle(el).color") != "rgb(0, 0, 238)"


def test_pipeline_card_job_link_is_readable_in_dark_mode(page, server):
    page.emulate_media(color_scheme="dark")
    page.goto(f"{server.url}/pipeline")
    link = page.locator(".pcard-src a").first
    expect(link).to_be_visible()
    assert link.evaluate(CONTRAST_JS) >= 4.5


def test_links_are_readable_in_light_mode_too(page, server):
    page.goto(f"{server.url}/pipeline")
    assert page.locator(".pcard-src a").first.evaluate(CONTRAST_JS) >= 4.5
