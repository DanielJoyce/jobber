"""The shared definitions every rule reads (specs/021 "Definitions used by the rules").

``load`` runs a handful of small SELECTs over tables jobhunter already keeps and returns a
:class:`Facts`: user events (merge events dropped), sent dates, deliberate early events, the
local calendar, and active days. Everything here is pure SQL and Python over the local
database: no network, no model call, and no table that records scoring or spending.

Times: stored values are UTC ISO timestamps; days and weeks are local to ``tz`` (the console
process's zone), weeks Monday to Sunday. A date-only value (``2026-10-14``) is a local
calendar date and is never shifted through UTC.
"""

from __future__ import annotations

import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta, tzinfo

# Mirrors of console.tracking / apply.packets constants (a test checks they agree). Kept local
# so this package imports nothing from the console or the packet generator.
SENT_STATUSES = ("applied", "acknowledged", "screening", "interview", "offer")
RESPONSE_STATUSES = ("screening", "interview", "offer")
EARLY_STATUSES = ("interested", "preparing")
CLOSING_STATUSES = ("rejected", "no_response", "withdrawn")
SHORTLIST_NOTE = "shortlisted from inbox"
APPLY_CLICK_NOTE = "opened apply link"
PREPARE_NOTE = "packet started"
AUTO_NOTES = (SHORTLIST_NOTE, APPLY_CLICK_NOTE, PREPARE_NOTE)
NOT_YET_NOTE = "not applied yet"
MERGE_NOTE_PREFIX = "Merged duplicate application"
# A shortlist press shares its exact ``at`` with this many others or more: a bulk batch.
BULK_SHORTLIST = 3
# Notes the console writes itself (route-generated), so not "a note you wrote".
GENERATED_NOTES = frozenset(
    {
        *AUTO_NOTES,
        NOT_YET_NOTE,
        "marked applied",
        "marked from follow-ups",
        "marked applied from follow-ups",
    }
)
GENERATED_PREFIXES = ("moved from ", "from email:", MERGE_NOTE_PREFIX)
# Capture outcomes that mean you added or linked a job yourself.
CAPTURE_ACTIVE = ("added", "linked", "description_added", "linked_groups")
CAPTURE_FOUND = ("added", "linked", "description_added")

_PACKET_REF = re.compile(r"^packet:(\d+)/resume/v(\d+)$")
_PACKET_FILE = re.compile(r"(?:^|/)packets/(\d+)/resume-v(\d+)\.pdf$")


def parse_utc(value: str) -> datetime:
    """An ISO timestamp as an aware datetime; a naive one is UTC, a bare date UTC midnight."""
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def is_date_only(value: str) -> bool:
    return len(value.strip()) == 10


def week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


@dataclass(frozen=True)
class Event:
    id: int
    app: int
    at: datetime
    status: str
    note: str | None
    source: str
    recorded: datetime  # recorded_at when set, else at


@dataclass
class Facts:
    now: datetime
    tz: tzinfo
    events: list[Event]  # user events, by (at, id)
    sent: dict[int, Event]  # application id -> its earliest sent event
    deliberate: dict[int, Event]  # application id -> its earliest deliberate early event
    deliberate_days: dict[int, set[date]]  # application id -> local days with one
    conn: sqlite3.Connection = field(repr=False)
    _active: dict[date, list[tuple[str, object]]] | None = field(default=None, repr=False)

    # --- calendar

    def day(self, value: datetime | str) -> date:
        """The local calendar day of a timestamp; a date-only string is that date."""
        if isinstance(value, str):
            if is_date_only(value):
                return date.fromisoformat(value.strip())
            value = parse_utc(value)
        return value.astimezone(self.tz).date()

    @property
    def today(self) -> date:
        return self.day(self.now)

    @property
    def current_week(self) -> date:
        return week_start(self.today)

    def week_end(self, start: date) -> datetime:
        """Monday 00:00 local after the week starting ``start``: when it is judged."""
        return datetime.combine(start + timedelta(days=7), time(0), self.tz)

    def finished(self, start: date) -> bool:
        return start < self.current_week

    # --- active days

    @property
    def active(self) -> dict[date, list[tuple[str, object]]]:
        """Local day -> the deliberate actions on it, as (table, id) refs."""
        if self._active is None:
            self._active = _active_days(self)
        return self._active


def load(conn: sqlite3.Connection, now: datetime, tz: tzinfo) -> Facts:
    rows = conn.execute(
        "SELECT id, application_id, at, status, note, source, recorded_at "
        "FROM application_event "
        "WHERE note IS NULL OR substr(note, 1, ?) != ? ORDER BY at, id",
        (len(MERGE_NOTE_PREFIX), MERGE_NOTE_PREFIX),
    ).fetchall()
    events: list[Event] = []
    for r in rows:
        at = parse_utc(r[2])
        events.append(Event(r[0], r[1], at, r[3], r[4], r[5], parse_utc(r[6]) if r[6] else at))
    events.sort(key=lambda e: (e.at, e.id))
    sent: dict[int, Event] = {}
    for e in events:
        if e.status in SENT_STATUSES and e.app not in sent:
            sent[e.app] = e
    batch = Counter(e.at for e in events if e.note == SHORTLIST_NOTE)
    facts = Facts(
        now=now, tz=tz, events=events, sent=sent, deliberate={}, deliberate_days={}, conn=conn
    )
    for e in events:
        if is_deliberate_early(e, batch):
            facts.deliberate.setdefault(e.app, e)
            facts.deliberate_days.setdefault(e.app, set()).add(facts.day(e.at))
    return facts


def is_deliberate_early(e: Event, batch: Counter[datetime]) -> bool:
    """An ``interested``/``preparing`` event you chose: a single shortlist press, Prepare
    packet, or a manual move that is not an automatic note or "Did you apply? Not yet"."""
    if e.status not in EARLY_STATUSES:
        return False
    if e.note == SHORTLIST_NOTE:
        return batch[e.at] < BULK_SHORTLIST
    if e.note == PREPARE_NOTE:
        return True
    return e.source == "manual" and e.note not in AUTO_NOTES and e.note != NOT_YET_NOTE


def is_user_note(note: str | None) -> bool:
    """A note you typed, not one a route or the classifier wrote."""
    text = (note or "").strip()
    return bool(text) and text not in GENERATED_NOTES and not text.startswith(GENERATED_PREFIXES)


# --- sent packets ------------------------------------------------------------------------


@dataclass(frozen=True)
class SentPacket:
    packet_id: int
    resume_origin: str
    cover_recorded: bool  # the packet has a cover letter and the application records it


def sent_packets(facts: Facts) -> dict[int, SentPacket]:
    """Sent application id -> its sent packet with a tailored (generated or edited) resume.

    The version is found from ``resume_version`` when it parses as ``packet:<id>/resume/v<n>``
    with a packet of that application, else from a ``resume`` attachment at
    ``packets/<id>/resume-v<n>.pdf`` for such a packet. A typed string that resolves to no
    such packet counts for nothing; a ``base`` version is not tailoring.
    """
    conn = facts.conn
    packets: dict[int, tuple[int, int | None]] = {
        r[0]: (r[1], r[2])
        for r in conn.execute("SELECT id, application_id, cover_doc_id FROM application_packet")
    }
    if not packets:
        return {}
    resumes: dict[tuple[int, int], str] = {
        (r[0], r[1]): r[2]
        for r in conn.execute(
            "SELECT packet_id, version, origin FROM packet_document WHERE kind = 'resume'"
        )
    }
    apps = {
        r[0]: (r[1] or "", r[2] or "")
        for r in conn.execute("SELECT id, resume_version, cover_letter_path FROM application")
    }
    attachments: dict[int, list[tuple[str, str]]] = {}
    for r in conn.execute(
        "SELECT application_id, kind, path FROM attachment "
        "WHERE kind IN ('resume', 'cover_letter') ORDER BY id"
    ):
        attachments.setdefault(r[0], []).append((r[1], r[2]))

    out: dict[int, SentPacket] = {}
    for app_id in facts.sent:
        if app_id not in apps:
            continue
        resume_version, cover_path = apps[app_id]
        candidates: list[tuple[int, int]] = []
        m = _PACKET_REF.match(resume_version.strip())
        if m:
            candidates.append((int(m[1]), int(m[2])))
        for kind, path in attachments.get(app_id, []):
            fm = _PACKET_FILE.search(path.replace("\\", "/")) if kind == "resume" else None
            if fm:
                candidates.append((int(fm[1]), int(fm[2])))
        for pid, version in candidates:
            pk = packets.get(pid)
            origin = resumes.get((pid, version))
            if pk is None or pk[0] != app_id or origin not in ("generated", "edited"):
                continue
            marker = f"packets/{pid}/"
            cover = pk[1] is not None and (
                marker in cover_path.replace("\\", "/")
                or any(
                    k == "cover_letter" and marker in p.replace("\\", "/")
                    for k, p in attachments.get(app_id, [])
                )
            )
            out[app_id] = SentPacket(pid, origin, cover)
            break
    return out


# --- active days -------------------------------------------------------------------------


def _active_days(facts: Facts) -> dict[date, list[tuple[str, object]]]:
    """specs/021 "Active day": a local day with at least one deliberate action."""
    conn = facts.conn
    days: dict[date, list[tuple[str, object]]] = {}

    def add(value: datetime | str | None, ref: tuple[str, object]) -> None:
        if value:
            days.setdefault(facts.day(value), []).append(ref)

    for app_id, e in facts.sent.items():
        if e.source == "email":  # a confirmation's time is when you applied
            add(e.at, ("application", app_id))
    for e in facts.events:
        if e.source == "manual" and (
            e.status in SENT_STATUSES or e.status in ("withdrawn", "no_response")
        ):
            add(e.recorded, ("application_event", e.id))
    for r in conn.execute("SELECT job_group_id, labeled_at FROM label"):
        add(r[1], ("label", r[0]))
    for r in conn.execute("SELECT id, at FROM followup_log WHERE backfill = 0"):
        add(r[1], ("followup_log", r[0]))
    for r in conn.execute("SELECT id, created_at FROM contact WHERE created_at IS NOT NULL"):
        add(r[1], ("contact", r[0]))
    for r in conn.execute("SELECT id, ready_at FROM application_packet WHERE ready_at IS NOT NULL"):
        add(r[1], ("application_packet", r[0]))
    for r in conn.execute("SELECT id, source, created_at, confirmed_at FROM rejection"):
        if r[1] == "manual":
            add(r[2], ("rejection", r[0]))
        add(r[3], ("rejection", r[0]))
    for r in conn.execute(
        "SELECT source_key, subscribed_at FROM alert_signup WHERE subscribed_at IS NOT NULL"
    ):
        add(r[1], ("alert_signup", r[0]))
    for r in conn.execute(
        f"SELECT id, captured_at FROM capture_log WHERE outcome IN "
        f"({','.join('?' * len(CAPTURE_ACTIVE))})",
        CAPTURE_ACTIVE,
    ):
        add(r[1], ("capture_log", r[0]))
    for r in conn.execute("SELECT id, at FROM profile_change WHERE source = 'ui'"):
        add(r[1], ("profile_change", r[0]))
    return days
