"""Migration 0043 (bug c369bcc): ``apply_link_attempt``, the per-group count of apply-link tries.

Run on a database at 0042 holding apply links: nothing existing changes, every group starts
with no tries counted, and a try is counted once per call.
"""

from __future__ import annotations

from datetime import UTC, datetime

from jobhunter.core import db
from jobhunter.pipeline import apply_attempts

ISO = "2026-10-12T12:00:00+00:00"


def test_0043_on_a_database_with_apply_links(monkeypatch):
    real = db._load_migrations
    monkeypatch.setattr(db, "_load_migrations", lambda: [m for m in real() if m[0] <= 42])
    c = db.connect(":memory:")
    db.migrate(c)
    c.execute(
        "INSERT INTO source (key, class, name, family, tier, entry, policy) VALUES "
        "('co', 'A', 'Synthetic', 'x', 'http', 'https://example.com', 'enabled')"
    )
    c.execute(
        "INSERT INTO job_group (id, canonical_job_id, member_count, method, created_at) "
        "VALUES (1, NULL, 1, 'exact_hash', ?)",
        (ISO,),
    )
    c.execute(
        "INSERT INTO apply_link (job_group_id, start_url, final_url, chain, status, resolved_at) "
        "VALUES (1, 'https://example.com/1', NULL, '[]', 'unresolved', ?)",
        (ISO,),
    )
    monkeypatch.setattr(db, "_load_migrations", real)
    db.migrate(c)
    assert 43 in db.applied_versions(c)
    assert c.execute("SELECT status FROM apply_link").fetchone()[0] == "unresolved"
    assert apply_attempts.get(c, 1) is None
    now = datetime(2026, 10, 12, 13, tzinfo=UTC)
    apply_attempts.record(c, 1, now=now, outcome="unresolved")
    apply_attempts.record(c, 1, now=now, outcome="deferred")
    got = apply_attempts.get(c, 1)
    assert got is not None and (got.count, got.last_at, got.last_outcome) == (2, now, "deferred")
    apply_attempts.reset(c, 1)
    assert apply_attempts.get(c, 1) is None
    c.close()
