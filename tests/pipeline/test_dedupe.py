from datetime import UTC, datetime, timedelta

from jobhunter.pipeline.dedupe import (
    group_members,
    group_pending,
    normalize_employer,
    split_job,
)

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
DESC = " ".join(f"word{i}" for i in range(60))


def add(conn, ext, *, title="Data Analyst", employer="Dept of Revenue", state="WA", desc=DESC,
        posted=NOW, chash=None, completeness="full"):  # fmt: skip
    cur = conn.execute(
        "INSERT INTO job (source_key, external_id, url, title, employer, description_text, "
        "description_completeness, content_hash, posted_at, stage, first_seen_at, last_seen_at) "
        "VALUES ('wa', ?, ?, ?, ?, ?, ?, ?, ?, 'normalized', ?, ?)",
        (ext, f"https://example.com/{ext}", title, employer, desc, completeness, chash,
         posted.isoformat(), NOW.isoformat(), NOW.isoformat()),
    )  # fmt: skip
    jid = cur.lastrowid
    if state:
        conn.execute(
            "INSERT INTO job_locations (job_id, state, is_primary) VALUES (?, ?, 1)", (jid, state)
        )
    return jid


def grp(conn, jid):
    return conn.execute("SELECT job_group_id FROM job WHERE id = ?", (jid,)).fetchone()[0]


def test_normalize_employer():
    assert normalize_employer("Dept. of Revenue, Inc.") == normalize_employer("REVENUE LLC")
    assert normalize_employer(None) is None


def test_exact_hash_pair_grouped(conn):
    a = add(conn, "1", chash="h", title="A", desc="x")
    b = add(conn, "2", chash="h", title="A", desc="x", employer="Other", state="OR")
    res = group_pending(conn, now=NOW)
    assert (res.created, res.joined, res.exact, res.near) == (1, 1, 1, 0)
    assert grp(conn, a) == grp(conn, b)
    g = conn.execute("SELECT * FROM job_group").fetchone()
    assert (g["member_count"], g["method"], g["confidence"]) == (2, "exact_hash", 1.0)
    assert conn.execute("SELECT stage FROM job WHERE id = ?", (a,)).fetchone()[0] == "grouped"


def test_near_duplicate_other_county_grouped(conn):
    a = add(conn, "1", title="Data Analyst 2", employer="Dept of Revenue", chash="a")
    conn.execute("UPDATE job_locations SET county='King' WHERE job_id = ?", (a,))
    b = add(
        conn, "2", title="Data Analyst 2 ", employer="Revenue, Department of", chash="b",
        desc=DESC + " extra", posted=NOW - timedelta(days=10),
    )  # fmt: skip
    conn.execute("UPDATE job_locations SET county='Pierce' WHERE job_id = ?", (b,))
    res = group_pending(conn, now=NOW)
    assert res.near == 1
    assert grp(conn, a) == grp(conn, b)
    g = conn.execute("SELECT * FROM job_group").fetchone()
    assert g["method"] == "near_dupe"
    assert 0.9 < g["confidence"] < 1.0
    assert g["member_count"] == 2


def test_same_title_different_agency_not_grouped(conn):
    a = add(conn, "1", chash="a")
    b = add(conn, "2", chash="b", employer="Dept of Transportation")
    group_pending(conn, now=NOW)
    assert grp(conn, a) != grp(conn, b)


def test_different_state_not_grouped(conn):
    a = add(conn, "1", chash="a")
    b = add(conn, "2", chash="b", state="OR")
    group_pending(conn, now=NOW)
    assert grp(conn, a) != grp(conn, b)


def test_similar_title_different_description_not_grouped(conn):
    a = add(conn, "1", chash="a")
    other = " ".join(f"other{i}" for i in range(60))
    b = add(conn, "2", chash="b", desc=other)
    group_pending(conn, now=NOW)
    assert grp(conn, a) != grp(conn, b)


def test_outside_window_not_grouped(conn):
    a = add(conn, "1", chash="a")
    b = add(conn, "2", chash="b", posted=NOW - timedelta(days=120))
    group_pending(conn, now=NOW)
    assert grp(conn, a) != grp(conn, b)


def test_canonical_prefers_full_over_partial(conn):
    stub = add(conn, "1", chash="h", completeness="partial", desc=DESC + " longer " * 50)
    full = add(conn, "2", chash="h", completeness="full", desc=DESC)
    group_pending(conn, now=NOW)
    g = conn.execute("SELECT * FROM job_group").fetchone()
    assert g["canonical_job_id"] == full
    assert [r["id"] for r in group_members(conn, g["id"])] == [full, stub]


def test_split_job_restores_counts(conn):
    a = add(conn, "1", chash="h")
    b = add(conn, "2", chash="h")
    group_pending(conn, now=NOW)
    old = grp(conn, a)
    new = split_job(conn, b, now=NOW)
    assert new != old and grp(conn, b) == new
    rows = {r["id"]: r for r in conn.execute("SELECT * FROM job_group")}
    assert rows[old]["member_count"] == 1 and rows[old]["canonical_job_id"] == a
    assert rows[new]["member_count"] == 1 and rows[new]["canonical_job_id"] == b
    assert rows[new]["method"] == "manual"
    assert conn.execute("SELECT COUNT(*) FROM job").fetchone()[0] == 2
    # splitting a lone member is a no-op
    assert split_job(conn, b, now=NOW) == new


def test_idempotent_second_run(conn):
    add(conn, "1", chash="h")
    add(conn, "2", chash="h")
    add(conn, "3", chash="c", title="Nurse", desc="nursing duties " * 10)
    group_pending(conn, now=NOW)
    before = [tuple(r) for r in conn.execute("SELECT * FROM job_group ORDER BY id")]
    res = group_pending(conn, now=NOW)
    assert (res.created, res.joined) == (0, 0)
    assert before == [tuple(r) for r in conn.execute("SELECT * FROM job_group ORDER BY id")]
