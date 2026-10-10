"""Stage 1: deterministic prefilter (specs/006 "Stage 1", specs/004 "prefilter").

Free, instant, auditable. **Reject only on facts explicitly present in the posting**: a missing
salary, location, employment type or closing date never rejects. Every rejection records its
rule keys in ``prefilter_result.reasons``. Results are keyed on ``filter_version``, so changing
the profile's hard fields re-evaluates for free.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any

from jobhunter.core.models import JobLocation, LocationScope
from jobhunter.core.textnorm import annualize
from jobhunter.pipeline.listing import _txn
from jobhunter.pipeline.locations import REMOTE_SCOPES, load_job_group_locations
from jobhunter.scoring.profile import Hard, Profile

logger = logging.getLogger(__name__)

STATE_NOT_ALLOWED = "state_not_allowed"
REMOTE_ONLY_VIOLATION = "remote_only_violation"
SALARY_BELOW_FLOOR = "salary_below_floor"
SALARY_UNSTATED = "salary_unstated"
EMPLOYMENT_TYPE_EXCLUDED = "employment_type_excluded"
TITLE_EXCLUDED = "title_excluded"
CLOSED = "closed"
CREDENTIAL_REQUIRED = "credential_required"
OVERSEAS = "overseas"

_EVALUATE_STAGES = ("grouped", "prefiltered", "scored", "triaged")

# ─── credential phrase table ────────────────────────────────────────────────
# Conservative: a credential counts only when the text says it is required/must be held.
# "preferred", "desired" and "ability to obtain" never match.
_CLEARANCE = r"(?:(?:top\s+)?secret|ts(?:\s*/\s*sci)?|security|dod)"
_CDL = r"(?:cdl(?:\s*-?\s*class\s+[abc])?|commercial\s+driver(?:'|\u2019)?s?\s+licen[sc]e)"
_RN = (
    r"(?:rn\s+licen[sc]e|registered\s+nurse\s+licen[sc]e|licen[sc]e\s+as\s+a\s+registered\s+nurse)"
)
_PE = (
    r"(?:pe\s+licen[sc]e|professional\s+engineer(?:ing)?\s+licen[sc]e|"
    r"licen[sc]e\s+as\s+a\s+professional\s+engineer)"
)
_ADJ = r"(?:(?:active|current|valid|unrestricted|state)[\s,]+){0,3}"
_REQ = r"(?:\s+is|\s+are)?\s+required\b"
_MUST = r"\b(?:must|shall)\s+(?:possess|have|hold|maintain)\s+(?:an?\s+)?"
_REQUIRES = r"\brequires?\s+(?:an?\s+)?"

_CREDENTIAL_PATTERNS: dict[str, tuple[str, ...]] = {
    "active_security_clearance": (
        rf"\bactive\s+{_CLEARANCE}\s+clearance{_REQ}",
        rf"{_MUST}active\s+(?:[\w/\-]+\s+){{0,3}}clearance\b",
        rf"{_REQUIRES}active\s+(?:[\w/\-]+\s+){{0,3}}clearance\b",
        rf"\b{_CLEARANCE}\s+clearance{_REQ}",
    ),
    "cdl": (
        rf"\b{_ADJ}{_CDL}{_REQ}",
        rf"{_MUST}{_ADJ}{_CDL}\b",
        rf"{_REQUIRES}{_ADJ}{_CDL}\b",
    ),
    "rn_license": (
        rf"\b{_ADJ}{_RN}{_REQ}",
        rf"{_MUST}{_ADJ}{_RN}\b",
        rf"{_REQUIRES}{_ADJ}{_RN}\b",
    ),
    "pe_license": (
        rf"\b{_ADJ}{_PE}{_REQ}",
        rf"{_MUST}{_ADJ}{_PE}\b",
        rf"{_REQUIRES}{_ADJ}{_PE}\b",
        rf"\blicensed\s+professional\s+engineer{_REQ}",
    ),
}
_COMPILED = {k: [re.compile(p, re.I) for p in v] for k, v in _CREDENTIAL_PATTERNS.items()}
_warned_credentials: set[str] = set()


def _get(job: Any, key: str, default: Any = None) -> Any:
    try:
        value = job[key]
    except (KeyError, IndexError):
        return default
    return default if value is None else value


# ─── rules: each returns a reason key or None ───────────────────────────────


def rule_state_not_allowed(job: Any, locations: Sequence[JobLocation], hard: Hard) -> str | None:
    if _get(job, "location_scope", "unknown") in {s.value for s in REMOTE_SCOPES}:
        return None
    states = {loc.state for loc in locations if loc.state}
    if not states:
        return None  # no enumerated state: silence is not evidence
    allowed = {s for s in states if s not in hard.states_excluded}
    if hard.states_allowed != "all":
        allowed &= set(hard.states_allowed)
    return None if allowed else STATE_NOT_ALLOWED


def rule_remote_only_violation(job: Any, hard: Hard) -> str | None:
    if hard.remote_only and _get(job, "remote") == "onsite":
        return REMOTE_ONLY_VIOLATION
    return None


def rule_salary(job: Any, hard: Hard) -> str | None:
    if not _get(job, "salary_stated", 0):
        return SALARY_UNSTATED if hard.hide_unstated_salary else None
    floor = hard.salary_floor
    if floor is None or floor.amount is None:
        return None
    floor_year = annualize(None, floor.amount, floor.period)[1]
    _, top = annualize(_get(job, "salary_min"), _get(job, "salary_max"), _get(job, "salary_period"))
    if floor_year is None or top is None:
        return None
    return SALARY_BELOW_FLOOR if top < floor_year else None


def rule_employment_type(job: Any, hard: Hard) -> str | None:
    etype = _get(job, "employment_type", "unknown")
    if etype != "unknown" and etype in {str(e) for e in hard.employment_types_excluded}:
        return EMPLOYMENT_TYPE_EXCLUDED
    return None


def rule_title_excluded(job: Any, hard: Hard) -> str | None:
    title = _get(job, "title", "")
    for pattern in hard.title_exclusions:
        pattern = pattern.strip()
        if pattern and re.search(rf"(?<!\w){re.escape(pattern)}(?!\w)", title, re.I):
            return TITLE_EXCLUDED
    return None


def _parse_dt(raw: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def rule_closed(job: Any, now: datetime) -> str | None:
    raw = _get(job, "closes_at")
    if not raw:
        return None
    closes = _parse_dt(str(raw))
    if closes is None:
        return None
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    return CLOSED if closes < now else None


def rule_credential_required(job: Any, hard: Hard) -> str | None:
    text = _get(job, "description_text") or _get(job, "description_raw") or ""
    for key in hard.requires_i_lack:
        patterns = _COMPILED.get(key)
        if patterns is None:
            if key not in _warned_credentials:
                _warned_credentials.add(key)
                logger.warning("prefilter: unknown requires_i_lack credential %r ignored", key)
            continue
        if any(p.search(text) for p in patterns):
            return CREDENTIAL_REQUIRED
    return None


def rule_overseas(job: Any) -> str | None:
    # No profile opt-in field exists yet, so overseas always rejects.
    return OVERSEAS if _get(job, "location_scope") == LocationScope.overseas.value else None


# ─── evaluation ─────────────────────────────────────────────────────────────


def evaluate(
    job: Any, locations: Sequence[JobLocation], profile: Profile, now: datetime
) -> tuple[bool, list[str]]:
    """Return ``(passed, reasons)``; reasons are every rule key that fired."""
    hard = profile.hard
    checks: list[Callable[[], str | None]] = [
        lambda: rule_state_not_allowed(job, locations, hard),
        lambda: rule_remote_only_violation(job, hard),
        lambda: rule_salary(job, hard),
        lambda: rule_employment_type(job, hard),
        lambda: rule_title_excluded(job, hard),
        lambda: rule_closed(job, now),
        lambda: rule_credential_required(job, hard),
        lambda: rule_overseas(job),
    ]
    reasons = [r for check in checks if (r := check()) is not None]
    return (not reasons, reasons)


def run_prefilter(
    conn: sqlite3.Connection,
    profile: Profile,
    *,
    now: datetime,
    limit: int | None = None,
    force: bool = False,
) -> dict[str, int]:
    """Evaluate canonical jobs lacking a result for the current ``filter_version``.

    Advances 'grouped' jobs to 'prefiltered'. Returns counts: evaluated, passed, rejected.
    """
    version = profile.filter_version
    marks = ",".join("?" for _ in _EVALUATE_STAGES)
    sql = (
        "SELECT j.* FROM job_group g JOIN job j ON j.id = g.canonical_job_id "
        "LEFT JOIN prefilter_result p ON p.job_id = j.id AND p.filter_version = ? "
        f"WHERE j.stage IN ({marks})"
        # A group scored only on request (a pasted or captured posting, specs/017 "Scored on
        # request: a group flag") passes because the user chose it (Score this group now); a
        # later rule run must not overturn that, whichever member is canonical.
        " AND g.score_on_request = 0"
    )
    if not force:
        sql += " AND p.job_id IS NULL"
    sql += " ORDER BY j.id"
    params: list[Any] = [version, *_EVALUATE_STAGES]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    jobs = conn.execute(sql, params).fetchall()

    counts = {"evaluated": 0, "passed": 0, "rejected": 0}
    if not jobs:
        return counts  # no empty write transaction (the /prefs panel asks on every load)
    with _txn(conn):
        for job in jobs:
            passed, reasons = evaluate(job, load_job_group_locations(conn, job["id"]), profile, now)
            conn.execute(
                "INSERT OR REPLACE INTO prefilter_result "
                "(job_id, passed, reasons, filter_version, evaluated_at) VALUES (?, ?, ?, ?, ?)",
                (job["id"], int(passed), json.dumps(reasons), version, now.isoformat()),
            )
            if job["stage"] == "grouped":
                conn.execute("UPDATE job SET stage = 'prefiltered' WHERE id = ?", (job["id"],))
            counts["evaluated"] += 1
            counts["passed" if passed else "rejected"] += 1
    return counts


def rejected_summary(conn: sqlite3.Connection, filter_version: str) -> dict[str, int]:
    """Rejected-job counts by reason key, for the /rejected page."""
    rows = conn.execute(
        "SELECT reasons FROM prefilter_result WHERE filter_version = ? AND passed = 0",
        (filter_version,),
    ).fetchall()
    out: dict[str, int] = {}
    for row in rows:
        for reason in json.loads(row["reasons"]):
            out[reason] = out.get(reason, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: (-kv[1], kv[0])))
