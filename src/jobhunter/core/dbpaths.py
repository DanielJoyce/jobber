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
    """The path a stored value names: relative values join ``data_dir``, absolute ones (rows
    ``relativize-paths`` has not rewritten yet) are returned as they are. No containment
    check: use :func:`resolve_inside` before reading or copying the file."""
    p = Path(stored.strip()).expanduser()
    if p.is_absolute():
        return p
    return Path(data_dir).expanduser() / p


def is_inside(path: Path | str, data_dir: Path | str) -> bool:
    """True when both the normalised path and its realpath lie under the data directory.

    The normpath catches ``..`` segments; the realpath catches a symlink inside the data dir
    that points out of it (``packets/5/resume-v1.pdf`` -> ``~/.ssh/id_rsa``).
    """
    p = Path(path).expanduser()
    if not p.is_absolute():
        return False
    roots = data_dir_roots(data_dir)

    def under(candidate: Path) -> bool:
        return any(candidate != r and candidate.is_relative_to(r) for r in roots)

    return under(Path(os.path.normpath(p))) and under(Path(os.path.realpath(p)))


def resolve_inside(stored: str | None, data_dir: Path | str) -> Path | None:
    """The file a stored value names, or None when it would leave the data directory.

    Every read of a stored path goes through this: a value edited to ``../.ssh/id_rsa`` (or a
    symlink planted under the data dir) must never be followed, attached or copied.
    """
    if not (stored or "").strip():
        return None
    p = resolve(stored, data_dir)
    return p if is_inside(p, data_dir) else None


def escapes(stored: str | None, data_dir: Path | str | None = None) -> bool:
    """A relative stored value that leaves the data directory: ``..`` always counts; with a
    ``data_dir``, so does one whose realpath (through a symlink) lands outside it."""
    v = (stored or "").strip()
    if not v or is_absolute(v):
        return False
    if os.path.normpath(v).split(os.sep)[0] == os.pardir:
        return True
    return data_dir is not None and resolve_inside(v, data_dir) is None


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
    """:func:`copy_into_attachments`, plus whether a new file was written.

    Every failure, including an unreadable source or an unwritable data dir, is a
    :class:`CopyError`, so callers can report it instead of failing the request.
    """
    tmp: Path | None = None
    try:
        if not src.is_file():
            raise CopyError(f"no such file: {src}")
        digest = sha256_file(src)
        stored, exists = attachment_target(src, digest, data_dir)
        if exists:
            return stored, False
        target = Path(data_dir).expanduser() / stored
        mkdir_private(target.parent)
        fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
        os.close(fd)
        tmp = Path(tmp_name)
        os.chmod(tmp, 0o600)
        shutil.copyfile(src, tmp)
        if sha256_file(tmp) != digest:
            raise CopyError(f"copy of {src} did not match its sha256; nothing stored")
        if target.exists():  # appeared meanwhile: never overwrite another file
            raise CopyError(f"{target} appeared while copying {src}")
        os.replace(tmp, target)
        return stored, True
    except CopyError:
        raise
    except OSError as exc:
        raise CopyError(f"cannot copy {src} into the data dir: {exc}") from exc
    finally:
        if tmp is not None:
            with contextlib.suppress(FileNotFoundError):
                tmp.unlink()


# --- the whole database ----------------------------------------------------------------------


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _rows(conn: sqlite3.Connection, table: str, column: str) -> list[tuple[int, str, int | None]]:
    """(id, value, application_id or None) for every non-empty value of one path column."""
    app = "application_id" if table == "attachment" else "NULL"
    return [
        (int(r[0]), str(r[1]), r[2])
        for r in conn.execute(
            f"SELECT id, {column}, {app} FROM {table} "  # names from PATH_COLUMNS
            f"WHERE trim(coalesce({column}, '')) <> '' ORDER BY id"
        )
    ]


def unsafe(value: str | None, data_dir: Path | str | None = None) -> bool:
    """A stored value that is not a plain path inside the data dir: absolute (written before
    C14) or relative but leaving it (see :func:`escapes`)."""
    return is_absolute(value) or escapes(value, data_dir)


def absolute_path_counts(
    conn: sqlite3.Connection, data_dir: Path | str | None = None
) -> dict[str, int]:
    """``{"table.column": n}`` of :func:`unsafe` values per path column (0 for tables this
    schema lacks). With ``data_dir``, relative values that symlink out of it count too."""
    tables = _tables(conn)
    return {
        f"{table}.{column}": (
            sum(unsafe(v, data_dir) for _, v, _ in _rows(conn, table, column))
            if table in tables
            else 0
        )
        for table, column in PATH_COLUMNS
    }


def count_absolute_paths(conn: sqlite3.Connection, data_dir: Path | str | None = None) -> int:
    """How many stored paths are absolute or escape the data dir. C13's preflight refuses
    while this is > 0; pass the data dir so symlink escapes are counted as well."""
    return sum(absolute_path_counts(conn, data_dir).values())


def absolute_path_count(db_path: Path, data_dir: Path) -> tuple[int | None, str]:
    """Stored paths that are absolute or escape the data dir (C14), read-only: ``(count,
    "")``, ``(None, "")`` when there is no database yet, ``(None, why)`` when unreadable.

    Shared by ``container preflight`` and the readiness checks (specs/019)."""
    if not db_path.is_file():
        return None, ""
    try:
        conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            return count_absolute_paths(conn, data_dir), ""
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return None, f"{type(exc).__name__}: {exc}"


RELATIVIZE = "relativize"
COPY = "copy"
DUPLICATE = "duplicate"  # an attachment row that would repeat another: removed
UNRESOLVED = "unresolved"


@dataclass
class Change:
    table: str
    row_id: int
    column: str
    old: str
    action: str  # RELATIVIZE, COPY, DUPLICATE or UNRESOLVED
    new: str | None = None
    note: str = ""
    application_id: int | None = None

    @property
    def where(self) -> str:
        return f"{self.table}.{self.column} id={self.row_id}"

    def describe(self) -> str:
        if self.action == RELATIVIZE:
            return f"{self.where}: {self.old} -> {self.new}"
        if self.action == COPY:
            return f"{self.where}: copy {self.old} -> {self.new}"
        if self.action == DUPLICATE:
            return f"{self.where}: remove, the application already has {self.new}"
        return f"{self.where}: cannot resolve {self.old} ({self.note})"


@dataclass
class Plan:
    data_dir: Path
    changes: list[Change] = field(default_factory=list)

    def of(self, action: str) -> list[Change]:
        return [c for c in self.changes if c.action == action]

    @property
    def actionable(self) -> bool:
        return bool(self.of(RELATIVIZE) or self.of(COPY) or self.of(DUPLICATE))

    @property
    def summary(self) -> str:
        return (
            f"{len(self.changes)} path(s) to fix: {len(self.of(RELATIVIZE))} under the data "
            f"dir, {len(self.of(COPY))} to copy into {ATTACHMENTS_SUBDIR}/, "
            f"{len(self.of(DUPLICATE))} duplicate(s) to remove, "
            f"{len(self.of(UNRESOLVED))} unresolved"
        )


def plan_relativize(conn: sqlite3.Connection, data_dir: Path | str) -> Plan:
    """What ``relativize-paths`` would change. Reads files (to hash them); writes nothing."""
    data_dir = Path(data_dir).expanduser()
    plan = Plan(data_dir=data_dir)
    tables = _tables(conn)
    taken: set[tuple[int | None, str]] = set()  # (application_id, path) attachment rows keep
    for table, column in PATH_COLUMNS:
        if table not in tables:
            continue
        rows = _rows(conn, table, column)
        if table == "attachment":
            taken |= {(a, v) for _, v, a in rows if not unsafe(v, data_dir)}
        for row_id, old, app_id in rows:
            if not unsafe(old, data_dir):
                continue
            change = Change(table, row_id, column, old, UNRESOLVED, application_id=app_id)
            plan.changes.append(change)
            if not is_absolute(old):
                change.note = "relative path that leaves the data dir; never followed"
                continue
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
            if table == "attachment" and change.new is not None:
                if (app_id, change.new) in taken:
                    change.action = DUPLICATE
                else:
                    taken.add((app_id, change.new))
    return plan


@dataclass
class Applied:
    rewritten: list[Change] = field(default_factory=list)
    removed: list[Change] = field(default_factory=list)  # duplicate attachment rows
    copied: list[str] = field(default_factory=list)  # stored values of files newly copied
    failed: list[Change] = field(default_factory=list)  # copy failed or the row changed


def apply_relativize(conn: sqlite3.Connection, plan: Plan) -> Applied:
    """Copy what the plan copies, then rewrite every planned row in one transaction.

    A row is updated only while it still holds the value the plan read. An attachment row
    whose new value another row of the same application already holds is deleted instead
    (the two named the same file). Unresolved rows are left alone. Back the database up
    before calling this (the CLI does).
    """
    out = Applied()
    ready: list[Change] = []
    for change in plan.changes:
        if change.action in (RELATIVIZE, DUPLICATE):
            ready.append(change)
        elif change.action == COPY:
            src = Path(change.old.strip()).expanduser()
            try:
                change.new, written = _copy(src, plan.data_dir)
            except CopyError as exc:
                change.note = str(exc)
                out.failed.append(change)
                continue
            if written:
                out.copied.append(change.new)
            ready.append(change)
    with db.transaction(conn):
        for change in ready:
            if (
                change.table == "attachment"
                and conn.execute(
                    "SELECT 1 FROM attachment WHERE application_id = ? AND path = ? AND id <> ?",
                    (change.application_id, change.new, change.row_id),
                ).fetchone()
            ):
                cur = conn.execute(
                    "DELETE FROM attachment WHERE id = ? AND path = ?",
                    (change.row_id, change.old),
                )
                bucket = out.removed
            else:
                cur = conn.execute(
                    f"UPDATE {change.table} SET {change.column} = ? "
                    f"WHERE id = ? AND {change.column} = ?",
                    (change.new, change.row_id, change.old),
                )
                bucket = out.rewritten
            if cur.rowcount:
                bucket.append(change)
            else:
                change.note = "row changed since the plan was made"
                out.failed.append(change)
    return out
