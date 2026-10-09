"""Computed dimensions, overall, buckets and verdicts (specs/006, specs/014).

Everything here is pure Python, free to recompute and never calls a model. The model supplies
skills / seniority / domain; ``comp`` and ``location`` are computed from facts and the current
filter settings; ``overall`` and the bucket are derived on read, so editing a salary floor or
state ranking re-sorts stored scores at no cost.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from jobhunter.core.models import Bucket, JobLocation, LocationScope, Verdict
from jobhunter.core.textnorm import annualize
from jobhunter.pipeline.locations import load_locations
from jobhunter.scoring.profile import Profile

REMOTE_BASE = 70
NEGOTIABLE_BASE = 60
NO_REMOTE_FALLBACK = 30
UNRANKED_STATE = 50
RANK_TOP = 100
RANK_STEP = 10
RANK_FLOOR = 60
UNKNOWN_LOCATION = 50
_TARGET_FLOOR_RATIO = 1.2

DEFAULT_THRESHOLDS: dict[str, float] = {
    "g_skills": 35,
    "g_domain": 25,
    "f_recency": 55,
    "f_raw": 70,
    "e_comp": 45,
    "c_skills": 65,
    "d_skills": 60,
    "d_domain": 55,
    "b_overall": 62,
    "a_overall": 80,
    "a_recency": 80,
    "verdict_strong": 78,
    "verdict_possible": 60,
    "verdict_weak": 40,
    "fallback_mismatch_overall": 40,
}


def thresholds_for(profile: Profile) -> dict[str, float]:
    """Defaults overlaid with ``profile.buckets`` overrides (unknown keys are ignored)."""
    merged = dict(DEFAULT_THRESHOLDS)
    for key, value in (profile.buckets or {}).items():
        if key in merged:
            merged[key] = float(value)
    return merged


# ─── comp ───────────────────────────────────────────────────────────────────


def _annual(amount: float, period: str) -> float:
    return annualize(amount, None, period)[0] or 0.0


def comp_fit(
    salary_min: float | None,
    salary_max: float | None,
    period: str | None,
    salary_stated: bool,
    profile: Profile,
) -> int | None:
    """Score pay against the profile, or None when it cannot be judged.

    Curve, on the annualized top of the range (``salary_max``, else ``salary_min``):
    ``floor`` is ``hard.salary_floor``, ``target`` is ``hard.salary_target`` (or ``floor * 1.2``
    when only a floor is set; with only a target, ``floor = target / 1.2``).

    - max <= floor          -> 0
    - floor < max < target  -> linear, ``(max - floor) / (target - floor) * 100``
    - max >= target         -> 100 (above target caps)

    None means "no evidence": salary not stated, unparseable period, or no floor/target
    configured. ``overall`` then drops the dimension instead of scoring it 0 (specs/006).
    """
    if not salary_stated:
        return None
    top = salary_max if salary_max is not None else salary_min
    if top is None:
        return None
    ann_top = annualize(top, None, period)[0]
    if ann_top is None:
        return None

    hard = profile.hard
    floor = (
        _annual(hard.salary_floor.amount, hard.salary_floor.period)
        if hard.salary_floor and hard.salary_floor.amount
        else None
    )
    target = (
        _annual(hard.salary_target.amount, hard.salary_target.period)
        if hard.salary_target
        else None
    )
    if floor is None and target is None:
        return None
    if target is None:
        assert floor is not None
        target = floor * _TARGET_FLOOR_RATIO
    if floor is None:
        floor = target / _TARGET_FLOOR_RATIO
    if ann_top >= target:
        return 100
    if ann_top <= floor:
        return 0
    return round((ann_top - floor) / (target - floor) * 100)


# ─── location ───────────────────────────────────────────────────────────────


def _clamp(value: float) -> int:
    return max(0, min(100, round(value)))


def location_fit(
    locations: Sequence[JobLocation], scope: LocationScope | str, profile: Profile
) -> int:
    """Score where the job is (specs/011 "Location fit scoring"); multi-location = best location.

    Per state: excluded or outside a ``states_allowed`` list -> not acceptable; ranked state ->
    ``100 - 10 * index`` floored at 60; allowed but unranked -> 50 (``states_allowed: all``
    allows every state). ``remote_us`` / ``nationwide`` -> ``70 + remote_bonus``, ``negotiable``
    -> ``60 + remote_bonus`` (a mild positive), the bonus applying only when ``remote_ok``.
    Remote scope without ``remote_ok`` is a weak 30 unless a state location does better.
    The best candidate wins, clamped to 0..100. Nothing acceptable -> 0; no location data
    at all -> 50 (silence is not evidence).
    """
    scope = LocationScope(scope)
    hard, soft = profile.hard, profile.soft
    excluded = set(hard.states_excluded)
    ranking = soft.state_ranking
    candidates: list[int] = []

    states = [loc.state for loc in locations if loc.state]
    for state in states:
        if state in excluded:
            continue
        if hard.states_allowed != "all" and state not in hard.states_allowed:
            continue
        if state in ranking:
            candidates.append(max(RANK_FLOOR, RANK_TOP - RANK_STEP * ranking.index(state)))
        else:
            candidates.append(UNRANKED_STATE)

    if scope in (LocationScope.remote_us, LocationScope.nationwide, LocationScope.negotiable):
        base = NEGOTIABLE_BASE if scope is LocationScope.negotiable else REMOTE_BASE
        if hard.remote_ok:
            candidates.append(_clamp(base + soft.remote_bonus))
        else:
            candidates.append(NO_REMOTE_FALLBACK)

    if candidates:
        return _clamp(max(candidates))
    if states or scope is LocationScope.overseas:
        return 0
    return UNKNOWN_LOCATION


# ─── overall ────────────────────────────────────────────────────────────────


def overall(
    model_dims: Mapping[str, int],
    comp: int | None,
    location: int,
    weights: Mapping[str, float],
) -> int:
    """Weighted mean of skills, seniority, domain, comp, location.

    ``model_dims["skills"]`` must be the recency-weighted skills score, never raw (006).
    When ``comp`` is None the dimension is dropped and the remaining weights renormalized.
    """
    values: dict[str, float] = {
        "skills": model_dims["skills"],
        "seniority": model_dims["seniority"],
        "domain": model_dims["domain"],
        "location": location,
    }
    if comp is not None:
        values["comp"] = comp
    total = sum(weights[k] for k in values)
    if total <= 0:
        return _clamp(sum(values.values()) / len(values))
    return _clamp(sum(values[k] * weights[k] for k in values) / total)


# ─── seniority direction ────────────────────────────────────────────────────


def default_seniority_direction(dims: Mapping[str, Any]) -> str:
    """ "above" / "below" / "match" from a stored ``seniority_direction`` (top level or inside
    the seniority dimension as ``direction``); the score alone cannot tell, so default "match".
    """
    value = dims.get("seniority_direction")
    if value is None and isinstance(dims.get("seniority"), Mapping):
        value = dims["seniority"].get("direction")
    return value if value in ("above", "below", "match") else "match"


# Pluggable: swap this if the screen schema later emits direction explicitly.
seniority_direction: Callable[[Mapping[str, Any]], str] = default_seniority_direction


# ─── buckets and verdict ────────────────────────────────────────────────────


def assign_bucket(
    *,
    raw_skills: int,
    recency_skills: int,
    domain: int,
    comp: int | None,
    overall_score: int,
    direction: str,
    blockers: Sequence[str],
    thresholds: Mapping[str, float] | None = None,
) -> Bucket:
    """Apply the 006 rules. Order: G, F, E, C, D, A, B.

    The spec lists B before A with "first match wins", which makes A unreachable (A implies
    B). A is checked first so a qualifying bullseye is not swallowed. Any blocker demotes one
    level (A->B, B->C). ``skills`` in the rules is the recency-weighted score. E needs a
    known comp: an unstated salary is not evidence of "pay below floor".
    """
    t = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    if recency_skills < t["g_skills"] or domain < t["g_domain"]:
        return Bucket.G
    if recency_skills < t["f_recency"] and raw_skills >= t["f_raw"]:
        return Bucket.F
    if direction == "below" and comp is not None and comp < t["e_comp"]:
        return Bucket.E
    if direction == "above" and recency_skills >= t["c_skills"]:
        return Bucket.C
    if recency_skills >= t["d_skills"] and domain < t["d_domain"]:
        return Bucket.D
    blocked = bool(blockers)
    qualifies_a = overall_score >= t["a_overall"] and recency_skills >= t["a_recency"]
    if qualifies_a:
        return Bucket.B if blocked else Bucket.A
    if overall_score >= t["b_overall"]:
        return Bucket.C if blocked else Bucket.B
    # No rule matched (spec gap): clearly weak overall is a mismatch, otherwise lateral.
    if overall_score < t["fallback_mismatch_overall"]:
        return Bucket.G
    return Bucket.D


def verdict_for(
    overall_score: int, blockers: Sequence[str], thresholds: Mapping[str, float] | None = None
) -> Verdict:
    """Coarse rollup: strong >= 78, possible >= 60, weak >= 40, else mismatch.

    ``blockers`` is accepted for API symmetry but does not change the verdict (spec silent).
    """
    del blockers
    t = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    if overall_score >= t["verdict_strong"]:
        return Verdict.strong
    if overall_score >= t["verdict_possible"]:
        return Verdict.possible
    if overall_score >= t["verdict_weak"]:
        return Verdict.weak
    return Verdict.mismatch


# ─── per-row recompute ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class ComputedFit:
    comp: int | None
    location: int
    overall: int
    bucket: Bucket
    verdict: Verdict
    raw_skills: int
    recency_weighted_skills: int
    seniority_direction: str = "match"
    blockers: list[str] = field(default_factory=list)


def _score(dim: Any, default: int = 0) -> int:
    if isinstance(dim, Mapping):
        dim = dim.get("score")
    return int(dim) if isinstance(dim, int | float) else default


def _loads(raw: Any, default: Any) -> Any:
    if raw is None or raw == "":
        return default
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except ValueError:
            return default
    return raw


def compute_row(
    fit_score_row: Mapping[str, Any],
    job_row: Mapping[str, Any],
    locations: Sequence[JobLocation],
    profile: Profile,
) -> ComputedFit:
    """Recompute every derived field from stored model dimensions and current settings."""
    dims = _loads(fit_score_row["dimensions"], {})
    if not isinstance(dims, dict):
        dims = {}
    skills = _score(dims.get("skills"))
    recency = _score(dims.get("recency_weighted_skills"), skills)
    raw = _score(dims.get("raw_skills"), skills)
    seniority = _score(dims.get("seniority"), 50)
    domain = _score(dims.get("domain"), 50)
    blockers_raw = _loads(fit_score_row["blockers"], [])
    blockers = [str(b) for b in blockers_raw] if isinstance(blockers_raw, list) else []

    comp = comp_fit(
        job_row["salary_min"],
        job_row["salary_max"],
        job_row["salary_period"],
        bool(job_row["salary_stated"]),
        profile,
    )
    loc = location_fit(locations, job_row["location_scope"], profile)
    total = overall(
        {"skills": recency, "seniority": seniority, "domain": domain},
        comp,
        loc,
        profile.soft.weights.model_dump(),
    )
    th = thresholds_for(profile)
    direction = seniority_direction(dims)
    bucket = assign_bucket(
        raw_skills=raw,
        recency_skills=recency,
        domain=domain,
        comp=comp,
        overall_score=total,
        direction=direction,
        blockers=blockers,
        thresholds=th,
    )
    return ComputedFit(
        comp=comp,
        location=loc,
        overall=total,
        bucket=bucket,
        verdict=verdict_for(total, blockers, th),
        raw_skills=raw,
        recency_weighted_skills=recency,
        seniority_direction=direction,
        blockers=blockers,
    )


# ─── live preview ───────────────────────────────────────────────────────────

_PREVIEW_SQL = """
SELECT fs.*, j.id AS job_id, j.salary_min, j.salary_max, j.salary_period, j.salary_stated,
       j.location_scope, j.title
FROM fit_score fs
JOIN job_group g ON g.id = fs.job_group_id
JOIN job j ON j.id = g.canonical_job_id
WHERE fs.created_at >= ?
  AND fs.id = (
    SELECT f2.id FROM fit_score f2 WHERE f2.job_group_id = fs.job_group_id
    ORDER BY (f2.tier = 'deep') DESC, f2.created_at DESC, f2.id DESC LIMIT 1
  )
"""


def preview(
    conn: sqlite3.Connection,
    profile_before: Profile,
    profile_after: Profile,
    *,
    days: int = 30,
) -> dict[str, Any]:
    """Bucket counts under two profiles over recent scored jobs (the /prefs live preview).

    Uses the latest score per job group (deep preferred) for its canonical job, with no
    prefilter applied, so it shows the effect of the setting itself. Returns ``total``,
    ``before`` / ``after`` / ``delta`` bucket counts, ``salary_filtered`` before/after (jobs
    whose pay tops out at or below the floor), and ``moves`` (every job whose bucket changed).
    """
    since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    cur = conn.cursor()
    cur.row_factory = sqlite3.Row
    rows = cur.execute(_PREVIEW_SQL, (since,)).fetchall()
    before: Counter[str] = Counter()
    after: Counter[str] = Counter()
    moves: list[dict[str, Any]] = []
    salary_before = salary_after = 0
    for row in rows:
        locs = load_locations(conn, row["job_id"])
        cb = compute_row(row, row, locs, profile_before)
        ca = compute_row(row, row, locs, profile_after)
        before[cb.bucket.value] += 1
        after[ca.bucket.value] += 1
        salary_before += cb.comp == 0
        salary_after += ca.comp == 0
        if ca.bucket != cb.bucket:
            ab = (Bucket.A, Bucket.B)
            moves.append(
                {
                    "job_id": row["job_id"],
                    "job_group_id": row["job_group_id"],
                    "title": row["title"],
                    "before": cb.bucket.value,
                    "after": ca.bucket.value,
                    "into_ab": ca.bucket in ab and cb.bucket not in ab,
                }
            )
    keys = [b.value for b in Bucket]
    return {
        "total": len(rows),
        "before": {k: before[k] for k in keys},
        "after": {k: after[k] for k in keys},
        "delta": {k: after[k] - before[k] for k in keys},
        "salary_filtered": {"before": salary_before, "after": salary_after},
        "moves": moves,
    }
