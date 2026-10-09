from datetime import UTC, datetime, timedelta

from jobhunter.core.models import JobStub
from jobhunter.pipeline.listing import (
    compute_watermark,
    record_run_watermark,
    upsert_stubs,
)

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def stub(**kw):
    base = {
        "source_key": "wa",
        "external_id": "1",
        "title": "Analyst",
        "url": "https://example.com/j/1",
        "posted_at": T0 - timedelta(days=1),
    }
    return JobStub(**{**base, **kw})


def row(conn, ext="1"):
    return conn.execute("SELECT * FROM job WHERE external_id = ?", (ext,)).fetchone()


def test_insert_then_update_counts(conn):
    assert upsert_stubs(conn, [stub(), stub(external_id="2")], T0).inserted == 2
    res = upsert_stubs(conn, [stub(), stub(external_id="3")], T0 + timedelta(days=1))
    assert (res.inserted, res.updated) == (1, 1)
    assert row(conn)["stage"] == "listed"


def test_first_seen_immutable_last_seen_moves(conn):
    upsert_stubs(conn, [stub()], T0)
    later = T0 + timedelta(days=3)
    upsert_stubs(conn, [stub()], later)
    r = row(conn)
    assert r["first_seen_at"] == T0.isoformat()
    assert r["last_seen_at"] == later.isoformat()


def test_conflict_updates_volatile_fields_only(conn):
    upsert_stubs(conn, [stub(location_raw="Olympia", salary_raw="$10/hr")], T0)
    upsert_stubs(
        conn,
        [
            stub(
                title="Senior Analyst",
                closes_at=T0 + timedelta(days=10),
                location_raw="Seattle",
                salary_raw="$99/hr",
                url="https://example.com/other",
                posted_at=T0,
            )
        ],
        T0 + timedelta(days=1),
    )
    r = row(conn)
    assert r["title"] == "Senior Analyst"
    assert r["closes_at"] == (T0 + timedelta(days=10)).isoformat()
    assert r["location_raw"] == "Olympia"
    assert r["salary_raw"] == "$10/hr"
    assert r["url"] == "https://example.com/j/1"
    assert r["posted_at"] == (T0 - timedelta(days=1)).isoformat()


def test_closes_at_not_cleared_by_stub_without_it(conn):
    upsert_stubs(conn, [stub(closes_at=T0 + timedelta(days=5))], T0)
    upsert_stubs(conn, [stub()], T0 + timedelta(days=1))
    assert row(conn)["closes_at"] == (T0 + timedelta(days=5)).isoformat()


def test_description_filled_only_when_row_has_none(conn):
    upsert_stubs(conn, [stub()], T0)
    assert row(conn)["needs_resolve"] == 1
    upsert_stubs(conn, [stub(description_raw="<p>first</p>", needs_resolve=False)], T0)
    r = row(conn)
    assert r["description_raw"] == "<p>first</p>"
    assert r["needs_resolve"] == 0
    upsert_stubs(conn, [stub(description_raw="<p>second</p>", needs_resolve=True)], T0)
    r = row(conn)
    assert r["description_raw"] == "<p>first</p>"
    assert r["needs_resolve"] == 0


def test_posted_at_estimated_when_missing(conn):
    upsert_stubs(conn, [stub(posted_at=None)], T0)
    r = row(conn)
    assert r["posted_at_estimated"] == 1
    assert r["posted_at"] == T0.isoformat()
    upsert_stubs(conn, [stub(external_id="2")], T0)
    assert row(conn, "2")["posted_at_estimated"] == 0


def test_naive_datetimes_assumed_utc(conn):
    upsert_stubs(conn, [stub(posted_at=datetime(2026, 9, 30, 8, 0))], T0)
    assert row(conn)["posted_at"] == "2026-09-30T08:00:00+00:00"


def test_watermark_no_state_uses_lookback(conn):
    assert compute_watermark(conn, "wa", T0) == T0 - timedelta(days=7)
    assert compute_watermark(conn, "wa", T0, lookback_days=3) == T0 - timedelta(days=3)


def test_watermark_overlap(conn):
    posted = T0 - timedelta(hours=6)
    record_run_watermark(conn, "wa", posted, T0)
    assert compute_watermark(conn, "wa", T0) == posted - timedelta(days=2)


def test_watermark_lookback_floor(conn):
    record_run_watermark(conn, "wa", T0 - timedelta(days=30), T0)
    assert compute_watermark(conn, "wa", T0) == T0 - timedelta(days=7)


def test_record_run_keeps_max_and_resets_failures(conn):
    conn.execute("INSERT INTO source_state (source_key, consecutive_failures) VALUES ('wa', 3)")
    newer = T0 - timedelta(days=1)
    record_run_watermark(conn, "wa", newer, T0)
    record_run_watermark(conn, "wa", T0 - timedelta(days=5), T0 + timedelta(days=1))
    record_run_watermark(conn, "wa", None, T0 + timedelta(days=2))
    s = conn.execute("SELECT * FROM source_state WHERE source_key = 'wa'").fetchone()
    assert s["max_posted_at_seen"] == newer.isoformat()
    assert s["last_run_at"] == (T0 + timedelta(days=2)).isoformat()
    assert s["last_ok_at"] == (T0 + timedelta(days=2)).isoformat()
    assert s["consecutive_failures"] == 0
