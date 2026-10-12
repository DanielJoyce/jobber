"""Writing ``<data dir>/models.toml``, its history, and what a screening change does to queued
re-scores (specs/020-model-selection.md#storage, M2).

``config.load_layers`` reads the file; this module is its only writer, for ``jobhunter models``
and the ``/models`` page (M4). Every write:

- is validated per field first (:func:`check_save`): the same load-time rules the reader
  applies, which nothing overrides, then the catalog's offer rules (the screening reservation,
  the OpenRouter cache, the local server list), which ``force`` ("save anyway") overrides;
- quotes the SHA-256 of the file it was based on, and is refused with :class:`StaleDigest`
  when the file changed meanwhile ("changed elsewhere; reload"), writing nothing;
- runs under a ``flock`` on ``models.toml.lock``, writes a temporary file, ``fsync``s it and
  renames it over the file, mode 0600, and returns the new digest;
- writes only the fields it was given, leaving every other key in the file as it was;
- records one ``model_change`` row per field that changed.

Nothing here spends, makes a network call or runs a subprocess. :func:`queued_impact` plans
queued re-scores with ``rescore.make_plan``, whose only write is the free local prefilter
catch-up.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import math
import os
import sqlite3
import tempfile
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

from jobhunter.config import (
    MODEL_KEYS,
    MODELS_FILE_NAME,
    Scoring,
    check_model_key,
    read_models_file,
    shown_value,
)
from jobhunter.pipeline.listing import to_iso

# The names `jobhunter models set|reset` and /models use for each field: the catalog's uses for
# the model fields, the field name for the rest.
NAMES: dict[str, tuple[str, str]] = {
    "screen": ("scoring", "screen_scorer"),
    "deep": ("scoring", "deep_scorer"),
    "runner": ("apply", "runner"),
    "draft_cli": ("apply", "cli_model"),
    "draft_api": ("apply", "api_model"),
    "effort": ("apply", "effort"),
    "entailment_check": ("apply", "entailment_check"),
    "check_cli": ("apply", "cli_entailment_model"),
    "check_api": ("apply", "api_entailment_model"),
}
# Names that reset several fields at once.
GROUPS: dict[str, tuple[str, ...]] = {
    "drafting": ("runner", "draft_cli", "draft_api", "effort"),
    "check": ("entailment_check", "check_cli", "check_api"),
    "all": tuple(NAMES),
}
# The use a field belongs to, for the history table and the page's rows.
USE_OF = {
    "screen": "screen",
    "deep": "deep",
    "runner": "drafting",
    "draft_cli": "drafting",
    "draft_api": "drafting",
    "effort": "drafting",
    "entailment_check": "check",
    "check_cli": "check",
    "check_api": "check",
}
SOURCES = ("ui", "cli", "reset", "revert")
HEADER = (
    "# Model choices, written by /models in the jobhunter console and by `jobhunter models`.\n"
    "# Each key here overrides the same key in config.toml; remove it (Reset) and config.toml's\n"
    "# value or the default applies again. Edit with `jobhunter models set` rather than by hand.\n"
)
HISTORY_LIMIT = 20
INVALID_TEXT = "<invalid value>"

assert set(NAMES.values()) == {(s, k) for s, keys in MODEL_KEYS.items() for k in keys}


class ModelsFileError(Exception):
    """A write to models.toml was refused; nothing was written."""


class StaleDigest(ModelsFileError):
    """The file changed since the caller read it (another writer). The page answers 409."""

    def __init__(self) -> None:
        super().__init__(f"{MODELS_FILE_NAME} changed elsewhere; reload and try again")


class BrokenFile(ModelsFileError):
    """The file is not valid TOML, so a field cannot be changed without losing the rest."""

    def __init__(self) -> None:
        super().__init__(
            f"{MODELS_FILE_NAME} is not valid TOML; reset it first (`jobhunter models "
            "reset-file`, or Reset file on /models), which keeps the old file beside it"
        )


class Refused(ModelsFileError):
    """A value failed validation. ``forceable``: only a "save anyway" rule refused it."""

    def __init__(self, message: str, *, forceable: bool = False) -> None:
        super().__init__(message)
        self.forceable = forceable


def field_of(name: str) -> tuple[str, str]:
    try:
        return NAMES[name]
    except KeyError:
        known = ", ".join(NAMES)
        raise ModelsFileError(f"unknown use {name!r} (known: {known})") from None


def expand(name: str) -> tuple[str, ...]:
    """The field names ``name`` stands for: itself, or a group's members (for reset)."""
    if name in GROUPS:
        return GROUPS[name]
    field_of(name)
    return (name,)


def parse_value(name: str, text: str) -> Any:
    """A command-line value as the field's type (``entailment_check`` is a bool)."""
    if NAMES[name][1] == "entailment_check":
        lowered = text.strip().lower()
        if lowered in ("true", "on", "yes", "1"):
            return True
        if lowered in ("false", "off", "no", "0"):
            return False
        raise Refused("entailment_check must be true or false")
    return text.strip()


def to_text(value: Any) -> str | None:
    """A stored value as the history table's text (bools as "true"/"false"). A value that
    is not a model id, spec or choice (a hand edit gone wrong) is not copied into history."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    return shown_value(value) or INVALID_TEXT


def from_text(name: str, text: str | None) -> Any:
    if text is None:
        return None
    return parse_value(name, text)


# ─── Validation ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SaveCheck:
    """``error``: refused outright. ``needs_force``: reasons only "save anyway" overrides."""

    error: str | None = None
    needs_force: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.error is None and not self.needs_force


def check_save(
    name: str,
    value: Any,
    scoring: Scoring | None = None,
    *,
    local_ids: list[str] | None = None,
) -> SaveCheck:
    """Validate one field for a save. Local and free: no network, no subprocess.

    First the rules the reader applies on every load (``config.check_model_key``): a value
    failing them would be ignored (drafting) or block its use (screening, deep), so it is never
    written. Then the catalog's offer rules (``catalog.judge``): a screening option above the
    flat reservation or with no price, an OpenRouter slug that is not in the cache or lacks
    structured output, a local model not on the server's list. Those save only with ``force``.
    The credential check ("save anyway" when the key is missing) is M3's ``model_access``.
    """
    section, key = field_of(name)
    if error := check_model_key(section, key, value):
        return SaveCheck(error=f"{section}.{key}: {error}")
    if name not in USE_OF or MODEL_KEYS[section][key] is None:
        return SaveCheck()
    from jobhunter.scoring import catalog

    verdict = catalog.judge(name, str(value), scoring, local_ids=local_ids)
    reasons: list[str] = []
    if not verdict.offered and verdict.reason:
        reasons.append(f"not offered for {catalog.USE_LABELS[name].lower()}: {verdict.reason}")
    if verdict.note:
        reasons.append(verdict.note)
    return SaveCheck(needs_force=tuple(reasons))


# ─── Reading and writing the file ───────────────────────────────────────────


def current_digest(path: Path) -> str:
    return read_models_file(path)[1]


@contextlib.contextmanager
def file_lock(path: Path) -> Iterator[None]:
    """Exclusive ``flock`` on ``<path>.lock`` for one read-modify-write of ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_name(path.name + ".lock")
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _toml_key(key: str) -> str:
    if key and all(c.isalnum() or c in "_-" for c in key):
        return key
    return json.dumps(key, ensure_ascii=False)


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value):
            return "nan"
        if math.isinf(value):
            return "inf" if value > 0 else "-inf"
        return repr(value)
    if isinstance(value, str):
        # JSON's string escapes are TOML basic-string escapes; ensure_ascii=False avoids
        # \\ud83d-style surrogate pairs, which TOML does not accept.
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, datetime | date | time):
        return value.isoformat()
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    if isinstance(value, dict):
        inner = ", ".join(f"{_toml_key(k)} = {_toml_value(v)}" for k, v in value.items())
        return "{" + inner + "}"
    raise TypeError(f"cannot write a {type(value).__name__} to TOML")


def dumps(data: Mapping[str, Any]) -> str:
    """A small TOML writer for this file's shape: top-level scalars, then one table per
    section. Keys this version does not know (a newer version's) are kept as they were read."""
    lines = [HEADER.rstrip("\n")]
    scalars = {k: v for k, v in data.items() if not isinstance(v, dict)}
    tables = {k: v for k, v in data.items() if isinstance(v, dict)}
    if scalars:
        lines.append("")
        lines += [f"{_toml_key(k)} = {_toml_value(v)}" for k, v in scalars.items()]
    for name, table in tables.items():
        if not table:
            continue
        lines += ["", f"[{_toml_key(name)}]"]
        lines += [f"{_toml_key(k)} = {_toml_value(v)}" for k, v in table.items()]
    return "\n".join(lines) + "\n"


def _atomic_write(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


@dataclass
class WriteResult:
    digest: str
    changed: dict[str, tuple[Any, Any]] = field(default_factory=dict)  # name -> (old, new)


def write_changes(
    path: Path,
    changes: Mapping[str, Any],
    base_digest: str,
    *,
    conn: sqlite3.Connection | None = None,
    source: str,
    now: datetime,
) -> WriteResult:
    """Set (a value) or remove (None) each named field, under the lock, if the file is still
    the one ``base_digest`` names. Validation is the caller's (:func:`check_save`): this
    writes what it is given. A field whose value does not change is not written or recorded.
    """
    if source not in SOURCES:
        raise ValueError(f"source must be one of {SOURCES}")
    fields = {name: field_of(name) for name in changes}
    with file_lock(path):
        _exists, digest, data, _problems = read_models_file(path)
        if digest != base_digest:
            raise StaleDigest()
        if data is None or any(not isinstance(data.get(s, {}), dict) for s, _ in fields.values()):
            raise BrokenFile()
        changed: dict[str, tuple[Any, Any]] = {}
        for name, value in changes.items():
            section, key = fields[name]
            table = data.setdefault(section, {})
            old = table.get(key)
            if value is None:
                table.pop(key, None)
            else:
                table[key] = value
            if old != value or type(old) is not type(value):
                changed[name] = (old, value)
        if not changed:
            return WriteResult(digest)
        text = dumps(data)
        _atomic_write(path, text)
        new_digest = read_models_file(path)[1]
    if conn is not None:
        record_changes(conn, changed, source=source, now=now)
    return WriteResult(new_digest, changed)


def reset_file(path: Path, *, now: datetime) -> tuple[str, Path | None]:
    """Replace models.toml with an empty one, keeping the old file as
    ``models.toml.broken-<time>``. Returns (new digest, where the old file went)."""
    with file_lock(path):
        kept: Path | None = None
        if path.exists():
            stamp = now.strftime("%Y%m%dT%H%M%S")
            kept = path.with_name(f"{path.name}.broken-{stamp}")
            os.replace(path, kept)
        _atomic_write(path, dumps({}))
        return read_models_file(path)[1], kept


# ─── History ────────────────────────────────────────────────────────────────


def record_changes(
    conn: sqlite3.Connection,
    changed: Mapping[str, tuple[Any, Any]],
    *,
    source: str,
    now: datetime,
) -> None:
    at = to_iso(now)
    with conn:
        conn.executemany(
            "INSERT INTO model_change (at, use, field, old_value, new_value, source) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                (at, USE_OF[name], ".".join(NAMES[name]), to_text(old), to_text(new), source)
                for name, (old, new) in changed.items()
            ],
        )


def history(conn: sqlite3.Connection, limit: int = HISTORY_LIMIT) -> list[sqlite3.Row]:
    """The latest changes, newest first."""
    cur = conn.execute("SELECT * FROM model_change ORDER BY id DESC LIMIT ?", (limit,))
    cur.row_factory = sqlite3.Row
    return cur.fetchall()


_NAME_OF_FIELD = {".".join(v): k for k, v in NAMES.items()}


def revert_change(change: sqlite3.Row | Mapping[str, Any]) -> tuple[str, Any]:
    """(name, value) that reverts ``change``: its old value, or None (remove the key) when
    models.toml did not set the field before. The caller writes it as a new change with
    ``source="revert"``, after the usual checks."""
    name = _NAME_OF_FIELD.get(change["field"])
    if name is None:
        raise ModelsFileError(f"history entry names an unknown field {change['field']!r}")
    return name, from_text(name, change["old_value"])


# ─── Queued re-scores (specs/020#queued-re-scores) ──────────────────────────


@dataclass
class QueuedImpact:
    """What a screening change does to re-scores queued with no plan (a paid /prefs save).

    The drain runs pending requests in id order and a finished run closes the later ones it
    covers, so this plans the widest scope among them once, rather than summing every plan.
    """

    request_ids: list[int]
    scope: str
    new_spec: str
    old_spec: str
    new_usd: float | None
    old_usd: float | None
    superseded: list[int]

    @property
    def line(self) -> str:
        n = len(self.request_ids)
        what = f"{n} queued re-score{'s' if n != 1 else ''} would run tonight with {self.new_spec}"
        new = "unknown cost" if self.new_usd is None else f"about ${self.new_usd:.2f}"
        old = "" if self.old_usd is None else f", instead of about ${self.old_usd:.2f}"
        old += f" with {self.old_spec}" if old else ""
        tail = ""
        if self.superseded:
            ids = ", ".join(f"#{i}" for i in self.superseded)
            tail = f"; {ids} would be superseded by the first run"
        return f"{what}: {new}{old}{tail}"


def unplanned_requests(conn: sqlite3.Connection) -> list[tuple[int, str]]:
    """(id, scope) of pending re-scores with no plan and no scorer: they follow the current
    screening model when the drain plans them (``rescore.run_request``)."""
    return [
        (int(r[0]), str(r[1]))
        for r in conn.execute(
            "SELECT id, scope FROM rescore_request WHERE status = 'pending' "
            "AND group_ids IS NULL AND scorer IS NULL ORDER BY id"
        )
    ]


def _plan_usd(conn, profile, scoring: Scoring, scope: str, spec: str, now) -> float | None:
    from jobhunter.scoring import rescore
    from jobhunter.scoring.scorers import ScorerError

    try:
        plan = rescore.make_plan(conn, profile, scoring, scope, spec, now)
    except (rescore.RescoreError, ScorerError, ValueError):
        return None
    return plan.estimated_usd


def queued_impact(
    conn: sqlite3.Connection,
    profile: Any,
    scoring: Scoring,
    old_spec: str,
    new_spec: str,
    now: datetime,
) -> QueuedImpact | None:
    """The queued re-score line for changing screening from ``old_spec`` to ``new_spec``, or
    None when no unplanned request is queued. No network: ``make_plan`` prices from history
    and the cached price list."""
    rows = unplanned_requests(conn)
    if not rows:
        return None
    scope = "all" if any(s == "all" for _, s in rows) else rows[0][1]
    ids = [i for i, _ in rows]
    return QueuedImpact(
        request_ids=ids,
        scope=scope,
        new_spec=new_spec,
        old_spec=old_spec,
        new_usd=_plan_usd(conn, profile, scoring, scope, new_spec, now),
        old_usd=_plan_usd(conn, profile, scoring, scope, old_spec, now),
        superseded=ids[1:],
    )


def keep_queued(conn: sqlite3.Connection, old_spec: str) -> int:
    """ "Keep queued re-scores on the old model": write ``old_spec`` into every unplanned
    pending request, which ``run_request`` then honours. Returns how many rows changed."""
    with conn:
        cur = conn.execute(
            "UPDATE rescore_request SET scorer = ? WHERE status = 'pending' "
            "AND group_ids IS NULL AND scorer IS NULL",
            (old_spec,),
        )
    return cur.rowcount
