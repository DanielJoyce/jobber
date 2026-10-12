"""The console sees models.toml (specs/020 M2): a blocked screening use refuses the re-score
panel and the extension's Score now, and an invalid drafting key shows its notice on the
packet page. No network, no credits: scorer and client factories fail the test."""

from __future__ import annotations

import pytest
from test_ext_api import aid, captured, client, conn, env, post, token  # noqa: F401
from test_packet_documents import env as packet_env  # noqa: F401
from test_rescore_routes import db_path, make_client, pdir, priced_test_model  # noqa: F401

from jobhunter.config import load_settings

MESSAGE = "models.toml screen_scorer is invalid"


def loaded_settings(tmp_path, monkeypatch, settings, models: str):
    """``settings`` as ``load_settings`` builds it with this models.toml beside it."""
    data = tmp_path / "models-data"
    data.mkdir(exist_ok=True)
    (data / "models.toml").write_text(models)
    p = settings.paths
    cfg = tmp_path / "models-config.toml"
    cfg.write_text(
        f'[paths]\ndata_dir = "{data}"\ndb_path = "{p.db_path}"\n'
        f'profile_dir = "{p.profile_dir}"\ncache_dir = "{p.cache_dir}"\n'
        f'[scoring]\nscreen_scorer = "{settings.scoring.screen_scorer}"\n'
        f"[console]\nport = {settings.console.port}\n"
    )
    monkeypatch.setenv("JOBHUNTER_CONFIG", str(cfg))
    return load_settings()


BAD = '[scoring]\nscreen_scorer = "anthropic:claude-opus-5-5"\n'


def test_rescore_panel_is_blocked(pdir, db_path, tmp_path, monkeypatch):  # noqa: F811
    c = make_client(pdir, db_path)
    c.app.state.rescore_scorer_factory = lambda spec: pytest.fail("no scorer may be built")
    settings = c.app.state.settings
    c.app.state.settings = loaded_settings(tmp_path, monkeypatch, settings, BAD)
    html = c.get("/prefs/rescore/panel").text
    assert MESSAGE in html
    assert 'id="rescore-run"' not in html
    spec = c.app.state.settings.scoring.screen_scorer
    r = c.post("/prefs/rescore/run", data={"scope": "all", "scorer": spec, "plan": "x"})
    assert MESSAGE in r.text
    rows = c.app.state.conn_factory().execute("SELECT count(*) FROM rescore_request")
    assert rows.fetchone()[0] == 0


def test_extension_score_now_is_refused(client, token, captured, tmp_path, monkeypatch):  # noqa: F811
    settings = client.app.state.settings
    client.app.state.settings = loaded_settings(tmp_path, monkeypatch, settings, BAD)
    est = post(client, token, f"groups/{captured}/estimate").json()
    assert est["refusal"] and MESSAGE in est["refusal"]
    r = post(client, token, f"groups/{captured}/score", {"action_id": aid(), "token": est["token"]})
    assert r.status_code == 409 and MESSAGE in r.json()["error"]


def test_packet_page_shows_the_drafting_fallback_notice(packet_env, tmp_path, monkeypatch):  # noqa: F811
    penv = packet_env
    penv.app.state.settings = loaded_settings(
        tmp_path, monkeypatch, penv.settings, '[apply]\ncli_model = "haiku"\n'
    )
    page = penv.client.get(f"/packet/{penv.pid}").text
    assert "models.toml&#39;s apply.cli_model is invalid; using opus from the default" in page
    assert penv.app.state.settings.apply.cli_model == "opus"
