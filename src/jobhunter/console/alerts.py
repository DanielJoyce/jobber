"""Data layer for /alerts: the email-alert subscription checklist (specs/012 "Subscribing").

Covers the registry rows that email alerts stand in for (policy blocked or manual, plus NEOGOV
class B rows when present). User-entered state lives in ``alert_signup`` and
``alert_plus_rejected``; arrivals come from ``mail_message``. Nothing here touches the network
or Gmail, and every mail-derived field degrades to empty when the mail tables are empty.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from jobhunter.config import Mail
from jobhunter.core.db import transaction
from jobhunter.core.models import Policy, SourceRow
from jobhunter.scoring.profile import Profile

PLACEHOLDER_ADDRESS = "<you>+jobs@gmail.com"
SETUP_DOC = "specs/012-email-ingest.md#setting-up-gmail"
STALE_AFTER = timedelta(days=3)
MAX_KEYWORDS = 8
POSTED_WITHIN = "1 day"

STATUS_NOT_SUBSCRIBED = "not subscribed"
STATUS_SUBSCRIBED = "subscribed"
STATUS_RECEIVING = "receiving"
STATUS_STALE = "stale"
STATUS_UNSUBSCRIBED = "unsubscribed"


@dataclass(frozen=True)
class AlertRow:
    key: str
    name: str
    state: str | None
    class_: str
    policy: str
    entry: str
    domain: str  # the sender domain used for filters and "+ rejected"
    address: str
    address_is_placeholder: bool
    plain_address: bool  # True when the plain address is used (fallback or rejected)
    rejected: bool  # the board refused the +address; the domain is on the rejected list
    in_config: bool  # the domain is already in mail.fallback_sender_domains
    keywords: list[str]
    posted_within: str
    subscribed_at: datetime | None
    unsubscribed_at: datetime | None
    last_alert_at: datetime | None
    alerts_since: int
    status: str
    warning: str | None


@dataclass
class Summary:
    total: int = 0
    subscribed: int = 0
    receiving: int = 0
    stale: int = 0


@dataclass
class Checklist:
    rows: list[AlertRow] = field(default_factory=list)
    summary: Summary = field(default_factory=Summary)
    toml_line: str | None = None  # the config line to copy when rejected domains are missing
    placeholder: bool = False


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def bare_domain(value: str) -> str:
    """Lowercase domain of a host or address: no '@', no 'www.', no trailing dot."""
    host = value.strip().lower().lstrip("@").strip(".")
    if "@" in host:
        host = host.rpartition("@")[2]
    return host.removeprefix("www.")


def _host_domain(entry: str) -> str:
    return bare_domain(urlsplit(entry).hostname or "")


def domain_matches(domain: str, listed: list[str]) -> bool:
    """True when ``domain`` equals a listed domain or is a subdomain of one."""
    if not domain:
        return False
    return any(d and (domain == d or domain.endswith("." + d)) for d in listed)


def is_placeholder(address: str) -> bool:
    return not address.strip() or "<you>" in address


def plain_address(alerts_address: str) -> str:
    """The address without its ``+suffix``: ``<you>+jobs@example.com`` -> ``<you>@example.com``."""
    local, _, domain = alerts_address.strip().partition("@")
    return f"{local.partition('+')[0]}@{domain}" if domain else alerts_address


def suggest_criteria(profile: Profile) -> list[str]:
    """Broad keyword list from the profile's queries and target titles (specs/012)."""
    candidates = [kw for q in profile.queries for kw in q.keywords]
    candidates += [q.title for q in profile.queries if q.title]
    candidates += profile.target_titles
    out: list[str] = []
    seen: set[str] = set()
    for raw in candidates:
        text = raw.strip()
        if text and text.lower() not in seen:
            seen.add(text.lower())
            out.append(text)
    return out[:MAX_KEYWORDS]


def _configured(mail: Mail) -> list[str]:
    return [d for d in (bare_domain(x) for x in mail.fallback_sender_domains) if d]


def toml_line(mail: Mail, rejected_domains: list[str]) -> str | None:
    """The exact ``fallback_sender_domains`` line to paste, or None when nothing is missing."""
    configured = _configured(mail)
    missing = [d for d in rejected_domains if d not in configured]
    if not missing:
        return None
    merged: list[str] = []
    for d in configured + missing:
        if d not in merged:
            merged.append(d)
    quoted = ", ".join(f'"{d}"' for d in merged)
    return f"fallback_sender_domains = [{quoted}]"


def _signups(conn: sqlite3.Connection) -> dict[str, tuple[datetime | None, datetime | None]]:
    return {
        r["source_key"]: (_parse_ts(r["subscribed_at"]), _parse_ts(r["unsubscribed_at"]))
        for r in conn.execute("SELECT * FROM alert_signup")
    }


def _rejected(conn: sqlite3.Connection) -> dict[str, str | None]:
    return {
        r["domain"]: r["source_key"]
        for r in conn.execute("SELECT domain, source_key FROM alert_plus_rejected")
    }


@dataclass
class _Arrivals:
    times: list[datetime]
    latest_domain: str | None


def _arrivals(conn: sqlite3.Connection) -> dict[str, _Arrivals]:
    """Per origin source: every alert arrival time, and the newest sender domain seen."""
    out: dict[str, _Arrivals] = {}
    rows = conn.execute(
        "SELECT origin_source_key, sender_domain, received_at FROM mail_message "
        "WHERE origin_source_key IS NOT NULL ORDER BY received_at"
    )
    for r in rows:
        arr = out.setdefault(r["origin_source_key"], _Arrivals([], None))
        ts = _parse_ts(r["received_at"])
        if ts is not None:
            arr.times.append(ts)
        if r["sender_domain"]:
            arr.latest_domain = bare_domain(r["sender_domain"])
    return out


def checklist_rows(registry: list[SourceRow]) -> list[SourceRow]:
    """The rows email alerts cover: blocked or manual, plus NEOGOV class B rows."""
    picked = [
        r
        for r in registry
        if r.policy in (Policy.blocked, Policy.manual)
        or (r.class_.value == "B" and r.family == "neogov")
    ]
    return sorted(picked, key=lambda r: ((r.state or "ZZ"), r.name.lower()))


def build_checklist(
    conn: sqlite3.Connection,
    registry: list[SourceRow],
    mail: Mail,
    profile: Profile,
    now: datetime,
) -> Checklist:
    signups = _signups(conn)
    rejected = _rejected(conn)
    arrivals = _arrivals(conn)
    configured = _configured(mail)
    placeholder = is_placeholder(mail.alerts_address)
    keywords = suggest_criteria(profile)
    out = Checklist(placeholder=placeholder, toml_line=toml_line(mail, sorted(rejected)))

    for reg in checklist_rows(registry):
        arr = arrivals.get(reg.key)
        domain = (arr.latest_domain if arr and arr.latest_domain else None) or _host_domain(
            reg.entry
        )
        subscribed_at, unsubscribed_at = signups.get(reg.key, (None, None))
        is_rejected = domain in rejected
        in_config = domain_matches(domain, configured) or domain_matches(
            _host_domain(reg.entry), configured
        )
        use_plain = in_config or is_rejected
        if use_plain:
            address = plain_address(mail.alerts_address)
        else:
            address = mail.alerts_address.strip() or PLACEHOLDER_ADDRESS
        times = arr.times if arr else []
        last = max(times) if times else None
        since = [t for t in times if subscribed_at and t >= subscribed_at]

        status, warning = STATUS_NOT_SUBSCRIBED, None
        if subscribed_at is not None:
            if since:
                status = STATUS_RECEIVING
            elif now - subscribed_at > STALE_AFTER:
                status = STATUS_STALE
                warning = (
                    f"subscribed {subscribed_at.date().isoformat()} and no alerts since: "
                    "check spam, or the sign-up may have failed"
                )
            else:
                status = STATUS_SUBSCRIBED
        elif unsubscribed_at is not None:
            status = STATUS_UNSUBSCRIBED

        out.rows.append(
            AlertRow(
                key=reg.key,
                name=reg.name,
                state=reg.state,
                class_=reg.class_.value,
                policy=reg.policy.value,
                entry=reg.entry,
                domain=domain,
                address=address,
                address_is_placeholder=placeholder and not use_plain,
                plain_address=use_plain,
                rejected=is_rejected,
                in_config=in_config,
                keywords=keywords,
                posted_within=POSTED_WITHIN,
                subscribed_at=subscribed_at,
                unsubscribed_at=unsubscribed_at,
                last_alert_at=last,
                alerts_since=len(since),
                status=status,
                warning=warning,
            )
        )

    s = out.summary
    s.total = len(out.rows)
    for row in out.rows:
        if row.subscribed_at is not None:
            s.subscribed += 1
        if row.status == STATUS_RECEIVING:
            s.receiving += 1
        if row.status == STATUS_STALE:
            s.stale += 1
    return out


def mark_subscribed(conn: sqlite3.Connection, key: str, now: datetime) -> None:
    with transaction(conn):
        conn.execute(
            "INSERT INTO alert_signup (source_key, subscribed_at, unsubscribed_at, updated_at) "
            "VALUES (?, ?, NULL, ?) ON CONFLICT(source_key) DO UPDATE SET "
            "subscribed_at = excluded.subscribed_at, unsubscribed_at = NULL, "
            "updated_at = excluded.updated_at",
            (key, now.isoformat(), now.isoformat()),
        )


def mark_unsubscribed(conn: sqlite3.Connection, key: str, now: datetime) -> None:
    with transaction(conn):
        conn.execute(
            "INSERT INTO alert_signup (source_key, subscribed_at, unsubscribed_at, updated_at) "
            "VALUES (?, NULL, ?, ?) ON CONFLICT(source_key) DO UPDATE SET "
            "subscribed_at = NULL, unsubscribed_at = excluded.unsubscribed_at, "
            "updated_at = excluded.updated_at",
            (key, now.isoformat(), now.isoformat()),
        )


def reject_plus(conn: sqlite3.Connection, key: str, domain: str, now: datetime) -> None:
    """Record that this board's sender domain refused the +address (idempotent)."""
    clean = bare_domain(domain)
    if not clean:
        raise ValueError("no sender domain to record")
    with transaction(conn):
        conn.execute(
            "INSERT OR IGNORE INTO alert_plus_rejected (domain, source_key, rejected_at) "
            "VALUES (?, ?, ?)",
            (clean, key, now.isoformat()),
        )
