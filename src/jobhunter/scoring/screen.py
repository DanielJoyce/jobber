"""Stage 2 screen: Haiku 5.5 over the Batch API (specs/006 "Stage 2 — the screen").

One request per ``job_group`` (its canonical job), never per job or location. The request is
``system = [rubric, profile scoring inputs + cache_control]`` and a user message holding only
the posting. Salary floors, state preferences and weights never reach the model (specs/014).

Results are parsed into ``Screen``, every evidence quote is checked verbatim against the
posting, and one ``fit_score`` row is written per (group, tier, prompt_version,
scoring_version, model). ``overall`` is computed later in Python, so it is stored as 0 here.
"""

from __future__ import annotations

import json
import logging
import math
import re
import sqlite3
import time
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

from pydantic import ValidationError

from jobhunter import logsetup
from jobhunter.config import Scoring
from jobhunter.core import rejections
from jobhunter.core.models import LocationScope, Screen
from jobhunter.core.spend import SCORING_ONLY
from jobhunter.pipeline.listing import _txn, to_iso
from jobhunter.pipeline.locations import load_job_group_locations, location_summary
from jobhunter.scoring.profile import Profile, scoring_inputs
from jobhunter.scoring.rubric import (
    PROMPT_VERSION,
    RUBRIC_TEXT,
    SCREEN_SCHEMA,
    packed_json_schema,
)
from jobhunter.scoring.scorers import (  # noqa: F401  (pricing/cost helpers re-exported)
    PRICING_PER_MTOK,
    AnthropicScorer,
    FitScorer,
    LocalScorer,
    OpenAICompatScorer,
    OpenRouterScorer,
    ScoreRequest,
    ScoreResult,
    Usage,
    _usage_int,
    anthropic_params,
    cached_openrouter_rates,
    ceiling_cost,
    compute_cost,
    model_id,
)

logger = logging.getLogger(__name__)

TIER = "screen"
DEFAULT_SCORER = "anthropic:claude-haiku-5-5"
MAX_TOKENS = 2048
# Planning estimate per request for spend caps. Set for Haiku 4.5 batched (about $0.002); Haiku
# 5.5 costs roughly a tenth of that (specs/006 "Cost"). Kept high on purpose so a cap check is
# never passed on an estimate that is too low.
ESTIMATED_COST_PER_REQUEST_USD = 0.002
# A typical screen call (specs/006 "Cost"), in the newer tokenizer's counts (Haiku 5.5 and
# Opus 4.7 onward count the same text as about 30% more tokens): 2,600 uncached input, 3,900
# cached, 455 output. For an older-tokenizer model this overestimates by about 30%, the safe
# direction for a spend reservation.
TYPICAL_USAGE = SimpleNamespace(
    input_tokens=2600,
    cache_creation_input_tokens=0,
    cache_read_input_tokens=3900,
    output_tokens=455,
)
_CUSTOM_ID = re.compile(r"^g(\d+)$")

# The cached prefix of a screen request (``system`` = rubric + profile scoring inputs, plus the
# output schema, which ``output_config`` also bills as input), sized from its text for the
# floor of every Anthropic spend hold (020 M5b review B1, B2). 2.75 characters a token: the
# newer tokenizer counts English prose at about 3.1 characters a token, but the JSON schema
# at about 2.3, so 3 could undercount a schema-heavy prefix; 2.75 errs high on the mix (the
# rubric is mostly prose). Without a profile at hand, ``DEFAULT_PROFILE_CHARS`` stands in
# for the scoring inputs (a long resume and narrative).
PREFIX_CHARS_PER_TOKEN = 2.75
DEFAULT_PROFILE_CHARS = 12_000
_PREFIX_FIXED_CHARS = len(RUBRIC_TEXT) + len(json.dumps(SCREEN_SCHEMA))


def prefix_tokens(profile: Profile | None = None) -> int:
    """Estimated tokens of the cached prefix for ``profile`` (``DEFAULT_PROFILE_CHARS`` when
    None). Never below ``TYPICAL_USAGE``'s cached part."""
    chars = _PREFIX_FIXED_CHARS + (
        len(scoring_inputs(profile)) if profile is not None else DEFAULT_PROFILE_CHARS
    )
    return max(math.ceil(chars / PREFIX_CHARS_PER_TOKEN), TYPICAL_USAGE.cache_read_input_tokens)


def cold_usage(prefix: int) -> SimpleNamespace:
    """A typical screen whose cached prefix is written, never read: the dearest case for a
    cached prompt (a cache write is 1.25x input; a miss 1x; a hit 0.1x or 0.05x). Batch cache
    hits are best effort, so a model with no history is held at this."""
    return SimpleNamespace(
        input_tokens=TYPICAL_USAGE.input_tokens,
        cache_creation_input_tokens=prefix,
        cache_read_input_tokens=0,
        output_tokens=TYPICAL_USAGE.output_tokens,
    )


def jobs_affordable(remaining_usd: float, per_job_usd: float) -> int:
    """How many jobs held at ``per_job_usd`` fit in ``remaining_usd``. The one comparison
    every cap check and every page uses, so a plan and its run agree at the boundary."""
    if per_job_usd <= 0:
        return 10**9
    return math.floor(max(remaining_usd, 0.0) / per_job_usd + 1e-9)


class ScreenError(Exception):
    """A screen result could not be turned into a Screen."""


# ─── Request construction ───────────────────────────────────────────────────


def _employment(value: str | None) -> str:
    if not value or value == "unknown":
        return "not stated"
    return value.replace("_", " ")


def posting_text(job: Mapping[str, Any], locations_summary: str) -> str:
    """The user message: this one posting, nothing about the candidate's filters or ids."""
    scope = job.get("location_scope") or LocationScope.unknown.value
    remote = job.get("remote") or "unknown"
    location = f"{locations_summary} (scope: {scope}; remote: {remote})"
    salary = (job.get("salary_raw") or "").strip() or "not stated"
    description = (job.get("description_text") or "").strip() or "(no description provided)"
    lines = [
        "# Job posting",
        "",
        f"Title: {job.get('title') or 'not stated'}",
        f"Employer: {job.get('employer') or 'not stated'}",
        f"Location: {location}",
        f"Stated salary: {salary}",
        f"Employment type: {_employment(job.get('employment_type'))}",
    ]
    if job.get("prior_rejection"):
        # A neutral fact (specs/006 "Employer rejections"); never pay or location.
        lines.append(f"Candidate history with this employer: {job['prior_rejection']}.")
    if job.get("description_completeness") == "partial":
        lines.append(
            "Note: the description below is partial (a summary or truncated listing, such as "
            "an email alert entry); the full posting was not available and may say more. "
            "Do not treat missing requirements or details as evidence either way."
        )
    lines += ["", "## Description", "", description]
    return "\n".join(lines) + "\n"


def build_score_request(
    job: Mapping[str, Any],
    locations_summary: str,
    profile: Profile,
    *,
    custom_id: str = "",
    max_tokens: int = MAX_TOKENS,
) -> ScoreRequest:
    """The provider-neutral pieces of one screen call."""
    return ScoreRequest(
        custom_id=custom_id,
        system=RUBRIC_TEXT,
        profile=scoring_inputs(profile),
        posting=posting_text(job, locations_summary),
        schema=SCREEN_SCHEMA,
        max_tokens=max_tokens,
    )


def build_request(
    job: Mapping[str, Any],
    locations_summary: str,
    profile: Profile,
    *,
    scorer: str = DEFAULT_SCORER,
    max_tokens: int = MAX_TOKENS,
) -> dict[str, Any]:
    """Anthropic Messages API params for one screen (see ``scorers.anthropic_params``)."""
    request = build_score_request(job, locations_summary, profile, max_tokens=max_tokens)
    return anthropic_params(request, model_id(scorer))


# ─── Evidence verification ──────────────────────────────────────────────────

_PUNCT = str.maketrans(
    {
        0x2018: "'",  # left single quotation mark
        0x2019: "'",  # right single quotation mark
        0x201C: '"',  # left double quotation mark
        0x201D: '"',  # right double quotation mark
        0x2013: "-",  # en dash
        0x2014: "-",  # em dash
        0x00A0: " ",  # no-break space
    }
)
_WS = re.compile(r"\s+")
_QUOTE_EDGES = "\"' \t\r\n"


def normalize_for_match(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).translate(_PUNCT)
    return _WS.sub(" ", text).strip().casefold()


def quote_found(quote: str, haystack_normalized: str) -> bool:
    needle = normalize_for_match(quote).strip(_QUOTE_EDGES)
    return bool(needle) and needle in haystack_normalized


def evidence_checks(screen: Screen, text: str) -> list[bool]:
    """Per evidence item: is its quote present in ``text`` (whitespace/case-insensitive)?"""
    haystack = normalize_for_match(text or "")
    return [quote_found(item.quote, haystack) for item in screen.evidence]


def verify_evidence(screen: Screen, description_text: str) -> tuple[Screen, int]:
    """Count evidence quotes not found verbatim in the posting text.

    Any nonzero count marks the stored score ``evidence_unverified`` (specs/006).
    """
    return screen, evidence_checks(screen, description_text).count(False)


def _posting_haystack(job: Mapping[str, Any]) -> str:
    """Posting text quotes may come from: title, employer, stated salary, description."""
    keys = ("title", "employer", "salary_raw", "description_text")
    return "\n".join(job[k] for k in keys if job.get(k))


# ─── Parsing and storage ────────────────────────────────────────────────────


def _message_text(message: Any) -> str:
    for block in getattr(message, "content", None) or []:
        if getattr(block, "type", None) == "text":
            return block.text
    raise ScreenError("response has no text block")


def parse_message(message: Any) -> Screen:
    stop = getattr(message, "stop_reason", None)
    if stop in ("max_tokens", "refusal"):
        raise ScreenError(f"stop_reason {stop}")
    try:
        return Screen.model_validate_json(_message_text(message))
    except ValidationError as exc:
        raise ScreenError(f"output does not match Screen: {exc.error_count()} errors") from exc


def parse_result(result: ScoreResult) -> Screen:
    if result.stop_reason in ("max_tokens", "refusal"):
        raise ScreenError(f"stop_reason {result.stop_reason}")
    if result.text is None:
        raise ScreenError("response has no text block")
    try:
        return Screen.model_validate_json(result.text)
    except ValidationError as exc:
        raise ScreenError(f"output does not match Screen: {exc.error_count()} errors") from exc


_DIRECTION = re.compile(r"^\s*(above|below|match)\s*:", re.IGNORECASE)


def seniority_direction(screen: Screen) -> str:
    """ "above" / "below" / "match" from the rubric's required prefix on seniority.why.

    Stored in the dimensions JSON as ``seniority_direction`` for buckets C and E.
    A missing or malformed prefix falls back to "match".
    """
    dim = screen.dimensions.get("seniority")
    m = _DIRECTION.match(dim.why) if dim is not None else None
    return m.group(1).lower() if m else "match"


@dataclass
class _Key:
    model: str
    prompt_version: str
    scoring_version: str


def _write_fit_score(
    conn: sqlite3.Connection,
    group_id: int,
    screen: Screen,
    job: Mapping[str, Any],
    usage: Any,
    cost: float,
    key: _Key,
    batch_id: str | None,
    now: datetime,
    served_model: str = "",
    input_rev: int = 0,
) -> tuple[int | None, bool]:
    """Insert one fit_score row; returns (row id or None if it already existed, unverified)."""
    checks = evidence_checks(screen, _posting_haystack(job))
    evidence = [
        {"claim": e.claim, "quote": e.quote, "verified": ok}
        for e, ok in zip(screen.evidence, checks, strict=True)
    ]
    unverified = any(not e["verified"] for e in evidence)
    dimensions: dict[str, Any] = {
        name: dim.model_dump(mode="json") for name, dim in screen.dimensions.items()
    }
    dimensions.update(
        raw_skills=screen.raw_skills,
        recency_weighted_skills=screen.recency_weighted_skills,
        current_focus_overlap=screen.current_focus_overlap,
        stale_skills=screen.stale_skills,
        done_with_hits=screen.done_with_hits,
        seniority_direction=seniority_direction(screen),
        evidence_unverified=unverified,
    )
    cur = conn.execute(
        "INSERT INTO fit_score (job_group_id, tier, model, prompt_version, scoring_version, "
        "input_rev, verdict, overall, dimensions, evidence, blockers, missing_info, "
        "tailoring_hints, shape_flags, evidence_unverified, input_tokens, output_tokens, "
        "cache_read_tokens, cost_usd, batch_id, created_at, served_model) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (job_group_id, tier, prompt_version, scoring_version, model, input_rev) "
        "DO NOTHING",
        (
            group_id,
            TIER,
            key.model,
            key.prompt_version,
            key.scoring_version,
            input_rev,
            screen.verdict.value,
            json.dumps(dimensions),
            json.dumps(evidence),
            json.dumps(screen.blockers),
            json.dumps(screen.missing_info),
            json.dumps(screen.tailoring_hints),
            json.dumps(screen.shape_flags),
            int(unverified),
            _usage_int(usage, "input_tokens"),
            _usage_int(usage, "output_tokens"),
            _usage_int(usage, "cache_read_input_tokens"),
            cost,
            batch_id,
            to_iso(now),
            served_model or None,
        ),
    )
    if cur.rowcount == 0:
        return None, unverified
    conn.execute(
        "UPDATE job SET stage = 'scored' WHERE job_group_id = ? AND stage = 'prefiltered'",
        (group_id,),
    )
    return cur.lastrowid, unverified


def _record_spend(
    conn: sqlite3.Connection,
    model: str,
    now: datetime,
    calls: int,
    input_tokens: int,
    output_tokens: int,
    cost: float,
    tier: str = TIER,
) -> None:
    if calls == 0:
        return
    conn.execute(
        "INSERT INTO llm_spend (day, model, tier, calls, input_tokens, output_tokens, cost_usd) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (day, model, tier) DO UPDATE SET calls = calls + excluded.calls, "
        "input_tokens = coalesce(input_tokens, 0) + excluded.input_tokens, "
        "output_tokens = coalesce(output_tokens, 0) + excluded.output_tokens, "
        "cost_usd = cost_usd + excluded.cost_usd",
        (to_iso(now)[:10], model, tier, calls, input_tokens, output_tokens, cost),
    )


def _all_input_tokens(usage: Any) -> int:
    return (
        _usage_int(usage, "input_tokens")
        + _usage_int(usage, "cache_creation_input_tokens")
        + _usage_int(usage, "cache_read_input_tokens")
    )


# ─── Selection and spend ────────────────────────────────────────────────────

# Groups scored only on request (``job_group.score_on_request``: pasted, captured and
# email-manual groups) are eligible only when named in ``:only``: such a group is scored solely
# by Score this group now / Score it (specs/017), never by a daily run, which selects without
# ``:only``. Re-score plans also drop flagged groups from their own id lists (rescore.py).
_ELIGIBLE = """
SELECT g.id AS group_id, g.description_rev AS input_rev, j.*
FROM job_group g
JOIN job j ON j.id = g.canonical_job_id
JOIN prefilter_result p ON p.job_id = j.id AND p.passed = 1 AND p.filter_version = :filter_version
WHERE NOT EXISTS (
    SELECT 1 FROM fit_score f
    WHERE f.job_group_id = g.id AND f.tier = :tier AND f.model = :model
      AND f.prompt_version = :prompt_version AND f.scoring_version = :scoring_version
      AND f.input_rev = g.description_rev)
  AND NOT EXISTS (
    SELECT 1 FROM score_batch_item i JOIN score_batch b ON b.id = i.batch_id
    WHERE i.job_group_id = g.id AND b.collected_at IS NULL AND b.tier = :tier
      AND b.model = :model AND b.prompt_version = :prompt_version
      AND b.scoring_version = :scoring_version)
  AND g.id NOT IN (SELECT value FROM json_each(:rejected))
  AND (:only IS NULL OR g.id IN (SELECT value FROM json_each(:only)))
  AND (:only IS NOT NULL OR g.score_on_request = 0)
  AND (NOT :new_only OR (
    NOT EXISTS (
      SELECT 1 FROM fit_score f
      WHERE f.job_group_id = g.id AND f.tier = :tier AND f.input_rev = g.description_rev)
    AND NOT EXISTS (
      SELECT 1 FROM score_batch_item i JOIN score_batch b ON b.id = i.batch_id
      WHERE i.job_group_id = g.id AND b.collected_at IS NULL AND b.tier = :tier)
    AND g.id NOT IN (
      SELECT c.value FROM rescore_request r, json_each(r.group_ids) c
      WHERE r.status IN ('pending', 'running') AND r.group_ids IS NOT NULL)
    AND NOT EXISTS (
      SELECT 1 FROM score_failure x
      WHERE x.job_group_id = g.id AND x.tier = :tier AND x.model = :model
        AND x.prompt_version = :prompt_version AND x.scoring_version = :scoring_version
        AND x.input_rev = g.description_rev)))
ORDER BY j.posted_at IS NULL, j.posted_at DESC, g.id
LIMIT :limit
"""


def eligible_groups(
    conn: sqlite3.Connection,
    profile: Profile,
    *,
    scorer: str,
    limit: int,
    group_ids: Sequence[int] | None = None,
    new_only: bool = False,
) -> list[sqlite3.Row]:
    """Groups whose canonical job passed the current prefilter and have no screen yet.

    "Yet" is per description revision: pasting a description bumps
    ``job_group.description_rev`` and the group is screened again (specs/012); the earlier
    score stays. A job with no prefilter_result row is not yet eligible. Groups already in an
    uncollected batch for the same key are skipped so they are never submitted twice. A
    posting the candidate was rejected for (``core/rejections``) is never eligible.
    ``group_ids`` narrows the result to those groups (a re-score runs only its confirmed plan).

    ``new_only`` is the daily run's rule: only groups with no screen score at all for their
    current description revision (under any model, prompt or ``scoring_version``), none in
    an uncollected batch, and none listed in the confirmed plan of a pending or running
    re-score. Re-scoring an already scored group costs money, so it happens only through a
    confirmed re-score, never automatically (specs/006, specs/014). A group a plan covers is
    left to that plan: the plan checks eligibility per scorer, so a daily score with another
    model would not stop the plan paying for the same group again.
    """
    return conn.execute(
        _ELIGIBLE,
        {
            "filter_version": profile.filter_version,
            "tier": TIER,
            "model": scorer,
            "prompt_version": PROMPT_VERSION,
            "scoring_version": profile.scoring_version,
            "limit": limit,
            "rejected": rejections.rejected_json(conn),
            "only": only_json(group_ids),
            "new_only": int(new_only),
        },
    ).fetchall()


def only_json(group_ids: Sequence[int] | None) -> str | None:
    """The ``:only`` parameter of ``_ELIGIBLE``: None means every group."""
    return None if group_ids is None else json.dumps([int(g) for g in group_ids])


def table_price_per_request_usd(scorer: str) -> float:
    """``scorer``'s batch price for one ``TYPICAL_USAGE`` request, or 0.0 when unpriced.

    Only Anthropic models have a price table; any other spec, or a model missing from it,
    gives 0.0 so the caller falls back to its own floor.
    """
    try:
        return compute_cost(TYPICAL_USAGE, scorer, batch=True)
    except ValueError:
        return 0.0


def measured_cost_per_job_usd(conn: sqlite3.Connection, scorer: str) -> float:
    """Mean measured screen cost per job for ``scorer`` (``fit_score``), or 0.0 with none."""
    avg = conn.execute(
        "SELECT avg(cost_usd) FROM fit_score WHERE tier = ? AND model = ? AND cost_usd > 0",
        (TIER, scorer),
    ).fetchone()[0]
    return float(avg or 0.0)


# A model with no known price is over the line, not free (bug df51dff). A chat model (an
# OpenRouter slug missing from the cached catalog, or a router with variable pricing) reserves
# this per job, or ``scoring.openrouter.est_cost_per_request_usd`` when that is higher; an
# Anthropic model missing from the price table reserves the table's dearest rates.
UNPRICED_RESERVE_PER_JOB_USD = 0.04
# score_sync sends at most this many single-job requests between spend-cap checks (or one
# wave of ``max_concurrency`` when that is larger), so a run cannot get far past the cap.
SYNC_CHUNK = 8


def chat_price_usd(rates: tuple[float, float], n_jobs: int = 1) -> float:
    """A chat model's price for ``n_jobs`` typical screens in one request.

    ``rates`` is (input, output) USD per million tokens. Every prompt token is priced at the
    input rate: OpenRouter and openai-compat usage count cached tokens as prompt tokens, and
    the scorers' ``cost`` prices them that way. The rubric + profile prefix
    (``TYPICAL_USAGE.cache_read_input_tokens``) is sent once per request; the posting and the
    answer once per job.
    """
    n = max(int(n_jobs), 1)
    tokens_in = TYPICAL_USAGE.cache_read_input_tokens + TYPICAL_USAGE.input_tokens * n
    tokens_out = TYPICAL_USAGE.output_tokens * n
    return (tokens_in * rates[0] + tokens_out * rates[1]) / 1_000_000


def list_price_per_job_usd(
    spec: str, scoring: Scoring | None = None, *, batch: bool = False
) -> float | None:
    """``spec``'s list price for one ``TYPICAL_USAGE`` screen, or None when it has no price.

    Anthropic: the price table. OpenRouter: the cached public catalog at
    ``scoring.openrouter.catalog_cache``, read from disk at any age and never fetched (the
    reservation is made before any request goes out). openai-compat: its configured prices
    (0 when unset, a self-hosted server; its cost is recorded as 0 too). Local models are
    free; Jev/decisions requests check the cap themselves, per request (decisions.py).
    """
    scoring = scoring or Scoring()
    provider, _, name = spec.partition(":")
    if not name:
        provider, name = "anthropic", spec
    if provider == "anthropic":
        try:
            return compute_cost(TYPICAL_USAGE, f"anthropic:{name}", batch=batch)
        except ValueError:
            return None
    if provider == "openrouter":
        rates = cached_openrouter_rates(scoring.openrouter, name)
        return None if rates is None else chat_price_usd(rates)
    if provider == "openai-compat":
        compat = scoring.openai_compat
        return chat_price_usd((compat.input_usd_per_mtok, compat.output_usd_per_mtok))
    return 0.0


def unpriced_reserve_usd(spec: str, scoring: Scoring | None = None, *, batch: bool) -> float:
    """What one job to a model with no known price reserves (``UNPRICED_RESERVE_PER_JOB_USD``)."""
    if spec.startswith("anthropic:") or ":" not in spec:
        return ceiling_cost(TYPICAL_USAGE, batch=batch)
    est = (scoring or Scoring()).openrouter.est_cost_per_request_usd
    return max(UNPRICED_RESERVE_PER_JOB_USD, est)


_LOOK_UP: Any = object()


def reserve_per_job_usd(
    conn: sqlite3.Connection,
    spec: str,
    scoring: Scoring | None = None,
    *,
    batch: bool = False,
    price: float | None = _LOOK_UP,
    profile: Profile | None = None,
) -> float:
    """What the spend cap reserves for one screen job to ``spec`` (bugs 026b50c, df51dff).

    The highest of ``ESTIMATED_COST_PER_REQUEST_USD``, the mean measured for that model, and
    its list price at ``TYPICAL_USAGE`` (``list_price_per_job_usd``, or ``price`` when the
    caller already looked it up). Never below the old flat $0.002, so a cheap model reserves
    exactly what it did; a dearer one reserves its own price. A model with no list price is
    priced by its measured history when it has some, else by the ``unpriced_reserve_usd``
    guess: over the line only while nothing at all is known about its price.

    An Anthropic model is always held at no less than ``cold_price_per_job_usd``: the
    prefix sized from ``profile`` and every cached token written, none read (020 M5b review
    B1, B2). ``TYPICAL_USAGE`` assumes a 3,900-token prefix that always hits the cache, and a
    measured mean only says what earlier batches happened to reuse; batch cache hits are best
    effort (30-98% seen), so the next batch could otherwise cost two to three times its hold.
    The floor is permanent: a measured mean above it still raises the hold.
    """
    if price is _LOOK_UP:
        price = list_price_per_job_usd(spec, scoring, batch=batch)
    measured = measured_cost_per_job_usd(conn, spec)
    if price is None:
        price = measured if measured > 0 else unpriced_reserve_usd(spec, scoring, batch=batch)
    hold = max(ESTIMATED_COST_PER_REQUEST_USD, measured, price)
    if _is_anthropic_spec(spec):
        hold = max(hold, cold_price_per_job_usd(spec, batch=batch, profile=profile))
    return hold


def _is_anthropic_spec(spec: str) -> bool:
    return spec.startswith("anthropic:") or ":" not in spec


def cold_price_per_job_usd(spec: str, *, batch: bool, profile: Profile | None = None) -> float:
    """An Anthropic ``spec``'s price for one screen whose cached prefix (``prefix_tokens``)
    is all cache writes (``cold_usage``). A model missing from the price table is priced at
    the table's dearest rates."""
    usage = cold_usage(prefix_tokens(profile))
    name = spec if spec.startswith("anthropic:") else f"anthropic:{spec}"
    try:
        return compute_cost(usage, name, batch=batch)
    except ValueError:
        return ceiling_cost(usage, batch=batch)


def scorer_list_price_usd(scorer: Any) -> float | None:
    """``list_price_per_job_usd`` for a scorer that is about to send requests.

    An OpenRouter scorer reads its catalog through its own ``catalog()``: the disk cache when
    under a day old, else one fetch of the public, free ``/models`` list, which is then cached
    on disk for the console's estimates (those only read the cache). Only scoring paths call
    this; nothing that renders a page does.
    """
    if isinstance(scorer, OpenRouterScorer):
        rates = scorer.catalog_rates()
        return None if rates is None else chat_price_usd(rates)
    return list_price_per_job_usd(scorer.name, scoring_for(scorer))


def reserve_per_request_usd(
    conn: sqlite3.Connection, scorer: str, profile: Profile | None = None
) -> float:
    """What the spend cap reserves for one batched screen request to Anthropic ``scorer``.

    ``reserve_per_job_usd`` at batch prices: Haiku 5.5 reserves the flat $0.002; Sonnet 5.5
    at least its all-cache-writes price (about $0.013 to $0.015 a job for a real profile);
    a model missing from the price table the table's dearest rates.
    """
    return reserve_per_job_usd(conn, scorer, batch=True, profile=profile)


def scoring_for(scorer: Any) -> Scoring:
    """A ``Scoring`` carrying ``scorer``'s own provider config, for pricing a reservation."""
    if isinstance(scorer, OpenRouterScorer):
        return Scoring(openrouter=scorer.openrouter)
    if isinstance(scorer, OpenAICompatScorer) and not isinstance(scorer, LocalScorer):
        return Scoring(openai_compat=scorer.config)
    return Scoring()


def _pending_batch_usd(conn: sqlite3.Connection) -> float:
    """Estimated cost of submitted batches not yet collected (not in ``llm_spend`` yet).

    Each batch's requests are held at what was reserved for them when it was submitted
    (``score_batch.reserve_per_request_usd``), or at ``reserve_per_request_usd`` for its model
    for a batch from before that column; never below the model's measured mean since.
    """
    rows = conn.execute(
        "SELECT model, reserve_per_request_usd, coalesce(sum(request_count), 0) "
        "FROM score_batch WHERE collected_at IS NULL GROUP BY model, reserve_per_request_usd"
    ).fetchall()
    total = 0.0
    for model, stored, n in rows:
        held = float(stored) if stored else reserve_per_request_usd(conn, str(model))
        total += int(n) * max(held, measured_cost_per_job_usd(conn, str(model)))
    return total


def remaining_daily_budget(conn: sqlite3.Connection, cap_usd: float, now: datetime) -> float:
    """``cap_usd`` minus today's recorded spend and an estimate for uncollected batches."""
    spent = conn.execute(
        f"SELECT coalesce(sum(cost_usd), 0) FROM llm_spend WHERE day = ? AND {SCORING_ONLY}",
        (to_iso(now)[:10],),
    ).fetchone()[0]
    return cap_usd - spent - _pending_batch_usd(conn)


def weekly_remaining(conn: sqlite3.Connection, cap_usd: float, now: datetime) -> float:
    """``cap_usd`` minus the last seven days' spend and an estimate for uncollected batches."""
    since = (now - timedelta(days=6)).date().isoformat()
    spent = conn.execute(
        f"SELECT coalesce(sum(cost_usd), 0) FROM llm_spend WHERE day >= ? AND {SCORING_ONLY}",
        (since,),
    ).fetchone()[0]
    return cap_usd - spent - _pending_batch_usd(conn)


def remaining_budget(conn: sqlite3.Connection, scoring: Scoring, now: datetime) -> float:
    """The tighter of the daily and weekly spend caps (``scoring.daily_cap_usd`` and
    ``scoring.weekly_cap_usd``). Every automatic or confirmed scoring run checks this."""
    return min(
        remaining_daily_budget(conn, scoring.daily_cap_usd, now),
        weekly_remaining(conn, scoring.weekly_cap_usd, now),
    )


def _summary_for(conn: sqlite3.Connection, job: Mapping[str, Any]) -> str:
    return location_summary(load_job_group_locations(conn, job["id"]), job["location_scope"])


def _row(job: sqlite3.Row) -> dict[str, Any]:
    return dict(job)


def _context_rows(
    conn: sqlite3.Connection,
    groups: Sequence[Mapping[str, Any]],
    now: datetime | None,
    rejection_days: int,
) -> list[dict[str, Any]]:
    """Job rows as dicts, each carrying ``prior_rejection`` when the employer turned the
    candidate down for another role recently. Not part of any version key: adding a fact
    never forces a re-score (specs/006 "Employer rejections")."""
    rows = [dict(g) for g in groups]
    rejections.annotate(conn, rows, now=now, days=rejection_days)
    return rows


# ─── Batch submit / collect ─────────────────────────────────────────────────


def _score_requests(
    conn: sqlite3.Connection,
    groups: list[sqlite3.Row],
    profile: Profile,
    *,
    now: datetime | None = None,
    rejection_days: int = rejections.DEFAULT_WINDOW_DAYS,
) -> list[ScoreRequest]:
    return [
        build_score_request(row, _summary_for(conn, row), profile, custom_id=f"g{row['group_id']}")
        for row in _context_rows(conn, groups, now, rejection_days)
    ]


def submit_batch(
    conn: sqlite3.Connection,
    client: Any,
    profile: Profile,
    *,
    limit: int,
    now: datetime,
    scorer: str = DEFAULT_SCORER,
    remaining_usd: Callable[[], float] | None = None,
    rejection_days: int = rejections.DEFAULT_WINDOW_DAYS,
    group_ids: Sequence[int] | None = None,
    new_only: bool = False,
) -> str | None:
    """Submit one Message Batch for eligible groups. Returns the batch id, or None.

    ``remaining_usd`` is the spend cap: the batch is cut to what the remaining budget covers
    at ``reserve_per_request_usd`` for ``scorer``; nothing is submitted when that is zero.
    Raises ``ValueError`` for a model missing from the price table: its spend could not be
    recorded correctly, so nothing is sent (bug df51dff).
    """
    if list_price_per_job_usd(scorer, batch=True) is None:
        raise ValueError(
            f"no price for {scorer!r}: add it to PRICING_PER_MTOK in scoring/scorers.py, or "
            "pick a listed model; nothing submitted"
        )
    per_request = reserve_per_request_usd(conn, scorer, profile)
    if remaining_usd is not None:
        affordable = jobs_affordable(remaining_usd(), per_request)
        if affordable < limit:
            logger.warning("spend cap: screening limited to %d groups", affordable)
        limit = min(limit, affordable)
    if limit <= 0:
        logger.info("screen batch not submitted: spend cap leaves room for 0 groups")
        return None
    groups = eligible_groups(
        conn, profile, scorer=scorer, limit=limit, group_ids=group_ids, new_only=new_only
    )
    if not groups:
        logger.info("screen batch not submitted: no eligible groups")
        return None

    requests = _score_requests(conn, groups, profile, now=now, rejection_days=rejection_days)
    batch_id = AnthropicScorer(client, scorer).submit(requests)
    with _txn(conn):
        conn.execute(
            "INSERT INTO score_batch (id, tier, model, prompt_version, scoring_version, "
            "request_count, submitted_at, reserve_per_request_usd) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                batch_id,
                TIER,
                scorer,
                PROMPT_VERSION,
                profile.scoring_version,
                len(requests),
                to_iso(now),
                per_request,
            ),
        )
        conn.executemany(
            "INSERT INTO score_batch_item (batch_id, custom_id, job_group_id, input_rev) "
            "VALUES (?, ?, ?, ?)",
            [(batch_id, f"g{g['group_id']}", g["group_id"], g["input_rev"]) for g in groups],
        )
    logger.info(
        "submitted screen batch %s: %d requests, model %s, hold $%.2f against the cap",
        batch_id,
        len(requests),
        logsetup.spec_label(scorer),
        per_request * len(requests),
    )
    return batch_id


@dataclass
class CollectResult:
    status: str = "ended"  # ended | in_progress | already_collected | error: <detail>
    succeeded: int = 0
    written: int = 0
    duplicate: int = 0
    invalid: int = 0
    errored: int = 0
    canceled: int = 0
    expired: int = 0
    unverified: int = 0
    cost_usd: float = 0.0
    cache_read_zero: int = 0
    unknown_ids: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if k != "unknown_ids"}


def _canonical_job(conn: sqlite3.Connection, group_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT j.* FROM job_group g JOIN job j ON j.id = g.canonical_job_id WHERE g.id = ?",
        (group_id,),
    ).fetchone()


def check_cache_reads(cache_reads: list[int]) -> int:
    """Warn when no result after the first read the cache. Returns zero-read results past #1."""
    if len(cache_reads) < 2:
        return 0
    zero_after_first = sum(1 for n in cache_reads[1:] if n == 0)
    if all(n == 0 for n in cache_reads):
        logger.warning(
            "prompt cache never hit across %d screen results (cache_read_input_tokens = 0). "
            "Likely cause: the rubric + profile prefix is shorter than the model's minimum "
            "cacheable length, or volatile content in the system blocks is changing the prefix.",
            len(cache_reads),
        )
    return zero_after_first


def collect_batch(
    conn: sqlite3.Connection,
    client: Any,
    batch_id: str,
    profile: Profile,
    *,
    now: datetime,
) -> CollectResult:
    """Read a finished batch and write fit_score rows. Errored/expired groups stay eligible.

    Scores are stored under the versions the batch was submitted with, not the current
    profile's, since that is what the model actually read.
    """
    batch_row = conn.execute("SELECT * FROM score_batch WHERE id = ?", (batch_id,)).fetchone()
    if batch_row is None:
        raise ValueError(f"unknown batch {batch_id!r}")
    if batch_row["collected_at"] is not None:
        return CollectResult(status="already_collected")
    scorer = AnthropicScorer(client, batch_row["model"])
    if not scorer.ready(batch_id):
        logger.info("batch %s is still in progress", batch_id)
        return CollectResult(status="in_progress")
    logger.info("batch %s ended; collecting %s requests", batch_id, batch_row["request_count"])
    if batch_row["scoring_version"] != profile.scoring_version:
        logger.warning("batch %s was scored with an older profile scoring_version", batch_id)

    key = _Key(batch_row["model"], batch_row["prompt_version"], batch_row["scoring_version"])
    # A model missing from the price table (a batch submitted before unpriced models were
    # refused) is priced at the table's dearest rates, so the batch is still collected and its
    # reservation released instead of raising and staying stuck (bug df51dff).
    unpriced = list_price_per_job_usd(batch_row["model"], batch=True) is None
    if unpriced:
        logger.warning(
            "batch %s: no price for %s; its spend is recorded at the dearest listed rates",
            batch_id,
            batch_row["model"],
        )
    items: dict[str, int] = {}
    revs: dict[str, int] = {}
    for r in conn.execute(
        "SELECT custom_id, job_group_id, input_rev FROM score_batch_item WHERE batch_id = ?",
        (batch_id,),
    ):
        items[r["custom_id"]] = r["job_group_id"]
        revs[r["custom_id"]] = r["input_rev"]
    out = CollectResult()
    outcomes: dict[str, str] = {}
    cache_reads: list[int] = []
    tokens_in = tokens_out = 0

    with _txn(conn):
        for entry in scorer.collect(batch_id):
            custom_id = entry.custom_id
            group_id = items.get(custom_id)
            if group_id is None:
                out.unknown_ids.append(custom_id)
                continue
            kind = entry.status
            if kind != "succeeded":
                setattr(out, kind, getattr(out, kind) + 1)
                outcomes[custom_id] = kind
                if kind == "errored":
                    logger.warning("batch %s %s errored: %s", batch_id, custom_id, entry.detail)
                continue

            usage = entry.usage
            cost = ceiling_cost(usage, batch=True) if unpriced else scorer.cost(usage, batch=True)
            out.succeeded += 1
            out.cost_usd += cost
            tokens_in += _all_input_tokens(usage)
            tokens_out += _usage_int(usage, "output_tokens")
            cache_reads.append(_usage_int(usage, "cache_read_input_tokens"))

            job = _canonical_job(conn, group_id)
            try:
                screen = parse_result(entry)
            except ScreenError as exc:
                logger.warning("batch %s %s: %s", batch_id, custom_id, exc)
                out.invalid += 1
                outcomes[custom_id] = "invalid"
                if entry.stop_reason == "refusal":
                    # Billed in some categories; the daily run must not re-submit it forever.
                    outcomes[custom_id] = "refused"
                    record_failure(
                        conn, group_id, TIER, key, revs.get(custom_id, 0), "refusal", now
                    )
                continue
            if job is None:
                out.invalid += 1
                outcomes[custom_id] = "invalid"
                continue
            row_id, unverified = _write_fit_score(
                conn,
                group_id,
                screen,
                _row(job),
                usage,
                cost,
                key,
                batch_id,
                now,
                entry.model,
                revs.get(custom_id, 0),
            )
            outcomes[custom_id] = "succeeded"
            if row_id is None:
                out.duplicate += 1
            else:
                out.written += 1
                out.unverified += int(unverified)

        conn.executemany(
            "UPDATE score_batch_item SET result = ? WHERE batch_id = ? AND custom_id = ?",
            [(result, batch_id, cid) for cid, result in outcomes.items()],
        )
        _record_spend(conn, key.model, now, out.succeeded, tokens_in, tokens_out, out.cost_usd)
        conn.execute(
            "UPDATE score_batch SET collected_at = ?, counts = ? WHERE id = ?",
            (to_iso(now), json.dumps(out.as_dict()), batch_id),
        )

    out.cache_read_zero = check_cache_reads(cache_reads)
    if out.unknown_ids:
        logger.warning("batch %s returned unknown custom_ids: %s", batch_id, out.unknown_ids)
    logger.info(
        "batch %s collected: %d succeeded, %d written, %d duplicate, %d invalid, %d errored, "
        "%d canceled, %d expired, cost $%.4f",
        batch_id,
        out.succeeded,
        out.written,
        out.duplicate,
        out.invalid,
        out.errored,
        out.canceled,
        out.expired,
        out.cost_usd,
    )
    return out


def record_failure(
    conn: sqlite3.Connection,
    group_id: int,
    tier: str,
    key: _Key,
    input_rev: int,
    reason: str,
    now: datetime,
) -> None:
    """Note a paid attempt that wrote no score, so automatic runs do not pay for it again.

    The daily run (``new_only``) and the deep shortlist skip a group with a failure under the
    same model, prompt, ``scoring_version`` and description revision. Explicit requests still
    run it (bug 15f7012 review)."""
    conn.execute(
        "INSERT INTO score_failure (job_group_id, tier, model, prompt_version, scoring_version, "
        "input_rev, reason, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            group_id,
            tier,
            key.model,
            key.prompt_version,
            key.scoring_version,
            input_rev,
            reason,
            to_iso(now),
        ),
    )


def pending_batch_ids(conn: sqlite3.Connection) -> list[str]:
    """Submitted screen batches not yet collected, oldest first."""
    rows = conn.execute(
        "SELECT id FROM score_batch WHERE collected_at IS NULL ORDER BY submitted_at, id"
    )
    return [r["id"] for r in rows]


def collect_pending(
    conn: sqlite3.Connection, client: Any, profile: Profile, *, now: datetime
) -> dict[str, CollectResult]:
    """Collect every uncollected batch (``jobhunter score --collect-pending``).

    One failing batch (API error) is recorded as ``status="error: ..."`` and does not stop the
    others; it stays uncollected and is retried on the next call.
    """
    results: dict[str, CollectResult] = {}
    pending = pending_batch_ids(conn)
    logger.info("collecting %d pending screen batches", len(pending))
    for batch_id in pending:
        try:
            results[batch_id] = collect_batch(conn, client, batch_id, profile, now=now)
        except Exception as exc:  # one bad batch must not block the rest
            logger.warning("collect %s failed: %s", batch_id, exc)
            results[batch_id] = CollectResult(status=f"error: {exc}")
    return results


# ─── Synchronous, on-demand path ────────────────────────────────────────────


def score_one(
    conn: sqlite3.Connection,
    client: Any,
    profile: Profile,
    group_id: int,
    *,
    now: datetime,
    scorer: str = DEFAULT_SCORER,
) -> int | None:
    """Screen one group now via ``messages.parse`` (standard pricing).

    Returns the new fit_score id, or None when a score already exists for this key.
    Raises ``ScreenError`` when the output cannot be parsed.
    """
    job = _canonical_job(conn, group_id)
    if job is None:
        raise ValueError(f"job group {group_id} has no canonical job")
    return score_with(conn, AnthropicScorer(client, scorer), profile, group_id, now=now)


def score_with(
    conn: sqlite3.Connection,
    scorer: FitScorer,
    profile: Profile,
    group_id: int,
    *,
    now: datetime,
    rejection_days: int = rejections.DEFAULT_WINDOW_DAYS,
) -> int | None:
    """Screen one group now through any scorer's ``score_one`` (standard pricing).

    Raises ``ScreenError`` for a posting the candidate was rejected for: no credits spent.
    """
    job = _canonical_job(conn, group_id)
    if job is None:
        raise ValueError(f"job group {group_id} has no canonical job")
    if group_id in rejections.rejected_group_ids(conn):
        raise ScreenError(f"group {group_id}: the employer already rejected this posting")
    (row,) = _context_rows(conn, [{**dict(job), "group_id": group_id}], now, rejection_days)
    request = build_score_request(row, _summary_for(conn, job), profile, custom_id=f"g{group_id}")
    result = scorer.score_one(request)
    if result.status != "succeeded":
        raise ScreenError(f"{result.status}: {result.detail}")
    usage = result.usage
    cost = scorer.cost(usage, batch=False)
    scorer_name = scorer.name
    # Billed: record it, committed on its own, before parsing or writing (bug 6c33b40), so
    # unparsable output or a failing write still counts against the cap.
    with _txn(conn):
        _record_spend(
            conn,
            scorer_name,
            now,
            1,
            _all_input_tokens(usage),
            _usage_int(usage, "output_tokens"),
            cost,
        )
    screen = parse_result(result)
    key = _Key(scorer_name, PROMPT_VERSION, profile.scoring_version)
    rev = conn.execute(
        "SELECT description_rev FROM job_group WHERE id = ?", (group_id,)
    ).fetchone()[0]
    with _txn(conn):
        row_id, _ = _write_fit_score(
            conn, group_id, screen, _row(job), usage, cost, key, None, now, result.model, rev
        )
    return row_id


@dataclass
class SyncResult:
    submitted: int = 0
    written: int = 0
    duplicate: int = 0
    invalid: int = 0
    errored: int = 0
    unverified: int = 0
    cost_usd: float = 0.0
    requests: int = 0  # packed mode only: requests sent (omitted from as_dict otherwise)

    def as_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        if not self.requests:
            del d["requests"]
        return d


def score_sync(
    conn: sqlite3.Connection,
    scorer: FitScorer,
    profile: Profile,
    *,
    limit: int,
    now: datetime,
    remaining_usd: Callable[[], float] | None = None,
    rejection_days: int = rejections.DEFAULT_WINDOW_DAYS,
    group_ids: Sequence[int] | None = None,
    new_only: bool = False,
) -> SyncResult:
    """Screen eligible groups through a non-batching scorer and write fit_score rows now.

    Same eligibility, spend cap, evidence verification and write path as the batch route;
    rows have ``batch_id`` NULL. Errored and unparseable results write nothing, so those
    groups stay eligible for the next run.
    """
    if getattr(scorer, "evidence_mode", "quotes") == "none":
        # Decisions-model scorers (Jev) answer questions, not prompts: see decisions.py.
        from jobhunter.scoring.decisions import score_decisions

        return score_decisions(
            conn,
            scorer,
            profile,
            limit=limit,
            now=now,
            remaining_usd=remaining_usd,
            rejection_days=rejection_days,
            group_ids=group_ids,
            new_only=new_only,
        )
    out = SyncResult()
    if int(getattr(scorer, "jobs_per_request", 1) or 1) > 1:
        return _score_sync_packed(
            conn,
            scorer,
            profile,
            limit=limit,
            now=now,
            remaining_usd=remaining_usd,
            rejection_days=rejection_days,
            group_ids=group_ids,
            new_only=new_only,
        )
    price = scorer_list_price_usd(scorer)
    scoring = scoring_for(scorer)

    def per_job_now() -> float:
        # Re-read before every chunk: the chunks already sent are measured history now.
        reserve = reserve_per_job_usd(conn, scorer.name, scoring, price=price, profile=profile)
        if out.submitted:
            reserve = max(reserve, out.cost_usd / out.submitted)
        return reserve

    per_job = per_job_now()
    # Nothing known about this model's price: the first chunk is a single request, so its
    # real cost is learned before more is sent. The up-front cut is skipped then: the guess
    # would set the night's volume, and the per-chunk check enforces the cap at measured cost.
    guessed = price is None and measured_cost_per_job_usd(conn, scorer.name) <= 0
    if remaining_usd is not None:
        affordable = jobs_affordable(remaining_usd(), per_job)
        if guessed:
            affordable = limit if affordable > 0 else 0
        if affordable < limit:
            logger.warning(
                "spend cap: screening limited to %d groups at $%.4f a job", affordable, per_job
            )
        limit = min(limit, affordable)
    if limit <= 0:
        logger.info("sync scoring skipped: spend cap leaves room for 0 groups")
        return out
    groups = eligible_groups(
        conn, profile, scorer=scorer.name, limit=limit, group_ids=group_ids, new_only=new_only
    )
    if not groups:
        logger.info("sync scoring: no eligible groups")
        return out
    workers = max(1, int(getattr(getattr(scorer, "config", None), "max_concurrency", 1) or 1))
    chunk = max(SYNC_CHUNK, workers)
    key = _Key(scorer.name, PROMPT_VERSION, profile.scoring_version)
    label = logsetup.spec_label(scorer.name)
    logger.info("sync scoring %d groups with %s in chunks of %d", len(groups), label, chunk)
    progress = logsetup.Throttle(logger)
    t0 = time.monotonic()
    pos = 0
    reserved = 0.0  # what this run has reserved for the chunks it sent
    while pos < len(groups):
        size = 1 if guessed and pos == 0 else chunk
        part = groups[pos : pos + size]
        if remaining_usd is not None:
            # Recorded spend so far counts here, so a model dearer than its reservation
            # stops the run within one chunk of the cap. What was reserved for sent chunks
            # beyond what they recorded still counts: a model that reports no cost cannot
            # run past the cap at its reservation.
            per = per_job_now()
            room = remaining_usd() - max(0.0, reserved - out.cost_usd)
            fits = math.floor(max(room, 0.0) / per + 1e-9)
            if fits < len(part):
                logger.warning(
                    "spend cap: screening stopped with %d groups not sent",
                    len(groups) - pos - max(fits, 0),
                )
                part = part[: max(fits, 0)]
            if not part:
                break
            reserved += len(part) * per
        pos += len(part)
        _score_sync_chunk(conn, scorer, profile, part, key, out, now, rejection_days)
        progress.tick(
            "sync scoring %s: %d/%d groups sent, %d written, %d errored, cost $%.4f",
            label,
            pos,
            len(groups),
            out.written,
            out.errored,
            out.cost_usd,
        )
        if len(part) < size:
            break
    logger.info(
        "sync scoring %s done in %.1fs: %d sent, %d written, %d duplicate, %d invalid, "
        "%d errored, cost $%.4f",
        label,
        time.monotonic() - t0,
        out.submitted,
        out.written,
        out.duplicate,
        out.invalid,
        out.errored,
        out.cost_usd,
    )
    return out


def _score_sync_chunk(
    conn: sqlite3.Connection,
    scorer: FitScorer,
    profile: Profile,
    groups: list[sqlite3.Row],
    key: _Key,
    out: SyncResult,
    now: datetime,
    rejection_days: int,
) -> None:
    """Send one chunk of single-job requests and write what comes back, spend included."""
    by_id = {f"g{g['group_id']}": g for g in groups}
    results = scorer.submit(
        _score_requests(conn, groups, profile, now=now, rejection_days=rejection_days)
    )
    if isinstance(results, str):
        raise ValueError(f"scorer {scorer.name} batches; use submit_batch")
    out.submitted += len(groups)
    tokens_in = tokens_out = calls = 0
    chunk_cost = 0.0
    billed: list[tuple[Any, sqlite3.Row, float]] = []
    for res in results:
        g = by_id.get(res.custom_id)
        if g is None:
            continue
        if res.status != "succeeded":
            out.errored += 1
            logger.warning("%s %s: %s", scorer.name, res.custom_id, res.detail)
            continue
        cost = scorer.cost(res.usage, batch=False)
        chunk_cost += cost
        calls += 1
        tokens_in += _all_input_tokens(res.usage)
        tokens_out += _usage_int(res.usage, "output_tokens")
        billed.append((res, g, cost))
    # The chunk's billed calls are recorded, committed on their own, before any row is
    # written (bug 6c33b40): a failing write must not roll the spend back.
    with _txn(conn):
        _record_spend(conn, key.model, now, calls, tokens_in, tokens_out, chunk_cost)
    out.cost_usd += chunk_cost
    with _txn(conn):
        for res, g, cost in billed:
            try:
                screen = parse_result(res)
            except ScreenError as exc:
                logger.warning("%s %s: %s", scorer.name, res.custom_id, exc)
                out.invalid += 1
                continue
            row_id, unverified = _write_fit_score(
                conn,
                g["group_id"],
                screen,
                _row(g),
                res.usage,
                cost,
                key,
                None,
                now,
                res.model,
                g["input_rev"],
            )
            if row_id is None:
                out.duplicate += 1
            else:
                out.written += 1
                out.unverified += int(unverified)


# ─── Packed requests (specs/016 "Packed requests") ──────────────────────────

MAX_JOBS_PER_REQUEST = 16
CHARS_PER_TOKEN = 4  # rough input-size estimate for the pack cap


def packed_instruction(ids: Sequence[str]) -> str:
    return (
        f"# Packed request: {len(ids)} job postings\n\n"
        f"Below are {len(ids)} job postings, each between a BEGIN POSTING <id> line and an "
        'END POSTING <id> line. Return one JSON object {"judgments": [...]} holding exactly '
        "one judgment per posting, in any order. Each judgment is the usual screen output plus "
        "a `custom_id` field set to that posting's <id>, copied exactly. Judge each posting "
        "independently, using only that posting's own text: nothing from another posting may "
        "inform a score, a flag or a quote, and every evidence quote must be copied verbatim "
        "from the posting it is attached to. Do not merge, skip or repeat postings.\n"
        f"Ids: {', '.join(ids)}\n"
    )


def packed_posting_text(items: Sequence[tuple[str, str]]) -> str:
    """The single user message: an instruction, then each posting tagged with its custom_id."""
    parts = [packed_instruction([cid for cid, _ in items])]
    for cid, text in items:
        parts.append(
            f"\n===== BEGIN POSTING {cid} =====\n{text.rstrip()}\n===== END POSTING {cid} =====\n"
        )
    return "".join(parts)


def pack_id(ids: Sequence[str]) -> str:
    return "pack:" + "+".join(ids)


def build_packed_request(
    items: Sequence[tuple[str, str]],
    profile: Profile,
    *,
    max_tokens: int = MAX_TOKENS,
) -> ScoreRequest:
    """One request for several postings. ``items`` is ``[(custom_id, posting_text), ...]``.

    ``system`` and ``profile`` are exactly the single-mode text, so the cached prefix is the
    same; only the user message and the response schema differ.
    """
    ids = [cid for cid, _ in items]
    return ScoreRequest(
        custom_id=pack_id(ids),
        system=RUBRIC_TEXT,
        profile=scoring_inputs(profile),
        posting=packed_posting_text(items),
        schema=packed_json_schema(len(items)),
        max_tokens=max_tokens * len(items),
    )


def estimated_input_tokens(request: ScoreRequest) -> int:
    return (len(request.system) + len(request.profile) + len(request.posting)) // CHARS_PER_TOKEN


def pack_items(
    texts: Sequence[tuple[str, str]],
    profile: Profile,
    *,
    jobs_per_request: int,
    max_input_tokens: int,
) -> list[list[tuple[str, str]]]:
    """Greedy FIFO packs of at most ``jobs_per_request`` postings whose estimated input
    tokens stay under ``max_input_tokens``. A posting too big for any pack goes alone."""
    jobs_per_request = max(1, min(jobs_per_request, MAX_JOBS_PER_REQUEST))
    fixed = (len(RUBRIC_TEXT) + len(scoring_inputs(profile)) + 1500) // CHARS_PER_TOKEN
    packs: list[list[tuple[str, str]]] = []
    current: list[tuple[str, str]] = []
    used = fixed
    for cid, text in texts:
        cost = (len(text) + 100) // CHARS_PER_TOKEN
        if current and (len(current) >= jobs_per_request or used + cost > max_input_tokens):
            packs.append(current)
            current, used = [], fixed
        current.append((cid, text))
        used += cost
    if current:
        packs.append(current)
    return packs


@dataclass
class PackedParse:
    screens: dict[str, Screen] = field(default_factory=dict)
    problems: dict[str, str] = field(default_factory=dict)  # custom_id -> why it is not usable
    unknown: list[str] = field(default_factory=list)  # ids in the reply we never sent


def parse_packed(result: ScoreResult, ids: Sequence[str]) -> PackedParse:
    """Map a packed reply's judgments to jobs by ``custom_id``; validate each independently.

    A missing, duplicated or invalid item, or an id we did not send, leaves just that job
    without a screen (it stays eligible); the other items are kept. A reply that is unusable
    as a whole marks every id as a problem.
    """
    out = PackedParse()
    wanted = list(ids)

    def fail_all(why: str) -> PackedParse:
        out.problems = {cid: why for cid in wanted}
        return out

    if result.stop_reason in ("max_tokens", "refusal"):
        return fail_all(f"stop_reason {result.stop_reason}")
    if result.text is None:
        return fail_all("response has no text block")
    try:
        data = json.loads(result.text)
        items = data["judgments"]
        if not isinstance(items, list):
            raise TypeError("judgments is not a list")
    except (ValueError, KeyError, TypeError) as exc:
        return fail_all(f"output is not a judgments object: {exc}")
    counts: dict[str, int] = {}
    for item in items:
        cid = item.get("custom_id") if isinstance(item, dict) else None
        if isinstance(cid, str):
            counts[cid] = counts.get(cid, 0) + 1
    valid: dict[str, Screen] = {}
    for item in items:
        cid = item.get("custom_id") if isinstance(item, dict) else None
        if not isinstance(cid, str) or cid not in wanted:
            out.unknown.append(str(cid))
            continue
        if counts[cid] > 1:
            continue
        body = {k: v for k, v in item.items() if k != "custom_id"}
        try:
            valid[cid] = Screen.model_validate(body)
        except ValidationError as exc:
            out.problems[cid] = f"output does not match Screen: {exc.error_count()} errors"
    for cid in wanted:
        if cid in valid:
            out.screens[cid] = valid[cid]
        elif cid not in out.problems:
            dup = counts.get(cid, 0)
            out.problems[cid] = f"duplicated in reply ({dup}x)" if dup > 1 else "missing from reply"
    return out


def estimated_request_cost(
    conn: sqlite3.Connection, scorer: Any, now: datetime, n_jobs: int
) -> float:
    """Spend-cap estimate for one packed request.

    A scorer with a configured ``est_cost_per_request_usd`` (OpenRouter: $0.04) uses the mean
    cost per request measured in ``llm_spend`` (today, else all history), else that default.
    Any other scorer keeps the batch-era ``ESTIMATED_COST_PER_REQUEST_USD`` per job. Either
    way the estimate is never below the model's list price for a pack of ``n_jobs``
    (``chat_price_usd`` at its cached catalog or configured rates, bug df51dff), so a dear
    model is not sent on a cheap estimate; a model with no known price keeps the configured
    per-request estimate.
    """
    unpriced = (
        unpriced_reserve_usd(scorer.name, scoring_for(scorer), batch=False)
        if isinstance(scorer, OpenRouterScorer)
        else 0.0
    )
    return pack_request_estimate(
        conn,
        scorer.name,
        now,
        n_jobs,
        rates=_chat_rates(scorer),
        default=float(getattr(scorer, "est_cost_per_request_usd", 0.0) or 0.0),
        unpriced_per_job=unpriced,
    )


def pack_request_estimate(
    conn: sqlite3.Connection,
    name: str,
    now: datetime,
    n_jobs: int,
    *,
    rates: tuple[float, float] | None,
    default: float,
    unpriced_per_job: float = 0.0,
) -> float:
    """``estimated_request_cost`` from its inputs, so a page or a plan can show the figure a
    packed run checks the cap with (020 M5b review N2) without building a scorer."""
    floor = 0.0
    if rates is not None:
        floor = chat_price_usd(rates, n_jobs)
    elif (measured := measured_cost_per_job_usd(conn, name)) > 0:
        floor = measured * n_jobs  # no list price: the measured cost per job, per pack
    elif unpriced_per_job > 0:
        # Neither a price nor history: the unpriced guess per job, as on the single path.
        floor = unpriced_per_job * n_jobs
    if default <= 0:
        return max(ESTIMATED_COST_PER_REQUEST_USD * n_jobs, floor)
    for day_clause, args in (("AND day = ?", (to_iso(now)[:10],)), ("", ())):
        row = conn.execute(
            "SELECT sum(cost_usd), sum(calls) FROM llm_spend "
            f"WHERE model = ? AND tier = ? AND calls > 0 {day_clause}",
            (name, TIER, *args),
        ).fetchone()
        if row[0] and row[1] and row[0] > 0:
            return max(float(row[0]) / float(row[1]), floor)
    return max(default, floor)


def packed_hold_per_job_usd(
    conn: sqlite3.Connection,
    spec: str,
    scoring: Scoring,
    now: datetime,
    n_jobs: int | None = None,
) -> float:
    """Per job, what a packed OpenRouter or openai-compat run holds for a pack of ``n_jobs``
    (default a full pack, ``jobs_per_request``): ``estimated_request_cost`` / ``n_jobs``,
    from the cached catalog or the configured prices. Reads only; never fetches."""
    provider, _, name = spec.partition(":")
    if provider == "openrouter":
        cfg: Any = scoring.openrouter
        rates = cached_openrouter_rates(cfg, name)
        unpriced = unpriced_reserve_usd(spec, scoring, batch=False)
    else:
        cfg = scoring.openai_compat
        rates = (cfg.input_usd_per_mtok, cfg.output_usd_per_mtok)
        unpriced = 0.0
    n = max(int(n_jobs or cfg.jobs_per_request), 1)
    est = pack_request_estimate(
        conn,
        spec,
        now,
        n,
        rates=rates,
        default=float(cfg.est_cost_per_request_usd or 0.0),
        unpriced_per_job=unpriced,
    )
    return est / n


def _chat_rates(scorer: Any) -> tuple[float, float] | None:
    """A chat scorer's (input, output) USD per Mtok, or None. Called only while scoring."""
    if isinstance(scorer, OpenRouterScorer):
        return scorer.catalog_rates()  # a scoring path: refreshed at most daily
    if isinstance(scorer, OpenAICompatScorer) and not isinstance(scorer, LocalScorer):
        return (scorer.config.input_usd_per_mtok, scorer.config.output_usd_per_mtok)
    return None


def _share(usage: Any, n: int) -> Usage:
    """One job's slice of a packed request's token counts."""
    return Usage(
        input_tokens=_usage_int(usage, "input_tokens") // n,
        output_tokens=_usage_int(usage, "output_tokens") // n,
        cache_creation_input_tokens=_usage_int(usage, "cache_creation_input_tokens") // n,
        cache_read_input_tokens=_usage_int(usage, "cache_read_input_tokens") // n,
    )


def _score_sync_packed(
    conn: sqlite3.Connection,
    scorer: FitScorer,
    profile: Profile,
    *,
    limit: int,
    now: datetime,
    remaining_usd: Callable[[], float] | None,
    rejection_days: int = rejections.DEFAULT_WINDOW_DAYS,
    group_ids: Sequence[int] | None = None,
    new_only: bool = False,
) -> SyncResult:
    """``score_sync`` with several jobs per request.

    Eligible groups are ordered by group id (FIFO) and cut into packs. Packs go out in waves
    of ``max_concurrency``; before each wave the spend cap must cover the estimated cost of
    every request in it. Each request's total cost is split evenly over its jobs for the
    ``fit_score`` rows; ``llm_spend`` gets the true total with one call per request.
    """
    out = SyncResult()
    groups = sorted(
        eligible_groups(
            conn,
            profile,
            scorer=scorer.name,
            limit=max(limit, 0),
            group_ids=group_ids,
            new_only=new_only,
        ),
        key=lambda g: g["group_id"],
    )
    if not groups:
        logger.info("packed scoring: no eligible groups")
        return out
    by_id = {f"g{g['group_id']}": g for g in groups}
    texts = [
        (f"g{row['group_id']}", posting_text(row, _summary_for(conn, row)))
        for row in _context_rows(conn, groups, now, rejection_days)
    ]
    packs = pack_items(
        texts,
        profile,
        jobs_per_request=int(getattr(scorer, "jobs_per_request", 1)),
        max_input_tokens=int(getattr(scorer, "max_input_tokens", 40_000)),
    )
    workers = max(1, int(getattr(getattr(scorer, "config", None), "max_concurrency", 1)))
    key = _Key(scorer.name, PROMPT_VERSION, profile.scoring_version)
    label = logsetup.spec_label(scorer.name)
    logger.info(
        "packed scoring %d groups in %d requests with %s (%d at a time)",
        len(groups),
        len(packs),
        label,
        workers,
    )
    progress = logsetup.Throttle(logger)
    t0 = time.monotonic()
    pos = 0
    # Nothing known about the price: one pack goes alone first, so its real cost is measured
    # before a full wave is sent.
    guessed = (
        isinstance(scorer, OpenRouterScorer)
        and _chat_rates(scorer) is None
        and measured_cost_per_job_usd(conn, scorer.name) <= 0
    )
    while pos < len(packs):
        wave = packs[pos : pos + (1 if guessed and pos == 0 else workers)]
        if remaining_usd is not None:
            remaining = max(remaining_usd(), 0.0)
            affordable = 0
            for pack in wave:
                est = estimated_request_cost(conn, scorer, now, len(pack))
                if jobs_affordable(remaining, est) < 1:  # one comparison everywhere (N6)
                    break
                remaining -= est
                affordable += 1
            if affordable < len(wave):
                logger.warning(
                    "spend cap: screening stopped before %d packed requests",
                    len(packs) - pos - affordable,
                )
                wave = wave[:affordable]
            if not wave:
                break
        pos += len(wave)
        requests = [build_packed_request(pack, profile) for pack in wave]
        results = scorer.submit(requests)
        if isinstance(results, str):
            raise ValueError(f"scorer {scorer.name} batches; use submit_batch")
        by_pack = {r.custom_id: r for r in results}
        out.requests += len(wave)
        billed: list[tuple[list[str], Any, float]] = []
        for pack, request in zip(wave, requests, strict=True):
            ids = [cid for cid, _ in pack]
            out.submitted += len(ids)
            res = by_pack.get(request.custom_id)
            if res is None or res.status != "succeeded":
                out.errored += len(ids)
                detail = res.detail if res else "no result"
                logger.warning("%s %s: %s", scorer.name, request.custom_id, detail)
                continue
            billed.append((ids, res, scorer.cost(res.usage, batch=False)))
        # Every billed request is recorded, committed on its own, before any row is written
        # (bug 6c33b40): a failing write or a bad pack must not roll the spend back.
        with _txn(conn):
            for _ids, res, cost in billed:
                _record_spend(
                    conn,
                    key.model,
                    now,
                    1,
                    _all_input_tokens(res.usage),
                    _usage_int(res.usage, "output_tokens"),
                    cost,
                )
        out.cost_usd += sum(cost for _ids, _res, cost in billed)
        with _txn(conn):
            for ids, res, cost in billed:
                parsed = parse_packed(res, ids)
                for cid in parsed.unknown:
                    logger.warning("%s %s: unknown id %r", scorer.name, res.custom_id, cid)
                share = _share(res.usage, len(ids))
                for cid in ids:
                    g = by_id[cid]
                    judged = parsed.screens.get(cid)
                    if judged is None:
                        out.invalid += 1
                        logger.warning("%s %s: %s", scorer.name, cid, parsed.problems[cid])
                        continue
                    row_id, unverified = _write_fit_score(
                        conn,
                        g["group_id"],
                        judged,
                        _row(g),
                        share,
                        cost / len(ids),
                        key,
                        None,
                        now,
                        res.model,
                        g["input_rev"],
                    )
                    if row_id is None:
                        out.duplicate += 1
                    else:
                        out.written += 1
                        out.unverified += int(unverified)
        progress.tick(
            "packed scoring %s: %d/%d requests done, %d written, %d errored, cost $%.4f",
            label,
            pos,
            len(packs),
            out.written,
            out.errored,
            out.cost_usd,
        )
    logger.info(
        "packed scoring %s done in %.1fs: %d requests, %d written, %d duplicate, %d invalid, "
        "%d errored, cost $%.4f",
        label,
        time.monotonic() - t0,
        out.requests,
        out.written,
        out.duplicate,
        out.invalid,
        out.errored,
        out.cost_usd,
    )
    return out
