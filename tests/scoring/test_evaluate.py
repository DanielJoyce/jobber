"""jobhunter eval: calibration metrics on synthetic labels and fit_score rows. No network."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from jobhunter.cli import app
from jobhunter.core import db
from jobhunter.scoring import evaluate as ev
from jobhunter.scoring.profile import Profile

runner = CliRunner()
NOW = datetime(2026, 10, 1, tzinfo=UTC)
EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "preferences.example.yaml"


def make_profile() -> Profile:
    # location/comp weight 0 so overall == the common value of skills/seniority/domain.
    weights = {"skills": 0.4, "seniority": 0.3, "domain": 0.3, "comp": 0, "location": 0}
    return Profile.model_validate({"hard": {}, "soft": {"weights": weights}})


def _new_db(path):
    c = db.connect(path)
    db.migrate(c)
    c.execute(
        "INSERT INTO source (key, class, name, family, tier, entry, policy) "
        "VALUES ('wa', 'B', 'WA', 'neogov', 'http', 'https://example.com', 'enabled')"
    )
    return c


@pytest.fixture
def conn():
    c = _new_db(":memory:")
    yield c
    c.close()


_next = [0]


def add(
    conn,
    x,
    label,
    *,
    gid=None,
    raw=None,
    recency=None,
    domain=None,
    pv="v1",
    model="m1",
    unverified=False,
    cost=0.001,
    title=None,
    claim="did a thing",
    labeled_at=None,
    seniority=None,
):
    """One job group with a label (if given) and a screen fit_score whose dims are all ``x``."""
    if gid is None:
        _next[0] += 1
        gid = _next[0]
    iso = NOW.isoformat()
    if not conn.execute("SELECT 1 FROM job_group WHERE id = ?", (gid,)).fetchone():
        conn.execute(
            "INSERT INTO job (id, source_key, external_id, url, title, salary_stated, "
            "location_scope, first_seen_at, last_seen_at) "
            "VALUES (?, 'wa', ?, 'u', ?, 0, 'single', ?, ?)",
            (gid, str(gid), title or f"Job {gid}", iso, iso),
        )
        conn.execute(
            "INSERT INTO job_group (id, canonical_job_id, method, created_at) "
            "VALUES (?, ?, 'exact_hash', ?)",
            (gid, gid, iso),
        )
        conn.execute(
            "INSERT INTO job_locations (job_id, state, is_primary) VALUES (?, 'CO', 1)", (gid,)
        )
        if label:
            conn.execute(
                "INSERT INTO label (job_group_id, label, labeled_at) VALUES (?, ?, ?)",
                (gid, label, labeled_at or iso),
            )
    dims = {
        "skills": {"score": x, "why": "w"},
        "seniority": {"score": x if seniority is None else seniority, "why": "match: ok"},
        "domain": {"score": x if domain is None else domain, "why": "w"},
        "raw_skills": x if raw is None else raw,
        "recency_weighted_skills": x if recency is None else recency,
    }
    conn.execute(
        "INSERT INTO fit_score (job_group_id, tier, model, prompt_version, scoring_version, "
        "verdict, overall, dimensions, evidence, evidence_unverified, cost_usd, created_at) "
        "VALUES (?, 'screen', ?, ?, 's1', 'strong', 0, ?, ?, ?, ?, ?)",
        (
            gid,
            model,
            pv,
            json.dumps(dims),
            json.dumps([{"claim": claim, "quote": "q", "verified": not unverified}]),
            int(unverified),
            cost,
            iso,
        ),
    )
    return gid


def seed_hand_set(conn):
    for x in (90, 70, 50, 20, 85):
        add(conn, x, "interesting")
    for x in (82, 65, 30, 45, 10):
        add(conn, x, "not_interesting")


def test_recall_precision_hand_computed(conn):
    seed_hand_set(conn)
    r = ev.evaluate(conn, make_profile(), prompt_version="v1")
    assert (r.n, r.n_pos, r.n_neg) == (10, 5, 5)
    m = r.metrics
    assert m["recall_bucket_ab"] == 0.6  # 90,70,85 of 5 positives
    assert m["recall_verdict_strong_possible"] == 0.6
    assert m["precision_bucket_a"] == pytest.approx(2 / 3, abs=1e-4)  # 90,85 vs neg 82
    assert m["precision_bucket_ab"] == 0.6  # 3 of {90,70,85,82,65}
    assert m["discard_rate_not_interesting"] == 0.6  # 30,45,10 outside A+B
    assert r.bucket_counts["A"] == {"positive": 2, "negative": 1}
    assert r.targets["recall_bucket_ab"]["pass"] is False


def test_applied_counts_as_positive(conn):
    add(conn, 90, "applied")
    add(conn, 10, "not_interesting")
    r = ev.evaluate(conn, make_profile(), prompt_version="v1")
    assert (r.n_pos, r.n_neg) == (1, 1)


def test_insufficient_labels_still_shows_numbers(conn):
    seed_hand_set(conn)
    r = ev.evaluate(conn, make_profile(), prompt_version="v1")
    assert r.targets["min_labels"]["pass"] is False
    assert "insufficient labels (10/150)" in r.notes
    text = r.to_text()
    assert "insufficient labels (10/150)" in text
    assert "recall Bullseye (A) + Strong (B)" in text and "FAIL" in text


def test_targets_pass_with_enough_labels(conn):
    for _ in range(150):
        add(conn, 90, "interesting")
    r = ev.evaluate(conn, make_profile(), prompt_version="v1")
    assert r.notes == []
    assert all(t["pass"] for t in r.targets.values())
    assert "insufficient" not in r.to_text()


def test_distribution_and_never_varying_dimension(conn):
    for x in (80, 90, 100, 100):
        add(conn, x, "interesting", seniority=70)
    r = ev.evaluate(conn, make_profile(), prompt_version="v1")
    d = r.distribution
    # overall = 0.4*x + 0.3*70 + 0.3*x -> 77, 84, 91, 91 (seniority pinned at 70)
    assert d["histogram"]["70-79"] == 1 and d["histogram"]["80-89"] == 1
    assert d["histogram"]["90-100"] == 2
    assert d["mean"] == 85.75 and d["median"] == 87.5
    assert d["share_80_plus"] == 0.75
    dims = r.dimensions
    assert dims["seniority"]["never_varies"] is True
    assert dims["skills"]["never_varies"] is False
    assert dims["comp"]["n"] == 0 and dims["comp"]["never_varies"] is False
    assert "NEVER VARIES" in r.to_text()


def test_point_biserial():
    assert ev.pearson([1, 2, 3, 4], [0, 0, 1, 1]) == pytest.approx(0.8944, abs=1e-4)
    assert ev.pearson([1, 1, 1], [0, 1, 0]) is None


def test_point_biserial_in_report(conn):
    seed_hand_set(conn)
    r = ev.evaluate(conn, make_profile(), prompt_version="v1")
    assert r.dimensions["skills"]["point_biserial"] is not None


def test_unverified_rate(conn):
    add(conn, 90, "interesting", unverified=True)
    for _ in range(3):
        add(conn, 90, "interesting")
    r = ev.evaluate(conn, make_profile(), prompt_version="v1")
    assert r.metrics["evidence_unverified_rate"] == 0.25
    assert r.targets["evidence_unverified_rate"]["pass"] is False


def test_confusion_pairs_ordering_and_cap(conn):
    for x in range(30, 42):
        add(conn, x, "interesting", title=f"pos{x}", claim=f"claim{x}")
    for x in range(60, 72):
        add(conn, x, "not_interesting", title=f"neg{x}")
    r = ev.evaluate(conn, make_profile(), prompt_version="v1")
    missed, alarms = r.confusion["missed_positives"], r.confusion["false_alarms"]
    assert len(missed) == len(alarms) == 10
    assert [c["overall"] for c in missed] == list(range(30, 40))
    assert [c["overall"] for c in alarms] == list(range(71, 61, -1))
    assert missed[0]["title"] == "pos30" and missed[0]["claim"] == "claim30"
    assert {"group_id", "bucket"} <= set(missed[0])


def test_f_audit_and_d_fallback(conn):
    # F: strong raw skills, stale recency; one wanted, one not.
    add(conn, 60, "interesting", raw=90, recency=50, domain=60, seniority=60)
    add(conn, 60, "not_interesting", raw=90, recency=50, domain=60, seniority=60)
    # D fallback: overall 40-61, no rule matched.
    add(conn, 50, "interesting")
    add(conn, 45, "not_interesting")
    add(conn, 55, "not_interesting")
    # Explicit D rule (skills >= 60, domain < 55) is not a fallback.
    add(conn, 65, "interesting", domain=50, recency=65, raw=65, seniority=65)
    r = ev.evaluate(conn, make_profile(), prompt_version="v1")
    assert r.bucket_counts["F"] == {"positive": 1, "negative": 1}
    assert r.f_audit["positives_in_F"] == 1
    assert r.bucket_counts["D"] == {"positive": 2, "negative": 2}
    assert r.d_fallback == {"positive": 1, "negative": 2}


def test_default_prompt_version_is_newest_and_latest_row_wins(conn):
    gid = add(conn, 20, "interesting", pv="v1")
    add(conn, 95, None, gid=gid, pv="v2")
    later = (NOW + timedelta(days=1)).isoformat()
    conn.execute("UPDATE fit_score SET created_at = ? WHERE prompt_version = 'v2'", (later,))
    r = ev.evaluate(conn, make_profile())
    assert r.variant["prompt_version"] == "v2"
    assert r.metrics["recall_bucket_ab"] == 1.0
    assert ev.evaluate(conn, make_profile(), prompt_version="v1").metrics["recall_bucket_ab"] == 0
    assert ev.evaluate(conn, make_profile(), prompt_version="zzz").n == 0


def test_unscored_labels_counted(conn):
    seed_hand_set(conn)
    conn.execute("INSERT INTO job_group (id, method, created_at) VALUES (999, 'manual', 'x')")
    conn.execute(
        "INSERT INTO label (job_group_id, label, labeled_at) VALUES (999, 'interesting', 'x')"
    )
    r = ev.evaluate(conn, make_profile(), prompt_version="v1")
    assert (r.labels_total, r.n, r.labels_unscored) == (11, 10, 1)


def test_profile_changes_in_label_window(conn):
    add(conn, 90, "interesting", labeled_at="2026-09-01T00:00:00+00:00")
    add(conn, 10, "not_interesting", labeled_at="2026-09-20T00:00:00+00:00")
    for at, path in (
        ("2026-08-01T00:00:00+00:00", "hard.before"),
        ("2026-09-10T00:00:00+00:00", "hard.salary_floor"),
        ("2026-10-05T00:00:00+00:00", "hard.after"),
    ):
        conn.execute(
            "INSERT INTO profile_change (at, field_path, old_value, new_value, filter_version, "
            "scoring_version, source) VALUES (?, ?, '1', '2', 'f', 's', 'ui')",
            (at, path),
        )
    r = ev.evaluate(conn, make_profile(), prompt_version="v1")
    assert [c["field_path"] for c in r.profile_changes] == ["hard.salary_floor"]
    assert "hard.salary_floor" in r.to_text()


def seed_two_variants(conn):
    """v1@m1 scores 5 labeled groups; v2@m2 scores 3 of the positives and misses one of them."""
    for i in range(4):
        gid = add(conn, 90, "interesting", pv="v1", model="m1", cost=0.001)
        if i < 3:
            add(conn, 90 if i else 20, None, gid=gid, pv="v2", model="m2", cost=0.004)
    add(conn, 10, "not_interesting", pv="v1", model="m1", cost=0.001)


def test_compare_intersection_and_cost(conn):
    seed_two_variants(conn)
    c = ev.compare(conn, make_profile(), [ev.parse_variant("v1@m1"), ev.parse_variant("v2")])
    assert c.intersection == 3
    a, b = c.variants
    assert (a.n, b.n) == (3, 3)
    assert a.metrics["recall_bucket_ab"] == 1.0
    assert b.metrics["recall_bucket_ab"] == pytest.approx(2 / 3, abs=1e-4)
    assert a.cost_per_1000 == 1.0 and b.cost_per_1000 == 4.0
    text = c.to_text()
    assert "labeled: 3" in text and "cost per 1,000" in text and "v2@any" in text
    assert json.loads(c.to_json())["intersection"] == 3


def test_compare_needs_two_and_parse_variant(conn):
    with pytest.raises(ValueError):
        ev.compare(conn, make_profile(), [ev.VariantKey("v1")])
    k = ev.parse_variant("v3@claude-haiku-4-5")
    assert (k.prompt_version, k.model) == ("v3", "claude-haiku-4-5")
    assert ev.parse_variant("v3").model is None
    with pytest.raises(ValueError):
        ev.parse_variant("@m")


def test_json_roundtrip(conn):
    seed_hand_set(conn)
    r = ev.evaluate(conn, make_profile(), prompt_version="v1")
    data = json.loads(r.to_json())
    assert data["n"] == 10 and data["metrics"]["recall_bucket_ab"] == 0.6


# ─── CLI ────────────────────────────────────────────────────────────────────


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    pdir = tmp_path / "profile"
    pdir.mkdir()
    (pdir / "preferences.yaml").write_text(EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    resume = tmp_path / "resume.md"
    resume.write_text("Synthetic Person\n\n- Ran synthetic hosts.\n", encoding="utf-8")
    db_path = tmp_path / "jh.db"
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'[paths]\ndb_path = "{db_path}"\nprofile_dir = "{pdir}"\nresume_path = "{resume}"\n'
    )
    monkeypatch.setenv("JOBHUNTER_CONFIG", str(cfg))
    c = _new_db(db_path)
    seed_two_variants(c)
    c.close()


def test_cli_eval_text_and_json(cli_env):
    res = runner.invoke(app, ["eval", "--prompt-version", "v1"])
    assert res.exit_code == 0, res.output
    assert "insufficient labels" in res.output
    res = runner.invoke(app, ["eval", "--json", "--prompt-version", "v1", "--model", "m1"])
    assert res.exit_code == 0, res.output
    assert json.loads(res.output)["n"] == 5


def test_cli_eval_compare(cli_env):
    res = runner.invoke(app, ["eval", "--compare", "v1@m1", "v2@m2", "--json"])
    assert res.exit_code == 0, res.output
    assert json.loads(res.output)["intersection"] == 3
    assert runner.invoke(app, ["eval", "--compare", "v1"]).exit_code == 2
    assert runner.invoke(app, ["eval", "--tier", "bogus"]).exit_code == 2
