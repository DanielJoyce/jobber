"""Normalize stage: pure parsing over cached raw columns (specs/004 "normalize").

Never overwrites ``*_raw`` columns and never invents values: unparseable input becomes NULL
plus a ``parse_warnings`` entry. Safe to re-run over all history with ``force=True``.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from datetime import datetime

from jobhunter.core.textnorm import (
    content_hash,
    detect_employment_type,
    detect_remote,
    html_to_text,
    parse_date,
    parse_salary,
)
from jobhunter.pipeline.listing import _txn, to_iso

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


def _normalize_row(row: sqlite3.Row, tz: str) -> dict[str, object]:
    warnings: list[str] = []
    text = html_to_text(row["description_raw"])
    if not text:
        warnings.append(f"{WARNING_PREFIX}description empty after html_to_text")

    sal = parse_salary(row["salary_raw"])
    warnings.extend(f"{WARNING_PREFIX}{w}" for w in sal.warnings)

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
        "salary_min": sal.min,
        "salary_max": sal.max,
        "salary_period": sal.period,
        "salary_stated": 1 if sal.stated else 0,
        "remote": detect_remote(row["title"], row["location_raw"], text),
        "employment_type": emp,
        "employer": employer,
        "posted_at": posted_at,
        "closes_at": closes_at,
        "content_hash": content_hash(row["title"], employer, row["location_raw"], text),
        "parse_warnings": json.dumps(all_warnings) if all_warnings else None,
    }


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
            conn.execute(
                """
                UPDATE job SET
                  description_text = :description_text, salary_min = :salary_min,
                  salary_max = :salary_max, salary_period = :salary_period,
                  salary_stated = :salary_stated, remote = :remote,
                  employment_type = :employment_type, employer = :employer,
                  posted_at = :posted_at, closes_at = :closes_at,
                  content_hash = :content_hash, parse_warnings = :parse_warnings,
                  stage = CASE WHEN stage IN ('listed', 'resolved') THEN 'normalized'
                               ELSE stage END
                WHERE id = :id
                """,
                vals,
            )
    return len(rows)
