"""/setup live checks (specs/019 S6): the loopback-peer gate, the buttons, the fragments, and
that no result is stored or leaks a value. Every service is faked (tests/live_fakes.py)."""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import UTC, datetime
from html import unescape
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jobhunter import live_checks as lc
from jobhunter import readiness
from jobhunter.config import Settings, load_settings
from jobhunter.console.app import create_app
from jobhunter.core import db
from live_fakes import AUTH_WITH_IDENTITY, LEAK, Recorder, anthropic_factory, deps

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
LOOPBACK = ("127.0.0.1", 50123)
LAN = ("192.168.1.20", 50123)
HX = {"hx-request": "true"}


@pytest.fixture(autouse=True)
def _keys(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", f"sk-ant-{LEAK}")
    monkeypatch.setenv("OPENROUTER_API_KEY", f"sk-or-{LEAK}")
    readiness.clear_caches()
    yield
    readiness.clear_caches()


def make_settings(tmp_path: Path, **sections: dict) -> Settings:
    data = tmp_path / "data"
    client_json = tmp_path / "google_client.json"
    client_json.write_text('{"installed": {"client_id": "x"}}', encoding="utf-8")
    overrides: dict = {
        "paths": {
            "data_dir": str(data),
            "db_path": str(data / "jobhunter.db"),
            "profile_dir": str(data / "profile"),
            "resume_path": str(data / "resume"),
            "cache_dir": str(tmp_path / "cache"),
        },
        "fetch": {"user_agent_contact": f"{LEAK}-contact@example.org"},
        "mail": {
            "alerts_address": f"{LEAK}+jobs@example.org",
            "client_secrets_path": str(client_json),
        },
    }
    for name, values in sections.items():
        overrides.setdefault(name, {}).update(values)
    return load_settings(tmp_path / "no-config.toml", overrides=overrides)


def make_app(settings: Settings, rec: Recorder, *, mode=readiness.HOST, allow_remote=False):
    Path(settings.paths.data_dir).mkdir(parents=True, exist_ok=True)
    conn = db.connect(settings.paths.db_path)
    db.migrate(conn)
    conn.close()
    app = create_app(
        settings,
        lambda: db.connect(settings.paths.db_path),
        clock=lambda: NOW,
        allow_remote=allow_remote,
    )
    app.state.setup_mode = mode
    app.state.live_deps = deps(rec, settings)
    ticks = iter(range(100, 100_000, 60))  # each reading is a minute on: no cooldown by default
    app.state.live_clock = lambda: float(next(ticks))
    return app


@pytest.fixture
def rec():
    return Recorder()


@pytest.fixture
def settings(tmp_path):
    return make_settings(tmp_path)


def client(app, peer=LOOPBACK, **kw) -> TestClient:
    return TestClient(app, client=peer, **kw)


def row(html: str, check_id: str) -> str:
    ident = "row-" + check_id.replace(".", "-")
    match = re.search(rf'<article class="su-row[^"]*" id="{ident}".*?</article>', html, re.S)
    assert match, f"no row {check_id}"
    return match.group(0)


def text_of(fragment: str) -> str:
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", fragment)).split())


def digest(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file() and not p.name.endswith(("-wal", "-shm"))
    }


# --- the gate ----------


def test_non_loopback_peer_is_refused_even_with_a_loopback_host(settings, rec):
    c = client(make_app(settings, rec), peer=LAN)
    r = c.post("/setup/live/scorer.screen", headers={"host": "127.0.0.1:8808"})
    assert r.status_code == 403
    assert rec.seen == []


def test_allow_remote_refuses_every_live_request(settings, rec):
    app = make_app(settings, rec, allow_remote=True)
    for peer in (LOOPBACK, LAN):
        assert client(app, peer=peer).post("/setup/live/scorer.screen").status_code == 403
    assert rec.seen == []


def test_unknown_id_and_row_without_a_check_are_404(settings, rec):
    c = client(make_app(settings, rec))
    assert c.post("/setup/live/nope").status_code == 404
    assert c.post("/setup/live/profile.resume").status_code == 404
    assert rec.seen == []


def test_cross_origin_post_is_refused(settings, rec):
    c = client(make_app(settings, rec))
    r = c.post("/setup/live/scorer.screen", headers={"origin": "http://evil.example"})
    assert r.status_code == 403 and rec.seen == []


# --- buttons ----------


def test_loopback_peer_sees_test_free_buttons(settings, rec):
    html = client(make_app(settings, rec)).get("/setup?all=1").text
    for check_id in ("scorer.screen", "mail.token", "mail.label"):
        r = row(html, check_id)
        assert f'hx-post="/setup/live/{check_id}"' in r
        assert "Test (free)" in r
    assert "use Test (free)" in text_of(row(html, "mail.label"))  # the S3a wording, restored
    assert "Test (free)" not in row(html, "profile.resume")
    assert rec.seen == []  # rendering the page contacts nothing


@pytest.mark.parametrize("allow_remote,peer", [(False, LAN), (True, LOOPBACK)])
def test_no_buttons_without_a_loopback_peer(settings, rec, allow_remote, peer):
    html = client(make_app(settings, rec, allow_remote=allow_remote), peer=peer).get("/setup?all=1")
    main = html.text.split("<main>", 1)[1]
    assert "/setup/live/" not in main
    assert "Test (free)" not in text_of(main)
    assert "jobhunter doctor --live" in text_of(row(html.text, "scorer.screen"))
    assert "run jobhunter doctor --live" in text_of(row(html.text, "mail.label"))


def test_container_console_points_gmail_at_jobhunter_ctr(settings, rec):
    html = client(make_app(settings, rec, mode=readiness.CONTAINER)).get("/setup?all=1").text
    gmail = row(html, "mail.label")
    assert "/setup/live/mail.label" not in gmail
    assert "jobhunter-ctr mail status" in text_of(gmail)
    assert "/setup/live/scorer.screen" in row(html, "scorer.screen")


# --- results ----------


def test_click_returns_booleans_and_calls_no_model(settings, rec, caplog):
    caplog.set_level(logging.DEBUG)
    r = client(make_app(settings, rec)).post("/setup/live/scorer.screen", headers=HX)
    assert r.status_code == 200
    t = text_of(r.text)
    assert "Anthropic credential" in t and "accepted: yes" in t
    assert "<html" not in r.text  # a fragment
    assert rec.seen == ["api.anthropic.com /v1/models"]
    assert rec.model_calls() == []
    assert LEAK not in r.text and LEAK not in caplog.text


def test_every_live_row_leaks_nothing(settings, rec, fake_claude, caplog):
    caplog.set_level(logging.DEBUG)
    fake_claude.set(auth=AUTH_WITH_IDENTITY)
    c = client(make_app(settings, rec))
    for check_id in ("scorer.screen", "scorer.deep", "mail.token", "mail.label", "apply.runner"):
        r = c.post(f"/setup/live/{check_id}", headers=HX)
        assert r.status_code == 200, check_id
        assert LEAK not in r.text, check_id
    assert "claude.ai login (not an API key)" in text_of(r.text)
    assert LEAK not in c.get("/setup?all=1").text
    assert LEAK not in caplog.text
    assert rec.model_calls() == []
    assert fake_claude.calls(auth=False) == []


def test_refused_key_renders_fixed_text(settings, rec):
    app = make_app(settings, rec)
    app.state.live_deps.anthropic_client = anthropic_factory(rec, status=401)
    r = client(app).post("/setup/live/scorer.screen", headers=HX)
    t = text_of(r.text)
    assert "accepted: no" in t and "the credential was refused" in t
    assert LEAK not in r.text


def test_result_is_not_stored(settings, rec):
    app = make_app(settings, rec)
    c = client(app)
    data = Path(settings.paths.data_dir)
    before = digest(data)
    c.post("/setup/live/mail.token", headers=HX)
    assert digest(data) == before
    assert "token accepted" not in c.get("/setup?all=1").text


def test_plain_form_post_renders_the_page_with_the_result(settings, rec):
    r = client(make_app(settings, rec)).post("/setup/live/mail.label")
    assert r.status_code == 200 and "<h1>Setup</h1>" in r.text
    assert "token accepted: yes" in text_of(row(r.text, "mail.label"))


def test_a_second_click_inside_the_cooldown_sends_nothing(settings, rec):
    app = make_app(settings, rec)
    app.state.live_clock = lambda: 1000.0  # time stands still
    c = client(app)
    assert c.post("/setup/live/scorer.screen").status_code == 200
    seen = list(rec.seen)
    assert seen  # the first click contacted the service
    # the same row, and another row sharing the service, are refused without a request
    assert c.post("/setup/live/scorer.screen").status_code == 429
    assert c.post("/setup/live/scorer.deep").status_code == 429
    assert rec.seen == seen
    # a different service is not held up by it
    assert c.post("/setup/live/mail.token").status_code == 200


def test_held_back_click_shows_a_fragment_not_json(settings, rec):
    app = make_app(settings, rec)
    app.state.live_clock = lambda: 1000.0
    c = client(app)
    assert c.post("/setup/live/mail.token").status_code == 200
    r = c.post("/setup/live/mail.label", headers={"hx-request": "true"})
    assert r.status_code == 429
    assert "su-live-result" in r.text and "detail" not in r.text
    assert "Checked moments ago; try again in 5 s." in r.text


def test_held_back_plain_post_renders_the_page(settings, rec):
    app = make_app(settings, rec)
    app.state.live_clock = lambda: 1000.0
    c = client(app)
    assert c.post("/setup/live/mail.token").status_code == 200
    r = c.post("/setup/live/mail.label")
    assert r.status_code == 429
    assert "<html" in r.text and "Checked moments ago" in r.text
    assert '{"detail"' not in r.text


def test_the_cooldown_resets_after_an_exception(settings, rec, monkeypatch):
    app = make_app(settings, rec)
    now = [1000.0]
    app.state.live_clock = lambda: now[0]
    c = client(app, raise_server_exceptions=False)
    real = lc.run_row

    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(lc, "run_row", boom)
    assert c.post("/setup/live/scorer.screen").status_code == 500
    monkeypatch.setattr(lc, "run_row", real)
    now[0] += 6  # not stuck "running": only the short cooldown applied
    assert c.post("/setup/live/scorer.screen").status_code == 200


def test_a_click_while_the_check_is_running_is_refused(settings, rec):
    from jobhunter.console import setup_routes

    app = make_app(settings, rec)
    c = client(app)
    app.state.live_busy = {"anthropic": setup_routes.INFLIGHT}
    assert c.post("/setup/live/scorer.screen").status_code == 429
    assert rec.seen == []


def test_the_cooldown_ends(settings, rec):
    app = make_app(settings, rec)
    now = [1000.0]
    app.state.live_clock = lambda: now[0]
    c = client(app)
    assert c.post("/setup/live/scorer.screen").status_code == 200
    now[0] += 6
    assert c.post("/setup/live/scorer.screen").status_code == 200


# --- Console Restart: a live check is registered work (019 S12b) ----------------------------


def test_a_live_check_is_registered_while_it_runs(settings, rec, monkeypatch):
    app = make_app(settings, rec)
    seen: list[list[str]] = []
    real = lc.run_row

    def recording(*a, **k):
        seen.append(app.state.work.labels())
        return real(*a, **k)

    monkeypatch.setattr(lc, "run_row", recording)
    r = client(app).post("/setup/live/scorer.screen", headers=HX)
    assert r.status_code == 200
    assert len(seen) == 1 and len(seen[0]) == 1 and seen[0][0].startswith("a live check (")
    assert app.state.work.labels() == []


def test_a_live_check_is_503_while_restarting(settings, rec, monkeypatch):
    app = make_app(settings, rec)
    monkeypatch.setattr(lc, "run_row", lambda *a, **k: pytest.fail("no check while restarting"))
    assert app.state.work.begin_restart() == []
    r = client(app).post("/setup/live/scorer.screen", headers=HX)
    assert r.status_code == 503 and "console restarting" in r.text
    assert r.headers["X-Jobhunter-Restarting"] == "1"
