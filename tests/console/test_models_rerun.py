"""/models Save + re-run (specs/020 "Save + re-run", ticket M5).

Save + re-score saves, then shows the existing re-score panel for the saved model inline; only
its confirm (the plan token, for exactly the saved value) starts a paid run, through the one
``RescoreManager`` (single flight, run lock, the cap check). Synthetic data only. No network,
no Anthropic client, no subprocess; the scorer is a fake that counts its calls."""

from __future__ import annotations

import re
import threading
from datetime import UTC, datetime
from html import unescape
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from jobhunter import readiness
from jobhunter.config import load_settings
from jobhunter.console.app import create_app
from jobhunter.core import db
from jobhunter.scoring.profile import load_profile
from jobhunter.scoring.scorers import ScoreResult

NOW = datetime.now(UTC)
LOOPBACK = ("127.0.0.1", 50000)
LAN = ("192.168.1.20", 50000)
SPEC = "openrouter:test/model"
HAIKU = "anthropic:claude-haiku-5-5"
SONNET = "anthropic:claude-sonnet-5-5"
OPUS = "anthropic:claude-opus-5-5"
PREFS = """\
resume_path: resume.md
target_titles: [Systems Engineer]
hard:
  states_allowed: all
  remote_ok: true
soft:
  state_ranking: [CO, WA]
narrative:
  want: Synthetic systems work.
"""
QUOTE = "Write Terraform modules and Go tooling."
BODY = (
    '{"verdict": "strong", "dimensions": {"skills": {"score": 90, "why": "match: ok"}, '
    '"seniority": {"score": 90, "why": "match: ok"}, "domain": {"score": 90, "why": "match: ok"}}, '
    '"raw_skills": 90, "recency_weighted_skills": 90, "stale_skills": [], '
    '"current_focus_overlap": 50, "done_with_hits": [], '
    f'"evidence": [{{"claim": "tooling", "quote": "{QUOTE}"}}], "blockers": [], '
    '"missing_info": [], "shape_flags": [], "tailoring_hints": []}'
)


@pytest.fixture(autouse=True)
def _guards(monkeypatch: pytest.MonkeyPatch):
    """No network (a recording transport), no Anthropic client, no subprocess; the test
    model is priced from a fake cache read, so no file and no fetch."""
    import subprocess

    import anthropic

    from jobhunter.scoring import screen

    readiness.clear_caches()
    requests: list[httpx.Request] = []
    original = httpx.Client.__init__

    def init(self, *args, **kwargs):
        def record(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(500)

        kwargs.setdefault("transport", httpx.MockTransport(record))
        original(self, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "__init__", init)
    monkeypatch.setattr(anthropic, "Anthropic", lambda *a, **k: pytest.fail("no Anthropic client"))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("no subprocess"))
    monkeypatch.setattr(screen, "cached_openrouter_rates", lambda config, slug: (0.10, 0.50))
    for var in ("OPENROUTER_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    yield requests
    assert requests == [], "a test reached the network"
    readiness.clear_caches()


class FakeScorer:
    """Counts the jobs it is sent; ``gate`` holds a run open."""

    name = SPEC

    def __init__(self, sent: list[str], gate: threading.Event | None = None):
        self.sent = sent
        self.gate = gate

    def submit(self, requests):
        if self.gate is not None:
            assert self.gate.wait(10)
        self.sent.extend(r.custom_id for r in requests)
        return [ScoreResult(custom_id=r.custom_id, status="succeeded", text=BODY) for r in requests]

    def cost(self, usage, batch):
        return 0.0


def setup_data(tmp_path: Path, scoring: str = "") -> Path:
    data = tmp_path / "data"
    prof = data / "profile"
    prof.mkdir(parents=True)
    (prof / "preferences.yaml").write_text(PREFS, encoding="utf-8")
    (prof / "resume.md").write_text("Synthetic Person\n- ran synthetic hosts\n", encoding="utf-8")
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'[paths]\ndata_dir = "{data}"\ndb_path = "{data / "jobhunter.db"}"\n'
        f'profile_dir = "{prof}"\ncache_dir = "{tmp_path / "cache"}"\n'
        f"[scoring]\n{scoring}"
    )
    c = db.connect(data / "jobhunter.db")
    db.migrate(c)
    c.execute(
        "INSERT INTO source (key, class, name, family, tier, entry, policy) VALUES "
        "('co', 'A', 'Synthetic', 'x', 'http', 'https://example.com', 'enabled')"
    )
    fv = load_profile(prof).filter_version
    for n in range(1, 4):
        add_job(c, n, fv)
    c.close()
    return cfg


def add_job(c, n: int, fv: str) -> None:
    c.execute(
        "INSERT INTO job (id, source_key, external_id, url, title, employer, description_text, "
        "location_scope, employment_type, stage, posted_at, first_seen_at, last_seen_at) "
        "VALUES (?, 'co', ?, 'https://example.com/j', ?, 'Acme', ?, 'single', 'full_time', "
        "'prefiltered', ?, ?, ?)",
        (n, str(n), f"Job {n}", f"Own the fleet.\n{QUOTE}", NOW.isoformat(), NOW.isoformat(),
         NOW.isoformat()),
    )  # fmt: skip
    c.execute(
        "INSERT INTO job_group (id, canonical_job_id, method, created_at) "
        "VALUES (?, ?, 'exact_hash', 'x')",
        (n, n),
    )
    c.execute("UPDATE job SET job_group_id = ? WHERE id = ?", (n, n))
    c.execute("INSERT INTO job_locations (job_id, state, is_primary) VALUES (?, 'CO', 1)", (n,))
    c.execute(
        "INSERT INTO prefilter_result (job_id, passed, reasons, filter_version, evaluated_at) "
        "VALUES (?, 1, '[]', ?, 'x')",
        (n, fv),
    )


def make_client(cfg: Path, *, peer=LOOPBACK, allow_remote: bool = False, gate=None):
    settings = load_settings(cfg)
    app = create_app(
        settings,
        lambda: db.connect(settings.paths.db_path),
        clock=lambda: NOW,
        allow_remote=allow_remote,
    )
    app.state.setup_mode = readiness.HOST
    sent: list[str] = []
    app.state.rescore_scorer_factory = lambda spec: FakeScorer(sent, gate)
    client = TestClient(app, client=peer)
    client.sent = sent  # type: ignore[attr-defined]
    return client


@pytest.fixture
def cfg(tmp_path):
    return setup_data(tmp_path)


@pytest.fixture
def client(cfg):
    return make_client(cfg)


def digest_of(text: str) -> str:
    m = re.search(r'id="models-digest" name="digest" value="([0-9a-f]*)"', text)
    assert m
    return m.group(1)


def save(client, row: str, page: str, **values):
    """Post a row as the browser does (see test_models_page.save)."""
    m = re.search(rf'<section class="mo-row[^"]*" id="models-row-{row}".*?</section>', page, re.S)
    assert m
    data: dict[str, object] = {"digest": digest_of(page)}
    for name, shown in re.findall(r'name="shown_([a-z_]+)" value="([^"]*)"', m.group(0)):
        data[f"shown_{name}"] = unescape(shown)
        data[name] = unescape(shown)
    data.update(values)
    return client.post(f"/models/{row}", data=data, headers={"HX-Request": "true"})


def save_rerun(client, **values):
    """Save + re-score to the test model ("save anyway": it is not in the OpenRouter cache)."""
    page = client.get("/models").text
    return save(client, "screen", page, screen=SPEC, force_for_screen=SPEC, rerun="1", **values)


def token_of(html: str) -> str:
    m = re.search(r'name="plan" value="([0-9a-f]+)"', html)
    assert m, "no confirmable plan shown"
    return m.group(1)


def requests_of(client) -> list:
    conn = client.app.state.conn_factory()
    try:
        return conn.execute("SELECT * FROM rescore_request").fetchall()
    finally:
        conn.close()


def confirm(client, token: str, saved: str = SPEC, scope: str = "all"):
    return client.post(
        "/models/screen/rescore/run",
        data={"saved": saved, "scope": scope, "plan": token},
        headers={"HX-Request": "true"},
    )


# ─── Save + re-score opens the confirm, spends nothing ──────────────────────


def queue_rescore(client) -> None:
    conn = client.app.state.conn_factory()
    with conn:
        conn.execute(
            "INSERT INTO rescore_request (requested_at, scope, scoring_version, job_count, "
            "cost_per_job_usd, estimated_usd) VALUES ('2026-10-10T01:21:00Z', 'recent', 'v', "
            "3, 0.002, 0.006)"
        )
    conn.close()


def test_page_offers_save_and_rerun_but_runs_nothing(client):
    text = client.get("/models").text
    assert 'data-rerun="1"' in text and "Save + re-score" in text and "Save + deep-score" in text
    # The intent field sits in the row form, outside the preview region a preview replaces.
    screen = re.search(r'id="models-row-screen".*?</section>', text, re.S).group(0)
    intent = screen.index('class="mo-rerun-intent"')
    assert intent < screen.index('id="models-screen-preview"')
    assert 'name="rerun" class="mo-rerun-intent" value=""' in screen
    assert 'id="rescore-form"' not in text  # the panel opens only on Save + re-score
    assert requests_of(client) == [] and client.sent == []


def test_save_rerun_saves_and_shows_the_estimate_without_spending(client):
    r = save_rerun(client, scope="all")
    assert r.status_code == 200, r.text
    assert "Saved at" in r.text
    assert client.app.state.settings.scoring.screen_scorer == SPEC
    text = unescape(r.text)
    assert 'id="rescore-panel"' in text and 'hx-post="/models/screen/rescore/run"' in text
    assert f'name="saved" value="{SPEC}"' in text
    assert f"Re-score with {SPEC}</h3>" in text
    assert "Will score <strong>" in text and "estimated <strong>$" in text
    assert "Spend cap remaining" in text
    assert "Cancel only cancels the re-score" in text
    assert 'hx-disinherit="hx-confirm"' in text and 'hx-disabled-elt="#rescore-run"' in text
    # Nothing ran, nothing was queued.
    assert requests_of(client) == [] and client.sent == []


def test_the_confirm_dialog_names_the_model_count_and_cost(client):
    save_rerun(client)
    text = unescape(client.get("/models/screen/rescore", params={"scope": "all"}).text)
    m = re.search(r'<form id="rescore-form"[^>]*hx-confirm="([^"]*)"', text)
    assert m, "the run form has its confirm dialog"
    assert m.group(1) == (
        f"Re-score 3 job groups with {SPEC}? Estimated cost $0.01; the run stops before it "
        "spends more than $0.02. This spends credits."
    )


def test_scope_change_replans_on_the_models_route(client):
    save_rerun(client)
    r = client.get("/models/screen/rescore", params={"scope": "all"})
    assert r.status_code == 200
    assert '<option value="all" selected>' in r.text
    assert 'hx-get="/models/screen/rescore"' in r.text  # not /prefs/rescore/panel
    assert "Will score <strong>3</strong>" in r.text
    assert requests_of(client) == [] and client.sent == []


def test_confirm_runs_the_saved_plan_once_and_redraws_the_models_panel(client):
    token_of(save_rerun(client).text)  # the save shows a confirmable plan ("recent")
    token = token_of(client.get("/models/screen/rescore", params={"scope": "all"}).text)
    r = confirm(client, token)
    assert r.status_code == 200
    assert 'hx-get="/prefs/rescore/progress/' in r.text and "?via=models" in r.text
    client.app.state.rescore.join()
    rows = requests_of(client)
    assert len(rows) == 1
    assert (rows[0]["status"], rows[0]["scorer"], rows[0]["scored"]) == ("done", SPEC, 3)
    assert len(client.sent) == 3
    # The finished poll redraws the /models variant, not the /prefs one.
    done = client.get(
        f"/prefs/rescore/progress/{rows[0]['id']}",
        params={"via": "models"},
        headers={"HX-Request": "true"},
    )
    assert done.headers["HX-Retarget"] == "#rescore-panel"
    assert "Done." in done.text and 'id="rescore-heading"' in done.text
    assert 'name="saved"' in done.text or "Will score <strong>0</strong>" in done.text
    assert "/prefs/rescore/run" not in done.text and 'id="rescore-queued"' not in done.text
    # Without via, the /prefs variant as before.
    prefs = client.get(f"/prefs/rescore/progress/{rows[0]['id']}", headers={"HX-Request": "true"})
    assert 'id="rescore-heading"' not in prefs.text


# ─── The confirm is tied to the plan and the saved value ────────────────────


def test_a_stale_plan_runs_nothing(client, cfg):
    save_rerun(client)
    token = token_of(client.get("/models/screen/rescore", params={"scope": "all"}).text)
    c = db.connect(load_settings(cfg).paths.db_path)
    add_job(c, 4, load_profile(load_settings(cfg).paths.profile_dir).filter_version)
    c.close()
    r = confirm(client, token)
    assert "The plan changed since you confirmed it" in unescape(r.text)
    assert requests_of(client) == [] and client.sent == []
    r = confirm(client, "0123456789abcdef")
    assert "plan changed" in r.text
    assert requests_of(client) == [] and client.sent == []


def test_a_confirm_for_another_model_runs_nothing_and_relabels_the_panel(client):
    save_rerun(client)
    token = token_of(client.get("/models/screen/rescore", params={"scope": "all"}).text)
    page = client.get("/models").text  # another tab saves another model
    assert "Saved at" in save(client, "screen", page, screen=HAIKU, force_for_screen=HAIKU).text
    r = confirm(client, token)
    assert "The screening model changed since this re-score was planned" in r.text
    assert f"Re-score with {HAIKU}</h3>" in r.text and f"with {SPEC}" not in r.text
    assert requests_of(client) == [] and client.sent == []
    # A confirm that names no saved value runs nothing either.
    confirm(client, token, saved="")
    assert requests_of(client) == [] and client.sent == []


def test_the_spend_cap_holds(tmp_path):
    cfg = setup_data(tmp_path, "daily_cap_usd = 0.001\n")
    client = make_client(cfg)
    assert "Saved at" in save_rerun(client).text
    text = client.get("/models/screen/rescore", params={"scope": "all"}).text
    assert "over the remaining spend cap" in text
    assert re.search(r'id="rescore-run"[^>]* disabled', text)
    r = confirm(client, token_of(text))
    assert "over the remaining spend cap" in r.text
    assert requests_of(client) == [] and client.sent == []


def test_a_double_submit_runs_once(cfg):
    gate = threading.Event()
    client = make_client(cfg, gate=gate)
    save_rerun(client)
    token = token_of(client.get("/models/screen/rescore", params={"scope": "all"}).text)
    first = confirm(client, token)
    second = confirm(client, token)  # the run is still going
    assert first.status_code == second.status_code == 200
    assert "already running" in second.text
    gate.set()
    client.app.state.rescore.join()
    third = confirm(client, token)  # after it finished: the plan is spent
    assert "plan changed" in third.text or "Will score <strong>0</strong>" in third.text
    rows = requests_of(client)
    assert len(rows) == 1 and rows[0]["status"] == "done"
    assert sorted(client.sent) == sorted(set(client.sent)) and len(client.sent) == 3


def test_a_key_shaped_saved_value_is_never_rendered_or_run(tmp_path):
    secret = "openrouter:sk-or-v1-SENTINEL0123456789abcdef"
    cfg = setup_data(tmp_path, f'screen_scorer = "{secret}"\n')
    client = make_client(cfg)
    page = client.get("/models").text
    r = save(client, "screen", page, rerun="1")  # no change: opens the panel for it
    assert "SENTINEL" not in r.text
    assert "cannot be shown here" in r.text and 'id="rescore-form"' not in r.text
    for resp in (
        client.get("/models/screen/rescore", params={"scope": "all"}),
        confirm(client, "0123456789abcdef", saved=secret),
    ):
        assert "SENTINEL" not in resp.text and 'name="saved"' not in resp.text
    assert requests_of(client) == [] and client.sent == []


def test_a_panel_failure_after_the_save_offers_a_retry(client, monkeypatch):
    def broken(*a, **k):
        raise RuntimeError("db is busy")

    monkeypatch.setattr(client.app.state, "rescore_panel_context", broken)
    r = save_rerun(client)
    assert r.status_code == 200 and "Saved at" in r.text
    assert "could not be opened" in r.text and 'hx-get="/models/screen/rescore"' in r.text
    assert client.app.state.settings.scoring.screen_scorer == SPEC
    assert requests_of(client) == []


# ─── Gates ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("method", "url", "data"),
    [
        ("get", "/models/screen/rescore", None),
        ("post", "/models/screen/rescore/run", {"saved": SPEC, "plan": "x", "scope": "all"}),
        ("post", "/models/screen", {"screen": SPEC, "rerun": "1"}),
        ("post", "/models/deep", {"deep": SONNET, "rerun": "1"}),
    ],
)
@pytest.mark.parametrize("remote", ["peer", "allow_remote", "forwarded"])
def test_rerun_routes_are_loopback_only_and_off_under_allow_remote(cfg, method, url, data, remote):
    kw = {"peer": LAN} if remote == "peer" else {}
    if remote == "allow_remote":
        kw["allow_remote"] = True
    client = make_client(cfg, **kw)
    headers = {"X-Forwarded-For": "203.0.113.9"} if remote == "forwarded" else {}
    if method == "get":
        r = client.get(url, headers=headers)
    else:
        r = client.post(url, data=data, headers=headers)
    assert r.status_code == 403
    assert not (Path(client.app.state.settings.paths.data_dir) / "models.toml").exists()
    assert requests_of(client) == [] and client.sent == []


def test_allow_remote_hides_the_rerun_buttons_but_keeps_save(cfg):
    text = make_client(cfg, allow_remote=True).get("/models").text
    assert "Save + re-score" not in text and "Save + deep-score" not in text
    assert 'class="mo-save"' in text


# ─── Pending questions and the deep row ─────────────────────────────────────


def test_queued_rescores_offer_save_and_rerun_variants(client):
    queue_rescore(client)
    preview = client.post(
        "/models/screen/preview",
        data={"screen": "jev:typesafe/jev-1.13", "shown_screen": HAIKU},
    )
    assert "queued re-score" in preview.text
    assert "Save + re-score; queued re-scores use the new model" in preview.text
    assert f"Save + re-score; keep queued re-scores on {HAIKU}" in preview.text
    assert preview.text.count('data-rerun="1"') == 2


def test_a_pending_question_carries_rerun_and_then_opens_the_panel(client):
    queue_rescore(client)
    r = save_rerun(client)
    assert "choose what queued re-scores use" in r.text
    assert 'name="rerun" class="mo-rerun-intent" value="1"' in r.text
    assert 'id="rescore-form"' not in r.text
    r = save_rerun(client, queued="new")
    assert "Saved at" in r.text and 'id="rescore-form"' in r.text
    assert "can be run or dismissed on" in r.text  # the queued request stays on /prefs
    assert len(requests_of(client)) == 1 and client.sent == []


def test_deep_row_has_one_deep_panel_that_loads_after_save_and_deep_score(client):
    page = client.get("/models").text
    deep = re.search(r'id="models-row-deep".*?</section>', page, re.S).group(0)
    assert deep.count('id="deep-panel"') == 1
    assert 'hx-trigger="click from:#deep-open"' in deep  # not loaded on render
    r = save(client, "deep", page, deep=SONNET, force_for_deep=SONNET, rerun="1")
    assert r.status_code == 200 and "Saved at" in r.text
    assert r.text.count('id="deep-panel"') == 1
    assert 'hx-trigger="load, click from:#deep-open"' in r.text
    assert requests_of(client) == []
    # A plain Save does not load it.
    page = client.get("/models").text
    r = save(client, "deep", page, deep=OPUS, force_for_deep=OPUS)
    assert 'hx-trigger="click from:#deep-open"' in r.text


def test_the_run_route_refuses_a_key_shaped_value_even_with_a_valid_token(tmp_path):
    from jobhunter.scoring import rescore as rs

    secret = "openrouter:sk-or-v1-SENTINEL0123456789abcdef"
    cfg = setup_data(tmp_path, f'screen_scorer = "{secret}"\n')
    client = make_client(cfg)
    settings = client.app.state.settings
    conn = client.app.state.conn_factory()
    try:
        plan = rs.make_plan(
            conn, load_profile(settings.paths.profile_dir), settings.scoring, "all", secret, NOW
        )
    finally:
        conn.close()
    assert plan.total == 3 and not plan.refusal  # without the guard, this would run
    r = confirm(client, plan.token, saved=secret)
    assert r.status_code == 200
    assert "SENTINEL" not in r.text and "cannot be shown here" in r.text
    assert requests_of(client) == [] and client.sent == []


def test_a_restart_refusal_keeps_the_models_panel(client):
    save_rerun(client)
    token = token_of(client.get("/models/screen/rescore", params={"scope": "all"}).text)
    assert client.app.state.work.begin_restart() == []
    r = confirm(client, token)
    assert r.status_code == 503
    assert 'id="rescore-heading"' in r.text and "/prefs/rescore/run" not in r.text
    assert 'id="rescore-queued"' not in r.text
    assert requests_of(client) == [] and client.sent == []


def test_the_reset_question_offers_no_rerun_buttons(client):
    page = client.get("/models").text
    saved = save(client, "screen", page, screen=SPEC, force_for_screen=SPEC)
    assert "Saved at" in saved.text
    queue_rescore(client)
    r = client.post(
        "/models/screen/reset",
        data={"digest": digest_of(saved.text)},
        headers={"HX-Request": "true"},
    )
    assert "choose what queued re-scores use" in r.text
    assert "Reset; queued re-scores use the new model" in r.text
    assert 'data-rerun="1"' not in r.text and "Save + re-score" not in r.text


def test_a_save_without_the_intent_opens_no_panel(client):
    """What a plain button sends once models.js has cleared a stale intent: rerun empty."""
    page = client.get("/models").text
    r = save(client, "screen", page, screen=SPEC, force_for_screen=SPEC, rerun="")
    assert "Saved at" in r.text and 'id="rescore-form"' not in r.text
    assert 'name="rerun" class="mo-rerun-intent" value=""' in r.text


# ─── 020 M5b: per-model reservation on ──────────────────────────────────────


def test_sonnet_saves_without_save_anyway_and_its_plan_is_its_reservation(client, monkeypatch):
    # A key is present, so the only rule that could ask for "save anyway" is the reservation.
    # No client is ever built (the guard fails the test if one is).
    monkeypatch.setenv("ANTHROPIC_API_KEY", "placeholder")
    readiness.clear_caches()
    page = client.get("/models").text
    r = save(client, "screen", page, screen=SONNET, rerun="1", scope="all")
    text = unescape(r.text)
    assert "Save anyway" not in text and "Saved at" in text
    assert client.app.state.settings.scoring.screen_scorer == SONNET
    # No history: the plan is priced at what the cap holds for Sonnet's first batch (its
    # prefix as cache writes, above the warm $0.00507), so there is no separate hold line.
    priced = re.search(r"at \$([0-9.]+) per job", text)
    assert priced and float(priced.group(1)) > 0.00507
    assert 'id="rescore-reserve"' not in text
    assert requests_of(client) == [] and client.sent == []


def test_the_panel_says_when_the_cap_holds_more_than_the_estimate(tmp_path):
    cfg = setup_data(tmp_path, "daily_cap_usd = 0.004\n")
    conn = db.connect(tmp_path / "data" / "jobhunter.db")
    with conn:
        conn.execute(
            "INSERT INTO fit_score (job_group_id, tier, model, prompt_version, scoring_version, "
            "verdict, overall, dimensions, evidence, cost_usd, created_at) "
            "VALUES (1, 'screen', ?, 'p', 'old', 'weak', 10, '{}', '[]', 0.0003, 'x')",
            (HAIKU,),
        )
    conn.close()
    client = make_client(cfg)
    text = unescape(client.get("/models/screen/rescore", params={"scope": "all"}).text)
    # Measured $0.0003 is below Haiku 5.5's no-cache-reuse floor, so it is priced there.
    priced = re.search(r"at \$([0-9.]+) per job \(the price with no cache reuse", text)
    assert priced and 0.0003 < float(priced.group(1)) < 0.002
    hold = re.search(r'<p class="hint" id="rescore-reserve">(.*?)</p>', text, re.S)
    assert hold, "the hold line is shown"
    assert "holds $0.0020 a job ($0.01 for all 3)" in hold.group(1)
    assert "Only 2 of 3 fit under the cap now" in hold.group(1)
    assert "Spend cap remaining: $0.0040." in text  # under a cent: four decimals (N4)
    assert not re.search(r'id="rescore-run"[^>]* disabled', text)
