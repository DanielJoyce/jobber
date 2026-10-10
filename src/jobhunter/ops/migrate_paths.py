"""Move the old repo-relative data (./data, ./profile, ./resume) to the XDG locations
(specs/002-architecture.md#where-files-live).

``plan()`` works everything out without changing anything (the default, a dry run).
``apply()`` then, in order:

1. refuses unless no other process has the old database open (a scan of ``/proc`` plus an
   exclusive SQLite lock, which it holds until the end so nothing can open it meanwhile);
2. copies the database with SQLite's online backup API, ``data/backups``, ``data/cache``,
   ``profile/`` and ``resume/`` to their new homes;
3. verifies every copy: ``integrity_check`` and the same tables, schema and row counts for the
   database, the same sha256 for every file of the trees;
4. makes the data directory 0700 and the database, backups, profile and resume 0600;
5. renames the old folders in place to ``data.migrated-YYYYMMDD`` (and so on) with a
   ``MOVED.txt`` inside. A rename is atomic and reversible, and afterwards nothing can keep
   using the old path. Nothing is deleted: the user removes the ``.migrated`` folders by hand.

A failure before step 5 removes what this run copied and leaves the old layout untouched.
A destination that already exists and differs is a conflict, and nothing is done until the
user resolves it.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from jobhunter import legacy_data
from jobhunter.config import Settings, resolve_path
from jobhunter.xdg import mkdir_private

NOTE_NAME = legacy_data.NOTE_NAME
TIMERS = "jobhunter-run.timer jobhunter-collect.timer jobhunter-verify.timer"
STOP_HINT = f"stop `jobhunter console` (Ctrl-C) and the timers: systemctl --user stop {TIMERS}"
START_HINT = f"restart the timers if you use them: systemctl --user start {TIMERS}"
LOCK_TIMEOUT_S = 1.0

# Item states.
MOVE = "move"
SAME = "same"  # the destination already holds an identical copy: nothing to copy
CONFLICT = "conflict"


class MigrateError(Exception):
    """The migration cannot proceed; the message says what to do."""


@dataclass
class Item:
    key: str  # "db", "backups", "cache", "profile", "resume"
    kind: str  # "db", "tree" or "file"
    src: Path
    dest: Path
    folder: str  # the old top-level folder it lives in: "data", "profile" or "resume"
    state: str = MOVE
    detail: str = ""


@dataclass
class Plan:
    root: Path | None  # the folder holding the old layout, or None when there is none
    why: str = ""  # how the root was found
    items: list[Item] = field(default_factory=list)
    renames: list[tuple[Path, Path]] = field(default_factory=list)
    left_behind: list[str] = field(default_factory=list)  # in data/ but not migrated
    problems: list[str] = field(default_factory=list)  # refuse while any
    archives: list[Path] = field(default_factory=list)  # earlier *.migrated-* folders
    notes: list[str] = field(default_factory=list)

    @property
    def nothing_to_do(self) -> bool:
        return not self.problems and not self.renames


# ─── digests and comparisons ────────────────────────────────────────────────


def _file_sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def tree_digests(root: Path) -> dict[str, str]:
    """Relative path -> sha256 for every file below ``root`` (symlinks by their target)."""
    if root.is_file():
        return {".": _file_sha(root)}
    found: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in [*dirnames, *filenames]:
            path = Path(dirpath) / name
            rel = path.relative_to(root).as_posix()
            if path.is_symlink():
                found[rel] = "symlink:" + os.readlink(path)
            elif name in filenames:
                found[rel] = _file_sha(path)
    return found


def _has_files(path: Path) -> bool:
    if path.is_file() or path.is_symlink():
        return True
    return any(names for _d, _s, names in os.walk(path))


def _connect_ro(path: Path) -> sqlite3.Connection:
    """Open for reading without creating anything beside the database. A plain read-only
    open of a WAL database creates -wal and -shm files when they are missing; with no WAL
    content the main file is complete, so it is opened as immutable instead."""
    wal = Path(f"{path}-wal")
    has_wal = wal.is_file() and wal.stat().st_size > 0
    query = "mode=ro" if has_wal else "mode=ro&immutable=1"
    return sqlite3.connect(f"{path.resolve().as_uri()}?{query}", uri=True)


def table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    names = [
        r[0]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    ]
    return {
        n: conn.execute('SELECT count(*) FROM "{}"'.format(n.replace('"', '""'))).fetchone()[0]
        for n in names
    }


def _schema(conn: sqlite3.Connection) -> list[tuple[str, str, str | None]]:
    return conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY type, name").fetchall()


def _counts_summary(path: Path) -> str:
    try:
        conn = _connect_ro(path)
        try:
            counts = table_counts(conn)
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return f"unreadable: {exc}"
    shown = {k: counts[k] for k in ("job", "application", "score") if k in counts}
    body = ", ".join(f"{k}={v}" for k, v in shown.items()) or "no jobhunter tables"
    return f"{len(counts)} tables; {body}"


# ─── planning ───────────────────────────────────────────────────────────────


def _archive_name(parent: Path, name: str, today: date) -> Path:
    base = f"{name}.migrated-{today:%Y%m%d}"
    candidate, n = parent / base, 2
    while candidate.exists() or candidate.is_symlink():
        candidate, n = parent / f"{base}-{n}", n + 1
    return candidate


def _find_archives(roots: list[Path]) -> list[Path]:
    found: list[Path] = []
    for root in roots:
        for name in ("data", "profile", "resume"):
            found += sorted(root.glob(f"{name}.migrated-*"))
    return found


def _find_root(from_dir: Path | None) -> tuple[Path | None, str, list[str]]:
    if from_dir is not None:
        root = resolve_path(from_dir).absolute()
        if not root.is_dir():
            return None, "", [f"--from {root}: no such directory"]
        if legacy_data.is_marked_moved(root / legacy_data.LEGACY_DB):
            return None, "", [f"{root / 'data'} has a {NOTE_NAME}: it was migrated already"]
        return root, "--from", []
    found = [p for p in legacy_data.legacy_dbs() if not legacy_data.is_marked_moved(p)]
    if not found:
        return None, "", []
    if len(found) > 1:
        listed = ", ".join(map(str, found))
        return None, "", [f"old databases in more than one place ({listed}); pass --from DIR"]
    root = found[0].parent.parent
    main = legacy_data.main_checkout()
    if main is not None and main.resolve() == root.resolve():
        return root, "the main checkout of this jobhunter install", []
    return root, "the working directory", []


def plan(settings: Settings, *, from_dir: Path | None = None, today: date | None = None) -> Plan:
    """Work out the migration. Reads only: nothing on disk changes."""
    today = today or date.today()
    root, why, problems = _find_root(from_dir)
    roots = [root] if root is not None else legacy_data.candidate_roots()
    p = Plan(root, why, problems=problems, archives=_find_archives(roots))
    if root is None:
        return p
    paths = settings.paths
    data_dir = resolve_path(paths.data_dir).absolute()
    wanted = [
        ("db", "db", root / legacy_data.LEGACY_DB, resolve_path(paths.db_path), "data"),
        ("backups", "tree", root / "data" / "backups", data_dir / "backups", "data"),
        ("cache", "tree", root / "data" / "cache", resolve_path(paths.cache_dir), "data"),
        ("profile", "tree", root / "profile", resolve_path(paths.profile_dir), "profile"),
        ("resume", "tree", root / "resume", resolve_path(paths.resume_path), "resume"),
    ]
    pinned_here: set[str] = set()
    for key, kind, src, dest, folder in wanted:
        if not (src.exists() or src.is_symlink()):
            continue
        dest = dest.absolute()
        if src.resolve() == dest.resolve():
            pinned_here.add(folder)  # configured to stay where it is: leave the folder alone
            p.notes.append(f"{key} stays at {src}: your settings point there")
            continue
        if kind == "tree" and src.is_file():
            kind = "file"
        p.items.append(Item(key, kind, src, dest, folder))

    for item in p.items:
        if not (item.dest.exists() or item.dest.is_symlink()):
            continue
        if item.kind == "db":
            item.state = CONFLICT
            item.detail = (
                f"{item.dest} already exists ({_counts_summary(item.dest)}); the old one at "
                f"{item.src} ({_counts_summary(item.src)}) was never migrated. jobhunter will "
                "not pick one. Rename the one you do not want to a .unused name: the old one "
                "(then nothing is left to migrate), or the new one (then run this again)"
            )
        elif not _has_files(item.dest):
            continue  # an empty folder: copy into its place
        elif tree_digests(item.src) == tree_digests(item.dest):
            item.state = SAME
        else:
            item.state = CONFLICT
            item.detail = (
                f"{item.dest} already exists and differs from {item.src}; compare them, keep "
                "what you need in one place, and run this again"
            )
    p.problems += [i.detail for i in p.items if i.state == CONFLICT]

    for folder in ("data", "profile", "resume"):
        src = root / folder
        if folder in pinned_here or not any(i.folder == folder for i in p.items):
            continue
        p.renames.append((src, _archive_name(root, folder, today)))
        if folder == "data" and src.is_dir():
            moved = {"jobhunter.db", "jobhunter.db-wal", "jobhunter.db-shm", "backups", "cache"}
            p.left_behind = sorted(c.name for c in src.iterdir() if c.name not in moved)
    return p


def render_plan(p: Plan, *, apply: bool) -> list[str]:
    lines = ["migrate-paths: applying" if apply else "migrate-paths: dry run, nothing changed"]
    if not p.items and not p.problems:
        if p.root is None:
            lines.append("nothing to migrate: no old data/jobhunter.db that has not been moved.")
        else:
            lines.append(f"nothing to migrate in {p.root}: no data/, profile/ or resume/.")
        lines += [f"  note: {note}" for note in p.notes]
        lines += _archive_lines(p.archives)
        return lines
    if p.root is not None:
        lines.append(f"old data: {p.root} ({p.why})")
    for item in p.items:
        verb = {MOVE: "copy", SAME: "same", CONFLICT: "conflict"}[item.state]
        lines.append(f"  {verb:<8} {item.key:<8} {item.src} -> {item.dest}")
        if item.key == "db" and item.state == MOVE:
            lines.append("           sqlite backup, checked with integrity_check and row counts")
        if item.state == SAME:
            lines.append("           already identical at the destination; nothing to copy")
    for note in p.notes:
        lines.append(f"  note: {note}")
    if p.renames and not p.problems:
        lines.append("then rename the old folders, so nothing can keep using them:")
        lines += [f"  {src} -> {dest}" for src, dest in p.renames]
    if p.left_behind and not p.problems:
        lines.append("  not migrated, kept in the renamed data folder: " + ", ".join(p.left_behind))
    if p.problems:
        lines += [f"refusing: {problem}" for problem in p.problems]
        return lines
    if not apply and p.renames:
        lines.append("before --apply, stop everything that uses the database:")
        lines.append(f"  {STOP_HINT}")
        lines.append("run again with --apply to do it.")
    return lines


def _archive_lines(archives: list[Path]) -> list[str]:
    if not archives:
        return []
    return [
        "the old copies are still kept; delete them by hand once you are satisfied:",
        "  rm -rf " + " ".join(f"'{a}'" for a in archives),
    ]


# ─── safety checks ──────────────────────────────────────────────────────────


def other_holders(paths: list[Path]) -> list[str]:
    """Other processes with any of ``paths`` open, as "pid (name)" (Linux /proc; else [])."""
    targets = set()
    for path in paths:
        try:
            st = path.stat()
        except OSError:
            continue
        targets.add((st.st_dev, st.st_ino))
    proc = Path("/proc")
    if not targets or not proc.is_dir():
        return []
    me = os.getpid()
    holders: list[str] = []
    for entry in proc.iterdir():
        if not entry.name.isdigit() or int(entry.name) == me:
            continue
        try:
            fds = list((entry / "fd").iterdir())
        except OSError:
            continue  # gone, or another user's process
        for fd in fds:
            try:
                st = fd.stat()
            except OSError:
                continue
            if (st.st_dev, st.st_ino) in targets:
                try:
                    name = (entry / "comm").read_text(encoding="utf-8").strip()
                except OSError:
                    name = "?"
                holders.append(f"{entry.name} ({name})")
                break
    return holders


def _busy(db: Path, who: list[str]) -> MigrateError:
    held = f" (held by: {', '.join(who)})" if who else ""
    return MigrateError(
        f"{db} is open in another process{held}. {STOP_HINT}. Then run this again; nothing "
        "was changed."
    )


def lock_db(db: Path) -> sqlite3.Connection:
    """Hold an exclusive lock on ``db`` until the returned connection is closed.

    In WAL mode every open connection holds a shared lock, so the exclusive lock is refused
    while any other connection (in this process or another) has the database open, and once
    taken it keeps new ones out.
    """
    who = other_holders([db, Path(f"{db}-wal"), Path(f"{db}-shm")])
    if who:
        raise _busy(db, who)
    conn = sqlite3.connect(db, timeout=LOCK_TIMEOUT_S, isolation_level=None)
    try:
        conn.execute("PRAGMA locking_mode=EXCLUSIVE")
        conn.execute("BEGIN EXCLUSIVE")
        conn.execute("COMMIT")
    except sqlite3.OperationalError as exc:
        conn.close()
        raise _busy(db, []) from exc
    return conn


# ─── applying ───────────────────────────────────────────────────────────────


def make_private(path: Path) -> None:
    """Owner-only modes: 0700 for directories, 0600 for files, recursively. Symlinks are
    left alone."""
    if path.is_symlink() or not path.exists():
        return
    if path.is_file():
        os.chmod(path, 0o600)
        return
    os.chmod(path, 0o700)
    for dirpath, dirnames, filenames in os.walk(path):
        for name in dirnames:
            child = Path(dirpath) / name
            if not child.is_symlink():
                os.chmod(child, 0o700)
        for name in filenames:
            child = Path(dirpath) / name
            if not child.is_symlink():
                os.chmod(child, 0o600)


def secure_data(settings: Settings, also: tuple[Path, ...] = ()) -> list[Path]:
    """Make the data directory 0700 and the database, backups, profile and resume private.
    Returns what it touched. The cache (public job pages) keeps its modes.

    Folders are made private recursively only inside the data directory, or when listed in
    ``also`` (copies this run made): a profile or resume path set explicitly to some shared
    folder elsewhere is not chmodded wholesale."""
    paths = settings.paths
    data_dir = resolve_path(paths.data_dir).absolute()
    db_path = resolve_path(paths.db_path)
    touched: list[Path] = []
    if data_dir.is_dir() and not data_dir.is_symlink():
        os.chmod(data_dir, 0o700)
        touched.append(data_dir)
    for path in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
        if path.is_file() and not path.is_symlink():
            os.chmod(path, 0o600)
            touched.append(path)
    for path in (
        data_dir / "backups",
        resolve_path(paths.profile_dir).absolute(),
        resolve_path(paths.resume_path).absolute(),
    ):
        inside = path.is_relative_to(data_dir) or path in also
        if inside and path.exists() and not path.is_symlink():
            make_private(path)
            touched.append(path)
    return touched


def _copy_db(lock: sqlite3.Connection, item: Item) -> str:
    """Back up the locked database into a temp file beside the destination, verify it, then
    move it into place. Returns a one-line summary."""
    with contextlib.suppress(sqlite3.Error):
        lock.execute("PRAGMA wal_checkpoint(TRUNCATE)")  # fold the WAL into the old file too
    mkdir_private(item.dest.parent)
    fd, tmp_name = tempfile.mkstemp(dir=item.dest.parent, prefix=".jobhunter-migrate.")
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        target = sqlite3.connect(tmp)
        try:
            lock.backup(target)
            ok = target.execute("PRAGMA integrity_check").fetchone()
            if not ok or ok[0] != "ok":
                raise MigrateError(f"the copy of {item.src} failed integrity_check: {ok}")
            src_counts, dest_counts = table_counts(lock), table_counts(target)
            if src_counts != dest_counts or _schema(lock) != _schema(target):
                raise MigrateError(f"the copy of {item.src} has different tables or row counts")
        finally:
            target.close()
        os.chmod(tmp, 0o600)
        os.replace(tmp, item.dest)
    finally:
        tmp.unlink(missing_ok=True)
    rows = sum(dest_counts.values())
    return f"{len(dest_counts)} tables, {rows} rows, integrity ok"


def _copy_tree(item: Item) -> str:
    """Copy into a staging name beside the destination, then rename it into place."""
    mkdir_private(item.dest.parent)
    if item.dest.is_dir():
        shutil.rmtree(item.dest)  # an empty folder (checked by plan)
    staging = item.dest.with_name(f".{item.dest.name}.migrating")
    shutil.rmtree(staging, ignore_errors=True)
    try:
        if item.kind == "file":
            shutil.copy2(item.src, staging)
        else:
            shutil.copytree(item.src, staging, symlinks=True)
        os.rename(staging, item.dest)
    except BaseException:
        if staging.is_dir():
            shutil.rmtree(staging, ignore_errors=True)
        else:
            staging.unlink(missing_ok=True)
        raise
    digests = tree_digests(item.src)
    if digests != tree_digests(item.dest):
        raise MigrateError(f"the copy of {item.src} at {item.dest} does not match it")
    return f"{len(digests)} file{'s' if len(digests) != 1 else ''}, sha256 verified"


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, ignore_errors=True)
    else:
        path.unlink(missing_ok=True)


def _note(p: Plan, folder: str, archive: Path, today: date) -> str:
    moved = [i for i in p.items if i.folder == folder]
    lines = [f"jobhunter migrate-paths moved this folder on {today:%Y-%m-%d}."]
    lines.append(f"It was {p.root / folder if p.root else folder}; jobhunter now uses:")
    lines += [f"  {i.src.name} -> {i.dest}" for i in moved]
    lines.append("This folder is the untouched original, kept so you can check the copy.")
    lines.append(f"Delete it by hand once you are satisfied: rm -rf '{archive}'")
    return "\n".join(lines) + "\n"


def apply(p: Plan, settings: Settings, *, today: date | None = None) -> list[str]:
    """Perform the plan. Raises ``MigrateError`` with nothing changed in the old layout if a
    check fails or a copy does not verify."""
    if p.problems:
        raise MigrateError("; ".join(p.problems))
    if p.root is None or not p.renames:
        return []
    today = today or date.today()
    out: list[str] = []
    db_item = next((i for i in p.items if i.key == "db"), None)
    lock = lock_db(db_item.src) if db_item is not None else None
    created: list[Path] = []
    try:
        try:
            mkdir_private(resolve_path(settings.paths.data_dir))
            for item in sorted(p.items, key=lambda i: i.key == "db"):  # the database last
                if item.state == SAME:
                    out.append(f"  same    {item.key}: {item.dest} already holds it")
                    continue
                if item.kind == "db":
                    assert lock is not None
                    summary = _copy_db(lock, item)
                else:
                    summary = _copy_tree(item)
                created.append(item.dest)
                out.append(f"  copied  {item.key}: {item.src} -> {item.dest} ({summary})")
            secure_data(settings, also=tuple(i.dest for i in p.items if i.key != "cache"))
        except (OSError, sqlite3.Error, MigrateError) as exc:
            for path in reversed(created):
                _remove(path)  # personal data: leave no partial copy behind
            raise MigrateError(
                f"{exc}. The copies made so far were removed and {p.root} is unchanged; it is "
                "safe to run this again."
            ) from exc
        out.append("  permissions: data directory 0700, database, backups, profile, resume 0600")
        archives: list[Path] = []
        for src, archive in p.renames:
            try:
                os.rename(src, archive)
            except OSError as exc:
                raise MigrateError(
                    f"copied and verified, but renaming {src} to {archive} failed: {exc}. "
                    "Rename it by hand so nothing keeps using the old copy."
                ) from exc
            archives.append(archive)
            folder = src.name
            if archive.is_dir():
                (archive / NOTE_NAME).write_text(_note(p, folder, archive, today), "utf-8")
            out.append(f"  renamed {src} -> {archive}")
    finally:
        if lock is not None:
            lock.close()
    out += _archive_lines(archives)
    out.append(f"  {START_HINT}")
    out.append("done: run `jobhunter paths` to see where everything is now.")
    return out
