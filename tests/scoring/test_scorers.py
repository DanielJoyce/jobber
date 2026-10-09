"""FitScorer factory and OpenAI-compatible scorer (specs/006). httpx.MockTransport only."""

from __future__ import annotations

import json
import logging
import threading
import time

import httpx
import pytest
from test_screen import (  # fixtures and helpers shared with the Anthropic screen tests
    NOW,
    SALARY_FLOOR,
    add_group,
    conn,  # noqa: F401
    profile,  # noqa: F401
    screen_json,
)
from typer.testing import CliRunner

from jobhunter.config import OpenAICompat, Scoring, Settings
from jobhunter.scoring import screen
from jobhunter.scoring.rubric import RUBRIC_TEXT, SCREEN_SCHEMA
from jobhunter.scoring.scorers import (
    AnthropicScorer,
    FitScorer,
    OpenAICompatScorer,
    ScoreRequest,
    ScorerError,
    privacy_notice,
    scorer_from_string,
)

SPEC = "openai-compat:llama-3.1-70b"
GOOD_QUOTE = "Write Terraform modules"


def chat_reply(text: str, *, finish: str = "stop", model: str = "llama-3.1-70b-instruct") -> dict:
    return {
        "model": model,
        "choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": finish}],
        "usage": {"prompt_tokens": 2000, "completion_tokens": 400},
    }


def make_scorer(handler, **cfg) -> OpenAICompatScorer:
    config = OpenAICompat(base_url="https://llm.example.com", api_key_env="TEST_LLM_KEY", **cfg)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return OpenAICompatScorer("llama-3.1-70b", config, client=client, env={"TEST_LLM_KEY": "k-123"})


def request(n: int = 1) -> ScoreRequest:
    return ScoreRequest(
        custom_id=f"g{n}",
        system="RUBRIC-TEXT",
        profile="PROFILE-TEXT",
        posting="POSTING-TEXT",
        schema={"type": "object"},
    )


# ─── Factory ────────────────────────────────────────────────────────────────


def test_factory_parses_anthropic_and_openai_compat():
    a = scorer_from_string("anthropic:claude-haiku-4-5", client=object())
    assert isinstance(a, AnthropicScorer)
    assert a.name == "anthropic:claude-haiku-4-5"
    assert a.model == "claude-haiku-4-5"
    assert a.supports_batching

    o = scorer_from_string(SPEC, scoring=Scoring(openai_compat=OpenAICompat(base_url="http://h")))
    assert isinstance(o, OpenAICompatScorer)
    assert o.name == SPEC
    assert o.model == "llama-3.1-70b"
    assert not o.supports_batching
    assert o.url == "http://h/v1/chat/completions"
    assert isinstance(o, FitScorer) and isinstance(a, FitScorer)


@pytest.mark.parametrize("spec", ["mystery:model", "openai-compat:", "anthropic:"])
def test_factory_rejects_bad_specs(spec):
    with pytest.raises(ScorerError):
        scorer_from_string(spec, client=object())


def test_base_url_with_v1_suffix_is_not_doubled():
    s = make_scorer(lambda r: httpx.Response(200))
    s.config = s.config.model_copy(update={"base_url": "https://llm.example.com/v1/"})
    assert s.url == "https://llm.example.com/v1/chat/completions"


def test_config_defaults_are_safe():
    c = Settings().scoring.openai_compat
    assert c.input_usd_per_mtok == 0 and c.output_usd_per_mtok == 0
    assert c.max_concurrency >= 1 and c.base_url.startswith("http://localhost")


# ─── Request shape ──────────────────────────────────────────────────────────


def test_request_shape_json_schema_and_auth():
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json=chat_reply("{}"))

    result = make_scorer(handler).score_one(request())
    [req] = seen
    assert str(req.url) == "https://llm.example.com/v1/chat/completions"
    assert req.headers["authorization"] == "Bearer k-123"
    body = json.loads(req.content)
    assert body["model"] == "llama-3.1-70b"
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["schema"] == {"type": "object"}
    text = "\n".join(m["content"] for m in body["messages"])
    assert "RUBRIC-TEXT" in text and "PROFILE-TEXT" in text and "POSTING-TEXT" in text
    assert body["messages"][0]["role"] == "system"
    assert body["messages"][-1] == {"role": "user", "content": "POSTING-TEXT"}
    assert result.status == "succeeded"
    assert result.model == "llama-3.1-70b-instruct"  # what the server says it used
    assert result.usage.input_tokens == 2000 and result.usage.output_tokens == 400


def test_missing_api_key_env_is_a_clear_error():
    config = OpenAICompat(api_key_env="NOPE_NOT_SET")
    s = OpenAICompatScorer("m", config, client=httpx.Client(transport=httpx.MockTransport(404)))
    s._env = {}
    with pytest.raises(ScorerError, match="NOPE_NOT_SET"):
        s.score_one(request())


def test_empty_api_key_env_sends_no_auth_header():
    seen: list[httpx.Request] = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json=chat_reply("{}"))

    config = OpenAICompat(api_key_env="")
    client = httpx.Client(transport=httpx.MockTransport(handler))
    OpenAICompatScorer("m", config, client=client).score_one(request())
    assert "authorization" not in seen[0].headers


def test_real_request_carries_rubric_profile_and_posting(conn, profile):  # noqa: F811
    group_id = add_group(conn, 1, profile)
    seen: list[dict] = []

    def handler(req):
        seen.append(json.loads(req.content))
        return httpx.Response(200, json=chat_reply(screen_json([GOOD_QUOTE])))

    scorer = make_scorer(handler)
    screen.score_with(conn, scorer, profile, group_id, now=NOW)
    body = seen[0]
    assert body["response_format"]["json_schema"]["schema"] == SCREEN_SCHEMA
    text = "\n".join(m["content"] for m in body["messages"])
    assert RUBRIC_TEXT in text
    assert "Synthetic Person" in text  # resume text via profile scoring inputs
    assert "Systems Engineer 1" in text
    assert str(SALARY_FLOOR) not in text  # filters never reach any provider


# ─── JSON fallback and validation ───────────────────────────────────────────


def test_falls_back_to_instructed_json_when_response_format_rejected(caplog):
    bodies: list[dict] = []

    def handler(req):
        body = json.loads(req.content)
        bodies.append(body)
        if "response_format" in body:
            return httpx.Response(400, json={"error": "response_format unsupported"})
        return httpx.Response(200, json=chat_reply('```json\n{"a": 1}\n```'))

    scorer = make_scorer(handler)
    with caplog.at_level(logging.WARNING):
        result = scorer.score_one(request())
        second = scorer.score_one(request(2))
    assert [("response_format" in b) for b in bodies] == [True, False, False]  # remembered
    assert "JSON schema" in bodies[1]["messages"][0]["content"]
    assert result.text == '{"a": 1}' and second.text == '{"a": 1}'
    assert "falling back" in caplog.text


def test_prose_around_json_is_stripped():
    s = make_scorer(
        lambda r: httpx.Response(200, json=chat_reply('Sure! Here it is: {"a": 1} Hope it helps'))
    )
    assert s.score_one(request()).text == '{"a": 1}'


def test_fallback_output_is_validated_and_scored(conn, profile):  # noqa: F811
    group_id = add_group(conn, 1, profile)

    def handler(req):
        if "response_format" in json.loads(req.content):
            return httpx.Response(422)
        return httpx.Response(200, json=chat_reply(f"```json\n{screen_json([GOOD_QUOTE])}\n```"))

    assert screen.score_with(conn, make_scorer(handler), profile, group_id, now=NOW)
    row = conn.execute("SELECT * FROM fit_score").fetchone()
    assert row["model"] == SPEC and row["verdict"] == "strong" and row["batch_id"] is None
    assert not row["evidence_unverified"]


def test_fabricated_quote_is_flagged_same_as_anthropic(conn, profile):  # noqa: F811
    group_id = add_group(conn, 1, profile)
    scorer = make_scorer(
        lambda r: httpx.Response(200, json=chat_reply(screen_json(["never in the posting"])))
    )
    screen.score_with(conn, scorer, profile, group_id, now=NOW)
    assert conn.execute("SELECT evidence_unverified FROM fit_score").fetchone()[0] == 1


@pytest.mark.parametrize(
    "reply",
    [
        chat_reply("not json at all"),
        chat_reply('{"verdict": "strong"}'),
        chat_reply("{}", finish="length"),
    ],
)
def test_invalid_json_leaves_group_eligible(conn, profile, reply):  # noqa: F811
    add_group(conn, 1, profile)
    scorer = make_scorer(lambda r: httpx.Response(200, json=reply))
    res = screen.score_sync(conn, scorer, profile, limit=10, now=NOW)
    assert (res.invalid, res.written) == (1, 0)
    assert conn.execute("SELECT count(*) FROM fit_score").fetchone()[0] == 0
    again = screen.eligible_groups(conn, profile, scorer=SPEC, limit=10)
    assert len(again) == 1


def test_http_error_leaves_group_eligible(conn, profile):  # noqa: F811
    add_group(conn, 1, profile)
    scorer = make_scorer(lambda r: httpx.Response(500))
    res = screen.score_sync(conn, scorer, profile, limit=10, now=NOW)
    assert (res.errored, res.written) == (1, 0)
    assert len(screen.eligible_groups(conn, profile, scorer=SPEC, limit=10)) == 1


def test_score_sync_writes_rows_and_is_idempotent(conn, profile):  # noqa: F811
    for n in (1, 2):
        add_group(conn, n, profile)
    scorer = make_scorer(
        lambda r: httpx.Response(200, json=chat_reply(screen_json([GOOD_QUOTE]))),
        input_usd_per_mtok=0.5,
        output_usd_per_mtok=1.0,
    )
    res = screen.score_sync(conn, scorer, profile, limit=10, now=NOW)
    assert res.written == 2 and res.invalid == 0
    assert screen.score_sync(conn, scorer, profile, limit=10, now=NOW).submitted == 0
    rows = conn.execute("SELECT * FROM fit_score").fetchall()
    assert len(rows) == 2 and {r["tier"] for r in rows} == {"screen"}
    spend = conn.execute("SELECT * FROM llm_spend").fetchone()
    assert spend["model"] == SPEC and spend["calls"] == 2
    assert spend["cost_usd"] == pytest.approx(2 * (2000 * 0.5 + 400 * 1.0) / 1e6)


# ─── Cost ───────────────────────────────────────────────────────────────────


def test_cost_uses_configured_prices():
    s = make_scorer(
        lambda r: httpx.Response(200, json=chat_reply("{}")),
        input_usd_per_mtok=0.60,
        output_usd_per_mtok=0.80,
    )
    usage = s.score_one(request()).usage
    assert s.cost(usage) == pytest.approx((2000 * 0.60 + 400 * 0.80) / 1e6)


def test_zero_default_prices_warn_once(caplog):
    s = make_scorer(lambda r: httpx.Response(200, json=chat_reply("{}")))
    usage = s.score_one(request()).usage
    with caplog.at_level(logging.WARNING):
        assert s.cost(usage) == 0.0
        assert s.cost(usage) == 0.0
    warnings = [r for r in caplog.records if "cost is unknown" in r.getMessage()]
    assert len(warnings) == 1


# ─── Concurrency ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("limit", [1, 2, 3])
def test_concurrency_limit_respected(limit):
    lock = threading.Lock()
    state = {"now": 0, "peak": 0}

    def handler(req):
        with lock:
            state["now"] += 1
            state["peak"] = max(state["peak"], state["now"])
        time.sleep(0.03)
        with lock:
            state["now"] -= 1
        return httpx.Response(200, json=chat_reply("{}"))

    s = make_scorer(handler, max_concurrency=limit)
    results = s.submit([request(n) for n in range(8)])
    assert [r.custom_id for r in results] == [f"g{n}" for n in range(8)]
    assert 1 <= state["peak"] <= limit
    if limit > 1:
        assert state["peak"] > 1  # actually concurrent


def test_openai_compat_does_not_batch():
    s = make_scorer(lambda r: httpx.Response(200))
    assert s.ready("x")
    with pytest.raises(ScorerError):
        s.collect("x")


# ─── Privacy notice ─────────────────────────────────────────────────────────


def test_privacy_notice_text():
    scoring = Scoring(openai_compat=OpenAICompat(base_url="https://llm.example.com"))
    assert privacy_notice("anthropic:claude-haiku-4-5", scoring) is None
    notice = privacy_notice(SPEC, scoring)
    assert notice and "https://llm.example.com" in notice and "resume" in notice


def test_cli_prints_privacy_notice_once(tmp_path, monkeypatch):
    from jobhunter import cli
    from jobhunter.config import load_settings
    from jobhunter.core import db

    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'[paths]\ndb_path = "{tmp_path / "t.db"}"\n'
        f'[scoring]\nscreen_scorer = "{SPEC}"\n'
        '[scoring.openai_compat]\nbase_url = "https://llm.example.com"\n'
    )
    monkeypatch.setenv("JOBHUNTER_CONFIG", str(cfg))
    settings = load_settings()
    c = db.connect(settings.paths.db_path)
    db.migrate(c)
    c.close()

    from jobhunter.scoring import profile as profile_mod

    monkeypatch.setattr(profile_mod, "load_profile", lambda *a, **k: object())
    monkeypatch.setattr(screen, "score_sync", lambda *a, **k: screen.SyncResult(), raising=True)
    monkeypatch.setattr(
        "anthropic.Anthropic", lambda *a, **k: pytest.fail("no Anthropic client needed")
    )
    result = CliRunner().invoke(cli.app, ["score", "--submit"])
    assert result.exit_code == 0, result.output
    assert result.output.count("https://llm.example.com") == 1
    assert "resume" in result.output
