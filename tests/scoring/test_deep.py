"""Stage 3 deep pass with a fake streaming client (specs/006). No network, synthetic data."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from jobhunter.core import db
from jobhunter.pipeline.locations import load_locations
from jobhunter.scoring import deep
from jobhunter.scoring.buckets import compute_row
from jobhunter.scoring.profile import (
    CurrentFocus,
    Hard,
    Narrative,
    Profile,
    SalaryFloor,
    Soft,
)
from jobhunter.scoring.rubric import RUBRIC_TEXT

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
DESCRIPTION = (
    "Own the   Linux fleet for a small team.\n"
    "Write Terraform modules and Go tooling. Participate in a shared weekly on-call rotation."
)
GOOD = "Write Terraform modules and Go tooling"


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
            states_allowed=["CO", "WA"], salary_floor=SalaryFloor(amount=987654, period="year")
        ),
        soft=Soft(state_ranking=["CO", "WA"]),
        current_focus=CurrentFocus(since="2023-01", doing="Linux fleet", done_with=["helpdesk"]),
        narrative=Narrative(avoid="24/7 on-call"),
        resume_text="Synthetic Person\n- 2023-present: ran synthetic hosts.\n",
    )


def add_group(conn, n: int) -> int:
    job_id = conn.execute(
        "INSERT INTO job (source_key, external_id, url, title, employer, description_text, "
        "description_completeness, salary_raw, location_scope, remote, employment_type, stage, "
        "first_seen_at, last_seen_at) VALUES ('wa', ?, 'https://example.com/j', ?, "
        "'Synthetic Agency', ?, 'full', '$40.00 - $50.00 hourly', 'single', 'hybrid', "
        "'full_time', 'scored', 'x', 'x')",
        (f"e{n}", f"Systems Engineer {n}", DESCRIPTION),
    ).lastrowid
    gid = conn.execute(
        "INSERT INTO job_group (canonical_job_id, method, created_at) "
        "VALUES (?, 'exact_hash', 'x')",
        (job_id,),
    ).lastrowid
    conn.execute("UPDATE job SET job_group_id = ? WHERE id = ?", (gid, job_id))
    conn.execute(
        "INSERT INTO job_locations (job_id, state, city, is_primary) "
        "VALUES (?, 'WA', 'Olympia', 1)",
        (job_id,),
    )
    return gid


def add_screen(conn, gid: int, profile: Profile, *, skills: int = 90, domain: int = 80) -> None:
    dims = {
        "skills": {"score": skills, "why": "x"},
        "seniority": {"score": 80, "why": "match: x"},
        "domain": {"score": domain, "why": "x"},
        "raw_skills": skills,
        "recency_weighted_skills": skills,
        "seniority_direction": "match",
    }
    conn.execute(
        "INSERT INTO fit_score (job_group_id, tier, model, prompt_version, scoring_version, "
        "verdict, overall, dimensions, evidence, blockers, created_at) "
        "VALUES (?, 'screen', 'anthropic:claude-haiku-4-5', 'p', ?, 'strong', 0, ?, '[]', "
        "'[]', 'x')",
        (gid, profile.scoring_version, json.dumps(dims)),
    )


def report(*, skills: int = 90, domain: int = 80, quote: str = GOOD, gap_quote: str = GOOD) -> str:
    return json.dumps(
        {
            "verdict": "strong",
            "dimensions": {
                "skills": {"score": skills, "why": "Daily work."},
                "seniority": {"score": 80, "why": "match: senior IC."},
                "domain": {"score": domain, "why": "Public sector."},
            },
            "raw_skills": skills,
            "recency_weighted_skills": skills,
            "stale_skills": [],
            "current_focus_overlap": 85,
            "done_with_hits": [],
            "evidence": [{"claim": "Tooling", "quote": quote}],
            "blockers": [],
            "missing_info": ["salary range"],
            "shape_flags": [],
            "tailoring_hints": ["Lead with fleet work."],
            "recommendation": "apply",
            "summary": "Good fit.",
            "requirement_gaps": [
                {"requirement": "Terraform", "status": "met", "evidence": gap_quote}
            ],
            "emphasize": ["Fleet work", "Terraform"],
            "questions_to_ask": ["Is on-call paid?"],
            "salary_read": "Hourly range stated.",
            "screen_disagreement": {"flag": False, "reason": ""},
        }
    )


def final(
    text: str, *, stop_reason: str = "end_turn", model: str = "claude-opus-5-5"
) -> SimpleNamespace:
    return SimpleNamespace(
        model=model,
        stop_reason=stop_reason,
        content=[
            SimpleNamespace(type="thinking", thinking="hmm"),
            SimpleNamespace(type="text", text=text),
        ],
        usage=SimpleNamespace(
            input_tokens=5000,
            output_tokens=1500,
            cache_read_input_tokens=3000,
            cache_creation_input_tokens=0,
        ),
    )


class FakeStream:
    def __init__(self, message):
        self.message = message

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self.message


class FakeClient:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls: list[dict[str, Any]] = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))

    def _stream(self, **params):
        self.calls.append(params)
        return FakeStream(self.replies.pop(0))


def test_request_shape(conn, profile):
    gid = add_group(conn, 1)
    client = FakeClient(final(report()))
    res = deep.deep_score(conn, client, profile, gid, now=NOW)
    assert res.status == "scored"
    (p,) = client.calls
    assert p["model"] == "claude-opus-5-5"
    assert p["thinking"] == {"type": "adaptive"}
    assert p["output_config"]["effort"] == "high"
    assert p["output_config"]["format"]["type"] == "json_schema"
    assert "temperature" not in p and "budget_tokens" not in json.dumps(p)
    assert p["max_tokens"] == 32000  # room for Opus 5.5's thinking plus the report
    assert p["betas"] == ["server-side-fallback-2026-07-01"]
    assert p["fallbacks"] == "default"
    assert [m["role"] for m in p["messages"]] == ["user"]
    system = p["system"]
    assert system[0]["text"] == RUBRIC_TEXT
    assert system[1]["cache_control"] == {"type": "ephemeral"}
    assert "Resume" in system[1]["text"]
    assert "Deep pass" in system[2]["text"] and "cache_control" not in system[2]
    assert "987654" not in json.dumps(p)


def test_scored_row_readable_by_buckets_and_cost(conn, profile):
    gid = add_group(conn, 1)
    res = deep.deep_score(conn, FakeClient(final(report())), profile, gid, now=NOW)
    row = res.row
    assert row["tier"] == "deep" and row["model"] == "anthropic:claude-opus-5-5"
    assert row["evidence_unverified"] == 0
    job = conn.execute("SELECT * FROM job").fetchone()
    fit = compute_row(row, job, load_locations(conn, job["id"]), profile)
    assert fit.bucket.value in "ABCDEFG"
    stored = json.loads(row["deep_report"])
    assert stored["recommendation"] == "apply"
    assert stored["requirement_gaps"][0]["verified"] is True
    assert stored["screen_disagreement"]["screen_bucket"] is None
    spend = conn.execute("SELECT * FROM llm_spend").fetchone()
    # Opus 5.5: 5000 in at $4 + 3000 cache read at $0.20 (0.05x) + 1500 out at $20, per MTok
    assert spend["tier"] == "deep" and spend["calls"] == 1
    assert spend["cost_usd"] == pytest.approx(0.0506)
    assert row["cost_usd"] == pytest.approx(0.0506)


def test_a_fallback_answer_is_priced_at_the_model_that_answered(conn, profile):
    """A server-side fallback from Opus 5.5 to Opus 5 bills at Opus 5's rates (kept in the
    price table for exactly this and for stored history)."""
    gid = add_group(conn, 1)
    msg = final(report(), model="claude-opus-5")
    res = deep.deep_score(conn, FakeClient(msg), profile, gid, now=NOW)
    # 5000 in at $5 + 3000 cache read at $0.50 + 1500 out at $25, per MTok
    assert res.row["cost_usd"] == pytest.approx(0.064)
    assert res.row["model"] == "anthropic:claude-opus-5-5"
    assert res.row["served_model"] == "claude-opus-5"


def test_a_fallback_to_opus_4_8_is_priced_at_opus_4_8_rates(conn, profile):
    gid = add_group(conn, 1)
    msg = final(report(), model="claude-opus-4-8")
    res = deep.deep_score(conn, FakeClient(msg), profile, gid, now=NOW)
    # $5 / $0.50 cache read / $25 per MTok, not Opus 5.5's $4 / $0.20 / $20 ($0.0506)
    assert res.row["cost_usd"] == pytest.approx(0.064)


def test_every_fallback_attempt_in_usage_iterations_is_recorded(conn, profile):
    """The declined Opus 5.5 attempt is billed too; usage.iterations is the per-attempt
    record and top-level usage covers only the answering model."""
    gid = add_group(conn, 1)
    msg = final(report(), model="claude-opus-4-8")
    msg.usage.iterations = [
        SimpleNamespace(
            type="message",
            model="claude-opus-5-5",
            input_tokens=5000,
            output_tokens=800,
            cache_read_input_tokens=3000,
            cache_creation_input_tokens=0,
        ),
        SimpleNamespace(
            type="fallback_message",
            model="claude-opus-4-8",
            input_tokens=5000,
            output_tokens=1500,
            cache_read_input_tokens=3000,
            cache_creation_input_tokens=0,
        ),
    ]
    res = deep.deep_score(conn, FakeClient(msg), profile, gid, now=NOW)
    declined = (5000 * 4 + 3000 * 0.20 + 800 * 20) / 1e6
    answered = (5000 * 5 + 3000 * 0.50 + 1500 * 25) / 1e6
    assert res.row["cost_usd"] == pytest.approx(declined + answered)
    spent = conn.execute("SELECT sum(cost_usd) FROM llm_spend").fetchone()[0]
    assert spent == pytest.approx(declined + answered)


@pytest.mark.parametrize("stop", ["max_tokens", "refusal"])
def test_a_paid_attempt_that_wrote_nothing_is_not_shortlisted_again(conn, profile, stop):
    g1, g2 = (add_group(conn, n) for n in (1, 2))
    add_screen(conn, g1, profile, skills=95, domain=90)
    add_screen(conn, g2, profile, skills=60, domain=60)
    cut = FakeClient(final('{"verdict": "str', stop_reason=stop))
    res = deep.deep_score(conn, cut, profile, g1, now=NOW)
    assert res.status in ("invalid", "refused") and res.cost_usd > 0
    assert deep.shortlist(conn, profile, 2) == [g2]  # not paid for again by --deep N
    # A new description revision tries again; so does asking for the group by id.
    conn.execute("UPDATE job_group SET description_rev = description_rev + 1 WHERE id = ?", (g1,))
    assert deep.shortlist(conn, profile, 2) == [g1, g2]
    again = FakeClient(final(report()))
    assert deep.deep_score(conn, again, profile, g1, now=NOW).status == "scored"


def test_rerun_returns_stored_row_without_calling(conn, profile):
    gid = add_group(conn, 1)
    client = FakeClient(final(report()))
    first = deep.deep_score(conn, client, profile, gid, now=NOW)
    again = deep.deep_score(conn, client, profile, gid, now=NOW)
    assert again.status == "existing" and again.row["id"] == first.row["id"]
    assert len(client.calls) == 1
    assert conn.execute("SELECT count(*) FROM fit_score WHERE tier='deep'").fetchone()[0] == 1


def test_refusal_is_not_reshortlisted_but_can_be_run_by_id(conn, profile):
    gid = add_group(conn, 1)
    add_screen(conn, gid, profile)
    client = FakeClient(final("", stop_reason="refusal"), final(report()))
    res = deep.deep_score(conn, client, profile, gid, now=NOW)
    assert res.status == "refused" and res.row is None
    assert conn.execute("SELECT count(*) FROM fit_score WHERE tier='deep'").fetchone()[0] == 0
    assert conn.execute("SELECT calls FROM llm_spend").fetchone()[0] == 1
    # A refusal is billed; --deep N does not pay for it again on every run (score_failure),
    # while --deep-group still runs it on request.
    assert deep.shortlist(conn, profile, 5) == []
    assert deep.deep_score(conn, client, profile, gid, now=NOW).status == "scored"


def test_truncated_output_is_invalid_not_stored(conn, profile):
    gid = add_group(conn, 1)
    client = FakeClient(final("{", stop_reason="max_tokens"))
    res = deep.deep_score(conn, client, profile, gid, now=NOW)
    assert res.status == "invalid"
    assert conn.execute("SELECT count(*) FROM fit_score").fetchone()[0] == 0


def test_fabricated_quote_flags_unverified(conn, profile):
    gid = add_group(conn, 1)
    text = report(quote="Requires ten years of COBOL")
    row = deep.deep_score(conn, FakeClient(final(text)), profile, gid, now=NOW).row
    assert row["evidence_unverified"] == 1
    assert json.loads(row["evidence"])[0]["verified"] is False


def test_fabricated_gap_quote_flags_unverified(conn, profile):
    gid = add_group(conn, 1)
    text = report(gap_quote="Must hold a pilot license")
    row = deep.deep_score(conn, FakeClient(final(text)), profile, gid, now=NOW).row
    assert row["evidence_unverified"] == 1
    assert json.loads(row["deep_report"])["requirement_gaps"][0]["verified"] is False


def test_disagreement_flagged_when_buckets_differ_by_more_than_one(conn, profile):
    gid = add_group(conn, 1)
    add_screen(conn, gid, profile, skills=90)
    text = report(skills=10, domain=10)
    res = deep.deep_score(conn, FakeClient(final(text)), profile, gid, now=NOW)
    info = json.loads(res.row["deep_report"])["screen_disagreement"]
    assert info["levels"] > 1 and info["bucket_flag"] is True
    assert res.disagreement is True
    assert "screen_disagreement" in json.loads(res.row["shape_flags"])
    # The screen row is kept.
    assert conn.execute("SELECT count(*) FROM fit_score WHERE tier='screen'").fetchone()[0] == 1


def test_no_disagreement_when_buckets_match(conn, profile):
    gid = add_group(conn, 1)
    add_screen(conn, gid, profile, skills=90)
    res = deep.deep_score(conn, FakeClient(final(report())), profile, gid, now=NOW)
    info = json.loads(res.row["deep_report"])["screen_disagreement"]
    assert info["levels"] == 0 and res.disagreement is False
    assert "screen_disagreement" not in json.loads(res.row["shape_flags"])


def test_daily_cap_respected(conn, profile):
    gid = add_group(conn, 1)
    client = FakeClient(final(report()))
    res = deep.deep_score(conn, client, profile, gid, now=NOW, remaining_usd=lambda: 0.01)
    assert res.status == "capped" and client.calls == []
    ok = deep.deep_score(conn, client, profile, gid, now=NOW, remaining_usd=lambda: 1.0)
    assert ok.status == "scored"


def test_shortlist_orders_by_screen_overall_and_skips_deep(conn, profile):
    g1, g2, g3 = (add_group(conn, n) for n in (1, 2, 3))
    add_screen(conn, g1, profile, skills=60, domain=60)
    add_screen(conn, g2, profile, skills=95, domain=90)
    add_screen(conn, g3, profile, skills=80, domain=80)
    assert deep.shortlist(conn, profile, 2) == [g2, g3]
    assert deep.shortlist(conn, profile, 10) == [g2, g3, g1]
    deep.deep_score(conn, FakeClient(final(report())), profile, g2, now=NOW)
    assert deep.shortlist(conn, profile, 10) == [g3, g1]


def test_shortlist_skips_groups_deep_scored_by_an_older_model(conn, profile):
    """Moving deep_scorer (or its default) from Opus 5 to Opus 5.5 must not make
    ``score --deep N`` pay again for the groups Opus 5 already deep-scored (bug 15f7012)."""
    g1, g2, g3 = (add_group(conn, n) for n in (1, 2, 3))
    add_screen(conn, g1, profile, skills=60, domain=60)
    add_screen(conn, g2, profile, skills=95, domain=90)
    add_screen(conn, g3, profile, skills=80, domain=80)
    old = FakeClient(final(report(), model="claude-opus-5"))
    deep.deep_score(conn, old, profile, g2, now=NOW, scorer="anthropic:claude-opus-5")
    assert deep.DEFAULT_SCORER == "anthropic:claude-opus-5-5"
    assert deep.shortlist(conn, profile, 2) == [g3, g1]
    # Asked for by name, one group can still be re-run on the new model.
    new = FakeClient(final(report()))
    assert deep.deep_score(conn, new, profile, g2, now=NOW).status == "scored"
    assert len(new.calls) == 1


def test_a_call_is_admitted_only_when_the_cap_covers_its_worst_case(conn, profile):
    """specs/020 M11: the CLI's --deep and --deep-group use the same admission as the console.
    Opus 5.5's worst case is 32,000 tokens at Opus 4.8's rates (its fallback), about $0.83."""
    worst = deep.worst_case_usd("anthropic:claude-opus-5-5")
    assert worst == pytest.approx(0.8265)
    assert deep.worst_case_usd("anthropic:claude-sonnet-5-5") == pytest.approx(0.3306)
    gid = add_group(conn, 1)
    client = FakeClient(final(report()))
    res = deep.deep_score(conn, client, profile, gid, now=NOW, remaining_usd=lambda: worst - 0.01)
    assert res.status == "capped" and client.calls == []
    assert "one deep call can cost up to $0.83" in res.detail
    ok = deep.deep_score(conn, client, profile, gid, now=NOW, remaining_usd=lambda: worst)
    assert ok.status == "scored"
