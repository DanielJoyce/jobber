"""Find data left in the old repo-relative layout and refuse to fork it
(specs/002-architecture.md#where-files-live).

Before the XDG move the database lived at ``./data/jobhunter.db`` in the checkout. Paths no
longer fall back to it (``jobhunter.config`` resolves explicit settings, then XDG defaults,
nothing else). Instead, every command first checks for an unmigrated old database and stops:
it must never create or use a second, empty database while the real one sits in the old
place.

Detection is narrow on purpose. Only ``data/jobhunter.db`` counts (a ``resume/`` or
``profile/`` folder alone is not a jobhunter checkout), and only in two places: the main
checkout of the git repository this package runs from (``git rev-parse --git-common-dir``, so
every worktree maps to the same main checkout) and the working directory.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from jobhunter import container
from jobhunter.config import Settings, resolve_path

LEGACY_DB = Path("data") / "jobhunter.db"
NOTE_NAME = "MOVED.txt"
MIGRATE_HINT = "run `jobhunter migrate-paths` (a dry run) to see the move"

# Status kinds.
OK = "ok"  # nothing old to worry about (the database may or may not exist yet)
UNMIGRATED = "unmigrated"  # an old database exists and the configured one does not
CONFLICT = "conflict"  # both exist and the old one was never migrated: refuse to pick


def package_checkout() -> Path | None:
    """The source checkout this package is imported from, or None for an installed copy."""
    root = Path(__file__).resolve().parents[2]
    if (root / "pyproject.toml").is_file() and (root / "src" / "jobhunter").is_dir():
        return root
    return None


def main_checkout() -> Path | None:
    """The main working tree of the repository this package runs from.

    A worktree's ``--git-common-dir`` is the main checkout's ``.git``, so every worktree (and
    the main checkout itself) maps to the same directory. Falls back to the package checkout
    when git is unavailable or the repository has no ordinary ``.git`` directory.
    """
    checkout = package_checkout()
    if checkout is None:
        return None
    try:
        out = subprocess.run(
            ["git", "-C", str(checkout), "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return checkout
    common = Path(out)
    if common.name == ".git" and common.is_dir():
        return common.parent
    return checkout


def candidate_roots() -> list[Path]:
    """Where old data is looked for: the main checkout, then the working directory."""
    roots: list[Path] = []
    for root in (main_checkout(), Path.cwd()):
        if root is not None and root.resolve() not in [r.resolve() for r in roots]:
            roots.append(root)
    return roots


def legacy_dbs() -> list[Path]:
    """Every old-layout database found in ``candidate_roots()``."""
    return [root / LEGACY_DB for root in candidate_roots() if (root / LEGACY_DB).is_file()]


def is_marked_moved(legacy_db: Path) -> bool:
    """True when the old ``data/`` folder carries a MOVED.txt (its database is a stale copy)."""
    return (legacy_db.parent / NOTE_NAME).is_file()


@dataclass
class Status:
    kind: str
    db_path: Path  # the database jobhunter is configured to use (resolved)
    unmigrated: list[Path] = field(default_factory=list)  # old databases with no MOVED.txt
    stale: list[Path] = field(default_factory=list)  # old databases next to a MOVED.txt
    message: str = ""

    @property
    def blocked(self) -> bool:
        return self.kind in (UNMIGRATED, CONFLICT)

    @property
    def warnings(self) -> list[str]:
        notes = [
            f"note: an old database is still at {p} although its folder says it was moved; "
            f"jobhunter uses {self.db_path}. Delete the old one once you have checked it."
            for p in self.stale
        ]
        if self.kind == OK and self.unmigrated:
            notes.append(
                "note: your old data is still at "
                + ", ".join(map(str, self.unmigrated))
                + f"; this command uses {self.db_path} because it is set explicitly. "
                + MIGRATE_HINT
                + "."
            )
        return notes


def check(settings: Settings) -> Status:
    """Decide whether commands may run against ``settings.paths.db_path``.

    * no old database found: OK (a missing database is created as usual: a fresh install);
    * the configured database *is* the old one (``data_dir = "data"``): OK, the user chose it;
    * an old database without MOVED.txt, and the configured one is missing: UNMIGRATED, every
      command stops so none creates an empty database next to the real one;
    * both exist and the configured one is a default (not set explicitly): CONFLICT, refuse to
      pick one silently. An explicitly configured, existing database (a one-off run on a copy)
      is allowed, with a note.
    """
    db_path = resolve_path(settings.paths.db_path).absolute()
    if container.is_container():
        return Status(OK, db_path)  # the image has no checkout (specs/018 C1)
    found = [p for p in legacy_dbs() if p.resolve() != db_path.resolve()]
    unmigrated = [p for p in found if not is_marked_moved(p)]
    stale = [p for p in found if is_marked_moved(p)]
    status = Status(OK, db_path, unmigrated, stale)
    if not unmigrated:
        return status
    old = ", ".join(map(str, unmigrated))
    explicit = not settings.paths.sources.get("db_path", "default").startswith("default")
    if not db_path.exists():
        status.kind = UNMIGRATED
        status.message = (
            f"your data is still in {old}; run `jobhunter migrate-paths` to move it to "
            f"{db_path.parent} (a dry run first; --apply does it). Nothing was created."
        )
        if explicit:
            status.message = (
                f"refusing to create a new empty database at {db_path} while your data is "
                f"still in {old}. Run `jobhunter migrate-paths`, or point the setting at an "
                "existing database."
            )
    elif not explicit:
        status.kind = CONFLICT
        status.message = (
            f"two databases: {db_path} (the default location) and {old} (the old layout, "
            "not migrated: no MOVED.txt beside it). jobhunter will not pick one. "
            "`jobhunter migrate-paths` shows what each holds. Move the one you do not want "
            "aside (rename it to a .unused name): if that is the old one, commands run again; "
            "if it is the new one, `jobhunter migrate-paths --apply` then moves the old one."
        )
    return status
