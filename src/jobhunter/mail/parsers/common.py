"""Shared, tolerant extraction helpers for job-alert emails.

Alert layouts differ per board and change without notice, so parsers lean on two stable
facts: each job is a link whose URL looks like a posting, and the job's other fields sit in
the same visual block (table row, card, list item) as that link. ``entries_from_links`` finds
the block around each posting link and reads labelled fields ("Employer: ...") first, then
falls back to position (the first plain line is usually the employer, a "City, ST" line the
location, a long line the snippet).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from html import unescape

from selectolax.lexbor import LexborHTMLParser as HTMLParser
from selectolax.lexbor import LexborNode as Node

from jobhunter.core.geo import parse_city_state
from jobhunter.pipeline.ats_rules import is_http_url, unwrap


@dataclass
class AlertEntry:
    """One job in an alert email."""

    title: str
    url: str
    employer: str | None = None
    location_raw: str | None = None
    snippet: str | None = None
    salary_raw: str | None = None
    posted_raw: str | None = None
    closes_raw: str | None = None


LinkTest = Callable[[str], bool]

_WS = re.compile(r"\s+")
_BLOCK_TAGS = ("tr", "li", "div", "td", "table", "section", "article", "p")
_MAX_CLIMB = 6
_SNIPPET_MIN = 50
_TITLE_MAX = 160

_LABELS: dict[str, tuple[str, ...]] = {
    "employer": ("employer", "company", "agency", "organization", "department", "hiring agency"),
    "location": ("location", "city", "job location", "work location"),
    "salary": ("salary", "pay", "wage", "wages", "compensation", "pay rate"),
    "posted": ("posted", "date posted", "posted on", "open date", "opening date"),
    "closes": ("closing", "closes", "closing date", "close date", "deadline", "apply by"),
    "snippet": ("description", "summary", "job description", "duties"),
}
_LABEL_RE = re.compile(r"^\s*([A-Za-z][A-Za-z /]{1,24}?)\s*[:\-\u2013]\s*(.+)$")
_SKIP_LINE = re.compile(
    r"^(view( this)? job|view details|apply( now)?|more info|details|read more|new!?|"
    r"see more|learn more|job (id|order|number)\s*[:#]?.*|#\s*\d+)$",
    re.I,
)
_NOT_TITLES = re.compile(
    r"unsubscribe|manage (your )?(job )?(alerts?|subscriptions|preferences|virtual recruiter)|"
    r"email preferences|privacy policy|view all|see all|browse all|edit (this )?(search|alert)|"
    r"view in (a )?browser|^(help|contact( us)?|privacy|log ?in|sign ?in|faq)$",
    re.I,
)
_FIELD_SEP = re.compile(r"\s+[\u00b7|\u2022]\s+")


def clean(text: str | None) -> str:
    return _WS.sub(" ", unescape(text or "")).strip()


def unwrap_url(url: str) -> str:
    """Strip known redirect wrappers (ats_rules.unwrap) a few levels deep."""
    url = url.strip()
    for _ in range(3):
        inner = unwrap(url)
        if inner is None:
            break
        url = inner
    return url


def split_fields(lines: list[str]) -> list[str]:
    """``Employer · City, ST`` on one line becomes two lines (middot, bullet or pipe)."""
    out: list[str] = []
    for line in lines:
        out += [c for c in (clean(x) for x in _FIELD_SEP.split(line)) if c]
    return out


def _lines(node: Node) -> list[str]:
    raw = node.text(separator="\n", deep=True) or ""
    return split_fields([c for c in (clean(x) for x in raw.split("\n")) if c])


def _count_job_links(node: Node, test: LinkTest) -> int:
    """Distinct posting URLs under ``node`` (a title link plus a "View job" button is one)."""
    urls = {unwrap_url(a.attributes.get("href") or "") for a in node.css("a[href]")}
    return sum(1 for u in urls if is_http_url(u) and test(u))


def _block(link: Node, test: LinkTest) -> Node:
    """The largest ancestor (up to a few levels) that still holds only this one job link."""
    best = link
    node = link.parent
    climbed = 0
    while node is not None and node.tag not in ("body", "html") and climbed < _MAX_CLIMB:
        if node.tag in _BLOCK_TAGS:
            if _count_job_links(node, test) > 1:
                break
            best = node
        node = node.parent
        climbed += 1
    return best


def _label_kind(label: str) -> str | None:
    key = clean(label).lower().rstrip(".")
    for kind, names in _LABELS.items():
        if key in names:
            return kind
    return None


def looks_like_location(line: str) -> bool:
    if parse_city_state(line)[1] is not None:
        return True
    return bool(re.search(r"\b(remote|statewide|multiple locations|telework)\b", line, re.I))


def fill_fields(entry: AlertEntry, lines: list[str]) -> AlertEntry:
    """Labelled fields first; then position: employer, location, then the longest line."""
    rest: list[str] = []
    title_key = entry.title.lower()
    for line in lines:
        if line.lower() == title_key or _SKIP_LINE.match(line) or _NOT_TITLES.search(line):
            continue
        m = _LABEL_RE.match(line)
        kind = _label_kind(m.group(1)) if m else None
        if m and kind:
            value = clean(m.group(2))
            if kind == "employer" and not entry.employer:
                entry.employer = value
            elif kind == "location" and not entry.location_raw:
                entry.location_raw = value
            elif kind == "salary" and not entry.salary_raw:
                entry.salary_raw = value
            elif kind == "posted" and not entry.posted_raw:
                entry.posted_raw = value
            elif kind == "closes" and not entry.closes_raw:
                entry.closes_raw = value
            elif kind == "snippet" and not entry.snippet:
                entry.snippet = value
            continue
        rest.append(line)
    for line in list(rest):
        if not entry.location_raw and looks_like_location(line) and len(line) < 80:
            entry.location_raw = line
            rest.remove(line)
    if not entry.employer:
        for line in rest:
            if len(line) < 100 and not re.search(r"\$\s*\d", line):
                entry.employer = line
                rest.remove(line)
                break
    if not entry.location_raw and rest and len(rest[0]) < 50 and not re.search(r"\d", rest[0]):
        entry.location_raw = rest.pop(0)  # title, employer, then a bare city ("Pittsburgh")
    if not entry.salary_raw:
        for line in rest:
            if re.search(r"\$\s*\d", line) and len(line) < 80:
                entry.salary_raw = line
                rest.remove(line)
                break
    if not entry.snippet:
        longest = max(rest, key=len, default="")
        if len(longest) >= _SNIPPET_MIN:
            entry.snippet = longest
    return entry


def entries_from_links(html: str, test: LinkTest) -> list[AlertEntry]:
    """One entry per distinct posting link that ``test`` accepts (after unwrapping)."""
    if not html.strip():
        return []
    tree = HTMLParser(html)
    out: list[AlertEntry] = []
    seen: set[str] = set()
    for a in tree.css("a[href]"):
        url = unwrap_url(a.attributes.get("href") or "")
        if not is_http_url(url) or not test(url):
            continue
        title = clean(a.text(deep=True))
        if not title or len(title) > _TITLE_MAX or _NOT_TITLES.search(title):
            continue
        if _SKIP_LINE.match(title):
            continue  # a "View job" button: the title link for this job is elsewhere
        if url in seen:
            continue
        seen.add(url)
        entry = AlertEntry(title=title, url=url)
        out.append(fill_fields(entry, _lines(_block(a, test))))
    return out


_URL_IN_TEXT = re.compile(r"https?://[^\s<>\"')\]]+")


def entries_from_text(text: str, test: LinkTest) -> list[AlertEntry]:
    """Plain-text alerts: the line(s) before each posting URL hold title, employer, location."""
    lines = [clean(x) for x in text.splitlines()]
    out: list[AlertEntry] = []
    seen: set[str] = set()
    block: list[str] = []
    for line in lines:
        if not line:
            block = []
            continue
        m = _URL_IN_TEXT.search(line)
        if not m:
            block.append(line)
            continue
        url = unwrap_url(m.group(0).rstrip(".,;"))
        before = clean(line[: m.start()])
        if before:
            block.append(before.rstrip(":"))
        if test(url) and url not in seen and block:
            seen.add(url)
            title, *fields = split_fields([b for b in block if not _SKIP_LINE.match(b)]) or [""]
            if title and not _NOT_TITLES.search(title):
                out.append(fill_fields(AlertEntry(title=title, url=url), fields))
        block = []
    return out


def entries(html: str, text: str, test: LinkTest) -> list[AlertEntry]:
    """HTML first; plain text when the email has no usable HTML part."""
    found = entries_from_links(html, test) if html else []
    return found or entries_from_text(text, test)
