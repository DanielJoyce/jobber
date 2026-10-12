"""/setup at phone width (no horizontal scroll) and in dark mode (rows readable)."""

from __future__ import annotations

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.e2e


def test_setup_has_no_horizontal_scroll_at_375px(page, server):
    page.set_viewport_size({"width": 375, "height": 800})
    page.goto(f"{server.url}/setup?all=1")
    expect(page.locator(".su-row").first).to_be_visible()
    page.wait_for_timeout(500)  # let the activity bar load
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


def test_setup_dark_mode_rows_use_dark_surface(page, server):
    page.emulate_media(color_scheme="dark")
    page.goto(f"{server.url}/setup")
    row = page.locator(".su-row").first
    expect(row).to_be_visible()
    bg = row.evaluate("el => getComputedStyle(el).backgroundColor")
    assert bg == "rgb(26, 26, 25)"  # --surface-1 in dark


def test_refused_resume_upload_shows_its_error_with_js_on(page, server, tmp_path):
    page.goto(f"{server.url}/setup?all=1")
    pdf = tmp_path / "cv.pdf"
    pdf.write_bytes(b"%PDF-1.4 synthetic")
    page.locator('#row-profile-resume input[type="file"]').set_input_files(str(pdf))
    page.locator('#row-profile-resume button[type="submit"]').click()
    expect(page.locator("#row-profile-resume .su-error")).to_contain_text(
        "Can't read that file type"
    )
