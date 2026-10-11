"""Old-layout detection and the guard that stops commands from forking the database.

The rule under test: while an unmigrated ``data/jobhunter.db`` exists in the main checkout or
the working directory, no command (other than paths, migrate-paths, init and schedule) runs,
and none ever creates a database. Synthetic data only.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import typer.main
from typer.testing import CliRunner

from jobhunter import legacy_data
from jobhunter.cli import UNGUARDED_COMMANDS, app
from jobhunter.config import load_settings
from jobhunter.legacy_data import main_checkout as real_main_checkout
from jobhunter.xdg import data_home

runner = CliRunner()
REPO = Path(__file__).resolve().parents[1]


def files_below(path: Path) -> list[Path]:
    return sorted(p for p in path.rglob("*") if p.is_file()) if path.exists() else []


def snapshot(root: Path) -> dict[str, tuple[int, bytes]]:
    return {
        str(p.relative_to(root)): (p.stat().st_mode, p.read_bytes())
        for p in root.rglob("*")
        if p.is_file()
    }


def all_guarded_commands() -> list[str]:
    names = sorted(typer.main.get_command(app).commands)  # type: ignore[attr-defined]
    return [n for n in names if n not in UNGUARDED_COMMANDS]


def git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        timeout=30,
    )


# ─── detection ──────────────────────────────────────────────────────────────


def test_real_main_checkout_is_the_repository_main_working_tree():
    """The unpatched detector, run against this repository: a worktree maps to the main
    checkout (only paths are compared; nothing in it is read)."""
    common = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    ).stdout.strip()
    assert legacy_data.package_checkout() == REPO
    assert real_main_checkout() == Path(common).parent


def test_worktree_maps_to_its_main_checkout(tmp_path, monkeypatch, old_layout):
    main = tmp_path / "main"
    main.mkdir()
    git("init", "-q", cwd=main)
    git("commit", "-q", "--allow-empty", "-m", "init", cwd=main)
    git("worktree", "add", "-q", "-b", "wt", str(tmp_path / "wt"), cwd=main)
    old_layout(main)
    monkeypatch.setattr(legacy_data, "package_checkout", lambda: tmp_path / "wt")
    monkeypatch.setattr(legacy_data, "main_checkout", real_main_checkout)
    assert legacy_data.main_checkout() == main
    # run from somewhere unrelated, with the package imported from the worktree
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert legacy_data.legacy_dbs() == [main / "data" / "jobhunter.db"]
    result = runner.invoke(app, ["dedupe", "--apply-url"])
    assert result.exit_code == 1
    assert f"your data is still in {main / 'data' / 'jobhunter.db'}" in result.output
    assert not data_home().exists()


def test_cwd_with_unrelated_resume_and_profile_is_not_old_data(tmp_path, monkeypatch):
    docs = tmp_path / "Documents"
    (docs / "resume").mkdir(parents=True)
    (docs / "resume" / "cv.md").write_text("# someone else\n", encoding="utf-8")
    (docs / "profile").mkdir()
    monkeypatch.chdir(docs)
    assert legacy_data.legacy_dbs() == []
    status = legacy_data.check(load_settings())
    assert status.kind == legacy_data.OK and not status.warnings
    result = runner.invoke(app, ["migrate-paths"])
    assert result.exit_code == 0, result.output
    assert "nothing to migrate" in result.output
    result = runner.invoke(app, ["migrate-paths", "--apply"])
    assert result.exit_code == 0, result.output
    assert sorted(p.name for p in docs.iterdir()) == ["profile", "resume"]
    assert (docs / "resume" / "cv.md").read_text(encoding="utf-8") == "# someone else\n"


# ─── the guard ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("command", all_guarded_commands())
def test_no_command_creates_or_uses_a_database_while_old_data_exists(
    command, old_layout, monkeypatch
):
    root = old_layout()
    monkeypatch.chdir(root)
    before = snapshot(root)
    result = runner.invoke(app, [command])
    assert result.exit_code == 1, result.output
    assert "your data is still in" in result.output
    assert "jobhunter migrate-paths" in result.output
    assert files_below(data_home()) == []
    assert snapshot(root) == before


@pytest.mark.parametrize(
    "argv",
    [["run"], ["run", "--dry-run"], ["dedupe", "--apply-url"], ["score", "--submit"], ["console"]],
)
def test_realistic_invocations_stop_before_touching_anything(argv, old_layout, monkeypatch):
    root = old_layout()
    monkeypatch.chdir(root)
    result = runner.invoke(app, argv)
    assert result.exit_code == 1
    assert "Nothing was created" in result.output
    assert not data_home().exists()


@pytest.mark.parametrize("command", sorted(UNGUARDED_COMMANDS - {"init", "schedule", "secrets"}))
def test_paths_and_migrate_paths_still_work(command, old_layout, monkeypatch):
    root = old_layout()
    monkeypatch.chdir(root)
    result = runner.invoke(app, [command])
    assert result.exit_code == 0, result.output
    assert files_below(data_home()) == []


def test_both_databases_without_moved_note_is_a_conflict(old_layout, monkeypatch):
    root = old_layout()
    monkeypatch.chdir(root)
    new_db = data_home() / "jobhunter.db"
    new_db.parent.mkdir(parents=True)
    new_db.write_bytes(b"")
    status = legacy_data.check(load_settings())
    assert status.kind == legacy_data.CONFLICT
    result = runner.invoke(app, ["run"])
    assert result.exit_code == 1
    assert "two databases" in result.output and "will not pick one" in result.output
    assert new_db.read_bytes() == b""  # untouched, not migrated into


def test_old_database_beside_a_moved_note_does_not_stop_commands(old_layout, monkeypatch):
    root = old_layout()
    (root / "data" / "MOVED.txt").write_text("moved\n", encoding="utf-8")
    monkeypatch.chdir(root)
    new_db = data_home() / "jobhunter.db"
    new_db.parent.mkdir(parents=True)
    new_db.write_bytes(b"")
    status = legacy_data.check(load_settings())
    assert status.kind == legacy_data.OK
    assert status.stale == [root / "data" / "jobhunter.db"]
    assert "although its folder says it was moved" in status.warnings[0]


def test_old_data_found_in_the_main_checkout_from_another_directory(
    tmp_path, old_layout, monkeypatch
):
    root = old_layout()
    monkeypatch.setattr(legacy_data, "main_checkout", lambda: root)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["backfill-salary"])
    assert result.exit_code == 1
    assert str(root / "data" / "jobhunter.db") in result.output
    assert not data_home().exists()


def test_explicit_existing_database_runs_with_a_note(tmp_path, old_layout, monkeypatch):
    """A one-off run against an explicit copy is allowed; the note names the old data."""
    root = old_layout()
    monkeypatch.chdir(root)
    copy = tmp_path / "copy.db"
    copy.write_bytes((root / "data" / "jobhunter.db").read_bytes())
    monkeypatch.setenv("JOBHUNTER_DB_PATH", str(copy))
    status = legacy_data.check(load_settings())
    assert status.kind == legacy_data.OK
    assert "set explicitly" in status.warnings[0]


def test_explicit_missing_database_is_not_created(tmp_path, old_layout, monkeypatch):
    root = old_layout()
    monkeypatch.chdir(root)
    monkeypatch.setenv("JOBHUNTER_DB_PATH", str(tmp_path / "new.db"))
    result = runner.invoke(app, ["dedupe", "--apply-url"])
    assert result.exit_code == 1
    assert "refusing to create a new empty database" in result.output
    assert not (tmp_path / "new.db").exists()


def test_settings_pointing_at_the_old_location_are_honoured(old_layout, monkeypatch):
    root = old_layout()
    monkeypatch.chdir(root)
    monkeypatch.setenv("JOBHUNTER_DATA_DIR", "data")
    status = legacy_data.check(load_settings())
    assert status.kind == legacy_data.OK and not status.warnings


def test_fresh_install_without_old_data_creates_a_private_database():
    result = runner.invoke(app, ["dedupe", "--apply-url"])
    assert result.exit_code == 0, result.output
    db_file = data_home() / "jobhunter.db"
    assert db_file.is_file()
    assert os.stat(data_home()).st_mode & 0o777 == 0o700
    assert os.stat(db_file).st_mode & 0o777 == 0o600


# ─── init ───────────────────────────────────────────────────────────────────


def test_init_creates_the_private_data_dir_and_database():
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0, result.output
    db_file = data_home() / "jobhunter.db"
    assert f"created {db_file}" in result.output
    assert os.stat(data_home()).st_mode & 0o777 == 0o700
    assert os.stat(db_file).st_mode & 0o777 == 0o600
    again = runner.invoke(app, ["init"])
    assert again.exit_code == 0 and "already set up" in again.output


def test_init_fixes_a_world_readable_data_dir():
    data_home().mkdir(parents=True, mode=0o755)
    os.chmod(data_home(), 0o755)
    assert runner.invoke(app, ["init"]).exit_code == 0
    assert os.stat(data_home()).st_mode & 0o777 == 0o700


def test_init_refuses_while_old_data_exists(old_layout, monkeypatch):
    root = old_layout()
    monkeypatch.chdir(root)
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 1
    assert "migrate-paths" in result.output
    assert not data_home().exists()


def test_init_leaves_an_explicitly_configured_shared_folder_alone(tmp_path, monkeypatch):
    shared = tmp_path / "Documents"
    shared.mkdir()
    (shared / "notes.txt").write_text("x\n", encoding="utf-8")
    os.chmod(shared, 0o755)
    os.chmod(shared / "notes.txt", 0o644)
    monkeypatch.setenv("JOBHUNTER_RESUME_PATH", str(shared))
    assert runner.invoke(app, ["init"]).exit_code == 0
    assert os.stat(shared).st_mode & 0o777 == 0o755
    assert os.stat(shared / "notes.txt").st_mode & 0o777 == 0o644
