"""Setup banner on / and /inbox, and the first-run redirect (specs/019 S3b).
Synthetic data only; no network."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from test_setup import NOW, _fresh, make_client, make_settings  # noqa: F401

from jobhunter import readiness as rd
from jobhunter.console import setup_routes as sr
from jobhunter.core import db


def check(cid, level, status=rd.MISSING, due=False, title=""):
    return rd.Check(
        id=cid,
        group=rd.BASICS,
        level=level,
        status=status,
        due=due,
        title=title or cid,
        detail="",
        unlocks="",
    )


@pytest.fixture
def client(tmp_path):
    return make_client(make_settings(tmp_path))


def add_job(client) -> None:
    conn = db.connect(client.app.state.settings.paths.db_path)
    conn.execute(
        "INSERT OR IGNORE INTO source "
        "(key, state, class, name, family, tier, entry, policy, status) "
        "VALUES ('s', 'live', 'A', 's', 'joblink', 'http', 'https://example.invalid/', "
        "'enabled', 'ok')"
    )
    conn.execute(
        "INSERT INTO job (source_key, external_id, url, title, stage, first_seen_at, "
        "last_seen_at) VALUES ('s', '1', 'https://example.com/1', 'T', 'listed', ?, ?)",
        (NOW.isoformat(), NOW.isoformat()),
    )
    conn.commit()
    conn.close()


def fake(monkeypatch, checks):
    monkeypatch.setattr(sr.rd, "run_checks", lambda *a, **k: checks)


def test_empty_database_with_failing_essential_redirects_to_setup(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/setup"


def test_redirect_stops_once_a_job_exists_and_banner_remains(client):
    add_job(client)
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 200
    assert 'id="setup-banner"' in r.text
    assert "Open setup" in r.text
    assert 'action="/setup/dismiss"' not in r.text


def test_no_redirect_when_essentials_ok_even_if_empty(client, monkeypatch):
    fake(monkeypatch, [check("a", rd.RECOMMENDED)])
    assert client.get("/", follow_redirects=False).status_code == 200


def test_inbox_never_redirects_and_shows_the_one_banner(client):
    r = client.get("/inbox", follow_redirects=False)
    assert r.status_code == 200
    assert r.text.count('id="setup-banner"') == 1
    assert "No profile yet" not in r.text


def test_essentials_line_has_no_hide_and_names_titles(client, monkeypatch):
    fake(
        monkeypatch,
        [
            check("a", rd.ESSENTIAL, title="Resume"),
            check("b", rd.RECOMMENDED, title="Rec"),
        ],
    )
    add_job(client)
    html = client.get("/").text
    assert "Resume" in html and "Rec" not in html
    assert 'action="/setup/dismiss"' not in html
    assert 'role="alert"' in html


def test_recommended_line_hide_and_return_on_status_change(client, monkeypatch):
    settings = client.app.state.settings
    checks = [
        check("a", rd.RECOMMENDED, title="Alpha"),
        check("b", rd.RECOMMENDED, rd.OK, due=True, title="Beta"),
        check("c", rd.OPTIONAL, title="Opt"),
        check("d", rd.INFO, rd.UNKNOWN, title="Inf"),
    ]
    fake(monkeypatch, checks)
    html = client.get("/inbox").text
    assert "Alpha" in html and "Beta (due)" in html
    assert "Opt" not in html and "Inf" not in html
    assert 'name="check_id" value="a"' in html and 'name="check_id" value="b"' in html
    path = Path(settings.paths.data_dir) / sr.DISMISSED_FILE
    path.write_text(json.dumps({"dismissed": {"a": {"status": "missing"}, "b": {"status": "due"}}}))
    assert 'id="setup-banner"' not in client.get("/inbox").text
    # a changes status: it returns, b stays hidden
    fake(monkeypatch, [replace(checks[0], status=rd.BROKEN), checks[1]])
    html = client.get("/").text
    assert "Alpha" in html and "Beta" not in html


def test_banner_view_unit():
    assert sr.banner_view([check("a", rd.OPTIONAL)], {}) is None
    v = sr.banner_view([check("a", rd.ESSENTIAL), check("b", rd.RECOMMENDED)], {})
    assert [c.id for c in v["essentials"]] == ["a"] and v["ids"] == []


def test_hide_returns_to_the_page_only_for_known_paths(client):
    cid = next(
        c.id
        for c in rd.run_checks(
            client.app.state.settings, secrets_report=None, now=NOW, mode=rd.HOST
        )
        if c.level == rd.RECOMMENDED and c.status in rd.FAILING
    )
    r = client.post(
        "/setup/dismiss", data={"check_id": cid, "next": "/inbox"}, follow_redirects=False
    )
    assert r.status_code == 303 and r.headers["location"] == "/inbox"
    r = client.post(
        "/setup/dismiss",
        data={"check_id": cid, "next": "https://evil.example"},
        follow_redirects=False,
    )
    assert r.headers["location"].startswith("/setup")


def test_banner_survives_a_crashing_check(client, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(sr.rd, "run_checks", boom)
    assert client.get("/inbox").status_code == 200
    assert client.get("/", follow_redirects=False).status_code == 200


def test_banner_css_dark_safe_and_phone_width():
    css = (Path(sr.__file__).parent / "static" / "app.css").read_text()
    block = css[css.index(".setup-banner {") :]
    assert "var(--" in block and "flex-wrap: wrap" in block and "#fff" not in block
