"""Capture from any job page (specs/017 "Phase 1e": the console side).

**The extension reads, the console decides.** ``capture/extract.js`` sends raw facts from the
one page the user clicked on (JSON-LD objects as parsed, microdata, page text, selection, URLs,
on LinkedIn and Indeed the Apply control). Here, in tested Python:

1. **Map** the chosen JobPosting (or microdata, or a site row) to job fields. Description HTML
   becomes text (``textnorm.html_to_text``); captured HTML is never stored or rendered.
2. **Choose the URL**: a JSON-LD ``url`` only when it identifies one posting on the page's host
   or a known ATS, else the canonical link under the same test, else the page URL; tracking
   parameters dropped, other query parameters kept (many career systems carry the id there).
3. **Choose the posting** among several, and spot a stale one (a single-page board that kept
   the first job's JSON-LD).
4. **Dedupe** (board id, URL, decoded Apply destination, employer and title) and then add at
   once, or preview, or answer existing / possible / same job; all inside one
   ``BEGIN IMMEDIATE`` with the ``action_id`` lookup and the ``capture_log`` row, so two
   concurrent captures of one posting cannot both insert.

Capture adds; it never scores, prepares, fetches or re-scores anything. A captured group is a
``paste-manual`` group with ``score_on_request = 1``.
"""

from __future__ import annotations

import html
import json
import re
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from rapidfuzz import fuzz

from jobhunter.apply import packets, paste
from jobhunter.apply.capture_models import (
    MAX_POSTINGS,
    MAX_TEXT,
    AddRequest,
    BoardCapture,
    BoardInfo,
    CaptureRequest,
    CaptureResponse,
    DescribeRequest,
    DescribeResponse,
    Facts,
    FetchOffer,
    FetchRequest,
    FetchResponse,
    GroupCard,
    LinkOffer,
    LinkRequest,
    LinkResponse,
    MicroProp,
    PostingChoice,
    Preview,
    SiteFacts,
)
from jobhunter.console import detail
from jobhunter.core import bucketnames, db
from jobhunter.core.manual_sources import EMAIL_MANUAL, MANUAL_SOURCES, PASTE_MANUAL
from jobhunter.core.models import EmploymentType
from jobhunter.core.textnorm import html_to_text
from jobhunter.pipeline import dedupe_url
from jobhunter.pipeline.ats_rules import host_of, is_http_url, match_ats, unwrap
from jobhunter.pipeline.board_ids import board_id, board_key, board_of, board_of_host
from jobhunter.pipeline.dedupe import _refresh_group, normalize_employer
from jobhunter.pipeline.dedupe_url import _identifies_job, normalize_apply_url
from jobhunter.pipeline.dedupe_xstate import normalize_title
from jobhunter.pipeline.listing import to_iso
from jobhunter.pipeline.posting_urls import is_posting_url
from jobhunter.scoring.profile import Profile

__all__ = ["board_key"]  # re-exported: specs/017 names it apply/capture.py::board_key

MIN_DESCRIPTION = 200  # less is a snippet, an error page or a JavaScript shell
MIN_SELECTION = 40
TITLE_AGREE = 92  # rapidfuzz token_set_ratio, as dedupe's near-duplicate test
OFFER_AGREE = 80  # click-through offer: "roughly agree"
FETCH_ATS = ("greenhouse", "lever")  # robots.txt allows the direct board URL
SELECT_ATS = ("ashby",)  # robots.txt disallows its /api/: select the text instead
# Hosts whose og:site_name names the board, not the employer.
BOARD_SITE_HOSTS = re.compile(
    r"(?:^|\.)(?:linkedin|indeed|glassdoor|ziprecruiter|monster|dice|simplyhired|careerbuilder|"
    r"builtin|wellfound|usajobs|governmentjobs|schooljobs)\.[a-z.]+$"
)
WRITE_OUTCOMES = ("added", "existing", "linked", "description_added", "linked_groups", "not_same")
_EMPLOYMENT = {
    "FULL_TIME": EmploymentType.full_time,
    "PART_TIME": EmploymentType.part_time,
    "CONTRACTOR": EmploymentType.contract,
    "CONTRACT": EmploymentType.contract,
    "TEMPORARY": EmploymentType.temporary,
    "INTERN": EmploymentType.temporary,
    "PER_DIEM": EmploymentType.part_time,
    "SEASONAL": EmploymentType.seasonal,
}
_UNITS = {"HOUR": "HOUR", "DAY": "DAY", "WEEK": "WEEK", "MONTH": "MONTH", "YEAR": "YEAR"}
_TAG = re.compile(r"<[a-zA-Z/!]")


class CaptureError(ValueError):
    """A request the console refuses (422); the message is shown in the popup."""


class Conflict(Exception):
    """A request that conflicts with current state (409); nothing was written."""


# ─── small helpers ──────────────────────────────────────────────────────────


def _s(value: Any, limit: int = 1000) -> str:
    """A JSON-LD scalar as clean text, or ''. Lists give their first string."""
    if isinstance(value, list):
        value = next((v for v in value if isinstance(v, str | int | float)), "")
    if isinstance(value, dict):
        value = value.get("name") or value.get("@value") or ""
    if not isinstance(value, str | int | float) or isinstance(value, bool):
        return ""
    return " ".join(html.unescape(str(value)).split())[:limit]


def _types(obj: dict[str, Any]) -> list[str]:
    t = obj.get("@type")
    names = t if isinstance(t, list) else [t]
    return [str(n).rsplit("/", 1)[-1].rsplit(":", 1)[-1] for n in names if isinstance(n, str)]


def strip_tracking(url: str) -> str:
    """The URL with ``utm_*`` and ``dedupe_url._TRACKING_PARAMS`` dropped, other query
    parameters kept in order, and the fragment dropped."""
    p = urlsplit(url)
    kept = [
        (k, v)
        for k, v in parse_qsl(p.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in dedupe_url._TRACKING_PARAMS
    ]
    return urlunsplit((p.scheme, p.netloc, p.path, urlencode(kept), ""))


def tracking_params() -> list[str]:
    return sorted(dedupe_url._TRACKING_PARAMS)


def _key(url: str | None) -> str | None:
    try:
        return normalize_apply_url(url) if url else None
    except ValueError:
        return None


def identifies_posting(url: str | None) -> bool:
    key = _key(url)
    return bool(key) and _identifies_job(key)  # type: ignore[arg-type]


def _resolve(url: str | None, base: str) -> str | None:
    if not url or not isinstance(url, str):
        return None
    try:
        out = urljoin(base, url.strip())
    except ValueError:
        return None
    return out if is_http_url(out) else None


def _same_posting(a: str | None, b: str | None) -> bool:
    ka, kb = _key(a), _key(b)
    if ka and kb and ka == kb:
        return True
    ba, bb = board_key(a), board_key(b)
    return bool(ba) and ba == bb


def _plain(text: str) -> str:
    """Page text or a selection: trimmed, runs of blank lines collapsed, capped."""
    text = text.replace("\r\n", "\n").replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()[:MAX_TEXT]


def description_text(value: str) -> str:
    """A structured description as text. HTML goes through ``html_to_text`` (selectolax, no
    rendering); an entity-encoded description with no tags is decoded once more first."""
    value = value or ""
    if not _TAG.search(value) and re.search(r"&lt;\s*[a-zA-Z/]", value):
        value = html.unescape(value)
    if _TAG.search(value) or "&" in value:
        return html_to_text(value)[:MAX_TEXT]
    return _plain(value)


def _first_lines(text: str, n: int = 3, limit: int = 300) -> str:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    return "\n".join(lines[:n])[:limit]


# ─── 1. map ─────────────────────────────────────────────────────────────────


@dataclass
class Mapped:
    source: str  # jsonld | microdata | site
    title: str = ""
    employer: str = ""
    location_raw: str = ""
    salary_raw: str = ""
    posted_at: str = ""
    closes_at: str = ""
    employment_type: str = EmploymentType.unknown.value
    description: str = ""
    url: str | None = None
    identifier: str = ""


def postings_from_jsonld(blobs: Iterable[str]) -> list[dict[str, Any]]:
    """Every object whose ``@type`` is or includes JobPosting, in arrays and ``@graph``."""
    found: list[dict[str, Any]] = []

    def walk(node: Any, depth: int = 0) -> None:
        if depth > 4 or len(found) >= MAX_POSTINGS:
            return
        if isinstance(node, list):
            for item in node:
                walk(item, depth + 1)
        elif isinstance(node, dict):
            if "JobPosting" in _types(node):
                found.append(node)
                return
            if "@graph" in node:
                walk(node["@graph"], depth + 1)

    for blob in blobs:
        try:
            walk(json.loads(blob))
        except (ValueError, RecursionError):
            continue
    return found[:MAX_POSTINGS]


def _org(value: Any) -> str:
    if isinstance(value, list):
        value = value[0] if value else ""
    if isinstance(value, dict):
        return _s(value.get("name") or value.get("legalName"))
    return _s(value)


def _address(addr: Any) -> str:
    if isinstance(addr, str):
        return _s(addr)
    if not isinstance(addr, dict):
        return ""
    parts = [
        _s(addr.get(k))
        for k in ("addressLocality", "addressRegion", "postalCode", "addressCountry")
    ]
    out = ", ".join(p for p in parts if p)
    return out or _s(addr.get("streetAddress"))


def _locations(obj: dict[str, Any]) -> str:
    locs = obj.get("jobLocation")
    items = locs if isinstance(locs, list) else ([locs] if locs else [])
    out: list[str] = []
    for loc in items[:20]:
        text = _address(loc.get("address")) if isinstance(loc, dict) else _s(loc)
        if text and text not in out:
            out.append(text)
    kinds = obj.get("jobLocationType")
    kinds = kinds if isinstance(kinds, list) else [kinds]
    if any(isinstance(k, str) and k.upper() == "TELECOMMUTE" for k in kinds):
        out.append("Remote")
    return "; ".join(out)[:1000]


def _money(n: Any) -> str:
    try:
        f = float(n)
    except (TypeError, ValueError):
        return ""
    return f"{f:,.2f}".rstrip("0").rstrip(".") if f % 1 else f"{f:,.0f}"


def salary_text(base: Any) -> str:
    """``baseSalary`` as stated text ("USD 120,000-150,000 per YEAR"), which the normalize
    stage's salary parser reads."""
    if isinstance(base, list):
        base = base[0] if base else None
    if not isinstance(base, dict):
        return _s(base) if isinstance(base, str) else ""
    currency = _s(base.get("currency")) or "USD"
    value = base.get("value")
    unit = ""
    if isinstance(value, dict):
        unit = _s(value.get("unitText")).upper()
        lo = value.get("minValue", value.get("value"))
        hi = value.get("maxValue")
    else:
        lo, hi = value, None
        unit = _s(base.get("unitText")).upper()
    lo_s, hi_s = _money(lo), _money(hi)
    if not lo_s and not hi_s:
        return ""
    amount = f"{lo_s}-{hi_s}" if lo_s and hi_s and lo_s != hi_s else (lo_s or hi_s)
    per = f" per {_UNITS[unit]}" if unit in _UNITS else ""
    return f"{currency} {amount}{per}"


def employment_type(value: Any) -> str:
    items = value if isinstance(value, list) else [value]
    for item in items:
        if isinstance(item, str):
            got = _EMPLOYMENT.get(item.strip().upper().replace("-", "_").replace(" ", "_"))
            if got is not None:
                return got.value
    return EmploymentType.unknown.value


def map_jsonld(obj: dict[str, Any], page_url: str) -> Mapped:
    ident = obj.get("identifier")
    if isinstance(ident, dict):
        ident = ident.get("value") or ident.get("name")
    desc = obj.get("description")
    return Mapped(
        source="jsonld",
        title=_s(obj.get("title") or obj.get("name")),
        employer=_org(obj.get("hiringOrganization")),
        location_raw=_locations(obj),
        salary_raw=salary_text(obj.get("baseSalary") or obj.get("estimatedSalary")),
        posted_at=_s(obj.get("datePosted"), 64),
        closes_at=_s(obj.get("validThrough"), 64),
        employment_type=employment_type(obj.get("employmentType")),
        description=description_text(desc) if isinstance(desc, str) else "",
        url=_resolve(obj.get("url") if isinstance(obj.get("url"), str) else None, page_url),
        identifier=_s(ident, 200),
    )


def map_microdata(props: list[MicroProp]) -> Mapped | None:
    got: dict[str, str] = {}
    for p in props:
        got.setdefault(p.name.strip(), p.value)
    if not got:
        return None
    return Mapped(
        source="microdata",
        title=_s(got.get("title") or got.get("name")),
        employer=_s(got.get("hiringOrganization") or got.get("name.hiringOrganization")),
        location_raw=_s(got.get("jobLocation") or got.get("addressLocality")),
        salary_raw=_s(got.get("baseSalary") or got.get("salaryCurrency")),
        posted_at=_s(got.get("datePosted"), 64),
        closes_at=_s(got.get("validThrough"), 64),
        employment_type=employment_type(got.get("employmentType")),
        description=description_text(got.get("description", "")),
    )


def map_site(site: SiteFacts | None) -> Mapped | None:
    if site is None or not any((site.title, site.employer, site.description)):
        return None
    return Mapped(
        source="site",
        title=_s(site.title),
        employer=_s(site.employer),
        location_raw=_s(site.location),
        description=description_text(site.description or ""),
    )


# ─── 2-3. URL and posting choice, add or preview ────────────────────────────


@dataclass
class Analysis:
    page_url: str
    host: str
    url: str  # chosen URL for job.url
    postings: list[Mapped]
    index: int | None  # chosen posting
    stale: bool
    ambiguous: bool
    mapped: Mapped | None  # the structured source actually used
    title: str
    employer: str
    structured_title: bool
    description: str
    desc_source: str  # jsonld | microdata | site | selection | page
    page_description: str
    selection: str
    board: tuple[str, str] | None
    apply_mode: str
    destination: str | None
    fetch: FetchOffer | None
    reasons: list[str] = field(default_factory=list)

    @property
    def board_key(self) -> str | None:
        return f"{self.board[0]}:{self.board[1]}" if self.board else None

    @property
    def method(self) -> str:
        return self.desc_source

    @property
    def add_at_once(self) -> bool:
        return not self.reasons


def page_board_id(url: str) -> tuple[str, str] | None:
    """A board id named by the page URL itself (single-page boards change it by pushState)."""
    return board_id(url)


def _acceptable_url(url: str | None, page_host: str) -> bool:
    if not url or not identifies_posting(url):
        return False
    return host_of(url) == page_host or match_ats(url) is not None


def choose_url(posting_url: str | None, canonical: str | None, page_url: str) -> str:
    """The URL stored as ``job.url`` (specs/017 1e step 2)."""
    board = page_board_id(page_url) or board_id(canonical) or board_id(posting_url)
    if board is not None:
        return board_url(board)
    page_host = host_of(page_url)
    chosen = page_url
    for cand in (posting_url, _resolve(canonical, page_url)):
        if _acceptable_url(cand, page_host):
            chosen = cand  # type: ignore[assignment]
            break
    chosen = strip_tracking(chosen)
    try:
        return paste.direct_board_url(chosen)
    except paste.PasteError:
        return chosen


def board_url(board: tuple[str, str]) -> str:
    """The one URL a board posting is stored under (its id only)."""
    name, jid = board
    if name == "linkedin":
        return f"https://www.linkedin.com/jobs/view/{jid}/"
    return f"https://www.indeed.com/viewjob?jk={jid}"


def choose_posting(
    postings: list[Mapped], page_url: str, canonical: str | None
) -> tuple[int | None, bool, bool]:
    """(index, stale, ambiguous) for the JSON-LD postings found (specs/017 1e step 3)."""
    if not postings:
        return None, False, False
    page_board = page_board_id(page_url)

    def matches_board(m: Mapped) -> bool:
        return bool(page_board) and page_board[1] in f"{m.url or ''} {m.identifier}"

    def matches_page(m: Mapped) -> bool:
        return bool(m.url) and (
            _same_posting(m.url, page_url) or _same_posting(m.url, _resolve(canonical, page_url))
        )

    if page_board is not None:
        hits = [i for i, m in enumerate(postings) if matches_board(m)]
        if len(hits) == 1:
            return hits[0], False, False
        if not hits:
            return None, True, len(postings) > 1
        return None, False, True
    if len(postings) > 1:
        hits = [i for i, m in enumerate(postings) if matches_page(m)]
        return (hits[0], False, False) if len(hits) == 1 else (None, False, True)
    only = postings[0]
    # A single posting whose url names one posting but neither this page nor its canonical:
    # a single-page app may keep the first job's JSON-LD.
    if only.url and identifies_posting(only.url) and not matches_page(only):
        return None, True, False
    return 0, False, False


def decode_apply(facts: Facts) -> tuple[str, str | None]:
    """(apply_mode, destination) from a LinkedIn / Indeed Apply control, decoded locally.

    A direct off-board http(s) URL is the destination; a board redirect wrapper is unwrapped
    by ``ats_rules.unwrap`` with no request; a ``button`` (no href) or a board URL gives none.
    """
    ctl = facts.apply_control
    if ctl is None or board_of(facts.url) is None:
        return "unknown", None
    mode = ctl.kind
    href = _resolve(ctl.href, facts.url) if ctl.href else None
    for _ in range(3):
        if href is None or board_of(href) is None:
            break
        inner = unwrap(href)
        href = inner if inner and is_http_url(inner) else None
    if href is None or board_of(href) is not None:
        return mode, None
    return mode, strip_tracking(href)


def _fetch_offer(facts: Facts, destination: str | None, description: str) -> FetchOffer | None:
    """A supported ATS to fetch from (or select on), for a page with little description."""
    if len(description) >= MIN_DESCRIPTION:
        return None
    for url in [*(u for u in facts.iframes), *([destination] if destination else [])]:
        if not url or urlsplit(url).scheme != "https":
            continue
        rule = match_ats(url)
        if rule is None:
            continue
        try:
            direct = paste.direct_board_url(url)
        except paste.PasteError:
            continue
        if rule.name in FETCH_ATS:
            return FetchOffer(url=direct, host=host_of(direct), kind="fetch")
        if rule.name in SELECT_ATS:
            return FetchOffer(url=direct, host=host_of(direct), kind="select")
    return None


def analyse(facts: Facts, posting_index: int | None = None) -> Analysis:
    """Everything decided from the facts alone (no database): pure and table-tested."""
    page_url = facts.url
    if not is_http_url(page_url):
        raise CaptureError("only http(s) pages can be captured")
    host = host_of(page_url)
    postings = [map_jsonld(p, page_url) for p in postings_from_jsonld(facts.jsonld)]
    reasons: list[str] = []
    if posting_index is not None:
        if posting_index >= len(postings):
            raise CaptureError("that posting is not on the page")
        index, stale, ambiguous = posting_index, False, False
    else:
        index, stale, ambiguous = choose_posting(postings, page_url, facts.canonical)
    mapped: Mapped | None = postings[index] if index is not None else None
    if mapped is None:
        mapped = map_microdata(facts.microdata) or map_site(facts.site)
    if stale:
        reasons.append("the page's structured data names another job than this page")
    if ambiguous:
        reasons.append("several postings on the page; pick one")

    on_board = board_of_host(host) is not None or bool(BOARD_SITE_HOSTS.search(host))
    page_title, page_employer = paste.guess_from_page_title(facts.og_title or facts.document_title)
    if not page_employer and facts.og_site_name and not on_board:
        page_employer = _s(facts.og_site_name)
    structured_title = bool(mapped and mapped.title and mapped.employer)
    title = (mapped.title if mapped and mapped.title else page_title) or ""
    employer = (mapped.employer if mapped and mapped.employer else page_employer) or ""

    selection = _plain(facts.selection)
    if len(selection) < MIN_SELECTION:
        selection = ""
    structured_desc = mapped.description if mapped else ""
    page_description = structured_desc or _plain(facts.page_text)
    if facts.trigger == "menu-selection" and selection:
        description, desc_source = selection, "selection"
    elif structured_desc:
        description, desc_source = structured_desc, mapped.source  # type: ignore[union-attr]
    else:
        description, desc_source = _plain(facts.page_text), "page"

    if desc_source == "page":
        reasons.append("no structured posting data; check the page text")
    elif desc_source != "selection" and len(description) < MIN_DESCRIPTION:
        reasons.append("the structured description is short; check it")
    if not structured_title:
        reasons.append("no structured title and employer; check them")
    if selection and facts.trigger != "menu-selection":
        reasons.append("you have text selected: choose the page's description or it")

    board = page_board_id(page_url)
    apply_mode, destination = decode_apply(facts)
    url = choose_url(mapped.url if mapped else None, facts.canonical, page_url)
    return Analysis(
        page_url=page_url,
        host=host,
        url=url,
        postings=postings,
        index=index,
        stale=stale,
        ambiguous=ambiguous,
        mapped=mapped,
        title=title,
        employer=employer,
        structured_title=structured_title,
        description=description,
        desc_source=desc_source,
        page_description=page_description,
        selection=selection,
        board=board,
        apply_mode=apply_mode,
        destination=destination,
        fetch=_fetch_offer(facts, destination, description),
        reasons=reasons,
    )


# ─── cards ──────────────────────────────────────────────────────────────────


def card(
    conn: sqlite3.Connection, profile: Profile, group_id: int, now: datetime, why: str | None = None
) -> GroupCard | None:
    """What the popup shows for a group: fields, bucket and verdict when scored, packet."""
    d = detail.load_detail(conn, profile, group_id, now)
    if d is None:
        return None
    job = d.job
    text = job["description_text"] or ""
    url = detail.posting_url(job)
    return GroupCard(
        group_id=group_id,
        job_id=int(job["id"]),
        title=job["title"],
        employer=d.employer,
        location=d.location,
        salary=d.salary if d.salary_stated else "",
        posted_at=job["posted_at"],
        host=host_of(url) if is_http_url(url) else "pasted text",
        scored=d.scored,
        bucket=d.bucket,
        bucket_name=bucketnames.bucket_name(d.bucket) if d.bucket else None,
        verdict=d.verdict,
        packet_id=d.packet_id,
        partial=d.partial,
        has_description=bool(text.strip()),
        description_chars=len(text),
        first_lines=_first_lines(text),
        score_on_request=d.score_on_request,
        manual=job["source_key"] in MANUAL_SOURCES,
        why=why,
        path=f"/job/{group_id}",
    )


def current_group(conn: sqlite3.Connection, group_id: int, job_id: int | None) -> int | None:
    """Where a merged-away group's posting now lives: the job the popup holds, else the job a
    capture_log row recorded for that group."""
    jobs = [job_id] if job_id else []
    jobs += [
        int(r[0])
        for r in conn.execute(
            "SELECT job_id FROM capture_log WHERE job_group_id = ? AND job_id IS NOT NULL "
            "ORDER BY id DESC",
            (group_id,),
        )
    ]
    for jid in jobs:
        row = conn.execute("SELECT job_group_id FROM job WHERE id = ?", (jid,)).fetchone()
        if row is not None and row[0] is not None:
            return int(row[0])
    return None


def group_exists(conn: sqlite3.Connection, group_id: int) -> bool:
    return conn.execute("SELECT 1 FROM job_group WHERE id = ?", (group_id,)).fetchone() is not None


# ─── capture_log ────────────────────────────────────────────────────────────


def _prior(conn: sqlite3.Connection, action_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM capture_log WHERE action_id = ?", (action_id,)).fetchone()


def _log(
    conn: sqlite3.Connection,
    *,
    action_id: str,
    route: str,
    host: str,
    outcome: str,
    now: datetime,
    group_id: int | None = None,
    job_id: int | None = None,
    method: str | None = None,
    response: dict[str, Any] | None = None,
    ext_version: str | None = None,
) -> None:
    """One row per action. A write outcome keeps its (text-free) answer for a repeated
    ``action_id``; a non-write outcome is re-evaluated and its row updated."""
    stored = json.dumps(response) if outcome in WRITE_OUTCOMES and response is not None else None
    conn.execute(
        "INSERT INTO capture_log (action_id, route, job_group_id, job_id, captured_at, host, "
        "method, outcome, response, ext_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (action_id) DO UPDATE SET route = excluded.route, "
        "job_group_id = excluded.job_group_id, job_id = excluded.job_id, "
        "captured_at = excluded.captured_at, host = excluded.host, method = excluded.method, "
        "outcome = excluded.outcome, response = excluded.response, "
        "ext_version = excluded.ext_version",
        (
            action_id,
            route,
            group_id,
            job_id,
            to_iso(now),
            host[:255],
            method,
            outcome,
            stored,
            ext_version,
        ),
    )


def _replay(
    conn: sqlite3.Connection, prior: sqlite3.Row, profile: Profile, now: datetime
) -> CaptureResponse | None:
    """The stored answer of a repeated write action, with a fresh card."""
    if prior["response"] is None or prior["route"] not in ("capture", "capture/add"):
        return None
    data = json.loads(prior["response"])
    gid = data.get("group_id")
    if gid and not group_exists(conn, gid):
        gid = current_group(conn, gid, data.get("job_id"))
    group = card(conn, profile, gid, now) if gid else None
    return CaptureResponse(
        outcome=data["outcome"],
        message=data.get("message", ""),
        method=prior["method"],
        group=group,
        board=BoardInfo(**data["board"]) if data.get("board") else None,
        job_id=data.get("job_id"),
        replayed=True,
    )


def _stored(resp: CaptureResponse) -> dict[str, Any]:
    """The answer kept in ``capture_log.response``: ids, titles, employers; never text."""
    g = resp.group
    return {
        "outcome": resp.outcome,
        "message": resp.message,
        "group_id": g.group_id if g else None,
        "job_id": resp.job_id or (g.job_id if g else None),
        "title": g.title if g else None,
        "employer": g.employer if g else None,
        "board": resp.board.model_dump() if resp.board else None,
    }


# ─── job_board_ref and apply links ──────────────────────────────────────────


def record_board_ref(
    conn: sqlite3.Connection,
    board: tuple[str, str],
    job_id: int,
    apply_mode: str,
    apply_url: str | None,
    now: datetime,
) -> None:
    """Map a board job id to a job (the first mapping wins; the mode and destination are
    filled in when known)."""
    conn.execute(
        "INSERT INTO job_board_ref (board, board_id, job_id, apply_mode, apply_url, seen_at) "
        "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (board, board_id) DO UPDATE SET "
        "apply_mode = CASE WHEN excluded.apply_mode != 'unknown' THEN excluded.apply_mode "
        "ELSE job_board_ref.apply_mode END, "
        "apply_url = coalesce(excluded.apply_url, job_board_ref.apply_url), "
        "seen_at = excluded.seen_at",
        (board[0], board[1], job_id, apply_mode, apply_url, to_iso(now)),
    )


def offer_apply_link(conn: sqlite3.Connection, group_id: int, url: str, now: datetime) -> bool:
    """Write the decoded destination as the group's apply link when it has none, or only an
    ``unresolved`` / ``blocked`` one; never over ``live`` or ``expired``. No request is made."""
    row = conn.execute(
        "SELECT status FROM apply_link WHERE job_group_id = ?", (group_id,)
    ).fetchone()
    if row is not None and row[0] not in ("unresolved", "blocked"):
        return False
    rule = match_ats(url)
    conn.execute(
        "INSERT INTO apply_link (job_group_id, start_url, final_url, chain, ats, employer_host, "
        "status, resolved_at) VALUES (?, ?, NULL, '[]', ?, ?, 'unresolved', ?) "
        "ON CONFLICT (job_group_id) DO UPDATE SET start_url = excluded.start_url, "
        "final_url = NULL, chain = '[]', ats = excluded.ats, "
        "employer_host = excluded.employer_host, status = 'unresolved', "
        "resolved_at = excluded.resolved_at, verified_at = NULL, error = NULL",
        (group_id, url, rule.name if rule else None, host_of(url), to_iso(now)),
    )
    return True


def _canonical_job(conn: sqlite3.Connection, group_id: int) -> int | None:
    row = conn.execute(
        "SELECT canonical_job_id FROM job_group WHERE id = ?", (group_id,)
    ).fetchone()
    return int(row[0]) if row and row[0] is not None else None


# ─── decide ─────────────────────────────────────────────────────────────────


def _titles_agree(a: str, b: str) -> bool:
    ta, tb = normalize_title(a), normalize_title(b)
    if ta and tb and ta == tb:
        return True
    return bool(a and b) and fuzz.token_set_ratio(a, b) >= TITLE_AGREE


def _describe_targets(conn: sqlite3.Connection, group_ids: Iterable[int]) -> list[int]:
    """Manual groups (pasted or email-manual) whose canonical job has no description text."""
    out: list[int] = []
    for gid in group_ids:
        row = conn.execute(
            "SELECT j.source_key, coalesce(trim(j.description_text), '') AS t FROM job_group g "
            "JOIN job j ON j.id = g.canonical_job_id WHERE g.id = ?",
            (gid,),
        ).fetchone()
        if row is not None and row["source_key"] in (PASTE_MANUAL, EMAIL_MANUAL) and not row["t"]:
            out.append(gid)
    return out


def _preview(a: Analysis) -> Preview:
    m = a.mapped
    return Preview(
        title=a.title,
        employer=a.employer,
        location=m.location_raw if m else "",
        salary=m.salary_raw if m else "",
        description=a.description,
        page_description=a.page_description if a.selection else None,
        selection=a.selection or None,
        postings=[
            PostingChoice(
                index=i,
                title=p.title,
                employer=p.employer,
                url=p.url,
                first_line=_first_lines(p.description, 1, 160),
                description=p.description,
            )
            for i, p in enumerate(a.postings)
        ]
        if len(a.postings) > 1 or a.stale
        else [],
        posting_index=a.index,
        method=a.method,
        url=a.url,
        reasons=a.reasons,
    )


def _board_info(a: Analysis) -> BoardInfo | None:
    if a.board is None:
        return None
    return BoardInfo(
        key=a.board_key or "",
        board=a.board[0],
        apply_mode=a.apply_mode,  # type: ignore[arg-type]
        apply_url=a.destination,
        destination_host=host_of(a.destination) if a.destination else None,
    )


def _ident(a: Analysis) -> str:
    """What a Not the same answer is remembered by for a posting with no group yet."""
    return a.board_key or f"url:{_key(a.url) or a.url}"


def _said_not_same(conn: sqlite3.Connection, ident: str, gid: int) -> bool:
    for r in conn.execute("SELECT response FROM capture_log WHERE outcome = 'not_same'"):
        try:
            data = json.loads(r[0] or "{}")
        except ValueError:
            continue
        if data.get("ident") == ident and gid in data.get("pair", []):
            return True
    return False


def _not_same(conn: sqlite3.Connection, a: int, b: int) -> bool:
    pair = sorted((a, b))
    for r in conn.execute("SELECT response FROM capture_log WHERE outcome = 'not_same'"):
        try:
            if sorted(json.loads(r[0] or "{}").get("pair", [])) == pair:
                return True
        except (ValueError, AttributeError):
            continue
    return False


def _link_offer(
    conn: sqlite3.Connection, bc: BoardCapture | None, a: Analysis, group_id: int | None
) -> LinkOffer | None:
    """Click-through: "Same job as the LinkedIn posting you captured N minutes ago?"."""
    if bc is None or a.board is not None or not group_exists(conn, bc.group_id):
        return None
    if group_id is not None and (group_id == bc.group_id or _not_same(conn, bc.group_id, group_id)):
        return None
    emp_ok = bool(a.employer) and normalize_employer(a.employer) == normalize_employer(bc.employer)
    title_ok = bool(a.title and bc.title) and fuzz.token_set_ratio(a.title, bc.title) >= OFFER_AGREE
    if not (emp_ok or title_ok):
        return None
    ago = "just now" if bc.minutes_ago < 1 else f"{bc.minutes_ago} minutes ago"
    board = bc.board_key.split(":", 1)[0].title().replace("Linkedin", "LinkedIn")
    return LinkOffer(
        group_id=bc.group_id,
        board_key=bc.board_key,
        title=bc.title,
        employer=bc.employer,
        minutes_ago=bc.minutes_ago,
        message=(
            f"Same job as the {board} posting you captured {ago} ({bc.title} at {bc.employer})?"
        ),
    )


def _insert(
    conn: sqlite3.Connection,
    a: Analysis,
    *,
    title: str,
    employer: str,
    description: str,
    now: datetime,
) -> tuple[int, int]:
    m = a.mapped
    try:
        gid = paste.insert_pasted_posting(
            conn,
            url=a.url,
            text=description,
            employer=employer,
            title=title,
            now=now,
            salary_raw=(m.salary_raw or None) if m else None,
            location_raw=(m.location_raw or None) if m else None,
            posted_at=(m.posted_at or None) if m else None,
            closes_at=(m.closes_at or None) if m else None,
            employment_type=m.employment_type if m else None,
            page_url=a.page_url,
            apply_url=a.destination,
            locate=True,
        )
    except paste.PasteError as exc:
        raise CaptureError(str(exc)) from exc
    jid = _canonical_job(conn, gid)
    assert jid is not None
    if a.board is not None:
        record_board_ref(conn, a.board, jid, a.apply_mode, a.destination, now)
    return gid, jid


def capture(
    conn: sqlite3.Connection,
    req: CaptureRequest,
    profile: Profile,
    *,
    now: datetime,
    ext_version: str | None = None,
) -> CaptureResponse:
    """``POST /ext/v1/capture``: map, choose, dedupe, then add at once or answer."""
    a = analyse(req.facts)
    with db.transaction(conn):
        prior = _prior(conn, req.action_id)
        if prior is not None and (replayed := _replay(conn, prior, profile, now)) is not None:
            return replayed
        resp, gid, jid = _decide(conn, a, req.board_capture, profile, now)
        _log(
            conn,
            action_id=req.action_id,
            route="capture",
            host=a.host,
            outcome=resp.outcome,
            now=now,
            group_id=gid,
            job_id=jid,
            method=a.method,
            response=_stored(resp),
            ext_version=ext_version,
        )
    return resp


def _decide(
    conn: sqlite3.Connection,
    a: Analysis,
    bc: BoardCapture | None,
    profile: Profile,
    now: datetime,
) -> tuple[CaptureResponse, int | None, int | None]:
    board = _board_info(a)
    # A page URL that differs from a chosen URL naming one posting (a search page that keeps
    # its URL while the posting changes) is only a weak match: it may show another posting.
    separate = bool(
        identifies_posting(a.url) and _key(a.page_url) and _key(a.page_url) != _key(a.url)
    )
    dups = paste.find_duplicates(
        conn,
        a.url,
        a.employer,
        a.title,
        urls=() if separate else (a.page_url,),
        board_keys=(a.board_key,) if a.board_key else (),
    )
    if separate:
        seen = {d.group_id for d in dups}
        for d in paste.find_duplicates(conn, None, "", "", urls=(a.page_url,)):
            if d.group_id not in seen and d.why != paste.SAME_BOARD_ID:
                d.why = paste.SAME_SITE_URL
                dups.append(d)
    board_hits = [d for d in dups if d.why == paste.SAME_BOARD_ID]
    strong = [d for d in dups if d.by_url]
    weak = [d for d in dups if not d.by_url]
    dest_strong: list[paste.Duplicate] = []
    dest_weak: list[paste.Duplicate] = []
    if a.destination and identifies_posting(a.destination):
        # Linked at once only when an ATS rule says the destination is one posting; a
        # generic careers path is only offered, and only when the titles agree (one shared
        # apply path would otherwise offer every later job of that employer). A careers home
        # matches nothing.
        posting = is_posting_url(a.destination)
        ident = _ident(a)
        for d in paste.find_duplicates(conn, a.destination, "", ""):
            if d.why != paste.SAME_URL or any(d.group_id == b.group_id for b in board_hits):
                continue
            if _said_not_same(conn, ident, d.group_id):
                continue  # the user said Not the same for this posting and that group
            agree = _titles_agree(a.title, d.title)
            if posting and agree:
                dest_strong.append(d)
            elif posting or agree:
                dest_weak.append(d)

    def mk(outcome: str, message: str, gid: int | None = None, **kw: Any) -> CaptureResponse:
        group = card(conn, profile, gid, now) if gid else None
        cands = kw.pop("candidates", [])
        cand_ids = [c.group_id for c in cands]
        return CaptureResponse(
            outcome=outcome,  # type: ignore[arg-type]
            message=message,
            method=a.method,
            group=group,
            candidates=[c for c in cands if c is not None],
            describe=_describe_targets(conn, ([gid] if gid else []) + cand_ids),
            fetch=a.fetch,
            offer=_link_offer(conn, bc, a, gid),
            board=board,
            job_id=group.job_id if group else None,
            url=a.url,
            **kw,
        )

    def cards(ds: list[paste.Duplicate]) -> list[GroupCard]:
        return [c for d in ds if (c := card(conn, profile, d.group_id, now, d.why)) is not None]

    if (
        board_hits
        and dest_strong
        and dest_strong[0].group_id != board_hits[0].group_id
        and not _not_same(conn, board_hits[0].group_id, dest_strong[0].group_id)
    ):
        resp = mk(
            "same_job",
            "This board posting and the employer's posting are both in jobhunter: link them?",
            candidates=cards([board_hits[0], dest_strong[0]]),
            preview=_preview(a),
            link_mode="pair",
        )
        return resp, None, None
    if board_hits:
        gid = board_hits[0].group_id
        jid = _canonical_job(conn, gid)
        if a.board is not None and jid is not None:
            record_board_ref(conn, a.board, jid, a.apply_mode, a.destination, now)
        if a.destination:
            offer_apply_link(conn, gid, a.destination, now)
        return mk("existing", "Already in jobhunter", gid), gid, jid
    if dest_strong:
        gid = dest_strong[0].group_id
        jid = _canonical_job(conn, gid)
        if a.board is not None and jid is not None:
            record_board_ref(conn, a.board, jid, a.apply_mode, a.destination, now)
        g = card(conn, profile, gid, now)
        src = match_ats(a.destination or "")
        via = f", from {src.name.title()}" if src else ""
        name = (a.board[0] if a.board else "board").title().replace("Linkedin", "LinkedIn")
        msg = f"This {name} job is {g.title if g else a.title} at {g.employer if g else ''}{via}"
        return mk("linked", msg, gid), gid, jid
    if dest_weak:
        return (
            mk(
                "same_job",
                "The Apply button goes to a posting jobhunter has; is it this job?",
                candidates=cards(dest_weak[:3]),
                preview=_preview(a),
                link_mode="pick",
            ),
            None,
            None,
        )
    if strong:
        gid = strong[0].group_id
        return mk("existing", "Already in jobhunter", gid), gid, _canonical_job(conn, gid)
    if weak:
        return (
            mk(
                "possible",
                "Looks like a posting already in jobhunter",
                candidates=cards(weak[:5]),
                preview=_preview(a),
            ),
            None,
            None,
        )
    if a.add_at_once:
        gid, jid = _insert(
            conn, a, title=a.title, employer=a.employer, description=a.description, now=now
        )
        return mk("added", "Added to jobhunter", gid), gid, jid
    return mk("previewed", "Check this before adding it", preview=_preview(a)), None, None


def add(
    conn: sqlite3.Connection,
    req: AddRequest,
    profile: Profile,
    *,
    now: datetime,
    ext_version: str | None = None,
) -> CaptureResponse:
    """``POST /ext/v1/capture/add``: **Add** after a preview (the edited fields), or **Add as
    new** (``force_new``: past a ``possible`` match or a ``same_job`` offer; a sure match is
    still answered)."""
    a = analyse(req.facts, req.posting_index)
    title, employer = _s(req.title), _s(req.employer)
    description = _plain(req.description)
    if not title or not employer:
        raise CaptureError("title and employer are both needed")
    if not description:
        raise CaptureError("the description is empty; use the page's text or a selection")
    method = {"selection": "selection", "fetch": "fetch", "page": "page"}.get(req.source)
    if method is None:
        method = a.mapped.source if a.mapped and req.source == "structured" else a.desc_source
    # The user checked the fields: add with them, whatever the preview's reasons were.
    a.title, a.employer, a.description, a.desc_source, a.reasons = (
        title,
        employer,
        description,
        method,
        [],
    )
    with db.transaction(conn):
        prior = _prior(conn, req.action_id)
        if prior is not None and (replayed := _replay(conn, prior, profile, now)) is not None:
            return replayed
        resp, gid, jid = _decide(conn, a, None, profile, now)
        # Add as new: past a possible match or a "which one is it?" offer. Never past a
        # sure board-id match (a "pair" offer: this board posting is already here).
        if req.force_new and (
            resp.outcome == "possible" or (resp.outcome == "same_job" and resp.link_mode == "pick")
        ):
            gid, jid = _insert(
                conn, a, title=title, employer=employer, description=description, now=now
            )
            g = card(conn, profile, gid, now)
            resp = CaptureResponse(
                outcome="added",
                message="Added to jobhunter",
                method=method,
                group=g,
                board=_board_info(a),
                job_id=jid,
            )
        _log(
            conn,
            action_id=req.action_id,
            route="capture/add",
            host=a.host,
            outcome=resp.outcome,
            now=now,
            group_id=gid,
            job_id=jid,
            method=method,
            response=_stored(resp),
            ext_version=ext_version,
        )
    return resp


# ─── Add this description ───────────────────────────────────────────────────


def describe(
    conn: sqlite3.Connection,
    group_id: int,
    req: DescribeRequest,
    profile: Profile,
    *,
    now: datetime,
    ext_version: str | None = None,
) -> DescribeResponse:
    """``groups/{id}/describe``: fill an empty manual group's description and empty fields.

    No ``description_rev`` bump, so no re-score is queued. Refused (Conflict) when the group
    is not manual or already has text.
    """
    a = analyse(req.facts, req.posting_index)
    text = _plain(req.description) or a.description
    m = a.mapped
    fields = {
        "salary_raw": m.salary_raw if m else None,
        "location_raw": m.location_raw if m else None,
        "posted_at": m.posted_at if m else None,
        "closes_at": m.closes_at if m else None,
        "employment_type": m.employment_type if m else None,
    }
    with db.transaction(conn):
        prior = _prior(conn, req.action_id)
        if prior is not None and prior["response"] is not None:
            g = card(conn, profile, group_id, now)
            if g is not None:
                return DescribeResponse(outcome="description_added", group=g, replayed=True)
        if group_id not in _describe_targets(conn, [group_id]):
            raise Conflict("this posting already has a description, or is not one you added")
        try:
            paste.store_posting_text_in_txn(conn, group_id, text, now, fields)
        except paste.PasteError as exc:
            raise CaptureError(str(exc)) from exc
        jid = _canonical_job(conn, group_id)
        if a.board is not None and jid is not None:
            record_board_ref(conn, a.board, jid, a.apply_mode, a.destination, now)
        g = card(conn, profile, group_id, now)
        assert g is not None
        _log(
            conn,
            action_id=req.action_id,
            route="describe",
            host=a.host,
            outcome="description_added",
            now=now,
            group_id=group_id,
            job_id=jid,
            method=a.method,
            response={"outcome": "description_added", "group_id": group_id, "job_id": jid},
            ext_version=ext_version,
        )
    return DescribeResponse(outcome="description_added", group=g)


# ─── Link them ──────────────────────────────────────────────────────────────


def _scored(conn: sqlite3.Connection, gid: int) -> bool:
    return (
        conn.execute("SELECT 1 FROM fit_score WHERE job_group_id = ? LIMIT 1", (gid,)).fetchone()
        is not None
    )


def _busy(conn: sqlite3.Connection, gid: int, now: datetime) -> bool:
    """The group is being scored: an open batch item, or a ``user-requested`` claim younger
    than ``CLAIM_TTL`` on any member job that no score written since has answered."""
    from jobhunter.pipeline.dedupe import CLAIM_TTL, USER_REQUESTED_REASONS
    from jobhunter.pipeline.listing import from_iso

    if conn.execute(
        "SELECT 1 FROM score_batch_item i JOIN score_batch b ON b.id = i.batch_id "
        "WHERE i.job_group_id = ? AND b.collected_at IS NULL LIMIT 1",
        (gid,),
    ).fetchone():
        return True
    scores = []
    for r in conn.execute("SELECT created_at FROM fit_score WHERE job_group_id = ?", (gid,)):
        try:
            scores.append(from_iso(r[0]))
        except (TypeError, ValueError):
            continue
    for r in conn.execute(
        "SELECT p.evaluated_at FROM prefilter_result p JOIN job j ON j.id = p.job_id "
        "WHERE j.job_group_id = ? AND p.reasons = ?",
        (gid, USER_REQUESTED_REASONS),
    ):
        try:
            started = from_iso(r[0])
        except (TypeError, ValueError):
            continue
        if now - started < CLAIM_TTL and not any(at >= started for at in scores):
            return True
    return False


def _rescore_running(conn: sqlite3.Connection, now: datetime) -> bool:
    """A re-score run is writing scores now: a merge could delete a group it is about to
    write (its spend record would roll back with the failed write)."""
    from jobhunter.scoring import rescore

    return rescore.running_request(conn, now) is not None


def _flag(conn: sqlite3.Connection, gid: int) -> int:
    row = conn.execute("SELECT score_on_request FROM job_group WHERE id = ?", (gid,)).fetchone()
    return int(row[0]) if row else 0


def _applied(conn: sqlite3.Connection, gid: int) -> bool:
    return (
        conn.execute("SELECT 1 FROM application WHERE job_group_id = ?", (gid,)).fetchone()
        is not None
    )


def _ats_capture(conn: sqlite3.Connection, gid: int) -> bool:
    row = conn.execute(
        "SELECT j.url FROM job_group g JOIN job j ON j.id = g.canonical_job_id WHERE g.id = ?",
        (gid,),
    ).fetchone()
    return row is not None and match_ats(row[0] or "") is not None


def _older(conn: sqlite3.Connection, a: int, b: int) -> int:
    rows = conn.execute(
        "SELECT id FROM job_group WHERE id IN (?, ?) ORDER BY created_at, id", (a, b)
    ).fetchall()
    return int(rows[0][0])


def keep_of(conn: sqlite3.Connection, a: int, b: int) -> tuple[int, int]:
    """(kept group, its ``score_on_request`` after) for ``link_same_job`` (specs/017).

    "Ingested" means scored by the nightly run: the group flag is 0. The canonical job's source
    says nothing about scoring (a captured group kept on request can have an ingested copy as
    its canonical, for its text). The kept group always keeps its own flag.
    """
    sa, sb = _scored(conn, a), _scored(conn, b)
    if sa != sb:
        kept = a if sa else b
        return kept, _flag(conn, kept)
    fa, fb = _flag(conn, a), _flag(conn, b)
    if fa != fb:
        kept = a if fa == 0 else b  # the nightly-scored (ingested) group
        return kept, 0
    if fa == 0:
        return _older(conn, a, b), 0
    for test in (_applied, _ats_capture):
        ta, tb = test(conn, a), test(conn, b)
        if ta != tb:
            return (a if ta else b), 1
    return _older(conn, a, b), 1


def link_same_job(conn: sqlite3.Connection, a: int, b: int, now: datetime) -> int:
    """Merge two groups the user said are the same job (no transaction of its own).

    Built on the nightly merge (``dedupe_url._absorb``): jobs, applications (packets re-pointed
    or abandoned), labels, scores and apply links follow the kept group; ``job_board_ref``
    follows its jobs. No score is redone. Refused (Conflict, nothing written) while either
    group is being scored or a re-score run is writing. Returns the kept group.
    """
    if a == b:
        return a
    for gid in (a, b):
        if not group_exists(conn, gid):
            raise KeyError(gid)
        if _busy(conn, gid, now):
            raise Conflict("scoring in progress; link when it finishes")
    if _rescore_running(conn, now):
        raise Conflict("a re-score is running; link when it finishes")
    kept, flag = keep_of(conn, a, b)
    other = b if kept == a else a
    dedupe_url._absorb(conn, kept, other, now, dedupe_url.MergeResult())
    _refresh_group(conn, kept)
    conn.execute("UPDATE job_group SET score_on_request = ? WHERE id = ?", (flag, kept))
    if flag == 0:
        # A pasted or captured job sat at 'normalized' so no nightly stage touched it; in a
        # group the nightly run scores, it is an ordinary member (as dedupe._after_join does).
        conn.execute(
            "UPDATE job SET stage = 'grouped' WHERE job_group_id = ? AND stage = 'normalized'",
            (kept,),
        )
    return kept


def _may_link(
    conn: sqlite3.Connection, a: int, b: int, board_key: str | None, apply_url: str | None
) -> bool:
    """Two existing groups are merged only when they are surely the same posting: one is a
    ``same_job_candidates`` of the other, or one holds the board id being linked and the
    other's own posting URL is the employer posting the board's Apply led to. The client's
    board key alone is never enough, and two candidates that merely share a careers home
    are never merged."""
    if b in {d.group_id for d in same_job_candidates(conn, a)}:
        return True
    if a in {d.group_id for d in same_job_candidates(conn, b)}:
        return True
    if not (board_key and apply_url):
        return False
    key = _key(apply_url)
    if not key or not _identifies_job(key):
        return False
    for holder in set(paste.board_groups(conn, board_key)) & {a, b}:
        other = b if holder == a else a
        own = {
            k
            for r in conn.execute("SELECT url, page_url FROM job WHERE job_group_id = ?", (other,))
            for u in r
            if (k := _key(u))
        }
        if key in own:
            return True
    return False


def link(
    conn: sqlite3.Connection,
    req: LinkRequest,
    *,
    now: datetime,
    ext_version: str | None = None,
) -> LinkResponse:
    """``POST /ext/v1/link``: **Link them** or **Not the same**, only on the user's click."""
    with db.transaction(conn):
        prior = _prior(conn, req.action_id)
        if prior is not None and prior["response"] is not None:
            data = json.loads(prior["response"])
            return LinkResponse(
                outcome=data["outcome"],
                group_id=data.get("group_id"),
                message=data.get("message", ""),
                replayed=True,
            )
        for gid in (req.a, req.b):
            if gid is not None and not group_exists(conn, gid):
                raise KeyError(gid)
        b = req.b or req.a
        if req.not_same:
            msg = "Not linked; this offer will not be made again for these two"
            _log(
                conn,
                action_id=req.action_id,
                route="link",
                host="",
                outcome="not_same",
                now=now,
                group_id=req.a,
                response={
                    "outcome": "not_same",
                    "pair": sorted((req.a, b)),
                    "ident": req.board_key
                    or (f"url:{_key(req.apply_url)}" if req.apply_url else None),
                    "message": msg,
                },
                ext_version=ext_version,
            )
            return LinkResponse(outcome="not_same", group_id=None, message=msg)
        if b != req.a:
            for gid in (req.a, b):
                if _busy(conn, gid, now):
                    raise Conflict("scoring in progress; link when it finishes")
            if not _may_link(conn, req.a, b, req.board_key, req.apply_url):
                raise CaptureError(
                    "only a posting you captured can be linked: pick the one that is this job"
                )
        kept = link_same_job(conn, req.a, b, now)
        if req.board_key:
            board, _, bid = req.board_key.partition(":")
            if board not in ("linkedin", "indeed") or not bid:
                raise CaptureError("not a LinkedIn or Indeed job id")
            dest = (
                req.apply_url
                or conn.execute(
                    "SELECT j.url FROM job_group g JOIN job j ON j.id = g.canonical_job_id "
                    "WHERE g.id = ?",
                    (kept,),
                ).fetchone()[0]
            )
            jid = _canonical_job(conn, kept)
            ats_url = (
                strip_tracking(dest) if is_http_url(dest or "") and board_of(dest) is None else None
            )
            if jid is not None:
                record_board_ref(conn, (board, bid), jid, "offsite", ats_url, now)
                conn.execute(
                    "UPDATE job_board_ref SET apply_url = coalesce(?, apply_url) "
                    "WHERE board = ? AND board_id = ?",
                    (ats_url, board, bid),
                )
        msg = "Linked: one job in jobhunter now"
        _log(
            conn,
            action_id=req.action_id,
            route="link",
            host="",
            outcome="linked_groups",
            now=now,
            group_id=kept,
            job_id=_canonical_job(conn, kept),
            response={"outcome": "linked_groups", "group_id": kept, "message": msg},
            ext_version=ext_version,
        )
    return LinkResponse(outcome="linked_groups", group_id=kept, message=msg)


# ─── Fetch from a supported ATS ─────────────────────────────────────────────


def check_fetch_url(url: str) -> str:
    """The direct board URL to fetch, or CaptureError (422) for anything else: re-checked on
    the server whatever the popup sent (https, a supported ATS, never an embed)."""
    if urlsplit(url).scheme != "https":
        raise CaptureError("only https URLs on a supported ATS can be fetched")
    rule = match_ats(url)
    if rule is None or rule.name not in FETCH_ATS + SELECT_ATS:
        raise CaptureError("only Greenhouse, Lever or Ashby postings can be fetched")
    try:
        direct = paste.direct_board_url(url)
    except paste.PasteError as exc:
        raise CaptureError(str(exc)) from exc
    if urlsplit(direct).scheme != "https" or match_ats(direct) is not rule:
        raise CaptureError("only https URLs on a supported ATS can be fetched")
    return direct


def fetch(
    conn: sqlite3.Connection,
    req: FetchRequest,
    fetcher: Callable[[str], paste.Fetched],
    *,
    now: datetime,
    ext_version: str | None = None,
) -> FetchResponse:
    """``capture/fetch``: 1a's Fetch posting text on the direct board URL (robots.txt decides).

    Writes nothing but a ``capture_log`` row; the text goes to the preview.
    """
    direct = check_fetch_url(req.url)
    got = fetcher(direct)  # raises paste.PasteError with the message for the popup
    with db.transaction(conn):
        _log(
            conn,
            action_id=req.action_id,
            route="capture/fetch",
            host=host_of(direct),
            outcome="fetched",
            now=now,
            method="fetch",
            ext_version=ext_version,
        )
    return FetchResponse(url=direct, text=got.text, title=got.title, employer=got.employer)


def facts_from_dict(data: dict[str, Any]) -> Facts:
    """For tests and the CLI: validate a facts object."""
    return Facts.model_validate(data)


def same_job_candidates(conn: sqlite3.Connection, group_id: int) -> list[paste.Duplicate]:
    """Other groups that are surely the same posting as this one (a shared board id, or a
    one-posting URL among its jobs' URLs, apply links and decoded board destinations): what
    ``/job/{id}`` offers to **Link them**. Employer and title alone are never offered."""
    own: list[str | None] = []  # the posting's own URLs
    applies: list[str | None] = []  # where its Apply goes: counted only when one posting
    keys: list[str] = []
    for r in conn.execute(
        "SELECT url, apply_url, page_url FROM job WHERE job_group_id = ?", (group_id,)
    ):
        own += [r[0], r[2]]
        applies.append(r[1])
    for r in conn.execute(
        "SELECT r.board, r.board_id, r.apply_url FROM job_board_ref r JOIN job j "
        "ON j.id = r.job_id WHERE j.job_group_id = ?",
        (group_id,),
    ):
        keys.append(f"{r[0]}:{r[1]}")
        applies.append(r[2])
    link = conn.execute(
        "SELECT start_url, final_url FROM apply_link WHERE job_group_id = ?", (group_id,)
    ).fetchone()
    if link is not None:
        applies += [link[0], link[1]]
    real = tuple(
        [u for u in own if u and is_http_url(u)] + [u for u in applies if u and is_posting_url(u)]
    )
    if not real and not keys:
        return []
    dups = paste.find_duplicates(conn, None, "", "", urls=real, board_keys=tuple(keys))
    out = [d for d in dups if d.by_url and d.group_id != group_id]
    # The other direction: a board posting whose decoded Apply destination is this posting.
    mine = {k for u in real if (k := _key(u)) and _identifies_job(k)}
    seen = {d.group_id for d in out}
    for r in conn.execute(
        "SELECT j.job_group_id, j.title, j.employer, j.agency_raw, r.apply_url "
        "FROM job_board_ref r JOIN job j ON j.id = r.job_id "
        "WHERE r.apply_url IS NOT NULL AND j.job_group_id IS NOT NULL"
    ):
        gid = int(r[0])
        if gid == group_id or gid in seen or _key(r[4]) not in mine:
            continue
        if not is_posting_url(r[4]):
            continue  # a careers path the board's Apply led to names no single posting
        seen.add(gid)
        out.append(
            paste.Duplicate(
                gid,
                r[1],
                r[2] or r[3] or "employer not stated",
                paste.SAME_BOARD_ID,
                packets.live_packet_id(conn, gid),
            )
        )
    return out
