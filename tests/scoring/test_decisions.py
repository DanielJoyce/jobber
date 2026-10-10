"""Jev decisions scorer (specs/016). httpx.MockTransport only: no network, synthetic data."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest

from jobhunter.config import OpenRouter, Scoring
from jobhunter.console import detail
from jobhunter.core import db
from jobhunter.core.models import Bucket
from jobhunter.pipeline.locations import load_locations
from jobhunter.scoring import buckets, decisions, evaluate, screen
from jobhunter.scoring.decisions import (
    DECISIONS_PROMPT_VERSION,
    DecisionsError,
    DecisionsScorer,
)
from jobhunter.scoring.profile import (
    CurrentFocus,
    Hard,
    Narrative,
    Profile,
    SalaryFloor,
    Soft,
)
from jobhunter.scoring.scorers import ScorerError, privacy_notice, scorer_from_string

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
SPEC = "jev:typesafe/jev-1.13"
SERVED = "typesafe/jev-1.13-20260917"
SALARY_FLOOR = 987654  # a marker: must never reach the model
DESCRIPTION = "Own the Linux fleet for a small team. Write Terraform modules and Go tooling."


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
    weights = {"skills": 0.4, "seniority": 0.2, "domain": 0.2, "comp": 0.1, "location": 0.1}
    return Profile(
        hard=Hard(
            states_allowed=["CO", "WA"],
            salary_floor=SalaryFloor(amount=SALARY_FLOOR, period="year"),
            requires_i_lack=["cdl", "active_security_clearance"],
        ),
        soft=Soft.model_validate({"weights": weights, "state_ranking": ["WA", "CO"]}),
        current_focus=CurrentFocus(
            since="2023-01",
            doing="Linux fleet operations",
            want_more_of=["platform engineering"],
            done_with=["desk-side support", "VMware"],
        ),
        narrative=Narrative(
            want="Small teams.",
            avoid="Roles that are really 24/7 on-call rotations.\n- Pure Windows shops\n",
            dealbreakers_soft="A posting that is mostly project management.",
            context="Senior IC.",
        ),
        resume_text="Synthetic Person\n- 2023-present: ran synthetic hosts.\n",
    )


def add_group(conn, n: int, profile: Profile, *, description: str = DESCRIPTION) -> int:
    job_id = conn.execute(
        "INSERT INTO job (source_key, external_id, url, title, employer, description_text, "
        "salary_raw, location_scope, remote, employment_type, stage, first_seen_at, "
        "last_seen_at, posted_at) VALUES ('wa', ?, 'https://example.com/j', ?, "
        "'Synthetic Agency', ?, '$40.00 - $50.00 hourly', 'single', 'hybrid', 'full_time', "
        "'prefiltered', 'x', 'x', ?)",
        (f"e{n}", f"Systems Engineer {n}", description, f"2026-10-{n:02d}"),
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
    conn.execute(
        "INSERT INTO prefilter_result (job_id, passed, reasons, filter_version, evaluated_at) "
        "VALUES (?, 1, '[]', ?, 'x')",
        (job_id, profile.filter_version),
    )
    return gid


# ─── Fake API ───────────────────────────────────────────────────────────────

LEVEL_DEFAULTS = {
    "skills_raw": 4,
    "skills_recent": 4,
    "job_level": 2,
    "level": 2,  # candidate.level
    "domain": 4,
    "focus_overlap": 3,
}


def _suffix(qid: str) -> str:
    """``g1.done_with.vmware`` -> ``done_with.vmware``; ``candidate.level`` -> ``level``."""
    return qid.split(".", 1)[1]


def answer(qid: str, q: dict, overrides: dict) -> dict:
    """A plausible answer for one question; ``overrides`` keyed by full id or suffix."""
    over = overrides.get(qid, overrides.get(_suffix(qid)))
    if q["type"] == "score":
        n = len(q["criteria"])
        level = over if isinstance(over, int) else LEVEL_DEFAULTS[_suffix(qid)]
        conf = over.get("confidence", 0.9) if isinstance(over, dict) else 0.9
        if isinstance(over, dict):
            level = over["level"]
        return {
            "type": "score",
            "score": level / (n - 1),
            "legend": {str(i): c for i, c in enumerate(q["criteria"])},
            "probabilities": {str(i): 1.0 if i == level else 0.0 for i in range(n)},
            "confidence": conf,
        }
    if q["type"] == "choice":
        opts = list(q["criteria"])
        default = "strong" if "strong" in opts else "match"
        pick = over if isinstance(over, str) else default
        probs = {o: (0.97 if o == pick else 0.03 / (len(opts) - 1)) for o in opts}
        conf = 0.96
        if isinstance(over, dict):
            pick, probs, conf = over["choice"], over["probabilities"], over["confidence"]
        return {"type": "choice", "choice": pick, "probabilities": probs, "confidence": conf}
    default = 0.9 if _suffix(qid).startswith("states.") else 0.1
    return {"type": "noul", "noul": over if isinstance(over, float) else default}


class FakeAPI:
    def __init__(self, overrides: dict | None = None, *, cost: float = 0.0003, drop=()):
        self.overrides = overrides or {}
        self.cost = cost
        self.drop = set(drop)
        self.bodies: list[dict] = []
        self.headers: list[httpx.Headers] = []
        self.status = 200
        self.error_body: str = ""

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.bodies.append(body)
        self.headers.append(request.headers)
        if self.status != 200:
            return httpx.Response(self.status, text=self.error_body)
        answers = {
            qid: answer(qid, q, self.overrides)
            for qid, q in body["questions"].items()
            if qid not in self.drop
        }
        return httpx.Response(
            200,
            json={
                "id": f"dec-{len(self.bodies)}",
                "model": SERVED,
                "provider": "TypeSafe",
                "answers": answers,
                "usage": {"input_tokens": 3000, "output_tokens": 0, "cost": self.cost},
            },
        )


def make(api: FakeAPI, **kw) -> DecisionsScorer:
    client = httpx.Client(transport=httpx.MockTransport(api))
    scorer = decisions.decisions_scorer(
        SPEC, OpenRouter(), client=client, env={"OPENROUTER_API_KEY": "or-key"}
    )
    for k, v in kw.items():
        setattr(scorer, k, v)
    return scorer


def run(conn, scorer, profile, *, limit=50, remaining=None):
    return decisions.score_decisions(
        conn, scorer, profile, limit=limit, now=NOW, remaining_usd=remaining
    )


def fit_rows(conn):
    return conn.execute("SELECT * FROM fit_score ORDER BY job_group_id").fetchall()


def computed(conn, profile, row):
    job = conn.execute(
        "SELECT j.* FROM job_group g JOIN job j ON j.id = g.canonical_job_id WHERE g.id = ?",
        (row["job_group_id"],),
    ).fetchone()
    return buckets.compute_row(row, job, load_locations(conn, job["id"]), profile)


# ─── Factory and notice ─────────────────────────────────────────────────────


def test_factory_and_url():
    s = scorer_from_string(SPEC, scoring=Scoring())
    assert isinstance(s, DecisionsScorer)
    assert (s.name, s.model, s.evidence_mode) == (SPEC, "typesafe/jev-1.13", "none")
    assert s.url == "https://openrouter.ai/api/alpha/decisions"
    g = scorer_from_string("decisions:typesafe/jev-latest", scoring=Scoring())
    assert (g.name, g.model) == ("decisions:typesafe/jev-latest", "typesafe/jev-latest")
    with pytest.raises(ScorerError):
        decisions.decisions_scorer("jev:", OpenRouter())
    with pytest.raises(ScorerError):
        s.score_one(None)  # type: ignore[arg-type]


def test_privacy_notice_names_openrouter_and_typesafe():
    notice = privacy_notice(SPEC, Scoring())
    assert notice and "openrouter.ai" in notice and "TypeSafe" in notice and "resume" in notice


def test_missing_key_is_a_scorer_error(conn, profile):
    add_group(conn, 1, profile)
    scorer = decisions.decisions_scorer(SPEC, OpenRouter(), env={})
    with pytest.raises(ScorerError, match="OPENROUTER_API_KEY"):
        run(conn, scorer, profile)


# ─── Request shape ──────────────────────────────────────────────────────────


def test_request_body_shape(conn, profile):
    for n in (1, 2):
        add_group(conn, n, profile)
    api = FakeAPI()
    run(conn, make(api), profile)
    assert len(api.bodies) == 1
    body = api.bodies[0]
    h = api.headers[0]
    assert h["authorization"] == "Bearer or-key" and h["x-title"] == "jobhunter"
    assert body["model"] == "typesafe/jev-1.13"
    assert body["provider"] == {"data_collection": "deny"}
    state = body["state"]
    assert set(state["candidate"]) == {
        "resume",
        "current_focus",
        "done_with",
        "want",
        "avoid",
        "dealbreakers",
        "context",
    }
    jobs = state["jobs"]
    # newest posting first, as in screen eligibility
    assert [j["id"] for j in jobs] == ["g2", "g1"]
    assert set(jobs[0]) == {
        "id",
        "title",
        "employer",
        "location",
        "employment_type",
        "remote",
        "description",
    }
    # nothing filter-related, and no stated salary, reaches the model (specs/014)
    dumped = json.dumps(body)
    assert str(SALARY_FLOOR) not in dumped and "$40.00" not in dumped
    assert "state_ranking" not in dumped and "weights" not in dumped

    qs = body["questions"]
    assert "candidate.level" in qs
    for i, cid in enumerate(["g2", "g1"]):
        mine = {k: v for k, v in qs.items() if k.startswith(f"{cid}.")}
        assert f"{cid}.skills_raw" in mine and f"{cid}.verdict" in mine
        assert f"{cid}.done_with.desk_side_support" in mine
        assert f"{cid}.done_with.vmware" in mine
        assert f"{cid}.avoid.roles_that_are_really_24_7" in mine
        assert f"{cid}.avoid.pure_windows_shops" in mine
        assert f"{cid}.avoid.a_posting_that_is_mostly_project" in mine
        assert f"{cid}.requires.cdl" in mine
        assert f"{cid}.requires.active_security_clearance" in mine
        for q in mine.values():
            assert f"`jobs[{i}]`" in q["instructions"]
        # pay and location are only asked as "does the posting state ..." facts
        for qid, q in mine.items():
            text = q["instructions"].casefold()
            if "salary" in text or "pay" in text.replace("pay range", ""):
                assert qid == f"{cid}.states.salary" or "not pay" in text
            if "located" in text or "location" in text:
                assert qid in (f"{cid}.states.location", f"{cid}.verdict")
    assert qs["g2.skills_recent"]["instructions"].count("`candidate.current_focus`") == 1
    assert "`candidate.resume`" in qs["g2.skills_raw"]["instructions"]
    assert qs["g2.verdict"]["type"] == "choice" and "other" in qs["g2.verdict"]["criteria"]
    assert set(qs["g2.seniority_direction"]["criteria"]) == {"below", "match", "above", "unclear"}
    assert len(qs["g2.skills_raw"]["criteria"]) == 5


# ─── Mapping ────────────────────────────────────────────────────────────────


def test_strong_answers_map_to_dimensions_and_bucket_a(conn, profile):
    gid = add_group(conn, 1, profile)
    res = run(conn, make(FakeAPI()), profile)
    assert res.written == 1 and res.requests == 1
    (row,) = fit_rows(conn)
    assert row["job_group_id"] == gid
    assert row["prompt_version"] == DECISIONS_PROMPT_VERSION and row["model"] == SPEC
    assert row["served_model"] == SERVED
    assert row["evidence_mode"] == "none" and row["evidence_unverified"] == 0
    assert json.loads(row["evidence"]) == []
    assert row["verdict"] == "strong"
    dims = json.loads(row["dimensions"])
    assert dims["skills"]["score"] == 100 and dims["raw_skills"] == 100
    assert dims["recency_weighted_skills"] == 100
    assert dims["seniority"]["score"] == 100 and dims["seniority_direction"] == "match"
    assert dims["domain"]["score"] == 100 and dims["current_focus_overlap"] == 100
    assert dims["stale_skills"] == [] and dims["done_with_hits"] == []
    assert dims["skills"]["why"].startswith("Jev: Does the work described daily")
    assert json.loads(row["shape_flags"]) == []
    assert json.loads(row["blockers"]) == [] and json.loads(row["missing_info"]) == []
    report = json.loads(row["decisions"])
    assert report["verdict"]["choice"] == "strong" and report["verdict"]["confidence"] == 0.96
    assert report["answers"]["verdict"]["probabilities"]["strong"] == 0.97
    assert report["request_id"] == "dec-1" and report["jobs_in_request"] == 1
    fit = computed(conn, profile, row)
    assert fit.bucket is Bucket.A
    stage = conn.execute("SELECT stage FROM job WHERE job_group_id = ?", (gid,)).fetchone()[0]
    assert stage == "scored"


def test_stale_match_lands_in_bucket_f(conn, profile):
    add_group(conn, 1, profile)
    api = FakeAPI({"skills_raw": 4, "skills_recent": 2})  # raw 100, recent 50
    run(conn, make(api), profile)
    (row,) = fit_rows(conn)
    dims = json.loads(row["dimensions"])
    assert (dims["raw_skills"], dims["recency_weighted_skills"]) == (100, 50)
    assert "stale_match" in json.loads(row["shape_flags"])
    assert computed(conn, profile, row).bucket is Bucket.F


def test_seniority_gap_and_direction(conn, profile):
    add_group(conn, 1, profile)
    api = FakeAPI({"job_level": 3, "level": 2, "seniority_direction": "unclear"})
    run(conn, make(api), profile)
    dims = json.loads(fit_rows(conn)[0]["dimensions"])
    # one level above: 100 - 25, direction derived from the level gap when "unclear"
    assert dims["seniority"]["score"] == 75
    assert dims["seniority_direction"] == "above"
    assert dims["seniority"]["why"].startswith("above: Jev: Lead or staff")
    assert decisions.seniority_score(0.0, 1.0) == 0
    assert decisions.seniority_score(0.5, 0.25) == 75


def test_fan_out_nouls_to_hits_flags_blockers_missing_info(conn, profile):
    add_group(conn, 1, profile)
    api = FakeAPI(
        {
            "done_with.vmware": 0.7,
            "done_with.desk_side_support": 0.45,  # below 0.5: not a hit
            "avoid.pure_windows_shops": 0.65,
            "avoid.roles_that_are_really_24_7": 0.55,  # below 0.6: no flag
            "requires.cdl": 0.8,
            "states.salary": 0.2,
            "states.remote_policy": 0.5,  # between: not reported missing
        }
    )
    run(conn, make(api), profile)
    (row,) = fit_rows(conn)
    dims = json.loads(row["dimensions"])
    assert dims["done_with_hits"] == ["VMware"]
    assert json.loads(row["shape_flags"]) == ["pure_windows_shops", "done_with"]
    assert json.loads(row["blockers"]) == ["cdl"]
    assert json.loads(row["missing_info"]) == ["salary or pay range not stated"]
    fit = computed(conn, profile, row)
    assert fit.blockers == ["cdl"] and fit.bucket is Bucket.B  # blocker demotes A -> B


def test_other_verdict_falls_back_to_best_stored_verdict(conn, profile):
    add_group(conn, 1, profile)
    probs = {"strong": 0.1, "possible": 0.3, "weak": 0.05, "mismatch": 0.05, "other": 0.5}
    api = FakeAPI({"verdict": {"choice": "other", "probabilities": probs, "confidence": 0.2}})
    run(conn, make(api), profile)
    (row,) = fit_rows(conn)
    assert row["verdict"] == "possible"
    assert "low_confidence" in json.loads(row["shape_flags"])


def test_low_confidence_from_dimension_scores(conn, profile):
    add_group(conn, 1, profile)
    low = {"confidence": 0.2}
    api = FakeAPI(
        {
            "skills_raw": {"level": 4, **low},
            "skills_recent": {"level": 4, **low},
            "domain": {"level": 4, **low},
        }
    )
    run(conn, make(api), profile)
    assert "low_confidence" in json.loads(fit_rows(conn)[0]["shape_flags"])


def test_split_items_skips_comments_and_lead_ins():
    text = "# what shapes of job to avoid\nI avoid:\n- On-call rotations\n\n* Vendor consoles\n"
    assert decisions.split_items(text) == ["On-call rotations", "Vendor consoles"]
    assert decisions.slug("24/7 On-call rotations, mostly!") == "24_7_on_call_rotations_mostly"


def test_position_uses_probabilities_then_score():
    assert decisions.position({"probabilities": {"0": 0.81, "1": 0.19}, "score": 0.19}, 2) == (
        pytest.approx(0.19)
    )
    assert decisions.position({"probabilities": {"1": 0.57, "2": 0.43}}, 3) == pytest.approx(0.715)
    assert decisions.position({"score": 1.43}, 3) == pytest.approx(0.715)  # docs form
    assert decisions.position({"score": 0.5}, 5) == 0.5  # live form
    with pytest.raises(KeyError):
        decisions.position({}, 3)
    assert decisions.confidence({"type": "noul", "noul": 0.94}) == pytest.approx(0.88)


# ─── Batching, cost, spend ──────────────────────────────────────────────────


def test_batching_splits_by_jobs_per_request(conn, profile):
    for n in range(1, 6):
        add_group(conn, n, profile)
    api = FakeAPI()
    res = run(conn, make(api, jobs_per_request=2), profile)
    assert [len(b["state"]["jobs"]) for b in api.bodies] == [2, 2, 1]
    assert res.requests == 3 and res.written == 5
    # each request's own job indexes start at 0
    assert "`jobs[1]`" in api.bodies[0]["questions"]["g4.skills_raw"]["instructions"]
    assert "`jobs[0]`" in api.bodies[1]["questions"]["g3.skills_raw"]["instructions"]


def test_jobs_per_request_is_capped_at_25(conn, profile):
    for n in range(1, 28):
        add_group(conn, n, profile, description="short")
    api = FakeAPI()
    run(conn, make(api, jobs_per_request=100), profile)
    assert [len(b["state"]["jobs"]) for b in api.bodies] == [25, 2]


def test_batching_splits_by_token_cap(conn, profile):
    for n in range(1, 4):
        add_group(conn, n, profile, description="word " * 2000)  # about 2,500 tokens each
    api = FakeAPI()
    run(conn, make(api, jobs_per_request=8, max_input_tokens=6000), profile)
    assert [len(b["state"]["jobs"]) for b in api.bodies] == [1, 1, 1]
    api2 = FakeAPI()
    conn.execute("DELETE FROM fit_score")
    run(conn, make(api2, jobs_per_request=8, max_input_tokens=60_000), profile)
    assert [len(b["state"]["jobs"]) for b in api2.bodies] == [3]


def test_cost_split_evenly_and_llm_spend(conn, profile):
    for n in range(1, 4):
        add_group(conn, n, profile)
    res = run(conn, make(FakeAPI(cost=0.0003)), profile)
    rows = fit_rows(conn)
    assert [r["cost_usd"] for r in rows] == [pytest.approx(0.0001)] * 3
    assert [r["input_tokens"] for r in rows] == [1000] * 3
    assert res.cost_usd == pytest.approx(0.0003)
    assert res.served_models == {SERVED: 3}
    spend = conn.execute("SELECT * FROM llm_spend").fetchone()
    assert (spend["model"], spend["tier"], spend["calls"]) == (SPEC, "screen", 1)
    assert spend["cost_usd"] == pytest.approx(0.0003) and spend["input_tokens"] == 3000


def test_cost_falls_back_to_observed_rate():
    s = DecisionsScorer("typesafe/jev-1.13", OpenRouter())
    usage = decisions.Usage(input_tokens=1_000_000)
    assert s.cost(usage) == pytest.approx(0.042)


def test_spend_cap_checked_before_each_request(conn, profile):
    for n in range(1, 5):
        add_group(conn, n, profile)
    api = FakeAPI()
    budget = iter([1.0, 0.0])
    res = run(conn, make(api, jobs_per_request=2), profile, remaining=lambda: next(budget))
    assert res.requests == 1 and res.capped and res.written == 2
    assert len(api.bodies) == 1


def test_score_sync_dispatches_to_decisions(conn, profile):
    add_group(conn, 1, profile)
    res = screen.score_sync(conn, make(FakeAPI()), profile, limit=10, now=NOW)
    assert isinstance(res, decisions.DecisionsResult) and res.written == 1
    # scored once: not eligible again under the same key
    assert decisions.eligible(conn, profile, SPEC, 10) == []


# ─── Failures ───────────────────────────────────────────────────────────────

ZOD = json.dumps(
    {
        "error": {
            "code": 400,
            "message": json.dumps(
                [
                    {
                        "code": "invalid_type",
                        "expected": "array",
                        "received": "object",
                        "path": ["questions", "g1.skills_raw", "criteria"],
                        "message": "Expected array, received object",
                    }
                ]
            ),
        }
    }
)


def test_zod_400_is_surfaced_and_jobs_stay_eligible(conn, profile):
    add_group(conn, 1, profile)
    api = FakeAPI()
    api.status, api.error_body = 400, ZOD
    res = run(conn, make(api), profile)
    assert res.errored == 1 and res.written == 0
    assert res.errors == [
        "HTTP 400 invalid request: questions.g1.skills_raw.criteria: "
        "Expected array, received object"
    ]
    assert fit_rows(conn) == []
    assert conn.execute("SELECT count(*) FROM llm_spend").fetchone()[0] == 0
    assert len(decisions.eligible(conn, profile, SPEC, 10)) == 1


def test_auth_errors_stop_the_run(conn, profile):
    add_group(conn, 1, profile)
    api = FakeAPI()
    api.status, api.error_body = 403, '{"error": {"message": "guardrail: model not allowed"}}'
    with pytest.raises(DecisionsError, match="HTTP 403: guardrail"):
        run(conn, make(api), profile)


def test_partial_answers_leave_those_jobs_eligible(conn, profile):
    g1 = add_group(conn, 1, profile)
    g2 = add_group(conn, 2, profile)
    api = FakeAPI(drop={f"g{g2}.verdict", f"g{g2}.done_with.vmware"})
    res = run(conn, make(api), profile)
    assert res.written == 1 and res.invalid == 1
    assert [r["job_group_id"] for r in fit_rows(conn)] == [g1]
    assert [r["group_id"] for r in decisions.eligible(conn, profile, SPEC, 10)] == [g2]
    # the whole request's cost is still recorded
    assert conn.execute("SELECT calls FROM llm_spend").fetchone()[0] == 1


def test_missing_candidate_level_skips_every_job(conn, profile):
    add_group(conn, 1, profile)
    res = run(conn, make(FakeAPI(drop={"candidate.level"})), profile)
    assert res.written == 0 and res.invalid == 1


def test_format_api_error_plain_text():
    assert decisions.format_api_error(502, "bad gateway") == "HTTP 502: bad gateway"


# ─── Eval, detail page, bench ───────────────────────────────────────────────


def test_eval_excludes_decisions_rows_from_unverified_rate(conn, profile):
    gids = [add_group(conn, n, profile) for n in (1, 2)]
    run(conn, make(FakeAPI()), profile)
    for gid in gids:
        conn.execute(
            "INSERT INTO label (job_group_id, label, labeled_at) VALUES (?, 'interesting', 'x')",
            (gid,),
        )
    r = evaluate.evaluate(conn, profile, prompt_version=DECISIONS_PROMPT_VERSION)
    assert r.n == 2
    assert r.metrics["evidence_unverified_rate"] is None
    assert r.targets["evidence_unverified_rate"]["excluded"] == 2
    assert r.targets["evidence_unverified_rate"]["pass"] is True
    assert any("left out of the evidence_unverified rate" in note for note in r.notes)
    assert r.served_models == {SERVED: 2}


def test_eval_mixed_rows_count_only_quote_rows(conn, profile):
    g1 = add_group(conn, 1, profile)
    run(conn, make(FakeAPI()), profile)
    g2 = add_group(conn, 2, profile)
    conn.execute(
        "INSERT INTO fit_score (job_group_id, tier, model, prompt_version, scoring_version, "
        "verdict, overall, dimensions, evidence, evidence_unverified, created_at) "
        "VALUES (?, 'screen', 'm', ?, 's', 'strong', 0, '{}', '[]', 1, 'z')",
        (g2, DECISIONS_PROMPT_VERSION),
    )
    for gid in (g1, g2):
        conn.execute(
            "INSERT INTO label (job_group_id, label, labeled_at) VALUES (?, 'interesting', 'x')",
            (gid,),
        )
    r = evaluate.evaluate(conn, profile, prompt_version=DECISIONS_PROMPT_VERSION)
    assert r.metrics["evidence_unverified_rate"] == 1.0  # 1 of the 1 quote-bearing rows


def test_detail_shows_probabilities_instead_of_quotes(conn, profile):
    gid = add_group(conn, 1, profile)
    run(conn, make(FakeAPI({"avoid.pure_windows_shops": 0.65})), profile)
    d = detail.load_detail(conn, profile, gid, NOW)
    assert d is not None and d.evidence_mode == "none"
    assert d.decisions_verdict == "fit strong (0.97, confidence 0.96)"
    assert d.served_model == SERVED
    assert ("Pure Windows shops", "0.65") in d.decisions_answers
    assert not d.evidence_unverified and d.text.unverified == []


def test_bench_writes_nothing(conn, profile):
    for n in (1, 2, 3):
        add_group(conn, n, profile)
    clock = iter([0.0, 2.0, 10.0, 11.0])
    api = FakeAPI()
    report = decisions.run_bench(
        conn, make(api, jobs_per_request=2), profile, n=3, clock=lambda: next(clock)
    )
    assert len(api.bodies) == 2
    assert report.attempted == 3 and report.schema_valid == 3
    assert report.seconds == [1.0, 1.0, 1.0]
    assert report.cost_usd == pytest.approx(0.0006)
    assert fit_rows(conn) == []
    assert conn.execute("SELECT count(*) FROM llm_spend").fetchone()[0] == 0


# ─── Employer rejections (specs/006 "Employer rejections") ──────────────────


def set_job(conn, gid: int, employer: str, title: str) -> None:
    conn.execute(
        "UPDATE job SET employer = ?, title = ? WHERE job_group_id = ?", (employer, title, gid)
    )


def reject(conn, employer, title, *, gid=None, day="2026-09-01"):
    from jobhunter.core import rejections

    rejections.record(
        conn,
        received_at=f"{day}T12:00:00+00:00",
        employer=employer,
        title=title,
        source="email",
        now=NOW,
        job_group_id=gid,
    )


def test_rejected_posting_is_never_sent_to_jev(conn, profile):
    g1, g2, g3 = (add_group(conn, n, profile) for n in (1, 2, 3))
    set_job(conn, g2, "Northwind Analytics, Inc.", "Senior Data Engineer")
    reject(conn, "Synthetic Agency", "Systems Engineer 1", gid=g1)  # matched by mail
    reject(conn, "Northwind Analytics", "Senior Data Engineer")  # unmatched: by employer+title
    api = FakeAPI()
    res = run(conn, make(api), profile)
    assert len(api.bodies) == 1
    (body,) = api.bodies
    assert [j["id"] for j in body["state"]["jobs"]] == [f"g{g3}"]
    assert not [q for q in body["questions"] if q.startswith((f"g{g1}.", f"g{g2}."))]
    assert "Northwind" not in json.dumps(body)
    assert (res.submitted, res.written) == (1, 1)
    # A second run spends nothing: the rejected postings are still not eligible.
    run(conn, make(api), profile)
    assert len(api.bodies) == 1
    assert decisions.eligible(conn, profile, SPEC, 10) == []


def test_all_rejected_means_no_request(conn, profile):
    gid = add_group(conn, 1, profile)
    reject(conn, "Synthetic Agency", "Systems Engineer 1", gid=gid)
    api = FakeAPI()
    res = run(conn, make(api), profile)
    assert api.bodies == [] and res.requests == 0


def test_same_employer_other_role_is_scored_with_the_fact(conn, profile):
    g1, g2 = add_group(conn, 1, profile), add_group(conn, 2, profile)
    set_job(conn, g1, "Northwind Analytics", "Platform Engineer")
    reject(conn, "Northwind Analytics Inc", "Senior Data Engineer", day="2026-09-01")
    api = FakeAPI()
    res = run(conn, make(api), profile)
    assert res.written == 2
    jobs = {j["id"]: j for j in api.bodies[0]["state"]["jobs"]}
    assert jobs[f"g{g1}"]["employer_history"] == (
        "candidate was rejected by this employer for Senior Data Engineer on 2026-09-01"
    )
    assert "employer_history" not in jobs[f"g{g2}"]
    # No question asks about it; pay and location never reach the model.
    assert not [q for q in api.bodies[0]["questions"] if "reject" in q]
    assert str(SALARY_FLOOR) not in json.dumps(api.bodies[0])


def test_old_employer_rejection_is_outside_the_window(conn, profile):
    gid = add_group(conn, 1, profile)
    set_job(conn, gid, "Northwind Analytics", "Platform Engineer")
    reject(conn, "Northwind Analytics", "Senior Data Engineer", day="2026-03-01")
    api = FakeAPI()
    decisions.score_decisions(conn, make(api), profile, limit=10, now=NOW, rejection_days=90)
    assert "employer_history" not in api.bodies[0]["state"]["jobs"][0]


def test_new_rejection_context_does_not_force_a_paid_rescore(conn, profile):
    gid = add_group(conn, 1, profile)
    set_job(conn, gid, "Northwind Analytics", "Platform Engineer")
    api = FakeAPI()
    run(conn, make(api), profile)
    assert len(api.bodies) == 1
    reject(conn, "Northwind Analytics", "Senior Data Engineer")  # a different role
    run(conn, make(api), profile)
    assert len(api.bodies) == 1  # no version key changed, so nothing is re-sent
