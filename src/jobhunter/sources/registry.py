"""Load and validate the source registry (specs/003-sources-and-adapters.md)."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping
from importlib import resources
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from ruamel.yaml import YAML

from jobhunter.core.db import transaction
from jobhunter.core.models import Policy, SourceRow

REGISTRY_PACKAGE = "jobhunter.sources"
REGISTRY_FILE = "registry.yaml"


def _read_registry(path: Path | str | None) -> str:
    if path is None:
        return resources.files(REGISTRY_PACKAGE).joinpath(REGISTRY_FILE).read_text(encoding="utf-8")
    return Path(path).read_text(encoding="utf-8")


def load_registry(path: Path | str | None = None) -> list[SourceRow]:
    """Parse and validate registry rows. Defaults to the packaged registry.yaml.

    Raises ValueError naming the row key and field for any invalid row, and for duplicate keys.
    """
    data = YAML(typ="safe").load(_read_registry(path))
    if not isinstance(data, list):
        raise ValueError("registry: top level must be a list of rows")

    rows: list[SourceRow] = []
    seen: set[str] = set()
    for index, raw in enumerate(data):
        if not isinstance(raw, Mapping):
            raise ValueError(f"registry row #{index}: expected a mapping, got {type(raw).__name__}")
        key = raw.get("key")
        if isinstance(key, str):
            if key in seen:
                raise ValueError(f"registry: duplicate key {key!r}")
            seen.add(key)
        label = f"registry row {key!r}" if isinstance(key, str) else f"registry row #{index}"
        try:
            rows.append(SourceRow.model_validate(dict(raw)))
        except ValidationError as exc:
            problems = "; ".join(
                f"field {'.'.join(str(p) for p in err['loc']) or '<row>'}: {err['msg']}"
                for err in exc.errors()
            )
            raise ValueError(f"{label}: {problems}") from exc
    return rows


def enabled_sources(
    rows: Iterable[SourceRow], states: Iterable[str] | None = None
) -> list[SourceRow]:
    """Rows with policy enabled. With ``states``, only rows whose state code is listed.

    National and federal rows have state None and are excluded by a state filter.
    """
    wanted = None if states is None else {s.upper() for s in states}
    return [
        r
        for r in rows
        if r.policy is Policy.enabled and (wanted is None or (r.state or "").upper() in wanted)
    ]


# Status is written only on insert: runs own it afterwards (specs/005 source.status).
_INITIAL_STATUS = {
    Policy.enabled: "ok",
    Policy.blocked: "blocked",
    Policy.manual: "manual",
    Policy.disabled: "disabled",
}

_UPSERT = """
INSERT INTO source (key, state, class, name, family, tier, entry, policy,
                    robots_status, robots_checked, verified_at, status)
VALUES (:key, :state, :class, :name, :family, :tier, :entry, :policy,
        :robots_status, :robots_checked, :verified_at, :status)
ON CONFLICT(key) DO UPDATE SET
    state = excluded.state,
    class = excluded.class,
    name = excluded.name,
    family = excluded.family,
    tier = excluded.tier,
    entry = excluded.entry,
    policy = excluded.policy,
    robots_status = excluded.robots_status,
    robots_checked = excluded.robots_checked,
    verified_at = excluded.verified_at
"""


def _params(row: SourceRow) -> dict[str, Any]:
    checked = row.robots.checked
    return {
        "key": row.key,
        "state": row.state,
        "class": row.class_.value,
        "name": row.name,
        "family": row.family,
        "tier": row.tier.value,
        "entry": row.entry,
        "policy": row.policy.value,
        "robots_status": row.robots.status,
        "robots_checked": checked.isoformat() if checked else None,
        "verified_at": row.verified.isoformat() if row.verified else None,
        "status": _INITIAL_STATUS[row.policy],
    }


def sync_sources_table(conn: sqlite3.Connection, rows: Iterable[SourceRow]) -> int:
    """Upsert registry rows into ``source``. Returns the number of rows written.

    Config fields (policy, robots, verification) are refreshed from the registry; ``status``
    and ``status_note`` are never touched after insert, so run results survive a resync.
    """
    params = [_params(r) for r in rows]
    with transaction(conn):
        conn.executemany(_UPSERT, params)
    return len(params)
