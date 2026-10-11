"""``jobhunter apply stats`` (specs/017 "Phase 2 gate"): how phase 1 is used. Read only.

The phase 2 gate asks for at least 8 packets marked ready for postings on the four public ATSs
(Greenhouse, Lever, Ashby, Workable) after four weeks. This counts packets by status and board,
documents by kind and runner, saved answers (counts only, never values), packet spend, and,
once phase 1e's ``capture_log`` table exists, captures by outcome, method and host.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from jobhunter.apply import checklists
from jobhunter.apply.packets import PACKET_REF_PREFIX

GATE_READY_ON_PUBLIC_ATS = 8
CAPTURE_TABLE = "capture_log"


@dataclass
class Stats:
    since: str | None
    packets: Counter[str] = field(default_factory=Counter)  # by status
    ready_by_ats: Counter[str] = field(default_factory=Counter)  # ats name or "other"
    sent_with_packet: int = 0
    documents: Counter[tuple[str, str]] = field(default_factory=Counter)  # (kind, origin/runner)
    saved_answers: Counter[str] = field(default_factory=Counter)  # by source
    api_usd: float = 0.0
    cli_calls: int = 0
    # None: the capture_log table is not there (phase 1e not merged or not migrated).
    captures: Counter[str] | None = None
    capture_methods: Counter[str] = field(default_factory=Counter)
    capture_hosts: Counter[str] = field(default_factory=Counter)

    @property
    def ready_on_public_ats(self) -> int:
        return sum(self.ready_by_ats[a] for a in checklists.PUBLIC_ATS)

    def format(self) -> str:
        out: list[str] = []
        window = f" since {self.since[:10]}" if self.since else ""
        total = sum(self.packets.values())
        parts = ", ".join(f"{s} {n}" for s, n in sorted(self.packets.items()))
        out.append(f"Packets{window}: {total}" + (f" ({parts})" if parts else ""))
        gate = self.ready_on_public_ats
        out.append(
            f"Ready on the four public ATSs: {gate} (phase 2 gate: {GATE_READY_ON_PUBLIC_ATS})"
        )
        others = sorted(set(self.ready_by_ats) - set(checklists.PUBLIC_ATS))
        for ats in (*checklists.PUBLIC_ATS, *others):
            out.append(f"  {ats}: {self.ready_by_ats[ats]}")
        out.append(f"Sent with a packet: {self.sent_with_packet}")
        if self.documents:
            out.append("Documents:")
            for (kind, how), n in sorted(self.documents.items()):
                out.append(f"  {kind.replace('_', ' ')} ({how}): {n}")
        n_answers = sum(self.saved_answers.values())
        detail = ", ".join(f"{s} {n}" for s, n in sorted(self.saved_answers.items()))
        out.append(f"Saved answers: {n_answers}" + (f" ({detail})" if detail else ""))
        out.append(
            f"Packet spend: ${self.api_usd:.2f} on the API; {self.cli_calls} subscription calls"
        )
        if self.captures is None:
            out.append("Captures: no capture_log table yet (phase 1e)")
        else:
            total = sum(self.captures.values())
            parts = ", ".join(f"{o} {n}" for o, n in sorted(self.captures.items()))
            out.append(f"Captures: {total}" + (f" ({parts})" if parts else ""))
            if self.capture_methods:
                methods = ", ".join(f"{m} {n}" for m, n in sorted(self.capture_methods.items()))
                out.append(f"  by method: {methods}")
            if self.capture_hosts:
                top = ", ".join(f"{h} {n}" for h, n in self.capture_hosts.most_common(10))
                out.append(f"  hosts with page or previewed captures: {top}")
        return "\n".join(out)


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def compute(conn: sqlite3.Connection, now: datetime, days: int | None = None) -> Stats:
    since = None if days is None else (now - timedelta(days=days)).astimezone(UTC).isoformat()
    s = Stats(since=since)
    cut = since or ""
    rows = conn.execute(
        "SELECT p.id, p.status, a.status AS app_status, a.applied_at, a.resume_version "
        "FROM application_packet p "
        "JOIN application a ON a.id = p.application_id WHERE p.created_at >= ?",
        (cut,),
    ).fetchall()
    for r in rows:
        s.packets[r["status"]] += 1
        if r["status"] == "ready":
            ats = checklists.ats_of(checklists.packet_urls(conn, r["id"])) or "other"
            s.ready_by_ats[ats] += 1
        # Sent with this packet: attach_sent_packet recorded it, or the application went out
        # (an applied date) while the packet was ready. A later rejection or no_response does
        # not unsend it; a draft packet was not what went out.
        ref = (r["resume_version"] or "").startswith(f"{PACKET_REF_PREFIX}{r['id']}/")
        if ref or (r["status"] == "ready" and r["applied_at"] is not None):
            s.sent_with_packet += 1
    for r in conn.execute(
        "SELECT kind, origin, runner, count(*) AS n FROM packet_document "
        "WHERE created_at >= ? GROUP BY kind, origin, runner",
        (cut,),
    ):
        how = r["origin"] if r["origin"] != "generated" else f"generated, {r['runner'] or '?'}"
        s.documents[(r["kind"], how)] += r["n"]
    ids = {r["id"] for r in rows}
    for r in conn.execute(
        "SELECT packet_id, source FROM packet_answer WHERE field_key LIKE 'q:%'"
    ).fetchall():
        if since is None or r["packet_id"] in ids:
            s.saved_answers[r["source"]] += 1
    day = cut[:10]
    for r in conn.execute(
        "SELECT tier, sum(calls) AS calls, sum(cost_usd) AS usd FROM llm_spend "
        "WHERE tier IN ('packet', 'packet-cli') AND day >= ? GROUP BY tier",
        (day,),
    ):
        if r["tier"] == "packet":
            s.api_usd += float(r["usd"] or 0)
        else:
            s.cli_calls += int(r["calls"] or 0)
    if _has_table(conn, CAPTURE_TABLE):
        s.captures = Counter()
        for r in conn.execute(
            f"SELECT outcome, method, host FROM {CAPTURE_TABLE} WHERE captured_at >= ?", (cut,)
        ):
            s.captures[r["outcome"]] += 1
            if r["method"]:
                s.capture_methods[r["method"]] += 1
            if r["method"] == "page" or r["outcome"] == "previewed":
                s.capture_hosts[r["host"]] += 1
    return s
