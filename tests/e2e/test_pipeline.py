"""Pipeline board keyboard moves, HTMX out-of-band swaps and the drawer."""

from __future__ import annotations

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.e2e


def app_id(server, name: str) -> int:
    return server.rows("SELECT id FROM application WHERE job_group_id = ?", (server.ids[name],))[0][
        0
    ]


def events(server, aid: int) -> list[str]:
    rows = server.rows(
        "SELECT status FROM application_event WHERE application_id = ? ORDER BY id", (aid,)
    )
    return [r[0] for r in rows]


def focus_card(page, aid: int) -> None:
    page.locator(f"#card-{aid} .pcard-employer").click()
    expect(page.locator(f"#card-{aid}")).to_have_class("pcard focused")


def test_bracket_moves_card_right_and_back(page, server):
    aid = app_id(server, "p_applied")
    page.goto(f"{server.url}/pipeline")
    expect(page.locator("#col-applied #count-applied")).to_have_text("1")
    expect(page.locator("#count-acknowledged")).to_have_text("0")
    before = events(server, aid)

    focus_card(page, aid)
    page.keyboard.press("]")
    expect(page.locator(f"#cards-acknowledged #card-{aid}")).to_have_count(1)
    expect(page.locator(f"#cards-applied #card-{aid}")).to_have_count(0)
    expect(page.locator("#count-applied")).to_have_text("0")
    expect(page.locator("#count-acknowledged")).to_have_text("1")
    expect(page.locator(f"#card-{aid}")).to_have_class("pcard focused")  # focus follows the card
    assert events(server, aid) == [*before, "acknowledged"]
    assert server.rows("SELECT status FROM application WHERE id = ?", (aid,))[0][0] == (
        "acknowledged"
    )

    page.keyboard.press("[")
    expect(page.locator(f"#cards-applied #card-{aid}")).to_have_count(1)
    expect(page.locator("#count-applied")).to_have_text("1")
    expect(page.locator("#count-acknowledged")).to_have_text("0")
    assert events(server, aid) == [*before, "acknowledged", "applied"]


def test_enter_opens_drawer_escape_closes(page, server):
    aid = app_id(server, "p_preparing")
    page.goto(f"{server.url}/pipeline")
    focus_card(page, aid)
    page.keyboard.press("Enter")
    drawer = page.locator("#drawer")
    expect(drawer).not_to_be_empty()
    expect(drawer).to_contain_text("Pipe Preparing")
    page.keyboard.press("Escape")
    expect(drawer).to_be_empty()
