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
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pydantic import ValidationError

from jobhunter.core.models import LocationScope, Screen
from jobhunter.pipeline.listing import _txn, to_iso
from jobhunter.pipeline.locations import load_locations, location_summary
from jobhunter.scoring.profile import Profile, scoring_inputs
from jobhunter.scoring.rubric import PROMPT_VERSION, RUBRIC_TEXT, SCREEN_SCHEMA

logger = logging.getLogger(__name__)

TIER = "screen"
DEFAULT_SCORER = "anthropic:claude-haiku-4-5"
MAX_TOKENS = 2048
# Planning estimate per request for spend caps (specs/006 "Cost": about $0.002 batched).
ESTIMATED_COST_PER_REQUEST_USD = 0.002
# USD per million tokens, standard (non-batch) rates: (input, output).
PRICING_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-opus-5": (5.00, 25.00),
}
BATCH_DISCOUNT = 0.5
CACHE_READ_MULTIPLIER = 0.1
CACHE_WRITE_MULTIPLIER = 1.25  # 5-minute ephemeral cache writes
_CUSTOM_ID = re.compile(r"^g(\d+)$")


class ScreenError(Exception):
    """A screen result could not be turned into a Screen."""


# ─── Model ids and cost ─────────────────────────────────────────────────────


def model_id(scorer: str) -> str:
    """``"anthropic:claude-haiku-4-5"`` -> ``"claude-haiku-4-5"``. Other providers raise."""
    provider, sep, name = scorer.partition(":")
    if not sep:
        return scorer
    if provider != "anthropic":
        raise ValueError(f"scorer {scorer!r} is not an Anthropic model")
    return name


def _usage_int(usage: Any, name: str) -> int:
    return int(getattr(usage, name, None) or 0)


def compute_cost(usage: Any, model: str, *, batch: bool) -> float:
    """USD for one response's ``usage``: uncached input, cache writes, cache reads, output."""
    try:
        in_rate, out_rate = PRICING_PER_MTOK[model_id(model)]
    except KeyError as exc:
        raise ValueError(f"no pricing for model {model!r}") from exc
    factor = BATCH_DISCOUNT if batch else 1.0
    cost = (
        _usage_int(usage, "input_tokens") * in_rate
        + _usage_int(usage, "cache_creation_input_tokens") * in_rate * CACHE_WRITE_MULTIPLIER
        + _usage_int(usage, "cache_read_input_tokens") * in_rate * CACHE_READ_MULTIPLIER
        + _usage_int(usage, "output_tokens") * out_rate
    )
    return cost * factor / 1_000_000


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
    if job.get("description_completeness") == "partial":
        lines.append(
            "Note: the description below is partial (a summary or truncated listing); "
            "the full posting may say more."
        )
    lines += ["", "## Description", "", description]
    return "\n".join(lines) + "\n"


def build_request(
    job: Mapping[str, Any],
    locations_summary: str,
    profile: Profile,
    *,
    scorer: str = DEFAULT_SCORER,
    max_tokens: int = MAX_TOKENS,
) -> dict[str, Any]:
    """Messages API params for one screen. No ``thinking`` and no ``output_config.effort``.

    Haiku 4.5 rejects ``effort``, and a bounded classification runs with thinking off.
    """
    return {
        "model": model_id(scorer),
        "max_tokens": max_tokens,
        "system": [
            {"type": "text", "text": RUBRIC_TEXT},
            {
                "type": "text",
                "text": scoring_inputs(profile),
                "cache_control": {"type": "ephemeral"},
            },
        ],
        "messages": [{"role": "user", "content": posting_text(job, locations_summary)}],
        "output_config": {"format": {"type": "json_schema", "schema": SCREEN_SCHEMA}},
    }


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
        "verdict, overall, dimensions, evidence, blockers, missing_info, tailoring_hints, "
        "shape_flags, evidence_unverified, input_tokens, output_tokens, cache_read_tokens, "
        "cost_usd, batch_id, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (job_group_id, tier, prompt_version, scoring_version, model) DO NOTHING",
        (
            group_id,
            TIER,
            key.model,
            key.prompt_version,
            key.scoring_version,
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
        (to_iso(now)[:10], model, TIER, calls, input_tokens, output_tokens, cost),
    )


def _all_input_tokens(usage: Any) -> int:
    return (
        _usage_int(usage, "input_tokens")
        + _usage_int(usage, "cache_creation_input_tokens")
        + _usage_int(usage, "cache_read_input_tokens")
    )


# ─── Selection and spend ────────────────────────────────────────────────────

_ELIGIBLE = """
SELECT g.id AS group_id, j.*
FROM job_group g
JOIN job j ON j.id = g.canonical_job_id
JOIN prefilter_result p ON p.job_id = j.id AND p.passed = 1 AND p.filter_version = :filter_version
WHERE NOT EXISTS (
    SELECT 1 FROM fit_score f
    WHERE f.job_group_id = g.id AND f.tier = :tier AND f.model = :model
      AND f.prompt_version = :prompt_version AND f.scoring_version = :scoring_version)
  AND NOT EXISTS (
    SELECT 1 FROM score_batch_item i JOIN score_batch b ON b.id = i.batch_id
    WHERE i.job_group_id = g.id AND b.collected_at IS NULL AND b.tier = :tier
      AND b.model = :model AND b.prompt_version = :prompt_version
      AND b.scoring_version = :scoring_version)
ORDER BY j.posted_at IS NULL, j.posted_at DESC, g.id
LIMIT :limit
"""


def eligible_groups(
    conn: sqlite3.Connection, profile: Profile, *, scorer: str, limit: int
) -> list[sqlite3.Row]:
    """Groups whose canonical job passed the current prefilter and have no screen yet.

    A job with no prefilter_result row is not yet eligible. Groups already in an uncollected
    batch for the same key are skipped so they are never submitted twice.
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
        },
    ).fetchall()


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
    return location_summary(load_locations(conn, job["id"]), job["location_scope"])


def _row(job: sqlite3.Row) -> dict[str, Any]:
    return dict(job)


# ─── Batch submit / collect ─────────────────────────────────────────────────


def submit_batch(
    conn: sqlite3.Connection,
    client: Any,
    profile: Profile,
    *,
    limit: int,
    now: datetime,
    scorer: str = DEFAULT_SCORER,
    remaining_usd: Callable[[], float] | None = None,
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
    groups = eligible_groups(conn, profile, scorer=scorer, limit=limit)
    if not groups:
        return None

    requests = [
        {
            "custom_id": f"g{g['group_id']}",
            "params": build_request(_row(g), _summary_for(conn, g), profile, scorer=scorer),
        }
        for g in groups
    ]
    batch = client.messages.batches.create(requests=requests)
    with _txn(conn):
        conn.execute(
            "INSERT INTO score_batch (id, tier, model, prompt_version, scoring_version, "
            "request_count, submitted_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                batch.id,
                TIER,
                scorer,
                PROMPT_VERSION,
                profile.scoring_version,
                len(requests),
                to_iso(now),
            ),
        )
        conn.executemany(
            "INSERT INTO score_batch_item (batch_id, custom_id, job_group_id) VALUES (?, ?, ?)",
            [(batch.id, f"g{g['group_id']}", g["group_id"]) for g in groups],
        )
    logger.info("submitted screen batch %s with %d requests", batch.id, len(requests))
    return batch.id


@dataclass
class CollectResult:
    status: str = "ended"  # ended | in_progress | already_collected
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
    if client.messages.batches.retrieve(batch_id).processing_status != "ended":
        return CollectResult(status="in_progress")
    if batch_row["scoring_version"] != profile.scoring_version:
        logger.warning("batch %s was scored with an older profile scoring_version", batch_id)

    key = _Key(batch_row["model"], batch_row["prompt_version"], batch_row["scoring_version"])
    items = {
        r["custom_id"]: r["job_group_id"]
        for r in conn.execute(
            "SELECT custom_id, job_group_id FROM score_batch_item WHERE batch_id = ?",
            (batch_id,),
        )
    }
    out = CollectResult()
    outcomes: dict[str, str] = {}
    cache_reads: list[int] = []
    tokens_in = tokens_out = 0

    with _txn(conn):
        for entry in client.messages.batches.results(batch_id):
            custom_id = entry.custom_id
            group_id = items.get(custom_id)
            if group_id is None:
                out.unknown_ids.append(custom_id)
                continue
            kind = entry.result.type
            if kind != "succeeded":
                setattr(out, kind, getattr(out, kind) + 1)
                outcomes[custom_id] = kind
                if kind == "errored":
                    logger.warning("batch %s %s errored: %s", batch_id, custom_id, entry.result)
                continue

            message = entry.result.message
            usage = message.usage
            cost = compute_cost(usage, key.model, batch=True)
            out.succeeded += 1
            out.cost_usd += cost
            tokens_in += _all_input_tokens(usage)
            tokens_out += _usage_int(usage, "output_tokens")
            cache_reads.append(_usage_int(usage, "cache_read_input_tokens"))

            job = _canonical_job(conn, group_id)
            try:
                screen = parse_message(message)
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
                conn, group_id, screen, _row(job), usage, cost, key, batch_id, now
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
    params = build_request(_row(job), _summary_for(conn, job), profile, scorer=scorer)
    message = client.messages.parse(**params)
    screen = parse_message(message)
    usage = message.usage
    cost = compute_cost(usage, scorer, batch=False)
    key = _Key(scorer, PROMPT_VERSION, profile.scoring_version)
    with _txn(conn):
        row_id, _ = _write_fit_score(conn, group_id, screen, _row(job), usage, cost, key, None, now)
        _record_spend(
            conn, scorer, now, 1, _all_input_tokens(usage), _usage_int(usage, "output_tokens"), cost
        )
    return row_id
