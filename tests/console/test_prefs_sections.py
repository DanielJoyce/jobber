"""/prefs per-section Save (b25ec96): a section writes only its own fields, over the file as it
is now, with the same validation, conflict check and history as the full Save. Synthetic data."""

from __future__ import annotations

import os
import re
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from selectolax.lexbor import LexborHTMLParser as HTMLParser

from jobhunter.config import Paths, Settings
from jobhunter.console import prefs as pf
from jobhunter.console.app import create_app
from jobhunter.core import db
from jobhunter.scoring import buckets as bk
from jobhunter.scoring.profile import load_profile, preferences_mtime_ns, profile_data

PREFS = """\
# synthetic test profile
resume_path: resume.md
target_titles: [Systems Engineer]
hard:
  states_allowed: all
  remote_ok: true
  salary_floor: {amount: 140000, period: year}  # floor
soft:
  weights: {skills: 0.30, seniority: 0.20, domain: 0.20, comp: 0.15, location: 0.15}
  state_ranking: [CO, WA]
narrative:
  want: Synthetic systems work.
"""
NOW = datetime.now(UTC)


@pytest.fixture
def pdir(tmp_path):
    d = tmp_path / "profile"
    d.mkdir()
    (d / "preferences.yaml").write_text(PREFS, encoding="utf-8")
    (d / "resume.md").write_text("Synthetic Person\n- ran synthetic hosts\n", encoding="utf-8")
    return d


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "t.db"
    c = db.connect(path)
    db.migrate(c)
    c.close()
    return path


@pytest.fixture
def conn(db_path):
    c = db.connect(db_path)
    yield c
    c.close()


@pytest.fixture
def client(pdir, db_path):
    settings = Settings(paths=Paths(profile_dir=pdir, db_path=db_path))
    return TestClient(create_app(settings, lambda: db.connect(db_path), clock=lambda: NOW))


def form_for(pdir, **overrides):
    """The full form the page would post for the current file, with overrides."""
    view = pf.view_from_data(profile_data(load_profile(pdir)))
    form: dict[str, list[str]] = {}
    for key, value in view.items():
        if isinstance(value, bool):
            if value:
                form[key] = ["1"]
        elif isinstance(value, list):
            form[key] = list(value)
        else:
            form[key] = [value]
    form["mtime_ns"] = [str(preferences_mtime_ns(pdir))]
    form["ans.present"] = ["1"]
    for key, value in overrides.items():
        key = key.replace("__", ".")
        if value is None:
            form.pop(key, None)
        else:
            form[key] = value if isinstance(value, list) else [value]
    return form


def text(pdir):
    return (pdir / "preferences.yaml").read_text(encoding="utf-8")


def history(conn):
    return {r["field_path"] for r in conn.execute("SELECT field_path FROM profile_change")}


def test_section_save_writes_only_its_own_fields(client, pdir, conn):
    # Every other section's fields are posted with different values: none may be written.
    form = form_for(
        pdir,
        hard__salary_floor__amount="150000",
        hard__remote_ok=None,
        target_titles="Other Title",
        narrative__avoid="Posted but unsaved.",
        buckets__bullseye_min="77",
    )
    r = client.post("/prefs/section/pay", data=form)
    assert r.status_code == 200 and "Saved 1 change" in r.text
    after = load_profile(pdir)
    assert after.hard.salary_floor.amount == 150000
    assert after.hard.remote_ok is True
    assert after.target_titles == ["Systems Engineer"]
    assert after.narrative.avoid is None
    assert "# floor" in text(pdir)  # the file's comment survives the round trip
    assert history(conn) == {"hard.salary_floor.amount"}
    assert conn.execute("SELECT count(*) FROM profile_snapshot").fetchone()[0] >= 1
    # The new mtime comes back so the next save does not conflict with this one.
    assert f'value="{preferences_mtime_ns(pdir)}"' in r.text and 'hx-swap-oob="true"' in r.text
    assert (
        "HX-Trigger-After-Swap" in r.headers and "prefs-saved" in r.headers["HX-Trigger-After-Swap"]
    )


def test_section_save_applies_on_top_of_a_changed_file_only_if_mtime_matches(client, pdir):
    form = form_for(pdir, hard__salary_floor__amount="150000")
    stat = os.stat(pdir / "preferences.yaml")
    hand = text(pdir).replace("remote_ok: true", "remote_ok: false")
    (pdir / "preferences.yaml").write_text(hand, encoding="utf-8")
    os.utime(pdir / "preferences.yaml", ns=(stat.st_atime_ns, stat.st_mtime_ns + 10**9))
    r = client.post("/prefs/section/pay", data=form)
    assert r.status_code == 409 and "changed on disk" in r.text and 'href="/prefs"' in r.text
    assert text(pdir) == hand


def test_section_save_after_an_earlier_section_save_does_not_conflict(client, pdir):
    # Saving "where" with the page's old mtime would conflict; the OOB mtime fixes that.
    r1 = client.post("/prefs/section/pay", data=form_for(pdir, hard__salary_floor__amount="150000"))
    new_mtime = re.search(r'name="mtime_ns" value="(\d+)"', r1.text).group(1)
    form = form_for(pdir, soft__remote_bonus="12")
    form["mtime_ns"] = [new_mtime]
    r2 = client.post("/prefs/section/where", data=form)
    assert r2.status_code == 200 and "Saved 1 change" in r2.text
    after = load_profile(pdir)
    assert after.hard.salary_floor.amount == 150000 and after.soft.remote_bonus == 12


def test_validation_error_stays_inline_and_writes_nothing(client, pdir):
    before = text(pdir)
    r = client.post("/prefs/section/pay", data=form_for(pdir, hard__salary_floor__amount="lots"))
    assert r.status_code == 422 and "Not saved" in r.text and "hard.salary_floor.amount" in r.text
    assert text(pdir) == before
    # A bad value in another section is not this section's problem.
    other = form_for(pdir, hard__salary_floor__amount="150000", soft__remote_bonus="x")
    r = client.post("/prefs/section/pay", data=other)
    assert r.status_code == 200 and load_profile(pdir).hard.salary_floor.amount == 150000


def test_weights_section_alone(client, pdir):
    form = form_for(pdir, w__skills="60", hard__salary_floor__amount="1")
    r = client.post("/prefs/section/weights", data=form)
    assert r.status_code == 200
    after = load_profile(pdir)
    assert after.soft.weights.skills > 0.3
    assert after.hard.salary_floor.amount == 140000
    zero = {f"w.{k}": ["0"] for k in pf.WEIGHT_KEYS}
    r = client.post(
        "/prefs/section/weights", data=form_for(pdir, **{k: v for k, v in zero.items()})
    )
    assert r.status_code == 422


def test_unchanged_section_says_so(client, pdir):
    before = text(pdir)
    r = client.post("/prefs/section/where", data=form_for(pdir, narrative__avoid="x"))
    assert r.status_code == 200 and "No changes" in r.text and text(pdir) == before


def test_paid_section_shows_the_cost_choice_before_writing(client, pdir, conn):
    before = text(pdir)
    form = form_for(pdir, narrative__avoid="On-call rotations.")
    r = client.post("/prefs/section/narrative", data=form)
    assert r.status_code == 200
    assert 'name="rescore_narrative"' in r.text and "This section is paid" in r.text
    assert text(pdir) == before and not history(conn)
    r = client.post("/prefs/section/narrative", data={**form, "confirm": "1"})
    assert "Saved 1 change" in r.text
    assert load_profile(pdir).narrative.avoid == "On-call rotations."
    assert conn.execute("SELECT count(*) FROM rescore_request").fetchone()[0] == 0


def test_paid_section_queues_the_chosen_rescore(client, pdir, conn):
    form = form_for(pdir, current_focus__doing="Synthetic work", rescore_recency="all")
    r = client.post("/prefs/section/recency", data={**form, "confirm": "1"})
    assert "Re-score (all) queued" in r.text
    assert conn.execute("SELECT scope FROM rescore_request").fetchone()["scope"] == "all"


def test_free_section_never_queues_a_rescore(client, pdir, conn):
    form = form_for(pdir, hard__salary_floor__amount="150000", rescore_pay="all")
    client.post("/prefs/section/pay", data={**form, "confirm": "1"})
    assert conn.execute("SELECT count(*) FROM rescore_request").fetchone()[0] == 0


def test_answers_section_saves_and_rerenders_its_rows(client, pdir):
    form = form_for(pdir, hard__salary_floor__amount="1")
    form["ans.notice_period"] = ["two weeks"]
    form["ans.custom.question"] = ["Why here?"]
    form["ans.custom.answer"] = ["Synthetic reasons."]
    r = client.post("/prefs/section/answers", data=form)
    assert r.status_code == 200 and r.headers["HX-Retarget"] == "#sec-answers"
    assert 'id="sec-answers"' in r.text and "Why here?" in r.text and "Saved 2 change" in r.text
    assert "answers:" in text(pdir) and "two weeks" in text(pdir)
    assert load_profile(pdir).hard.salary_floor.amount == 140000


def test_answers_never_store_refused_inline(client, pdir):
    before = text(pdir)
    form = form_for(pdir)
    form["ans.custom.question"] = ["Desired salary"]
    form["ans.custom.answer"] = ["a lot"]
    r = client.post("/prefs/section/answers", data=form)
    assert r.status_code == 422 and "never-store list" in r.text
    assert text(pdir) == before


def test_unknown_section_is_404_and_resume_has_no_save(client, pdir):
    assert client.post("/prefs/section/nope", data=form_for(pdir)).status_code == 404
    assert client.post("/prefs/section/resume", data=form_for(pdir)).status_code == 404


def test_page_renders_a_disabled_save_per_editable_section(client):
    doc = HTMLParser(client.get("/prefs").text)
    ids = {b.attributes["data-section-save"]: b for b in doc.css("button[data-section-save]")}
    assert set(ids) == set(pf.SAVEABLE_SECTIONS)
    assert all("disabled" in b.attributes for b in ids.values())
    assert "hidden" in doc.css_first("#dirty-bar").attributes
    for section in ids:
        assert doc.css_first(f"#prefs-form #sec-{section} #status-{section}") is not None


def test_failed_full_save_marks_the_unsaved_sections_dirty(client, pdir):
    form = form_for(pdir, hard__salary_floor__amount="lots", target_titles="Changed")
    r = client.post("/prefs/save", data=form)
    assert r.status_code == 422
    marked = HTMLParser(r.text).css_first("#prefs-form").attributes["data-unsaved"].split()
    assert marked == ["pay", "what"]


# ─── the mapping ────────────────────────────────────────────────────────────


def form_field_names(html: str) -> list[str]:
    doc = HTMLParser(html)
    names = []
    for el in doc.css("#prefs-form input, #prefs-form select, #prefs-form textarea"):
        name = el.attributes.get("name")
        if name and not el.attributes.get("form"):
            names.append(name)
    return names


def test_every_form_field_belongs_to_exactly_one_section(client):
    names = set(form_field_names(client.get("/prefs").text))
    owned = [n for fields in pf.SECTION_FIELDS.values() for n in fields]
    assert len(owned) == len(set(owned)), "a field is listed under two sections"
    plain = {n for n in names if not n.startswith(pf.ANSWERS_PREFIX)} - pf.PLUMBING_FIELDS
    assert plain == set(owned)
    assert {n for n in names if pf.section_of_field(n) is None} <= pf.PLUMBING_FIELDS
    assert all(pf.section_of_field(n) for n in owned)
    # Bucket threshold inputs: one per threshold, so a new threshold cannot go unowned.
    assert {n for n in names if n.startswith("buckets.")} == {
        f"buckets.{k}" for k in bk.DEFAULT_THRESHOLDS
    }


def test_sections_together_reproduce_the_full_form(pdir):
    base = profile_data(load_profile(pdir))
    form = form_for(
        pdir,
        hard__salary_floor__amount="150000",
        hard__salary_target__amount="170000",
        salary_period="month",
        hard__hide_unstated_salary="1",
        state_ranking="TX,CO",
        states_excluded="NY",
        hard__remote_ok=None,
        soft__remote_bonus="9",
        hard__relocation_ok="1",
        target_titles="A\nB",
        hard__title_exclusions="Intern",
        hard__employment_types_excluded=["contract"],
        hard__requires_i_lack=["clearance"] if "clearance" in pf.CREDENTIAL_CHOICES else [],
        w__skills="10",
        buckets__bullseye_min="77",
        queries="systems, linux",
        current_focus__since="2024-01",
        current_focus__doing="x",
        current_focus__want_more_of="y",
        current_focus__done_with="z",
        narrative__want="w",
        narrative__avoid="a",
        narrative__dealbreakers_soft="d",
        narrative__context="c",
    )
    whole, errors = pf.form_to_data(form, base)
    assert not errors
    merged = base
    for section in pf.SECTION_PATHS:
        merged, errs = pf.section_to_data(section, form, merged)
        assert not errs
    assert merged == whole
    # And each section changes only paths that belong to it.
    for section in pf.SECTION_PATHS:
        data, _ = pf.section_to_data(section, form, base)
        assert {pf.section_of_path(p) for p in pf.diff(base, data)} <= {section}


def test_one_paid_sections_choice_is_not_used_by_the_other(client, pdir, conn):
    # Both confirm boxes post their radios; each save reads only its own group.
    form = form_for(
        pdir,
        narrative__avoid="On-call rotations.",
        current_focus__doing="x",
        rescore_recency="all",
        rescore_narrative="none",
    )
    client.post("/prefs/section/narrative", data={**form, "confirm": "1"})
    assert conn.execute("SELECT count(*) FROM rescore_request").fetchone()[0] == 0
    changed = form_for(pdir, current_focus__doing="y", narrative__avoid="z")
    recency = client.post("/prefs/section/recency", data=changed).text
    narrative = client.post("/prefs/section/narrative", data=changed).text
    assert 'name="rescore_recency"' in recency and 'name="rescore_narrative"' not in recency
    assert 'name="rescore_narrative"' in narrative and 'name="rescore_recency"' not in narrative


def test_concurrent_section_saves_never_lose_an_update(client, pdir):
    import threading

    base = form_for(pdir)
    pay = {**base, "hard.salary_floor.amount": ["150000"]}
    where = {**base, "soft.remote_bonus": ["12"]}
    results = {}

    def go(name, data):
        results[name] = client.post(f"/prefs/section/{name}", data=data).status_code

    threads = [
        threading.Thread(target=go, args=("pay", pay)),
        threading.Thread(target=go, args=("where", where)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    after = load_profile(pdir)
    landed = {
        "pay": after.hard.salary_floor is not None and after.hard.salary_floor.amount == 150000,
        "where": after.soft.remote_bonus == 12,
    }
    # Same mtime posted twice: the loser is told (409), never "Saved" for a lost change.
    for name in ("pay", "where"):
        assert (results[name] == 200) == landed[name], (results, landed)
    assert 200 in results.values()


def test_crlf_and_list_order_do_not_mark_sections_unsaved(client, pdir):
    form = form_for(pdir, hard__salary_floor__amount="lots")
    form["narrative.want"] = ["Synthetic systems work.\r\n"]
    form["target_titles"] = ["Systems Engineer\r\n"]
    r = client.post("/prefs/save", data=form)
    assert r.status_code == 422
    marked = HTMLParser(r.text).css_first("#prefs-form").attributes["data-unsaved"].split()
    assert marked == ["pay"]
    assert pf._norm(["b", "a"]) == pf._norm(["a", "b"])


def test_nochange_settles_the_section(client, pdir):
    r = client.post("/prefs/section/where", data=form_for(pdir))
    assert "prefs-saved" in r.headers["HX-Trigger-After-Swap"]
