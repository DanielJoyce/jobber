"""Stage 2 screen: Haiku 4.5 over the Batch API (specs/006 "Stage 2 — the screen").

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
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pydantic import ValidationError

from jobhunter.core import rejections
from jobhunter.core.models import LocationScope, Screen
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
    ScoreRequest,
    ScoreResult,
    Usage,
    _usage_int,
    anthropic_params,
    compute_cost,
    model_id,
)

logger = logging.getLogger(__name__)

TIER = "screen"
DEFAULT_SCORER = "anthropic:claude-haiku-4-5"
MAX_TOKENS = 2048
# Planning estimate per request for spend caps (specs/006 "Cost": about $0.002 batched).
ESTIMATED_COST_PER_REQUEST_USD = 0.002
_CUSTOM_ID = re.compile(r"^g(\d+)$")


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
  AND (NOT :new_only OR (
    NOT EXISTS (
      SELECT 1 FROM fit_score f
      WHERE f.job_group_id = g.id AND f.tier = :tier AND f.input_rev = g.description_rev)
    AND NOT EXISTS (
      SELECT 1 FROM score_batch_item i JOIN score_batch b ON b.id = i.batch_id
      WHERE i.job_group_id = g.id AND b.collected_at IS NULL AND b.tier = :tier)))
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
    current description revision (under any model, prompt or ``scoring_version``) and none in
    an uncollected batch. Re-scoring an already scored group costs money, so it happens only
    through a confirmed re-score, never automatically (specs/006, specs/014).
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


def remaining_daily_budget(conn: sqlite3.Connection, cap_usd: float, now: datetime) -> float:
    """``cap_usd`` minus today's recorded spend and an estimate for uncollected batches."""
    spent = conn.execute(
        "SELECT coalesce(sum(cost_usd), 0) FROM llm_spend WHERE day = ?", (to_iso(now)[:10],)
    ).fetchone()[0]
    pending = conn.execute(
        "SELECT coalesce(sum(request_count), 0) FROM score_batch WHERE collected_at IS NULL"
    ).fetchone()[0]
    return cap_usd - spent - pending * ESTIMATED_COST_PER_REQUEST_USD


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
    at ``ESTIMATED_COST_PER_REQUEST_USD``; nothing is submitted when that is zero.
    """
    if remaining_usd is not None:
        affordable = math.floor(max(remaining_usd(), 0.0) / ESTIMATED_COST_PER_REQUEST_USD)
        if affordable < limit:
            logger.warning("spend cap: screening limited to %d groups", affordable)
        limit = min(limit, affordable)
    if limit <= 0:
        return None
    groups = eligible_groups(
        conn, profile, scorer=scorer, limit=limit, group_ids=group_ids, new_only=new_only
    )
    if not groups:
        return None

    requests = _score_requests(conn, groups, profile, now=now, rejection_days=rejection_days)
    batch_id = AnthropicScorer(client, scorer).submit(requests)
    with _txn(conn):
        conn.execute(
            "INSERT INTO score_batch (id, tier, model, prompt_version, scoring_version, "
            "request_count, submitted_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                batch_id,
                TIER,
                scorer,
                PROMPT_VERSION,
                profile.scoring_version,
                len(requests),
                to_iso(now),
            ),
        )
        conn.executemany(
            "INSERT INTO score_batch_item (batch_id, custom_id, job_group_id, input_rev) "
            "VALUES (?, ?, ?, ?)",
            [(batch_id, f"g{g['group_id']}", g["group_id"], g["input_rev"]) for g in groups],
        )
    logger.info("submitted screen batch %s with %d requests", batch_id, len(requests))
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
        return CollectResult(status="in_progress")
    if batch_row["scoring_version"] != profile.scoring_version:
        logger.warning("batch %s was scored with an older profile scoring_version", batch_id)

    key = _Key(batch_row["model"], batch_row["prompt_version"], batch_row["scoring_version"])
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
            cost = scorer.cost(usage, batch=True)
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
    return out


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
    for batch_id in pending_batch_ids(conn):
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
    screen = parse_result(result)
    usage = result.usage
    cost = scorer.cost(usage, batch=False)
    scorer_name = scorer.name
    key = _Key(scorer_name, PROMPT_VERSION, profile.scoring_version)
    rev = conn.execute(
        "SELECT description_rev FROM job_group WHERE id = ?", (group_id,)
    ).fetchone()[0]
    with _txn(conn):
        row_id, _ = _write_fit_score(
            conn, group_id, screen, _row(job), usage, cost, key, None, now, result.model, rev
        )
        _record_spend(
            conn,
            scorer_name,
            now,
            1,
            _all_input_tokens(usage),
            _usage_int(usage, "output_tokens"),
            cost,
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
    if remaining_usd is not None:
        affordable = math.floor(max(remaining_usd(), 0.0) / ESTIMATED_COST_PER_REQUEST_USD)
        if affordable < limit:
            logger.warning("spend cap: screening limited to %d groups", affordable)
        limit = min(limit, affordable)
    if limit <= 0:
        return out
    groups = eligible_groups(
        conn, profile, scorer=scorer.name, limit=limit, group_ids=group_ids, new_only=new_only
    )
    if not groups:
        return out
    by_id = {f"g{g['group_id']}": g for g in groups}
    results = scorer.submit(
        _score_requests(conn, groups, profile, now=now, rejection_days=rejection_days)
    )
    if isinstance(results, str):
        raise ValueError(f"scorer {scorer.name} batches; use submit_batch")
    out.submitted = len(groups)
    key = _Key(scorer.name, PROMPT_VERSION, profile.scoring_version)
    tokens_in = tokens_out = calls = 0
    with _txn(conn):
        for res in results:
            g = by_id.get(res.custom_id)
            if g is None:
                continue
            if res.status != "succeeded":
                out.errored += 1
                logger.warning("%s %s: %s", scorer.name, res.custom_id, res.detail)
                continue
            cost = scorer.cost(res.usage, batch=False)
            out.cost_usd += cost
            calls += 1
            tokens_in += _all_input_tokens(res.usage)
            tokens_out += _usage_int(res.usage, "output_tokens")
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
        _record_spend(conn, key.model, now, calls, tokens_in, tokens_out, out.cost_usd)
    return out


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
    Any other scorer keeps the batch-era ``ESTIMATED_COST_PER_REQUEST_USD`` per job.
    """
    default = float(getattr(scorer, "est_cost_per_request_usd", 0.0) or 0.0)
    if default <= 0:
        return ESTIMATED_COST_PER_REQUEST_USD * n_jobs
    for day_clause, args in (("AND day = ?", (to_iso(now)[:10],)), ("", ())):
        row = conn.execute(
            "SELECT sum(cost_usd), sum(calls) FROM llm_spend "
            f"WHERE model = ? AND tier = ? AND calls > 0 {day_clause}",
            (scorer.name, TIER, *args),
        ).fetchone()
        if row[0] and row[1] and row[0] > 0:
            return float(row[0]) / float(row[1])
    return default


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
    pos = 0
    while pos < len(packs):
        wave = packs[pos : pos + workers]
        if remaining_usd is not None:
            remaining = max(remaining_usd(), 0.0)
            affordable = 0
            for pack in wave:
                est = estimated_request_cost(conn, scorer, now, len(pack))
                if remaining < est:
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
        with _txn(conn):
            for pack, request in zip(wave, requests, strict=True):
                ids = [cid for cid, _ in pack]
                out.submitted += len(ids)
                res = by_pack.get(request.custom_id)
                if res is None or res.status != "succeeded":
                    out.errored += len(ids)
                    detail = res.detail if res else "no result"
                    logger.warning("%s %s: %s", scorer.name, request.custom_id, detail)
                    continue
                cost = scorer.cost(res.usage, batch=False)
                out.cost_usd += cost
                parsed = parse_packed(res, ids)
                for cid in parsed.unknown:
                    logger.warning("%s %s: unknown id %r", scorer.name, request.custom_id, cid)
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
                _record_spend(
                    conn,
                    key.model,
                    now,
                    1,
                    _all_input_tokens(res.usage),
                    _usage_int(res.usage, "output_tokens"),
                    cost,
                )
    return out
