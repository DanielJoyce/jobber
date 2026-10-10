"""Assisted-apply packets survive the nightly group merges and the inbox undo (specs/017).

Packets hang off ``application``. A merge that deletes the losing application must first move
its packets to the winner (abandoning the loser's live one when the winner has its own), or the
DELETE fails on the foreign key and the whole ingest aborts, as the ``rejection`` FK did (#78).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from jobhunter.config import Settings
from jobhunter.console import inbox
from jobhunter.core import db
from jobhunter.pipeline import runner
from jobhunter.pipeline.dedupe import _refresh_group
from jobhunter.pipeline.dedupe_url import merge_by_apply_url
from jobhunter.pipeline.dedupe_xstate import merge_cross_state

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
ISO = NOW.isoformat()
GH = "https://boards.greenhouse.io/acme/jobs/4012345"
DESC = " ".join(f"word{i} builds reliable synthetic systems for the team" for i in range(40))


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.migrate(c)
    c.execute(
        "INSERT INTO source (key, class, name, family, tier, entry, policy) "
        "VALUES ('wa', 'B', 'WA', 'neogov', 'http', 'https://example.com', 'enabled')"
    )
    yield c
    c.close()


def group(conn, ext, *, created, state="WA", apply_url=None):
    gid = conn.execute(
        "INSERT INTO job_group (canonical_job_id, member_count, method, confidence, created_at) "
        "VALUES (NULL, 1, 'exact_hash', 1.0, ?)",
        (created,),
    ).lastrowid
    jid = conn.execute(
        "INSERT INTO job (source_key, external_id, url, title, employer, description_text, "
        "description_completeness, apply_url, job_group_id, stage, first_seen_at, last_seen_at) "
        "VALUES ('wa', ?, ?, 'Analyst', 'Acme', ?, 'full', ?, ?, 'grouped', ?, ?)",
        (ext, f"https://example.com/{ext}", DESC, apply_url, gid, ISO, ISO),
    ).lastrowid
    conn.execute(
        "INSERT INTO job_locations (job_id, state, is_primary) VALUES (?, ?, 1)", (jid, state)
    )
    _refresh_group(conn, gid)
    return gid


def application(conn, gid, status="preparing"):
    return conn.execute(
        "INSERT INTO application (job_group_id, status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?)",
        (gid, status, ISO, ISO),
    ).lastrowid


def packet(conn, app_id):
    pid = conn.execute(
        "INSERT INTO application_packet (application_id, created_at, updated_at) VALUES (?, ?, ?)",
        (app_id, ISO, ISO),
    ).lastrowid
    conn.execute(
        "INSERT INTO packet_document (packet_id, kind, version, origin, doc_json, body_md, "
        "check_report, created_at) VALUES (?, 'resume', 1, 'base', '{}', '', '{}', ?)",
        (pid, ISO),
    )
    conn.execute(
        "INSERT INTO packet_answer (packet_id, field_key, value, source) "
        "VALUES (?, 'notes:employer', 'n', 'user')",
        (pid,),
    )
    return pid


def packets(conn):
    return {
        r["id"]: (r["application_id"], r["status"])
        for r in conn.execute("SELECT * FROM application_packet")
    }


def apps(conn):
    return [r[0] for r in conn.execute("SELECT id FROM application ORDER BY id")]


def url_pair(conn):
    """Two groups sharing an apply URL: the older one (first) survives."""
    g1 = group(conn, "a", created="2026-09-01T00:00:00+00:00", apply_url=GH)
    g2 = group(conn, "b", created="2026-09-02T00:00:00+00:00", apply_url=GH)
    return g1, g2


def xstate_pair(conn):
    """The same posting on two state boards: merged by the cross-state pass."""
    g1 = group(conn, "a", created="2026-09-01T00:00:00+00:00", state="WA")
    g2 = group(conn, "b", created="2026-09-02T00:00:00+00:00", state="OR")
    return g1, g2


def merge_url(conn):
    return merge_by_apply_url(conn, now=NOW).groups_merged


def merge_x(conn):
    return merge_cross_state(conn, now=NOW).groups_merged


MERGES = [(url_pair, merge_url), (xstate_pair, merge_x)]


@pytest.mark.parametrize(("pair", "merge"), MERGES, ids=["url", "xstate"])
def test_packet_on_absorbed_side_moves_to_the_winner(conn, pair, merge):
    g1, g2 = pair(conn)
    a1, a2 = application(conn, g1, "interested"), application(conn, g2, "preparing")
    p2 = packet(conn, a2)
    assert merge(conn) == 1
    # a2 is more advanced, so it wins and a1 is deleted; either way the packet stays live
    (survivor,) = apps(conn)
    assert packets(conn) == {p2: (survivor, "draft")}
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert a1 != a2


@pytest.mark.parametrize(("pair", "merge"), MERGES, ids=["url", "xstate"])
def test_packet_on_losing_application_is_repointed(conn, pair, merge):
    g1, g2 = pair(conn)
    a1, a2 = application(conn, g1, "applied"), application(conn, g2, "preparing")
    p2 = packet(conn, a2)  # a2 loses (a1 is further along) and is deleted
    assert merge(conn) == 1
    assert apps(conn) == [a1]
    assert packets(conn) == {p2: (a1, "draft")}
    docs = conn.execute("SELECT packet_id FROM packet_document").fetchall()
    assert [d[0] for d in docs] == [p2]
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize(("pair", "merge"), MERGES, ids=["url", "xstate"])
def test_packet_on_winning_side_stays(conn, pair, merge):
    g1, g2 = pair(conn)
    a1, _a2 = application(conn, g1, "applied"), application(conn, g2, "interested")
    p1 = packet(conn, a1)
    assert merge(conn) == 1
    assert apps(conn) == [a1]
    assert packets(conn) == {p1: (a1, "draft")}


@pytest.mark.parametrize(("pair", "merge"), MERGES, ids=["url", "xstate"])
def test_packets_on_both_sides_winner_live_loser_abandoned(conn, pair, merge):
    g1, g2 = pair(conn)
    a1, a2 = application(conn, g1, "applied"), application(conn, g2, "preparing")
    p1, p2 = packet(conn, a1), packet(conn, a2)
    assert merge(conn) == 1
    assert apps(conn) == [a1]
    assert packets(conn) == {p1: (a1, "draft"), p2: (a1, "abandoned")}
    # the abandoned packet keeps its documents and answers
    assert (
        conn.execute("SELECT COUNT(*) FROM packet_document WHERE packet_id = ?", (p2,)).fetchone()[
            0
        ]
        == 1
    )
    assert (
        conn.execute("SELECT COUNT(*) FROM packet_answer WHERE packet_id = ?", (p2,)).fetchone()[0]
        == 1
    )
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_already_abandoned_packet_on_loser_moves_without_unique_clash(conn):
    g1, g2 = url_pair(conn)
    a1, a2 = application(conn, g1, "applied"), application(conn, g2, "preparing")
    p1 = packet(conn, a1)
    old = packet(conn, a2)
    conn.execute("UPDATE application_packet SET status = 'abandoned' WHERE id = ?", (old,))
    p2 = packet(conn, a2)
    assert merge_url(conn) == 1
    assert packets(conn) == {p1: (a1, "draft"), old: (a1, "abandoned"), p2: (a1, "abandoned")}


def test_daily_run_dedupe_completes_with_packets_on_both_sides(conn):
    g1, g2 = url_pair(conn)
    a1, a2 = application(conn, g1, "applied"), application(conn, g2, "preparing")
    packet(conn, a1)
    packet(conn, a2)
    conn.commit()
    report = runner.run_pipeline(
        conn, Settings(), [], stages=["dedupe"], profile_loader=lambda: None, now=NOW
    )
    assert report.counts["dedupe_url"]["groups_merged"] == 1
    assert conn.execute("SELECT COUNT(*) FROM job_group").fetchone()[0] == 1
    live = conn.execute(
        "SELECT COUNT(*) FROM application_packet WHERE status != 'abandoned'"
    ).fetchone()[0]
    assert live == 1


def test_one_live_packet_per_application_is_enforced(conn):
    g1, _ = url_pair(conn)
    a1 = application(conn, g1)
    packet(conn, a1)
    with pytest.raises(Exception, match="UNIQUE"):
        packet(conn, a1)


def test_shortlist_undo_keeps_an_application_that_has_a_packet(conn):
    g1, _ = url_pair(conn)
    inbox.set_label(conn, g1, "interesting")
    (app_id,) = apps(conn)
    pid = packet(conn, app_id)
    inbox.undo_label(conn, g1)
    assert apps(conn) == [app_id]
    assert packets(conn) == {pid: (app_id, "draft")}
    assert conn.execute("SELECT COUNT(*) FROM label").fetchone()[0] == 0


def test_shortlist_undo_without_packet_still_deletes(conn):
    g1, _ = url_pair(conn)
    inbox.set_label(conn, g1, "interesting")
    inbox.undo_label(conn, g1)
    assert apps(conn) == []
