"""One rule per catalog entry (specs/021 "The catalog"), computed from scratch each time.

A rule returns a :class:`Series`: the counted items in crossing order. Tier ``k`` of an
achievement with thresholds ``(t1, t2, ...)`` is reached at ``items[tk - 1]``; its evidence is
the refs of the items up to that one (or, for a non-cumulative series such as a rhythm run,
that item's own refs). Families that count your applications are capped at 3 per local day;
triage and follow-up counts have their own caps. Weekly rules look at finished weeks only.

No rule reads a scoring, spending or re-score table, and no copy here talks about them: a test
greps this package's SQL for those names.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from functools import cached_property

from jobhunter.achievements import catalog
from jobhunter.achievements.facts import (
    CAPTURE_FOUND,
    CLOSING_STATUSES,
    RESPONSE_STATUSES,
    Facts,
    SentPacket,
    is_user_note,
    parse_utc,
    sent_packets,
    week_start,
)

APP_DAILY_CAP = 3  # every family that counts applications you sent (your answer, 2026-10-11)
DECIDE_DAILY_CAP = 20
NO_DAILY_CAP = 5
FOLLOW_DAILY_CAP = 3
BOUNCE_BACK_DAYS = 14
LONG_GAME_DAYS = 30
ACTIVE_WEEK_DAYS = 2  # an active week has this many active days or more
REST_DAYS = 2
COMEBACK_MISSED = 2
FREEZE_EVERY = 4
FREEZE_MAX = 2

Ref = tuple[str, object]


@dataclass(frozen=True)
class Item:
    at: datetime
    refs: tuple[Ref, ...]
    app: int | None = None  # for per-application caps


@dataclass
class Series:
    items: list[Item] = field(default_factory=list)
    cumulative: bool = True

    @property
    def progress(self) -> int:
        return len(self.items)

    def evidence(self, n: int) -> dict[str, list[object]]:
        """Evidence for the ``n``-th item (1-based): ``{table: [ids]}``, ids in order."""
        picked = self.items[:n] if self.cumulative else [self.items[n - 1]]
        out: dict[str, list[object]] = {}
        for it in picked:
            for table, ident in it.refs:
                ids = out.setdefault(table, [])
                if ident not in ids:
                    ids.append(ident)
        return out


@dataclass(frozen=True)
class Week:
    start: date
    active_days: int

    @property
    def active(self) -> bool:
        return self.active_days >= ACTIVE_WEEK_DAYS


@dataclass(frozen=True)
class RhythmStatus:
    run: int  # current run of active weeks (0: paused, or nothing yet)
    best: int
    freezes: int
    this_week_days: int  # active days so far in the current (in-progress) week


def capped(
    f: Facts, items: Iterable[Item], per_day: int, per_app_day: int | None = None
) -> list[Item]:
    """Items in time order, keeping at most ``per_day`` per local day (and ``per_app_day``
    per application per local day). The rest still exist; they just do not count."""
    out: list[Item] = []
    days: Counter[date] = Counter()
    app_days: Counter[tuple[int | None, date]] = Counter()
    for it in sorted(items, key=lambda i: i.at):
        d = f.day(it.at)
        if days[d] >= per_day:
            continue
        if per_app_day is not None and app_days[(it.app, d)] >= per_app_day:
            continue
        days[d] += 1
        app_days[(it.app, d)] += 1
        out.append(it)
    return out


def ordered(items: Iterable[Item]) -> list[Item]:
    return sorted(items, key=lambda i: i.at)


class Rules:
    """Every rule over one :class:`Facts` snapshot; shared pieces are computed once."""

    def __init__(self, facts: Facts) -> None:
        self.f = facts
        self.conn: sqlite3.Connection = facts.conn

    # --- shared

    @cached_property
    def packets(self) -> dict[int, SentPacket]:
        return sent_packets(self.f)

    @cached_property
    def targeted(self) -> set[int]:
        """Sent applications with a sent packet, or a deliberate early event on a local day
        before the sent date."""
        out = set(self.packets)
        for app_id, e in self.f.sent.items():
            sent_day = self.f.day(e.at)
            if any(d < sent_day for d in self.f.deliberate_days.get(app_id, ())):
                out.add(app_id)
        return out

    def sent_items(self, apps: Iterable[int] | None = None) -> list[Item]:
        keep = set(self.f.sent) if apps is None else set(apps)
        return [
            Item(e.at, (("application", app_id),), app_id)
            for app_id, e in self.f.sent.items()
            if app_id in keep
        ]

    @cached_property
    def followups(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT id, application_id, at, kind, due_at, backfill FROM followup_log "
            "ORDER BY at, id"
        ).fetchall()

    @cached_property
    def weeks(self) -> list[Week]:
        """Every finished week from the first active one to last week, in order."""
        counts: Counter[date] = Counter(week_start(d) for d in self.f.active)
        firsts = [w for w, c in counts.items() if c >= ACTIVE_WEEK_DAYS and self.f.finished(w)]
        if not firsts:
            return []
        out: list[Week] = []
        w = min(firsts)
        while self.f.finished(w):
            out.append(Week(w, counts.get(w, 0)))
            w += timedelta(days=7)
        return out

    def week_item(self, w: Week, refs: tuple[Ref, ...] | None = None) -> Item:
        return Item(self.f.week_end(w.start), refs or (("week", w.start.isoformat()),))

    # --- Setup

    def setup_prefs(self) -> Series:
        rows = self.conn.execute(
            "SELECT id, at FROM profile_change WHERE source = 'ui' ORDER BY at, id LIMIT 1"
        ).fetchall()
        return Series([Item(parse_utc(r[1]), (("profile_change", r[0]),)) for r in rows])

    def setup_alerts(self) -> Series:
        rows = self.conn.execute(
            "SELECT source_key, subscribed_at FROM alert_signup WHERE subscribed_at IS NOT NULL"
        ).fetchall()
        return Series(ordered(Item(parse_utc(r[1]), (("alert_signup", r[0]),)) for r in rows))

    def setup_mail(self) -> Series:
        row = self.conn.execute(
            "SELECT value FROM achievement_setting WHERE key = 'mail_setup_at'"
        ).fetchone()
        if row is None or not row[0]:
            return Series()
        try:
            at = parse_utc(row[0])
        except ValueError:
            return Series()
        return Series([Item(at, ())])

    # --- Search

    def _labels(self, where: str = "") -> list[Item]:
        rows = self.conn.execute(f"SELECT job_group_id, labeled_at FROM label {where}").fetchall()
        return [Item(parse_utc(r[1]), (("job_group", r[0]),)) for r in rows]

    def search_decide(self) -> Series:
        return Series(capped(self.f, self._labels(), DECIDE_DAILY_CAP))

    def search_no(self) -> Series:
        items = self._labels("WHERE label = 'not_interesting'")
        return Series(capped(self.f, items, NO_DAILY_CAP))

    def search_shortlist(self) -> Series:
        items = [Item(e.at, (("application", a),), a) for a, e in self.f.deliberate.items()]
        return Series(capped(self.f, items, APP_DAILY_CAP))

    def search_found(self) -> Series:
        rows = self.conn.execute(
            f"SELECT id, captured_at FROM capture_log WHERE outcome IN "
            f"({','.join('?' * len(CAPTURE_FOUND))}) ORDER BY captured_at, id",
            CAPTURE_FOUND,
        ).fetchall()
        return Series(ordered(Item(parse_utc(r[1]), (("capture_log", r[0]),)) for r in rows))

    # --- Apply

    def apply_first(self) -> Series:
        return Series(ordered(self.sent_items()))

    def apply_momentum(self) -> Series:
        return Series(capped(self.f, self.sent_items(), APP_DAILY_CAP))

    def apply_targeted(self) -> Series:
        return Series(capped(self.f, self.sent_items(self.targeted), APP_DAILY_CAP))

    def apply_tailored(self) -> Series:
        return Series(capped(self.f, self.sent_items(self.packets), APP_DAILY_CAP))

    def _packet_items(self, keep: Callable[[SentPacket], bool]) -> Series:
        items = [
            Item(self.f.sent[a].at, (("application", a), ("application_packet", p.packet_id)), a)
            for a, p in self.packets.items()
            if keep(p)
        ]
        return Series(ordered(items))

    def apply_own_words(self) -> Series:
        return self._packet_items(lambda p: p.resume_origin == "edited")

    def apply_cover(self) -> Series:
        return self._packet_items(lambda p: p.cover_recorded)

    def apply_quality_week(self) -> Series:
        by_week: dict[date, list[int]] = {}
        for app_id, e in self.f.sent.items():
            by_week.setdefault(week_start(self.f.day(e.at)), []).append(app_id)
        items = []
        for start, apps in sorted(by_week.items()):
            if not self.f.finished(start) or not 2 <= len(apps) <= 5:
                continue
            if all(a in self.targeted for a in apps):
                refs = tuple(("application", a) for a in sorted(apps))
                items.append(Item(self.f.week_end(start), refs))
        return Series(items)

    # --- Follow-up

    def _pairs(self, kind: str, *, include_backfill: bool = False) -> dict[tuple, sqlite3.Row]:
        """The first row of each distinct (application, due date) pair of ``kind``."""
        out: dict[tuple, sqlite3.Row] = {}
        for r in self.followups:
            if r["kind"] != kind or (r["backfill"] and not include_backfill):
                continue
            out.setdefault((r["application_id"], (r["due_at"] or "").strip()), r)
        return out

    @staticmethod
    def _fu_item(r: sqlite3.Row) -> Item:
        return Item(parse_utc(r["at"]), (("followup_log", r["id"]),), r["application_id"])

    def follow_plan(self) -> Series:
        items = [self._fu_item(r) for r in self._pairs("planned").values()]
        return Series(capped(self.f, items, FOLLOW_DAILY_CAP, 1))

    def follow_done(self) -> Series:
        items = [self._fu_item(r) for r in self._pairs("done").values()]
        closed: dict[int, sqlite3.Row] = {}
        for r in self.followups:
            if r["kind"] == "no_response":
                closed.setdefault(r["application_id"], r)
        items += [self._fu_item(r) for r in closed.values()]
        return Series(capped(self.f, items, FOLLOW_DAILY_CAP, 1))

    def follow_on_time(self) -> Series:
        planned: dict[tuple, list[datetime]] = {}
        for r in self.followups:
            if r["kind"] == "planned":  # a pre-migration plan still counts as the plan
                key = (r["application_id"], (r["due_at"] or "").strip())
                planned.setdefault(key, []).append(parse_utc(r["at"]))
        items = []
        for key, r in self._pairs("done").items():
            done_at = parse_utc(r["at"])
            due = key[1]
            if not due or not any(p < done_at for p in planned.get(key, ())):
                continue
            try:
                due_day = self.f.day(due)
            except ValueError:
                continue
            if self.f.day(done_at) <= due_day:
                items.append(self._fu_item(r))
        return Series(capped(self.f, items, FOLLOW_DAILY_CAP, 1))

    def _first_per_app(self, keep: Callable[..., bool]) -> list[Item]:
        first: dict[int, Item] = {}
        for e in self.f.events:
            if e.app not in first and keep(e):
                first[e.app] = Item(e.at, (("application", e.app),), e.app)
        return ordered(first.values())

    def follow_tidy(self) -> Series:
        return Series(
            self._first_per_app(lambda e: e.status == "no_response" and e.source == "manual")
        )

    def follow_contact(self) -> Series:
        row = self.conn.execute(
            "SELECT applied_at FROM schema_version WHERE version = 35"
        ).fetchone()
        migrated = parse_utc(row[0]) if row and row[0] else self.f.now.astimezone(UTC)
        rows = self.conn.execute("SELECT id, created_at FROM contact ORDER BY id").fetchall()
        return Series(
            ordered(Item(parse_utc(r[1]) if r[1] else migrated, (("contact", r[0]),)) for r in rows)
        )

    def follow_loop(self) -> Series:
        items = []
        for r in self.conn.execute("SELECT id, source, created_at, confirmed_at FROM rejection"):
            if r[1] == "manual":
                items.append(Item(parse_utc(r[2]), (("rejection", r[0]),)))
            elif r[3]:
                items.append(Item(parse_utc(r[3]), (("rejection", r[0]),)))
        items += [
            Item(e.recorded, (("application", e.app),), e.app)
            for e in self.f.events
            if e.status == "rejected" and e.source in ("manual", "email")
        ]
        return Series(ordered(items))

    def follow_lessons(self) -> Series:
        return Series(
            self._first_per_app(
                lambda e: (
                    e.status in CLOSING_STATUSES and e.source == "manual" and is_user_note(e.note)
                )
            )
        )

    # --- Outcomes

    def _responses(self, statuses: tuple[str, ...]) -> list[Item]:
        return self._first_per_app(lambda e: e.status in statuses)

    def out_heard_back(self) -> Series:
        return Series(self._responses(RESPONSE_STATUSES))

    def out_conversations(self) -> Series:
        return Series(self._responses(RESPONSE_STATUSES))

    def out_interview(self) -> Series:
        return Series(self._responses(("interview",)))

    def out_interviews(self) -> Series:
        return Series(self._responses(("interview",)))

    def out_offer(self) -> Series:
        return Series(self._responses(("offer",)))

    def out_long_game(self) -> Series:
        items = []
        for e in self.f.events:
            sent = self.f.sent.get(e.app)
            if e.status not in RESPONSE_STATUSES or sent is None:
                continue
            if (self.f.day(e.at) - self.f.day(sent.at)).days >= LONG_GAME_DAYS:
                items.append(Item(e.at, (("application", e.app),), e.app))
        return Series(ordered(items))

    def out_bounce_back(self) -> Series:
        noes: list[tuple[datetime, Ref]] = [
            (parse_utc(r[1]), ("rejection", r[0]))
            for r in self.conn.execute(
                "SELECT id, received_at FROM rejection WHERE state = 'confirmed'"
            )
        ]
        noes += [(e.at, ("application", e.app)) for e in self.f.events if e.status == "rejected"]
        noes.sort(key=lambda x: x[0])
        window = timedelta(days=BOUNCE_BACK_DAYS)
        items = []
        for app_id, e in self.f.sent.items():
            hit = next((ref for n, ref in noes if n < e.at <= n + window), None)
            if hit is not None:
                items.append(Item(e.at, (("application", app_id), hit), app_id))
        return Series(ordered(items))

    # --- Rhythm

    def _walk(self) -> tuple[list[Item], RhythmStatus]:
        """Runs of active weeks with freezes: one item per new best (non-cumulative)."""
        run = best = freezes = 0
        run_weeks: list[Week] = []
        items: list[Item] = []
        for w in self.weeks:
            if w.active:
                run += 1
                run_weeks.append(w)
                if run % FREEZE_EVERY == 0:
                    freezes = min(FREEZE_MAX, freezes + 1)
                if run > best:
                    best = run
                    refs = tuple(("week", x.start.isoformat()) for x in run_weeks)
                    items.append(self.week_item(w, refs))
            elif run and freezes:
                freezes -= 1  # the run continues but does not grow
            else:
                run = freezes = 0
                run_weeks = []
        this_week = sum(1 for d in self.f.active if week_start(d) == self.f.current_week)
        return items, RhythmStatus(run, best, freezes, this_week)

    def rhythm_weeks(self) -> Series:
        return Series(self._walk()[0], cumulative=False)

    def rhythm_status(self) -> RhythmStatus:
        return self._walk()[1]

    def rhythm_rest(self) -> Series:
        return Series(
            [self.week_item(w) for w in self.weeks if w.active and 7 - w.active_days >= REST_DAYS]
        )

    def rhythm_comeback(self) -> Series:
        missed = 0
        items = []
        for w in self.weeks:  # starts at the first active week, so one came before
            if w.active:
                if missed >= COMEBACK_MISSED:
                    items.append(self.week_item(w))
                missed = 0
            else:
                missed += 1
        return Series(items)


def rule_for(rules: Rules, achievement_id: str) -> Series:
    return getattr(rules, achievement_id.replace(".", "_"))()


def compute(facts: Facts) -> dict[str, Series]:
    """Every catalog rule over one snapshot."""
    rules = Rules(facts)
    return {a.id: rule_for(rules, a.id) for a in catalog.CATALOG}
