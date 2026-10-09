"""Email alert adapter (specs/012-email-ingest.md): job-alert mail under the alerts label.

``search`` reads new messages (Gmail historyId watermark), detects each email's family,
parses its job entries and yields one partial ``JobStub`` per entry. Dedupe comes first: an
entry matching a job already held in full (same normalized posting/apply URL, or the same
title + employer + state) only records a sighting and yields nothing.

``resolve`` fetches the posting only where robots allows it. The decision at list time is
conservative: hosts the registry records as ``Disallow: /`` (VOS, PA, MA, ...) and VOS links
generally get ``needs_resolve = False``, so the pipeline never sends a single request to them,
not even for robots.txt. Everything else goes through ``FetchContext``, which enforces
robots.txt on every hop; a refusal keeps the job partial.

Gmail access is read-only and lives in ``jobhunter.mail``; this module never imports httpx.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html import escape
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from rapidfuzz import fuzz
from selectolax.lexbor import LexborHTMLParser as HTMLParser

from jobhunter.core.fetch import (
    AccessDenied,
    RobotsDisallowed,
    SourceBlocked,
    TransientFetchError,
)
from jobhunter.core.geo import parse_city_state
from jobhunter.core.models import (
    DescriptionCompleteness,
    JobDetail,
    JobLocation,
    JobStub,
    Query,
    SourceRow,
)
from jobhunter.core.textnorm import parse_date
from jobhunter.mail.message import MailMessage
from jobhunter.mail.parsers import AlertEntry, Detection, parse_message, row_for_host
from jobhunter.pipeline.dedupe import normalize_employer
from jobhunter.pipeline.dedupe_url import normalize_apply_url
from jobhunter.pipeline.listing import to_iso

if TYPE_CHECKING:
    from jobhunter.config import Settings
    from jobhunter.core.fetch import FetchContext

log = logging.getLogger(__name__)

SOURCE_KEY = "us-mailalerts"
DEFAULT_LIMIT = 200
TITLE_MATCH = 92  # same threshold as the dedupe stage's near-duplicate title pass
_MIN_DESCRIPTION_CHARS = 200
# Registry robots classes whose hosts are never fetched from an emailed link.
_NO_FETCH_ROBOTS = frozenset({"disallow_all", "unknown"})
_DESCRIPTION_SELECTORS = (
    "[itemprop=description]",
    "#jobDescription",
    "#job-description",
    ".job-description",
    ".jobDescription",
    "[class*=description]",
    "[id*=description]",
    "main",
    "article",
)


# ─── resolve policy ─────────────────────────────────────────────────────────


def may_resolve(url: str, family: str, rows: Sequence[SourceRow]) -> bool:
    """Whether an emailed posting link may be followed at all (specs/012 table).

    False for hosts recorded as ``disallow_all`` (or unknown) in the registry, and for VOS
    links on hosts the registry does not know (VOS serves ``Disallow: /``). True otherwise;
    FetchContext still checks robots.txt before the request.
    """
    host = urlsplit(url).hostname or ""
    row = row_for_host(host, rows)
    if row is not None:
        return row.robots.status not in _NO_FETCH_ROBOTS
    return family != "vos"


# ─── dedupe-first ───────────────────────────────────────────────────────────


@dataclass
class Match:
    job_id: int
    method: str  # apply_url | title_employer_state


@dataclass
class DedupeIndex:
    """Jobs already held with a full description, indexed for the two email match rules."""

    by_url: dict[str, int] = field(default_factory=dict)
    by_block: dict[tuple[str, str], list[tuple[str, int]]] = field(default_factory=dict)

    @classmethod
    def build(cls, conn: sqlite3.Connection, exclude_source: str) -> DedupeIndex:
        idx = cls()
        jobs = conn.execute(
            "SELECT id, url, apply_url, title, employer, agency_raw FROM job "
            "WHERE source_key != ? AND description_completeness = 'full' "
            "AND description_raw IS NOT NULL AND description_raw != ''",
            (exclude_source,),
        ).fetchall()
        states: dict[int, set[str]] = {}
        for r in conn.execute("SELECT job_id, state FROM job_locations WHERE state IS NOT NULL"):
            states.setdefault(r[0], set()).add(r[1])
        for j in jobs:
            for raw in (j["url"], j["apply_url"]):
                key = normalize_apply_url(raw)
                if key:
                    idx.by_url.setdefault(key, j["id"])
            emp = normalize_employer(j["employer"] or j["agency_raw"])
            if emp:
                for st in states.get(j["id"], ()):
                    idx.by_block.setdefault((st, emp), []).append((j["title"], j["id"]))
        full = {j["id"] for j in jobs}
        for r in conn.execute(
            "SELECT a.final_url, g.canonical_job_id FROM apply_link a "
            "JOIN job_group g ON g.id = a.job_group_id WHERE a.final_url IS NOT NULL"
        ):
            key = normalize_apply_url(r[0])
            if key and r[1] in full:
                idx.by_url.setdefault(key, r[1])
        return idx

    def match(self, stub: JobStub) -> Match | None:
        key = normalize_apply_url(stub.url)
        if key and key in self.by_url:
            return Match(self.by_url[key], "apply_url")
        emp = normalize_employer(stub.agency_raw)
        if not emp:
            return None
        best: tuple[float, int] | None = None
        for loc in stub.locations:
            if not loc.state:
                continue
            for title, job_id in self.by_block.get((loc.state, emp), ()):
                score = fuzz.token_set_ratio(stub.title, title)
                if score >= TITLE_MATCH and (best is None or score > best[0]):
                    best = (score, job_id)
        return Match(best[1], "title_employer_state") if best else None


# ─── entry -> stub ──────────────────────────────────────────────────────────


def external_id_for(url: str) -> str:
    key = normalize_apply_url(url) or url
    return "m" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:20]


def _locations(entry: AlertEntry, origin: SourceRow | None) -> list[JobLocation]:
    raw = (entry.location_raw or "").strip()
    if raw:
        city, state = parse_city_state(raw)
        if state:
            return [JobLocation(state=state, city=city or None, is_primary=True)]
    if origin is not None and origin.state:
        city = raw if raw and "," not in raw and len(raw) < 60 else None
        return [JobLocation(state=origin.state, city=city, is_primary=True)]
    return []


def _description(entry: AlertEntry) -> str:
    """What the email says about the job, as HTML. Never more than the email holds."""
    if entry.snippet:
        return f"<p>{escape(entry.snippet)}</p>"
    facts = " · ".join(escape(x) for x in (entry.title, entry.employer, entry.location_raw) if x)
    return f"<p>{facts}</p><p>(The email alert included no description.)</p>"


def stub_for(
    src: SourceRow,
    det: Detection,
    entry: AlertEntry,
    msg: MailMessage,
    rows: Sequence[SourceRow],
) -> JobStub:
    resolvable = may_resolve(entry.url, det.family, rows)
    tz = str(det.origin.config.get("timezone", "UTC")) if det.origin else "UTC"
    posted = parse_date(entry.posted_raw, tz) if entry.posted_raw else None
    closes = parse_date(entry.closes_raw, tz) if entry.closes_raw else None
    return JobStub(
        source_key=src.key,
        external_id=external_id_for(entry.url),
        title=entry.title,
        url=entry.url,
        posted_at=posted or msg.date,
        closes_at=closes,
        location_raw=entry.location_raw,
        salary_raw=entry.salary_raw,
        agency_raw=entry.employer,
        description_raw=_description(entry),
        description_completeness=DescriptionCompleteness.partial,
        needs_resolve=resolvable,
        locations=_locations(entry, det.origin),
        extra={
            "family": det.family,
            "origin_source_key": det.origin.key if det.origin else None,
            "message_id": msg.message_id,
        },
    )


# ─── detail extraction (resolve) ────────────────────────────────────────────


def _jsonld_description(tree: HTMLParser) -> str | None:
    for node in tree.css('script[type="application/ld+json"]'):
        try:
            data = json.loads(node.text() or "")
        except ValueError:
            continue
        items = data if isinstance(data, list) else data.get("@graph", [data])
        for item in items if isinstance(items, list) else []:
            if isinstance(item, dict) and item.get("@type") == "JobPosting":
                desc = item.get("description")
                if isinstance(desc, str) and desc.strip():
                    return desc
    return None


def extract_description(html: str) -> str | None:
    """The posting's description HTML: JSON-LD JobPosting, then common containers."""
    tree = HTMLParser(html)
    if desc := _jsonld_description(tree):
        return desc
    for sel in _DESCRIPTION_SELECTORS:
        for node in tree.css(sel):
            text = node.text(separator=" ", deep=True) or ""
            if len(" ".join(text.split())) >= _MIN_DESCRIPTION_CHARS:
                return node.html
    return None


def _stored_family(conn: sqlite3.Connection, external_id: str) -> str | None:
    """The alert family recorded when the entry was listed (the runner's stub has no extra)."""
    row = conn.execute(
        "SELECT m.family FROM mail_entry e JOIN mail_message m ON m.message_id = e.message_id "
        "WHERE e.external_id = ? AND e.outcome = 'new' ORDER BY e.id LIMIT 1",
        (external_id,),
    ).fetchone()
    return row[0] if row else None


# ─── adapter ────────────────────────────────────────────────────────────────


@dataclass
class SyncStats:
    messages: int = 0
    entries: int = 0
    new: int = 0
    sightings: int = 0
    families: dict[str, int] = field(default_factory=dict)


class MailAlertsAdapter:
    family = "mailalerts"

    def __init__(
        self,
        service: Any = None,
        rows: Sequence[SourceRow] | None = None,
        limit: int = DEFAULT_LIMIT,
        clock: Any = None,
    ) -> None:
        self.service = service
        self._rows = list(rows) if rows is not None else None
        self.limit = limit
        self._clock = clock or (lambda: datetime.now(UTC))
        self.stats = SyncStats()

    @staticmethod
    def unavailable(settings: Settings) -> str | None:
        """Why the runner should skip this source (no Gmail token or client secrets)."""
        from jobhunter.mail import auth

        if auth.load_token() is None:
            return "no Gmail token; run: jobhunter mail auth"
        if not auth.client_secrets_path(settings).is_file():
            return "Google client secrets file missing; run: jobhunter mail auth"
        return None

    @property
    def rows(self) -> list[SourceRow]:
        if self._rows is None:
            from jobhunter.sources.registry import load_registry

            self._rows = load_registry()
        return self._rows

    def _service(self, settings: Settings) -> Any:
        if self.service is None:
            from jobhunter.mail import auth

            self.service = auth.build_service(settings)
        return self.service

    def search(
        self, src: SourceRow, query: Query, since: datetime, ctx: FetchContext
    ) -> Iterator[JobStub]:
        """Yield partial stubs for new alert entries. ``query`` and ``since`` do not apply:
        the alert subscriptions are the query and Gmail's historyId is the watermark."""
        from jobhunter.mail import sync

        del query, since
        conn = ctx.conn
        settings = ctx.settings
        service = self._service(settings)
        label = settings.mail.label
        batch = sync.plan_batch(service, conn, label, self.limit)
        index = DedupeIndex.build(conn, src.key)
        for mid in batch.message_ids:
            msg = sync.fetch_message(service, mid)
            yield from self._process(conn, src, msg, index)
        sync.store_watermark(conn, label, batch.label_id, batch.history_id, self._clock())

    def _process(
        self, conn: sqlite3.Connection, src: SourceRow, msg: MailMessage, index: DedupeIndex
    ) -> Iterator[JobStub]:
        now = to_iso(self._clock())
        det, found = parse_message(msg, self.rows)
        self.stats.messages += 1
        self.stats.families[det.family] = self.stats.families.get(det.family, 0) + 1
        conn.execute(
            "INSERT OR IGNORE INTO mail_message (message_id, history_id, family, "
            "origin_source_key, sender_domain, subject, received_at, entries, processed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                msg.message_id,
                msg.history_id,
                det.family,
                det.origin.key if det.origin else None,
                msg.sender_domain,
                msg.subject[:300],
                to_iso(msg.date) if msg.date else None,
                len(found),
                now,
            ),
        )
        if not found:
            log.warning("alert %s (%s): no job entries parsed", msg.message_id, det.family)
        for i, entry in enumerate(found):
            try:
                stub = stub_for(src, det, entry, msg, self.rows)
            except ValueError as exc:  # a malformed link; one bad entry never stops a run
                log.warning("alert %s entry %d skipped: %s", msg.message_id, i, exc)
                continue
            self.stats.entries += 1
            match = index.match(stub)
            if match is not None:
                self.stats.sightings += 1
                conn.execute(
                    "INSERT OR IGNORE INTO mail_entry (message_id, idx, outcome, external_id, "
                    "matched_job_id, match_method, title, url, seen_at) "
                    "VALUES (?, ?, 'sighting', ?, ?, ?, ?, ?, ?)",
                    (
                        msg.message_id,
                        i,
                        stub.external_id,
                        match.job_id,
                        match.method,
                        stub.title,
                        stub.url,
                        now,
                    ),
                )
                conn.execute("UPDATE job SET last_seen_at = ? WHERE id = ?", (now, match.job_id))
                continue
            self.stats.new += 1
            conn.execute(
                "INSERT OR IGNORE INTO mail_entry (message_id, idx, outcome, external_id, "
                "title, url, seen_at) VALUES (?, ?, 'new', ?, ?, ?, ?)",
                (msg.message_id, i, stub.external_id, stub.title, stub.url, now),
            )
            yield stub

    def resolve(self, stub: JobStub, ctx: FetchContext) -> JobDetail:
        """Full description where robots allows; otherwise the stub, still partial."""
        data = stub.model_dump()
        data["needs_resolve"] = False
        family = stub.extra.get("family") or _stored_family(ctx.conn, stub.external_id)
        if not may_resolve(stub.url, family or "generic", self.rows):
            return JobDetail(**data)  # never touch a disallowed host
        try:
            resp = ctx.get(stub.url)
        except (RobotsDisallowed, AccessDenied, SourceBlocked) as exc:
            log.info("keeping %s partial: %s", stub.url, exc)
            return JobDetail(**data)
        except TransientFetchError as exc:
            # A plain error: the runner logs it and retries next run without failing the source.
            raise RuntimeError(f"transient fetch error for {stub.url}: {exc}") from exc
        desc = extract_description(resp.text) if resp.status == 200 else None
        if desc:
            data.update(
                description_raw=desc,
                description_completeness=DescriptionCompleteness.full,
            )
        return JobDetail(**data)
