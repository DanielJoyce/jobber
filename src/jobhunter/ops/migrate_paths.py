"""Move the old repo-relative data (./data, ./profile, ./resume) to the XDG locations.

``plan()`` works out every move without touching anything (the default, a dry run). ``apply()``
performs them: the database is copied with SQLite's online backup API (after a timestamped
backup next to the new database), profile/resume/cache trees are copied. Originals are never
deleted by ``apply()``; each old directory gets a ``MOVED.txt`` pointing at the new location.
``remove_old()`` deletes an original only once it is byte-identical to its copy.

A destination that already exists and differs from its source is a conflict: nothing is
moved until the user resolves it. The exception is a destination that differs only because it
has been in use since the migration: ``apply()`` records a digest of each original in the
``MOVED.txt`` note, and an original that still matches its recorded digest is the unchanged
copy of what was migrated, so the (newer) destination is the live one, not a conflict.
"""

from __future__ import annotations

import filecmp
import hashlib
import os
import re
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from jobhunter.config import DB_FILE_NAME, Settings, legacy_locations, load_settings

NOTE_NAME = "MOVED.txt"
STAMP_FORMAT = "%Y%m%dT%H%M%S"

# Statuses of a planned item.
MOVE = "move"  # source exists, destination does not
DONE = "already migrated"  # destination exists and matches the source
MISSING = "nothing to move"  # no source
CONFLICT = "conflict"  # destination exists and differs: refuse
IN_USE = "already migrated, destination changed since"  # original untouched; the copy is live

_PINNED_PREFIXES = ("explicit", "config file", "env ", "override")


class MigrateError(Exception):
    """The migration cannot proceed (a conflict, or a source that cannot be read)."""


@dataclass
class Item:
    key: str  # "db", "cache", "profile", "resume", "backups"
    kind: str  # "db", "tree" or "file"
    src: Path
    dest: Path
    old_dir: Path  # where MOVED.txt goes
    status: str = MOVE
    detail: str = ""
    digest: str = ""  # sha256 of the source as copied (set by apply, or read from the note)


@dataclass
class Plan:
    items: list[Item]
    backup_path: Path | None  # the timestamped DB backup apply() will write, if any
    stamp: str = ""

    @property
    def conflicts(self) -> list[Item]:
        return [i for i in self.items if i.status == CONFLICT]

    @property
    def settled(self) -> list[Item]:
        """Items whose destination already holds the data (identical, or live since)."""
        return [i for i in self.items if i.status in (DONE, IN_USE)]

    @property
    def moves(self) -> list[Item]:
        return [i for i in self.items if i.status == MOVE]


def _pinned(settings: Settings, name: str) -> bool:
    return settings.paths.sources[name].startswith(_PINNED_PREFIXES)


# ─── comparison ─────────────────────────────────────────────────────────────


def _files_of(root: Path) -> dict[str, Path]:
    found: dict[str, Path] = {}
    for dirpath, _dirs, names in os.walk(root):
        for name in names:
            path = Path(dirpath) / name
            rel = path.relative_to(root).as_posix()
            if rel == NOTE_NAME:
                continue
            found[rel] = path
    return found


def trees_match(a: Path, b: Path) -> bool:
    """True when both directories hold the same files with the same bytes (MOVED.txt aside)."""
    fa, fb = _files_of(a), _files_of(b)
    if fa.keys() != fb.keys():
        return False
    return all(filecmp.cmp(fa[k], fb[k], shallow=False) for k in fa)


def _ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)


def _dump(path: Path) -> list[str]:
    conn = _ro(path)
    try:
        return list(conn.iterdump())
    finally:
        conn.close()


def dbs_match(a: Path, b: Path) -> bool:
    """Same file bytes, or the same logical content (a WAL database's main file lags)."""
    if filecmp.cmp(a, b, shallow=False):
        return True
    try:
        return _dump(a) == _dump(b)
    except sqlite3.Error:
        return False


def _same(item: Item) -> bool:
    if item.kind == "db":
        return dbs_match(item.src, item.dest)
    if item.kind == "tree":
        return item.dest.is_dir() and trees_match(item.src, item.dest)
    return item.dest.is_file() and filecmp.cmp(item.src, item.dest, shallow=False)


def digest(item: Item) -> str:
    """sha256 of the source as it is now: file bytes, every file of a tree, or the logical
    content of a database (its main file lags behind its WAL)."""
    h = hashlib.sha256()
    if item.kind == "db":
        for stmt in _dump(item.src):
            h.update(stmt.encode("utf-8", "surrogatepass") + b"\n")
    elif item.kind == "tree":
        for rel, path in sorted(_files_of(item.src).items()):
            h.update(rel.encode() + b"\0" + _file_sha(path).encode() + b"\n")
    else:
        h.update(_file_sha(item.src).encode())
    return h.hexdigest()


def _file_sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _safe_digest(item: Item) -> str:
    try:
        return digest(item)
    except (OSError, sqlite3.Error):
        return ""


_DIGEST_LINE = re.compile(r"^  sha256 (?P<src>.+) (?P<hex>[0-9a-f]{64})$")


def _recorded(item: Item) -> str:
    """The digest ``apply()`` recorded for this item's original in the note, or ""."""
    note = item.old_dir / NOTE_NAME
    try:
        lines = note.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    if f"  {item.src} -> {item.dest}" not in lines:
        return ""
    for line in lines:
        m = _DIGEST_LINE.match(line)
        if m and m["src"] == str(item.src):
            return m["hex"]
    return ""


def _remember(item: Item) -> str:
    item.digest = _recorded(item)
    return item.digest


# ─── planning ───────────────────────────────────────────────────────────────


def plan(
    settings: Settings | None = None,
    *,
    legacy_root: Path | None = None,
    now: datetime | None = None,
) -> Plan:
    """List every move. ``settings`` must have been loaded with ``legacy_fallback=False``
    so its paths are the destinations, not the old locations still in use."""
    settings = settings or load_settings(legacy_fallback=False)
    old = legacy_locations(legacy_root)
    paths = settings.paths
    stamp = (now or datetime.now()).strftime(STAMP_FORMAT)
    items: list[Item] = []

    def add(key: str, field_name: str, src: Path, dest: Path, kind: str | None = None) -> None:
        if field_name and _pinned(settings, field_name):
            return  # the user chose this location; there is nothing to migrate it from
        if src.resolve() == dest.resolve():
            return
        kind = kind or ("file" if src.is_file() else "tree")
        # profile/ and resume/ carry their own note; everything else lives under data/.
        old_dir = src if kind == "tree" and key in ("profile", "resume") else src.parent
        items.append(Item(key, kind, src, dest, old_dir))

    add("db", "db_path", old["db_path"], paths.db_path, kind="db")
    add("profile", "profile_dir", old["profile_dir"], paths.profile_dir, kind="tree")
    add("resume", "resume_path", old["resume_path"], paths.resume_path)
    add("cache", "cache_dir", old["cache_dir"], paths.cache_dir, kind="tree")
    # Backups were kept in data/backups; they follow the database.
    if not _pinned(settings, "data_dir"):
        add("backups", "", old["data_dir"] / "backups", paths.data_dir / "backups", kind="tree")

    backup_path: Path | None = None
    for item in items:
        if not item.src.exists():
            item.status, item.detail = MISSING, f"nothing at {item.src}"
        elif item.dest.exists():
            if _same(item):
                item.status = DONE
            elif _remember(item) and _safe_digest(item) == item.digest:
                item.status = IN_USE
                item.detail = f"{item.dest} has changed since it was copied from {item.src}"
            elif item.digest:
                item.status = CONFLICT
                item.detail = (
                    f"{item.src} was changed after it was migrated, and {item.dest} (the "
                    "copy in use) has changed too; compare them and keep what you need"
                )
            else:
                item.status = CONFLICT
                item.detail = f"{item.dest} already exists and differs from {item.src}"
        elif item.key == "db":
            backup_path = paths.data_dir / "backups" / f"jobhunter-{stamp}.db"
    return Plan(items, backup_path, stamp)


def render_plan(p: Plan, *, apply: bool, remove_old: bool) -> list[str]:
    head = "migrate-paths: applying" if apply else "migrate-paths: dry run, nothing changed"
    lines = [head + (" (remove originals)" if remove_old else "")]
    for item in p.items:
        lines.append(f"  [{item.status}] {item.key}: {item.src} -> {item.dest}")
        if item.detail and item.status == CONFLICT:
            lines.append(f"      {item.detail}")
        if item.key == "db" and item.status == MOVE and p.backup_path:
            lines.append(f"      sqlite backup first: {p.backup_path}")
        if item.status == IN_USE:
            lines.append(f"      {item.detail}; the original is unchanged")
        if item.status in (MOVE, DONE, IN_USE) and item.kind != "file":
            lines.append(f"      note: {item.old_dir / NOTE_NAME}")
    if not p.items:
        lines.append("  nothing to do: no old locations apply to this configuration")
    if remove_old:
        lines.append("  originals are removed only where the copy is identical")
    if p.conflicts:
        lines.append(
            "refusing: a destination already holds different data. Compare the two and keep "
            "what you need; do not move aside a database or folder that is in use."
        )
    elif not apply and p.moves:
        lines.append("run again with --apply to perform these moves (originals are kept).")
    elif not apply and not remove_old and p.settled:
        lines.append("the originals are still in place; --apply --remove-old deletes them.")
    return lines


# ─── applying ───────────────────────────────────────────────────────────────


def _mkdir_private(path: Path) -> None:
    """Create ``path`` and any missing parents, each owner-only (user data lives here)."""
    missing = [p for p in (path, *path.parents) if not p.exists()]
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)
        os.chmod(directory, 0o700)


def _copy_db(src: Path, dest: Path, backup: Path) -> None:
    """Back up ``src`` with the online backup API, then install that copy at ``dest``."""
    _mkdir_private(backup.parent)
    source = _ro(src)
    try:
        target = sqlite3.connect(backup)
        try:
            source.backup(target)
            ok = target.execute("PRAGMA integrity_check").fetchone()
        finally:
            target.close()
    finally:
        source.close()
    if not ok or ok[0] != "ok":
        raise MigrateError(f"backup of {src} failed its integrity check; nothing was moved")
    os.chmod(backup, 0o600)
    _mkdir_private(dest.parent)
    fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=f".{DB_FILE_NAME}.", suffix=".tmp")
    os.close(fd)
    try:
        shutil.copyfile(backup, tmp)
        os.chmod(tmp, 0o600)
        os.replace(tmp, dest)
    finally:
        Path(tmp).unlink(missing_ok=True)


def _copy_tree(src: Path, dest: Path) -> None:
    _mkdir_private(dest.parent)
    staging = dest.with_name(dest.name + ".migrating")
    shutil.rmtree(staging, ignore_errors=True)
    try:
        shutil.copytree(src, staging, ignore=shutil.ignore_patterns(NOTE_NAME))
        os.rename(staging, dest)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)  # personal data: leave no stray copy
        raise


def _copy_file(src: Path, dest: Path) -> None:
    _mkdir_private(dest.parent)
    fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=f".{dest.name}.", suffix=".tmp")
    os.close(fd)
    try:
        shutil.copy2(src, tmp)
        os.replace(tmp, dest)
    finally:
        Path(tmp).unlink(missing_ok=True)


def _write_note(old_dir: Path, items: list[Item], stamp: str, removed: bool) -> None:
    """Write MOVED.txt. Originals still present keep a recorded digest, so a later run can
    tell a copy that has been in use from a conflict."""
    lines = [f"jobhunter moved these on {stamp}:"]
    lines += [f"  {i.src} -> {i.dest}" for i in items]
    if removed:
        lines.append("The originals were removed with --remove-old; use the new locations.")
    else:
        lines.append("The files here are the original copies, kept until you run")
        lines.append("`jobhunter migrate-paths --apply --remove-old`. jobhunter now uses the")
        lines.append("new locations; run `jobhunter paths` to see them.")
    lines += [f"  sha256 {i.src} {i.digest}" for i in items if i.digest and i.src.exists()]
    (old_dir / NOTE_NAME).write_text("\n".join(lines) + "\n", encoding="utf-8")


def apply(p: Plan) -> list[str]:
    """Perform the planned moves. Raises ``MigrateError`` (before changing anything) on a
    conflict, or if a copy fails part-way (the items already copied keep their notes).
    Never deletes originals."""
    if p.conflicts:
        raise MigrateError("; ".join(i.detail for i in p.conflicts) + " (nothing was changed)")
    done: list[str] = []
    copied: list[Item] = []
    # The backups tree goes first: the database backup is written into <data>/backups, which
    # must not exist yet when the old backups are installed there.
    order = sorted(p.moves, key=lambda i: i.key != "backups")
    try:
        for item in order:
            item.digest = _safe_digest(item)
            if item.kind == "db":
                assert p.backup_path is not None
                _copy_db(item.src, item.dest, p.backup_path)
            elif item.kind == "tree":
                _copy_tree(item.src, item.dest)
            else:
                _copy_file(item.src, item.dest)
            if not _same(item):
                raise MigrateError(f"copy of {item.src} to {item.dest} does not match the original")
            copied.append(item)
            done.append(f"  moved {item.key}: {item.src} -> {item.dest}")
    except (OSError, sqlite3.Error) as exc:
        raise MigrateError(
            f"{exc}. Already copied: {[i.key for i in copied] or 'none'}; the originals are "
            "untouched, so it is safe to run again."
        ) from exc
    finally:
        # Record what was copied even on failure, so a rerun knows those are migrated.
        _write_notes(p, copied)
    if p.backup_path and p.backup_path.exists():
        done.append(f"  database backup: {p.backup_path}")
    return done


def _write_notes(p: Plan, copied: list[Item]) -> None:
    by_dir: dict[Path, list[Item]] = {}
    for item in [*copied, *p.settled]:
        if item.kind == "file" or not item.src.exists():
            continue
        if not item.digest:
            item.digest = _safe_digest(item)
        group = by_dir.setdefault(item.old_dir, [])
        if item not in group:
            group.append(item)
    for old_dir, group in by_dir.items():
        _write_note(old_dir, group, p.stamp, removed=False)


def remove_old(p: Plan) -> list[str]:
    """Delete originals whose copy is identical. Call after ``apply()`` (or when every item
    is already migrated). Anything that differs or has no copy is kept and reported."""
    out: list[str] = []
    removed: list[Item] = []
    for item in p.items:
        if item.status == MISSING or not item.src.exists():
            continue
        if not item.dest.exists() or not (_same(item) or item.status == IN_USE):
            out.append(f"  kept {item.key}: {item.src} (no identical copy at {item.dest})")
            continue
        if item.kind == "db":
            for suffix in ("", "-wal", "-shm"):
                Path(str(item.src) + suffix).unlink(missing_ok=True)
        elif item.kind == "file":
            item.src.unlink()
        elif item.src == item.old_dir:  # profile/, resume/: keep the folder for its note
            for child in item.src.iterdir():
                if child.name == NOTE_NAME:
                    continue
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child)
                else:
                    child.unlink()
        else:
            shutil.rmtree(item.src)
        removed.append(item)
        out.append(f"  removed original {item.key}: {item.src}")
        if item.status == IN_USE:
            out.append(f"      (the copy at {item.dest} is newer and was left as it is)")
    by_dir: dict[Path, list[Item]] = {}
    for item in p.items:
        # Originals that were kept stay in the note with their digests.
        if item.kind != "file" and (item in removed or (item.digest and item.src.exists())):
            by_dir.setdefault(item.old_dir, []).append(item)
    for old_dir, group in by_dir.items():
        if old_dir.is_dir():
            _write_note(old_dir, group, p.stamp, removed=all(i in removed for i in group))
    return out
