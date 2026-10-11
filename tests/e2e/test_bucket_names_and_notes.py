"""Bucket names only (no letter chips anywhere) and the cover-letter notes layout (e55771a)."""

from __future__ import annotations

import re

import pytest

from jobhunter.core.bucketnames import BUCKET_TITLES

pytestmark = pytest.mark.e2e

NAMES = [t[0].title() for t in BUCKET_TITLES.values()]
# A bucket name followed by a lone capital letter A-G (a chip), e.g. "Strong B" or "Strong (B)".
LETTER_AFTER_NAME = re.compile(r"\b(?:" + "|".join(NAMES) + r")\s*\(?\b[A-G]\b\)?(?![\w'-])")


def _pages(page, server):
    page.goto(f"{server.url}/job/{server.ids['b2']}")
    page.keyboard.press("p")
    page.wait_for_url(re.compile(r"/packet/\d+$"))
    yield page.url
    for path in (
        f"/job/{server.ids['b2']}",
        "/inbox",
        "/prefs",
        "/captured",
        "/",
        "/search",
        "/pipeline",
    ):
        yield f"{server.url}{path}"


def test_no_page_shows_a_standalone_bucket_letter_next_to_a_bucket_name(page, server, fake_claude):
    seen_names = 0
    for url in _pages(page, server):
        page.goto(url)
        page.wait_for_load_state("networkidle")
        text = page.inner_text("body")
        seen_names += sum(n in text for n in NAMES)
        assert not LETTER_AFTER_NAME.search(text), (url, LETTER_AFTER_NAME.search(text).group(0))
        assert page.locator(".bucket-badge, .bucket-letter").count() == 0, url
    assert seen_names > 0, "the pages should show bucket names at all"


@pytest.mark.parametrize("width", [1400, 375])
def test_cover_letter_notes_textarea_and_button_sit_left_under_the_label(
    browser, server, fake_claude, width
):
    ctx = browser.new_context(viewport={"width": width, "height": 900})
    ctx.route("**/*", lambda r: r.continue_() if "127.0.0.1" in r.request.url else r.abort())
    pg = ctx.new_page()
    try:
        pg.goto(f"{server.url}/job/{server.ids['b2']}")
        pg.keyboard.press("p")
        pg.wait_for_url(re.compile(r"/packet/\d+$"))
        label = pg.locator("label[for=notes]").bounding_box()
        area = pg.locator("#notes").bounding_box()
        btn = pg.locator("#save-notes").bounding_box()
        assert abs(area["x"] - label["x"]) <= 1, "textarea starts at the label's left edge"
        assert abs(btn["x"] - label["x"]) <= 1, "button starts at the label's left edge"
        assert area["y"] >= label["y"] + label["height"] - 1, "textarea is under the label"
        assert area["width"] >= label["width"] - 2, "textarea is as wide as the label text"
        assert area["x"] + area["width"] <= width + 1
    finally:
        ctx.close()
