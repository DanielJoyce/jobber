"""/prefs: page, preview, save, history/revert, hand edits, estimates. Synthetic, no network."""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from stray_letters import stray_letters

from jobhunter.config import Paths, Settings
from jobhunter.console import prefs as pf
from jobhunter.console.app import create_app
from jobhunter.core import db
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
MINUS = "\N{MINUS SIGN}"


def dims(score=85):
    return {
        "skills": {"score": score},
        "seniority": {"score": score},
        "domain": {"score": score},
        "raw_skills": score,
        "recency_weighted_skills": score,
    }


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
    c.execute(
        "INSERT INTO source (key, class, name, family, tier, entry, policy) VALUES "
        "('co', 'A', 'Synthetic', 'x', 'http', 'https://example.com', 'enabled')"
    )
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


def add_job(conn, n, *, salary_max=120000, score=85, cost=None, days=2, closes=None):
    ts = (NOW - timedelta(days=days)).isoformat()
    conn.execute(
        "INSERT INTO job (id, source_key, external_id, url, title, salary_min, salary_max, "
        "salary_period, salary_stated, location_scope, posted_at, closes_at, first_seen_at, "
        "last_seen_at) VALUES (?, 'co', ?, 'https://example.com/j', ?, ?, ?, 'year', 1, "
        "'single', ?, ?, ?, ?)",
        (n, str(n), f"Job {n}", salary_max, salary_max, ts, closes, ts, ts),
    )
    conn.execute(
        "INSERT INTO job_group (id, canonical_job_id, method, created_at) "
        "VALUES (?, ?, 'exact_hash', ?)",
        (n, n, ts),
    )
    conn.execute("UPDATE job SET job_group_id = ? WHERE id = ?", (n, n))
    conn.execute("INSERT INTO job_locations (job_id, state, is_primary) VALUES (?, 'CO', 1)", (n,))
    conn.execute(
        "INSERT INTO fit_score (job_group_id, tier, model, prompt_version, scoring_version, "
        "verdict, overall, dimensions, evidence, cost_usd, created_at) "
        "VALUES (?, 'screen', 'm', 'p', 'old-version', 'strong', 80, ?, '[]', ?, ?)",
        (n, json.dumps(dims(score)), cost, ts),
    )


def form_for(pdir, **overrides):
    """The form the page would post for the current file, with overrides."""
    profile = load_profile(pdir)
    view = pf.view_from_data(profile_data(profile))
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
    for key, value in overrides.items():
        key = key.replace("__", ".")
        if value is None:
            form.pop(key, None)
        else:
            form[key] = value if isinstance(value, list) else [value]
    return form


def prefs_text(pdir):
    return (pdir / "preferences.yaml").read_text(encoding="utf-8")


# ─── page ───────────────────────────────────────────────────────────────────


def test_page_renders_all_sections_in_order(client):
    r = client.get("/prefs")
    assert r.status_code == 200
    ids = [
        "sec-pay",
        "sec-where",
        "sec-what",
        "sec-weights",
        "sec-buckets",
        "sec-queries",
        "sec-spend",
        "sec-recency",
        "sec-narrative",
        "sec-resume",
    ]
    positions = [r.text.index(f'id="{i}"') for i in ids]
    assert positions == sorted(positions)
    assert r.text.index("paid-divider") < r.text.index('id="sec-recency"')
    assert 'value="140000"' in r.text
    assert "config.toml" in r.text
    assert 'data-state="CO"' in r.text and "Colorado: ranked 1" in r.text
    assert 'name="state_ranking" value="CO,WA"' in r.text
    assert "Synthetic systems work." in r.text
    assert "prefs.js" in r.text


def test_page_without_profile_shows_banner(tmp_path, db_path):
    settings = Settings(paths=Paths(profile_dir=tmp_path / "nope", db_path=db_path))
    c = TestClient(create_app(settings, lambda: db.connect(db_path)))
    r = c.get("/prefs")
    assert r.status_code == 200
    assert "No profile yet" in r.text
    assert 'name="hard.salary_floor.amount"' in r.text


def test_state_picker_cycle(client):
    data = {"state_ranking": "CO,WA", "states_excluded": ""}
    r = client.post("/prefs/state/NM", data=data)
    assert r.status_code == 200
    assert r.headers["HX-Trigger"] == "prefs-changed"
    assert 'name="state_ranking" value="CO,WA,NM"' in r.text
    assert "New Mexico: ranked 3" in r.text
    r = client.post("/prefs/state/co", data=data)
    assert 'name="state_ranking" value="WA"' in r.text
    assert 'name="states_excluded" value="CO"' in r.text
    r = client.post("/prefs/state/CO", data={"state_ranking": "WA", "states_excluded": "CO"})
    assert 'name="states_excluded" value=""' in r.text
    assert client.post("/prefs/state/ZZ", data=data).status_code == 404


def test_cycle_state_unit():
    assert pf.cycle_state([], [], "CO") == (["CO"], [])
    assert pf.cycle_state(["CO"], [], "CO") == ([], ["CO"])
    assert pf.cycle_state([], ["CO"], "CO") == ([], [])


# ─── preview ────────────────────────────────────────────────────────────────


def test_preview_no_changes(client, pdir):
    r = client.post("/prefs/preview", data=form_for(pdir))
    assert "No unsaved changes" in r.text


def test_preview_bucket_deltas_when_floor_drops(client, pdir, conn):
    add_job(conn, 1, salary_max=120000)  # below the 140k floor: comp 0
    add_job(conn, 2, salary_max=200000)
    r = client.post("/prefs/preview", data=form_for(pdir, hard__salary_floor__amount="100000"))
    assert r.status_code == 200
    assert "hard.salary_floor.amount" in r.text
    assert "140000</span> → 100000" in r.text
    assert "2 scored jobs" in r.text
    lines = re.findall(
        r'<li>[A-Za-z ]+ <span class="bucket-badge"[^>]*>([A-G])</span> (\d+) → (\d+) \(([^)]+)\)',
        r.text,
    )
    assert lines, r.text
    assert all(sign.startswith(("+", MINUS)) for *_, sign in lines)
    assert any(b == "A" and sign.startswith("+") for b, _, _, sign in lines)
    assert f"Filtered out by salary 1 → 0 ({MINUS}1)" in r.text
    assert 'href="/job/1"' in r.text
    assert "Save — free" in r.text


def test_preview_names_buckets_not_letters_for_moved_jobs(client, pdir, conn):
    add_job(conn, 1, salary_max=120000)
    add_job(conn, 2, salary_max=200000)
    r = client.post("/prefs/preview", data=form_for(pdir, hard__salary_floor__amount="100000"))
    assert "that would move into Bullseye or Strong" in r.text
    assert re.search(r'<span class="muted">[A-Z][a-z]+( [A-Z][a-z]+)? → [A-Z][a-z]+', r.text)
    assert stray_letters(r.text) == []


def test_preview_paid_estimate_uses_measured_cost(client, pdir, conn):
    add_job(conn, 1, salary_max=200000, cost=0.01)
    add_job(conn, 2, salary_max=200000, cost=0.03)
    add_job(conn, 3, salary_max=200000, cost=0.02, days=40)  # open but not recent
    add_job(conn, 4, salary_max=200000, closes=(NOW - timedelta(days=1)).isoformat())  # closed
    r = client.post(
        "/prefs/preview",
        data=form_for(pdir, current_focus__done_with="Java enterprise", rescore="recent"),
    )
    assert "current_focus.done_with" in r.text
    assert "measured from fit_score" in r.text
    assert "$0.0200 per job" in r.text
    assert "(2 jobs)" in r.text and "≈ $0.04" in r.text  # recent A-E
    assert "(3 jobs)" in r.text and "≈ $0.06" in r.text  # all open
    assert re.search(r'value="recent"[^>]*checked', r.text)


def test_cost_per_job_sources(conn):
    assert pf.cost_per_job(conn) == (pf.FALLBACK_COST_PER_JOB, "default")
    add_job(conn, 1, cost=0.004)
    assert pf.cost_per_job(conn) == (0.004, "fit_score")
    conn.execute(
        "INSERT INTO llm_spend (day, model, tier, calls, cost_usd) "
        "VALUES ('2026-10-01', 'm', 'screen', 10, 0.05)"
    )
    cost, source = pf.cost_per_job(conn)
    assert source == "llm_spend" and cost == pytest.approx(0.005)


def test_preview_validation_errors(client, pdir):
    r = client.post("/prefs/preview", data=form_for(pdir, current_focus__since="last year"))
    assert "current_focus.since" in r.text and "YYYY-MM" in r.text
    r = client.post("/prefs/preview", data=form_for(pdir, hard__salary_floor__amount="lots"))
    assert "must be a number" in r.text


# ─── save, history, revert ─────────────────────────────────────────────────


def test_save_writes_file_and_logs(client, pdir, conn):
    client.get("/prefs")  # first load stores the snapshot
    form = form_for(pdir, hard__salary_floor__amount="125000", state_ranking="WA,CO,NM")
    r = client.post("/prefs/save", data=form, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/prefs?saved=2"
    text = prefs_text(pdir)
    assert "# synthetic test profile" in text and "# floor" in text
    assert "salary_floor: {amount: 125000, period: year}" in text
    assert "state_ranking: [WA, CO, NM]" in text
    rows = conn.execute("SELECT * FROM profile_change ORDER BY id").fetchall()
    assert [(r["field_path"], r["source"]) for r in rows] == [
        ("hard.salary_floor.amount", "ui"),
        ("soft.state_ranking", "ui"),
    ]
    assert json.loads(rows[0]["old_value"]) == 140000
    assert json.loads(rows[0]["new_value"]) == 125000
    profile = load_profile(pdir)
    assert rows[0]["filter_version"] == profile.filter_version
    assert rows[0]["scoring_version"] == profile.scoring_version
    page = client.get("/prefs?saved=2").text
    assert "Saved 2 change(s)." in page and "hard.salary_floor.amount" in page


def test_weights_normalized_on_save(client, pdir):
    form = form_for(pdir, **{"w.skills": "60", "w.seniority": "20", "w.domain": "20"})
    form["w.comp"] = ["0"]
    form["w.location"] = ["0"]
    client.post("/prefs/save", data=form, follow_redirects=False)
    w = load_profile(pdir).soft.weights
    assert (w.skills, w.seniority, w.domain, w.comp, w.location) == (0.6, 0.2, 0.2, 0, 0)


def test_revert(client, pdir, conn):
    client.get("/prefs")
    client.post("/prefs/save", data=form_for(pdir, hard__salary_floor__amount="125000"))
    change_id = conn.execute("SELECT max(id) FROM profile_change").fetchone()[0]
    r = client.post(f"/prefs/revert/{change_id}", follow_redirects=False)
    assert r.status_code == 303
    assert load_profile(pdir).hard.salary_floor.amount == 140000
    last = conn.execute("SELECT * FROM profile_change ORDER BY id DESC LIMIT 1").fetchone()
    assert last["source"] == "revert"
    assert last["field_path"] == "hard.salary_floor.amount"
    assert json.loads(last["new_value"]) == 140000
    assert client.post("/prefs/revert/9999").status_code == 404


def test_revert_weights_works_while_resume_missing(client, pdir, conn):
    client.get("/prefs")
    form = form_for(pdir, **{"w.skills": "60", "w.seniority": "20", "w.domain": "20"})
    form["w.comp"] = ["0"]
    form["w.location"] = ["0"]
    client.post("/prefs/save", data=form, follow_redirects=False)
    change_id = conn.execute("SELECT max(id) FROM profile_change").fetchone()[0]
    (pdir / "resume.md").unlink()  # the resume goes missing after the change

    r = client.post(f"/prefs/revert/{change_id}", follow_redirects=False)

    assert r.status_code == 303, r.text
    assert r.headers["location"] == f"/prefs?reverted={change_id}"
    w = load_profile(pdir, require_resume=False).soft.weights
    assert (w.skills, w.seniority, w.domain, w.comp, w.location) == (0.3, 0.2, 0.2, 0.15, 0.15)
    assert conn.execute("SELECT source FROM profile_change ORDER BY id DESC").fetchone()[0] == (
        "revert"
    )


def test_validation_error_inline_and_file_untouched(client, pdir):
    before = prefs_text(pdir)
    r = client.post("/prefs/save", data=form_for(pdir, current_focus__since="2024-13"))
    assert r.status_code == 422
    assert 'id="err-current_focus-since"' in r.text
    assert 'value="2024-13"' in r.text  # the submitted value is kept
    assert prefs_text(pdir) == before


def test_conflict_banner(client, pdir, conn):
    form = form_for(pdir, hard__salary_floor__amount="125000")
    stat = os.stat(pdir / "preferences.yaml")
    hand = prefs_text(pdir).replace("remote_ok: true", "remote_ok: false")
    (pdir / "preferences.yaml").write_text(hand, encoding="utf-8")
    os.utime(pdir / "preferences.yaml", ns=(stat.st_atime_ns, stat.st_mtime_ns + 10**9))
    r = client.post("/prefs/save", data=form, follow_redirects=False)
    assert r.status_code == 409
    assert "changed on disk" in r.text and 'href="/prefs">Reload from file' in r.text
    assert prefs_text(pdir) == hand
    preview = client.post("/prefs/preview", data=form)
    assert "Saving will be refused" in preview.text


def test_hand_edit_detection(pdir, conn):
    profile = load_profile(pdir)
    assert pf.detect_file_edits(conn, profile, NOW) == {}  # first sight: snapshot only
    assert pf.detect_file_edits(conn, profile, NOW) == {}
    edited = prefs_text(pdir).replace("[CO, WA]", "[CO, WA, OR]")
    (pdir / "preferences.yaml").write_text(edited, encoding="utf-8")
    (pdir / "resume.md").write_text("Synthetic Person\n- new synthetic job\n", encoding="utf-8")
    changes = pf.detect_file_edits(conn, load_profile(pdir), NOW)
    assert set(changes) == {"soft.state_ranking", pf.RESUME_PATH}
    assert changes["soft.state_ranking"] == (["CO", "WA"], ["CO", "WA", "OR"])
    rows = conn.execute("SELECT field_path, source FROM profile_change ORDER BY id").fetchall()
    assert {(r["field_path"], r["source"]) for r in rows} == {
        ("soft.state_ranking", "file"),
        (pf.RESUME_PATH, "file"),
    }
    assert pf.detect_file_edits(conn, load_profile(pdir), NOW) == {}


def test_hand_edit_logged_on_page_load(client, pdir, conn):
    client.get("/prefs")
    edited = prefs_text(pdir).replace("remote_ok: true", "remote_ok: false")
    (pdir / "preferences.yaml").write_text(edited, encoding="utf-8")
    r = client.get("/prefs")
    row = conn.execute("SELECT * FROM profile_change").fetchone()
    assert (row["field_path"], row["source"]) == ("hard.remote_ok", "file")
    assert ">file<" in r.text


def test_paid_save_records_rescore_request(client, pdir, conn):
    add_job(conn, 1, salary_max=200000, cost=0.01)
    form = form_for(pdir, narrative__avoid="On-call rotations.", rescore="all")
    r = client.post("/prefs/save", data=form, follow_redirects=False)
    assert r.headers["location"] == "/prefs?saved=1&rescore=all"
    req = conn.execute("SELECT * FROM rescore_request").fetchone()
    assert req["scope"] == "all" and req["status"] == "pending"
    assert req["job_count"] == 1
    assert req["estimated_usd"] == pytest.approx(0.01)
    assert req["scoring_version"] == load_profile(pdir).scoring_version
    assert "Queued re-scores" in client.get("/prefs").text


def test_paid_save_new_jobs_only_records_nothing(client, pdir, conn):
    form = form_for(pdir, narrative__avoid="On-call rotations.", rescore="none")
    client.post("/prefs/save", data=form, follow_redirects=False)
    assert conn.execute("SELECT count(*) FROM rescore_request").fetchone()[0] == 0
    # A free change never queues a re-score, whatever the radio says.
    client.post("/prefs/save", data=form_for(pdir, hard__remote_ok=None, rescore="all"))
    assert conn.execute("SELECT count(*) FROM rescore_request").fetchone()[0] == 0


def test_resume_changed_since_scoring(client, conn):
    assert "Nothing scored yet" in client.get("/prefs").text
    add_job(conn, 1)  # scored under 'old-version'
    assert "Changed since last scoring" in client.get("/prefs").text


def test_queries_round_trip():
    errors: dict[str, str] = {}
    raw = 'linux, systems\n{keywords: [unix], occupation_code: "2210"}\n'
    parsed = pf.parse_queries(raw, errors)
    assert not errors
    assert parsed[0]["keywords"] == ["linux", "systems"]
    assert parsed[1]["occupation_code"] == "2210"
    lines = [pf.format_query(q) for q in parsed]
    assert lines[0] == "linux, systems"
    assert pf.parse_queries("\n".join(lines), errors) == parsed
    pf.parse_queries("{keywords: [x], bogus: 1}", errors)
    assert "queries" in errors


def test_diff_and_weights_atomic():
    before = {"soft": {"weights": {"skills": 0.3}, "remote_bonus": 0}, "buckets": {}}
    after = {"soft": {"weights": {"skills": 0.4}, "remote_bonus": 5}, "buckets": {"b": 1}}
    assert pf.diff(before, after) == {
        "soft.weights": ({"skills": 0.3}, {"skills": 0.4}),
        "soft.remote_bonus": (0, 5),
        "buckets.b": (None, 1),
    }
    assert pf.diff({"a": None}, {"a": []}) == {}


def test_bucket_names_replace_letters_and_thresholds_group_by_bucket(client):
    text = client.get("/prefs").text
    start = text.index('id="sec-buckets"')
    section = text[start : text.index('id="sec-queries"', start)]
    legends = re.findall(r"<legend>\s*([A-Za-z ]+?)\s*(?:<span|</legend>)", section)
    assert legends[:3] == ["Bullseye", "Strong", "Stretch Up"]
    assert "Stale Match" in legends and "Mismatch" in legends and "Bullseye (A)" not in section
    assert re.search(r'<legend>Bullseye <span class="bucket-badge"[^>]*>A</span>', section)
    first_group = section[section.index("<legend>Bullseye") : section.index("<legend>Strong")]
    assert (
        'name="buckets.a_overall"' in first_group and 'name="buckets.b_overall"' not in first_group
    )
