"""Inbox keyboard triage in a real browser."""

from __future__ import annotations

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.e2e


def selected_gid(page) -> int:
    sel = page.locator("article.row.selected")
    expect(sel).to_have_count(1)
    return int(sel.get_attribute("data-gid"))


def visible_gids(page) -> list[int]:
    return page.evaluate(
        "Array.from(document.querySelectorAll('.inbox article.row')).filter(r => {"
        " const d = r.closest('details'); return !r.classList.contains('triaged') &&"
        " (!d || d.open); }).map(r => Number(r.dataset.gid))"
    )


@pytest.fixture
def inbox(page, server):
    page.goto(f"{server.url}/inbox")
    page.wait_for_selector("article.row.selected")
    return page


def test_j_k_move_selection(inbox):
    order = visible_gids(inbox)
    assert len(order) >= 5
    assert selected_gid(inbox) == order[0]
    inbox.keyboard.press("j")
    assert selected_gid(inbox) == order[1]
    inbox.keyboard.press("j")
    assert selected_gid(inbox) == order[2]
    inbox.keyboard.press("k")
    assert selected_gid(inbox) == order[1]
    inbox.keyboard.press("k")
    inbox.keyboard.press("k")  # clamps at the top
    assert selected_gid(inbox) == order[0]


def test_s_shortlists(inbox, server):
    order = visible_gids(inbox)
    gid = selected_gid(inbox)
    inbox.keyboard.press("s")
    triaged = inbox.locator(f"#row-{gid}")
    expect(triaged).to_have_class("row triaged")
    expect(triaged).to_contain_text("Shortlisted")
    assert selected_gid(inbox) == order[1]  # selection moved on
    assert server.rows("SELECT label FROM label WHERE job_group_id = ?", (gid,))[0][0] == (
        "interesting"
    )
    app = server.rows("SELECT status FROM application WHERE job_group_id = ?", (gid,))
    assert [r[0] for r in app] == ["interested"]


def test_x_dismisses(inbox, server):
    gid = selected_gid(inbox)
    inbox.keyboard.press("x")
    expect(inbox.locator(f"#row-{gid}")).to_contain_text("Dismissed")
    assert server.rows("SELECT label FROM label WHERE job_group_id = ?", (gid,))[0][0] == (
        "not_interesting"
    )
    assert not server.rows("SELECT 1 FROM application WHERE job_group_id = ?", (gid,))


def test_u_undoes(inbox, server):
    gid = selected_gid(inbox)
    inbox.keyboard.press("s")
    expect(inbox.locator(f"#row-{gid}")).to_have_class("row triaged")
    inbox.keyboard.press("u")
    expect(inbox.locator(f"article#row-{gid}")).to_be_visible()
    expect(inbox.locator(f"#row-{gid}.selected")).to_have_count(1)
    assert not server.rows("SELECT 1 FROM label WHERE job_group_id = ?", (gid,))
    assert not server.rows("SELECT 1 FROM application WHERE job_group_id = ?", (gid,))


def test_number_keys_jump_to_bucket(inbox):
    inbox.keyboard.press("2")
    gid_b = selected_gid(inbox)
    assert inbox.locator(f"#bucket-B #row-{gid_b}").count() == 1
    inbox.keyboard.press("1")
    gid_a = selected_gid(inbox)
    assert inbox.locator(f"#bucket-A #row-{gid_a}").count() == 1
    # "6" is bucket F: it opens the collapsed section and selects its first row
    inbox.keyboard.press("6")
    gid_f = selected_gid(inbox)
    assert inbox.locator(f"#bucket-F #row-{gid_f}").count() == 1
    assert inbox.locator("#stale-details").evaluate("d => d.open")


def test_f_toggles_bucket_f(inbox):
    details = inbox.locator("#stale-details")
    assert details.evaluate("d => d.open") is False
    inbox.keyboard.press("f")
    assert details.evaluate("d => d.open") is True
    inbox.keyboard.press("f")
    assert details.evaluate("d => d.open") is False


def test_typing_in_input_does_not_trigger_shortcuts(inbox, server):
    inbox.evaluate(
        "const i = document.createElement('input'); i.id = 'probe';"
        " document.querySelector('.inbox').prepend(i); i.focus();"
    )
    before = selected_gid(inbox)
    inbox.keyboard.type("sjxu12f")
    assert inbox.input_value("#probe") == "sjxu12f"
    assert selected_gid(inbox) == before
    assert not server.rows("SELECT 1 FROM label")
    assert inbox.locator("#stale-details").evaluate("d => d.open") is False
