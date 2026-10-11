"""Data-dir-relative paths in the database (specs/018 C14, D18).

The ``/home`` versus ``/var/home`` case is a tmp symlink: ``home`` -> ``var/home``, with the data
dir under it, so the configured path and its realpath differ the way they do on Fedora atomic.
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from jobhunter.core import db, dbpaths

ISO = "2026-10-10T12:00:00+00:00"


@pytest.fixture
def twin(tmp_path):
    """(configured data dir through the symlink, the same dir by its realpath)."""
    real_home = tmp_path / "var" / "home" / "you"
    real_home.mkdir(parents=True)
    (tmp_path / "home").symlink_to(tmp_path / "var" / "home")
    real = real_home / ".local" / "share" / "jobhunter"
    real.mkdir(parents=True)
    return tmp_path / "home" / "you" / ".local" / "share" / "jobhunter", real


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.migrate(c)
    c.execute("PRAGMA foreign_keys = OFF")  # rows below need no job groups or packets
    yield c
    c.close()


def add_app(conn, n, cover=None):
    conn.execute(
        "INSERT INTO application (id, job_group_id, status, cover_letter_path, created_at, "
        "updated_at) VALUES (?, ?, 'applied', ?, ?, ?)",
        (n, n, cover, ISO, ISO),
    )


def add_attachment(conn, app_id, path):
    return conn.execute(
        "INSERT INTO attachment (application_id, kind, path, added_at) VALUES (?, 'other', ?, ?)",
        (app_id, path, ISO),
    ).lastrowid


def add_doc(conn, n, rendered):
    conn.execute(
        "INSERT INTO packet_document (id, packet_id, kind, version, origin, doc_json, body_md, "
        "check_report, rendered_path, created_at) "
        "VALUES (?, 1, 'resume', ?, 'edited', '{}', 'x', '{}', ?, ?)",
        (n, n, rendered, ISO),
    )


def paths(conn):
    return {
        "attachment": [r[0] for r in conn.execute("SELECT path FROM attachment ORDER BY id")],
        "cover": [r[0] for r in conn.execute("SELECT cover_letter_path FROM application")],
        "rendered": [r[0] for r in conn.execute("SELECT rendered_path FROM packet_document")],
    }


# --- one path ------------------------------------------------------------------------------


def test_both_forms_of_the_data_dir_match_whichever_is_configured(twin):
    configured, real = twin
    for data_dir in (configured, real):
        for form in (configured, real):
            assert dbpaths.relative_to_data_dir(form / "packets/3/r.pdf", data_dir) == (
                "packets/3/r.pdf"
            )
    # a file that no longer exists still matches through its realpath'd parent
    assert dbpaths.relative_to_data_dir(real / "gone.pdf", configured) == "gone.pdf"


def test_outside_the_data_dir_and_the_dir_itself_do_not_match(twin, tmp_path):
    configured, real = twin
    assert dbpaths.relative_to_data_dir(tmp_path / "Documents" / "cv.pdf", configured) is None
    assert dbpaths.relative_to_data_dir(real.parent / "jobhunter-old" / "x", configured) is None
    assert dbpaths.relative_to_data_dir(real, configured) is None
    assert dbpaths.relative_to_data_dir("packets/x.pdf", configured) is None  # not absolute


def test_resolve_reads_relative_against_the_current_data_dir():
    assert dbpaths.resolve("attachments/cv.pdf", "/data") == Path("/data/attachments/cv.pdf")
    assert dbpaths.resolve("/old/abs.pdf", "/data") == Path("/old/abs.pdf")


def test_copy_verifies_sha256_and_leaves_nothing_on_mismatch(tmp_path, monkeypatch):
    src = tmp_path / "cv.pdf"
    src.write_bytes(b"real bytes")
    data = tmp_path / "data"
    real_sha = dbpaths.sha256_file
    calls = []

    def flaky(path):
        calls.append(path)
        return real_sha(path) if path == src else "0" * 64  # the copy "differs"

    monkeypatch.setattr(dbpaths, "sha256_file", flaky)
    with pytest.raises(dbpaths.CopyError, match="sha256"):
        dbpaths.copy_into_attachments(src, data)
    assert list((data / "attachments").iterdir()) == []
    assert len(calls) == 2  # the source, then the copy


# --- counting --------------------------------------------------------------------------------


def test_count_absolute_paths_covers_every_column(conn):
    add_app(conn, 1, cover="/home/you/cl.pdf")
    add_app(conn, 2, cover="attachments/cl.pdf")
    add_app(conn, 3)
    add_attachment(conn, 1, "~/Documents/x.pdf")
    add_attachment(conn, 1, "packets/1/r.pdf")
    add_doc(conn, 1, "/var/home/you/.local/share/jobhunter/packets/1/r.pdf")
    add_doc(conn, 2, None)
    assert dbpaths.absolute_path_counts(conn) == {
        "attachment.path": 1,
        "application.cover_letter_path": 1,
        "packet_document.rendered_path": 1,
    }
    assert dbpaths.count_absolute_paths(conn) == 3


def test_count_on_a_schema_without_packet_document(tmp_path):
    raw = sqlite3.connect(tmp_path / "old.db")
    raw.execute(
        "CREATE TABLE attachment (id INTEGER PRIMARY KEY, application_id INTEGER, path TEXT)"
    )
    raw.execute("CREATE TABLE application (id INTEGER PRIMARY KEY, cover_letter_path TEXT)")
    raw.execute("INSERT INTO attachment (path) VALUES ('/x/y.pdf')")
    assert dbpaths.count_absolute_paths(raw) == 1
    raw.close()


# --- relativize ------------------------------------------------------------------------------


@pytest.fixture
def seeded(conn, twin, tmp_path):
    """Rows under the configured dir, under its realpath twin, outside it, and missing."""
    configured, real = twin
    (real / "packets" / "1").mkdir(parents=True)
    (real / "packets" / "1" / "resume-v1.pdf").write_bytes(b"resume")
    outside = tmp_path / "Documents"
    outside.mkdir()
    (outside / "letter.pdf").write_bytes(b"letter")
    add_app(conn, 1, cover=str(outside / "letter.pdf"))
    add_app(conn, 2, cover="attachments/already.pdf")
    add_attachment(conn, 1, str(configured / "packets" / "1" / "resume-v1.pdf"))
    add_attachment(conn, 1, str(outside / "letter.pdf"))  # same file as the cover letter
    add_attachment(conn, 2, str(tmp_path / "gone" / "missing.pdf"))
    add_doc(conn, 1, str(real / "packets" / "1" / "resume-v1.pdf"))
    return configured, real, outside


def test_dry_run_plans_and_writes_nothing(conn, seeded, tmp_path):
    configured, real, _ = seeded
    before = paths(conn)
    plan = dbpaths.plan_relativize(conn, configured)
    got = sorted((c.table, c.action, c.new) for c in plan.changes)
    assert got == [
        ("application", "copy", "attachments/letter.pdf"),
        ("attachment", "copy", "attachments/letter.pdf"),
        ("attachment", "relativize", "packets/1/resume-v1.pdf"),
        ("attachment", "unresolved", None),
        ("packet_document", "relativize", "packets/1/resume-v1.pdf"),
    ]
    assert "5 path(s) to fix: 2 under the data dir, 2 to copy" in plan.summary
    assert paths(conn) == before
    assert not (real / "attachments").exists()


def test_apply_rewrites_copies_once_and_reports_the_unresolved(conn, seeded, tmp_path):
    configured, real, outside = seeded
    done = dbpaths.apply_relativize(conn, dbpaths.plan_relativize(conn, configured))
    assert done.copied == ["attachments/letter.pdf"]  # two rows, one copy
    assert len(done.rewritten) == 4 and done.failed == []
    assert paths(conn) == {
        "attachment": [
            "packets/1/resume-v1.pdf",
            "attachments/letter.pdf",
            str(tmp_path / "gone" / "missing.pdf"),
        ],
        "cover": ["attachments/letter.pdf", "attachments/already.pdf"],
        "rendered": ["packets/1/resume-v1.pdf"],
    }
    copy = real / "attachments" / "letter.pdf"
    assert hashlib.sha256(copy.read_bytes()).digest() == hashlib.sha256(b"letter").digest()
    assert copy.stat().st_mode & 0o777 == 0o600
    assert (outside / "letter.pdf").exists()  # the original is not moved
    assert dbpaths.count_absolute_paths(conn) == 1  # the missing file, left for you
    # A second run has only the unresolved row left and changes nothing.
    again = dbpaths.plan_relativize(conn, configured)
    assert [c.action for c in again.changes] == ["unresolved"]


def test_after_relativizing_reads_resolve_against_a_different_data_dir(conn, seeded, tmp_path):
    configured, real, _ = seeded
    dbpaths.apply_relativize(conn, dbpaths.plan_relativize(conn, configured))
    mounted = tmp_path / "data"  # the container's /data: the same tree, another mount point
    real.rename(mounted)
    for (stored,) in conn.execute(
        "SELECT path FROM attachment WHERE path NOT LIKE '/%' UNION ALL "
        "SELECT cover_letter_path FROM application UNION ALL "
        "SELECT rendered_path FROM packet_document"
    ):
        if stored == "attachments/already.pdf":
            continue  # a row that never named a real file
        assert dbpaths.resolve(stored, mounted).is_file(), stored


def test_a_row_changed_after_the_plan_is_not_overwritten(conn, seeded):
    configured, _, _ = seeded
    plan = dbpaths.plan_relativize(conn, configured)
    conn.execute("UPDATE packet_document SET rendered_path = 'packets/1/edited.pdf'")
    done = dbpaths.apply_relativize(conn, plan)
    assert [c.table for c in done.failed] == ["packet_document"]
    assert paths(conn)["rendered"] == ["packets/1/edited.pdf"]


# --- containment (review of C14: '..' and symlink escapes) ---------------------------------


def test_resolve_inside_refuses_dotdot_and_symlinks_out(tmp_path):
    data = tmp_path / "data"
    (data / "packets" / "5").mkdir(parents=True)
    secret = tmp_path / ".ssh" / "id_rsa"
    secret.parent.mkdir()
    secret.write_text("PRIVATE KEY")
    (data / "packets" / "5" / "resume-v1.pdf").symlink_to(secret)
    (data / "packets" / "5" / "ok.pdf").write_bytes(b"%PDF")
    assert dbpaths.resolve_inside("packets/5/ok.pdf", data) == data / "packets/5/ok.pdf"
    assert dbpaths.resolve_inside("../.ssh/id_rsa", data) is None
    assert dbpaths.resolve_inside("packets/../../.ssh/id_rsa", data) is None
    assert dbpaths.resolve_inside("packets/5/resume-v1.pdf", data) is None  # symlink out
    assert dbpaths.resolve_inside(str(secret), data) is None  # absolute elsewhere
    assert dbpaths.resolve_inside("", data) is None


def test_escaping_relative_values_count_for_preflight(conn, tmp_path):
    data = tmp_path / "data"
    (data / "packets").mkdir(parents=True)
    (data / "packets" / "link.pdf").symlink_to(tmp_path / "t.db")
    add_app(conn, 1, cover="../../etc/passwd")
    add_attachment(conn, 1, "packets/link.pdf")
    add_attachment(conn, 1, "packets/fine.pdf")
    assert dbpaths.count_absolute_paths(conn) == 1  # lexical '..' without a data dir
    assert dbpaths.count_absolute_paths(conn, data) == 2  # plus the symlink out
    plan = dbpaths.plan_relativize(conn, data)
    assert sorted((c.where, c.action) for c in plan.changes) == [
        ("application.cover_letter_path id=1", "unresolved"),
        ("attachment.path id=1", "unresolved"),
    ]
    assert not plan.actionable


def test_relativize_removes_a_row_that_would_duplicate_another(conn, twin):
    configured, real = twin
    (real / "packets" / "5").mkdir(parents=True)
    (real / "packets" / "5" / "r.pdf").write_bytes(b"%PDF")
    add_app(conn, 1)
    add_attachment(conn, 1, str(real / "packets/5/r.pdf"))  # written before C14
    add_attachment(conn, 1, "packets/5/r.pdf")  # re-attached after it
    add_attachment(conn, 1, str(configured / "packets/5/r.pdf"))  # the other form, too
    plan = dbpaths.plan_relativize(conn, configured)
    assert [c.action for c in plan.changes] == ["duplicate", "duplicate"]
    done = dbpaths.apply_relativize(conn, plan)
    assert len(done.removed) == 2 and done.rewritten == []
    assert paths(conn)["attachment"] == ["packets/5/r.pdf"]
    assert dbpaths.count_absolute_paths(conn, configured) == 0


def test_every_os_error_while_copying_is_a_copy_error(tmp_path):
    src = tmp_path / "locked.pdf"
    src.write_bytes(b"x")
    src.chmod(0)
    try:
        with pytest.raises(dbpaths.CopyError):
            dbpaths.store(src, tmp_path / "data")
    finally:
        src.chmod(0o600)
    ro = tmp_path / "ro"
    ro.mkdir()
    ok = tmp_path / "ok.pdf"
    ok.write_bytes(b"y")
    ro.chmod(0o500)
    try:
        with pytest.raises(dbpaths.CopyError):
            dbpaths.store(ok, ro)  # cannot create attachments/
    finally:
        ro.chmod(0o700)
