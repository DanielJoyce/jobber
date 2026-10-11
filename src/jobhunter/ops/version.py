"""``jobhunter version`` (specs/018-containers.md#version-skew, ticket C12).

Prints what C13 preflight and C15 upgrade compare between the host and the image: package
version, the git commit (``$JOBHUNTER_BUILD_COMMIT`` in the image, else ``git`` in a source
checkout), the highest migration this code ships, and the database's schema read without
writing anything (a newer database is reported, not refused).
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
from dataclasses import asdict, dataclass
from importlib import metadata
from pathlib import Path

from jobhunter.core import db

BUILD_COMMIT_ENV = "JOBHUNTER_BUILD_COMMIT"


@dataclass(frozen=True)
class VersionInfo:
    package: str
    commit: str | None
    commit_source: str
    code_schema: int
    db_path: str
    db_schema: int | None
    db_state: str  # missing, new, current, pending, newer, unreadable
    unknown_migrations: list[int]
    pending_migrations: list[int]
    deployment: str  # host or container
    marker: str | None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)

    def lines(self) -> list[str]:
        commit = f"{self.commit} ({self.commit_source})" if self.commit else "unknown"
        if self.db_state == "missing":
            database = f"missing ({self.db_path})"
        else:
            detail = {
                "new": "new, no schema yet",
                "current": "up to date",
                "pending": f"behind; pending {_join(self.pending_migrations)}",
                "newer": f"NEWER than this code; unknown {_join(self.unknown_migrations)}",
                "unreadable": "could not be read",
            }[self.db_state]
            schema = "-" if self.db_schema is None else str(self.db_schema)
            database = f"{schema} ({detail}) {self.db_path}"
        deployment = self.deployment + (f" ({self.marker})" if self.marker else "")
        return [
            f"jobhunter {self.package}",
            f"commit:     {commit}",
            f"schema:     {self.code_schema} (highest migration in this code)",
            f"database:   {database}",
            f"deployment: {deployment}",
        ]


def _join(versions: list[int]) -> str:
    return ", ".join(map(str, versions))


def package_version() -> str:
    try:
        return metadata.version("jobhunter")
    except metadata.PackageNotFoundError:
        return "unknown"


def build_commit(env: dict[str, str] | None = None) -> tuple[str | None, str]:
    """``($JOBHUNTER_BUILD_COMMIT, "build")`` in the image; else the checkout's HEAD."""
    env = os.environ if env is None else env
    if value := env.get(BUILD_COMMIT_ENV, "").strip():
        return value, "build"
    here = Path(__file__).resolve().parent
    try:
        out = subprocess.run(
            ["git", "-C", str(here), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None, "unknown"
    commit = out.stdout.strip()
    return (commit, "git") if commit else (None, "unknown")


def database_state(db_path: Path) -> tuple[int | None, str, list[int], list[int]]:
    """``(schema, state, unknown, pending)`` read through a read-only connection."""
    if not db_path.is_file():
        return None, "missing", [], []
    try:
        conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error:
        return None, "unreadable", [], []
    try:
        applied = db.applied_versions(conn)
    except sqlite3.Error:
        return None, "unreadable", [], []
    finally:
        conn.close()
    if not applied:
        return 0, "new", [], []
    known = [v for v, _, _ in db._load_migrations()]
    unknown = sorted(applied - set(known))
    pending = [v for v in known if v not in applied]
    state = "newer" if unknown else "pending" if pending else "current"
    return max(applied), state, unknown, pending


def collect(db_path: Path, data_dir: Path) -> VersionInfo:
    commit, source = build_commit()
    schema, state, unknown, pending = database_state(db_path)
    marker = db.deployment_marker(data_dir)
    return VersionInfo(
        package=package_version(),
        commit=commit,
        commit_source=source,
        code_schema=db.code_version(),
        db_path=str(db_path),
        db_schema=schema,
        db_state=state,
        unknown_migrations=unknown,
        pending_migrations=pending,
        deployment="container" if marker else "host",
        marker=str(marker) if marker else None,
    )
