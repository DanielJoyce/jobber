"""/alerts subscription checklist on a tmp DB with a synthetic registry. No network."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from jobhunter.config import Mail, Paths, Settings
from jobhunter.console import alerts as a
from jobhunter.console.app import create_app
from jobhunter.core import db
from jobhunter.core.models import SourceRow
from jobhunter.scoring.profile import Profile, ProfileQuery
from jobhunter.sources.registry import sync_sources_table

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
ALERTS = "pat+jobs@example.com"
PREFS = """\
hard:
  states_allowed: [CO, WA]
  remote_ok: true
soft:
  state_ranking: [CO, WA]
resume_path: resume.md
target_titles: [Data Engineer, Platform Engineer]
queries:
  - keywords: [data engineer, etl]
    title: Analytics Lead
"""


def reg(key, state, *, policy="blocked", entry=None, cls="A", family="x"):
    return SourceRow.model_validate(
        {
            "key": key,
            "state": state,
            "class": cls,
            "name": f"Board {key}",
            "family": family,
            "tier": "http",
            "entry": entry or f"https://{key}.example.gov/",
            "policy": policy,
            "robots": {"status": "disallow_all", "checked": "2026-10-09"},
            "expect": {"min_jobs_per_week": 0},
        }
    )


REGISTRY = [
    reg("co-ok", "CO", policy="enabled"),
    reg("az-blocked", "AZ"),
    reg("tx-manual", "TX", policy="manual", entry="https://www.tx-board.example/"),
    reg("us-blocked", None, entry="https://nat-board.example/"),
    reg("nv-neogov", "NV", policy="enabled", cls="B", family="neogov"),
    reg("wa-disabled", "WA", policy="disabled"),
]
REGISTRY_KEYS = {r.key for r in REGISTRY}


@pytest.fixture
def profile_dir(tmp_path):
    d = tmp_path / "profile"
    d.mkdir()
    (d / "preferences.yaml").write_text(PREFS, encoding="utf-8")
    (d / "resume.md").write_text("Synthetic Person\n", encoding="utf-8")
    return d


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.migrate(c)
    sync_sources_table(c, REGISTRY)
    yield c
    c.close()


def make_app(tmp_path, profile_dir, mail=None):
    settings = Settings(
        paths=Paths(profile_dir=profile_dir, db_path=tmp_path / "t.db"),
        mail=mail or Mail(alerts_address=ALERTS),
    )
    app = create_app(settings, lambda: db.connect(tmp_path / "t.db"), clock=lambda: NOW)
    app.state.registry_loader = lambda: REGISTRY
    return app


@pytest.fixture
def client(tmp_path, profile_dir, conn):
    return TestClient(make_app(tmp_path, profile_dir))


def add_alert(conn, message_id, origin, received, sender="jobs.example.net"):
    conn.execute(
        "INSERT INTO mail_message (message_id, family, origin_source_key, sender_domain, "
        "subject, received_at, entries, processed_at) VALUES (?, 'generic', ?, ?, 'Jobs', ?, 1, ?)",
        (message_id, origin, sender, received.isoformat(), NOW.isoformat()),
    )


def checklist(conn, mail=None, profile=None, now=NOW):
    return a.build_checklist(
        conn, REGISTRY, mail or Mail(alerts_address=ALERTS), profile or Profile(), now
    )


def row(data, key):
    return next(r for r in data.rows if r.key == key)


# ─── rows ───────────────────────────────────────────────────────────────────


def test_rows_are_blocked_manual_and_neogov_b_only(conn):
    data = checklist(conn)
    assert {r.key for r in data.rows} == {"az-blocked", "tx-manual", "us-blocked", "nv-neogov"}
    assert row(data, "us-blocked").state is None
    assert row(data, "tx-manual").policy == "manual"


def test_page_lists_only_covered_rows(client):
    html = client.get("/alerts").text
    assert 'id="alert-az-blocked"' in html
    assert 'id="alert-tx-manual"' in html
    assert 'id="alert-co-ok"' not in html
    assert 'id="alert-wa-disabled"' not in html
    assert "/alerts" in html and "Alerts" in html


# ─── address ────────────────────────────────────────────────────────────────


def test_address_normal_uses_plus_address(conn):
    assert row(checklist(conn), "az-blocked").address == ALERTS
    assert row(checklist(conn), "az-blocked").plain_address is False


def test_address_fallback_domain_uses_plain_address(conn):
    mail = Mail(alerts_address=ALERTS, fallback_sender_domains=["@tx-board.example"])
    r = row(checklist(conn, mail=mail), "tx-manual")
    assert r.address == "pat@example.com"
    assert r.plain_address and r.in_config
    assert r.address_is_placeholder is False


def test_address_placeholder_shows_placeholder_text(conn):
    data = checklist(conn, mail=Mail())
    r = row(data, "az-blocked")
    assert data.placeholder is True
    assert r.address == a.PLACEHOLDER_ADDRESS
    assert r.address_is_placeholder is True


def test_placeholder_page_links_setup_docs(tmp_path, profile_dir, conn):
    client = TestClient(make_app(tmp_path, profile_dir, mail=Mail()))
    html = client.get("/alerts").text
    assert a.SETUP_DOC in html
    assert "mail.alerts_address" in html


# ─── criteria ───────────────────────────────────────────────────────────────


def test_suggested_criteria_from_profile():
    profile = Profile(
        target_titles=["Data Engineer", "Platform Engineer"],
        queries=[ProfileQuery(keywords=["data engineer", "etl"], title="Analytics Lead")],
    )
    assert a.suggest_criteria(profile) == [
        "data engineer",
        "etl",
        "Analytics Lead",
        "Platform Engineer",
    ]


def test_criteria_capped_and_empty_without_profile():
    many = Profile(target_titles=[f"Title {i}" for i in range(20)])
    assert len(a.suggest_criteria(many)) == a.MAX_KEYWORDS
    assert a.suggest_criteria(Profile()) == []


def test_row_carries_criteria_and_one_day_window(conn):
    profile = Profile(target_titles=["Data Engineer"])
    r = row(checklist(conn, profile=profile), "az-blocked")
    assert r.keywords == ["Data Engineer"]
    assert r.posted_within == "1 day"


def test_toml_line_only_for_rejected_domains_missing_from_config():
    mail = Mail(fallback_sender_domains=["@one.example.gov"])
    assert a.toml_line(mail, ["one.example.gov"]) is None
    assert (
        a.toml_line(mail, ["two.example.gov", "one.example.gov"])
        == 'fallback_sender_domains = ["one.example.gov", "two.example.gov"]'
    )
    assert a.toml_line(Mail(), []) is None


# ─── actions ────────────────────────────────────────────────────────────────


def test_subscribe_unsubscribe_persist(client, conn):
    r = client.post("/alerts/az-blocked/subscribe", headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert 'id="alerts-body"' in r.text
    stored = conn.execute(
        "SELECT subscribed_at, unsubscribed_at FROM alert_signup WHERE source_key='az-blocked'"
    ).fetchone()
    assert stored["subscribed_at"] == NOW.isoformat()
    assert stored["unsubscribed_at"] is None

    client.post("/alerts/az-blocked/unsubscribe", headers={"HX-Request": "true"})
    stored = conn.execute(
        "SELECT subscribed_at, unsubscribed_at FROM alert_signup WHERE source_key='az-blocked'"
    ).fetchone()
    assert stored["subscribed_at"] is None
    assert stored["unsubscribed_at"] == NOW.isoformat()


def test_plain_post_redirects_back_to_page(client):
    r = client.post("/alerts/az-blocked/subscribe", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/alerts"


def test_reject_plus_persists_domain_and_shows_toml(client, conn):
    r = client.post("/alerts/tx-manual/reject-plus", headers={"HX-Request": "true"})
    assert r.status_code == 200
    stored = conn.execute("SELECT domain, source_key FROM alert_plus_rejected").fetchall()
    assert [(s["domain"], s["source_key"]) for s in stored] == [("tx-board.example", "tx-manual")]
    assert "fallback_sender_domains = [" in r.text
    assert "tx-board.example" in r.text


def test_unknown_key_is_404(client):
    assert client.post("/alerts/co-ok/subscribe").status_code == 404
    assert client.post("/alerts/nope/reject-plus").status_code == 404


# ─── status, warnings, summary ──────────────────────────────────────────────


def test_stale_after_three_days_without_alerts(conn):
    a.mark_subscribed(conn, "az-blocked", NOW - timedelta(days=4))
    a.mark_subscribed(conn, "tx-manual", NOW - timedelta(days=2))
    data = checklist(conn)
    stale = row(data, "az-blocked")
    assert stale.status == a.STATUS_STALE
    assert stale.warning and "check spam" in stale.warning
    fresh = row(data, "tx-manual")
    assert fresh.status == a.STATUS_SUBSCRIBED
    assert fresh.warning is None


def test_stale_boundary_is_strictly_more_than_three_days(conn):
    a.mark_subscribed(conn, "az-blocked", NOW - timedelta(days=3))
    assert row(checklist(conn), "az-blocked").status == a.STATUS_SUBSCRIBED


def test_alert_since_subscribing_clears_stale_and_sets_last_seen(conn):
    a.mark_subscribed(conn, "az-blocked", NOW - timedelta(days=5))
    add_alert(conn, "m1", "az-blocked", NOW - timedelta(days=1))
    r = row(checklist(conn), "az-blocked")
    assert r.status == a.STATUS_RECEIVING
    assert r.warning is None
    assert r.last_alert_at == NOW - timedelta(days=1)
    assert r.alerts_since == 1


def test_alerts_before_subscribing_do_not_count(conn):
    add_alert(conn, "old", "az-blocked", NOW - timedelta(days=10))
    a.mark_subscribed(conn, "az-blocked", NOW - timedelta(days=4))
    r = row(checklist(conn), "az-blocked")
    assert r.status == a.STATUS_STALE
    assert r.last_alert_at == NOW - timedelta(days=10)


def test_summary_counts(conn):
    a.mark_subscribed(conn, "az-blocked", NOW - timedelta(days=1))
    a.mark_subscribed(conn, "tx-manual", NOW - timedelta(days=4))
    a.mark_subscribed(conn, "us-blocked", NOW - timedelta(days=2))
    add_alert(conn, "m1", "us-blocked", NOW - timedelta(hours=3))
    a.mark_unsubscribed(conn, "nv-neogov", NOW)
    s = checklist(conn).summary
    assert (s.total, s.subscribed, s.receiving, s.stale) == (4, 3, 1, 1)


def test_summary_rendered_on_page(client, conn):
    a.mark_subscribed(conn, "tx-manual", NOW - timedelta(days=4))
    html = client.get("/alerts").text
    assert "<strong>1 of 4</strong>" in html
    assert "<strong>0</strong> receiving alerts" in html
    assert "<strong>1</strong> stale" in html
    assert "check spam" in html


# ─── degrades without mail data ─────────────────────────────────────────────


def test_page_renders_without_mail_data(client):
    r = client.get("/alerts")
    assert r.status_code == 200
    assert "not subscribed" in r.text
    assert "none yet" in r.text
    assert "No profile yet" not in r.text  # the synthetic profile loads


def test_page_renders_without_profile(tmp_path, conn):
    settings = Settings(
        paths=Paths(profile_dir=tmp_path / "missing", db_path=tmp_path / "t.db"),
        mail=Mail(alerts_address=ALERTS),
    )
    app = create_app(settings, lambda: db.connect(tmp_path / "t.db"), clock=lambda: NOW)
    app.state.registry_loader = lambda: REGISTRY
    r = TestClient(app).get("/alerts")
    assert r.status_code == 200
    assert "No profile yet" in r.text
