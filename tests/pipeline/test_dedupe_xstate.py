import logging
from datetime import UTC, datetime

import pytest
from typer.testing import CliRunner

from jobhunter.pipeline.dedupe import _refresh_group, normalize_employer
from jobhunter.pipeline.dedupe_xstate import merge_cross_state, normalize_title

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
ISO = NOW.isoformat()

WORDS = [f"w{i}" for i in range(400)]


def desc(start=0, n=200):
    return " ".join(WORDS[start : start + n])


@pytest.mark.parametrize(
    "name",
    ["Cribl", "Cribl, Inc", "CRIBL, INC.", "cribl inc", "Cribl Inc.", "Cribl, Incorporated"],
)
def test_employer_variants_normalize_identically(name):
    assert normalize_employer(name) == "cribl"


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Acme LLC", "ACME, L.L.C."),
        ("Acme Corp.", "Acme Corporation"),
        ("Acme Co", "acme company"),
        ("Acme Ltd", "ACME LIMITED"),
        ("O'Brien Inc", "OBrien, Inc."),
    ],
)
def test_employer_suffix_folding(a, b):
    assert normalize_employer(a) == normalize_employer(b) is not None


def test_employer_other_names_stay_distinct():
    assert normalize_employer("Cribl") != normalize_employer("Crible")
    assert normalize_employer("Inc.") is None


def test_normalize_title():
    assert normalize_title("Staff Software Engineer, Agentic Tools") == (
        "staff software engineer agentic tools"
    )
    assert normalize_title("  ") is None


def mkgroup(conn, created="2026-09-01T00:00:00+00:00", method="exact_hash"):
    return conn.execute(
        "INSERT INTO job_group (canonical_job_id, member_count, method, confidence, created_at) "
        "VALUES (NULL, 1, ?, 1.0, ?)",
        (method, created),
    ).lastrowid


def mkjob(conn, ext, *, state, gid, employer="Cribl", title="Staff Engineer", text=None, **kw):
    jid = conn.execute(
        "INSERT INTO job (source_key, external_id, url, title, employer, description_text, "
        "description_completeness, apply_url, job_group_id, stage, location_scope, "
        "first_seen_at, last_seen_at) VALUES ('wa', ?, ?, ?, ?, ?, 'full', ?, ?, 'grouped', "
        "?, ?, ?)",
        (
            ext,
            f"https://example.com/{ext}",
            title,
            employer,
            text if text is not None else desc(),
            kw.get("apply_url"),
            gid,
            kw.get("scope", "single"),
            ISO,
            ISO,
        ),
    ).lastrowid
    conn.execute(
        "INSERT INTO job_locations (job_id, state, is_primary) VALUES (?, ?, 1)", (jid, state)
    )
    _refresh_group(conn, gid)
    return jid


def copies(conn, states, **kw):
    out = []
    for i, st in enumerate(states):
        g = mkgroup(conn, created=f"2026-09-0{i + 1}T00:00:00+00:00")
        out.append((g, mkjob(conn, f"j-{st}", state=st, gid=g, **kw)))
    return out


def ngroups(conn):
    return conn.execute("SELECT COUNT(*) FROM job_group").fetchone()[0]


def test_five_state_copies_merge_into_multi_state(conn):
    cs = copies(conn, ["MI", "OH", "WA", "KY", "UT"], employer="Cribl, Inc")
    # vary employer spelling and truncate one copy's description
    conn.execute("UPDATE job SET employer = 'CRIBL, INC.' WHERE external_id = 'j-OH'")
    conn.execute("UPDATE job SET description_text = ? WHERE external_id = 'j-WA'", (desc(0, 180),))
    res = merge_cross_state(conn, now=NOW)
    assert res.groups_merged == 4 and res.would_merge == 4
    assert ngroups(conn) == 1
    survivor = cs[0][0]
    rows = conn.execute("SELECT job_group_id, location_scope FROM job").fetchall()
    assert {r[0] for r in rows} == {survivor}
    assert {r[1] for r in rows} == {"multi_state"}
    states = {r[0] for r in conn.execute("SELECT state FROM job_locations")}
    assert states == {"MI", "OH", "WA", "KY", "UT"}
    assert conn.execute("SELECT member_count FROM job_group").fetchone()[0] == 5


def test_same_title_different_description_not_merged(conn):
    g1, g2 = mkgroup(conn), mkgroup(conn)
    mkjob(conn, "a", state="MI", gid=g1, text=desc(0, 200))
    mkjob(conn, "b", state="OH", gid=g2, text=desc(200, 200))
    res = merge_cross_state(conn, now=NOW)
    assert res.would_merge == 0 and ngroups(conn) == 2


def test_different_employers_not_merged(conn):
    g1, g2 = mkgroup(conn), mkgroup(conn)
    mkjob(conn, "a", state="MI", gid=g1, employer="Cribl")
    mkjob(conn, "b", state="OH", gid=g2, employer="Other Co")
    assert merge_cross_state(conn, now=NOW).would_merge == 0


def test_shared_apply_url_merges_despite_different_text(conn):
    url = "https://boards.greenhouse.io/acme/jobs/4012345"
    g1, g2 = mkgroup(conn), mkgroup(conn)
    mkjob(conn, "a", state="MI", gid=g1, text=desc(0, 100), apply_url=url)
    mkjob(conn, "b", state="OH", gid=g2, text=desc(200, 100), apply_url=url + "#app")
    assert merge_cross_state(conn, now=NOW).groups_merged == 1


def test_remote_member_makes_group_remote_us(conn):
    cs = copies(conn, ["MI", "OH"])
    conn.execute("UPDATE job SET location_scope = 'remote_us' WHERE id = ?", (cs[1][1],))
    merge_cross_state(conn, now=NOW)
    scopes = {r[0] for r in conn.execute("SELECT location_scope FROM job")}
    assert scopes == {"remote_us"}


def test_labels_applications_scores_preserved(conn):
    (g1, _), (g2, _), (g3, _) = copies(conn, ["MI", "OH", "WA"])
    conn.execute("INSERT INTO label VALUES (?, 'applied', 'n', ?)", (g2, ISO))
    conn.execute("INSERT INTO label VALUES (?, 'interesting', NULL, ?)", (g3, ISO))
    conn.execute(
        "INSERT INTO application (id, job_group_id, status, created_at, updated_at) "
        "VALUES (1, ?, 'interview', ?, ?)",
        (g2, ISO, ISO),
    )
    for gid, tier, overall in ((g1, "screen", 50), (g3, "screen", 50), (g2, "deep", 70)):
        conn.execute(
            "INSERT INTO fit_score (job_group_id, tier, model, prompt_version, scoring_version, "
            "verdict, overall, dimensions, evidence, created_at) "
            "VALUES (?, ?, 'm', 'p1', 's1', 'possible', ?, '{}', '[]', ?)",
            (gid, tier, overall, ISO),
        )
    res = merge_cross_state(conn, now=NOW)
    assert res.groups_merged == 2
    row = conn.execute("SELECT job_group_id, label FROM label").fetchone()
    assert (row[0], row[1]) == (g1, "applied")
    assert conn.execute("SELECT job_group_id FROM application").fetchone()[0] == g1
    tiers = sorted(r[0] for r in conn.execute("SELECT tier FROM fit_score"))
    assert tiers == ["deep", "screen"]  # duplicate screen score dropped, deep moved
    assert res.scores_dropped == 1 and res.scores_moved == 1


def test_manual_group_untouched(conn):
    cs = copies(conn, ["MI", "OH", "WA"])
    manual_gid, manual_jid = cs[1]
    conn.execute("UPDATE job_group SET method = 'manual' WHERE id = ?", (manual_gid,))
    res = merge_cross_state(conn, now=NOW)
    assert res.groups_merged == 1
    method = conn.execute("SELECT method FROM job_group WHERE id = ?", (manual_gid,)).fetchone()[0]
    assert method == "manual"
    got = conn.execute("SELECT job_group_id FROM job WHERE id = ?", (manual_jid,)).fetchone()[0]
    assert got == manual_gid


def test_dry_run_writes_nothing(conn):
    copies(conn, ["MI", "OH", "WA"])
    before = conn.total_changes
    res = merge_cross_state(conn, now=NOW, dry_run=True)
    assert res.would_merge == 2 and res.groups_merged == 0
    assert conn.total_changes == before and ngroups(conn) == 3


def test_oversized_block_skipped_with_warning(conn, caplog):
    copies(conn, ["MI", "OH", "WA"])
    with caplog.at_level(logging.WARNING):
        res = merge_cross_state(conn, now=NOW, max_block=2)
    assert res.blocks_skipped == 1 and res.would_merge == 0 and ngroups(conn) == 3
    assert "skipping block" in caplog.text


def test_cli_dry_run_reports_and_does_not_write(tmp_path, monkeypatch):
    from jobhunter import cli
    from jobhunter.core import db

    path = tmp_path / "t.db"
    c = db.connect(path)
    db.migrate(c)
    c.execute(
        "INSERT INTO source (key, class, name, family, tier, entry, policy) "
        "VALUES ('wa', 'B', 'WA', 'neogov', 'http', 'https://example.com', 'enabled')"
    )
    copies(c, ["MI", "OH"])
    c.commit()
    c.close()
    cfg = tmp_path / "c.toml"
    cfg.write_text(f'[paths]\ndb_path = "{path}"\n')
    monkeypatch.setenv("JOBHUNTER_CONFIG", str(cfg))
    r = CliRunner().invoke(cli.app, ["dedupe", "--cross-state", "--dry-run"])
    assert r.exit_code == 0, r.output
    assert "would merge 1 groups" in r.output
    c = db.connect(path)
    assert c.execute("SELECT COUNT(*) FROM job_group").fetchone()[0] == 2
    c.close()
    r = CliRunner().invoke(cli.app, ["dedupe", "--cross-state"])
    assert r.exit_code == 0 and "merged 1 groups" in r.output


@pytest.mark.parametrize(
    ("u1", "u2"),
    [
        ("https://www.usajobs.gov/job/887826000", "https://www.usajobs.gov/job/887833200"),
        ("https://boards.greenhouse.io/acme/jobs/1", "https://boards.greenhouse.io/acme/jobs/2"),
    ],
)
def test_distinct_requisitions_on_same_host_do_not_merge(conn, u1, u2):
    mkjob(conn, "a", state="TX", gid=mkgroup(conn), apply_url=u1)
    mkjob(conn, "b", state="OH", gid=mkgroup(conn), apply_url=u2)
    assert merge_cross_state(conn, now=NOW).would_merge == 0


def test_cross_host_and_wrapper_apply_urls_still_merge(conn):
    urls = [
        "https://jobs.utah.gov/jsp/utjobs/single-job?j=11129818",
        "http://jobseeker.ohiomeansjobs.monster.com/jobview/GetJob.aspx?JobId=2",
        "https://www.aplitrak.com/?adid=AAA",
        "https://www.aplitrak.com/?adid=BBB",
    ]
    for i, u in enumerate(urls):
        mkjob(conn, f"u{i}", state="MI", gid=mkgroup(conn), apply_url=u)
    assert merge_cross_state(conn, now=NOW).would_merge == 3


def test_chain_cannot_bridge_distinct_requisitions(conn):
    # a and c are distinct postings on one host; b (no apply URL) is similar to both.
    mkjob(conn, "a", state="TX", gid=mkgroup(conn), apply_url="https://www.usajobs.gov/job/1")
    mkjob(conn, "b", state="OH", gid=mkgroup(conn))
    mkjob(conn, "c", state="CA", gid=mkgroup(conn), apply_url="https://www.usajobs.gov/job/2")
    assert merge_cross_state(conn, now=NOW).would_merge == 1
