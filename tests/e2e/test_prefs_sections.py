"""/prefs per-section Save in a real browser (b25ec96): a section's Save is enabled only while
it differs from what loaded, saving one section keeps another's unsaved edit, the sticky bar
counts, and leaving with unsaved edits warns. Temp profile and DB; network blocked."""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import expect

from jobhunter.scoring.profile import load_profile

pytestmark = pytest.mark.e2e

FLOOR = "input[name='hard.salary_floor.amount']"
BONUS = "input[name='soft.remote_bonus']"


def save_btn(page, section):
    return page.locator(f"button[data-section-save='{section}']")


def test_only_the_edited_sections_save_is_enabled_and_reverting_disables_it(page, server):
    page.goto(f"{server.url}/prefs")
    for s in ("pay", "where", "narrative", "answers"):
        expect(save_btn(page, s)).to_be_disabled()
    expect(page.locator("#dirty-bar")).to_be_hidden()
    original = page.input_value(FLOOR)
    page.fill(FLOOR, "151000")
    expect(save_btn(page, "pay")).to_be_enabled()
    expect(save_btn(page, "where")).to_be_disabled()
    expect(page.locator("#dirty-count")).to_have_text("1 section with unsaved changes")
    page.fill(FLOOR, original)
    expect(save_btn(page, "pay")).to_be_disabled()
    expect(page.locator("#dirty-bar")).to_be_hidden()


def test_state_tile_click_marks_where_dirty(page, server):
    page.goto(f"{server.url}/prefs")
    expect(save_btn(page, "where")).to_be_disabled()
    page.click("button.tile[data-state='TX']")
    expect(save_btn(page, "where")).to_be_enabled()
    page.click("button.tile[data-state='TX']")  # excluded
    page.click("button.tile[data-state='TX']")  # neutral again
    expect(save_btn(page, "where")).to_be_disabled()


def test_saving_one_section_keeps_another_sections_unsaved_edit(page, server):
    page.goto(f"{server.url}/prefs")
    page.fill(FLOOR, "151000")
    page.fill(BONUS, "17")
    expect(page.locator("#dirty-count")).to_have_text("2 sections with unsaved changes")
    save_btn(page, "pay").click()
    expect(page.locator("#status-pay")).to_contain_text("Saved")
    expect(save_btn(page, "pay")).to_be_disabled()
    expect(save_btn(page, "where")).to_be_enabled()
    assert page.input_value(BONUS) == "17"  # still in the page
    expect(page.locator("#dirty-count")).to_have_text("1 section with unsaved changes")
    profile_dir = server.app.state.settings.paths.profile_dir
    saved = load_profile(profile_dir)
    assert saved.hard.salary_floor.amount == 151000 and saved.soft.remote_bonus != 17
    # The new mtime came back, so the next section saves without a conflict.
    save_btn(page, "where").click()
    expect(page.locator("#status-where")).to_contain_text("Saved 1 change")
    assert load_profile(profile_dir).soft.remote_bonus == 17
    expect(page.locator("#dirty-bar")).to_be_hidden()
    # The live preview still answers after a section save.
    page.fill(FLOOR, "160000")
    expect(page.locator("#prefs-preview")).not_to_contain_text("Change a setting to preview")


def test_validation_error_is_shown_in_the_section_and_nothing_is_written(page, server):
    page.goto(f"{server.url}/prefs")
    page.fill(FLOOR, "lots")
    save_btn(page, "pay").click()
    expect(page.locator("#status-pay")).to_contain_text("Not saved")
    expect(save_btn(page, "pay")).to_be_enabled()
    assert load_profile(server.app.state.settings.paths.profile_dir).hard.salary_floor is None


def test_paid_section_asks_for_the_cost_choice_before_writing(page, server):
    profile_dir = server.app.state.settings.paths.profile_dir
    page.goto(f"{server.url}/prefs")
    page.fill("textarea[name='narrative.avoid']", "On-call rotations.")
    save_btn(page, "narrative").click()
    expect(page.locator("#status-narrative")).to_contain_text("This section is paid")
    assert load_profile(profile_dir).narrative.avoid != "On-call rotations."
    page.locator("#status-narrative [data-section-confirm]").click()
    expect(page.locator("#status-narrative")).to_contain_text("Saved 1 change")
    assert load_profile(profile_dir).narrative.avoid == "On-call rotations."
    expect(save_btn(page, "narrative")).to_be_disabled()


def test_answers_section_saves_alone_and_shows_the_new_row(page, server):
    page.goto(f"{server.url}/prefs")
    page.fill(FLOOR, "151000")
    page.fill("input[name='ans.notice_period']", "two weeks")
    save_btn(page, "answers").click()
    expect(page.locator("#status-answers")).to_contain_text("Saved 1 change")
    expect(page.locator("input[name='ans.notice_period']")).to_have_value("two weeks")
    expect(save_btn(page, "answers")).to_be_disabled()
    expect(save_btn(page, "pay")).to_be_enabled()
    assert page.input_value(FLOOR) == "151000"


def test_leaving_with_unsaved_edits_warns(browser, server):
    ctx = browser.new_context()
    page = ctx.new_page()
    page.goto(f"{server.url}/prefs")
    # A real click and key presses give the page user activation, which
    # Chromium needs before it will show a beforeunload dialog.
    page.click(FLOOR)
    page.keyboard.type("151000")
    with page.expect_event("dialog") as info:
        page.close(run_before_unload=True)
    dialog = info.value
    dialog.accept()
    assert dialog.type == "beforeunload"
    ctx.close()


def test_clean_page_leaves_without_a_warning(browser, server):
    ctx = browser.new_context()
    page = ctx.new_page()
    dialogs = []
    page.on("dialog", lambda d: (dialogs.append(d.type), d.accept()))
    page.goto(f"{server.url}/prefs")
    # Same real gesture as the warning test, so a false warning would show here.
    page.click(FLOOR)
    page.keyboard.type("151000")
    save_btn(page, "pay").click()
    expect(page.locator("#status-pay")).to_contain_text("Saved")
    page.close(run_before_unload=True)
    ctx.close()
    assert "beforeunload" not in dialogs


def test_two_section_saves_clicked_back_to_back_both_land(page, server):
    page.goto(f"{server.url}/prefs")
    page.fill(FLOOR, "151000")
    page.fill(BONUS, "17")
    save_btn(page, "pay").click()
    save_btn(page, "where").click()
    expect(page.locator("#status-where")).to_contain_text("Saved")
    expect(page.locator("#status-pay")).to_contain_text("Saved")
    saved = load_profile(server.app.state.settings.paths.profile_dir)
    assert saved.hard.salary_floor.amount == 151000 and saved.soft.remote_bonus == 17


def test_save_all_is_the_normal_save(page, server):
    page.goto(f"{server.url}/prefs")
    page.fill(FLOOR, "151000")
    page.fill(BONUS, "17")
    page.click("#save-all")
    page.wait_for_url(re.compile(r"/prefs\?saved=3"))
    expect(page.locator("#dirty-bar")).to_be_hidden()


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_dirty_bar_and_section_buttons_fit_a_phone(browser, server, scheme):
    ctx = browser.new_context(viewport={"width": 375, "height": 800}, color_scheme=scheme)
    page = ctx.new_page()
    try:
        page.goto(f"{server.url}/prefs#sec-pay")
        page.fill(FLOOR, "151000")
        expect(page.locator("#dirty-bar")).to_be_visible()
        overflow = page.evaluate(
            "document.documentElement.scrollWidth - document.documentElement.clientWidth"
        )
        assert overflow <= 1
        box = page.locator("#dirty-bar").bounding_box()
        assert box["x"] >= 0 and box["x"] + box["width"] <= 375
    finally:
        ctx.close()
