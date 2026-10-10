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


# ─── bulk triage ────────────────────────────────────────────────────────────


def check(page, gid):
    page.locator(f"#row-{gid} input.sel").check()


def test_bulk_dismiss_and_undo(inbox, server):
    order = visible_gids(inbox)
    a, b = order[0], order[1]
    expect(inbox.locator("#bulk-actions")).to_be_hidden()
    check(inbox, a)
    check(inbox, b)
    expect(inbox.locator("#bulk-count")).to_have_text("2 selected")
    inbox.locator("#bulk-bar").get_by_role("button", name="Dismiss").click()
    for gid in (a, b):
        expect(inbox.locator(f"#row-{gid}")).to_have_class("row triaged")
        expect(inbox.locator(f"#row-{gid}")).to_contain_text("Dismissed")
    labels = server.rows("SELECT job_group_id, label FROM label ORDER BY job_group_id")
    assert {r[0] for r in labels} == {a, b} and {r[1] for r in labels} == {"not_interesting"}
    expect(inbox.locator("#bulk-actions")).to_be_hidden()
    assert selected_gid(inbox) == order[2]
    inbox.get_by_role("button", name="undo all").click()
    for gid in (a, b):
        expect(inbox.locator(f"article#row-{gid}")).to_be_visible()
    assert not server.rows("SELECT 1 FROM label")
    assert not inbox.errors


def test_bulk_shortlist_button(inbox, server):
    a, b = visible_gids(inbox)[:2]
    check(inbox, a)
    check(inbox, b)
    inbox.locator("#bulk-bar").get_by_role("button", name="Shortlist").click()
    expect(inbox.locator(f"#row-{b}")).to_contain_text("Shortlisted")
    apps = server.rows(
        "SELECT job_group_id FROM application a JOIN label l USING (job_group_id) "
        "WHERE l.label = 'interesting'"
    )
    assert {r[0] for r in apps} == {a, b}


def test_checkbox_label_and_no_navigation(inbox):
    gid = visible_gids(inbox)[1]
    title = inbox.locator(f"#row-{gid} .row-title").inner_text()
    inbox.get_by_label(f"Select {title}").check()
    assert inbox.url.endswith("/inbox")
    expect(inbox.locator(f"#row-{gid} .row-detail")).to_be_hidden()
    expect(inbox.locator("#bulk-count")).to_have_text("1 selected")


def test_select_all_tristate_and_clear(inbox):
    gids = visible_gids(inbox)
    select_all = inbox.locator("#select-all")
    select_all.check()
    expect(inbox.locator("#bulk-count")).to_have_text(f"{len(gids)} selected")
    inbox.locator(f"#row-{gids[0]} input.sel").uncheck()
    assert select_all.evaluate("e => e.indeterminate") is True
    select_all.check()
    assert inbox.locator("input.sel:checked").count() == len(gids)
    inbox.locator("#bulk-bar").get_by_role("button", name="Clear selection").click()
    expect(inbox.locator("input.sel:checked")).to_have_count(0)
    expect(inbox.locator("#bulk-actions")).to_be_hidden()
    assert select_all.evaluate("e => e.indeterminate || e.checked") is False


def test_shift_click_range(inbox):
    gids = visible_gids(inbox)
    inbox.locator(f"#row-{gids[0]} input.sel").click()
    inbox.locator(f"#row-{gids[3]} input.sel").click(modifiers=["Shift"])
    expect(inbox.locator("#bulk-count")).to_have_text("4 selected")
    for g in gids[:4]:
        expect(inbox.locator(f"#row-{g} input.sel")).to_be_checked()
    expect(inbox.locator(f"#row-{gids[4]} input.sel")).not_to_be_checked()


def test_space_toggles_and_s_x_apply_to_selection(inbox, server):
    gids = visible_gids(inbox)
    inbox.keyboard.press(" ")  # row 0 is focused
    inbox.keyboard.press("j")
    inbox.keyboard.press("j")
    inbox.keyboard.press(" ")
    expect(inbox.locator("#bulk-count")).to_have_text("2 selected")
    inbox.keyboard.press("x")  # acts on the selection, not just the focused row
    for g in (gids[0], gids[2]):
        expect(inbox.locator(f"#row-{g}")).to_contain_text("Dismissed")
    expect(inbox.locator(f"article#row-{gids[1]}")).to_be_visible()
    assert len(server.rows("SELECT 1 FROM label")) == 2
    inbox.keyboard.press("u")  # one undo reverts the whole bulk action
    expect(inbox.locator(f"article#row-{gids[0]}")).to_be_visible()
    expect(inbox.locator(f"article#row-{gids[2]}")).to_be_visible()
    assert not server.rows("SELECT 1 FROM label")


# ─── bulk triage: mouse-selected, repeated, accessible ─────────────────────


def labelled(server) -> set[int]:
    return {r[0] for r in server.rows("SELECT job_group_id FROM label")}


def test_keys_work_after_clicking_a_checkbox(inbox, server):
    # Mouse-select, then press a key: focus stays on the checkbox just clicked.
    a, b = visible_gids(inbox)[:2]
    inbox.locator(f"#row-{a} input.sel").click()
    inbox.locator(f"#row-{b} input.sel").click()
    assert inbox.evaluate("document.activeElement.className") == "sel"
    inbox.keyboard.press("x")
    for g in (a, b):
        expect(inbox.locator(f"#row-{g}")).to_contain_text("Dismissed")
    assert labelled(server) == {a, b}


def test_keys_work_after_select_all_click(inbox, server):
    gids = visible_gids(inbox)
    inbox.locator("#select-all").click()
    inbox.keyboard.press("s")
    expect(inbox.locator(f"#row-{gids[-1]}")).to_contain_text("Shortlisted")
    assert labelled(server) == set(gids)


def test_navigation_keys_work_with_a_checkbox_focused(inbox):
    gids = visible_gids(inbox)
    inbox.locator(f"#row-{gids[0]} input.sel").click()
    inbox.keyboard.press("j")
    assert selected_gid(inbox) == gids[1]
    inbox.keyboard.press("k")
    assert selected_gid(inbox) == gids[0]


def test_space_on_a_focused_checkbox_toggles_only_that_box(inbox):
    gids = visible_gids(inbox)
    box = inbox.locator(f"#row-{gids[1]} input.sel")
    box.focus()
    inbox.keyboard.press(" ")
    expect(box).to_be_checked()
    expect(inbox.locator("#bulk-count")).to_have_text("1 selected")  # not also the current row
    inbox.keyboard.press(" ")
    expect(box).not_to_be_checked()


def test_two_bulk_batches_undo_one_at_a_time(inbox, server):
    gids = visible_gids(inbox)
    for g in gids[:2]:  # batch 1: first row; batch 2: second row
        inbox.locator(f"#row-{g} input.sel").click()
        inbox.keyboard.press("x")
        expect(inbox.locator(f"#row-{g}")).to_contain_text("Dismissed")
    assert labelled(server) == set(gids[:2])
    inbox.keyboard.press("u")
    expect(inbox.locator(f"article#row-{gids[1]}")).to_be_visible()
    assert labelled(server) == {gids[0]}
    inbox.keyboard.press("u")
    expect(inbox.locator(f"article#row-{gids[0]}")).to_be_visible()
    assert not labelled(server)
    assert not inbox.errors


def test_mouse_undo_all_leaves_no_stale_undo_entry(inbox, server):
    gids = visible_gids(inbox)
    inbox.locator(f"#row-{gids[0]} input.sel").click()
    inbox.keyboard.press("x")
    expect(inbox.locator(f"#row-{gids[0]}")).to_contain_text("Dismissed")
    inbox.get_by_role("button", name="undo all").click()
    expect(inbox.locator(f"article#row-{gids[0]}")).to_be_visible()
    assert not labelled(server)
    inbox.keyboard.press("x")  # a single dismiss of the focused row
    expect(inbox.locator(".row.triaged")).to_have_count(1)
    assert len(labelled(server)) == 1
    # htmx wires the new row's undo button when the swap settles (20ms); let it.
    inbox.wait_for_function("!document.querySelector('.htmx-settling, .htmx-swapping')")
    inbox.keyboard.press("u")  # the first u must undo this dismissal, not a spent bulk entry
    expect(inbox.locator(".row.triaged")).to_have_count(0)
    assert not labelled(server)


def test_bulk_toast_is_a_standing_live_region(inbox):
    gid = visible_gids(inbox)[0]
    inbox.evaluate("document.getElementById('bulk-toast').dataset.mark = 'same-node'")
    inbox.locator(f"#row-{gid} input.sel").click()
    inbox.keyboard.press("x")
    toast = inbox.locator("#bulk-toast")
    expect(toast).to_contain_text("Dismissed 1 job")
    assert toast.get_attribute("role") == "status"
    assert toast.get_attribute("data-mark") == "same-node"  # content swapped, node kept
    inbox.get_by_role("button", name="undo all").click()
    expect(toast).to_contain_text("Undone: 1 job restored")
    assert toast.get_attribute("data-mark") == "same-node"


def test_k_scrolls_the_selected_row_clear_of_the_sticky_bar(page, server):
    page.set_viewport_size({"width": 1000, "height": 350})
    page.goto(f"{server.url}/inbox")
    page.wait_for_selector("article.row.selected")
    for _ in range(8):
        page.keyboard.press("j")
    for _ in range(5):
        page.keyboard.press("k")
    top = page.evaluate(
        "document.querySelector('article.row.selected').getBoundingClientRect().top"
    )
    bar = page.evaluate("document.getElementById('bulk-bar').getBoundingClientRect().bottom")
    assert top >= bar - 1, f"selected row top {top} is under the bulk bar (bottom {bar})"


def test_phone_header_keeps_the_first_job_near_the_top(page, server):
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(f"{server.url}/inbox")
    head = page.evaluate("document.querySelector('.inbox-head').getBoundingClientRect().height")
    assert head <= 150, f"header is {head}px tall: chips are one per row"
    h1 = page.evaluate("document.querySelector('.inbox-head h1').getBoundingClientRect().bottom")
    chip = page.evaluate("document.querySelector('.chips').getBoundingClientRect().top")
    assert chip >= h1 - 1  # the chips sit under the title, not squeezed beside it
