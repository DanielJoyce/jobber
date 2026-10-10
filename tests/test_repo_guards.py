"""The repo's personal-data guards cover every place jobhunter puts personal data in a checkout.

``migrate-paths --apply`` renames the old ``data/``, ``profile/`` and ``resume/`` folders in
the checkout to ``<name>.migrated-YYYYMMDD`` archives. They hold the full DB, backups, contact
details and salary targets, so they must be gitignored (safe from ``git add -A`` and
``git clean -fd``) and blocked by the forbid-personal-paths pre-commit hook.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from datetime import date
from pathlib import Path

import pytest
from ruamel.yaml import YAML

from jobhunter.ops.migrate_paths import _archive_name

REPO = Path(__file__).resolve().parents[1]
TODAY = date(2026, 10, 9)


def _archive_files(root: Path) -> list[str]:
    """Personal files as they sit in the checkout after migrate-paths, relative to ``root``."""
    files = []
    for folder, inner in (
        ("data", ["jobhunter.db", "backups/jobhunter-1.db", "MOVED.txt"]),
        ("profile", ["preferences.yaml", "MOVED.txt"]),
        ("resume", ["me.md", "MOVED.txt"]),
    ):
        first = _archive_name(root, folder, TODAY)
        first.mkdir()
        second = _archive_name(root, folder, TODAY)  # a second run the same day: -2
        assert second.name.endswith("-2")
        for archive in (first, second):
            files += [f"{archive.relative_to(root).as_posix()}/{name}" for name in inner]
    return files


def _forbid_regex() -> re.Pattern[str]:
    config = YAML(typ="safe").load((REPO / ".pre-commit-config.yaml").read_text())
    for repo in config["repos"]:
        for hook in repo["hooks"]:
            if hook["id"] == "forbid-personal-paths":
                return re.compile(hook["files"])
    raise AssertionError("forbid-personal-paths hook not found")


def test_forbid_personal_paths_hook_blocks_migration_archives(tmp_path):
    pattern = _forbid_regex()
    # pre-commit matches `files` with re.search against the repo-relative path
    leaked = [f for f in _archive_files(tmp_path) if not pattern.search(f)]
    assert leaked == []


def test_forbid_personal_paths_hook_still_scoped():
    pattern = _forbid_regex()
    for path in ("data/jobhunter.db", "profile/preferences.yaml", "resume/me.md", "config.toml"):
        assert pattern.search(path), path
    for path in (
        "src/jobhunter/ops/migrate_paths.py",
        "specs/data/schema.md",
        "tests/fixtures/data.json",
        "docs/data.migrated.md",
    ):
        assert not pattern.search(path), path


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_gitignore_covers_migration_archives(tmp_path):
    shutil.copy(REPO / ".gitignore", tmp_path / ".gitignore")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    files = _archive_files(tmp_path)
    for f in files:
        (tmp_path / f).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / f).write_text("placeholder\n")

    result = subprocess.run(
        ["git", "-C", str(tmp_path), "check-ignore", "--no-index", "--stdin"],
        input="\n".join(files) + "\n",
        capture_output=True,
        text=True,
        check=False,
    )
    ignored = set(result.stdout.split())
    assert [f for f in files if f not in ignored] == []

    untracked = subprocess.run(
        ["git", "-C", str(tmp_path), "status", "--porcelain", "--untracked-files=all"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split("\n")
    assert [line for line in untracked if line and "migrated" in line] == []
