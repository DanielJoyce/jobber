"""Selector-driven adapter (specs/003-sources-and-adapters.md#the-htmlconfig-adapter).

One adapter, driven entirely by a registry row's ``config``, for boards that need no bespoke
Python: server-rendered result pages, classic ASP/WebForms lists, and JSON endpoints behind a
JS app. All network I/O goes through ``FetchContext``.

Registry shape (every key optional unless noted)::

    family: htmlconfig
    config:
      base_url: https://jobs.example.gov        # default: the entry URL's origin
      timezone: America/Detroit                 # for dates without a zone (default UTC)
      date_formats: ["%m/%d/%Y"]                # tried before the free-text parser
      search:                                   # required
        method: GET | POST
        path: /jobs/search                      # or ``url`` (absolute)
        params: {q: "{keywords}", days: "{posted_within_days}"}   # query string
        data: {...}                             # form body (POST)
        json: {...}                             # JSON body (POST); string values are templated
        days_buckets: [1, 7, 14, 30]            # round posted_within_days up to the nearest
        default_days: 30                        # used when the query has no age limit
        form:                                   # WebForms-style: GET a page, then POST its form
          path: /job-search
          selector: "form#JobSearch"
          submit: {"ctl00$Search": "Search"}
      list:                                     # required
        format: html | json                     # default html
        rows: "div.job-result"                  # html: row selector; json: dotted path to the array
        row_siblings: true                      # html: a row = the match + siblings after it
        id / title / url / posted_at / closes_at / employer / location / salary_raw /
        description / apply_url: <field spec>
      detail:                                   # optional: fetched by resolve()
        title / description / posted_at / closes_at / employer / location / salary_raw /
        apply_url: <field spec>
    pagination:
      kind: none | page | offset | next_link | postback
      max_pages: 5
      page_size: 25                             # short page => last page
      start: 1                                  # first page number (page/offset kinds)
      param: page                               # page/offset: add this key to the request
      next: "a.next::attr(href)"                # next_link
      target: "ctl00$Main$pager{page}"          # postback: __EVENTTARGET, {page} = next page
      argument: "Page${page}"                   # postback: __EVENTARGUMENT
      order: newest_first | unknown             # newest_first lets the watermark end paging

Templates in ``params``/``data``/``json``/``target``/``argument``: ``{keywords}`` (space-joined),
``{title}``, ``{query}`` (the title, else the keywords: for single-box search forms),
``{posted_within_days}``, ``{page}``, ``{offset}``.
A JSON string that is exactly one template (``"{page}"``) keeps its type only for ``{page}`` and
``{offset}`` (ints); everything else is text.

Field spec (HTML): ``"h3 a::text"``, ``"h3 a::attr(href)"``, ``"div.d::html"``, ``"span::own"``
(direct text only; default suffix is ``::text``; ``description`` defaults to ``::html``), or a dict
with any of

- ``sel``      selector with an optional suffix, as above
- ``label``    find a ``td``/``th``/``dt`` whose text starts with this and return its next sibling
- ``from``     start from another already-extracted field instead of the document
- ``re``       regex; the first group (else the whole match) is the value
- ``template`` ``str.format`` over the extracted fields (and, for JSON, the row's own keys)
- ``const``    a literal

Field spec (JSON): a dotted path (``"company.name"``), or a dict with ``path`` plus the same
``from``/``re``/``template``/``const`` keys.

A JSON body that is itself a JSON string (some ASP.NET handlers double-encode) is decoded once more.

Robots, rate limits, cookies (WebForms sessions) and caching are ``FetchContext``'s business.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode, urljoin, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from selectolax.lexbor import LexborHTMLParser, LexborNode

from jobhunter.core.models import JobDetail, JobLocation, JobStub, Query, SourceRow
from jobhunter.core.textnorm import parse_date

if TYPE_CHECKING:
    from jobhunter.core.fetch import FetchContext

DEFAULT_MAX_PAGES = 5
FIELDS = (
    "title",
    "url",
    "posted_at",
    "closes_at",
    "employer",
    "location",
    "salary_raw",
    "description",
    "apply_url",
)
_WS = re.compile(r"\s+")
_SUFFIX = re.compile(r"^(?P<sel>.*?)(?:::(?P<kind>text|own|html|attr\((?P<attr>[^)]+)\)))?$", re.S)
_CITY_ST = re.compile(r"^\s*(?P<city>[^,0-9]+?)\s*,\s*(?P<st>[A-Za-z]{2})\b")
_DO_POSTBACK = re.compile(r"__doPostBack\(\s*'([^']*)'\s*,\s*'([^']*)'")


class ConfigError(ValueError):
    """The registry row's htmlconfig block is missing something the adapter needs."""


# ─── small helpers ──────────────────────────────────────────────────────────


def _clean(text: str | None) -> str | None:
    if text is None:
        return None
    out = _WS.sub(" ", text.replace("\xa0", " ")).strip()
    return out or None


def _walk(obj: Any, path: str) -> Any:
    """Dotted-path lookup into JSON (``a.b.0.c``); None when any hop is missing."""
    cur = obj
    for part in path.split(".") if path else []:
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return None
        if cur is None:
            return None
    return cur


class _Ctx(dict):
    """str.format mapping that renders a missing key as an empty string."""

    def __missing__(self, key: str) -> str:
        return ""


def query_vars(query: Query, cfg: dict[str, Any], page: int, offset: int) -> dict[str, Any]:
    keywords = " ".join(k.strip() for k in (query.keywords or []) if k.strip())
    title = (query.title or "").strip()
    days = query.posted_within_days
    if days is None:
        days = cfg.get("default_days")
    buckets = sorted(cfg.get("days_buckets") or [])
    if days is not None and buckets:
        days = next((b for b in buckets if b >= days), buckets[-1])
    return {
        "keywords": keywords,
        "title": title,
        "query": title or keywords,
        "posted_within_days": "" if days is None else days,
        "page": page,
        "offset": offset,
    }


def render(value: Any, variables: dict[str, Any]) -> Any:
    """Expand ``{name}`` templates in strings, recursively through dicts and lists."""
    if isinstance(value, str):
        whole = re.fullmatch(r"\{(page|offset)\}", value)
        if whole:
            return variables[whole.group(1)]
        return value.format_map(_Ctx(variables))
    if isinstance(value, dict):
        return {k: render(v, variables) for k, v in value.items()}
    if isinstance(value, list):
        return [render(v, variables) for v in value]
    return value


def _drop_empty(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v not in ("", None)}


# ─── field extraction ───────────────────────────────────────────────────────


def _split_sel(spec: str, default_kind: str) -> tuple[str, str, str | None]:
    m = _SUFFIX.match(spec.strip())
    assert m is not None  # every string matches: both groups are optional
    kind = m.group("kind") or default_kind
    attr = m.group("attr")
    if attr:
        kind = "attr"
    return m.group("sel").strip(), kind, attr


def _node_value(node: LexborNode, kind: str, attr: str | None) -> str | None:
    if kind == "attr":
        return _clean(node.attributes.get(attr or "") or None)
    if kind == "html":
        return (node.html or "").strip() or None
    if kind == "own":  # direct text only: skips child elements such as a <b> label
        return _clean(
            " ".join(c.text() or "" for c in node.iter(include_text=True) if c.tag == "-text")
        )
    return _clean(node.text(separator=" "))


def _label_value(root: LexborNode | LexborHTMLParser, label: str, scope: str) -> str | None:
    want = _clean(label)
    if want is None:
        return None
    want = want.lower().rstrip(":")
    for node in root.css(scope):
        text = (_clean(node.text(separator=" ")) or "").lower().rstrip(":")
        if text == want or text.startswith(want):
            sib = node.next
            while sib is not None and sib.tag in ("-text", "-comment"):
                sib = sib.next
            if sib is not None:
                return _clean(sib.text(separator=" "))
    return None


def _post(spec: dict[str, Any], value: str | None, fields: dict[str, Any]) -> str | None:
    has_source = any(k in spec for k in ("sel", "path", "label", "from"))
    if "re" in spec and value is not None:
        m = re.search(spec["re"], value, re.S)
        value = (m.group(1) if m.groups() else m.group(0)) if m else None
    if "template" in spec and (value is not None or not has_source):
        value = _clean(str(spec["template"]).format_map(_Ctx({**fields, "value": value or ""})))
    return value or None


def extract_html(
    root: LexborNode | LexborHTMLParser,
    spec: Any,
    fields: dict[str, Any],
    *,
    default_kind: str = "text",
) -> str | None:
    """One field from an HTML subtree. ``fields`` holds values extracted earlier in the row."""
    if spec is None:
        return None
    if isinstance(spec, str):
        spec = {"sel": spec}
    if "const" in spec:
        return str(spec["const"])
    value: str | None = None
    if "from" in spec:
        raw = fields.get(spec["from"])
        value = None if raw is None else str(raw)
    elif "label" in spec:
        value = _label_value(root, spec["label"], spec.get("scope", "td, th, dt"))
    elif "sel" in spec:
        sel, kind, attr = _split_sel(spec["sel"], spec.get("kind", default_kind))
        node = root.css_first(sel) if sel else (root if isinstance(root, LexborNode) else None)
        value = _node_value(node, kind, attr) if node is not None else None
    return _post(spec, value, fields)


def extract_json(row: Any, spec: Any, fields: dict[str, Any]) -> str | None:
    if spec is None:
        return None
    if isinstance(spec, str):
        spec = {"path": spec}
    if "const" in spec:
        return str(spec["const"])
    value: Any = None
    if "from" in spec:
        value = fields.get(spec["from"])
    elif "path" in spec:
        value = _walk(row, spec["path"])
    if isinstance(value, bool):
        value = str(value).lower()
    elif isinstance(value, (dict, list)):
        value = json.dumps(value)
    value = None if value is None else str(value)
    ctx = {**(row if isinstance(row, dict) else {}), **fields}
    return _post(spec, value, ctx)


# ─── dates ──────────────────────────────────────────────────────────────────


def _zone(name: str) -> ZoneInfo | Any:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return UTC


def parse_when(raw: str | None, cfg: dict[str, Any]) -> datetime | None:
    """Date text -> aware UTC datetime. ``date_formats`` first, ISO next, free text last."""
    if not raw:
        return None
    raw = raw.strip()
    tz = str(cfg.get("timezone", "UTC"))
    for fmt in cfg.get("date_formats") or []:
        try:
            dt = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        return dt.replace(tzinfo=_zone(tz)).astimezone(UTC) if dt.tzinfo is None else dt
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        parsed = parse_date(raw, tz)
        return parsed.astimezone(UTC) if parsed else None
    return dt.replace(tzinfo=_zone(tz)).astimezone(UTC) if dt.tzinfo is None else dt.astimezone(UTC)


# ─── locations ──────────────────────────────────────────────────────────────


def locations_for(src: SourceRow, location_raw: str | None) -> list[JobLocation]:
    """``City, ST`` when the text says so, else the row's own state; empty if neither."""
    if location_raw:
        m = _CITY_ST.match(location_raw)
        if m:
            return [JobLocation(state=m["st"].upper(), city=m["city"].strip(), is_primary=True)]
    if src.state:
        city = location_raw.split(",")[0].strip() if location_raw else None
        return [JobLocation(state=src.state, city=city or None, is_primary=True)]
    return []


# ─── adapter ────────────────────────────────────────────────────────────────


def _cfg(src: SourceRow, key: str, *, required: bool = False) -> dict[str, Any]:
    block = src.config.get(key)
    if block is None:
        if required:
            raise ConfigError(f"{src.key}: htmlconfig needs config.{key}")
        return {}
    if not isinstance(block, dict):
        raise ConfigError(f"{src.key}: config.{key} must be a mapping")
    return block


def _base(src: SourceRow) -> str:
    base = src.config.get("base_url")
    if base:
        return str(base)
    parts = urlsplit(src.entry)
    return f"{parts.scheme}://{parts.netloc}"


def _absolute(src: SourceRow, spec: dict[str, Any], key: str = "path") -> str:
    if spec.get("url"):
        return str(spec["url"])
    path = spec.get(key)
    if not path:
        raise ConfigError(f"{src.key}: config.search needs `url` or `path`")
    return urljoin(_base(src) + "/", str(path))


def form_fields(form: LexborNode) -> dict[str, str]:
    """Names and current values of a form's text-like fields (hidden state, selects, inputs).

    Checkboxes, radios and buttons are left out: submitting them is the caller's choice.
    """
    out: dict[str, str] = {}
    for el in form.css("input, select, textarea"):
        name = el.attributes.get("name")
        if not name:
            continue
        kind = (el.attributes.get("type") or "").lower()
        if kind in ("submit", "button", "image", "checkbox", "radio", "file", "reset"):
            continue
        if el.tag == "select":
            chosen = el.css_first("option[selected]") or el.css_first("option")
            out[name] = (chosen.attributes.get("value") or "") if chosen else ""
        elif el.tag == "textarea":
            out[name] = el.text() or ""
        else:
            out[name] = el.attributes.get("value") or ""
    return out


def _with_siblings(node: LexborNode, starts: set[int]) -> LexborNode:
    """A fragment holding ``node`` and the siblings after it, up to the next row start.

    For result lists that are flat (title div, detail div, snippet p, repeat) rather than one
    wrapper per job. Field selectors then run against the fragment.
    """
    parts = [node.html or ""]
    sib = node.next
    while sib is not None and sib.mem_id not in starts:
        parts.append(sib.html or "")
        sib = sib.next
    frag = LexborHTMLParser("<div>" + "".join(parts) + "</div>")
    return frag.css_first("div")  # type: ignore[return-value]


class HtmlConfigAdapter:
    family = "htmlconfig"

    # ── search ──────────────────────────────────────────────────────────────

    def search(
        self, src: SourceRow, query: Query, since: datetime, ctx: FetchContext
    ) -> Iterator[JobStub]:
        if since.tzinfo is None:
            since = since.replace(tzinfo=UTC)
        search_cfg = _cfg(src, "search", required=True)
        list_cfg = _cfg(src, "list", required=True)
        pag = src.pagination or {}
        kind = str(pag.get("kind", "none"))
        max_pages = int(pag.get("max_pages", DEFAULT_MAX_PAGES))
        page_size = int(pag.get("page_size", 0))
        start = int(pag.get("start", 1))
        newest_first = pag.get("order") == "newest_first"
        is_json = list_cfg.get("format", "html") == "json"

        seen: set[str] = set()
        resp: Any = None
        for n in range(max_pages):
            page = start + n
            variables = query_vars(query, search_cfg, page, n * page_size)
            if n == 0 or kind in ("page", "offset"):
                resp = self._first_request(src, search_cfg, variables, kind, pag, ctx)
            elif kind == "next_link":
                nxt = self._next_link(resp, pag)
                if not nxt:
                    return
                resp = ctx.get(nxt)
            elif kind == "postback":
                resp = self._postback(resp, pag, variables, ctx)
                if resp is None:
                    return
            else:
                return

            rows = self._rows(resp, list_cfg, is_json)
            fresh = total = 0
            for fields, raw in rows:
                stub = self._stub(src, list_cfg, search_cfg, fields, raw, resp)
                if stub is None or stub.external_id in seen:
                    continue
                seen.add(stub.external_id)
                total += 1
                if stub.posted_at is not None and stub.posted_at < since:
                    continue
                fresh += 1
                yield stub
            if total == 0 and not rows:
                return
            if page_size and len(rows) < page_size:
                return
            if newest_first and total and fresh == 0:
                return

    def _first_request(
        self,
        src: SourceRow,
        cfg: dict[str, Any],
        variables: dict[str, Any],
        kind: str,
        pag: dict[str, Any],
        ctx: FetchContext,
    ) -> Any:
        method = str(cfg.get("method", "GET")).upper()
        params = _drop_empty(render(cfg.get("params") or {}, variables))
        data = render(cfg.get("data") or {}, variables)
        body = render(cfg.get("json"), variables) if cfg.get("json") is not None else None
        headers = render(cfg.get("headers") or {}, variables) or None
        param = pag.get("param")
        if kind in ("page", "offset") and param:
            value = variables["page"] if kind == "page" else variables["offset"]
            if method == "GET":
                params[param] = value
            elif body is not None:
                body[param] = value
            else:
                data[param] = value
        form = cfg.get("form")
        if form:
            return self._form_search(src, cfg, form, data, ctx, headers)
        url = _absolute(src, cfg)
        if method == "GET":
            return ctx.get(url, params=params or None, headers=headers)
        if params:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}{urlencode(params)}"
        return ctx.post(url, data=data or None, json=body, headers=headers)

    def _form_search(
        self,
        src: SourceRow,
        cfg: dict[str, Any],
        form_cfg: dict[str, Any],
        data: dict[str, Any],
        ctx: FetchContext,
        headers: dict[str, str] | None,
    ) -> Any:
        page_url = _absolute(src, form_cfg)
        page = ctx.get(page_url, headers=headers)
        tree = LexborHTMLParser(page.text)
        form = tree.css_first(str(form_cfg.get("selector", "form")))
        if form is None:
            raise ConfigError(f"{src.key}: search form {form_cfg.get('selector')!r} not found")
        body: dict[str, Any] = form_fields(form)
        body.update(form_cfg.get("submit") or {})
        body.update(data)
        action = urljoin(page.final_url, form.attributes.get("action") or page.final_url)
        return ctx.post(action, data=body, headers=headers)

    @staticmethod
    def _next_link(resp: Any, pag: dict[str, Any]) -> str | None:
        sel = pag.get("next")
        if not sel:
            return None
        tree = LexborHTMLParser(resp.text)
        href = extract_html(tree, sel, {})
        if not href or href.startswith(("#", "javascript:")):
            return None
        return urljoin(resp.final_url, href)

    @staticmethod
    def _postback(
        resp: Any, pag: dict[str, Any], variables: dict[str, Any], ctx: FetchContext
    ) -> Any:
        """ASP.NET ``__doPostBack`` paging: re-post the result form with the pager's target."""
        tree = LexborHTMLParser(resp.text)
        form = tree.css_first(str(pag.get("form", "form[method=post], form")))
        if form is None:
            return None
        target = render(str(pag["target"]), variables)
        # Only follow a pager link the page really offers (the last page has none).
        argument = render(str(pag.get("argument", "")), variables)
        offered = {
            (m.group(1), m.group(2))
            for a in tree.css("a[href]")
            if (m := _DO_POSTBACK.search(a.attributes.get("href") or ""))
        }
        if (target, argument) not in offered:
            return None
        body: dict[str, Any] = form_fields(form)
        body["__EVENTTARGET"] = target
        body["__EVENTARGUMENT"] = argument
        action = urljoin(resp.final_url, form.attributes.get("action") or resp.final_url)
        return ctx.post(action, data=body)

    # ── parsing ─────────────────────────────────────────────────────────────

    @staticmethod
    def _rows(resp: Any, cfg: dict[str, Any], is_json: bool) -> list[tuple[dict[str, Any], Any]]:
        """(extracted fields, raw row) pairs, in page order."""
        out: list[tuple[dict[str, Any], Any]] = []
        specs = {k: cfg.get(k) for k in (*FIELDS, "id") if cfg.get(k) is not None}
        if is_json:
            payload = resp.json()
            if isinstance(payload, str):  # a JSON document returned as a JSON string (HireClick)
                payload = json.loads(payload)
            items = _walk(payload, str(cfg.get("rows", ""))) if cfg.get("rows") else payload
            for item in items if isinstance(items, list) else []:
                fields: dict[str, Any] = {}
                for name, spec in specs.items():
                    fields[name] = extract_json(item, spec, fields)
                out.append((fields, item))
            return out
        tree = LexborHTMLParser(resp.text)
        rows_sel = cfg.get("rows")
        if not rows_sel:
            raise ConfigError("config.list.rows is required for html lists")
        matches = tree.css(str(rows_sel))
        for node in matches:
            if cfg.get("row_siblings"):
                node = _with_siblings(node, {m.mem_id for m in matches})
            fields = {}
            for name, spec in specs.items():
                fields[name] = extract_html(
                    node, spec, fields, default_kind="html" if name == "description" else "text"
                )
            out.append((fields, node))
        return out

    def _stub(
        self,
        src: SourceRow,
        list_cfg: dict[str, Any],
        search_cfg: dict[str, Any],
        fields: dict[str, Any],
        raw: Any,
        resp: Any,
    ) -> JobStub | None:
        title = fields.get("title")
        url = fields.get("url")
        if url:
            url = urljoin(resp.final_url, url)
        external = fields.get("id") or url
        if not (title and url and external):
            return None  # not a job row (header, ad, empty slot)
        cfg = src.config
        description = fields.get("description")
        apply_url = fields.get("apply_url")
        return JobStub(
            source_key=src.key,
            external_id=str(external),
            title=title,
            url=url,
            apply_url=urljoin(resp.final_url, apply_url) if apply_url else None,
            agency_raw=fields.get("employer"),
            posted_at=parse_when(fields.get("posted_at"), cfg),
            closes_at=parse_when(fields.get("closes_at"), cfg),
            location_raw=fields.get("location"),
            salary_raw=fields.get("salary_raw"),
            description_raw=description,
            needs_resolve=description is None and bool(src.config.get("detail")),
            locations=locations_for(src, fields.get("location")),
            extra={"description_format": "html" if description else None},
        )

    # ── resolve ─────────────────────────────────────────────────────────────

    def resolve(self, stub: JobStub, ctx: FetchContext) -> JobDetail:
        src = ctx.source
        detail_cfg = _cfg(src, "detail")
        cfg = src.config
        resp = ctx.get(stub.url)
        fields: dict[str, Any] = {}
        if detail_cfg.get("format", "html") == "json":
            payload = resp.json()
            for name in FIELDS:
                if detail_cfg.get(name) is not None:
                    fields[name] = extract_json(payload, detail_cfg[name], fields)
        else:
            tree = LexborHTMLParser(resp.text)
            for name in FIELDS:
                if detail_cfg.get(name) is not None:
                    fields[name] = extract_html(
                        tree,
                        detail_cfg[name],
                        fields,
                        default_kind="html" if name == "description" else "text",
                    )
        data = stub.model_dump()
        description = fields.get("description")
        apply_url = fields.get("apply_url")
        location_raw = fields.get("location") or stub.location_raw
        data.update(
            title=fields.get("title") or stub.title,
            description_raw=description or stub.description_raw,
            apply_url=urljoin(resp.final_url, apply_url) if apply_url else stub.apply_url,
            agency_raw=fields.get("employer") or stub.agency_raw,
            location_raw=location_raw,
            salary_raw=fields.get("salary_raw") or stub.salary_raw,
            posted_at=parse_when(fields.get("posted_at"), cfg) or stub.posted_at,
            closes_at=parse_when(fields.get("closes_at"), cfg) or stub.closes_at,
            needs_resolve=False,
            locations=locations_for(src, location_raw) or stub.locations,
        )
        data["extra"] = {
            **stub.extra,
            "description_format": "html" if (description or stub.description_raw) else None,
        }
        return JobDetail(**data)


__all__ = ["ConfigError", "HtmlConfigAdapter", "query_vars", "render"]
