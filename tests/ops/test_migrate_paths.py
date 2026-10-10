"""`jobhunter migrate-paths`: dry run, apply, conflicts, notes, --remove-old. Synthetic."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from jobhunter import config as cfg
from jobhunter.cli import app
from jobhunter.config import load_settings
from jobhunter.xdg import cache_home, data_home

runner = CliRunner()


@pytest.fixture(autouse=True)
def _reset_warned():
    cfg._warned_legacy.clear()


@pytest.fixture
def old(tmp_path, monkeypatch) -> Path:
    """A synthetic checkout in the cwd holding the old layout, with a WAL-mode database."""
    root = tmp_path / "checkout"
    (root / "data" / "cache" / "ab" / "cd").mkdir(parents=True)
    (root / "data" / "cache" / "ab" / "cd" / "abcdef.gz").write_bytes(b"cached")
    conn = sqlite3.connect(root / "data" / "jobhunter.db")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t (v TEXT)")
    conn.execute("INSERT INTO t VALUES ('synthetic row')")
    conn.commit()
    conn.close()
    (root / "profile").mkdir()
    (root / "profile" / "preferences.yaml").write_text("hard: {}\n", encoding="utf-8")
    (root / "resume").mkdir()
    (root / "resume" / "me.md").write_text("Synthetic Person\n", encoding="utf-8")
    monkeypatch.chdir(root)
    return root


def run(*args: str):
    return runner.invoke(app, ["migrate-paths", *args])


def snapshot(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_dry_run_lists_every_move_and_changes_nothing(old):
    before = snapshot(old)
    result = run()
    assert result.exit_code == 0, result.output
    assert "dry run" in result.output
    for key, dest in (
        ("db", data_home() / "jobhunter.db"),
        ("profile", data_home() / "profile"),
        ("resume", data_home() / "resume"),
        ("cache", cache_home()),
    ):
        assert f"[move] {key}: " in result.output and str(dest) in result.output
    assert "sqlite backup first" in result.output
    assert "--apply" in result.output
    assert snapshot(old) == before
    assert not data_home().exists() and not cache_home().exists()


def test_apply_copies_backs_up_and_leaves_notes(old):
    result = run("--apply")
    assert result.exit_code == 0, result.output
    # database: logically identical, plus a timestamped sqlite backup beside it
    moved = sqlite3.connect(data_home() / "jobhunter.db")
    assert moved.execute("SELECT v FROM t").fetchall() == [("synthetic row",)]
    moved.close()
    backups = list((data_home() / "backups").glob("jobhunter-*.db"))
    assert len(backups) == 1
    check = sqlite3.connect(backups[0])
    assert check.execute("SELECT count(*) FROM t").fetchone() == (1,)
    check.close()
    # trees
    assert (data_home() / "profile" / "preferences.yaml").read_text() == "hard: {}\n"
    assert (data_home() / "resume" / "me.md").read_text() == "Synthetic Person\n"
    assert (cache_home() / "ab" / "cd" / "abcdef.gz").read_bytes() == b"cached"
    # originals untouched, each old directory points at the new place
    assert (old / "data" / "jobhunter.db").exists()
    assert (old / "profile" / "preferences.yaml").exists()
    assert (old / "resume" / "me.md").exists()
    assert str(data_home() / "jobhunter.db") in (old / "data" / "MOVED.txt").read_text()
    assert str(data_home() / "profile") in (old / "profile" / "MOVED.txt").read_text()
    assert str(data_home() / "resume") in (old / "resume" / "MOVED.txt").read_text()
    # private directory for personal data
    assert (data_home().stat().st_mode & 0o777) == 0o700
    # the note does not leak into the copy, and settings now resolve to the new places
    assert not (data_home() / "profile" / "MOVED.txt").exists()
    s = load_settings()
    assert s.paths.db_path == data_home() / "jobhunter.db"
    assert "legacy" not in s.paths.sources.values()


def test_second_apply_is_a_no_op(old):
    assert run("--apply").exit_code == 0
    again = run("--apply")
    assert again.exit_code == 0, again.output
    assert "[already migrated]" in again.output and "[move]" not in again.output
    assert len(list((data_home() / "backups").glob("jobhunter-*.db"))) == 1


def test_refuses_when_destination_exists_and_differs(old):
    (data_home() / "profile").mkdir(parents=True)
    (data_home() / "profile" / "preferences.yaml").write_text("hard: {remote_ok: true}\n")
    result = run("--apply")
    assert result.exit_code == 1
    assert "[conflict] profile" in result.output and "refusing" in result.output
    # nothing was moved, not even the items that had no conflict
    assert not (data_home() / "jobhunter.db").exists()
    assert not (cache_home()).exists()
    assert not (old / "data" / "MOVED.txt").exists()
    assert (data_home() / "profile" / "preferences.yaml").read_text() == (
        "hard: {remote_ok: true}\n"
    )


def test_dry_run_also_reports_conflicts(old):
    (data_home() / "resume").mkdir(parents=True)
    (data_home() / "resume" / "other.md").write_text("different\n")
    result = run()
    assert result.exit_code == 1
    assert "[conflict] resume" in result.output


def test_existing_database_with_different_content_is_a_conflict(old):
    data_home().mkdir(parents=True)
    other = sqlite3.connect(data_home() / "jobhunter.db")
    other.execute("CREATE TABLE t (v TEXT)")
    other.execute("INSERT INTO t VALUES ('another row')")
    other.commit()
    other.close()
    result = run("--apply")
    assert result.exit_code == 1 and "[conflict] db" in result.output


def test_explicit_paths_are_not_migrated(old, tmp_path, monkeypatch):
    mine = tmp_path / "mine.db"
    monkeypatch.setenv("JOBHUNTER_DB_PATH", str(mine))
    result = run("--apply")
    assert result.exit_code == 0, result.output
    assert "] db:" not in result.output
    assert not mine.exists()
    assert (data_home() / "profile").is_dir()


def test_remove_old_needs_apply_and_never_runs_implicitly(old):
    assert run("--apply").exit_code == 0
    assert (old / "profile" / "preferences.yaml").exists()  # --apply alone keeps originals
    dry = run("--remove-old")
    assert dry.exit_code == 0 and "dry run" in dry.output
    assert (old / "profile" / "preferences.yaml").exists()


def test_remove_old_deletes_identical_originals_but_keeps_notes(old):
    assert run("--apply").exit_code == 0
    result = run("--apply", "--remove-old")
    assert result.exit_code == 0, result.output
    assert not (old / "data" / "jobhunter.db").exists()
    assert not (old / "data" / "cache").exists()
    assert not (old / "profile" / "preferences.yaml").exists()
    assert not (old / "resume" / "me.md").exists()
    for note in (old / "data", old / "profile", old / "resume"):
        assert "removed" in (note / "MOVED.txt").read_text()
    assert (data_home() / "profile" / "preferences.yaml").exists()  # the copies are untouched


def test_remove_old_keeps_an_original_that_changed_after_the_copy(old):
    assert run("--apply").exit_code == 0
    (old / "profile" / "preferences.yaml").write_text("hard: {remote_ok: false}\n")
    result = run("--apply", "--remove-old")
    assert result.exit_code == 1  # now a conflict: the copy no longer matches
    assert (old / "profile" / "preferences.yaml").exists()
    assert (old / "resume" / "me.md").exists()  # nothing at all was deleted


def test_nothing_to_migrate(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = run("--apply")
    assert result.exit_code == 0
    assert "nothing to move" in result.output
    assert not data_home().exists()
