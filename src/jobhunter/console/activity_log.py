"""Run history and the Today card's last-24-hours numbers (specs/019 Addendum C, bug be1d232).

Read-only. Every query is bounded: each event kind is fetched newest first with a fixed
``LIMIT`` by primary key, and the window counts filter on one timestamp column each. Nothing
here renders a secret: scorer specs go through ``shown_value``/``hidden_label`` and stored
error text goes through :func:`safe_error`.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from jobhunter.apply import draft_runs
from jobhunter.config import hidden_label, hidden_value, shown_value
from jobhunter.pipeline.listing import to_iso
from jobhunter.scoring import screen

KINDS: dict[str, str] = {
    "run": "Pipeline runs",
    "rescore": "Re-scores",
    "batch": "Score batches",
    "deep": "Deep passes",
    "mail": "Mail fetches",
    "capture": "Extension captures",
    "draft": "Drafting",
}
PER_KIND = 100
TOTAL = 150
WINDOW = timedelta(hours=24)
ERROR_MAX = 200
_EMAIL = re.compile(r"[^\s\"'()]*@[^\s\"'(),;]+")
_URL_QUERY = re.compile(r"(https?://[^\s?#]+)[?#]\S*")
_BARE_QUERY = re.compile(r"\?\S*=\S*")  # a query string with no scheme: host/path?a=b
_BEARER = re.compile(r"\b(bearer|basic)\s+\S+", re.IGNORECASE)
_KEY_ASSIGN = re.compile(
    r"\b([\w.-]*(?:key|token|secret|password|passwd|auth|credential)[\w.-]*)\s*[=:]\s*\S+",
    re.IGNORECASE,
)
_PHONE = re.compile(r"(?<!\w)\+?\d[\d\s().-]{7,}\d(?!\w)")


def safe_error(text: object) -> str:
    """Stored error text made fit to show: no email, no key-shaped token, no URL query."""
    if not text:
        return ""
    out = re.sub(r"%40", "@", str(text), flags=re.IGNORECASE)
    out = _EMAIL.sub("(email)", out)
    out = _BEARER.sub(r"\1 (hidden)", out)
    out = _KEY_ASSIGN.sub(r"\1=(hidden)", out)
    out = _URL_QUERY.sub(r"\1", out)
    out = _BARE_QUERY.sub("", out)
    out = _PHONE.sub("(number)", out)
    out = " ".join(
        "(hidden)" if hidden_value(tok.strip(".,;:()[]'\"")) else tok for tok in out.split()
    )
    return out if len(out) <= ERROR_MAX else out[: ERROR_MAX - 1] + "…"


def scorer_text(spec: object) -> str:
    return shown_value(spec) or hidden_label(str(spec))


def _dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def duration_text(start: str | None, end: str | None) -> str:
    a, b = _dt(start), _dt(end)
    if a is None or b is None:
        return ""
    s = max(0, int((b - a).total_seconds()))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60:02d}s"
    return f"{s // 3600}h {s % 3600 // 60:02d}m"


@dataclass
class Event:
    at: str  # UTC ISO, sort key and the local-time source
    kind: str
    title: str
    status: str = ""  # ok | failed | running | interrupted | partial | ""
    detail: list[str] = field(default_factory=list)
    duration: str = ""
    cost: float | None = None
    cost_note: str = ""  # shown instead of a price (a subscription run is not charged)
    error: str = ""

    @property
    def kind_label(self) -> str:
        return KINDS[self.kind]


def _count_text(counts: object) -> list[str]:
    """``run.counts`` as short ``stage: key n, key n`` strings (scalars only)."""
    if not isinstance(counts, dict):
        return []
    lines = []
    for stage, val in counts.items():
        if isinstance(val, dict):
            parts = [
                f"{str(k)[:24]} {v}"
                for k, v in val.items()
                if isinstance(v, int | float | str) and len(str(v)) <= 12
            ]
            lines.append(f"{str(stage)[:24]}: " + (", ".join(parts[:8]) or "done"))
        elif isinstance(val, int | float | str) and len(str(val)) <= 12:
            lines.append(f"{str(stage)[:24]}: {val}")
    return lines[:10]


HOLDER_SLACK = timedelta(minutes=5)


def is_running(started_at: str | None, holder: Any) -> bool:
    """Whether the run lock's holder is this ``run`` row's process. Any job takes the lock
    (re-score, deep pass, score, backfill), so a held lock alone proves nothing: the holder
    must be ``run`` and must have started about when the row did."""
    if holder is None or getattr(holder, "command", None) != "run":
        return False
    a, b = _dt(started_at), getattr(holder, "started_at", None)
    if a is None or b is None:
        return False
    if b.tzinfo is None:
        b = b.replace(tzinfo=UTC)
    return abs(a - b) <= HOLDER_SLACK


def _run_status(r: sqlite3.Row, holder: Any, newest_open: bool) -> str:
    if r["ended_at"]:
        if r["exit_status"] == "ok":
            return "ok"
        return "partial" if r["exit_status"] == "degraded" else "failed"
    return "running" if (newest_open and is_running(r["started_at"], holder)) else "interrupted"


def _runs(conn: sqlite3.Connection, holder: Any) -> list[Event]:
    rows = conn.execute(
        'SELECT id, started_at, ended_at, stages, exit_status, counts, "trigger" AS trig, '
        "(SELECT COUNT(*) FROM run_source s WHERE s.run_id = run.id "
        " AND s.status NOT IN ('ok', 'skipped', 'unavailable')) AS bad "
        "FROM run ORDER BY id DESC LIMIT ?",
        (PER_KIND,),
    ).fetchall()
    out = []
    newest_open = True
    for r in rows:
        status = _run_status(r, holder, newest_open)
        if not r["ended_at"]:
            newest_open = False
        try:
            counts = json.loads(r["counts"]) if r["counts"] else {}
        except ValueError:
            counts = {}
        detail = _count_text(counts)
        if r["bad"]:
            detail.append(f"{r['bad']} source(s) did not finish ok")
        out.append(
            Event(
                r["started_at"],
                "run",
                f"Run ({r['trig'] or 'unknown'}): {str(r['stages'])[:80]}",
                status,
                detail,
                duration_text(r["started_at"], r["ended_at"]),
                error=f"exit status: {safe_error(r['exit_status'] or 'none')}"
                if status in ("failed", "partial")
                else "",
            )
        )
    return out


def _rescores(conn: sqlite3.Connection) -> list[Event]:
    out = []
    for r in conn.execute("SELECT * FROM rescore_request ORDER BY id DESC LIMIT ?", (PER_KIND,)):
        scorer = scorer_text(r["scorer"]) if r["scorer"] else "scorer not chosen yet"
        out.append(
            Event(
                r["requested_at"],
                "rescore",
                f"Re-score ({r['scope']}) with {scorer}",
                r["status"],
                [f"scored {r['scored']} of {r['total'] or r['job_count']}, errored {r['errored']}"],
                duration_text(r["started_at"], r["done_at"]),
                r["cost_usd"],
                error=safe_error(r["error"]),
            )
        )
    return out


def _batches(conn: sqlite3.Connection) -> list[Event]:
    out = []
    rows = conn.execute(
        "SELECT tier, model, request_count, submitted_at, collected_at, counts "
        "FROM score_batch ORDER BY submitted_at DESC LIMIT ?",
        (PER_KIND,),
    ).fetchall()
    for r in rows:
        who = scorer_text(r["model"])
        out.append(
            Event(
                r["submitted_at"],
                "batch",
                f"Batch submitted ({r['tier']}) with {who}",
                "ok",
                [f"{r['request_count']} request(s)"],
            )
        )
        if r["collected_at"]:
            try:
                counts = json.loads(r["counts"]) if r["counts"] else {}
            except ValueError:
                counts = {}
            if not isinstance(counts, dict):
                counts = {}
            parts = [f"{str(k)[:20]} {v}" for k, v in counts.items() if isinstance(v, int)]
            bad = any(isinstance(counts.get(k), int) and counts[k] for k in ("errored", "expired"))
            out.append(
                Event(
                    r["collected_at"],
                    "batch",
                    f"Batch collected ({r['tier']}) from {who}",
                    "partial" if bad else "ok",
                    [", ".join(parts[:8])] if parts else [],
                    duration_text(r["submitted_at"], r["collected_at"]),
                )
            )
    return out


def _deeps(conn: sqlite3.Connection) -> list[Event]:
    out = []
    for r in conn.execute("SELECT * FROM deep_request ORDER BY id DESC LIMIT ?", (PER_KIND,)):
        out.append(
            Event(
                r["requested_at"],
                "deep",
                f"Deep pass ({r['mode']}) with {scorer_text(r['scorer'])}",
                r["status"],
                [f"{r['done']} of {r['total']} taken, {r['scored']} new report(s)"],
                duration_text(r["started_at"], r["done_at"]),
                r["spent_usd"],
                error=safe_error(r["error"]),
            )
        )
    return out


def _mail(conn: sqlite3.Connection) -> list[Event]:
    rows = conn.execute(
        "SELECT processed_at, entries FROM mail_message ORDER BY rowid DESC LIMIT ?",
        (PER_KIND * 5,),
    ).fetchall()
    groups: dict[str, list[int]] = {}
    for r in rows:
        groups.setdefault(str(r["processed_at"])[:16], []).append(r["entries"] or 0)
    return [
        Event(
            minute + ":00+00:00",
            "mail",
            "Mail fetch",
            "ok",
            [f"{len(v)} message(s), {sum(v)} posting(s) found"],
        )
        for minute, v in list(groups.items())[:PER_KIND]
    ]


def _captures(conn: sqlite3.Connection) -> list[Event]:
    return [
        Event(
            r["captured_at"],
            "capture",
            f"Captured from {str(r['host'])[:60]}",
            "ok",
            [f"{r['outcome']}" + (f" via {r['method']}" if r["method"] else "")],
        )
        for r in conn.execute(
            "SELECT captured_at, host, method, outcome FROM capture_log ORDER BY id DESC LIMIT ?",
            (PER_KIND,),
        )
    ]


def _drafts(conn: sqlite3.Connection, data_dir: Path | None) -> list[Event]:
    out = []
    rows = conn.execute(
        "SELECT d.id, d.packet_id, d.question_key, d.started_at, d.finished_at, d.kind, "
        "d.runner, d.outcome, d.error, p.api_equiv_usd AS cost FROM draft_run d "
        "LEFT JOIN packet_document p ON p.id = d.doc_id ORDER BY d.id DESC LIMIT ?",
        (PER_KIND,),
    )
    for r in rows:
        if r["outcome"]:
            status = r["outcome"]
        elif data_dir is None:
            status = "stopped"
        else:
            status = (
                "running" if draft_runs.row_state(r, data_dir) == draft_runs.DRAFTING else "stopped"
            )
        subscription = r["runner"] == "cli"
        out.append(
            Event(
                r["started_at"],
                "draft",
                f"Drafted {str(r['kind'])[:40]} ({str(r['runner'])[:10]})",
                status,
                [],
                duration_text(r["started_at"], r["finished_at"]),
                None if subscription else r["cost"],
                "subscription" if subscription else "",
                error=safe_error(r["error"]),
            )
        )
    return out


def events(
    conn: sqlite3.Connection, kind: str, *, holder: Any = None, data_dir: Path | None = None
) -> list[Event]:
    """Newest first, at most :data:`TOTAL`. ``kind`` is one of :data:`KINDS` or ``all``."""
    fetchers: dict[str, Any] = {
        "run": lambda: _runs(conn, holder),
        "rescore": lambda: _rescores(conn),
        "batch": lambda: _batches(conn),
        "deep": lambda: _deeps(conn),
        "mail": lambda: _mail(conn),
        "capture": lambda: _captures(conn),
        "draft": lambda: _drafts(conn, data_dir),
    }
    wanted = [kind] if kind in fetchers else list(fetchers)
    out: list[Event] = []
    for k in wanted:
        try:
            out.extend(fetchers[k]())
        except sqlite3.Error:
            continue  # a table the database does not have yet: that kind is just empty
    out.sort(key=lambda e: _dt(e.at) or datetime.min.replace(tzinfo=UTC), reverse=True)
    return out[:TOTAL]


def last_run(conn: sqlite3.Connection, holder: Any) -> dict[str, Any] | None:
    try:
        r = conn.execute(
            'SELECT started_at, ended_at, exit_status, "trigger" AS trig FROM run '
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    except sqlite3.Error:
        return None
    if r is None:
        return None
    return {
        "started_at": r["started_at"],
        "status": _run_status(r, holder, True),
        "duration": duration_text(r["started_at"], r["ended_at"]),
        "trigger": r["trig"] or "unknown",
    }


def since_yesterday(
    conn: sqlite3.Connection, now: datetime, profile: Any = None, scorer: str | None = None
) -> dict[str, Any]:
    """The last 24 hours' counts. ``profile`` None means it did not load: the two numbers that
    need it (new jobs that passed, backlog) are None. A query that fails gives None, shown "?"."""
    since = to_iso(now - WINDOW)
    day_from = (now - WINDOW).astimezone(UTC).date().isoformat()

    def one(sql: str, *args: object) -> Any:
        try:
            return conn.execute(sql, args).fetchone()[0]
        except sqlite3.Error:
            return None

    passed = backlog = None
    if profile is not None:
        # New in the window AND passing under the current filter: a profile change rewrites
        # every prefilter_result.evaluated_at, so counting evaluations would count old jobs.
        passed = one(
            "SELECT COUNT(*) FROM job j JOIN prefilter_result p ON p.job_id = j.id "
            "AND p.passed = 1 AND p.filter_version = ? WHERE j.first_seen_at >= ?",
            profile.filter_version,
            since,
        )
        if scorer:
            try:
                backlog = screen.count_eligible(conn, profile, scorer=scorer, new_only=True)
            except (sqlite3.Error, ValueError, KeyError):
                backlog = None
    return {
        "jobs": one("SELECT COUNT(*) FROM job WHERE first_seen_at >= ?", since),
        "groups": one("SELECT COUNT(*) FROM job_group WHERE created_at >= ?", since),
        "passed": passed,
        "scored": one("SELECT COUNT(*) FROM fit_score WHERE created_at >= ?", since),
        # Three separate causes; they can overlap, so they are never summed.
        "refused": one("SELECT COUNT(*) FROM score_failure WHERE created_at >= ?", since),
        "rescore_errored": one(
            "SELECT COALESCE(SUM(errored), 0) FROM rescore_request "
            "WHERE COALESCE(done_at, requested_at) >= ?",
            since,
        ),
        "rescore_failed": one(
            "SELECT COUNT(*) FROM rescore_request WHERE status = 'failed' "
            "AND COALESCE(done_at, requested_at) >= ?",
            since,
        ),
        "batch_errored": one(
            "SELECT COALESCE(SUM(COALESCE(json_extract(counts, '$.errored'), 0) "
            " + COALESCE(json_extract(counts, '$.expired'), 0)), 0) FROM score_batch "
            "WHERE collected_at >= ? AND json_valid(counts)",
            since,
        ),
        "spend": one(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM fit_score WHERE created_at >= ?", since
        ),
        # llm_spend has only a day: today and yesterday (UTC), so it includes drafting and
        # calls that were refused, which fit_score never sees.
        "spend_days": one(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM llm_spend WHERE day >= ?", day_from
        ),
        "backlog": backlog,
    }
