"""Stage 3 deep pass: Opus 5.5 on one job group (specs/006 "Stage 3 — deep pass").

Not batched. One streamed request per group, with the same cached system prefix as Stage 2
(rubric + profile scoring inputs) followed by a deep-pass instruction block. The result is
stored as a ``fit_score`` row with ``tier='deep'`` whose ``dimensions`` match Stage 2's shape,
so ``buckets.compute_row`` reads it unchanged; the full report goes in ``deep_report``. The
Stage 2 row is never touched: a disagreement is flagged, not overwritten.
"""

from __future__ import annotations

import copy
import json
import logging
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Literal

from anthropic import transform_schema
from pydantic import BaseModel, Field, ValidationError

from jobhunter.core import rejections
from jobhunter.core.models import Bucket, Evidence, Screen
from jobhunter.pipeline.listing import _txn, to_iso
from jobhunter.pipeline.locations import load_job_group_locations
from jobhunter.scoring import screen as stage2
from jobhunter.scoring.buckets import compute_row
from jobhunter.scoring.profile import Profile, scoring_inputs
from jobhunter.scoring.rubric import DIMENSIONS, PROMPT_VERSION, RUBRIC_TEXT
from jobhunter.scoring.scorers import (
    PRICING_PER_MTOK,
    ceiling_cost,
    compute_cost,
    compute_message_cost,
)

logger = logging.getLogger(__name__)

TIER = "deep"
DEFAULT_SCORER = "anthropic:claude-opus-5-5"
# Room for thinking plus the report. Opus 5.5 thinks more per turn than Opus 5 at the same
# effort, and max_tokens caps thinking and text together; a cut-off report is paid for and
# writes nothing. The call streams, so a high cap costs nothing unless it is used.
MAX_TOKENS = 32000
EFFORT = "high"
BETAS = ["server-side-fallback-2026-07-01"]
# specs/006 "Cost": about $0.051 per deep job on Opus 5.5 ($0.064 on Opus 5). Kept for
# reference; the spend-cap admission uses ``worst_case_usd`` (specs/020 M11).
ESTIMATED_COST_PER_REQUEST_USD = 0.08
# Server-side fallback targets per deep model (``BETAS``, ``fallbacks: "default"``): a refused
# request may be answered, and billed, by one of these at its own rates. Per the platform docs
# ("Migrating to Claude Opus 5.5", "... Sonnet 5.5", "... Opus 5", checked 2026-10-11): Opus 5.5
# falls back to Claude Opus 5 or Opus 4.8, Opus 5 (cyber) to Opus 4.8, Sonnet 5.5 to Sonnet 5.
FALLBACK_TARGETS: dict[str, tuple[str, ...]] = {
    "claude-opus-5-5": ("claude-opus-4-8", "claude-opus-5"),
    "claude-opus-5": ("claude-opus-4-8",),
    "claude-sonnet-5-5": ("claude-sonnet-5",),
}
# Typical input for one deep call (specs/006 "Cost", Stage 3): 5,000 in + 3,000 cached.
_WORST_INPUT = {"input_tokens": 5000, "cache_creation_input_tokens": 0}
_WORST_CACHED = 3000
DEEP_PROMPT_VERSION = f"{PROMPT_VERSION}+deep1"
DISAGREEMENT_LEVELS = 1  # flagged when buckets differ by MORE than this many levels
_BUCKET_ORDER = [b.value for b in Bucket]  # A..G, best to worst

DEEP_INSTRUCTIONS = """\
# Deep pass

You are now doing the deep pass on one posting that already received a quick screen. Apply the
same rubric, anchors and dimension definitions as above, but reason carefully and completely.
Return the screen fields (verdict, dimensions, raw_skills, recency_weighted_skills,
stale_skills, current_focus_overlap, done_with_hits, evidence, blockers, missing_info,
shape_flags, tailoring_hints) plus these report fields:

- recommendation: apply, consider or skip, reasoned from the whole picture, not one number.
- summary: three or four sentences a busy reader can act on.
- requirement_gaps: one entry per stated requirement, compared against the resume. status is
  met, partial or missing. evidence is copied verbatim from the posting (the sentence that
  states the requirement); never paraphrase it.
- emphasize: the two or three things to lead with in an application.
- questions_to_ask: questions worth asking the employer, driven by missing_info and risk.
- salary_read: one or two sentences reading the posting's stated pay, or say that no pay is
  stated. Do not invent figures.
- screen_disagreement: set flag true, with a reason, only if a quick screen of this posting
  would likely be materially wrong.

Every evidence quote must be copied verbatim from the posting.
"""


class RequirementGap(BaseModel):
    requirement: str
    status: Literal["met", "partial", "missing"]
    evidence: str  # verbatim quote from the posting, checked after parsing


class ScreenDisagreement(BaseModel):
    flag: bool
    reason: str


class DeepReport(Screen):
    """The Screen fields (so buckets read it) plus the Stage 3 report."""

    evidence: list[Evidence] = Field(min_length=1, max_length=10)
    recommendation: Literal["apply", "consider", "skip"]
    summary: str
    requirement_gaps: list[RequirementGap]
    emphasize: list[str] = Field(min_length=1, max_length=3)
    questions_to_ask: list[str]
    salary_read: str
    screen_disagreement: ScreenDisagreement


def deep_json_schema() -> dict[str, Any]:
    """Structured-output schema for ``DeepReport`` (same dimensions rewrite as the screen)."""
    schema = copy.deepcopy(DeepReport.model_json_schema())
    schema["properties"]["dimensions"] = {
        "type": "object",
        "properties": {name: {"$ref": "#/$defs/Dimension"} for name in DIMENSIONS},
        "required": list(DIMENSIONS),
        "additionalProperties": False,
    }
    return transform_schema(schema)


DEEP_SCHEMA: dict[str, Any] = deep_json_schema()


class DeepError(Exception):
    """A deep result could not be turned into a DeepReport."""


class DeepWriteError(Exception):
    """A billed deep call whose report could not be stored. Its spend is already recorded."""

    def __init__(self, group_id: int, cost_usd: float) -> None:
        super().__init__(f"deep report for group {group_id} not stored; the call was recorded")
        self.group_id = group_id
        self.cost_usd = cost_usd


@dataclass
class DeepResult:
    """Outcome of one ``deep_score`` call.

    status: scored | existing | refused | invalid | capped | rejected (the employer already
    rejected this posting; nothing sent). Only scored and existing carry a
    ``row``; every other status leaves the group eligible for another try.
    """

    status: str
    row: sqlite3.Row | None = None
    disagreement: bool = False
    cost_usd: float = 0.0
    detail: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


# ─── Request ────────────────────────────────────────────────────────────────


def build_request(
    job: Mapping[str, Any],
    locations_summary: str,
    profile: Profile,
    *,
    scorer: str = DEFAULT_SCORER,
    max_tokens: int = MAX_TOKENS,
) -> dict[str, Any]:
    """Params for ``client.beta.messages.stream``.

    Adaptive thinking and ``effort`` are the Opus 5.5 controls (its thinking cannot be turned
    off, and its default effort is ``medium``, so ``high`` is sent explicitly); ``budget_tokens``
    and ``temperature`` are rejected by the model and never sent. The cache breakpoint sits on the
    profile block, so the rubric + profile prefix is identical to Stage 2's; the deep-pass
    instructions follow it.
    """
    return {
        "model": stage2.model_id(scorer),
        "max_tokens": max_tokens,
        "thinking": {"type": "adaptive"},
        "system": [
            {"type": "text", "text": RUBRIC_TEXT},
            {
                "type": "text",
                "text": scoring_inputs(profile),
                "cache_control": {"type": "ephemeral"},
            },
            {"type": "text", "text": DEEP_INSTRUCTIONS},
        ],
        "messages": [{"role": "user", "content": stage2.posting_text(job, locations_summary)}],
        "output_config": {
            "effort": EFFORT,
            "format": {"type": "json_schema", "schema": DEEP_SCHEMA},
        },
        "betas": list(BETAS),
        "fallbacks": "default",
    }


def _stream_message(client: Any, params: dict[str, Any]) -> Any:
    with client.beta.messages.stream(**params) as stream:
        return stream.get_final_message()


def parse_message(message: Any) -> DeepReport:
    stop = getattr(message, "stop_reason", None)
    if stop in ("max_tokens", "refusal"):
        raise DeepError(f"stop_reason {stop}")
    try:
        text = stage2._message_text(message)
        return DeepReport.model_validate_json(text)
    except (ValidationError, stage2.ScreenError) as exc:
        raise DeepError(f"output does not match DeepReport: {exc}") from exc


# ─── Selection helpers ──────────────────────────────────────────────────────


def _bucket_gap(a: str, b: str) -> int:
    return abs(_BUCKET_ORDER.index(a) - _BUCKET_ORDER.index(b))


def _latest_screen(conn: sqlite3.Connection, group_id: int, profile: Profile) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM fit_score WHERE job_group_id = ? AND tier = 'screen' "
        "ORDER BY (scoring_version = ?) DESC, id DESC LIMIT 1",
        (group_id, profile.scoring_version),
    ).fetchone()


def _stored(conn: sqlite3.Connection, group_id: int, scorer: str, profile: Profile):
    return conn.execute(
        "SELECT * FROM fit_score WHERE job_group_id = ? AND tier = ? AND model = ? "
        "AND prompt_version = ? AND scoring_version = ?",
        (group_id, TIER, scorer, DEEP_PROMPT_VERSION, profile.scoring_version),
    ).fetchone()


def shortlist(
    conn: sqlite3.Connection, profile: Profile, n: int, *, scorer: str = DEFAULT_SCORER
) -> list[int]:
    """Top ``n`` group ids by computed screen overall that have no deep score yet.

    "Yet" is per prompt and ``scoring_version`` but under **any** model: changing
    ``scoring.deep_scorer`` (or its default, as when it moved from Opus 5 to Opus 5.5) must
    not make ``score --deep N`` pay again for groups already deep-scored by the previous model;
    it moves on to the next unscored ones. Re-running one group on the new model stays
    possible on request (``score --deep-group``). A group whose last paid attempt with this
    scorer wrote nothing (refused, cut off, malformed; ``score_failure``) for its current
    description revision is skipped too, so it is not paid for again on every run.
    """
    rows = conn.execute(
        "SELECT g.id AS gid, f.* FROM job_group g JOIN fit_score f ON f.job_group_id = g.id "
        "WHERE f.tier = 'screen' AND f.id = ("
        "  SELECT s.id FROM fit_score s WHERE s.job_group_id = g.id AND s.tier = 'screen' "
        "  ORDER BY (s.scoring_version = ?) DESC, s.id DESC LIMIT 1) "
        "AND NOT EXISTS (SELECT 1 FROM fit_score d WHERE d.job_group_id = g.id "
        "  AND d.tier = 'deep' AND d.prompt_version = ? AND d.scoring_version = ?) "
        "AND NOT EXISTS (SELECT 1 FROM score_failure x WHERE x.job_group_id = g.id "
        "  AND x.tier = 'deep' AND x.model = ? AND x.prompt_version = ? "
        "  AND x.scoring_version = ? AND x.input_rev = g.description_rev)",
        (
            profile.scoring_version,
            DEEP_PROMPT_VERSION,
            profile.scoring_version,
            scorer,
            DEEP_PROMPT_VERSION,
            profile.scoring_version,
        ),
    ).fetchall()
    return _top(conn, profile, rows, n)


def _top(conn: sqlite3.Connection, profile: Profile, rows: list[sqlite3.Row], n: int) -> list[int]:
    """The top ``n`` of ``rows`` (latest screen rows with ``gid``) by computed overall, best
    first, ties by group id; rejected groups and groups with no canonical job are left out."""
    scored: list[tuple[int, int]] = []
    rejected = rejections.rejected_group_ids(conn)
    for row in rows:
        if row["gid"] in rejected:
            continue
        job = stage2._canonical_job(conn, row["gid"])
        if job is None:
            continue
        fit = compute_row(row, job, load_job_group_locations(conn, job["id"]), profile)
        scored.append((fit.overall, row["gid"]))
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [gid for _, gid in scored[: max(n, 0)]]


def rerun_list(
    conn: sqlite3.Connection, profile: Profile, n: int, *, scorer: str = DEFAULT_SCORER
) -> list[int]:
    """Top ``n`` groups, ranked as ``shortlist`` ranks, whose deep report (current prompt and
    ``scoring_version``) came from another model and that have none from ``scorer`` yet.

    The console's "Re-run my top groups with <model>" (specs/020 M11). A paid attempt with
    ``scorer`` that wrote nothing for the current description revision is skipped, as in
    ``shortlist``.
    """
    rows = conn.execute(
        "SELECT g.id AS gid, f.* FROM job_group g JOIN fit_score f ON f.job_group_id = g.id "
        "WHERE f.tier = 'screen' AND f.id = ("
        "  SELECT s.id FROM fit_score s WHERE s.job_group_id = g.id AND s.tier = 'screen' "
        "  ORDER BY (s.scoring_version = ?) DESC, s.id DESC LIMIT 1) "
        "AND EXISTS (SELECT 1 FROM fit_score d WHERE d.job_group_id = g.id "
        "  AND d.tier = 'deep' AND d.prompt_version = ? AND d.scoring_version = ? "
        "  AND d.model != ?) "
        "AND NOT EXISTS (SELECT 1 FROM fit_score d WHERE d.job_group_id = g.id "
        "  AND d.tier = 'deep' AND d.prompt_version = ? AND d.scoring_version = ? "
        "  AND d.model = ?) "
        "AND NOT EXISTS (SELECT 1 FROM score_failure x WHERE x.job_group_id = g.id "
        "  AND x.tier = 'deep' AND x.model = ? AND x.prompt_version = ? "
        "  AND x.scoring_version = ? AND x.input_rev = g.description_rev)",
        (
            profile.scoring_version,
            DEEP_PROMPT_VERSION,
            profile.scoring_version,
            scorer,
            DEEP_PROMPT_VERSION,
            profile.scoring_version,
            scorer,
            scorer,
            DEEP_PROMPT_VERSION,
            profile.scoring_version,
        ),
    ).fetchall()
    return _top(conn, profile, rows, n)


def stored_report(
    conn: sqlite3.Connection, group_id: int, scorer: str, profile: Profile
) -> sqlite3.Row | None:
    """This group's deep report from ``scorer`` under the current prompt and profile."""
    return _stored(conn, group_id, scorer, profile)


# ─── Scoring ────────────────────────────────────────────────────────────────


def worst_case_usd(scorer: str = DEFAULT_SCORER) -> float:
    """What one deep call can cost at most (specs/020 "Worst case per call").

    ``MAX_TOKENS`` (thinking and output together) plus the typical input, priced at the
    dearest of the model and its server-side fallback targets: about $0.83 for Opus 5.5,
    answered by ``claude-opus-4-8``. A model missing from the price table is priced at the
    table's dearest rates. A call is admitted only when the remaining cap still covers this.
    """
    usage = SimpleNamespace(
        **_WORST_INPUT, cache_read_input_tokens=_WORST_CACHED, output_tokens=MAX_TOKENS
    )
    name = stage2.model_id(scorer)
    if name not in PRICING_PER_MTOK:
        return ceiling_cost(usage, batch=False)
    models = [name, *(m for m in FALLBACK_TARGETS.get(name, ()) if m in PRICING_PER_MTOK)]
    return max(compute_cost(usage, f"anthropic:{m}", batch=False) for m in models)


def capped_detail(worst: float) -> str:
    """Why a deep call was not started: the cap left is below one call's worst case."""
    return (
        f"spend cap reached: less than ${worst:.2f} of the scoring cap is left, and one deep "
        f"call can cost up to ${worst:.2f} (32,000 tokens at the dearest fallback model's "
        "rates), so it was not started. Nothing was spent on it"
    )


def _cost_model(message: Any, scorer: str) -> str:
    """Price by the model that actually answered (a server-side fallback may differ)."""
    answered = getattr(message, "model", None)
    if isinstance(answered, str) and answered in PRICING_PER_MTOK:
        return answered
    return scorer


def deep_score(
    conn: sqlite3.Connection,
    client: Any,
    profile: Profile,
    group_id: int,
    *,
    now: datetime,
    scorer: str = DEFAULT_SCORER,
    remaining_usd: Callable[[], float] | None = None,
    rejection_days: int = rejections.DEFAULT_WINDOW_DAYS,
) -> DeepResult:
    """Run the deep pass for one group and store it as ``fit_score`` tier 'deep'.

    Re-running returns the stored row without calling the API. A refusal, truncated or
    malformed output, or an exhausted daily cap writes no row. Every billed response records
    its spend first, committed on its own, so a refusal, bad output or a report write that
    fails (``DeepWriteError``, carrying the call's cost) still counts. A call cut off by the
    process dying mid-stream is not recorded: its usage never arrived. A paid attempt that
    wrote nothing is also noted in ``score_failure`` so ``shortlist`` skips the group; asking
    for it by id still runs it.
    """
    existing = _stored(conn, group_id, scorer, profile)
    if existing is not None:
        return DeepResult("existing", existing)
    job = stage2._canonical_job(conn, group_id)
    if job is None:
        raise ValueError(f"job group {group_id} has no canonical job")
    if group_id in rejections.rejected_group_ids(conn):
        return DeepResult("rejected", detail="the employer already rejected this posting")
    # Worst-case admission (specs/020 M11): a call starts only when the cap left still covers
    # the dearest it can be, so a long report never carries a run past the cap.
    worst = worst_case_usd(scorer)
    if remaining_usd is not None and remaining_usd() < worst:
        logger.warning("spend cap reached: deep pass skipped for group %d", group_id)
        return DeepResult("capped", detail=capped_detail(worst))

    (job_d,) = stage2._context_rows(
        conn, [{**dict(job), "group_id": group_id}], now, rejection_days
    )
    params = build_request(job_d, stage2._summary_for(conn, job), profile, scorer=scorer)
    message = _stream_message(client, params)
    usage = message.usage
    # Every attempt a server-side fallback made, each at its own model's rates.
    cost = compute_message_cost(usage, _cost_model(message, scorer), batch=False)
    # The call is billed now, whatever happens next: record it in its own committed
    # transaction before parsing or writing anything, so a refusal, bad output or a failing
    # report write can never lose it (bug 6c33b40). Exactly once per billed response.
    record_call_spend(conn, scorer, now, usage, cost)

    try:
        report = parse_message(message)
    except DeepError as exc:
        stop = getattr(message, "stop_reason", None)
        status = "refused" if stop == "refusal" else "invalid"
        logger.warning("deep pass group %d %s: %s", group_id, status, exc)
        rev = conn.execute(
            "SELECT description_rev FROM job_group WHERE id = ?", (group_id,)
        ).fetchone()
        with _txn(conn):
            stage2.record_failure(
                conn,
                group_id,
                TIER,
                stage2._Key(scorer, DEEP_PROMPT_VERSION, profile.scoring_version),
                rev[0] if rev else 0,
                stop if stop in ("refusal", "max_tokens") else "invalid",
                now,
            )
        return DeepResult(status, cost_usd=cost, detail=str(exc))

    try:
        with _txn(conn):
            result = _write(
                conn,
                group_id,
                report,
                job_d,
                usage,
                cost,
                scorer,
                profile,
                now,
                served_model=getattr(message, "model", "") or "",
            )
    except Exception as exc:
        # The spend is already recorded; the caller learns what the failed call cost.
        raise DeepWriteError(group_id, cost) from exc
    result.cost_usd = cost
    return result


def record_call_spend(
    conn: sqlite3.Connection, scorer: str, now: datetime, usage: Any, cost: float
) -> None:
    """Add one billed deep call to ``llm_spend`` and commit it before anything else runs."""
    with _txn(conn):
        stage2._record_spend(
            conn,
            scorer,
            now,
            1,
            stage2._all_input_tokens(usage),
            stage2._usage_int(usage, "output_tokens"),
            cost,
            tier=TIER,
        )


def _write(
    conn: sqlite3.Connection,
    group_id: int,
    report: DeepReport,
    job: dict[str, Any],
    usage: Any,
    cost: float,
    scorer: str,
    profile: Profile,
    now: datetime,
    served_model: str = "",
) -> DeepResult:
    haystack = stage2._posting_haystack(job)
    _, bad_quotes = stage2.verify_evidence(report, haystack)
    norm = stage2.normalize_for_match(haystack)
    evidence = [
        {"claim": e.claim, "quote": e.quote, "verified": stage2.quote_found(e.quote, norm)}
        for e in report.evidence
    ]
    gaps = [
        {**g.model_dump(mode="json"), "verified": stage2.quote_found(g.evidence, norm)}
        for g in report.requirement_gaps
    ]
    unverified = bad_quotes > 0 or any(not g["verified"] for g in gaps)

    dimensions: dict[str, Any] = {
        name: dim.model_dump(mode="json") for name, dim in report.dimensions.items()
    }
    dimensions.update(
        raw_skills=report.raw_skills,
        recency_weighted_skills=report.recency_weighted_skills,
        current_focus_overlap=report.current_focus_overlap,
        stale_skills=report.stale_skills,
        done_with_hits=report.done_with_hits,
        seniority_direction=stage2.seniority_direction(report),
        evidence_unverified=unverified,
    )
    row_for_buckets = {
        "dimensions": json.dumps(dimensions),
        "blockers": json.dumps(report.blockers),
    }
    locations = load_job_group_locations(conn, job["id"])
    deep_fit = compute_row(row_for_buckets, job, locations, profile)

    disagreement: dict[str, Any] = {
        "model_flag": report.screen_disagreement.flag,
        "model_reason": report.screen_disagreement.reason,
        "deep_bucket": deep_fit.bucket.value,
        "screen_bucket": None,
        "levels": None,
        "bucket_flag": False,
    }
    screen_row = _latest_screen(conn, group_id, profile)
    if screen_row is not None:
        screen_fit = compute_row(screen_row, job, locations, profile)
        levels = _bucket_gap(screen_fit.bucket.value, deep_fit.bucket.value)
        disagreement.update(
            screen_bucket=screen_fit.bucket.value,
            levels=levels,
            bucket_flag=levels > DISAGREEMENT_LEVELS,
        )
    flagged = bool(disagreement["bucket_flag"])
    shape_flags = list(report.shape_flags)
    if flagged and "screen_disagreement" not in shape_flags:
        shape_flags.append("screen_disagreement")

    stored_report = {
        "recommendation": report.recommendation,
        "summary": report.summary,
        "requirement_gaps": gaps,
        "emphasize": report.emphasize,
        "questions_to_ask": report.questions_to_ask,
        "salary_read": report.salary_read,
        "screen_disagreement": disagreement,
    }
    cur = conn.execute(
        "INSERT INTO fit_score (job_group_id, tier, model, prompt_version, scoring_version, "
        "verdict, overall, dimensions, evidence, blockers, missing_info, tailoring_hints, "
        "shape_flags, evidence_unverified, input_tokens, output_tokens, cache_read_tokens, "
        "cost_usd, batch_id, created_at, deep_report, served_model) "
        "VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)",
        (
            group_id,
            TIER,
            scorer,
            DEEP_PROMPT_VERSION,
            profile.scoring_version,
            report.verdict.value,
            json.dumps(dimensions),
            json.dumps(evidence),
            json.dumps(report.blockers),
            json.dumps(report.missing_info),
            json.dumps(report.tailoring_hints),
            json.dumps(shape_flags),
            int(unverified),
            stage2._usage_int(usage, "input_tokens"),
            stage2._usage_int(usage, "output_tokens"),
            stage2._usage_int(usage, "cache_read_input_tokens"),
            cost,
            to_iso(now),
            json.dumps(stored_report),
            served_model or None,
        ),
    )
    row = conn.execute("SELECT * FROM fit_score WHERE id = ?", (cur.lastrowid,)).fetchone()
    return DeepResult("scored", row, disagreement=flagged)
