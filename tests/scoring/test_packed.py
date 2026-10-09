"""Packed requests: several job judgments per request (specs/016). MockTransport only."""

from __future__ import annotations

import json
import re

import httpx
import pytest
from test_screen import (
    NOW,
    add_group,
    conn,  # noqa: F401
    profile,  # noqa: F401
    screen_json,
)
from typer.testing import CliRunner

from jobhunter.config import OpenRouter, Scoring
from jobhunter.core import db
from jobhunter.scoring import screen
from jobhunter.scoring.bench import run_bench
from jobhunter.scoring.profile import scoring_inputs
from jobhunter.scoring.rubric import RUBRIC_TEXT, packed_json_schema
from jobhunter.scoring.scorers import OpenRouterScorer, scorer_from_string

SLUG = "typesafe/jev-router"


def desc(n: int) -> str:
    return f"Distinct duty number {n} keeps the synthetic pipeline {n} healthy. " * 3


def quote(n: int) -> str:
    return f"Distinct duty number {n} keeps the synthetic pipeline {n} healthy."


def judgment(n: int, *, quote_from: int | None = None, custom_id: str | None = None) -> dict:
    body = json.loads(screen_json([quote(quote_from if quote_from is not None else n)]))
    return {"custom_id": custom_id or f"g{n}", **body}


def reply(judgments: list[dict], *, cost: float | None = 0.04, model: str = SLUG) -> dict:
    usage: dict = {"prompt_tokens": 4000, "completion_tokens": 800}
    if cost is not None:
        usage["cost"] = cost
    return {
        "model": model,
        "choices": [
            {
                "message": {"role": "assistant", "content": json.dumps({"judgments": judgments})},
                "finish_reason": "stop",
            }
        ],
        "usage": usage,
    }


def ids_in(body: dict) -> list[str]:
    return re.findall(r"BEGIN POSTING (g\d+)", body["messages"][1]["content"])


def make(handler, tmp_path, **cfg) -> OpenRouterScorer:
    cfg.setdefault("jobs_per_request", 4)
    cfg.setdefault("max_concurrency", 1)
    config = OpenRouter(catalog_cache=tmp_path / "cat.json", **cfg)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return OpenRouterScorer(SLUG, config, client=client, env={"OPENROUTER_API_KEY": "k"})


def seed(conn, profile, n: int) -> list[int]:  # noqa: F811
    return [add_group(conn, i, profile, description=desc(i)) for i in range(1, n + 1)]


def serve(handler_log: list, make_reply):
    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        handler_log.append(body)
        return httpx.Response(200, json=make_reply(body))

    return handler


def all_valid(body: dict) -> dict:
    return reply([judgment(int(i[1:])) for i in ids_in(body)])


# ─── Request shape ──────────────────────────────────────────────────────────


def test_request_shape(conn, profile, tmp_path):  # noqa: F811
    seed(conn, profile, 4)
    log: list = []
    scorer = make(serve(log, all_valid), tmp_path)
    screen.score_sync(conn, scorer, profile, limit=10, now=NOW)

    assert len(log) == 1
    body = log[0]
    single = scorer._body(
        screen.build_score_request(
            {"title": "t", "description_text": "d"}, "WA", profile, custom_id="g1"
        ),
        schema=True,
    )
    # the rubric + profile prefix is byte-identical to single mode
    assert body["messages"][0] == single["messages"][0]
    assert body["messages"][0]["content"] == f"{RUBRIC_TEXT}\n\n{scoring_inputs(profile)}"
    assert len(body["messages"]) == 2 and body["messages"][1]["role"] == "user"
    user = body["messages"][1]["content"]
    assert ids_in(body) == ["g1", "g2", "g3", "g4"]
    assert user.count("===== END POSTING") == 4 and "independently" in user
    for n in range(1, 5):
        assert quote(n) in user
    schema = body["response_format"]["json_schema"]["schema"]
    arr = schema["properties"]["judgments"]
    assert arr["minItems"] == arr["maxItems"] == 4
    assert "custom_id" in arr["items"]["required"]
    assert body["max_tokens"] == 4 * screen.MAX_TOKENS


def test_packed_schema_is_a_superset_of_screen():
    s = packed_json_schema(3)
    item = s["properties"]["judgments"]["items"]
    assert "$defs" in s and "$defs" not in item
    assert item["required"][0] == "custom_id" and "verdict" in item["required"]


def test_json_fallback_when_schema_rejected(conn, profile, tmp_path):  # noqa: F811
    seed(conn, profile, 2)
    log: list = []

    def handler(req):
        body = json.loads(req.content)
        log.append(body)
        if "response_format" in body:
            return httpx.Response(400, json={"error": "no"})
        return httpx.Response(200, json=all_valid(body))

    res = screen.score_sync(conn, make(handler, tmp_path), profile, limit=5, now=NOW)
    assert res.written == 2
    assert "judgments" in log[-1]["messages"][0]["content"]  # schema spelled out in the prompt


# ─── Parsing ────────────────────────────────────────────────────────────────


def run_with(conn, profile, tmp_path, judgments_for, n=3, **kw):  # noqa: F811
    seed(conn, profile, n)
    scorer = make(serve([], lambda body: reply(judgments_for(body), **kw)), tmp_path)
    return screen.score_sync(conn, scorer, profile, limit=10, now=NOW)


def written_ids(conn):  # noqa: F811
    return sorted(r[0] for r in conn.execute("SELECT job_group_id FROM fit_score"))


def test_all_valid(conn, profile, tmp_path):  # noqa: F811
    res = run_with(conn, profile, tmp_path, lambda b: [judgment(i) for i in (1, 2, 3)])
    assert (res.submitted, res.written, res.invalid, res.errored) == (3, 3, 0, 0)
    assert res.unverified == 0 and res.requests == 1
    assert len(written_ids(conn)) == 3


def test_one_invalid_item_others_written(conn, profile, tmp_path):  # noqa: F811
    def judgments(_b):
        bad = judgment(2)
        bad["verdict"] = "bogus"
        return [judgment(1), bad, judgment(3)]

    res = run_with(conn, profile, tmp_path, judgments)
    assert (res.written, res.invalid) == (2, 1)
    assert len(written_ids(conn)) == 2
    # the invalid one stays eligible
    eligible = screen.eligible_groups(conn, profile, scorer="openrouter:" + SLUG, limit=10)
    assert [g["group_id"] for g in eligible] == [2]


def test_missing_item(conn, profile, tmp_path):  # noqa: F811
    res = run_with(conn, profile, tmp_path, lambda b: [judgment(1), judgment(3)])
    assert (res.written, res.invalid) == (2, 1)
    eligible = screen.eligible_groups(conn, profile, scorer="openrouter:" + SLUG, limit=10)
    assert [g["group_id"] for g in eligible] == [2]


def test_unknown_id(conn, profile, tmp_path):  # noqa: F811
    res = run_with(
        conn,
        profile,
        tmp_path,
        lambda b: [judgment(1), judgment(2, custom_id="g99"), judgment(3)],
    )
    assert (res.written, res.invalid) == (2, 1)
    assert 2 not in written_ids(conn)


def test_duplicated_id_drops_both_copies(conn, profile, tmp_path):  # noqa: F811
    res = run_with(
        conn, profile, tmp_path, lambda b: [judgment(1), judgment(2), judgment(2), judgment(3)]
    )
    assert (res.written, res.invalid) == (2, 1)
    assert 2 not in written_ids(conn)


def test_unusable_reply_leaves_all_eligible(conn, profile, tmp_path):  # noqa: F811
    seed(conn, profile, 2)

    def handler(req):
        r = reply([])
        r["choices"][0]["message"]["content"] = "not json"
        return httpx.Response(200, json=r)

    res = screen.score_sync(conn, make(handler, tmp_path), profile, limit=5, now=NOW)
    assert (res.written, res.invalid) == (0, 2)
    assert written_ids(conn) == []


def test_http_error_marks_pack_errored(conn, profile, tmp_path):  # noqa: F811
    seed(conn, profile, 2)
    scorer = make(lambda r: httpx.Response(500), tmp_path)
    res = screen.score_sync(conn, scorer, profile, limit=5, now=NOW)
    assert (res.errored, res.written) == (2, 0)


def test_quote_from_neighbor_posting_fails_verification(conn, profile, tmp_path):  # noqa: F811
    res = run_with(
        conn,
        profile,
        tmp_path,
        lambda b: [judgment(1), judgment(2, quote_from=1), judgment(3)],
    )
    assert res.written == 3 and res.unverified == 1
    rows = {
        r["job_group_id"]: r
        for r in conn.execute("SELECT job_group_id, evidence_unverified, evidence FROM fit_score")
    }
    assert rows[1]["evidence_unverified"] == 0 and rows[3]["evidence_unverified"] == 0
    assert rows[2]["evidence_unverified"] == 1
    assert json.loads(rows[2]["evidence"])[0]["verified"] is False
    # the quote really is in the neighbor's text, i.e. the check is per job
    assert quote(1) in desc(1)


# ─── Cost and spend ─────────────────────────────────────────────────────────


def test_cost_split_and_llm_spend(conn, profile, tmp_path):  # noqa: F811
    res = run_with(
        conn,
        profile,
        tmp_path,
        lambda b: [judgment(i) for i in (1, 2, 3, 4)],
        n=4,
        cost=0.04,
        model="some/served-model",
    )
    assert res.cost_usd == pytest.approx(0.04)
    rows = conn.execute("SELECT cost_usd, served_model, input_tokens FROM fit_score").fetchall()
    assert len(rows) == 4
    for r in rows:
        assert r["cost_usd"] == pytest.approx(0.01)
        assert r["served_model"] == "some/served-model"
        assert r["input_tokens"] == 1000
    spend = conn.execute("SELECT * FROM llm_spend").fetchall()
    assert len(spend) == 1
    assert spend[0]["calls"] == 1 and spend[0]["cost_usd"] == pytest.approx(0.04)
    assert spend[0]["model"] == "openrouter:" + SLUG


def test_cost_split_counts_invalid_items_too(conn, profile, tmp_path):  # noqa: F811
    run_with(conn, profile, tmp_path, lambda b: [judgment(1), judgment(2)], n=4, cost=0.04)
    costs = [r[0] for r in conn.execute("SELECT cost_usd FROM fit_score")]
    assert costs == [pytest.approx(0.01)] * 2  # 4 jobs in the pack, 2 judged
    assert conn.execute("SELECT cost_usd FROM llm_spend").fetchone()[0] == pytest.approx(0.04)


def test_spend_cap_stops_before_next_pack(conn, profile, tmp_path):  # noqa: F811
    seed(conn, profile, 6)
    log: list = []
    scorer = make(serve(log, all_valid), tmp_path, jobs_per_request=2)
    res = screen.score_sync(
        conn,
        scorer,
        profile,
        limit=10,
        now=NOW,
        remaining_usd=lambda: screen.remaining_daily_budget(conn, 0.05, NOW),
    )
    # $0.05 covers one $0.04 request; after it only $0.01 is left
    assert len(log) == 1 and res.requests == 1 and res.written == 2
    assert len(screen.eligible_groups(conn, profile, scorer=scorer.name, limit=10)) == 4


def test_spend_cap_uses_measured_mean_cost(conn, profile, tmp_path):  # noqa: F811
    seed(conn, profile, 4)
    conn.execute(
        "INSERT INTO llm_spend (day, model, tier, calls, cost_usd) "
        "VALUES (?, ?, 'screen', 10, 1.0)",
        ("2026-10-09", "openrouter:" + SLUG),
    )
    scorer = make(serve([], all_valid), tmp_path)
    assert screen.estimated_request_cost(conn, scorer, NOW, 4) == pytest.approx(0.10)
    fresh = make(serve([], all_valid), tmp_path)
    fresh.name = "openrouter:other/model"
    assert screen.estimated_request_cost(conn, fresh, NOW, 4) == pytest.approx(0.04)


# ─── Packing ────────────────────────────────────────────────────────────────


def test_packs_are_fifo_by_group_id(conn, profile, tmp_path):  # noqa: F811
    seed(conn, profile, 5)
    log: list = []
    scorer = make(serve(log, all_valid), tmp_path, jobs_per_request=2)
    screen.score_sync(conn, scorer, profile, limit=10, now=NOW)
    assert [ids_in(b) for b in log] == [["g1", "g2"], ["g3", "g4"], ["g5"]]


def test_token_cap_splits_packs(conn, profile, tmp_path):  # noqa: F811
    texts = [(f"g{i}", "x" * 8000) for i in range(1, 7)]  # about 2,000 tokens each
    fixed = (len(RUBRIC_TEXT) + len(scoring_inputs(profile)) + 1500) // 4
    packs = screen.pack_items(texts, profile, jobs_per_request=8, max_input_tokens=fixed + 5000)
    assert [len(p) for p in packs] == [2, 2, 2]
    # without the cap all six fit in one pack
    assert len(screen.pack_items(texts, profile, jobs_per_request=8, max_input_tokens=10**6)) == 1
    # an oversize posting still goes out alone
    big = screen.pack_items(
        [("g1", "x" * 400_000)], profile, jobs_per_request=8, max_input_tokens=1000
    )
    assert big == [[("g1", "x" * 400_000)]]


def test_token_cap_end_to_end(conn, profile, tmp_path):  # noqa: F811
    for i in range(1, 5):
        add_group(conn, i, profile, description=f"Distinct duty number {i}. " + "y" * 8000)
    log: list = []
    fixed = (len(RUBRIC_TEXT) + len(scoring_inputs(profile)) + 1500) // 4
    scorer = make(serve(log, all_valid), tmp_path, max_input_tokens_per_request=fixed + 5000)
    screen.score_sync(conn, scorer, profile, limit=10, now=NOW)
    assert len(log) == 2 and all(len(ids_in(b)) == 2 for b in log)


# ─── Config, single-mode and CLI ────────────────────────────────────────────


def test_config_cap_and_defaults():
    s = Scoring()
    for section in (s.openai_compat, s.openrouter, s.local):
        assert section.jobs_per_request == 1
        assert section.max_input_tokens_per_request == 40_000
    assert s.openrouter.est_cost_per_request_usd == 0.04
    with pytest.raises(ValueError):
        OpenRouter(jobs_per_request=17)
    with pytest.raises(ValueError):
        OpenRouter(jobs_per_request=0)


def test_single_mode_unchanged(conn, profile, tmp_path):  # noqa: F811
    seed(conn, profile, 2)
    log: list = []

    def handler(req):
        body = json.loads(req.content)
        log.append(body)
        n = int(re.search(r"Systems Engineer (\d+)", body["messages"][1]["content"]).group(1))
        r = reply([])
        r["choices"][0]["message"]["content"] = screen_json([quote(n)])
        return httpx.Response(200, json=r)

    scorer = make(handler, tmp_path, jobs_per_request=1)
    res = screen.score_sync(conn, scorer, profile, limit=5, now=NOW)
    assert len(log) == 2 and res.written == 2
    assert all("BEGIN POSTING" not in b["messages"][1]["content"] for b in log)
    assert all("judgments" not in json.dumps(b["response_format"]) for b in log)
    assert "requests" not in res.as_dict()
    assert conn.execute("SELECT calls FROM llm_spend").fetchone()[0] == 2


def test_cli_jobs_per_request_override(tmp_path, monkeypatch):
    from jobhunter import cli
    from jobhunter.config import load_settings
    from jobhunter.scoring import profile as profile_mod

    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'[paths]\ndb_path = "{tmp_path / "t.db"}"\n[scoring.openrouter]\njobs_per_request = 3\n'
    )
    monkeypatch.setenv("JOBHUNTER_CONFIG", str(cfg))
    c = db.connect(load_settings().paths.db_path)
    db.migrate(c)
    c.close()
    seen: list = []

    def fake_sync(_conn, scorer, _profile, **kw):
        seen.append(scorer.jobs_per_request)
        return screen.SyncResult()

    monkeypatch.setattr(profile_mod, "load_profile", lambda *a, **k: object())
    monkeypatch.setattr(screen, "score_sync", fake_sync)
    run = lambda *extra: CliRunner().invoke(  # noqa: E731
        cli.app, ["score", "--submit", "--scorer", f"openrouter:{SLUG}", *extra]
    )
    assert run().exit_code == 0
    assert run("--jobs-per-request", "8").exit_code == 0
    assert seen == [3, 8]
    assert run("--jobs-per-request", "17").exit_code == 2
    anth = CliRunner().invoke(
        cli.app,
        ["score", "--submit", "--scorer", "anthropic:claude-haiku-4-5", "--jobs-per-request", "4"],
    )
    assert anth.exit_code == 2


# ─── bench ──────────────────────────────────────────────────────────────────


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def test_bench_packed_reports_per_job(conn, profile, tmp_path):  # noqa: F811
    seed(conn, profile, 4)
    clock = FakeClock()

    def handler(req):
        clock.t += 12.0
        return httpx.Response(200, json=all_valid(json.loads(req.content)))

    scorer = make(handler, tmp_path, jobs_per_request=1)
    before = conn.execute("SELECT count(*) FROM fit_score").fetchone()[0]
    rep = run_bench(conn, scorer, profile, n=4, clock=clock, jobs_per_request=2)
    assert conn.execute("SELECT count(*) FROM fit_score").fetchone()[0] == before == 0
    assert conn.execute("SELECT count(*) FROM llm_spend").fetchone()[0] == 0
    assert rep.attempted == 4 and len(rep.seconds) == 2
    assert rep.seconds_per_job == pytest.approx(6.0)
    assert rep.cost_per_job == pytest.approx(0.02)
    assert rep.schema_valid == 4 and (rep.evidence_verified, rep.evidence_total) == (4, 4)
    out = rep.format()
    assert "per job: 6.0 s (per request: 12.0 s)" in out
    assert "cost per job: $0.0200 (per request: $0.0400)" in out
    assert "2 packed requests of up to 2" in out


def test_scorer_from_string_reads_config():
    scoring = Scoring(openrouter=OpenRouter(jobs_per_request=8))
    assert scorer_from_string(f"openrouter:{SLUG}", scoring=scoring).jobs_per_request == 8
    assert scorer_from_string("local:m").jobs_per_request == 1
