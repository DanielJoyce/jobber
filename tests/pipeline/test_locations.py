import json
from datetime import UTC, datetime

from jobhunter.core.models import JobLocation, JobStub, LocationScope
from jobhunter.pipeline.listing import upsert_stubs
from jobhunter.pipeline.locations import (
    apply_locations,
    derive_locations,
    jobs_in_state,
    location_summary,
)
from jobhunter.pipeline.normalize import normalize_pending

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)

USAJOBS_14 = [
    ("Denver", "CO"),
    ("Fort Collins", "CO"),
    ("Austin", "TX"),
    ("Dallas", "TX"),
    ("Seattle", "WA"),
    ("Portland", "OR"),
    ("Boise", "ID"),
    ("Salt Lake City", "UT"),
    ("Phoenix", "AZ"),
    ("Tucson", "AZ"),
    ("Albuquerque", "NM"),
    ("Chicago", "IL"),
    ("Atlanta", "GA"),
    ("Washington", "DC"),
]


def add(conn, ext="1", locations=(), **kw):
    base = {
        "source_key": "wa",
        "external_id": ext,
        "title": "Data Analyst",
        "url": f"https://example.com/j/{ext}",
        "posted_at": NOW,
        "description_raw": "<p>Full-time role.</p>",
        "needs_resolve": False,
        "location_raw": "Olympia, WA",
        "locations": list(locations),
    }
    upsert_stubs(conn, [JobStub(**{**base, **kw})], NOW)


def state_city(conn, ext="1"):
    jid = conn.execute("SELECT id FROM job WHERE external_id = ?", (ext,)).fetchone()[0]
    rows = conn.execute("SELECT * FROM job_locations WHERE job_id = ? ORDER BY id", (jid,))
    return [(r["state"], r["city"], r["is_primary"]) for r in rows]


def scope(conn, ext="1"):
    row = conn.execute("SELECT location_scope FROM job WHERE external_id = ?", (ext,)).fetchone()
    return row[0]


def warnings(conn, ext="1"):
    row = conn.execute("SELECT parse_warnings FROM job WHERE external_id = ?", (ext,)).fetchone()
    return json.loads(row[0]) if row[0] else []


def test_single_city(conn):
    add(conn)
    normalize_pending(conn)
    assert state_city(conn) == [("WA", "Olympia", 1)]
    assert scope(conn) == "single"
    assert warnings(conn) == []


def test_usajobs_fourteen_locations_nine_states(conn):
    locs = [JobLocation(city=c, state=s) for c, s in USAJOBS_14]
    add(conn, locations=[*locs, locs[0]], location_raw=None)  # duplicate dedupes
    normalize_pending(conn)
    rows = state_city(conn)
    assert len(rows) == 14
    assert sum(r[2] for r in rows) == 1 and rows[0][2] == 1
    assert scope(conn) == "multi_state"


def test_structured_full_state_names_normalized(conn):
    add(conn, locations=[JobLocation(city="Fort Carson, Colorado", state="Colorado")])
    normalize_pending(conn)
    assert state_city(conn) == [("CO", "Fort Carson, Colorado", 1)]


def test_nationwide_adds_null_row(conn):
    add(conn, location_raw="Denver, CO; Austin, TX; Anywhere in the U.S.")
    normalize_pending(conn)
    assert state_city(conn) == [("CO", "Denver", 1), ("TX", "Austin", 0), (None, None, 0)]
    assert scope(conn) == "nationwide"


def test_negotiable_text(conn):
    add(conn, location_raw="Denver, CO", description_raw="<p>Duty location negotiable.</p>")
    normalize_pending(conn)
    assert scope(conn) == "negotiable"
    assert (None, None, 0) in state_city(conn)


def test_remote(conn):
    add(conn, title="Data Analyst (Remote)", location_raw="Remote")
    normalize_pending(conn)
    assert scope(conn) == "remote_us"
    assert state_city(conn) == [(None, None, 1)]
    assert warnings(conn) == []


def test_overseas(conn):
    add(conn, location_raw="Frankfurt, Germany")
    normalize_pending(conn)
    assert scope(conn) == "overseas"
    assert state_city(conn) == [(None, "Frankfurt, Germany", 1)]


def test_installation_verbatim_city(conn):
    add(conn, location_raw="Fort Carson, Colorado")
    normalize_pending(conn)
    assert state_city(conn) == [("CO", "Fort Carson", 1)]
    assert scope(conn) == "single"


def test_dc_area_is_multi_state(conn):
    add(conn, location_raw="Washington, DC; Arlington, VA; Bethesda, MD")
    normalize_pending(conn)
    assert [r[0] for r in state_city(conn)] == ["DC", "VA", "MD"]
    assert scope(conn) == "multi_state"


def test_garbage_location_warns_and_falls_back(conn):
    conn.execute("UPDATE source SET state = 'WA' WHERE key = 'wa'")
    add(conn, location_raw="Narnia, Zz")
    normalize_pending(conn)
    assert state_city(conn) == [("WA", None, 1)]
    assert scope(conn) == "single"
    assert any(x.startswith("locations: unrecognized location") for x in warnings(conn))


def test_multiple_locations_falls_back_without_guessing(conn):
    conn.execute("UPDATE source SET state = 'WA' WHERE key = 'wa'")
    add(conn, location_raw="Multiple Locations")
    normalize_pending(conn)
    assert state_city(conn) == [("WA", None, 1)]
    assert any("fell back" in x for x in warnings(conn))


def test_no_source_state_is_unknown(conn):
    add(conn, location_raw="Narnia, Zz")
    normalize_pending(conn)
    assert state_city(conn) == []
    assert scope(conn) == "unknown"


def test_force_replaces_rows_and_keeps_other_warnings(conn):
    add(conn, location_raw="Denver, CO; Austin, TX")
    conn.execute("UPDATE job SET parse_warnings = ?", (json.dumps(["adapter: odd"]),))
    normalize_pending(conn)
    before = state_city(conn)
    assert apply_locations(conn) == 0  # nothing pending
    assert apply_locations(conn, force=True) == 1
    assert apply_locations(conn, force=True) == 1
    assert state_city(conn) == before
    assert warnings(conn) == ["adapter: odd"]


def test_listed_jobs_untouched(conn):
    add(conn, description_raw=None, needs_resolve=True)
    assert apply_locations(conn) == 0
    assert state_city(conn) == []


def test_jobs_in_state_include_remote(conn):
    add(conn, "co", location_raw="Denver, CO")
    add(conn, "multi", location_raw="Denver, CO; Austin, TX")
    add(conn, "tx", location_raw="Austin, TX")
    add(conn, "nat", location_raw="Nationwide")
    normalize_pending(conn)

    def ids(rows):
        return sorted(r["external_id"] for r in rows)

    assert ids(jobs_in_state(conn, "co")) == ["co", "multi"]
    assert ids(jobs_in_state(conn, "TX")) == ["multi", "tx"]
    assert ids(jobs_in_state(conn, "CO", include_remote=True)) == ["co", "multi", "nat"]


def test_location_summary():
    locs = [JobLocation(city=c, state=s) for c, s in USAJOBS_14]
    locs[0] = locs[0].model_copy(update={"is_primary": True})
    assert location_summary(locs, LocationScope.multi_state) == "Denver, CO +13 more"
    assert location_summary(locs[:1], "single") == "Denver, CO"
    assert location_summary([JobLocation()], "nationwide") == "Nationwide"
    assert location_summary([JobLocation()], LocationScope.remote_us) == "Remote (US)"
    assert location_summary([], "unknown") == "Unknown"


def test_derive_accepts_plain_mappings():
    rows, sc, w = derive_locations(
        {"title": "T", "location_raw": "Boise, Idaho", "description_text": "x"}, None, "WA"
    )
    assert [(r.state, r.city, r.is_primary) for r in rows] == [("ID", "Boise", True)]
    assert sc is LocationScope.single and w == []


def test_load_group_locations_unions_members(conn):
    from jobhunter.pipeline.locations import load_group_locations, load_job_group_locations

    gid = conn.execute(
        "INSERT INTO job_group (member_count, method, created_at) VALUES (2, 'near_dupe', 'x')"
    ).lastrowid
    ids = []
    for ext, state in (("a", "MI"), ("b", "OH"), ("c", "OH")):
        jid = conn.execute(
            "INSERT INTO job (source_key, external_id, url, title, job_group_id, stage, "
            "first_seen_at, last_seen_at) VALUES ('wa', ?, 'u', 't', ?, 'grouped', 'x', 'x')",
            (ext, gid),
        ).lastrowid
        conn.execute(
            "INSERT INTO job_locations (job_id, state, is_primary) VALUES (?, ?, 1)", (jid, state)
        )
        ids.append(jid)
    conn.execute("UPDATE job_group SET canonical_job_id = ? WHERE id = ?", (ids[1], gid))
    locs = load_group_locations(conn, gid)
    assert [loc.state for loc in locs] == ["OH", "MI"]  # canonical first, OH deduped
    assert [loc.is_primary for loc in locs] == [True, False]
    assert {loc.state for loc in load_job_group_locations(conn, ids[0])} == {"MI", "OH"}
    solo = conn.execute(
        "INSERT INTO job (source_key, external_id, url, title, stage, first_seen_at, "
        "last_seen_at) VALUES ('wa', 'z', 'u', 't', 'normalized', 'x', 'x')"
    ).lastrowid
    conn.execute(
        "INSERT INTO job_locations (job_id, state, is_primary) VALUES (?, 'TX', 1)", (solo,)
    )
    assert [loc.state for loc in load_job_group_locations(conn, solo)] == ["TX"]
