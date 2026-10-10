"""`jobhunter migrate-paths`: dry run, apply on a real-shaped layout, verification, rename,
permissions, the open-database refusal, conflicts, rollback and reruns. Synthetic data only."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest
from typer.testing import CliRunner

from jobhunter import legacy_data
from jobhunter.cli import app
from jobhunter.config import load_settings
from jobhunter.core import db
from jobhunter.ops import migrate_paths as mp
from jobhunter.xdg import cache_home, data_home

runner = CliRunner()


@pytest.fixture
def old(old_layout, monkeypatch) -> Path:
    """The old layout in a synthetic checkout, which is also the working directory."""
    root = old_layout()
    monkeypatch.chdir(root)
    return root


def migrate(*args: str):
    return runner.invoke(app, ["migrate-paths", *args])


def snapshot(root: Path) -> dict[str, tuple[int, bytes]]:
    return {
        str(p.relative_to(root)): (p.stat().st_mode & 0o777, p.read_bytes())
        for p in root.rglob("*")
        if p.is_file()
    }


def counts(path: Path) -> dict[str, int]:
    conn = sqlite3.connect(path)
    try:
        return mp.table_counts(conn)
    finally:
        conn.close()


def archive(root: Path, name: str) -> Path:
    found = sorted(root.glob(f"{name}.migrated-*"))
    assert len(found) == 1, found
    return found[0]


def mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


# ─── dry run ────────────────────────────────────────────────────────────────


def test_dry_run_lists_every_copy_and_rename_and_changes_nothing(old):
    before = snapshot(old)
    result = migrate()
    assert result.exit_code == 0, result.output
    out = result.output
    assert "dry run, nothing changed" in out
    assert f"old data: {old} (the working directory)" in out
    for key, dest in (
        ("db", data_home() / "jobhunter.db"),
        ("backups", data_home() / "backups"),
        ("cache", cache_home()),
        ("profile", data_home() / "profile"),
        ("resume", data_home() / "resume"),
    ):
        assert f"{key:<8} " in out and str(dest) in out
    for name in ("data", "profile", "resume"):
        assert f"{old / name} -> {old / name}.migrated-" in out
    assert "openrouter_models.json" in out  # left behind in the renamed folder, and said so
    assert "systemctl --user stop" in out and "--apply" in out
    assert snapshot(old) == before
    assert not data_home().exists() and not cache_home().exists()


# ─── apply ──────────────────────────────────────────────────────────────────


def test_apply_copies_verifies_and_renames_the_old_folders(old):
    old_counts = counts(old / "data" / "jobhunter.db")
    digests = {
        name: mp.tree_digests(old / rel)
        for name, rel in (
            ("profile", "profile"),
            ("resume", "resume"),
            ("backups", "data/backups"),
            ("cache", "data/cache"),
        )
    }
    result = migrate("--apply")
    assert result.exit_code == 0, result.output

    new_db = data_home() / "jobhunter.db"
    assert counts(new_db) == old_counts and old_counts["synthetic_note"] == 50
    assert mp.tree_digests(data_home() / "profile") == digests["profile"]
    assert mp.tree_digests(data_home() / "resume") == digests["resume"]
    assert (data_home() / "resume" / "Synthetic Person - Resume.md").is_file()
    assert (data_home() / "resume" / ".previous" / "old.md").is_file()
    assert mp.tree_digests(data_home() / "backups") == digests["backups"]
    assert mp.tree_digests(cache_home()) == digests["cache"]

    # the old folders are renamed in place: nothing can keep using the old paths
    for name in ("data", "profile", "resume"):
        assert not (old / name).exists()
        kept = archive(old, name)
        note = (kept / "MOVED.txt").read_text(encoding="utf-8")
        assert f"rm -rf '{kept}'" in note
    data_archive = archive(old, "data")
    assert counts(data_archive / "jobhunter.db") == old_counts  # the original, untouched
    assert (data_archive / "openrouter_models.json").is_file()
    assert "rm -rf" in result.output and "systemctl --user start" in result.output
    assert "integrity ok" in result.output and "sha256 verified" in result.output


def test_commands_use_the_migrated_database_afterwards(old):
    assert migrate("--apply").exit_code == 0
    result = runner.invoke(app, ["dedupe", "--apply-url"])
    assert result.exit_code == 0, result.output
    assert counts(data_home() / "jobhunter.db")["synthetic_note"] == 50
    assert legacy_data.check(load_settings()).kind == legacy_data.OK


def test_rerun_after_success_has_nothing_to_migrate(old):
    assert migrate("--apply").exit_code == 0
    after = snapshot(data_home())
    for args in ((), ("--apply",)):
        result = migrate(*args)
        assert result.exit_code == 0, result.output
        assert "nothing to migrate" in result.output
        assert "rm -rf" in result.output and str(archive(old, "data")) in result.output
    assert snapshot(data_home()) == after
    assert len(list(old.glob("data.migrated-*"))) == 1


def test_permissions_are_owner_only_even_in_a_preexisting_data_dir(old):
    data_home().mkdir(parents=True)
    os.chmod(data_home(), 0o755)
    assert migrate("--apply").exit_code == 0
    assert mode(data_home()) == 0o700
    assert mode(data_home() / "jobhunter.db") == 0o600
    for sub in ("backups", "profile", "resume"):
        top = data_home() / sub
        assert mode(top) == 0o700
        for path in top.rglob("*"):
            assert mode(path) == (0o700 if path.is_dir() else 0o600), path


def test_archive_name_does_not_clobber_an_earlier_one(old):
    first = old / f"data.migrated-{date.today():%Y%m%d}"
    first.mkdir()
    (first / "keep.txt").write_text("earlier\n", encoding="utf-8")
    assert migrate("--apply").exit_code == 0
    assert (first / "keep.txt").read_text(encoding="utf-8") == "earlier\n"
    assert Path(f"{first}-2").is_dir() and (Path(f"{first}-2") / "jobhunter.db").is_file()


def test_explicit_destination_is_respected(old, tmp_path, monkeypatch):
    monkeypatch.setenv("JOBHUNTER_DATA_DIR", str(tmp_path / "mine"))
    assert migrate("--apply").exit_code == 0
    assert (tmp_path / "mine" / "jobhunter.db").is_file()
    assert (tmp_path / "mine" / "profile" / "preferences.yaml").is_file()
    assert not (data_home() / "jobhunter.db").exists()


def test_empty_destination_folders_are_filled(old):
    (data_home() / "resume").mkdir(parents=True)
    assert migrate("--apply").exit_code == 0
    assert (data_home() / "resume" / "Synthetic Person - Resume.md").is_file()


# ─── refusing while the database is open ────────────────────────────────────


def test_refuses_while_this_process_has_the_database_open(old):
    before = snapshot(old)
    conn = db.connect(old / "data" / "jobhunter.db")
    try:
        result = migrate("--apply")
    finally:
        conn.close()
    assert result.exit_code == 1
    assert "is open in another process" in result.output
    assert "systemctl --user stop" in result.output
    assert snapshot(old) == before
    assert not data_home().exists() or not any(data_home().rglob("*"))
    assert migrate("--apply").exit_code == 0  # closed: now it goes through


HOLD = (
    "import sqlite3, sys, time\n"
    "c = sqlite3.connect(sys.argv[1])\n"
    "c.execute('PRAGMA journal_mode=WAL').fetchall()\n"
    "print('ready', flush=True)\n"
    "time.sleep(60)\n"
)


def test_refuses_while_another_process_has_it_open_and_names_it(old):
    proc = subprocess.Popen(
        [sys.executable, "-c", HOLD, str(old / "data" / "jobhunter.db")],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None and proc.stdout.readline().strip() == "ready"
        result = migrate("--apply")
    finally:
        proc.kill()
        proc.wait()
    assert result.exit_code == 1
    assert f"held by: {proc.pid}" in result.output
    assert (old / "data" / "jobhunter.db").is_file() and not list(old.glob("*.migrated-*"))


def test_a_process_opening_the_database_during_the_copy_is_kept_out(old, monkeypatch):
    real_copy_tree = mp._copy_tree
    seen: list[str] = []

    def copy_then_try_to_open(item):
        summary = real_copy_tree(item)
        other = sqlite3.connect(old / "data" / "jobhunter.db", timeout=0.1)
        try:
            other.execute("SELECT count(*) FROM synthetic_note").fetchone()
            seen.append("read")
        except sqlite3.OperationalError as exc:
            seen.append(str(exc))
        finally:
            other.close()
        return summary

    monkeypatch.setattr(mp, "_copy_tree", copy_then_try_to_open)
    assert migrate("--apply").exit_code == 0
    assert seen and all("locked" in s for s in seen)


# ─── conflicts ──────────────────────────────────────────────────────────────


def test_existing_new_database_is_a_conflict_and_nothing_changes(old):
    new_db = data_home() / "jobhunter.db"
    conn = db.connect(new_db)
    db.migrate(conn)
    conn.close()
    before = snapshot(old)
    for args in ((), ("--apply",)):
        result = migrate(*args)
        assert result.exit_code == 1
        assert "refusing:" in result.output and "will not pick one" in result.output
    assert snapshot(old) == before
    assert "synthetic_note" not in counts(new_db)


def test_differing_destination_profile_is_a_conflict(old):
    (data_home() / "profile").mkdir(parents=True)
    (data_home() / "profile" / "preferences.yaml").write_text("other: 1\n", encoding="utf-8")
    result = migrate("--apply")
    assert result.exit_code == 1
    assert "differs from" in result.output
    assert (old / "data" / "jobhunter.db").is_file()
    assert not (data_home() / "jobhunter.db").exists()


def test_identical_destination_profile_is_kept_and_the_rest_moves(old):
    (data_home() / "profile").mkdir(parents=True)
    (data_home() / "profile" / "preferences.yaml").write_text("hard: {}\n", encoding="utf-8")
    result = migrate("--apply")
    assert result.exit_code == 0, result.output
    assert "same" in result.output
    assert not (old / "profile").exists() and archive(old, "profile").is_dir()


def test_failed_verification_removes_the_copies_and_leaves_the_old_layout(old, monkeypatch):
    before = snapshot(old)

    def broken(lock, item):
        raise mp.MigrateError("simulated: the copy has different row counts")

    monkeypatch.setattr(mp, "_copy_db", broken)
    result = migrate("--apply")
    assert result.exit_code == 1
    assert "simulated" in result.output and "safe to run this again" in result.output
    assert snapshot(old) == before
    assert not list(old.glob("*.migrated-*"))
    for gone in ("profile", "resume", "backups"):
        assert not (data_home() / gone).exists()
    assert not cache_home().exists() or not any(cache_home().iterdir())


# ─── where the old data is taken from ───────────────────────────────────────


def test_two_old_databases_need_from(tmp_path, old_layout, monkeypatch):
    main = old_layout(tmp_path / "main")
    other = old_layout(tmp_path / "other")
    monkeypatch.setattr(legacy_data, "main_checkout", lambda: main)
    monkeypatch.chdir(other)
    result = migrate("--apply")
    assert result.exit_code == 1 and "pass --from DIR" in result.output
    assert (main / "data").is_dir() and (other / "data").is_dir()
    result = migrate("--apply", "--from", str(main))
    assert result.exit_code == 0, result.output
    assert not (main / "data").exists() and (other / "data").is_dir()


def test_main_checkout_is_used_from_any_directory(tmp_path, old_layout, monkeypatch):
    main = old_layout(tmp_path / "main")
    monkeypatch.setattr(legacy_data, "main_checkout", lambda: main)
    monkeypatch.chdir(tmp_path)
    result = migrate("--apply")
    assert result.exit_code == 0, result.output
    assert "the main checkout of this jobhunter install" in result.output
    assert (data_home() / "jobhunter.db").is_file()


def test_an_unrelated_cwd_resume_is_only_taken_with_from(tmp_path, monkeypatch):
    docs = tmp_path / "docs"
    (docs / "resume").mkdir(parents=True)
    (docs / "resume" / "cv.md").write_text("# Synthetic CV\n", encoding="utf-8")
    monkeypatch.chdir(docs)
    assert "nothing to migrate" in migrate("--apply").output
    assert (docs / "resume" / "cv.md").is_file()
    result = migrate("--apply", "--from", str(docs))
    assert result.exit_code == 0, result.output
    assert (data_home() / "resume" / "cv.md").is_file()
    assert archive(docs, "resume").is_dir() and not (docs / "resume").exists()
