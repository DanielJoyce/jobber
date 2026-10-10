from __future__ import annotations

import json
import re
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from jobhunter.config import Settings
from jobhunter.console import dashboard as dash
from jobhunter.console.app import create_app
from jobhunter.core import db, geo
from jobhunter.core.models import Bucket
from jobhunter.scoring.profile import Profile

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
PROFILE = Profile()

# Under the default profile (no salary floor, every state allowed, remote not ok) these give
# bucket A, B and G respectively.
DIMS_A = {"skills": 95, "seniority": 95, "domain": 95}
DIMS_B = {"skills": 70, "seniority": 70, "domain": 70}
DIMS_G = {"skills": 20, "seniority": 50, "domain": 50}
DIMS_D = {"skills": 40, "seniority": 10, "domain": 80}  # bucket D (lateral)


def ts(day: str) -> str:
    return f"{day}T08:00:00+00:00"


def add_source(conn, key, state, policy="enabled", status="ok", family="joblink", last_ok=None):
    conn.execute(
        "INSERT INTO source (key, state, class, name, family, tier, entry, policy, status) "
        "VALUES (?, ?, 'A', ?, ?, 'http', 'https://example.invalid/', ?, ?)",
        (key, state, f"{key} board", family, policy, status),
    )
    if last_ok:
        conn.execute(
            "INSERT INTO source_state (source_key, last_ok_at) VALUES (?, ?)", (key, last_ok)
        )


_seq = iter(range(1, 10_000))


def add_job(
    conn,
    *,
    states=(),
    scope="single",
    first_seen="2026-10-08",
    dims=None,
    salary=None,
    source="co-src",
):
    n = next(_seq)
    gid = conn.execute(
        "INSERT INTO job_group (method, created_at) VALUES ('exact_hash', ?)", (ts(first_seen),)
    ).lastrowid
    lo, hi = salary or (None, None)
    jid = conn.execute(
        "INSERT INTO job (source_key, external_id, job_group_id, url, title, salary_min, "
        "salary_max, salary_period, salary_stated, location_scope, stage, first_seen_at, "
        "last_seen_at) VALUES (?, ?, ?, 'https://example.invalid/j', 'Sysadmin', ?, ?, ?, ?, "
        "?, 'scored', ?, ?)",
        (
            source,
            f"x{n}",
            gid,
            lo,
            hi,
            "year" if salary else None,
            int(bool(salary)),
            scope,
            ts(first_seen),
            ts(first_seen),
        ),
    ).lastrowid
    conn.execute("UPDATE job_group SET canonical_job_id = ? WHERE id = ?", (jid, gid))
    for i, st in enumerate(states):
        conn.execute(
            "INSERT INTO job_locations (job_id, state, is_primary) VALUES (?, ?, ?)",
            (jid, st, int(i == 0)),
        )
    if scope in ("remote_us", "nationwide", "negotiable"):
        conn.execute("INSERT INTO job_locations (job_id, state) VALUES (?, NULL)", (jid,))
    if dims is not None:
        conn.execute(
            "INSERT INTO fit_score (job_group_id, tier, model, prompt_version, scoring_version, "
            "verdict, overall, dimensions, evidence, blockers, created_at) VALUES "
            "(?, 'screen', 'm', 'p', 's', 'possible', 50, ?, '[]', '[]', ?)",
            (gid, json.dumps(dims), ts(first_seen)),
        )
    return gid


def add_app(conn, gid, status, applied=None, events=(), next_action_at=None):
    aid = conn.execute(
        "INSERT INTO application (job_group_id, status, applied_at, next_action_at, created_at, "
        "updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        (
            gid,
            status,
            ts(applied) if applied else None,
            next_action_at,
            ts("2026-09-01"),
            ts("2026-09-01"),
        ),
    ).lastrowid
    for day, st in events:
        conn.execute(
            "INSERT INTO application_event (application_id, at, status) VALUES (?, ?, ?)",
            (aid, ts(day), st),
        )
    return aid


def seed(conn) -> dict[str, int]:
    add_source(conn, "co-src", "CO", last_ok=ts("2026-10-08"))
    add_source(conn, "wa-src", "WA", status="suspect")
    add_source(conn, "ny-src", "NY", status="broken")
    add_source(conn, "tx-src", "TX", policy="blocked", status="blocked")
    add_source(conn, "al-src", "AL", policy="blocked", status="blocked")
    add_source(conn, "or-ok", "OR")
    add_source(conn, "or-blocked", "OR", policy="blocked", status="blocked")
    add_source(conn, "usajobs", None, family="usajobs", last_ok=ts("2026-10-09"))

    g = {
        "co_a": add_job(conn, states=["CO"], dims=DIMS_A, salary=(100_000, 120_000)),
        "fed_b": add_job(
            conn,
            states=["CO", "WA", "VA"],
            scope="multi_state",
            first_seen="2026-10-07",
            dims=DIMS_B,
            salary=(90_000, 110_000),
            source="usajobs",
        ),
        "remote_a": add_job(conn, scope="remote_us", first_seen="2026-10-09", dims=DIMS_A),
        "tx_g": add_job(conn, states=["TX"], dims=DIMS_G),
        "co_old_a": add_job(conn, states=["CO"], first_seen="2026-09-01", dims=DIMS_A),
        "co_unscored": add_job(conn, states=["CO"]),
    }
    conn.execute(
        "INSERT INTO label (job_group_id, label, labeled_at) VALUES (?, 'interesting', ?)",
        (g["co_a"], ts("2026-10-08")),
    )

    # Six CO applications applied 2026-10-05: two employer responses, one bare acknowledgment.
    apps = [add_job(conn, states=["CO"], first_seen="2025-01-01", dims=DIMS_B) for _ in range(6)]
    add_app(
        conn,
        apps[0],
        "screening",
        "2026-10-05",
        [("2026-10-05", "applied"), ("2026-10-07", "screening")],
    )
    add_app(
        conn,
        apps[1],
        "rejected",
        "2026-10-05",
        [("2026-10-05", "applied"), ("2026-10-06", "rejected")],
        "2026-10-01",
    )
    add_app(
        conn,
        apps[2],
        "acknowledged",
        "2026-10-05",
        [("2026-10-05", "applied"), ("2026-10-05", "acknowledged")],
    )
    add_app(conn, apps[3], "applied", "2026-10-05", [("2026-10-05", "applied")], "2026-10-09")
    add_app(conn, apps[4], "applied", "2026-10-05", [("2026-10-05", "applied")], "2026-10-07")
    add_app(conn, apps[5], "applied", "2026-10-05", [("2026-10-05", "applied")], "2026-10-20")
    add_app(
        conn,
        g["fed_b"],
        "interview",
        "2026-10-06",
        [("2026-10-06", "applied"), ("2026-10-08", "interview")],
    )
    add_app(conn, g["remote_a"], "interested")

    conn.executemany(
        "INSERT INTO llm_spend (day, model, tier, calls, cost_usd) VALUES (?, 'm', 'screen', 1, ?)",
        [("2026-10-08", 1.5), ("2026-10-03", 0.7), ("2026-10-01", 5.0)],
    )
    return g


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.migrate(c)
    seed(c)
    yield c
    c.close()


def by_state(rows):
    return {r["state"]: r for r in rows}


# ─── KPIs ──────────────────────────────────────────────────────────────────


def test_kpis(conn):
    k = dash.kpis(conn, PROFILE, 7, NOW, weekly_cap_usd=10.0)
    assert k["days"][0] == "2026-10-03" and k["days"][-1] == "2026-10-09"
    assert k["new_ab"]["value"] == 3  # co_a, fed_b, remote_a; tx_g is G, co_old_a out of range
    assert k["new_ab"]["series"] == [0, 0, 0, 0, 1, 1, 1]
    assert k["new_ab"]["previous"] == 0
    assert k["in_flight"]["value"] == 6
    assert k["in_flight"]["series"][-1] == 6
    assert k["in_flight"]["series"][0] == 0
    assert k["followups"] == {"due": 2, "overdue": 1}
    rr = k["response_rate"]
    assert (rr["numerator"], rr["denominator"]) == (3, 7)
    assert rr["rate"] == pytest.approx(3 / 7)
    sp = k["llm_spend"]
    assert sp["week_usd"] == pytest.approx(2.2)
    assert sp["cap_usd"] == 10.0 and not sp["over_cap"]
    assert len(sp["series"]) == 7 and sp["series"][-2] == 1.5


def test_kpis_wider_range(conn):
    assert dash.kpis(conn, PROFILE, 90, NOW)["new_ab"]["value"] == 4


def test_kpis_empty_db():
    c = db.connect(":memory:")
    db.migrate(c)
    k = dash.kpis(c, PROFILE, 30, NOW)
    assert k["new_ab"]["value"] == 0
    assert k["in_flight"]["value"] == 0
    assert k["response_rate"]["rate"] is None
    assert k["response_rate"]["denominator"] == 0
    assert k["llm_spend"]["week_usd"] == 0


# ─── per-state stats ───────────────────────────────────────────────────────


def test_state_stats_rows_cover_all_subdivisions(conn):
    rows = dash.state_stats(conn, PROFILE, "new_ab", 7, NOW)
    assert [r["state"] for r in rows] == [*geo.US_SUBDIVISIONS, "REMOTE"]


def test_multi_state_job_counts_in_each_state_remote_once(conn):
    s = by_state(dash.state_stats(conn, PROFILE, "new_ab", 7, NOW))
    assert s["CO"]["new_ab"] == 2  # co_a + fed_b
    assert s["WA"]["new_ab"] == 1 and s["VA"]["new_ab"] == 1
    assert s["REMOTE"]["new_ab"] == 1
    assert sum(r["new_ab"] for r in s.values()) == 5  # fed_b counted 3x, remote once
    assert s["TX"]["new_ab"] == 0 and s["TX"]["scored"] == 0  # G is outside the default buckets
    assert s["CO"]["scored"] == 2  # co_unscored has no fit_score
    assert s["CO"]["shortlisted"] == 1
    assert s["CO"]["median_salary"] == 105_000
    assert s["CO"]["col_adjusted"] is None
    assert s["CO"]["value"] == 2


def test_response_rate_needs_five_applied(conn):
    s = by_state(dash.state_stats(conn, PROFILE, "response_rate", 7, NOW))
    assert s["CO"]["applied"] == 7 and s["CO"]["responses"] == 3
    assert s["CO"]["response_rate"] == pytest.approx(3 / 7)
    assert s["WA"]["applied"] == 1 and s["WA"]["response_rate"] is None
    assert s["WA"]["value"] is None
    assert s["REMOTE"]["applications"] == 1 and s["REMOTE"]["applied"] == 0
    assert s["CO"]["applications"] == 7


def test_coverage_status(conn):
    s = by_state(dash.state_stats(conn, PROFILE, "new_ab", 7, NOW))
    assert s["CO"]["coverage"] == "direct"
    assert s["CO"]["coverage_icon"] == "●"
    assert s["CO"]["sources"] == ["co-src board"]
    assert s["CO"]["last_ok_at"] == ts("2026-10-08")
    assert s["WA"]["coverage"] == "serious"
    assert s["NY"]["coverage"] == "critical"
    assert s["TX"]["coverage"] == "email"
    assert s["OR"]["coverage"] == "direct"  # one open board beats a blocked one
    assert s["MT"]["coverage"] == "none" and s["MT"]["sources"] == []
    assert s["REMOTE"]["coverage"] == "direct"  # national sources feed the Remote row


def test_coverage_email_stale_needs_a_mail_source(conn):
    add_source(conn, "mailalerts", None, family="mailalerts")
    add_job(conn, states=["AL"], first_seen="2026-10-08", source="mailalerts")
    cov = dash.coverage(conn, NOW)
    assert cov["AL"].status == "email"  # alert seen within 3 days
    assert cov["TX"].status == "email_stale"


def test_state_stats_counts_only_the_selected_buckets(conn):
    a = by_state(dash.state_stats(conn, PROFILE, "new_ab", 7, NOW, ("A",)))
    assert a["CO"]["new_ab"] == 1 and a["WA"]["new_ab"] == 0 and a["REMOTE"]["new_ab"] == 1
    assert a["CO"]["median_salary"] == 110_000  # only co_a's salary
    b = by_state(dash.state_stats(conn, PROFILE, "new_ab", 7, NOW, ("B",)))
    assert b["CO"]["new_ab"] == 1 and b["WA"]["new_ab"] == 1 and b["REMOTE"]["new_ab"] == 0
    g = by_state(dash.state_stats(conn, PROFILE, "new_ab", 7, NOW, ("G",)))
    assert g["TX"]["new_ab"] == 1 and g["CO"]["new_ab"] == 0
    both = by_state(dash.state_stats(conn, PROFILE, "new_ab", 7, NOW, ("A", "B")))
    assert both["CO"]["new_ab"] == 2


def test_state_stats_default_buckets_are_a_and_b(conn):
    default = by_state(dash.state_stats(conn, PROFILE, "new_ab", 7, NOW))
    explicit = by_state(dash.state_stats(conn, PROFILE, "new_ab", 7, NOW, ("A", "B")))
    assert {k: r["new_ab"] for k, r in default.items()} == {
        k: r["new_ab"] for k, r in explicit.items()
    }


def test_rows_carry_per_bucket_counts_and_totals_count_groups_once(conn):
    rows, totals = dash.state_stats_with_totals(conn, PROFILE, "new_ab", 7, NOW, ("A",))
    s = by_state(rows)
    assert s["CO"]["by_bucket"] == {"A": 1, "B": 1}
    assert s["WA"]["by_bucket"] == {"B": 1} and s["TX"]["by_bucket"] == {"G": 1}
    assert s["REMOTE"]["by_bucket"] == {"A": 1}
    assert totals == {"A": 2, "B": 1, "G": 1}  # fed_b is one group though listed in 3 states


def test_clamp_buckets():
    assert dash.clamp_buckets("c,a") == ("A", "C")
    assert dash.clamp_buckets("") == dash.clamp_buckets(None) == ("A", "B")
    assert dash.clamp_buckets("zz") == ("A", "B")


def test_map_payload_names_the_selection(conn):
    rows, totals = dash.state_stats_with_totals(conn, PROFILE, "new_ab", 7, NOW, ("B", "A"))
    body = dash.map_payload(rows, "new_ab", 7, ("A", "B"), totals)
    assert body["label"] == "New: Bullseye + Strong"
    assert body["buckets"] == ["A", "B"] and body["bucket_label"] == "Bullseye + Strong"
    assert body["bucket_totals"]["A"] == 2 and body["bucket_totals"]["C"] == 0
    other = dash.map_payload(rows, "scored", 7, ("A",), totals)
    assert other["label"] == "All scored jobs: Bullseye"


def test_class_breaks():
    assert dash.class_breaks([0, None, 1, 1, 2], "count") == [1.0, 2.0]
    assert dash.class_breaks([None, 0], "count") == []
    br = dash.class_breaks(range(1, 11), "count")
    assert len(br) == 5 and br[-1] == 10
    assert dash.class_breaks([0.25, 0.5], "rate")[-1] == 0.5


def test_sort_rows_none_last():
    rows = [
        {"name": "B", "response_rate": None, "coverage": "none"},
        {"name": "A", "response_rate": 0.1, "coverage": "direct"},
        {"name": "C", "response_rate": 0.3, "coverage": "critical"},
    ]
    names = lambda key, d: [r["name"] for r in dash.sort_rows(rows, key, d)]  # noqa: E731
    assert names("response_rate", "desc") == ["C", "A", "B"]
    assert names("response_rate", "asc") == ["A", "C", "B"]
    assert names("coverage", "desc") == ["C", "A", "B"]
    assert names("state", "asc") == ["A", "B", "C"]


# ─── routes ────────────────────────────────────────────────────────────────


@pytest.fixture
def client(tmp_path):
    path = tmp_path / "t.db"
    c = db.connect(path)
    db.migrate(c)
    seed(c)
    c.close()
    app = create_app(Settings(), lambda: db.connect(path), lambda: PROFILE, lambda: NOW)
    return TestClient(app)


def test_today_page_renders(client):
    r = client.get("/")
    assert r.status_code == 200
    html = r.text
    assert 'id="map"' in html
    assert "vendor/d3-7.9.0.min.js" in html
    assert "vendor/topojson-client-3.1.0.min.js" in html
    assert "vendor/us-atlas-3.0.1-states-albers-10m.json" in html
    assert "dashboard.js" in html
    assert 'id="charts"' in html
    assert 'data-kpi="new_ab">3<' in html
    assert "(3/7)" in html
    grid = re.search(r'id="tile-grid">(.*?)</script>', html, re.S)
    assert grid and json.loads(grid.group(1))["CO"] == [4, 3]
    assert 'data-state="CO"' in html


def test_vendored_assets_served(client):
    for name in (
        "d3-7.9.0.min.js",
        "topojson-client-3.1.0.min.js",
        "us-atlas-3.0.1-states-albers-10m.json",
    ):
        assert client.get(f"/static/vendor/{name}").status_code == 200
    atlas = client.get("/static/vendor/us-atlas-3.0.1-states-albers-10m.json").json()
    ids = {g["id"] for g in atlas["objects"]["states"]["geometries"]}
    assert {geo.by_fips(i).usps for i in ids} == set(geo.ALL_STATES_AND_DC)


def test_api_kpis(client):
    body = client.get("/api/dash/kpis?range=30").json()
    assert body["range"] == 30
    assert body["new_ab"]["value"] == 3
    assert client.get("/api/dash/kpis?range=5").json()["range"] == 7


def test_api_map(client):
    body = client.get("/api/dash/map?metric=new_ab&range=7").json()
    assert body["metric"] == "new_ab" and body["range"] == 7 and body["kind"] == "count"
    assert body["breaks"] == [1, 2]
    assert len(body["states"]) == len(geo.US_SUBDIVISIONS) + 1
    co = next(s for s in body["states"] if s["state"] == "CO")
    for key in (
        "name",
        "value",
        "new_ab",
        "scored",
        "shortlisted",
        "applied",
        "responses",
        "response_rate",
        "median_salary",
        "col_adjusted",
        "coverage",
        "coverage_label",
        "sources",
        "last_ok_at",
        "applications",
    ):
        assert key in co
    assert co["value"] == 2
    remote = next(s for s in body["states"] if s["state"] == "REMOTE")
    assert remote["name"] == "Remote (US)" and remote["value"] == 1


def test_api_map_unknown_metric_falls_back(client):
    assert client.get("/api/dash/map?metric=bogus").json()["metric"] == "new_ab"
    body = client.get("/api/dash/map?metric=response_rate&range=7").json()
    assert body["kind"] == "rate"
    assert body["breaks"] == [pytest.approx(3 / 7, abs=1e-3)]


def test_api_map_buckets_param(client):
    body = client.get("/api/dash/map?metric=new_ab&range=7&buckets=B").json()
    assert body["buckets"] == ["B"] and body["bucket_label"] == "Strong"
    co = next(s for s in body["states"] if s["state"] == "CO")
    assert co["value"] == 1 and co["by_bucket"] == {"A": 1, "B": 1}
    assert body["bucket_totals"]["A"] == 2
    default = client.get("/api/dash/map").json()
    assert default["buckets"] == ["A", "B"] and default["default_buckets"] == ["A", "B"]
    assert client.get("/api/dash/map?buckets=nonsense").json()["buckets"] == ["A", "B"]


def test_state_table_follows_buckets(client):
    html = client.get("/dash/state-table?metric=new_ab&range=7&buckets=B").text
    assert "New: Strong" in html and "New A" not in html
    assert "&amp;bucket=B" in html  # state links carry the selection into the inbox
    assert 'data-buckets="B"' in html
    row = re.search(r'<tr data-state="WA">.*?<td class="num">(\d+)</td>', html, re.S)
    assert row and row.group(1) == "1"
    assert "buckets=B" in html  # sort links keep the selection


def test_state_table_is_in_a_collapsed_details(client):
    html = client.get("/").text
    m = re.search(
        r'<details id="state-details">\s*<summary><h2[^>]*>State statistics \((\d+) rows', html
    )
    assert m and int(m.group(1)) == len(geo.US_SUBDIVISIONS) + 1
    assert '<details id="state-details" open' not in html


def test_today_page_renders_bucket_chips(client):
    html = client.get("/?buckets=A,C").text
    assert 'id="bucket-filter"' in html
    assert re.search(r'data-bucket="A" aria-pressed="true"', html)
    assert re.search(r'data-bucket="B" aria-pressed="false"', html)
    assert re.search(r'data-bucket="C" aria-pressed="true"', html)
    assert 'name="buckets" value="A,C"' in html
    assert "Stretch Up" in html and "Stale Match" in html
    assert "New: Bullseye + Stretch Up" in html
    assert 'id="bucket-names"' in html


def table_order(html: str) -> list[str]:
    return re.findall(r'<tr data-state="([A-Z]+)">', html)


def test_state_table_sort(client):
    r = client.get("/dash/state-table?metric=new_ab&range=7&sort=new_ab&dir=desc")
    assert r.status_code == 200
    order = table_order(r.text)
    assert order[0] == "CO"
    assert len(order) == len(geo.US_SUBDIVISIONS) + 1
    assert 'aria-sort="descending"' in r.text

    asc = table_order(client.get("/dash/state-table?sort=state&dir=asc").text)
    assert asc[0] == "AL"  # Alabama
    assert asc[-1] == "WY"  # Wyoming

    cov = table_order(client.get("/dash/state-table?sort=coverage&dir=desc").text)
    assert cov[0] == "NY"  # broken source first

    applied = table_order(client.get("/dash/state-table?sort=applied").text)
    assert applied[0] == "CO"


def test_state_table_coverage_icon_and_text(client):
    html = client.get("/dash/state-table").text
    assert "collected directly" in html
    assert "co-src board" in html
    assert "n=1" in html  # WA: too few applications to rate


# ─── NLx coverage (010 "NLx coverage") ─────────────────────────────────────


def test_coverage_via_nlx_when_national_source_ok(conn):
    add_source(conn, "us-nlx", None, family="nlx", last_ok=ts("2026-10-09"))
    s = by_state(dash.state_stats(conn, PROFILE, "new_ab", 7, NOW))
    assert s["TX"]["coverage"] == "direct" and s["TX"]["via_nlx"] is True
    assert s["TX"]["coverage_label"] == "collected directly via NLx"
    assert "National Labor Exchange (NLx)" in s["TX"]["sources"]
    assert s["MT"]["coverage"] == "direct" and s["MT"]["via_nlx"] is True  # no board at all
    assert s["CO"]["via_nlx"] is False and s["CO"]["coverage_label"] == "collected directly"
    assert s["WA"]["coverage"] == "serious"  # a state's own fault still shows
    assert s["NY"]["coverage"] == "critical"


def test_coverage_nlx_not_ok_changes_nothing(conn):
    add_source(conn, "us-nlx", None, family="nlx", status="broken")
    s = by_state(dash.state_stats(conn, PROFILE, "new_ab", 7, NOW))
    assert s["TX"]["coverage"] == "email" and s["TX"]["via_nlx"] is False
    assert s["MT"]["coverage"] == "none"


def test_state_table_says_via_nlx(tmp_path):
    path = tmp_path / "n.db"
    c = db.connect(path)
    db.migrate(c)
    seed(c)
    add_source(c, "us-nlx", None, family="nlx", last_ok=ts("2026-10-09"))
    c.commit()
    c.close()
    app = create_app(Settings(), lambda: db.connect(path), lambda: PROFILE, lambda: NOW)
    html = TestClient(app).get("/dash/state-table").text
    assert "collected directly via NLx" in html


# ─── chart series ──────────────────────────────────────────────────────────


def test_series_shapes(conn):
    s = dash.series(conn, PROFILE, 7, NOW)
    assert set(s) == {
        "range",
        "buckets",
        "bucket_label",
        "min_points",
        "line",
        "funnel",
        "mix",
        "status",
    }
    assert [p["day"] for p in s["line"]["points"]] == dash._days(NOW, 7)
    counts = {p["day"]: p["count"] for p in s["line"]["points"]}
    assert counts["2026-10-09"] == 1 and counts["2026-10-08"] == 1 and counts["2026-10-07"] == 1
    assert s["line"]["mean7"] == pytest.approx(3 / 7, abs=1e-3)
    assert [x["key"] for x in s["mix"]["slots"]] == ["a", "b", "other", "f"]
    assert len(s["mix"]["weeks"]) == 4
    assert [i["status"] for i in s["status"]["items"]] == list(dash.STATUS_ORDER)


def test_kpis_series_and_funnel_follow_the_selected_buckets(conn):
    only_b = dash.kpis(conn, PROFILE, 7, NOW, buckets=("B",))
    assert only_b["new_ab"]["value"] == 1 and only_b["new_ab"]["buckets"] == ["B"]
    assert dash.kpis(conn, PROFILE, 7, NOW)["new_ab"]["value"] == 3
    s = dash.series(conn, PROFILE, 30, NOW, ("A",))
    assert s["bucket_label"] == "Bullseye" and s["buckets"] == ["A"]
    assert sum(p["count"] for p in s["line"]["points"]) == 2
    ab = next(x for x in s["funnel"]["stages"] if x["key"] == "ab")
    assert ab["count"] == 2 and ab["label"] == "Bullseye"
    assert [x["key"] for x in s["mix"]["slots"]] == ["a", "b", "other", "f"]  # mix is not filtered


def test_dashboard_fragments_take_buckets(client):
    assert client.get("/api/dash/kpis?buckets=B").json()["new_ab"]["value"] == 1
    assert "New Strong" in client.get("/dash/kpis?buckets=B").text
    assert client.get("/api/dash/series?buckets=A").json()["bucket_label"] == "Bullseye"
    assert "New Bullseye + Strong" in client.get("/").text


def test_funnel_downstream_stages_stay_within_the_selection(conn):
    # Stale Match has no applications at all, so a narrow selection must not show any
    # shortlisted/applied/responded groups (that gave conversions above 100%).
    s = dash.series(conn, PROFILE, 30, NOW, ("F",))
    stages = {x["key"]: x["count"] for x in s["funnel"]["stages"]}
    assert stages["ab"] == 0
    assert stages["shortlisted"] == stages["applied"] == stages["responded"] == 0
    # Strong: the six CO applications and fed_b are all bucket B.
    b = {x["key"]: x for x in dash.series(conn, PROFILE, 30, NOW, ("B",))["funnel"]["stages"]}
    assert b["applied"]["count"] == 7 and b["shortlisted"]["count"] == 7  # co_a's label is A


def test_state_counts_other_than_new_follow_the_selection(conn):
    a = by_state(dash.state_stats(conn, PROFILE, "applied", 7, NOW, ("A",)))
    assert a["CO"]["applied"] == 0 and a["CO"]["applications"] == 0
    assert a["CO"]["shortlisted"] == 1 and a["CO"]["scored"] == 1  # co_a only
    b = by_state(dash.state_stats(conn, PROFILE, "applied", 7, NOW, ("B",)))
    assert b["CO"]["applied"] == 7 and b["CO"]["shortlisted"] == 0 and b["CO"]["scored"] == 1
    assert b["CO"]["response_rate"] is not None


def test_series_funnel_counts(conn):
    stages = {x["key"]: x for x in dash.series(conn, PROFILE, 30, NOW)["funnel"]["stages"]}
    assert list(stages) == [k for k, _ in dash.FUNNEL_STAGES]
    found = stages["found"]["count"]
    assert stages["prefilter"]["count"] == found  # the seed marks every job 'scored'
    assert stages["ab"]["count"] == 3
    assert stages["ab"]["pct_prev"] == pytest.approx(3 / found)
    assert stages["applied"]["count"] == 7
    assert stages["shortlisted"]["count"] == 1 + 7  # one label plus the seven applied groups
    assert stages["responded"]["count"] == 3  # screening, rejected, interview
    assert stages["interviewed"]["count"] == 1
    assert stages["offer"]["count"] == 0
    assert stages["found"]["pct_prev"] is None
    assert stages["offer"]["pct_prev"] == 0


def test_series_mix_folds_cde_and_excludes_g(conn):
    add_job(conn, states=["CO"], first_seen="2026-10-08", dims=DIMS_G)
    add_job(conn, states=["CO"], first_seen="2026-10-08", dims=DIMS_D)
    wk = dash.series(conn, PROFILE, 7, NOW)["mix"]["weeks"][-1]
    assert wk["week"] == "2026-10-09"
    assert wk["a"] == 2 and wk["b"] == 1  # co_a, remote_a; fed_b
    assert wk["other"] == 1  # the D job folds into other fits; the G job appears nowhere
    facts = [f for f in dash.group_facts(conn, PROFILE, "2026-10-03") if f.bucket is not None]
    assert wk["other"] == sum(1 for f in facts if f.bucket in (Bucket.C, Bucket.D, Bucket.E))
    assert wk["f"] == sum(1 for f in facts if f.bucket == Bucket.F)
    total = sum(wk[k] for k in ("a", "b", "other", "f"))
    assert total == sum(1 for f in facts if f.bucket != Bucket.G)


def test_series_status_counts(conn):
    items = {i["status"]: i["count"] for i in dash.series(conn, PROFILE, 7, NOW)["status"]["items"]}
    assert items["applied"] == 3 and items["interview"] == 1 and items["interested"] == 1
    assert items["acknowledged"] == 1 and items["screening"] == 1 and items["rejected"] == 1
    assert items["offer"] == 0


def test_series_not_enough_data():
    c = db.connect(":memory:")
    db.migrate(c)
    s = dash.series(c, PROFILE, 7, NOW)
    for key in ("line", "funnel", "mix", "status"):
        assert s[key]["enough"] is False
    assert s["min_points"] == 3
    c.close()


def test_mix_chart_slots_are_named_not_lettered(conn):
    s = dash.series(conn, PROFILE, 7, NOW)
    assert [(x["key"], x["label"]) for x in s["mix"]["slots"]] == [
        ("a", "Bullseye"),
        ("b", "Strong"),
        ("other", "Other fits"),
        ("f", "Stale match"),
    ]


def test_series_enough_flag_needs_three_points(conn):
    s = dash.series(conn, PROFILE, 7, NOW)
    assert s["line"]["enough"] is True  # 3 days with A+B
    assert s["status"]["enough"] is True  # 6 distinct statuses


def test_api_series(client):
    r = client.get("/api/dash/series?range=30")
    assert r.status_code == 200
    body = r.json()
    assert body["range"] == 30 and len(body["line"]["points"]) == 30
    assert client.get("/api/dash/series?range=5").json()["range"] == 7


def test_today_page_has_chart_panels(client):
    html = client.get("/").text
    for cid in ("chart-line", "chart-funnel", "chart-mix", "chart-status"):
        assert f'id="{cid}"' in html
    assert "charts.js" in html
    assert client.get("/static/charts.js").status_code == 200


# ─── tokens ────────────────────────────────────────────────────────────────


def _luminance(hex_color: str) -> float:
    r, g, b = (int(hex_color[i : i + 2], 16) / 255 for i in (1, 3, 5))
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in (r, g, b)]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def _contrast(a: str, b: str) -> float:
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def test_dark_sequential_ramp_tokens(client):
    css = client.get("/static/app.css").text
    start = css.index("@media (prefers-color-scheme: dark)")
    forced_at = css.index(':root[data-theme="dark"]')
    scopes = (css[start:forced_at], css[forced_at : css.index("* { box-sizing")])
    for scope in scopes:
        ramp = dict(re.findall(r"--seq-(\d+): (#[0-9a-f]{6});", scope))
        assert {"100", "350", "700"} <= set(ramp)
        surface = "#1a1a19"
        assert _contrast(ramp["100"], surface) >= 1.5
        lum = [_luminance(ramp[k]) for k in sorted(ramp, key=int)]
        assert lum == sorted(lum)  # dark ramp climbs: low recedes, high is brightest
        assert _contrast(ramp["700"], surface) > 8


def test_link_tokens_are_readable_in_every_theme(client):
    css = client.get("/static/app.css").text
    # light :root, the dark media-query block, and the forced-dark block
    dark_at = css.index("@media (prefers-color-scheme: dark)")
    forced_at = css.index(':root[data-theme="dark"]')
    scopes = {
        "light": css[:dark_at],
        "dark": css[dark_at:forced_at],
        "forced dark": css[forced_at : css.index("* { box-sizing")],
    }
    for name, scope in scopes.items():
        tokens = dict(re.findall(r"--([\w-]+): (#[0-9a-f]{6});", scope))
        for link in ("link", "link-visited"):
            for ground in ("surface-1", "page"):
                assert _contrast(tokens[link], tokens[ground]) >= 4.5, (name, link, ground)
    # Bare links use the tokens; the browser default is what was unreadable in dark mode.
    assert re.search(r"^a \{ color: var\(--link\); \}", css, re.M)
    assert "a:where(:visited) { color: var(--link-visited); }" in css


# ─── outcomes Sankey ───────────────────────────────────────────────────────


def _set_stage(conn, gid, stage, passed=None):
    jid = conn.execute("SELECT canonical_job_id FROM job_group WHERE id = ?", (gid,)).fetchone()[0]
    conn.execute("UPDATE job SET stage = ? WHERE id = ?", (stage, jid))
    if passed is not None:
        conn.execute(
            "INSERT INTO prefilter_result (job_id, passed, reasons, filter_version, evaluated_at) "
            "VALUES (?, ?, '[]', 'v', ?)",
            (jid, passed, ts("2026-10-08")),
        )
    return jid


def _manual_app(conn, status="applied", applied="2026-10-05"):
    conn.execute(
        "INSERT OR IGNORE INTO source (key, class, name, family, tier, entry, policy, status) "
        "VALUES ('email-manual', 'C', 'Added from email', 'manual', 'manual', 'gmail', "
        "'manual', 'manual')"
    )
    gid = add_job(conn, source="email-manual", first_seen="2026-10-05")
    add_app(conn, gid, status, applied, [(applied, "applied")] if applied else [])
    return gid


def _links(sk):
    return {(link["source"], link["target"]): link["value"] for link in sk["links"]}


def _assert_conserved(sk):
    inflow: dict[str, int] = {}
    outflow: dict[str, int] = {}
    for link in sk["links"]:
        assert link["value"] > 0
        outflow[link["source"]] = outflow.get(link["source"], 0) + link["value"]
        inflow[link["target"]] = inflow.get(link["target"], 0) + link["value"]
    values = {n["id"]: n["value"] for n in sk["nodes"]}
    assert all(v > 0 for v in values.values())
    for node_id, out in outflow.items():
        assert out == values[node_id]
        if node_id in inflow:  # not a source column
            assert inflow[node_id] == out, node_id
    for node_id, inn in inflow.items():
        assert values[node_id] == inn


def test_sankey_counts_and_conservation(conn):
    seeded = conn.execute("SELECT COUNT(*) FROM job_group").fetchone()[0]
    # Add: a prefiltered-out group, one awaiting prefilter, a dismissed one, a manual app.
    out = add_job(conn, states=["CO"])
    _set_stage(conn, out, "prefiltered", passed=0)
    waiting = add_job(conn, states=["CO"])
    _set_stage(conn, waiting, "grouped")
    nay = add_job(conn, states=["TX"], dims=DIMS_G)
    conn.execute(
        "INSERT INTO label (job_group_id, label, labeled_at) VALUES (?, 'not_interesting', ?)",
        (nay, ts("2026-10-08")),
    )
    _manual_app(conn)
    sk = dash.sankey(conn, PROFILE)
    _assert_conserved(sk)
    links = _links(sk)
    fetched_total = seeded + 3
    assert sk["total"] == fetched_total + 1
    assert links[("fetched", "prefiltered_out")] == 1
    assert links[("fetched", "awaiting_prefilter")] == 1
    assert links[("fetched", "passed")] == fetched_total - 2
    assert links[("passed", "unscored")] >= 1  # co_unscored (stage 'scored', no fit_score)
    assert links[("bucket_G", "dismissed")] == 1
    assert links[("elsewhere", "applied")] == 1
    assert links[("shortlisted", "not_applied")] == 2  # co_a (label only) and remote_a
    assert links[("applied", "interview")] == 2  # screening + interview
    assert links[("applied", "rejected")] == 1
    assert links[("applied", "awaiting")] == 5  # 3 applied, 1 acknowledged, the manual one
    node_ids = {n["id"] for n in sk["nodes"]}
    assert "offer" not in node_ids and "closed" not in node_ids  # zero-value nodes hidden


def test_sankey_extra_rejected_hook_moves_to_rejected(conn):
    base = _links(dash.sankey(conn, PROFILE))
    gid = conn.execute(
        "SELECT job_group_id FROM application WHERE status = 'acknowledged'"
    ).fetchone()[0]
    sk = dash.sankey(conn, PROFILE, extra_rejected={gid})
    _assert_conserved(sk)
    links = _links(sk)
    assert links[("applied", "rejected")] == base[("applied", "rejected")] + 1
    assert links[("applied", "awaiting")] == base[("applied", "awaiting")] - 1


def test_sankey_empty_database():
    c = db.connect(":memory:")
    db.migrate(c)
    assert dash.sankey(c, PROFILE) == {"nodes": [], "links": [], "total": 0}


def test_sankey_node_links_point_at_real_lists(conn):
    nodes = {n["id"]: n for n in dash.sankey(conn, PROFILE)["nodes"]}
    assert nodes["applied"]["href"] == "/pipeline"
    assert nodes["fetched"]["href"] is None
    assert any(n["href"] == "/inbox?bucket=A" for n in nodes.values())


def test_today_page_embeds_sankey_payload(client):
    html = client.get("/").text
    m = re.search(r'id="sankey-data">(.*?)</script>', html, re.S)
    assert m
    payload = json.loads(m.group(1))
    assert payload["nodes"] and payload["links"]
    _assert_conserved(payload)
    assert 'id="chart-sankey"' in html


def test_sankey_rejected_without_application_was_applied_elsewhere(conn):
    base = _links(dash.sankey(conn, PROFILE))
    gid = add_job(conn, states=["CO"])
    sk = dash.sankey(conn, PROFILE, extra_rejected={gid})
    _assert_conserved(sk)
    links = _links(sk)
    assert links[("elsewhere", "applied")] == base.get(("elsewhere", "applied"), 0) + 1
    assert links[("applied", "rejected")] == base[("applied", "rejected")] + 1


def test_sankey_unmatched_email_rejections_flow_from_elsewhere(conn):
    base = _links(dash.sankey(conn, PROFILE))
    sk = dash.sankey(conn, PROFILE, elsewhere_rejected=3)
    _assert_conserved(sk)
    links = _links(sk)
    assert links[("elsewhere", "applied")] == base.get(("elsewhere", "applied"), 0) + 3
    assert links[("applied", "rejected")] == base[("applied", "rejected")] + 3


def test_today_page_wires_the_rejection_table_into_sankey(client, tmp_path):
    from jobhunter.core import rejections

    conn = db.connect(tmp_path / "t.db")
    rejections.record(
        conn,
        received_at="2026-10-01T12:00:00+00:00",
        employer="Seeq",
        title="Platform Engineer",
        source="email",
        now=datetime(2026, 10, 9, tzinfo=UTC),
        gmail_message_id="m1",
    )
    conn.commit()
    conn.close()
    html = client.get("/").text
    payload = json.loads(re.search(r'id="sankey-data">(.*?)</script>', html, re.S).group(1))
    links = {(x["source"], x["target"]): x["value"] for x in payload["links"]}
    assert links[("elsewhere", "applied")] >= 1
    _assert_conserved(payload)
