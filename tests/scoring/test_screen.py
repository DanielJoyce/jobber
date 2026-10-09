"""Stage 2 screen with a fake Anthropic client (specs/006). No network, synthetic data only."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from anthropic.types import Message
from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
from anthropic.types.messages import MessageBatchIndividualResponse
from anthropic.types.messages.batch_create_params import Request
from pydantic import TypeAdapter

from jobhunter.core import db
from jobhunter.core.models import Screen
from jobhunter.scoring import screen
from jobhunter.scoring.profile import (
    CurrentFocus,
    Hard,
    Narrative,
    Profile,
    SalaryFloor,
    Soft,
)
from jobhunter.scoring.rubric import PROMPT_VERSION, RUBRIC_TEXT

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
SCORER = "anthropic:claude-haiku-4-5"
SALARY_FLOOR = 987654  # a marker: must never appear in a prompt

DESCRIPTION = (
    "Own the   Linux fleet for a small team.\n"
    "Write Terraform modules and Go tooling. Participate in a shared weekly on-call rotation."
)


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.migrate(c)
    c.execute(
        "INSERT INTO source (key, class, name, family, tier, entry, policy) "
        "VALUES ('wa', 'B', 'WA', 'neogov', 'http', 'https://example.com', 'enabled')"
    )
    yield c
    c.close()


@pytest.fixture
def profile() -> Profile:
    return Profile(
        hard=Hard(
            states_allowed=["CO", "WA"],
            salary_floor=SalaryFloor(amount=SALARY_FLOOR, period="year"),
        ),
        soft=Soft(state_ranking=["CO", "WA"]),
        current_focus=CurrentFocus(
            since="2023-01", doing="Linux fleet operations", done_with=["desk-side support"]
        ),
        narrative=Narrative(avoid="24/7 on-call rotations"),
        resume_text="Synthetic Person\n- 2023-present: ran synthetic hosts.\n",
    )


def add_group(
    conn,
    n: int,
    profile: Profile,
    *,
    passed: bool | None = True,
    filter_version: str | None = None,
    description: str = DESCRIPTION,
    completeness: str = "full",
) -> int:
    job_id = conn.execute(
        "INSERT INTO job (source_key, external_id, url, title, employer, description_text, "
        "description_completeness, salary_raw, location_scope, remote, employment_type, stage, "
        "first_seen_at, last_seen_at) VALUES ('wa', ?, 'https://example.com/j', ?, "
        "'Synthetic Agency', ?, ?, '$40.00 - $50.00 hourly', 'single', 'hybrid', 'full_time', "
        "'prefiltered', 'x', 'x')",
        (f"e{n}", f"Systems Engineer {n}", description, completeness),
    ).lastrowid
    group_id = conn.execute(
        "INSERT INTO job_group (canonical_job_id, method, created_at) "
        "VALUES (?, 'exact_hash', 'x')",
        (job_id,),
    ).lastrowid
    conn.execute("UPDATE job SET job_group_id = ? WHERE id = ?", (group_id, job_id))
    conn.execute(
        "INSERT INTO job_locations (job_id, state, city, is_primary) "
        "VALUES (?, 'WA', 'Olympia', 1)",
        (job_id,),
    )
    if passed is not None:
        conn.execute(
            "INSERT INTO prefilter_result (job_id, passed, reasons, filter_version, evaluated_at) "
            "VALUES (?, ?, '[]', ?, 'x')",
            (job_id, int(passed), filter_version or profile.filter_version),
        )
    return group_id


def screen_json(quotes: list[str]) -> str:
    return json.dumps(
        {
            "verdict": "strong",
            "dimensions": {
                "skills": {"score": 88, "why": "Daily work."},
                "seniority": {"score": 80, "why": "match: senior IC."},
                "domain": {"score": 70, "why": "Public sector."},
            },
            "raw_skills": 90,
            "recency_weighted_skills": 88,
            "stale_skills": [],
            "current_focus_overlap": 85,
            "done_with_hits": [],
            "evidence": [{"claim": "Fleet work", "quote": q} for q in quotes],
            "blockers": [],
            "missing_info": ["salary range"],
            "shape_flags": [],
            "tailoring_hints": ["Lead with fleet work."],
        }
    )


def message(text: str, *, cache_read: int = 3000, stop_reason: str = "end_turn") -> dict:
    return {
        "id": "msg_x",
        "type": "message",
        "role": "assistant",
        "model": "claude-haiku-4-5",
        "content": [{"type": "text", "text": text}],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": 2000,
            "output_tokens": 350,
            "cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": 0,
        },
    }


def succeeded(custom_id: str, text: str, **kw: Any) -> MessageBatchIndividualResponse:
    return MessageBatchIndividualResponse.model_validate(
        {"custom_id": custom_id, "result": {"type": "succeeded", "message": message(text, **kw)}}
    )


def errored(custom_id: str) -> MessageBatchIndividualResponse:
    return MessageBatchIndividualResponse.model_validate(
        {
            "custom_id": custom_id,
            "result": {
                "type": "errored",
                "error": {"type": "error", "error": {"type": "api_error", "message": "boom"}},
            },
        }
    )


class FakeBatches:
    def __init__(self) -> None:
        self.created: list[list[dict]] = []
        self.results_by_id: dict[str, list] = {}
        self.status = "ended"

    def create(self, *, requests):
        self.created.append(list(requests))
        return SimpleNamespace(id=f"msgbatch_{len(self.created)}", processing_status="in_progress")

    def retrieve(self, batch_id):
        return SimpleNamespace(id=batch_id, processing_status=self.status)

    def results(self, batch_id):
        return iter(self.results_by_id[batch_id])


class FakeMessages:
    def __init__(self) -> None:
        self.batches = FakeBatches()
        self.parse_calls: list[dict] = []
        self.parse_reply: dict | None = None

    def parse(self, **params):
        self.parse_calls.append(params)
        return Message.model_validate(self.parse_reply)


class FakeClient:
    def __init__(self) -> None:
        self.messages = FakeMessages()


def all_prompt_text(params: dict) -> str:
    parts = [block["text"] for block in params["system"]]
    parts += [m["content"] for m in params["messages"]]
    return "\n".join(parts)


# ─── Request shape ──────────────────────────────────────────────────────────


def test_request_shape(conn, profile):
    add_group(conn, 1, profile)
    client = FakeClient()
    batch_id = screen.submit_batch(conn, client, profile, limit=10, now=NOW, scorer=SCORER)
    assert batch_id == "msgbatch_1"
    [requests] = client.messages.batches.created
    [request] = requests
    TypeAdapter(Request).validate_python(request)
    params = request["params"]
    TypeAdapter(MessageCreateParamsNonStreaming).validate_python(params)

    assert request["custom_id"] == "g1"
    assert params["model"] == "claude-haiku-4-5"
    assert "thinking" not in params
    assert "effort" not in params.get("output_config", {})
    assert "output_format" not in params
    assert params["output_config"]["format"]["type"] == "json_schema"

    system = params["system"]
    assert len(system) == 2
    assert system[0]["text"] == RUBRIC_TEXT
    assert "cache_control" not in system[0]
    assert system[-1]["cache_control"] == {"type": "ephemeral"}
    assert "Synthetic Person" in system[1]["text"]

    [user] = params["messages"]
    assert user["role"] == "user"
    body = user["content"]
    assert "Systems Engineer 1" in body
    assert "Synthetic Agency" in body
    assert "Olympia, WA (scope: single; remote: hybrid)" in body
    assert "$40.00 - $50.00 hourly" in body
    assert "full time" in body
    assert "Own the   Linux fleet" in body
    assert "partial" not in body


def test_partial_description_is_announced(conn, profile):
    add_group(conn, 1, profile, completeness="partial")
    client = FakeClient()
    screen.submit_batch(conn, client, profile, limit=10, now=NOW, scorer=SCORER)
    body = client.messages.batches.created[0][0]["params"]["messages"][0]["content"]
    assert "description below is partial" in body


def test_no_filter_data_or_volatile_values_in_prompt(conn, profile):
    for n in (1, 2):
        add_group(conn, n, profile)
    client = FakeClient()
    screen.submit_batch(conn, client, profile, limit=10, now=NOW, scorer=SCORER)
    requests = client.messages.batches.created[0]
    for request in requests:
        text = all_prompt_text(request["params"])
        assert str(SALARY_FLOOR) not in text
        assert "987,654" not in text
        assert "state_ranking" not in text
        assert "states_allowed" not in text
        assert profile.filter_version not in text
        assert "2026" not in text  # no timestamps
        assert request["custom_id"] not in text  # ids are in custom_id, never the prompt
    # The cached prefix is byte-identical across requests.
    assert requests[0]["params"]["system"] == requests[1]["params"]["system"]


def test_model_id_strips_provider():
    assert screen.model_id("anthropic:claude-haiku-4-5") == "claude-haiku-4-5"
    assert screen.model_id("claude-haiku-4-5") == "claude-haiku-4-5"
    with pytest.raises(ValueError):
        screen.model_id("openai-compat:local")


# ─── Eligibility ────────────────────────────────────────────────────────────


def test_eligibility_requires_current_prefilter_pass(conn, profile):
    add_group(conn, 1, profile, passed=True)
    add_group(conn, 2, profile, passed=False)
    add_group(conn, 3, profile, passed=None)  # not prefiltered yet
    add_group(conn, 4, profile, passed=True, filter_version="old")
    rows = screen.eligible_groups(conn, profile, scorer=SCORER, limit=10)
    assert [r["group_id"] for r in rows] == [1]


def test_pending_batch_groups_are_not_resubmitted(conn, profile):
    add_group(conn, 1, profile)
    client = FakeClient()
    assert screen.submit_batch(conn, client, profile, limit=10, now=NOW, scorer=SCORER)
    assert screen.submit_batch(conn, client, profile, limit=10, now=NOW, scorer=SCORER) is None


def test_spend_cap_limits_batch(conn, profile):
    for n in range(1, 6):
        add_group(conn, n, profile)
    client = FakeClient()
    cost = screen.ESTIMATED_COST_PER_REQUEST_USD
    batch_id = screen.submit_batch(
        conn, client, profile, limit=10, now=NOW, scorer=SCORER, remaining_usd=lambda: cost * 2.5
    )
    assert batch_id is not None
    assert len(client.messages.batches.created[0]) == 2
    assert (
        screen.submit_batch(
            conn, client, profile, limit=10, now=NOW, scorer=SCORER, remaining_usd=lambda: 0.0
        )
        is None
    )


def test_remaining_daily_budget(conn, profile):
    conn.execute(
        "INSERT INTO llm_spend (day, model, tier, calls, cost_usd) "
        "VALUES ('2026-10-09', 'm', 'screen', 1, 0.5)"
    )
    add_group(conn, 1, profile)
    screen.submit_batch(conn, FakeClient(), profile, limit=10, now=NOW, scorer=SCORER)
    remaining = screen.remaining_daily_budget(conn, 2.0, NOW)
    assert remaining == pytest.approx(2.0 - 0.5 - screen.ESTIMATED_COST_PER_REQUEST_USD)


# ─── Evidence verification ──────────────────────────────────────────────────


def test_verify_evidence_normalizes_whitespace_and_case():
    s = Screen.model_validate_json(screen_json(["own the linux FLEET for a small team."]))
    _, missing = screen.verify_evidence(s, DESCRIPTION)
    assert missing == 0


def test_verify_evidence_catches_fabrication():
    s = Screen.model_validate_json(
        screen_json(["Write Terraform modules", "Requires a deep love of Kubernetes"])
    )
    _, missing = screen.verify_evidence(s, DESCRIPTION)
    assert missing == 1


def test_verify_evidence_rejects_empty_quote():
    s = Screen.model_validate_json(screen_json(["  "]))
    assert screen.verify_evidence(s, DESCRIPTION)[1] == 1


# ─── Collect ────────────────────────────────────────────────────────────────


def submit_three(conn, profile) -> tuple[FakeClient, str]:
    for n in (1, 2, 3):
        add_group(conn, n, profile)
    client = FakeClient()
    batch_id = screen.submit_batch(conn, client, profile, limit=10, now=NOW, scorer=SCORER)
    assert batch_id is not None
    # Results arrive out of order: one good, one errored, one with a fabricated quote.
    client.messages.batches.results_by_id[batch_id] = [
        succeeded("g3", screen_json(["Write Terraform modules", "Fabricated requirement here"])),
        errored("g2"),
        succeeded("g1", screen_json(["Write Terraform modules and Go tooling."]), cache_read=0),
    ]
    return client, batch_id


def test_collect_writes_scores(conn, profile):
    client, batch_id = submit_three(conn, profile)
    result = screen.collect_batch(conn, client, batch_id, profile, now=NOW)
    assert (result.succeeded, result.written, result.errored, result.unverified) == (2, 2, 1, 1)

    rows = {
        r["job_group_id"]: r
        for r in conn.execute("SELECT * FROM fit_score ORDER BY job_group_id").fetchall()
    }
    assert set(rows) == {1, 3}
    good, bad = rows[1], rows[3]
    assert good["tier"] == "screen"
    assert good["model"] == SCORER
    assert good["prompt_version"] == PROMPT_VERSION
    assert good["scoring_version"] == profile.scoring_version
    assert good["batch_id"] == batch_id
    assert good["overall"] == 0  # computed later in Python
    assert good["verdict"] == "strong"
    assert good["evidence_unverified"] == 0
    assert bad["evidence_unverified"] == 1
    dims = json.loads(good["dimensions"])
    assert dims["skills"]["score"] == 88
    assert dims["raw_skills"] == 90
    assert dims["recency_weighted_skills"] == 88
    assert dims["current_focus_overlap"] == 85
    assert dims["evidence_unverified"] is False
    assert dims["seniority_direction"] == "match"
    assert json.loads(bad["dimensions"])["evidence_unverified"] is True
    assert [e["verified"] for e in json.loads(bad["evidence"])] == [True, False]
    assert json.loads(good["missing_info"]) == ["salary range"]
    assert json.loads(good["tailoring_hints"]) == ["Lead with fleet work."]
    assert json.loads(good["shape_flags"]) == []
    assert good["input_tokens"] == 2000
    assert good["output_tokens"] == 350
    assert good["cache_read_tokens"] == 0
    assert bad["cache_read_tokens"] == 3000

    stages = dict(conn.execute("SELECT job_group_id, stage FROM job").fetchall())
    assert stages == {1: "scored", 2: "prefiltered", 3: "scored"}

    batch = conn.execute("SELECT * FROM score_batch WHERE id = ?", (batch_id,)).fetchone()
    assert batch["collected_at"] is not None
    results = dict(conn.execute("SELECT custom_id, result FROM score_batch_item").fetchall())
    assert results == {"g1": "succeeded", "g2": "errored", "g3": "succeeded"}


def test_errored_results_stay_eligible(conn, profile):
    client, batch_id = submit_three(conn, profile)
    screen.collect_batch(conn, client, batch_id, profile, now=NOW)
    rows = screen.eligible_groups(conn, profile, scorer=SCORER, limit=10)
    assert [r["group_id"] for r in rows] == [2]


def test_collect_twice_writes_once(conn, profile):
    client, batch_id = submit_three(conn, profile)
    screen.collect_batch(conn, client, batch_id, profile, now=NOW)
    again = screen.collect_batch(conn, client, batch_id, profile, now=NOW)
    assert again.status == "already_collected"
    assert conn.execute("SELECT count(*) FROM fit_score").fetchone()[0] == 2
    spend = conn.execute("SELECT calls FROM llm_spend").fetchone()[0]
    assert spend == 2


def test_unique_key_respected_across_batches(conn, profile):
    client, batch_id = submit_three(conn, profile)
    screen.collect_batch(conn, client, batch_id, profile, now=NOW)
    # A stale second batch for an already-scored group must not add a row.
    conn.execute(
        "INSERT INTO score_batch (id, tier, model, prompt_version, scoring_version, "
        "request_count, submitted_at) VALUES ('b2', 'screen', ?, ?, ?, 1, 'x')",
        (SCORER, PROMPT_VERSION, profile.scoring_version),
    )
    conn.execute(
        "INSERT INTO score_batch_item (batch_id, custom_id, job_group_id) VALUES ('b2', 'g1', 1)"
    )
    client.messages.batches.results_by_id["b2"] = [
        succeeded("g1", screen_json(["Write Terraform modules"]))
    ]
    result = screen.collect_batch(conn, client, "b2", profile, now=NOW)
    assert (result.written, result.duplicate) == (0, 1)
    assert conn.execute("SELECT count(*) FROM fit_score WHERE job_group_id = 1").fetchone()[0] == 1


def test_collect_waits_for_batch_end(conn, profile):
    client, batch_id = submit_three(conn, profile)
    client.messages.batches.status = "in_progress"
    result = screen.collect_batch(conn, client, batch_id, profile, now=NOW)
    assert result.status == "in_progress"
    assert conn.execute("SELECT count(*) FROM fit_score").fetchone()[0] == 0


def test_invalid_output_stays_eligible(conn, profile):
    add_group(conn, 1, profile)
    client = FakeClient()
    batch_id = screen.submit_batch(conn, client, profile, limit=10, now=NOW, scorer=SCORER)
    client.messages.batches.results_by_id[batch_id] = [
        succeeded("g1", '{"verdict": "strong"}'),
    ]
    result = screen.collect_batch(conn, client, batch_id, profile, now=NOW)
    assert (result.succeeded, result.invalid, result.written) == (1, 1, 0)
    assert [
        r["group_id"] for r in screen.eligible_groups(conn, profile, scorer=SCORER, limit=5)
    ] == [1]


# ─── Cost ───────────────────────────────────────────────────────────────────


def test_cost_batch_pricing():
    usage = SimpleNamespace(
        input_tokens=2000,
        output_tokens=350,
        cache_read_input_tokens=3000,
        cache_creation_input_tokens=0,
    )
    # specs/006 "Cost": 0.00100 + 0.00015 + 0.000875
    assert screen.compute_cost(usage, SCORER, batch=True) == pytest.approx(0.002025)
    assert screen.compute_cost(usage, SCORER, batch=False) == pytest.approx(0.00405)


def test_cost_counts_cache_writes():
    usage = SimpleNamespace(
        input_tokens=0, output_tokens=0, cache_read_input_tokens=0, cache_creation_input_tokens=1000
    )
    assert screen.compute_cost(usage, SCORER, batch=True) == pytest.approx(1000 * 1.25 * 0.5 / 1e6)


def test_collect_records_cost_and_spend(conn, profile):
    client, batch_id = submit_three(conn, profile)
    result = screen.collect_batch(conn, client, batch_id, profile, now=NOW)
    per_cached = 0.002025
    per_uncached = (2000 * 0.5 + 350 * 2.5) / 1e6
    assert result.cost_usd == pytest.approx(per_cached + per_uncached)
    costs = dict(conn.execute("SELECT job_group_id, cost_usd FROM fit_score").fetchall())
    assert costs[3] == pytest.approx(per_cached)
    assert costs[1] == pytest.approx(per_uncached)
    spend = conn.execute("SELECT * FROM llm_spend").fetchone()
    assert (spend["day"], spend["model"], spend["tier"], spend["calls"]) == (
        "2026-10-09",
        SCORER,
        "screen",
        2,
    )
    assert spend["cost_usd"] == pytest.approx(per_cached + per_uncached)
    assert spend["input_tokens"] == 2000 + 5000


# ─── Cache check ────────────────────────────────────────────────────────────


def test_cache_warning_when_no_reads(conn, profile, caplog):
    for n in (1, 2):
        add_group(conn, n, profile)
    client = FakeClient()
    batch_id = screen.submit_batch(conn, client, profile, limit=10, now=NOW, scorer=SCORER)
    client.messages.batches.results_by_id[batch_id] = [
        succeeded("g1", screen_json(["Write Terraform modules"]), cache_read=0),
        succeeded("g2", screen_json(["Write Terraform modules"]), cache_read=0),
    ]
    with caplog.at_level(logging.WARNING, logger="jobhunter.scoring.screen"):
        result = screen.collect_batch(conn, client, batch_id, profile, now=NOW)
    assert result.cache_read_zero == 1
    assert "minimum cacheable" in caplog.text
    assert "volatile" in caplog.text


def test_no_cache_warning_when_reads_happen(caplog):
    with caplog.at_level(logging.WARNING, logger="jobhunter.scoring.screen"):
        assert screen.check_cache_reads([0, 3000, 3000]) == 0
        assert screen.check_cache_reads([0]) == 0
    assert "cache" not in caplog.text


# ─── Synchronous path ───────────────────────────────────────────────────────


def test_score_one_uses_parse(conn, profile):
    group_id = add_group(conn, 1, profile)
    client = FakeClient()
    client.messages.parse_reply = message(screen_json(["Write Terraform modules"]))
    row_id = screen.score_one(conn, client, profile, group_id, now=NOW, scorer=SCORER)
    assert row_id is not None
    [params] = client.messages.parse_calls
    assert params["model"] == "claude-haiku-4-5"
    assert "thinking" not in params
    assert "output_format" not in params
    assert params["system"][-1]["cache_control"] == {"type": "ephemeral"}
    row = conn.execute("SELECT * FROM fit_score").fetchone()
    assert row["batch_id"] is None
    assert row["cost_usd"] == pytest.approx((2000 * 1 + 3000 * 0.1 + 350 * 5) / 1e6)
    # Already scored under this key: no second row.
    assert screen.score_one(conn, client, profile, group_id, now=NOW, scorer=SCORER) is None
    assert conn.execute("SELECT count(*) FROM fit_score").fetchone()[0] == 1


def test_score_one_refusal_raises(conn, profile):
    group_id = add_group(conn, 1, profile)
    client = FakeClient()
    client.messages.parse_reply = message("{}", stop_reason="refusal")
    with pytest.raises(screen.ScreenError):
        screen.score_one(conn, client, profile, group_id, now=NOW, scorer=SCORER)


@pytest.mark.parametrize(
    ("why", "expected"),
    [
        ("above: leads four teams.", "above"),
        ("Below: entry level", "below"),
        ("no prefix", "match"),
    ],
)
def test_seniority_direction_from_why(why, expected):
    s = Screen.model_validate_json(screen_json(["x"]))
    s.dimensions["seniority"].why = why
    assert screen.seniority_direction(s) == expected


def test_stored_row_feeds_buckets(conn, profile):
    from jobhunter.scoring import buckets

    client, batch_id = submit_three(conn, profile)
    screen.collect_batch(conn, client, batch_id, profile, now=NOW)
    row = conn.execute("SELECT * FROM fit_score WHERE job_group_id = 1").fetchone()
    dims = json.loads(row["dimensions"])
    assert buckets.seniority_direction(dims) == "match"
    job = conn.execute("SELECT * FROM job WHERE job_group_id = 1").fetchone()
    computed = buckets.compute_row(dict(row), dict(job), [], profile)
    assert computed.raw_skills == 90
