"""NLx / National Labor Exchange adapter (specs/003-sources-and-adapters.md#3-nlx).

``usnlx.com`` and the state sites (``kyjobs.usnlx.com``, ``montana.usnlx.com``,
``myjobsny.usnlx.com``) are one DirectEmployers Nuxt app. The HTML pages are client-rendered
shells; the data comes from two JSON endpoints the page calls (found 2026-10-09):

- search: ``GET https://prod-search-api.jobsyn.org/api/v1/solr/search`` with ``q``,
  ``location`` (a place slug, e.g. ``kentucky``), ``sort=date``, ``num_items``, ``offset``,
  ``page`` and an ``X-Origin`` header naming the site. The site decides the page size (15 on
  usnlx.com, 10 on montana.usnlx.com) and, for state sites, a regional scope that spills into
  neighbouring states, so state rows also send their own state as ``location``. Each listing
  carries the full description as Markdown.
- detail: ``GET https://microsites.dejobs.org/ALL_JOBS/<GUID>.json``: the same posting with
  ``html_description`` and the ``link`` apply redirector.

The human-facing detail URL is ``https://<site>/<location-slug>/<title_slug>/<GUID>/job/``.
Robots: every NLx site serves ``Disallow: /*feed/`` and ``/*feeds/`` and allows the rest; both
JSON hosts have no robots.txt (404, so allowed). This adapter never builds a feed URL.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Iterator
from datetime import UTC, datetime
from html import escape
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from jobhunter.core.geo import by_usps
from jobhunter.core.models import JobDetail, JobLocation, JobStub, Query, SourceRow

if TYPE_CHECKING:
    from jobhunter.core.fetch import FetchContext

SEARCH_API = "https://prod-search-api.jobsyn.org/api/v1/solr/search"
DETAIL_API = "https://microsites.dejobs.org/ALL_JOBS/{guid}.json"
APPLY_REDIRECTOR = "https://nlx.jobsyn.org/{guid}1"  # what the site's Apply button opens
DEFAULT_PAGE_SIZE = 15
DEFAULT_MAX_PAGES = 20


# ─── query building ─────────────────────────────────────────────────────────


def slugify(text: str) -> str:
    """The site's slug rule: strip diacritics/quotes, keep word runs, lowercase, join with '-'."""
    text = unicodedata.normalize("NFD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub('["\u2019+:/]', "", text)
    return "-".join(w.lower() for w in re.findall(r"\w+", text))


def _phrase(term: str) -> str:
    term = term.strip().replace('"', "")
    return f'"{term}"' if " " in term else term


def query_string(query: Query) -> str:
    """Query -> the ``q`` param. Multi-word terms become phrases.

    The search backend treats bare words loosely (``software engineer`` matches maintenance
    technicians), so a title or multi-word keyword is sent quoted.
    """
    parts: list[str] = []
    if query.title:
        parts.append(_phrase(query.title))
    for kw in query.keywords or []:
        if kw.strip():
            parts.append(_phrase(kw))
    return " ".join(parts)


def site_origin(src: SourceRow) -> str:
    """The X-Origin host: ``config.x_origin`` or the entry URL's host."""
    origin = src.config.get("x_origin")
    return str(origin) if origin else (urlsplit(src.entry).hostname or "usnlx.com")


def location_filter(src: SourceRow) -> str | None:
    """``config.location`` wins; a state row filters to its state's slug; national rows none."""
    if "location" in src.config:
        return src.config["location"] or None
    if src.state:
        st = by_usps(src.state)
        return slugify(st.name) if st else None
    return None


def build_params(src: SourceRow, query: Query, offset: int, page_size: int) -> dict[str, Any]:
    params: dict[str, Any] = {"q": query_string(query)}
    loc = location_filter(src)
    if loc:
        params["location"] = loc
    params["sort"] = "date"
    params["num_items"] = page_size
    params["offset"] = offset
    params["page"] = offset // page_size + 1 if page_size else 1
    return params


# ─── parsing ────────────────────────────────────────────────────────────────


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def _other(item: dict[str, Any]) -> dict[str, Any]:
    raw = item.get("other")
    if isinstance(raw, dict):
        return raw
    try:
        val = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    return val if isinstance(val, dict) else {}


def _http(url: Any) -> str | None:
    if isinstance(url, str) and urlsplit(url).scheme in ("http", "https") and urlsplit(url).netloc:
        return url
    return None


def apply_link(item: dict[str, Any]) -> str | None:
    """The employer's own application link when the posting carries one, else the redirector."""
    direct = _http(_other(item).get("application_link"))
    if direct:
        return direct
    link = _http(item.get("link"))
    if link:
        return link
    guid = item.get("guid")
    return APPLY_REDIRECTOR.format(guid=guid) if guid else None


_MD_HEADING = re.compile(r"^#{1,6}\s*(.*)$")
_MD_BULLET = re.compile(r"^\s*[-*+]\s+(.*)$")
_MD_STRONG = re.compile(r"\*\*(.+?)\*\*|__(.+?)__")


def _inline(text: str) -> str:
    return _MD_STRONG.sub(lambda m: m.group(1) or m.group(2), text).strip()


def markdown_to_html(md: str) -> str:
    """Just enough Markdown -> HTML for the listing description (headings, bullets, paragraphs).

    Normalize runs html_to_text afterwards, so fidelity beyond block structure is not needed.
    """
    out: list[str] = []
    para: list[str] = []
    items: list[str] = []

    def flush() -> None:
        if para:
            out.append("<p>" + escape(" ".join(para)) + "</p>")
            para.clear()
        if items:
            out.append("<ul>" + "".join(f"<li>{escape(i)}</li>" for i in items) + "</ul>")
            items.clear()

    for raw in md.splitlines():
        line = raw.rstrip()
        if not line.strip():
            flush()
            continue
        if m := _MD_HEADING.match(line):
            flush()
            out.append(f"<h3>{escape(_inline(m.group(1)))}</h3>")
        elif m := _MD_BULLET.match(line):
            if para:
                flush()
            items.append(_inline(m.group(1)))
        else:
            if items:
                flush()
            para.append(_inline(line))
    flush()
    return "".join(out)


def detail_url(site: str, item: dict[str, Any]) -> str:
    guid = item["guid"]
    loc = slugify(item.get("location_exact") or item.get("location") or "") or "none"
    title = item.get("title_slug") or slugify(item.get("title_exact") or "") or "job"
    return f"https://{site}/{loc}/{title}/{guid}/job/"


def _location(item: dict[str, Any]) -> list[JobLocation]:
    state = item.get("state_short_exact") or item.get("state_short")
    city = item.get("city_exact") or item.get("city")
    if not state and not city:
        return []
    lat = lon = None
    geo = item.get("GeoLocation")
    if isinstance(geo, str) and "," in geo:
        try:
            lat, lon = (float(x) for x in geo.split(",", 1))
        except ValueError:
            lat = lon = None
    return [JobLocation(state=state or None, city=city or None, lat=lat, lon=lon, is_primary=True)]


def stub_from_item(src: SourceRow, site: str, item: dict[str, Any]) -> JobStub:
    guid = str(item["guid"]).upper()
    desc = item.get("description") or ""
    return JobStub(
        source_key=src.key,
        external_id=guid,
        title=item.get("title_exact") or item.get("title") or "",
        url=detail_url(site, {**item, "guid": guid}),
        apply_url=apply_link({**item, "guid": guid}),
        agency_raw=item.get("company_exact") or item.get("company"),
        posted_at=_parse_dt(item.get("date_new")),
        location_raw=item.get("location_exact") or item.get("location"),
        description_raw=markdown_to_html(desc) if desc.strip() else None,
        needs_resolve=not desc.strip(),
        locations=_location(item),
        extra={
            "guid": guid,
            "reqid": item.get("reqid"),
            "onet": item.get("onet_exact") or [],
            "buid": item.get("buid"),
            "description_format": "markdown" if desc.strip() else None,
        },
    )


# ─── adapter ────────────────────────────────────────────────────────────────


class NlxAdapter:
    family = "nlx"

    def search(
        self, src: SourceRow, query: Query, since: datetime, ctx: FetchContext
    ) -> Iterator[JobStub]:
        if since.tzinfo is None:
            since = since.replace(tzinfo=UTC)
        site = site_origin(src)
        endpoint = str(src.config.get("search_api", SEARCH_API))
        headers = {"Accept": "application/json", "X-Origin": site}
        page_size = int(src.config.get("page_size", DEFAULT_PAGE_SIZE))
        max_pages = int(src.pagination.get("max_pages", DEFAULT_MAX_PAGES))
        offset, pages = 0, 0
        while pages < max_pages:
            body = ctx.get(
                endpoint, params=build_params(src, query, offset, page_size), headers=headers
            ).json()
            pages += 1
            jobs = body.get("jobs") or []
            # Results are sorted by a per-day "salted" date, so order within a day is not
            # strict: skip stale rows, and stop once a whole page predates the watermark.
            fresh = 0
            for item in jobs:
                if not item.get("guid"):
                    continue
                stub = stub_from_item(src, site, item)
                if stub.posted_at is not None and stub.posted_at < since:
                    continue
                fresh += 1
                yield stub
            pagination = body.get("pagination") or {}
            if not jobs or fresh == 0 or not pagination.get("has_more_pages"):
                return
            # The site, not the request, picks the page size (15 national, 10 on Montana).
            offset += int(pagination.get("page_size") or len(jobs))

    def resolve(self, stub: JobStub, ctx: FetchContext) -> JobDetail:
        guid = stub.extra.get("guid") or stub.external_id
        item = ctx.get(DETAIL_API.format(guid=guid), headers={"Accept": "application/json"}).json()
        html = item.get("html_description") or None
        desc = item.get("description") or ""
        if html is None and desc.strip():
            html = markdown_to_html(desc)
        data = stub.model_dump()
        data.update(
            description_raw=html or stub.description_raw,
            apply_url=apply_link(item) or stub.apply_url,
            needs_resolve=False,
            title=item.get("title_exact") or item.get("title") or stub.title,
            agency_raw=item.get("company_exact") or item.get("company") or stub.agency_raw,
            location_raw=item.get("location_exact") or item.get("location") or stub.location_raw,
            posted_at=_parse_dt(item.get("date_new")) or stub.posted_at,
            # NLx postings carry no closing date; an expired one has ``deleted_at``.
            closes_at=_parse_dt(item.get("deleted_at")) or stub.closes_at,
            locations=_location(item) or stub.locations,
        )
        data["extra"] = {**stub.extra, "description_format": "html" if html else None}
        return JobDetail(**data)
