import json
from datetime import UTC, datetime

from jobhunter.core.models import JobStub
from jobhunter.pipeline.listing import upsert_stubs
from jobhunter.pipeline.normalize import normalize_pending

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
RAW_COLS = ("description_raw", "salary_raw", "location_raw", "agency_raw")


def add(conn, ext="1", **kw):
    base = {
        "source_key": "wa",
        "external_id": ext,
        "title": "Data Analyst",
        "url": f"https://example.com/j/{ext}",
        "posted_at": NOW,
        "description_raw": "<p>Full-time role.</p><ul><li>SQL</li><li>Python</li></ul>",
        "needs_resolve": False,
        "salary_raw": "$5,432.00 - $7,310.00 Monthly",
        "location_raw": "Olympia, WA",
        "agency_raw": "Dept of X",
    }
    upsert_stubs(conn, [JobStub(**{**base, **kw})], NOW)


def row(conn, ext="1"):
    return conn.execute("SELECT * FROM job WHERE external_id = ?", (ext,)).fetchone()


def test_fills_fields(conn):
    add(conn)
    assert normalize_pending(conn) == 1
    r = row(conn)
    assert r["stage"] == "normalized"
    assert r["description_text"] == "Full-time role.\n\n- SQL\n- Python"
    assert (r["salary_min"], r["salary_max"], r["salary_period"]) == (5432.0, 7310.0, "month")
    assert r["salary_stated"] == 1
    assert r["employment_type"] == "full_time"
    assert r["remote"] == "unknown"
    assert r["employer"] == "Dept of X"
    assert len(r["content_hash"]) == 64
    assert r["parse_warnings"] is None


def test_unparseable_salary_is_null_with_warning(conn):
    add(conn, salary_raw="DOE")
    normalize_pending(conn)
    r = row(conn)
    assert r["salary_min"] is None
    assert r["salary_max"] is None
    assert r["salary_period"] is None
    assert r["salary_stated"] == 0
    assert any("DOE" in w for w in json.loads(r["parse_warnings"]))


def test_skips_rows_without_description_and_other_stages(conn):
    add(conn, "1", description_raw=None, needs_resolve=True)
    add(conn, "2")
    conn.execute("UPDATE job SET stage = 'scored' WHERE external_id = '2'")
    assert normalize_pending(conn) == 0
    assert row(conn)["stage"] == "listed"
    assert row(conn, "2")["description_text"] is None


def test_resolved_stage_is_processed_and_limit(conn):
    add(conn, "1")
    add(conn, "2")
    conn.execute("UPDATE job SET stage = 'resolved' WHERE external_id = '1'")
    assert normalize_pending(conn, limit=1) == 1
    assert row(conn, "1")["stage"] == "normalized"
    assert row(conn, "2")["stage"] == "listed"


def test_idempotent(conn):
    add(conn, salary_raw="DOE")
    normalize_pending(conn)
    first = dict(row(conn))
    assert normalize_pending(conn) == 0  # nothing pending
    assert normalize_pending(conn, force=True) == 1
    assert dict(row(conn)) == first  # warnings replaced, not duplicated


def test_raw_columns_untouched(conn):
    add(conn)
    before = {c: row(conn)[c] for c in RAW_COLS}
    normalize_pending(conn)
    normalize_pending(conn, force=True)
    assert {c: row(conn)[c] for c in RAW_COLS} == before


def test_force_picks_up_changes_and_keeps_stage(conn):
    add(conn)
    normalize_pending(conn)
    conn.execute(
        "UPDATE job SET salary_raw = '$85k-110k/yr', stage = 'scored' WHERE external_id = '1'"
    )
    assert normalize_pending(conn) == 0
    assert row(conn)["salary_period"] == "month"
    assert normalize_pending(conn, force=True) == 1
    r = row(conn)
    assert (r["salary_min"], r["salary_period"]) == (85000.0, "year")
    assert r["stage"] == "scored"


def test_adapter_warnings_preserved(conn):
    add(conn, salary_raw="DOE")
    conn.execute("UPDATE job SET parse_warnings = '[\"adapter: odd markup\"]'")
    normalize_pending(conn)
    normalize_pending(conn, force=True)
    warns = json.loads(row(conn)["parse_warnings"])
    assert warns[0] == "adapter: odd markup"
    assert len(warns) == 2


def test_free_text_dates_parsed_with_source_tz(conn):
    add(conn)
    conn.execute("UPDATE job SET posted_at = '2026-10-03 08:00', closes_at = 'garbage'")
    normalize_pending(conn, source_timezones={"wa": "America/Los_Angeles"})
    r = row(conn)
    assert r["posted_at"] == "2026-10-03T15:00:00+00:00"
    assert r["closes_at"] == "garbage"
    assert any("closes_at" in w for w in json.loads(r["parse_warnings"]))


def test_remote_detected_and_adapter_employment_kept(conn):
    add(conn, title="Analyst (Remote)", description_raw="<p>Do things</p>")
    conn.execute("UPDATE job SET employment_type = 'contract'")
    normalize_pending(conn)
    r = row(conn)
    assert r["remote"] == "remote"
    assert r["employment_type"] == "contract"
