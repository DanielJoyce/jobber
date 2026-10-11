"""Data-dir-relative file paths in the database (specs/018-containers.md#paths-in-the-database,
ticket C14, decision D18).

The database stores the files it points at (``attachment.path``,
``application.cover_letter_path``, ``packet_document.rendered_path``) relative to the data
directory, and every read resolves them against the data directory the process has now. The
host sees ``/home/<you>/.local/share/jobhunter`` (or ``/var/home/<you>/...`` once anything
called ``.resolve()``), a container sees ``/data``; a relative path means the same file in all
three.

A file outside the data directory is copied into ``<data dir>/attachments/`` (sha256 checked)
and the copy is what the database points at: the container never mounts ``~/Documents``.

:func:`plan_relativize` and :func:`apply_relativize` back ``jobhunter db relativize-paths``,
which rewrites rows written before this. :func:`count_absolute_paths` is what C13's preflight
calls to refuse switching to container mode while any absolute path remains.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from jobhunter.core import db
from jobhunter.xdg import mkdir_private

# (table, column): every column that stores a filesystem path. Keyed on the table's ``id``.
PATH_COLUMNS: tuple[tuple[str, str], ...] = (
    ("attachment", "path"),
    ("application", "cover_letter_path"),
    ("packet_document", "rendered_path"),
)
ATTACHMENTS_SUBDIR = "attachments"
_CHUNK = 1 << 20


class CopyError(OSError):
    """A file could not be copied into the data directory, or its copy did not verify."""


# --- reading and writing one path ---------------------------------------------------------


def is_absolute(value: str | None) -> bool:
    """A stored value that names an absolute (or ``~``) path, i.e. one written before C14."""
    v = (value or "").strip()
    return v.startswith("/") or v.startswith("~")


def data_dir_roots(data_dir: Path | str) -> list[Path]:
    """The data directory as configured (absolute, ``~`` expanded) and its realpath.

    Both, because a row may hold either form: ``/home/<you>/...`` from the config and
    ``/var/home/<you>/...`` from a ``.resolve()`` (Fedora atomic links ``/home`` there).
    """
    configured = Path(os.path.abspath(Path(data_dir).expanduser()))
    real = Path(os.path.realpath(configured))
    return [configured] if real == configured else [configured, real]


def relative_to_data_dir(path: Path | str, data_dir: Path | str) -> str | None:
    """``path`` relative to the data directory (POSIX form), or None when it is outside it.

    Matches the path as written and its realpath against both forms of the data directory,
    so ``/home/<you>/...`` and ``/var/home/<you>/...`` of the same directory both match
    whichever one is configured. The data directory itself is not a file inside it (None).
    """
    p = Path(path).expanduser()
    if not p.is_absolute():
        return None
    roots = data_dir_roots(data_dir)
    for candidate in (Path(os.path.normpath(p)), Path(os.path.realpath(p))):
        for root in roots:
            if candidate != root and candidate.is_relative_to(root):
                return candidate.relative_to(root).as_posix()
    return None


def resolve(stored: str, data_dir: Path | str) -> Path:
    """The file a stored value names: relative values resolve against ``data_dir``.

    An absolute value (a row ``relativize-paths`` has not rewritten yet) is returned as it is.
    """
    p = Path(stored).expanduser()
    if p.is_absolute():
        return p
    return Path(data_dir).expanduser() / p


def store(path: Path | str, data_dir: Path | str) -> str:
    """The value to store for an existing file: relative to the data directory, after copying
    it into ``<data dir>/attachments/`` when it lives outside (raises :class:`CopyError`)."""
    rel = relative_to_data_dir(path, data_dir)
    if rel is not None:
        return rel
    return copy_into_attachments(Path(path).expanduser(), data_dir)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def attachment_target(src: Path, digest: str, data_dir: Path | str) -> tuple[str, bool]:
    """Where ``src`` (with content ``digest``) lands in ``attachments/``, and whether an
    identical copy is already there.

    The file's own name when free or already holding the same bytes; otherwise the name with
    ``-<first 12 hex of sha256>`` before the suffix. Returns the stored (relative) value.
    """
    directory = Path(data_dir).expanduser() / ATTACHMENTS_SUBDIR
    name = src.name
    tagged = f"{src.stem}-{digest[:12]}{src.suffix}"
    for candidate in (name, tagged):
        target = directory / candidate
        if not target.exists():
            return f"{ATTACHMENTS_SUBDIR}/{candidate}", False
        if target.is_file() and not target.is_symlink() and sha256_file(target) == digest:
            return f"{ATTACHMENTS_SUBDIR}/{candidate}", True
    raise CopyError(f"cannot place {src} in {directory}: {name} and {tagged} are both taken")


def copy_into_attachments(src: Path, data_dir: Path | str) -> str:
    """Copy ``src`` into ``<data dir>/attachments/`` and return the stored value.

    The copy is written to a temp file in that directory, made owner-only, and checked against
    the source's sha256 before it takes its name; an identical file already there is reused.
    """
    return _copy(Path(src), data_dir)[0]


def _copy(src: Path, data_dir: Path | str) -> tuple[str, bool]:
    """:func:`copy_into_attachments`, plus whether a new file was written."""
    if not src.is_file():
        raise CopyError(f"no such file: {src}")
    digest = sha256_file(src)
    stored, exists = attachment_target(src, digest, data_dir)
    if exists:
        return stored, False
    data_dir = Path(data_dir).expanduser()
    target = data_dir / stored
    mkdir_private(target.parent)
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        os.chmod(tmp, 0o600)
        shutil.copyfile(src, tmp)
        if sha256_file(tmp) != digest:
            raise CopyError(f"copy of {src} did not match its sha256; nothing stored")
        if target.exists():  # appeared meanwhile: never overwrite another file
            raise CopyError(f"{target} appeared while copying {src}")
        os.replace(tmp, target)
    except OSError as exc:
        if isinstance(exc, CopyError):
            raise
        raise CopyError(f"cannot copy {src} into {target.parent}: {exc}") from exc
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
    return stored, True


# --- the whole database ----------------------------------------------------------------------


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _absolute_rows(conn: sqlite3.Connection, table: str, column: str) -> list[tuple[int, str]]:
    return [
        (int(r[0]), str(r[1]))
        for r in conn.execute(
            f"SELECT id, {column} FROM {table} "  # names from PATH_COLUMNS
            f"WHERE substr(ltrim({column}), 1, 1) IN ('/', '~') ORDER BY id"
        )
    ]


def absolute_path_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """``{"table.column": n}`` for every path column (0 for tables this schema lacks)."""
    tables = _tables(conn)
    return {
        f"{table}.{column}": (len(_absolute_rows(conn, table, column)) if table in tables else 0)
        for table, column in PATH_COLUMNS
    }


def count_absolute_paths(conn: sqlite3.Connection) -> int:
    """How many stored paths are still absolute. C13's preflight refuses while this is > 0."""
    return sum(absolute_path_counts(conn).values())


RELATIVIZE = "relativize"
COPY = "copy"
UNRESOLVED = "unresolved"


@dataclass
class Change:
    table: str
    row_id: int
    column: str
    old: str
    action: str  # RELATIVIZE, COPY or UNRESOLVED
    new: str | None = None
    note: str = ""

    @property
    def where(self) -> str:
        return f"{self.table}.{self.column} id={self.row_id}"

    def describe(self) -> str:
        if self.action == RELATIVIZE:
            return f"{self.where}: {self.old} -> {self.new}"
        if self.action == COPY:
            return f"{self.where}: copy {self.old} -> {self.new}"
        return f"{self.where}: cannot resolve {self.old} ({self.note})"


@dataclass
class Plan:
    data_dir: Path
    changes: list[Change] = field(default_factory=list)

    def of(self, action: str) -> list[Change]:
        return [c for c in self.changes if c.action == action]

    @property
    def summary(self) -> str:
        return (
            f"{len(self.changes)} absolute path(s): {len(self.of(RELATIVIZE))} under the data "
            f"dir, {len(self.of(COPY))} to copy into {ATTACHMENTS_SUBDIR}/, "
            f"{len(self.of(UNRESOLVED))} unresolved"
        )


def plan_relativize(conn: sqlite3.Connection, data_dir: Path | str) -> Plan:
    """What ``relativize-paths`` would change. Reads files (to hash them); writes nothing."""
    data_dir = Path(data_dir).expanduser()
    plan = Plan(data_dir=data_dir)
    tables = _tables(conn)
    for table, column in PATH_COLUMNS:
        if table not in tables:
            continue
        for row_id, old in _absolute_rows(conn, table, column):
            change = Change(table, row_id, column, old, UNRESOLVED)
            path = Path(old.strip()).expanduser()
            rel = relative_to_data_dir(path, data_dir)
            if rel is not None:
                change.action, change.new = RELATIVIZE, rel
            elif not path.exists():
                change.note = "no such file outside the data dir"
            elif not path.is_file():
                change.note = "not a regular file"
            else:
                try:
                    change.new = attachment_target(path, sha256_file(path), data_dir)[0]
                    change.action = COPY
                except OSError as exc:
                    change.note = str(exc)
            plan.changes.append(change)
    return plan


@dataclass
class Applied:
    rewritten: list[Change] = field(default_factory=list)
    copied: list[str] = field(default_factory=list)  # stored values of files newly copied
    failed: list[Change] = field(default_factory=list)  # copy failed or the row changed


def apply_relativize(conn: sqlite3.Connection, plan: Plan) -> Applied:
    """Copy what the plan copies, then rewrite every planned row in one transaction.

    A row is updated only while it still holds the value the plan read. Unresolved rows are
    left alone. Back the database up before calling this (the CLI does).
    """
    out = Applied()
    ready: list[Change] = []
    for change in plan.changes:
        if change.action == RELATIVIZE:
            ready.append(change)
        elif change.action == COPY:
            src = Path(change.old.strip()).expanduser()
            try:
                change.new, written = _copy(src, plan.data_dir)
            except OSError as exc:
                change.note = str(exc)
                out.failed.append(change)
                continue
            if written:
                out.copied.append(change.new)
            ready.append(change)
    with db.transaction(conn):
        for change in ready:
            cur = conn.execute(
                f"UPDATE {change.table} SET {change.column} = ? "
                f"WHERE id = ? AND {change.column} = ?",
                (change.new, change.row_id, change.old),
            )
            if cur.rowcount:
                out.rewritten.append(change)
            else:
                change.note = "row changed since the plan was made"
                out.failed.append(change)
    return out
