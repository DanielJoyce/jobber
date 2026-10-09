import sqlite3
from importlib import resources

import pytest

from jobhunter.core import db

EXPECTED_TABLES = {
    "schema_version", "source", "source_state", "run", "run_source", "fetch_log", "job",
    "job_group", "prefilter_result", "fit_score", "label", "llm_spend", "application",
    "application_event", "contact", "attachment", "job_locations", "profile_change",
    "apply_link", "apply_click", "job_fts",
}  # fmt: skip


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.migrate(c)
    yield c
    c.close()


def add_job(conn, title="Data Analyst", desc="builds dashboards", employer="Acme", ext="1"):
    if not conn.execute("SELECT 1 FROM source").fetchone():
        conn.execute(
            "INSERT INTO source(key, class, name, family, tier, entry, policy) "
            "VALUES ('s1', 'A', 'n', 'vos', 'http', 'e', 'enabled')"
        )
    cur = conn.execute(
        "INSERT INTO job(source_key, external_id, url, title, employer, description_text, "
        "first_seen_at, last_seen_at) VALUES ('s1', ?, 'u', ?, ?, ?, 't', 't')",
        (ext, title, employer, desc),
    )
    return cur.lastrowid


def add_group(conn):
    return conn.execute(
        "INSERT INTO job_group(method, created_at) VALUES ('exact_hash', 't')"
    ).lastrowid


def fts(conn, q):
    return [r[0] for r in conn.execute("SELECT rowid FROM job_fts WHERE job_fts MATCH ?", (q,))]


def test_migrate_fresh_memory_and_pragmas(conn):
    assert db.current_version(conn) == 2
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_file_db_creates_parents_and_wal(tmp_path):
    path = tmp_path / "a" / "b" / "x.db"
    c = db.connect(path)
    assert db.migrate(c) == [1, 2]
    assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    c.close()
    c2 = db.connect(path)
    assert db.migrate(c2) == []
    c2.close()


def test_idempotent(conn):
    assert db.migrate(conn) == []
    assert conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == 2


def test_all_tables_exist(conn):
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert names >= EXPECTED_TABLES


def test_migration_files_are_package_data():
    names = [e.name for e in resources.files(db.MIGRATIONS_PACKAGE).iterdir()]
    assert "0001_initial.sql" in names


def test_transaction_rolls_back_on_error():
    c = db.connect(":memory:")
    with pytest.raises(sqlite3.Error), db.transaction(c):
        c.execute("CREATE TABLE t(x)")
        c.execute("CREATE TABLE t(x)")
    assert not c.execute("SELECT 1 FROM sqlite_master WHERE name='t'").fetchone()


def test_fk_enforced(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO job(source_key, external_id, url, title, first_seen_at, last_seen_at) "
            "VALUES ('nope', '1', 'u', 't', 't', 't')"
        )


def test_fts_insert_update_delete(conn):
    jid = add_job(conn, title="Data Analyst", desc="builds dashboards")
    assert fts(conn, "dashboard") == [jid]  # porter stemming
    assert fts(conn, "acme") == [jid]
    conn.execute("UPDATE job SET description_text='writes poetry' WHERE id=?", (jid,))
    assert fts(conn, "dashboard") == []
    assert fts(conn, "poetry") == [jid]
    conn.execute("DELETE FROM job WHERE id=?", (jid,))
    assert fts(conn, "poetry") == []
    assert fts(conn, "analyst") == []


def test_fit_score_unique(conn):
    gid = add_group(conn)
    sql = (
        "INSERT INTO fit_score(job_group_id, tier, model, prompt_version, scoring_version, "
        "verdict, overall, dimensions, evidence, created_at) "
        "VALUES (?, 'screen', 'm', 'p1', 's1', 'strong', 80, '{}', '[]', 't')"
    )
    conn.execute(sql, (gid,))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql, (gid,))
    conn.execute(sql.replace("'s1'", "'s2'"), (gid,))


def test_check_rejects_bad_status(conn):
    gid = add_group(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO application(job_group_id, status, created_at, updated_at) "
            "VALUES (?, 'bogus', 't', 't')",
            (gid,),
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO source(key, class, name, family, tier, entry, policy, status) "
            "VALUES ('k', 'A', 'n', 'f', 'http', 'e', 'enabled', 'bogus')"
        )


def test_job_locations_dedupes_null_state_rows():
    import sqlite3

    import pytest

    from jobhunter.core.db import connect, migrate

    conn = connect(":memory:")
    migrate(conn)
    conn.execute(
        "INSERT INTO source (key, class, name, family, tier, entry, policy) "
        "VALUES ('s', 'C', 'S', 'usajobs', 'api', 'https://x.example', 'enabled')"
    )
    conn.execute(
        "INSERT INTO job (source_key, external_id, url, title, first_seen_at, last_seen_at) "
        "VALUES ('s', '1', 'https://x.example/1', 'T', '2026-10-09', '2026-10-09')"
    )
    conn.execute("INSERT INTO job_locations (job_id, state, city) VALUES (1, NULL, NULL)")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO job_locations (job_id, state, city) VALUES (1, NULL, NULL)")
