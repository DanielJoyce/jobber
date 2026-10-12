"""Builders for achievements tests: a migrated database and a ``World`` that writes rows in
the real shapes the console and ``mail match`` write (UTC ISO timestamps, ``recorded_at``).

Times are given as local wall-clock strings in America/Los_Angeles (``"2026-09-22 10:00"``)
and stored as UTC, as the console stores them.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from jobhunter.achievements import engine

TZ = ZoneInfo("America/Los_Angeles")
# Sunday evening local: the week of Mon 2026-10-05 is still in progress.
NOW = datetime(2026, 10, 11, 20, 0, tzinfo=TZ)


def local(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=TZ)


def iso(text: str | datetime) -> str:
    dt = local(text) if isinstance(text, str) else text
    return dt.astimezone(UTC).isoformat()


class World:
    def __init__(self, conn: sqlite3.Connection, path: Path) -> None:
        self.conn = conn
        self.path = path
        self._group = 0

    # --- applications and events

    def group(self) -> int:
        self._group += 1
        cur = self.conn.execute(
            "INSERT INTO job_group (method, created_at) VALUES ('manual', ?)", (iso(NOW),)
        )
        return int(cur.lastrowid or 0)

    def app(
        self,
        sent: str | None = None,
        *,
        source: str = "email",
        status: str = "applied",
        resume_version: str | None = None,
        cover: str | None = None,
    ) -> int:
        """An application; with ``sent``, its sending event (``status`` at that time)."""
        gid = self.group()
        cur = self.conn.execute(
            "INSERT INTO application (job_group_id, status, resume_version, cover_letter_path, "
            "created_at, updated_at) VALUES (?, 'interested', ?, ?, ?, ?)",
            (gid, resume_version, cover, iso(NOW), iso(NOW)),
        )
        app_id = int(cur.lastrowid or 0)
        if sent is not None:
            self.event(
                app_id,
                status,
                sent,
                source=source,
                note="from email: Thanks for applying" if source == "email" else None,
            )
        return app_id

    def event(
        self,
        app_id: int,
        status: str,
        at: str,
        *,
        note: str | None = None,
        source: str = "manual",
        recorded: str | None = None,
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO application_event (application_id, at, status, note, source, "
            "recorded_at) VALUES (?, ?, ?, ?, ?, ?)",
            (app_id, iso(at), status, note, source, iso(recorded or at)),
        )
        return int(cur.lastrowid or 0)

    def group_of(self, app_id: int) -> int:
        return self.conn.execute(
            "SELECT job_group_id FROM application WHERE id = ?", (app_id,)
        ).fetchone()[0]

    # --- other tables

    def label(self, at: str, label: str = "interesting") -> int:
        gid = self.group()
        self.conn.execute(
            "INSERT INTO label (job_group_id, label, labeled_at) VALUES (?, ?, ?)",
            (gid, label, iso(at)),
        )
        return gid

    def followup(self, app_id: int, kind: str, at: str, due: str | None, backfill: int = 0) -> int:
        cur = self.conn.execute(
            "INSERT INTO followup_log (application_id, at, kind, due_at, backfill) "
            "VALUES (?, ?, ?, ?, ?)",
            (app_id, iso(at), kind, due, backfill),
        )
        return int(cur.lastrowid or 0)

    def contact(self, app_id: int, at: str | None) -> int:
        cur = self.conn.execute(
            "INSERT INTO contact (application_id, name, created_at) VALUES (?, 'A. Recruiter', ?)",
            (app_id, iso(at) if at else None),
        )
        return int(cur.lastrowid or 0)

    def rejection(
        self,
        received: str,
        *,
        source: str = "email",
        state: str = "confirmed",
        confirmed: str | None = None,
        created: str | None = None,
        app_id: int | None = None,
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO rejection (received_at, employer, source, created_at, state, "
            "confirmed_at, application_id) VALUES (?, 'Acme', ?, ?, ?, ?, ?)",
            (
                iso(received),
                source,
                iso(created or received),
                state,
                iso(confirmed) if confirmed else None,
                app_id,
            ),
        )
        return int(cur.lastrowid or 0)

    def prefs_save(self, at: str, source: str = "ui") -> None:
        self.conn.execute(
            "INSERT INTO profile_change (at, field_path, filter_version, scoring_version, source)"
            " VALUES (?, 'soft.x', 'f', 's', ?)",
            (iso(at), source),
        )

    def capture(self, at: str, outcome: str = "added") -> None:
        n = self.conn.execute("SELECT COUNT(*) FROM capture_log").fetchone()[0]
        self.conn.execute(
            "INSERT INTO capture_log (action_id, route, captured_at, host, outcome) "
            "VALUES (?, '/ext/capture', ?, 'example.com', ?)",
            (f"a{n}", iso(at), outcome),
        )

    def packet(
        self, app_id: int, origin: str = "generated", *, cover: bool = False, status: str = "ready"
    ) -> tuple[int, int]:
        """A packet with resume v1 of ``origin``; returns (packet id, resume version)."""
        cur = self.conn.execute(
            "INSERT INTO application_packet (application_id, status, created_at, updated_at, "
            "ready_at) VALUES (?, ?, ?, ?, ?)",
            (app_id, status, iso(NOW), iso(NOW), iso(NOW) if status == "ready" else None),
        )
        pid = int(cur.lastrowid or 0)
        doc = self._doc(pid, "resume", origin)
        cover_id = self._doc(pid, "cover_letter", "generated") if cover else None
        self.conn.execute(
            "UPDATE application_packet SET resume_doc_id = ?, cover_doc_id = ? WHERE id = ?",
            (doc, cover_id, pid),
        )
        return pid, 1

    def _doc(self, pid: int, kind: str, origin: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO packet_document (packet_id, kind, version, origin, doc_json, body_md, "
            "check_report, created_at) VALUES (?, ?, 1, ?, '{}', '', '{}', ?)",
            (pid, kind, origin, iso(NOW)),
        )
        return int(cur.lastrowid or 0)

    def attach(self, app_id: int, kind: str, path: str) -> None:
        self.conn.execute(
            "INSERT INTO attachment (application_id, kind, path, added_at) VALUES (?, ?, ?, ?)",
            (app_id, kind, path, iso(NOW)),
        )

    # --- evaluation

    def series(self, now: datetime = NOW) -> dict:
        return engine.compute(self.conn, now, TZ).series

    def progress(self, achievement_id: str, now: datetime = NOW) -> int:
        return self.series(now)[achievement_id].progress

    def reached(self, now: datetime = NOW) -> set[tuple[str, int]]:
        return set(engine.compute(self.conn, now, TZ).reached)

    def evaluate(self, now: datetime = NOW) -> list[engine.Unlock]:
        return engine.evaluate(self.conn, now, TZ)

    def unlocks(self) -> dict[tuple[str, int], engine.Unlock]:
        return {(u.achievement_id, u.tier): u for u in engine.stored(self.conn)}
