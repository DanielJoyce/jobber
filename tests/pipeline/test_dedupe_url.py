from datetime import UTC, datetime

import pytest

from jobhunter.core import db
from jobhunter.pipeline.dedupe_url import merge_by_apply_url, normalize_apply_url

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
ISO = NOW.isoformat()

WD = "https://acme.wd5.myworkdayjobs.com/en-US/External/job/Seattle-WA/Data-Analyst_R-1001"
GH = "https://boards.greenhouse.io/acme/jobs/4012345"
LEVER = "https://jobs.lever.co/acme/0f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b"
ASHBY = "https://jobs.ashbyhq.com/acme/0f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b"
ICIMS = "https://careers-acme.icims.com/jobs/12345"
NEOGOV = "https://www.governmentjobs.com/careers/kingcounty/jobs/4455667"
USAS = "https://apply.usastaffing.gov/ApplyStart/778899"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Workday: posting and apply URL compare equal; tracking params dropped
        (WD, WD),
        (WD + "/apply", WD),
        (WD + "/apply/applyManually?source=LinkedIn", WD),
        (
            "HTTPS://ACME.WD5.MYWORKDAYJOBS.COM:443"
            "/en-US/External/job/Seattle-WA/Data-Analyst_R-1001/",
            WD,
        ),
        # Greenhouse board
        (GH, GH),
        (GH + "#app", GH),
        (GH + "?gh_src=abc123&utm_source=x", GH),
        # Greenhouse embedded on an employer page: gh_jid IS the identity
        (
            "https://careers.acme.com/openings?utm_medium=e&gh_jid=4012345&gh_src=zz",
            "https://careers.acme.com/openings?gh_jid=4012345",
        ),
        # Lever / Ashby
        (LEVER + "/apply?lever-source=Indeed", LEVER),
        (LEVER + "/", LEVER),
        (ASHBY + "/application", ASHBY),
        (ASHBY + "?utm_campaign=c", ASHBY),
        # iCIMS: slug and iframe/mode params dropped
        (ICIMS + "/data-analyst/job?mode=job&iis=Indeed&iisn=Indeed&in_iframe=1", ICIMS),
        (ICIMS, ICIMS),
        # NEOGOV
        (NEOGOV + "/data-analyst/apply", NEOGOV),
        (NEOGOV + "/data-analyst", NEOGOV),
        (NEOGOV, NEOGOV),
        # USA Staffing (USAJOBS ApplyURI)
        (USAS, USAS),
        (USAS + "?utm_source=usajobs#top", USAS),
        # appcast-wrapped links unwrap to the destination
        (
            "https://click.appcast.io/track/abc?url=" + "https%3A%2F%2Fjobs.lever.co%2Facme%2F"
            "0f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b%2Fapply&jid=9",
            LEVER,
        ),
        ("https://click.appcast.io/t/x?url=" + WD.replace("/", "%2F").replace(":", "%3A"), WD),
        # non-ATS employer pages: keep identifying params, sort, drop tracking
        (
            "https://www.acme.org/careers/job?id=77&ref=nlx&utm_source=a&trk=t",
            "https://www.acme.org/careers/job?id=77",
        ),
        (
            "http://Jobs.Acme.org:8080/c/view?b=2&a=1#frag",
            "http://jobs.acme.org:8080/c/view?a=1&b=2",
        ),
        ("https://acme.org/jobs/42/", "https://acme.org/jobs/42"),
        ("", None),
        ("mailto:hr@example.com", None),
        (None, None),
    ],
)
def test_normalize_apply_url(raw, expected):
    assert normalize_apply_url(raw) == expected


def test_posting_and_apply_forms_equal_everywhere():
    for base in (WD, GH, LEVER, ASHBY, ICIMS, NEOGOV, USAS):
        assert normalize_apply_url(base) == normalize_apply_url(base + "?utm_source=z#app")


# ─── merge ─────────────────────────────────────────────────────────────────


def mkjob(conn, ext, *, gid, apply_url=None, desc="short", completeness="partial"):
    cur = conn.execute(
        "INSERT INTO job (source_key, external_id, url, title, employer, description_text, "
        "description_completeness, apply_url, job_group_id, stage, first_seen_at, last_seen_at) "
        "VALUES ('wa', ?, ?, 'Analyst', 'Acme', ?, ?, ?, ?, 'grouped', ?, ?)",
        (ext, f"https://example.com/{ext}", desc, completeness, apply_url, gid, ISO, ISO),
    )
    return cur.lastrowid


def mkgroup(conn, created="2026-09-01T00:00:00+00:00", method="exact_hash"):
    cur = conn.execute(
        "INSERT INTO job_group (canonical_job_id, member_count, method, confidence, created_at) "
        "VALUES (NULL, 1, ?, 1.0, ?)",
        (method, created),
    )
    return cur.lastrowid


def group_of(conn, jid):
    return conn.execute("SELECT job_group_id FROM job WHERE id = ?", (jid,)).fetchone()[0]


def n_groups(conn):
    return conn.execute("SELECT COUNT(*) FROM job_group").fetchone()[0]


def test_same_workday_job_via_different_wrappers_merge(conn):
    g1, g2 = mkgroup(conn), mkgroup(conn, "2026-09-02T00:00:00+00:00")
    wrapped = "https://click.appcast.io/t/x?url=" + (WD + "/apply").replace("/", "%2F").replace(
        ":", "%3A"
    )
    j1 = mkjob(conn, "a", gid=g1, apply_url=WD + "?source=nlx")
    j2 = mkjob(conn, "b", gid=g2, apply_url=wrapped, desc="a much longer full description",
               completeness="full")  # fmt: skip
    res = merge_by_apply_url(conn, now=NOW)
    assert (res.keys, res.groups_merged, res.jobs_moved) == (1, 1, 1)
    assert group_of(conn, j1) == group_of(conn, j2) == g1
    g = conn.execute("SELECT * FROM job_group").fetchone()
    assert (g["id"], g["method"], g["confidence"], g["member_count"]) == (g1, "apply_url", 1.0, 2)
    assert g["canonical_job_id"] == j2  # full description wins
    assert conn.execute("SELECT COUNT(*) FROM job").fetchone()[0] == 2  # non-destructive


def test_nlx_and_usajobs_same_apply_uri_merge(conn):
    g1, g2 = mkgroup(conn), mkgroup(conn)
    mkjob(conn, "nlx1", gid=g1, apply_url=USAS + "?utm_source=nlx")
    mkjob(conn, "usajobs1", gid=g2, apply_url=USAS)
    assert merge_by_apply_url(conn, now=NOW).groups_merged == 1
    assert n_groups(conn) == 1


def test_different_jobs_same_ats_do_not_merge(conn):
    g1, g2, g3 = mkgroup(conn), mkgroup(conn), mkgroup(conn)
    mkjob(conn, "a", gid=g1, apply_url=WD)
    mkjob(conn, "b", gid=g2, apply_url=WD.replace("R-1001", "R-1002"))
    mkjob(conn, "c", gid=g3, apply_url="https://boards.greenhouse.io/acme")  # board home
    mkjob(conn, "d", gid=g3, apply_url="https://boards.greenhouse.io/acme/")
    mkjob(conn, "e", gid=mkgroup(conn), apply_url="https://boards.greenhouse.io/acme")
    res = merge_by_apply_url(conn, now=NOW)
    assert res.groups_merged == 0
    assert n_groups(conn) == 4


def test_embedded_greenhouse_gh_jid_distinguishes(conn):
    g1, g2 = mkgroup(conn), mkgroup(conn)
    mkjob(conn, "a", gid=g1, apply_url="https://careers.acme.com/open?gh_jid=1")
    mkjob(conn, "b", gid=g2, apply_url="https://careers.acme.com/open?gh_jid=2")
    assert merge_by_apply_url(conn, now=NOW).groups_merged == 0


def test_resolved_final_url_triggers_merge_and_prefers_live_link(conn):
    g1, g2 = mkgroup(conn), mkgroup(conn)
    mkjob(conn, "a", gid=g1, apply_url="https://t.example/redirect/1")
    mkjob(conn, "b", gid=g2, apply_url="https://t.example/redirect/2")
    for gid, status in ((g1, "unresolved"), (g2, "live")):
        conn.execute(
            "INSERT INTO apply_link (job_group_id, start_url, final_url, chain, status, "
            "resolved_at) VALUES (?, 's', ?, '[]', ?, ?)",
            (gid, LEVER if status == "live" else None, status, ISO),
        )
    # g1 unresolved has no final_url, so no key yet
    assert merge_by_apply_url(conn, now=NOW).groups_merged == 0
    conn.execute(
        "UPDATE apply_link SET final_url = ?, status = 'expired' WHERE job_group_id = ?",
        (LEVER + "/apply", g1),
    )
    assert merge_by_apply_url(conn, now=NOW).groups_merged == 1
    row = conn.execute("SELECT * FROM apply_link").fetchall()
    assert len(row) == 1 and (row[0]["job_group_id"], row[0]["status"]) == (g1, "live")


def test_manual_split_groups_are_not_remerged(conn):
    g1, g2 = mkgroup(conn), mkgroup(conn, method="manual")
    mkjob(conn, "a", gid=g1, apply_url=GH)
    mkjob(conn, "b", gid=g2, apply_url=GH)
    assert merge_by_apply_url(conn, now=NOW).groups_merged == 0


def test_merge_preserves_labels_and_applications(conn):
    g1, g2 = mkgroup(conn), mkgroup(conn)
    mkjob(conn, "a", gid=g1, apply_url=GH)
    mkjob(conn, "b", gid=g2, apply_url=GH + "#app")
    conn.execute("INSERT INTO label VALUES (?, 'interesting', 'n1', ?)", (g1, ISO))
    conn.execute("INSERT INTO label VALUES (?, 'applied', 'n2', ?)", (g2, ISO))
    conn.execute(
        "INSERT INTO application (id, job_group_id, status, created_at, updated_at) "
        "VALUES (1, ?, 'preparing', ?, ?)",
        (g1, ISO, ISO),
    )
    conn.execute(
        "INSERT INTO application (id, job_group_id, status, applied_at, external_ref, "
        "created_at, updated_at) VALUES (2, ?, 'interview', ?, 'ref-9', ?, ?)",
        (g2, ISO, ISO, ISO),
    )
    conn.execute(
        "INSERT INTO application_event (application_id, at, status) VALUES (1, ?, 'preparing')",
        (ISO,),
    )
    conn.execute(
        "INSERT INTO contact (application_id, name) VALUES (1, 'Pat')",
    )
    res = merge_by_apply_url(conn, now=NOW)
    assert (res.labels_merged, res.applications_merged) == (1, 1)
    lab = conn.execute("SELECT * FROM label").fetchall()
    assert len(lab) == 1 and lab[0]["label"] == "applied" and lab[0]["job_group_id"] == g1
    assert "merged label interesting (n1)" in lab[0]["note"]
    apps = conn.execute("SELECT * FROM application").fetchall()
    assert len(apps) == 1
    app = apps[0]
    assert (app["id"], app["status"], app["job_group_id"], app["external_ref"]) == (
        2, "interview", g1, "ref-9",
    )  # fmt: skip
    events = conn.execute("SELECT * FROM application_event WHERE application_id = 2").fetchall()
    assert {e["status"] for e in events} == {"preparing", "interview"}
    assert any("Merged duplicate application" in (e["note"] or "") for e in events)
    assert conn.execute("SELECT application_id FROM contact").fetchone()[0] == 2


def test_label_only_on_absorbed_group_moves(conn):
    g1, g2 = mkgroup(conn), mkgroup(conn)
    mkjob(conn, "a", gid=g1, apply_url=GH)
    mkjob(conn, "b", gid=g2, apply_url=GH)
    conn.execute("INSERT INTO label VALUES (?, 'not_interesting', NULL, ?)", (g2, ISO))
    merge_by_apply_url(conn, now=NOW)
    assert conn.execute("SELECT job_group_id FROM label").fetchone()[0] == g1


def _score(conn, gid, model, overall):
    conn.execute(
        "INSERT INTO fit_score (job_group_id, tier, model, prompt_version, scoring_version, "
        "verdict, overall, dimensions, evidence, created_at) "
        "VALUES (?, 'screen', ?, 'p1', 's1', 'possible', ?, '{}', '[]', ?)",
        (gid, model, overall, ISO),
    )


def test_fit_score_conflict_handling(conn):
    g1, g2 = mkgroup(conn), mkgroup(conn)
    mkjob(conn, "a", gid=g1, apply_url=GH)
    mkjob(conn, "b", gid=g2, apply_url=GH)
    _score(conn, g1, "m-a", 70)  # survivor has it
    _score(conn, g2, "m-a", 10)  # same key: dropped
    _score(conn, g2, "m-b", 55)  # different model: re-pointed
    res = merge_by_apply_url(conn, now=NOW)
    assert (res.scores_dropped, res.scores_moved) == (1, 1)
    rows = {
        r["model"]: (r["job_group_id"], r["overall"])
        for r in conn.execute("SELECT * FROM fit_score")
    }
    assert rows == {"m-a": (g1, 70), "m-b": (g1, 55)}


def test_idempotent_rerun_and_chain_merge(conn):
    g1, g2, g3 = mkgroup(conn), mkgroup(conn), mkgroup(conn)
    mkjob(conn, "a", gid=g1, apply_url=GH)
    mkjob(conn, "b", gid=g2, apply_url=GH)
    mkjob(conn, "b2", gid=g2, apply_url=LEVER)
    mkjob(conn, "c", gid=g3, apply_url=LEVER + "/apply")
    first = merge_by_apply_url(conn, now=NOW)
    assert first.groups_merged == 2 and n_groups(conn) == 1
    second = merge_by_apply_url(conn, now=NOW)
    assert vars(second) == vars(type(second)())
    assert n_groups(conn) == 1
    assert conn.execute("SELECT member_count FROM job_group").fetchone()[0] == 4


# ─── migration ─────────────────────────────────────────────────────────────


def test_migration_0007_preserves_rows_fks_and_allows_apply_url(monkeypatch):
    real = db._load_migrations
    monkeypatch.setattr(db, "_load_migrations", lambda: [m for m in real() if m[0] < 7])
    c = db.connect(":memory:")
    db.migrate(c)
    c.execute(
        "INSERT INTO source (key, class, name, family, tier, entry, policy) "
        "VALUES ('wa', 'B', 'WA', 'neogov', 'http', 'https://example.com', 'enabled')"
    )
    gid = mkgroup(c, method="near_dupe")
    jid = mkjob(c, "a", gid=gid)
    c.execute(
        "UPDATE job_group SET canonical_job_id = ?, member_count = 1 WHERE id = ?", (jid, gid)
    )
    c.execute("INSERT INTO label VALUES (?, 'interesting', NULL, ?)", (gid, ISO))
    c.execute(
        "INSERT INTO apply_link (job_group_id, start_url, chain, status, resolved_at) "
        "VALUES (?, 's', '[]', 'live', ?)",
        (gid, ISO),
    )
    with pytest.raises(Exception, match="CHECK"):
        c.execute(
            "INSERT INTO job_group (member_count, method, created_at) VALUES (1, 'apply_url', 'x')"
        )
    before = dict(c.execute("SELECT * FROM job_group").fetchone())

    monkeypatch.setattr(db, "_load_migrations", real)
    assert db.migrate(c) == [v for v, _, _ in real() if v >= 7]

    assert dict(c.execute("SELECT * FROM job_group").fetchone()) == before
    assert c.execute("PRAGMA foreign_key_check").fetchall() == []
    assert c.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    c.execute(
        "INSERT INTO job_group (member_count, method, created_at) VALUES (1, 'apply_url', 'x')"
    )
    fks = {r["table"] for r in c.execute("PRAGMA foreign_key_list(job)")}
    assert "job_group" in fks
    with pytest.raises(Exception, match="FOREIGN KEY"):
        c.execute("INSERT INTO label VALUES (9999, 'applied', NULL, ?)", (ISO,))
    assert c.execute("SELECT COUNT(*) FROM label").fetchone()[0] == 1
    c.close()
