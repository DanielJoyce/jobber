"""``evaluate``: compute every rule, diff against the stored unlocks, write only on change
(specs/021 "How unlocks are computed", "Backfill", "Revocation").

1. Off: return ``[]``, no write.
2. Read phase, no transaction: compute from scratch and diff against ``achievement_unlock``.
3. Nothing to change: return without writing (the common case; takes no lock).
4. Write phase: ``busy_timeout`` 250 ms, ``BEGIN IMMEDIATE``, then re-read the settings and the
   stored rows and recompute inside the transaction (closes the race with another tab, an
   undo, or a first-time backfill in a second tab). Insert with ``ON CONFLICT DO NOTHING``,
   revoke, refresh evidence; on the first write (``backfilled_at`` empty) every new row gets
   ``seen_at = detected_at`` and ``backfilled_at`` is set. Commit; restore the timeout.
5. A ``sqlite3.Error`` propagates: the **caller** renders from the stored rows and the next
   call retries.

Revocation: within 24 hours of ``detected_at``, a tier the fresh computation no longer
reaches is deleted (and every tier above it). A tier that still holds keeps its row and its
``seen_at``; only its evidence is refreshed. After 24 hours nothing is revoked.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, tzinfo

from jobhunter.achievements import catalog, facts, rules

WRITE_BUSY_MS = 250
REVOKE_WINDOW = timedelta(hours=24)

ENABLED = "enabled"
NOTIFY = "notify"
HIDDEN = "hidden_categories"
BACKFILLED_AT = "backfilled_at"


def local_tz() -> tzinfo:
    """The console process's local zone (the container runs with ``Timezone=local``)."""
    tz = datetime.now().astimezone().tzinfo
    assert tz is not None
    return tz


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


# --- settings ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Settings:
    enabled: bool = True
    notify: bool = True
    hidden: frozenset[str] = frozenset()
    backfilled_at: str | None = None


def read_settings(conn: sqlite3.Connection) -> Settings:
    vals = dict(conn.execute("SELECT key, value FROM achievement_setting").fetchall())
    try:
        hidden = json.loads(vals.get(HIDDEN) or "[]")
    except ValueError:
        hidden = []
    known = set(catalog.CATEGORY_KEYS)
    return Settings(
        enabled=vals.get(ENABLED, "1") != "0",
        notify=vals.get(NOTIFY, "1") != "0",
        hidden=frozenset(h for h in hidden if isinstance(h, str) and h in known),
        backfilled_at=vals.get(BACKFILLED_AT) or None,
    )


def put_setting(conn: sqlite3.Connection, key: str, value: str, now: datetime) -> None:
    conn.execute(
        "INSERT INTO achievement_setting (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, value, _iso(now)),
    )


def hidden_json(categories: Iterable[str]) -> str:
    known = catalog.CATEGORY_KEYS
    return json.dumps([c for c in known if c in set(categories)])


# --- stored unlocks ----------------------------------------------------------------------


@dataclass(frozen=True)
class Unlock:
    id: int
    achievement_id: str
    tier: int
    unlocked_at: str
    detected_at: str
    evidence: str
    catalog_version: int
    seen_at: str | None

    @property
    def category(self) -> str:
        return catalog.category_of(self.achievement_id)


def stored(conn: sqlite3.Connection) -> list[Unlock]:
    return [
        Unlock(*r)
        for r in conn.execute(
            "SELECT id, achievement_id, tier, unlocked_at, detected_at, evidence, "
            "catalog_version, seen_at FROM achievement_unlock ORDER BY id"
        ).fetchall()
    ]


# --- computation -------------------------------------------------------------------------


@dataclass(frozen=True)
class Reached:
    unlocked_at: str
    evidence: str


@dataclass
class Computation:
    series: dict[str, rules.Series]
    rhythm: rules.RhythmStatus
    reached: dict[tuple[str, int], Reached]


def compute(conn: sqlite3.Connection, now: datetime, tz: tzinfo) -> Computation:
    """Every rule from scratch (the read phase; no writes)."""
    snapshot = facts.load(conn, now, tz)
    r = rules.Rules(snapshot)
    series = {a.id: rules.rule_for(r, a.id) for a in catalog.CATALOG}
    reached: dict[tuple[str, int], Reached] = {}
    for a in catalog.CATALOG:
        s = series[a.id]
        for tier, threshold in enumerate(a.thresholds, start=1):
            if s.progress < threshold:
                break
            evidence = json.dumps(s.evidence(threshold), sort_keys=True, default=str)
            reached[(a.id, tier)] = Reached(_iso(s.items[threshold - 1].at), evidence)
    return Computation(series, r.rhythm_status(), reached)


@dataclass
class Diff:
    insert: dict[tuple[str, int], Reached]
    revoke: list[int]  # unlock row ids
    refresh: dict[int, str]  # unlock row id -> new evidence

    @property
    def empty(self) -> bool:
        return not (self.insert or self.revoke or self.refresh)


def diff(reached: dict[tuple[str, int], Reached], rows: list[Unlock], now: datetime) -> Diff:
    have = {(u.achievement_id, u.tier): u for u in rows}
    insert = {k: v for k, v in reached.items() if k not in have}
    revoke: set[int] = set()
    refresh: dict[int, str] = {}
    lowest_revoked: dict[str, int] = {}
    for u in rows:
        if u.achievement_id not in catalog.BY_ID:
            continue  # a later (or earlier) catalog's id: never touched here
        if not _recent(u, now):
            continue
        fresh = reached.get((u.achievement_id, u.tier))
        if fresh is None:
            revoke.add(u.id)
            low = lowest_revoked.get(u.achievement_id, u.tier)
            lowest_revoked[u.achievement_id] = min(low, u.tier)
        elif fresh.evidence != u.evidence:
            refresh[u.id] = fresh.evidence
    for u in rows:  # every tier above a revoked one goes with it
        low = lowest_revoked.get(u.achievement_id)
        if low is not None and u.tier > low:
            revoke.add(u.id)
            refresh.pop(u.id, None)
    return Diff(insert, sorted(revoke), refresh)


def _recent(u: Unlock, now: datetime) -> bool:
    try:
        detected = facts.parse_utc(u.detected_at)
    except ValueError:
        return False
    return now - detected < REVOKE_WINDOW


# --- evaluate ----------------------------------------------------------------------------


def evaluate(conn: sqlite3.Connection, now: datetime, tz: tzinfo | None = None) -> list[Unlock]:
    """Bring ``achievement_unlock`` up to date; returns the newly written rows."""
    tz = tz or local_tz()
    settings = read_settings(conn)
    if not settings.enabled:
        return []
    comp = compute(conn, now, tz)
    if diff(comp.reached, stored(conn), now).empty and settings.backfilled_at:
        return []
    return _write(conn, now, tz)


def _write(conn: sqlite3.Connection, now: datetime, tz: tzinfo) -> list[Unlock]:
    if conn.in_transaction:
        raise RuntimeError("achievements.evaluate needs a connection outside a transaction")
    old_timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    conn.execute(f"PRAGMA busy_timeout = {WRITE_BUSY_MS}")
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            new = _apply(conn, now, tz)
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
        return new
    finally:
        conn.execute(f"PRAGMA busy_timeout = {int(old_timeout)}")


def _apply(conn: sqlite3.Connection, now: datetime, tz: tzinfo) -> list[Unlock]:
    """Inside the write transaction: re-read, recompute, apply."""
    settings = read_settings(conn)
    if not settings.enabled:
        return []
    comp = compute(conn, now, tz)
    d = diff(comp.reached, stored(conn), now)
    detected = _iso(now)
    backfill = not settings.backfilled_at
    for uid in d.revoke:
        conn.execute("DELETE FROM achievement_unlock WHERE id = ?", (uid,))
    for uid, evidence in d.refresh.items():
        conn.execute("UPDATE achievement_unlock SET evidence = ? WHERE id = ?", (evidence, uid))
    new: list[Unlock] = []
    for (aid, tier), r in sorted(d.insert.items(), key=lambda kv: (kv[1].unlocked_at, kv[0])):
        silent = backfill or not settings.notify or catalog.category_of(aid) in settings.hidden
        seen = detected if silent else None
        row = conn.execute(
            "INSERT INTO achievement_unlock (achievement_id, tier, unlocked_at, detected_at, "
            "evidence, catalog_version, seen_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (achievement_id, tier) DO NOTHING RETURNING id",
            (aid, tier, r.unlocked_at, detected, r.evidence, catalog.CATALOG_VERSION, seen),
        ).fetchone()
        if row is not None:
            new.append(
                Unlock(
                    row[0],
                    aid,
                    tier,
                    r.unlocked_at,
                    detected,
                    r.evidence,
                    catalog.CATALOG_VERSION,
                    seen,
                )
            )
    if backfill:
        put_setting(conn, BACKFILLED_AT, detected, now)
    return new


def mark_seen(
    conn: sqlite3.Connection, ids: Iterable[int], now: datetime, busy_ms: int | None = None
) -> int:
    """Set ``seen_at`` on the given unseen unlocks; returns how many changed. With
    ``busy_ms`` the write waits at most that long for the lock (then raises)."""
    ids = sorted({int(i) for i in ids})
    if not ids:
        return 0
    old_timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    if busy_ms is not None:
        conn.execute(f"PRAGMA busy_timeout = {int(busy_ms)}")
    try:
        cur = conn.execute(
            f"UPDATE achievement_unlock SET seen_at = ? WHERE seen_at IS NULL "
            f"AND id IN ({','.join('?' * len(ids))})",
            (_iso(now), *ids),
        )
        return cur.rowcount
    finally:
        conn.execute(f"PRAGMA busy_timeout = {int(old_timeout)}")


def mark_category_seen(conn: sqlite3.Connection, categories: Iterable[str], now: datetime) -> None:
    """A hidden category's unlocks get ``seen_at`` at once (no notice for what you hid)."""
    cats = set(categories)
    ids = [u.id for u in stored(conn) if u.seen_at is None and u.category in cats]
    mark_seen(conn, ids, now)


# --- the in-process change gate ----------------------------------------------------------

_FINGERPRINT_SQL = """
SELECT
  (SELECT MAX(id) FROM application_event), (SELECT COUNT(*) FROM application_event),
  (SELECT MAX(id) FROM contact), (SELECT COUNT(*) FROM contact),
  (SELECT MAX(id) FROM followup_log), (SELECT COUNT(*) FROM followup_log),
  (SELECT MAX(id) FROM attachment), (SELECT COUNT(*) FROM attachment),
  (SELECT MAX(id) FROM capture_log), (SELECT COUNT(*) FROM capture_log),
  (SELECT MAX(id) FROM profile_change), (SELECT COUNT(*) FROM profile_change),
  (SELECT COUNT(*) FROM label), (SELECT MAX(labeled_at) FROM label),
  (SELECT MAX(id) FROM rejection), (SELECT COUNT(*) FROM rejection),
  (SELECT COUNT(*) FROM rejection WHERE state = 'confirmed'),
  (SELECT MAX(confirmed_at) FROM rejection),
  (SELECT MAX(updated_at) FROM application_packet),
  (SELECT MAX(updated_at) FROM alert_signup),
  EXISTS (SELECT 1 FROM mail_message),
  (SELECT MAX(updated_at) FROM application),
  (SELECT value FROM achievement_setting WHERE key = 'mail_setup_at')
"""


def fingerprint(conn: sqlite3.Connection, now: datetime, tz: tzinfo) -> tuple[object, ...]:
    """One cheap SELECT that changes whenever a rule's input might have. Kept in process
    memory only, never written to the database. The local date and week start are part of it
    (a week ending is news even with no new rows)."""
    row = tuple(conn.execute(_FINGERPRINT_SQL).fetchone())
    today: date = now.astimezone(tz).date()
    return (*row, today.isoformat(), facts.week_start(today).isoformat())


class Gate:
    """The console's in-process state: the last fingerprint evaluated and the cached
    ``enabled`` flag the nav reads. Lives on ``app.state.achievements``.

    ``tz`` overrides the local zone (tests)."""

    def __init__(self, enabled: bool = True, tz: tzinfo | None = None) -> None:
        self.enabled = enabled
        self.tz = tz
        self._fp: tuple[object, ...] | None = None
        self._lock = threading.Lock()

    def zone(self) -> tzinfo:
        return self.tz or local_tz()

    def invalidate(self) -> None:
        with self._lock:
            self._fp = None

    def refresh(
        self, conn: sqlite3.Connection, now: datetime, *, force: bool = False
    ) -> list[Unlock]:
        """``evaluate`` when the fingerprint changed (or ``force``). A ``sqlite3.Error``
        propagates and leaves the fingerprint unchanged, so the next call retries."""
        tz = self.zone()
        fp = fingerprint(conn, now, tz)
        with self._lock:
            if not force and fp == self._fp:
                return []
        new = evaluate(conn, now, tz)
        with self._lock:
            self._fp = fp
        return new

    def reload_enabled(self, conn: sqlite3.Connection) -> bool:
        self.enabled = read_settings(conn).enabled
        return self.enabled
