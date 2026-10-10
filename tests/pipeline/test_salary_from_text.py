"""Salary from description text: normalize wiring, backfill idempotency, comp fit (specs/003)."""

from datetime import UTC, datetime

from jobhunter.core.models import JobStub
from jobhunter.pipeline.listing import upsert_stubs
from jobhunter.pipeline.normalize import backfill_salary, normalize_pending
from jobhunter.scoring.buckets import comp_fit
from jobhunter.scoring.profile import Profile

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
DESC = "<p>Great role.</p><p>The pay range for this position is $70.00 - $90.00/hr.</p>"


def add(conn, ext, salary_raw=None, desc=DESC):
    upsert_stubs(
        conn,
        [
            JobStub(
                source_key="wa",
                external_id=ext,
                title="Analyst",
                url=f"https://example.com/{ext}",
                posted_at=NOW,
                description_raw=desc,
                needs_resolve=False,
                salary_raw=salary_raw,
                location_raw="Olympia, WA",
            )
        ],
        NOW,
    )


def row(conn, ext):
    return conn.execute("SELECT * FROM job WHERE external_id = ?", (ext,)).fetchone()


def test_text_fills_when_structured_empty(conn):
    add(conn, "1")
    normalize_pending(conn)
    r = row(conn, "1")
    assert (r["salary_min"], r["salary_max"], r["salary_period"]) == (70, 90, "hour")
    assert (r["salary_stated"], r["salary_source"]) == (1, "text")
    assert "$70.00 - $90.00/hr" in r["salary_raw"]


def test_structured_wins(conn):
    add(conn, "2", salary_raw="$5,432.00 - $7,310.00 Monthly")
    normalize_pending(conn)
    r = row(conn, "2")
    assert (r["salary_min"], r["salary_period"], r["salary_source"]) == (
        5432,
        "month",
        "structured",
    )
    assert r["salary_raw"] == "$5,432.00 - $7,310.00 Monthly"


def test_no_salary_stays_unstated(conn):
    add(conn, "3", desc="<p>We raised $1,000,000 in funding. $5,000 sign-on bonus.</p>")
    normalize_pending(conn)
    r = row(conn, "3")
    assert (r["salary_stated"], r["salary_source"], r["salary_min"]) == (0, None, None)


def test_renormalize_force_idempotent(conn):
    add(conn, "1")
    normalize_pending(conn)
    first = dict(row(conn, "1"))
    normalize_pending(conn, force=True)
    assert dict(row(conn, "1")) == first


def test_backfill_idempotent_with_counts(conn):
    add(conn, "1")
    add(conn, "2", salary_raw="$100,000 - $120,000 per year")
    add(conn, "3", desc="<p>Nothing here.</p>")
    normalize_pending(conn)
    conn.execute(  # as if normalized before this feature existed
        "UPDATE job SET salary_min = NULL, salary_max = NULL, salary_period = NULL, "
        "salary_stated = 0, salary_source = NULL, salary_raw = NULL WHERE external_id = '1'"
    )
    before, after = backfill_salary(conn)
    assert before == {"structured": 1, "text": 0, "none": 2}
    assert after == {"structured": 1, "text": 1, "none": 1}
    snap = [dict(r) for r in conn.execute("SELECT * FROM job ORDER BY id")]
    b2, a2 = backfill_salary(conn)
    assert b2 == a2 == after
    assert [dict(r) for r in conn.execute("SELECT * FROM job ORDER BY id")] == snap


def test_comp_fit_uses_text_salary(conn):
    add(conn, "1")
    normalize_pending(conn)
    r = row(conn, "1")
    profile = Profile.model_validate(
        {"hard": {"salary_floor": {"amount": 100000, "period": "year"}}}
    )
    assert comp_fit(None, None, None, False, profile) is None  # the old "not stated" outcome
    got = comp_fit(
        r["salary_min"], r["salary_max"], r["salary_period"], bool(r["salary_stated"]), profile
    )
    assert got == 100  # $90/hr = $187k annual, above the 1.2x target


def test_text_salary_never_loses_the_structured_raw(conn):
    # A board's own "DOE" yields no numbers, so pay comes from the description; "DOE" must
    # survive later re-normalizes and come back when the description no longer states pay.
    add(conn, "1", salary_raw="DOE")
    normalize_pending(conn)
    r = row(conn, "1")
    assert (r["salary_source"], r["salary_min"]) == ("text", 70)
    normalize_pending(conn, force=True)
    assert row(conn, "1")["salary_source"] == "text"
    conn.execute("UPDATE job SET description_raw = '<p>Great role.</p>' WHERE external_id = '1'")
    normalize_pending(conn, force=True)
    r = row(conn, "1")
    assert (r["salary_raw"], r["salary_source"], r["salary_min"]) == ("DOE", None, None)


def test_backfill_keeps_the_structured_raw(conn):
    add(conn, "1", salary_raw="Competitive")
    normalize_pending(conn)
    backfill_salary(conn)
    assert row(conn, "1")["salary_source"] == "text"
    conn.execute("UPDATE job SET description_text = 'Great role.' WHERE external_id = '1'")
    backfill_salary(conn)
    r = row(conn, "1")
    assert (r["salary_raw"], r["salary_source"]) == ("Competitive", None)
