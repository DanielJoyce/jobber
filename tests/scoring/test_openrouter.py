"""OpenRouter preset, served-model recording and the score --scorer override (specs/016).

httpx.MockTransport only: no network.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import httpx
import pytest
from test_scorers import GOOD_QUOTE, request
from test_screen import (
    NOW,
    add_group,
    conn,  # noqa: F401
    profile,  # noqa: F401
    screen_json,
)
from typer.testing import CliRunner

from jobhunter.config import OpenAICompat, OpenRouter, Scoring
from jobhunter.core import db
from jobhunter.scoring import evaluate, screen
from jobhunter.scoring.scorers import (
    AnthropicScorer,
    OpenAICompatScorer,
    OpenRouterScorer,
    ScorerError,
    Usage,
    privacy_notice,
    scorer_from_string,
)

CATALOG = {
    "data": [
        {
            "id": "openai/gpt-oss-120b",
            "pricing": {"prompt": "0.0000001", "completion": "0.0000005"},
        },
        {"id": "openai/gpt-oss-20b:free", "pricing": {"prompt": "0", "completion": "0"}},
        {"id": "typesafe/jev-router", "pricing": {"prompt": "-1", "completion": "-1"}},
        {"id": "broken/model", "pricing": {}},
    ]
}


def reply(text: str, *, model: str = "openai/gpt-oss-120b", cost: float | None = None) -> dict:
    usage: dict = {"prompt_tokens": 2000, "completion_tokens": 400}
    if cost is not None:
        usage["cost"] = cost
    return {
        "model": model,
        "choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": usage,
    }


def make(handler, tmp_path, slug="openai/gpt-oss-120b", **cfg) -> OpenRouterScorer:
    config = OpenRouter(catalog_cache=tmp_path / "cat.json", **cfg)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return OpenRouterScorer(slug, config, client=client, env={"OPENROUTER_API_KEY": "or-key"})


# ─── Factory ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "slug",
    [
        "openai/gpt-oss-120b",
        "anthropic/claude-haiku-4.5",
        "typesafe/jev-router",
        "openai/gpt-oss-20b:free",
        "meta-llama/llama-3.3-70b-instruct:batch",
    ],
)
def test_factory_keeps_slashes_and_suffixes(slug):
    s = scorer_from_string(f"openrouter:{slug}")
    assert isinstance(s, OpenRouterScorer) and isinstance(s, OpenAICompatScorer)
    assert s.model == slug and s.name == f"openrouter:{slug}"
    assert s.supports_batching is False


def test_factory_rejects_empty_slug():
    with pytest.raises(ScorerError):
        scorer_from_string("openrouter:")


@pytest.mark.parametrize("base", ["https://openrouter.ai/api/v1", "https://openrouter.ai/api/v1/"])
def test_url_for_both_base_styles(base, tmp_path):
    s = make(lambda r: httpx.Response(200), tmp_path)
    s.config = s.config.model_copy(update={"base_url": base})
    assert s.url == "https://openrouter.ai/api/v1/chat/completions"
    plain = OpenAICompatScorer("m", OpenAICompat(base_url="https://llm.example.com"))
    assert plain.url == "https://llm.example.com/v1/chat/completions"


# ─── Request shape ──────────────────────────────────────────────────────────


def test_request_url_headers_and_body(tmp_path):
    seen = {}

    def handler(req):
        seen.update(url=str(req.url), headers=req.headers, body=json.loads(req.content))
        return httpx.Response(200, json=reply("{}"))

    make(handler, tmp_path, referer="https://example.com/me").score_one(request())
    assert seen["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert seen["headers"]["authorization"] == "Bearer or-key"
    assert seen["headers"]["x-title"] == "jobhunter"
    assert seen["headers"]["http-referer"] == "https://example.com/me"
    body = seen["body"]
    assert body["model"] == "openai/gpt-oss-120b"
    assert body["provider"] == {"require_parameters": True, "data_collection": "deny"}
    assert body["response_format"]["type"] == "json_schema"
    assert body["usage"] == {"include": True}


def test_referer_omitted_and_provider_configurable(tmp_path):
    seen = {}

    def handler(req):
        seen.update(headers=req.headers, body=json.loads(req.content))
        return httpx.Response(200, json=reply("{}"))

    provider = {"order": ["Together"], "data_collection": "allow"}
    make(handler, tmp_path, provider=provider).score_one(request())
    assert "http-referer" not in seen["headers"]
    assert seen["body"]["provider"] == provider


def test_missing_key_names_openrouter_section(tmp_path):
    s = make(lambda r: httpx.Response(200), tmp_path)
    s._env = {}
    with pytest.raises(ScorerError, match=r"OPENROUTER_API_KEY.*scoring\.openrouter"):
        s._headers()


# ─── Served model ───────────────────────────────────────────────────────────


def test_served_model_recorded_for_router(conn, profile, tmp_path):  # noqa: F811
    gid = add_group(conn, 1, profile)
    scorer = make(
        lambda r: httpx.Response(
            200, json=reply(screen_json([GOOD_QUOTE]), model="qwen/qwen3.7-flash", cost=0.0004)
        ),
        tmp_path,
        slug="typesafe/jev-router",
    )
    assert scorer.score_one(request()).model == "qwen/qwen3.7-flash"
    screen.score_with(conn, scorer, profile, gid, now=NOW)
    row = conn.execute("SELECT model, served_model, cost_usd FROM fit_score").fetchone()
    assert row["model"] == "openrouter:typesafe/jev-router"
    assert row["served_model"] == "qwen/qwen3.7-flash"
    assert row["cost_usd"] == pytest.approx(0.0004)


def test_score_sync_records_served_model_and_cost(conn, profile, tmp_path):  # noqa: F811
    add_group(conn, 1, profile)
    scorer = make(
        lambda r: httpx.Response(200, json=reply(screen_json([GOOD_QUOTE]), cost=0.001)), tmp_path
    )
    res = screen.score_sync(conn, scorer, profile, limit=5, now=NOW)
    assert res.written == 1 and res.cost_usd == pytest.approx(0.001)
    assert conn.execute("SELECT served_model FROM fit_score").fetchone()[0] == "openai/gpt-oss-120b"


def test_anthropic_scorer_records_served_model(conn, profile):  # noqa: F811
    gid = add_group(conn, 1, profile)
    msg = SimpleNamespace(
        content=[SimpleNamespace(type="text", text=screen_json([GOOD_QUOTE]))],
        usage=SimpleNamespace(input_tokens=1000, output_tokens=100),
        model="claude-haiku-4-5-20251001",
        stop_reason="end_turn",
    )
    client = SimpleNamespace(messages=SimpleNamespace(parse=lambda **kw: msg))
    screen.score_with(conn, AnthropicScorer(client), profile, gid, now=NOW)
    row = conn.execute("SELECT model, served_model FROM fit_score").fetchone()
    assert row["model"] == "anthropic:claude-haiku-4-5"
    assert row["served_model"] == "claude-haiku-4-5-20251001"


# ─── Cost ───────────────────────────────────────────────────────────────────


def test_catalog_cost_fetched_once_and_cached_on_disk(tmp_path):
    calls = []

    def handler(req):
        if req.url.path.endswith("/models"):
            calls.append(str(req.url))
            return httpx.Response(200, json=CATALOG)
        return httpx.Response(200, json=reply("{}"))

    s = make(handler, tmp_path)
    usage = s.score_one(request()).usage
    expected = (2000 * 0.1 + 400 * 0.5) / 1e6
    assert s.cost(usage) == pytest.approx(expected)
    assert s.cost(usage) == pytest.approx(expected)
    assert calls == ["https://openrouter.ai/api/v1/models"]
    assert (tmp_path / "cat.json").exists()

    def no_fetch(req):
        pytest.fail("catalog should come from the disk cache")

    assert make(no_fetch, tmp_path).cost(usage) == pytest.approx(expected)


def test_cache_refreshed_after_a_day(tmp_path):
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(200, json=CATALOG)

    t0 = 1_000_000.0
    for offset, expected_calls in ((0, 1), (3600, 1), (25 * 3600, 2)):
        s = make(handler, tmp_path)
        s._clock = lambda offset=offset: t0 + offset
        s.catalog()
        assert len(calls) == expected_calls


def test_response_cost_beats_catalog(tmp_path):
    s = make(lambda r: pytest.fail("no catalog fetch needed"), tmp_path)
    assert s.cost(Usage(input_tokens=10, output_tokens=10, cost_usd=0.0123)) == 0.0123


def test_router_without_cost_prices_by_served_model(tmp_path):
    s = make(lambda r: httpx.Response(200, json=CATALOG), tmp_path, slug="typesafe/jev-router")
    usage = Usage(input_tokens=1_000_000, output_tokens=0, model="openai/gpt-oss-120b")
    assert s.cost(usage) == pytest.approx(0.1)


def test_unknown_price_costs_zero_and_warns_once(tmp_path, caplog):
    s = make(lambda r: httpx.Response(500), tmp_path)
    with caplog.at_level(logging.WARNING):
        assert s.cost(Usage(input_tokens=5, output_tokens=5)) == 0.0
        assert s.cost(Usage(input_tokens=5, output_tokens=5)) == 0.0
    assert sum("cost is unknown" in r.message for r in caplog.records) == 1


def test_suffix_falls_back_to_base_slug(tmp_path):
    s = make(
        lambda r: httpx.Response(200, json=CATALOG), tmp_path, slug="openai/gpt-oss-120b:batch"
    )
    assert s._lookup("openai/gpt-oss-120b:batch") == pytest.approx((0.1, 0.5))


# ─── Privacy ────────────────────────────────────────────────────────────────


def test_privacy_notice_names_openrouter_and_data_collection():
    notice = privacy_notice("openrouter:openai/gpt-oss-120b", Scoring())
    assert notice and "openrouter.ai" in notice and "data_collection is deny" in notice
    allow = Scoring(openrouter=OpenRouter(provider={"data_collection": "allow"}))
    assert "data_collection is allow" in (privacy_notice("openrouter:x/y", allow) or "")


# ─── Eval: served-model mix ─────────────────────────────────────────────────


def test_compare_reports_served_model_mix(conn, profile, tmp_path):  # noqa: F811
    for n in (1, 2, 3):
        gid = add_group(conn, n, profile)
        conn.execute(
            "INSERT INTO label (job_group_id, label, labeled_at) "
            "VALUES (?, 'interesting', '2026-01-01T00:00:00+00:00')",
            (gid,),
        )
    served = iter(["a/one", "a/one", "b/two"])

    def chat(model):
        def handler(req):
            if req.url.path.endswith("/models"):
                return httpx.Response(200, json=CATALOG)
            return httpx.Response(200, json=reply(screen_json([GOOD_QUOTE]), model=model()))

        return handler

    jev = make(chat(lambda: next(served)), tmp_path, slug="typesafe/jev-router")
    fixed = make(chat(lambda: "openai/gpt-oss-120b"), tmp_path)
    for sc in (jev, fixed):
        screen.score_sync(conn, sc, profile, limit=10, now=NOW)
    keys = [evaluate.VariantKey(screen.PROMPT_VERSION, sc.name) for sc in (jev, fixed)]
    report = evaluate.compare(conn, profile, keys)
    assert report.variants[0].served_models == {"a/one": 2, "b/two": 1}
    assert report.variants[1].served_models == {"openai/gpt-oss-120b": 3}
    text = report.to_text()
    assert "a/one x2, b/two x1" in text and "openai/gpt-oss-120b x3" in text
    assert "served_models" in report.to_json()


# ─── CLI and migration ──────────────────────────────────────────────────────


def test_cli_scorer_override(tmp_path, monkeypatch):
    from jobhunter import cli
    from jobhunter.config import load_settings
    from jobhunter.scoring import profile as profile_mod

    cfg = tmp_path / "config.toml"
    cfg.write_text(f'[paths]\ndb_path = "{tmp_path / "t.db"}"\n')  # default: Anthropic screen
    monkeypatch.setenv("JOBHUNTER_CONFIG", str(cfg))
    c = db.connect(load_settings().paths.db_path)
    db.migrate(c)
    c.close()

    captured = {}

    def fake_sync(_conn, scorer, _profile, **kw):
        captured.update(scorer=scorer, limit=kw["limit"])
        return screen.SyncResult()

    monkeypatch.setattr(profile_mod, "load_profile", lambda *a, **k: object())
    monkeypatch.setattr(screen, "score_sync", fake_sync)
    monkeypatch.setattr(
        "anthropic.Anthropic", lambda *a, **k: pytest.fail("no Anthropic client needed")
    )
    result = CliRunner().invoke(
        cli.app,
        ["score", "--submit", "--scorer", "openrouter:openai/gpt-oss-120b", "--limit", "7"],
    )
    assert result.exit_code == 0, result.output
    assert captured["scorer"].name == "openrouter:openai/gpt-oss-120b"
    assert captured["limit"] == 7
    assert "openrouter.ai" in result.output and "data_collection is deny" in result.output

    bad = CliRunner().invoke(cli.app, ["score", "--deep", "3", "--scorer", "openrouter:x/y"])
    assert bad.exit_code == 2


def test_migration_adds_served_model():
    c = db.connect(":memory:")
    db.migrate(c)
    cols = {r["name"] for r in c.execute("PRAGMA table_info(fit_score)")}
    assert "served_model" in cols
    assert db.migrate(c) == []
