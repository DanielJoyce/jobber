"""List stage: upsert JobStubs and maintain per-source watermarks (specs/004 "list").

Re-listing is a no-op apart from volatile fields; ``first_seen_at`` is immutable.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from jobhunter.core.db import transaction
from jobhunter.core.models import JobStub

DEFAULT_LOOKBACK_DAYS = 7
DEFAULT_OVERLAP_DAYS = 2


@dataclass
class UpsertResult:
    inserted: int = 0
    updated: int = 0


def to_iso(dt: datetime) -> str:
    """Aware datetimes are converted to UTC; naive ones are assumed to be UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat()


def from_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


@contextmanager
def _txn(conn: sqlite3.Connection) -> Iterator[None]:
    if conn.in_transaction:
        yield
    else:
        with transaction(conn):
            yield


_UPSERT = """
INSERT INTO job (
  source_key, external_id, url, title, agency_raw, employer, description_raw,
  posted_at, posted_at_estimated, closes_at, salary_raw, location_raw,
  stage, needs_resolve, first_seen_at, last_seen_at
) VALUES (
  :source_key, :external_id, :url, :title, :agency_raw, :agency_raw, :description_raw,
  :posted_at, :posted_est, :closes_at, :salary_raw, :location_raw,
  'listed', :needs_resolve, :now, :now
)
ON CONFLICT(source_key, external_id) DO UPDATE SET
  title = excluded.title,
  closes_at = COALESCE(excluded.closes_at, job.closes_at),
  last_seen_at = excluded.last_seen_at,
  description_raw = CASE WHEN job.description_raw IS NULL AND excluded.description_raw IS NOT NULL
                         THEN excluded.description_raw ELSE job.description_raw END,
  needs_resolve = CASE WHEN job.description_raw IS NULL AND excluded.description_raw IS NOT NULL
                       THEN excluded.needs_resolve ELSE job.needs_resolve END
"""


def upsert_stubs(conn: sqlite3.Connection, stubs: Iterable[JobStub], now: datetime) -> UpsertResult:
    """Insert new stubs at stage 'listed'; on conflict touch only volatile fields."""
    now_iso = to_iso(now)
    result = UpsertResult()
    with _txn(conn):
        for stub in stubs:
            existed = conn.execute(
                "SELECT 1 FROM job WHERE source_key = ? AND external_id = ?",
                (stub.source_key, stub.external_id),
            ).fetchone()
            estimated = stub.posted_at is None
            conn.execute(
                _UPSERT,
                {
                    "source_key": stub.source_key,
                    "external_id": stub.external_id,
                    "url": stub.url,
                    "title": stub.title,
                    "agency_raw": stub.agency_raw,
                    "description_raw": stub.description_raw,
                    "posted_at": now_iso if estimated else to_iso(stub.posted_at),  # type: ignore[arg-type]
                    "posted_est": 1 if estimated else 0,
                    "closes_at": to_iso(stub.closes_at) if stub.closes_at else None,
                    "salary_raw": stub.salary_raw,
                    "location_raw": stub.location_raw,
                    "needs_resolve": 1 if stub.needs_resolve else 0,
                    "now": now_iso,
                },
            )
            if existed:
                result.updated += 1
            else:
                result.inserted += 1
    return result


def compute_watermark(
    conn: sqlite3.Connection,
    source_key: str,
    now: datetime,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    overlap_days: int = DEFAULT_OVERLAP_DAYS,
) -> datetime:
    """``max(max_posted_at_seen - overlap, now - lookback_days)``; floor only on first run."""
    floor = now.astimezone(UTC) - timedelta(days=lookback_days)
    row = conn.execute(
        "SELECT max_posted_at_seen FROM source_state WHERE source_key = ?", (source_key,)
    ).fetchone()
    if row is None or row[0] is None:
        return floor
    return max(from_iso(row[0]) - timedelta(days=overlap_days), floor)


def record_run_watermark(
    conn: sqlite3.Connection,
    source_key: str,
    max_posted_at: datetime | None,
    now: datetime,
) -> None:
    """Record a successful run: bump last_run_at/last_ok_at and keep the newest posted_at seen."""
    now_iso = to_iso(now)
    new_max = to_iso(max_posted_at) if max_posted_at else None
    with _txn(conn):
        conn.execute(
            """
            INSERT INTO source_state (source_key, last_run_at, last_ok_at, max_posted_at_seen)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(source_key) DO UPDATE SET
              last_run_at = excluded.last_run_at,
              last_ok_at = excluded.last_ok_at,
              consecutive_failures = 0,
              max_posted_at_seen = CASE
                WHEN excluded.max_posted_at_seen IS NULL THEN source_state.max_posted_at_seen
                WHEN source_state.max_posted_at_seen IS NULL
                  OR excluded.max_posted_at_seen > source_state.max_posted_at_seen
                THEN excluded.max_posted_at_seen
                ELSE source_state.max_posted_at_seen END
            """,
            (source_key, now_iso, now_iso, new_max),
        )
