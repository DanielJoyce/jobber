"""Normalize stage: pure parsing over cached raw columns (specs/004 "normalize").

Never overwrites ``*_raw`` columns and never invents values: unparseable input becomes NULL
plus a ``parse_warnings`` entry. Safe to re-run over all history with ``force=True``.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from datetime import datetime

from jobhunter.core.salary_text import extract_salary_from_text
from jobhunter.core.textnorm import (
    content_hash,
    detect_employment_type,
    detect_remote,
    html_to_text,
    parse_date,
    parse_salary,
)
from jobhunter.pipeline.listing import _txn, to_iso
from jobhunter.pipeline.locations import apply_locations

WARNING_PREFIX = "normalize: "
_EMP_HEAD_CHARS = 1500


def _norm_date(value: str | None, tz: str, label: str, warnings: list[str]) -> str | None:
    """Return ISO-UTC for a stored date; parse free text if it is not already ISO."""
    if not value:
        return value
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        dt = None
    if dt is not None and dt.tzinfo is not None:
        return to_iso(dt)
    parsed = parse_date(value, tz)
    if parsed is None:
        warnings.append(f"{WARNING_PREFIX}{label} unparseable: {value[:60]!r}")
        return value
    return to_iso(parsed)


def resolve_salary(
    raw: str | None, source: str | None, text: str, location: str | None
) -> dict[str, object]:
    """Salary columns for one job. Structured pay always wins; description text is the fallback.

    ``source == 'text'`` means ``raw`` is a snippet we extracted earlier, so it is ignored here
    and the description is searched again (idempotent, follows description changes).
    """
    warnings: list[str] = []
    structured = None if source == "text" else raw
    sal = parse_salary(structured)
    warnings.extend(sal.warnings)
    if sal.min is not None or sal.max is not None:
        return {
            "salary_raw": structured, "salary_min": sal.min, "salary_max": sal.max,
            "salary_period": sal.period, "salary_stated": 1 if sal.stated else 0,
            "salary_source": "structured", "warnings": warnings,
        }  # fmt: skip
    found = extract_salary_from_text(text, location)
    if found is not None:
        return {
            "salary_raw": found.raw, "salary_min": found.min, "salary_max": found.max,
            "salary_period": found.period, "salary_stated": 1, "salary_source": "text",
            "warnings": [*warnings, f"salary taken from description text: {found.raw[:60]!r}"],
        }  # fmt: skip
    return {
        "salary_raw": structured, "salary_min": None, "salary_max": None,
        "salary_period": None, "salary_stated": 1 if sal.stated else 0,
        "salary_source": None, "warnings": warnings,
    }  # fmt: skip


def _normalize_row(row: sqlite3.Row, tz: str) -> dict[str, object]:
    warnings: list[str] = []
    text = html_to_text(row["description_raw"])
    if not text:
        warnings.append(f"{WARNING_PREFIX}description empty after html_to_text")

    sal = resolve_salary(row["salary_raw"], row["salary_source"], text, row["location_raw"])
    warnings.extend(f"{WARNING_PREFIX}{w}" for w in sal["warnings"])  # type: ignore[attr-defined]

    employer = row["employer"] or row["agency_raw"]
    emp = detect_employment_type(f"{row['title']}\n{text[:_EMP_HEAD_CHARS]}")
    if emp == "unknown":
        emp = row["employment_type"]  # keep an adapter-supplied value

    # Preserve earlier non-normalize warnings (e.g. from adapters); replace our own.
    old: list[str] = json.loads(row["parse_warnings"]) if row["parse_warnings"] else []
    kept = [w for w in old if not w.startswith(WARNING_PREFIX)]

    posted_at = _norm_date(row["posted_at"], tz, "posted_at", warnings)
    closes_at = _norm_date(row["closes_at"], tz, "closes_at", warnings)
    all_warnings = kept + warnings
    return {
        "description_text": text,
        "salary_raw": sal["salary_raw"],
        "salary_min": sal["salary_min"],
        "salary_max": sal["salary_max"],
        "salary_period": sal["salary_period"],
        "salary_stated": sal["salary_stated"],
        "salary_source": sal["salary_source"],
        "remote": detect_remote(row["title"], row["location_raw"], text),
        "employment_type": emp,
        "employer": employer,
        "posted_at": posted_at,
        "closes_at": closes_at,
        "content_hash": content_hash(row["title"], employer, row["location_raw"], text),
        "parse_warnings": json.dumps(all_warnings) if all_warnings else None,
    }


_UPDATE = """
UPDATE job SET
  description_text = :description_text, salary_min = :salary_min,
  salary_max = :salary_max, salary_period = :salary_period, salary_raw = :salary_raw,
  salary_source = :salary_source,
  salary_stated = :salary_stated, remote = :remote,
  employment_type = :employment_type, employer = :employer,
  posted_at = :posted_at, closes_at = :closes_at,
  content_hash = :content_hash, parse_warnings = :parse_warnings,
  stage = CASE WHEN stage IN ('listed', 'resolved') THEN 'normalized'
               ELSE stage END
WHERE id = :id
"""


def normalize_job(conn: sqlite3.Connection, job_id: int, tz: str = "UTC") -> None:
    """Re-normalize one job in place (e.g. after a pasted description); stage is kept."""
    row = conn.execute("SELECT * FROM job WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        raise KeyError(f"no such job: {job_id}")
    vals = _normalize_row(row, tz)
    vals["id"] = job_id
    conn.execute(_UPDATE, vals)


def normalize_pending(
    conn: sqlite3.Connection,
    *,
    source_timezones: Mapping[str, str] | None = None,
    limit: int | None = None,
    force: bool = False,
) -> int:
    """Normalize jobs at stage 'listed'/'resolved' that have a description; return rows done.

    With ``force=True`` every row that has a description_raw is re-processed (any stage), so
    parser fixes apply retroactively; the stage of already-advanced rows is left unchanged.
    """
    tzs = source_timezones or {}
    where = "description_raw IS NOT NULL AND description_raw != ''"
    if not force:
        where += " AND stage IN ('listed', 'resolved')"
    sql = f"SELECT * FROM job WHERE {where} ORDER BY id"
    params: tuple[int, ...] = ()
    if limit is not None:
        sql += " LIMIT ?"
        params = (limit,)
    rows = conn.execute(sql, params).fetchall()
    with _txn(conn):
        for row in rows:
            vals = _normalize_row(row, tzs.get(row["source_key"], "UTC"))
            vals["id"] = row["id"]
            conn.execute(_UPDATE, vals)
    apply_locations(conn, force=force)  # leave job_locations populated (specs/011)
    return len(rows)


def _salary_counts(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT COALESCE(salary_source, 'none') AS s, COUNT(*) AS n FROM job GROUP BY s"
    ).fetchall()
    counts = {"structured": 0, "text": 0, "none": 0}
    counts.update({r["s"]: r["n"] for r in rows})
    return counts


def backfill_salary(conn: sqlite3.Connection) -> tuple[dict[str, int], dict[str, int]]:
    """Fill salary from description text for jobs with no structured salary; free and local.

    Touches only the salary columns of jobs whose pay did not come from a structured field, so it
    is idempotent and safe to re-run. Returns (counts before, counts after) keyed by salary_source
    ('structured', 'text', 'none').
    """
    before = _salary_counts(conn)
    rows = conn.execute(
        "SELECT id, salary_raw, salary_source, description_text, location_raw FROM job "
        "WHERE description_text IS NOT NULL AND description_text != '' "
        "AND (salary_source IS NULL OR salary_source = 'text')"
    ).fetchall()
    with _txn(conn):
        for r in rows:
            sal = resolve_salary(
                r["salary_raw"], r["salary_source"], r["description_text"], r["location_raw"]
            )
            sal.pop("warnings")
            sal["id"] = r["id"]
            conn.execute(
                "UPDATE job SET salary_raw = :salary_raw, salary_min = :salary_min, "
                "salary_max = :salary_max, salary_period = :salary_period, "
                "salary_stated = :salary_stated, salary_source = :salary_source WHERE id = :id",
                sal,
            )
    return before, _salary_counts(conn)
