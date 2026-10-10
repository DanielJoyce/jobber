"""Apply-link resolver (specs/015-apply-links.md).

Walks from the best source-provided link to the employer's application: tracking links are
unwrapped locally (``ats_rules.unwrap``), everything else is followed one hop at a time through
``FetchContext`` (3xx ``Location``, ``<meta http-equiv=refresh>``, literal ``window.location``
assignments), so every hop gets its own robots.txt check and rate limit. The walk stops at a
known ATS, which is canonicalized to its apply URL, or at the first page that goes nowhere.

The chain stored in ``apply_link.chain`` lists the hops *after* ``start_url``; each hop's
``status`` is the HTTP status of fetching it (None when it was never fetched: unwrapped through,
robots-disallowed, or not reached).
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from html import unescape
from typing import TYPE_CHECKING, Literal
from urllib.parse import parse_qsl, urldefrag, urlencode, urljoin, urlsplit, urlunsplit

from selectolax.lexbor import LexborHTMLParser

from jobhunter.core.fetch import (
    AccessDenied,
    CachedResponse,
    FetchContext,
    FetchError,
    RobotsDisallowed,
    fetch_context_for_url,
)
from jobhunter.core.models import ApplyHop, ApplyLink, ApplyStatus
from jobhunter.core.textnorm import html_to_text
from jobhunter.pipeline.ats_rules import (
    EXPIRED_PHRASES,
    EXPIRED_STATUSES,
    AtsRule,
    host_of,
    is_http_url,
    match_ats,
    unwrap,
)
from jobhunter.pipeline.board_ids import is_board_host
from jobhunter.pipeline.dedupe import group_members
from jobhunter.pipeline.listing import _txn, from_iso, to_iso

if TYPE_CHECKING:
    from jobhunter.config import Settings

MAX_HOPS = 8
MAX_META_DELAY_S = 10
# Candidate start URLs tried per group before giving up (each one costs requests).
MAX_CANDIDATES = 3
# LinkedIn and Indeed (specs/017 phase 1e): never requested by any path, robots.txt included.
# A link that starts there stays 'unresolved' until a capture supplies the destination.
NO_FETCH_ERROR = "board host: jobhunter never requests LinkedIn or Indeed pages"

CtxFactory = Callable[[str], FetchContext]
HopMethod = Literal["unwrap", "3xx", "meta", "js", "browser"]
# Hook for JS-only redirects (specs/015 step 5): given the page URL, return where a real browser
# ends up, or None. Not wired yet; the resolver works without it.
BrowserFallback = Callable[[str], str | None]

_RANK = {
    ApplyStatus.live: 0,
    ApplyStatus.expired: 1,
    ApplyStatus.blocked: 2,
    ApplyStatus.unresolved: 3,
}


@dataclass
class Resolution:
    start_url: str
    status: ApplyStatus
    final_url: str | None
    chain: list[ApplyHop] = field(default_factory=list)
    ats: str | None = None
    error: str | None = None
    fetched: bool = False  # True when the final page was actually requested

    @property
    def employer_host(self) -> str | None:
        return host_of(self.final_url) or None if self.final_url else None


def default_ctx_factory(settings: Settings, conn: sqlite3.Connection) -> CtxFactory:
    """Ad-hoc contexts (no registry source) for apply-link hosts."""
    return lambda url: fetch_context_for_url(settings, conn, url)


# ─── URL helpers ───────────────────────────────────────────────────────────


def normalize_url(url: str) -> str:
    """Loop-detection key: lowercase scheme/host, no default port, fragment or trailing slash;
    sorted query."""
    p = urlsplit(url)
    scheme = p.scheme.lower()
    host = (p.hostname or "").lower()
    if p.port and (scheme, p.port) not in (("http", 80), ("https", 443)):
        host = f"{host}:{p.port}"
    query = urlencode(sorted(parse_qsl(p.query, keep_blank_values=True)))
    return urlunsplit((scheme, host, p.path.rstrip("/") or "/", query, ""))


_REFRESH_URL = re.compile(r"^\s*(\d+)?\s*[;,]?\s*(?:url\s*=\s*)?(['\"]?)(.+?)\2\s*$", re.I)
_JS_ASSIGN = re.compile(
    r"(?:\b(?:window|document|top|self)\.)?\blocation(?:\.href)?\s*=\s*(['\"])([^'\"]+)\1"
    r"(?=\s*(?:;|$|<))",
    re.M,
)
_JS_CALL = re.compile(
    r"(?:\b(?:window|document|top|self)\.)?\blocation(?:\.href)?\.(?:replace|assign)\(\s*"
    r"(['\"])([^'\"]+)\1\s*\)"
)


def meta_refresh_target(html: str, base: str) -> str | None:
    """``<meta http-equiv="refresh" content="0;url=...">`` with a short delay, absolutized."""
    tree = LexborHTMLParser(html)
    for node in tree.css("meta"):
        if (node.attributes.get("http-equiv") or "").strip().lower() != "refresh":
            continue
        content = node.attributes.get("content") or ""
        m = _REFRESH_URL.match(content)
        if not m or "url" not in content.lower():
            continue
        delay = int(m.group(1) or 0)
        if delay > MAX_META_DELAY_S:
            continue
        target = urljoin(base, unescape(m.group(3).strip()))
        if normalize_url(target) != normalize_url(base):
            return target
    return None


def js_redirect_target(html: str, base: str) -> str | None:
    """A literal-string ``window.location = "..."`` / ``location.replace("...")`` in an inline
    script. Concatenations and variables are ignored."""
    tree = LexborHTMLParser(html)
    for node in tree.css("script"):
        if node.attributes.get("src"):
            continue
        code = node.text(deep=True) or ""
        for rx in (_JS_ASSIGN, _JS_CALL):
            m = rx.search(code)
            if m:
                target = urljoin(base, m.group(2).replace("\\/", "/"))
                if normalize_url(target) != normalize_url(base):
                    return target
    return None


def looks_expired(html: str) -> bool:
    text = " ".join(html_to_text(html).lower().split())
    return any(phrase in text for phrase in EXPIRED_PHRASES)


def _fetchable(url: str) -> str:
    """Fragments never go on the wire (canonical Greenhouse URLs end in ``#app``)."""
    return urldefrag(url).url


def _http_target(value: str) -> bool:
    """Whether a redirect target is an http(s) URL. Malformed values are not."""
    try:
        return is_http_url(value)
    except ValueError:  # urlsplit rejects e.g. "http://["
        return False


def _is_html(resp: CachedResponse) -> bool:
    ctype = resp.headers.get("content-type", "").lower()
    return not ctype or "html" in ctype


# ─── the walk ──────────────────────────────────────────────────────────────


def _left_posting(rule: AtsRule | None, from_url: str, to_url: str) -> bool:
    """An ATS posting redirecting to a non-posting page on the same ATS (e.g. Greenhouse's
    ``/acme?error=true``) means the posting is gone."""
    return (
        rule is not None
        and rule.is_posting(from_url)
        and match_ats(to_url) is rule
        and not rule.is_posting(to_url)
    )


def resolve_url(
    start_url: str,
    ctx: FetchContext,
    *,
    max_hops: int = MAX_HOPS,
    browser_fallback: BrowserFallback | None = None,
) -> Resolution:
    """Follow one start URL to its destination. Never raises for fetch failures."""
    res = Resolution(start_url=start_url, status=ApplyStatus.unresolved, final_url=start_url)
    seen = {normalize_url(start_url)}
    url = start_url

    def step(target: str, method: HopMethod) -> bool:
        """Append a hop; False (with res filled in) if the walk must stop."""
        nonlocal url
        if not _http_target(target):
            res.error = f"non-http destination: {target[:200]}"
            res.final_url = url
            return False
        if len(res.chain) >= max_hops:
            res.error = f"more than {max_hops} hops"
            res.final_url = url
            return False
        key = normalize_url(target)
        if key in seen:
            res.error = f"redirect loop at {target}"
            res.final_url = url
            return False
        seen.add(key)
        res.chain.append(ApplyHop(url=target, host=host_of(target), method=method))
        url = target
        return True

    def set_status(status: int) -> None:
        if res.chain:
            res.chain[-1].status = status

    while True:
        while (dest := unwrap(url)) is not None:
            if not step(dest, "unwrap"):
                return res
        rule = match_ats(url)
        if rule is not None and not rule.fetch:
            res.status, res.final_url, res.ats = ApplyStatus.live, rule.canonical(url), rule.name
            return res
        if is_board_host(host_of(url)):
            res.error, res.final_url = NO_FETCH_ERROR, url
            return res

        previous = res.chain[-2].url if len(res.chain) >= 2 else start_url
        try:
            resp = ctx.get(_fetchable(url), follow_redirects=False)
        except RobotsDisallowed as exc:
            res.status = ApplyStatus.blocked
            res.final_url = previous if res.chain else start_url
            res.error = str(exc)
            return res
        except AccessDenied as exc:
            res.error, res.final_url = str(exc), url
            set_status(exc.status)
            return res
        except FetchError as exc:  # TransientFetchError and friends
            res.error, res.final_url = str(exc), url
            return res
        set_status(resp.status)
        res.final_url = url

        if resp.is_redirect:
            target = resp.location or ""
            if not _http_target(target):
                res.error = f"non-http destination: {target[:200]}"
                return res
            if _left_posting(rule, url, target):
                res.status, res.ats = ApplyStatus.expired, rule.name if rule else None
                res.fetched = True
                return res
            if not step(target, "3xx"):
                return res
            continue
        if resp.status in EXPIRED_STATUSES:
            res.status, res.fetched = ApplyStatus.expired, True
            res.ats = rule.name if rule else "unknown"
            return res
        if not 200 <= resp.status < 300:
            res.error = f"HTTP {resp.status} at {url}"
            return res

        html = resp.text if _is_html(resp) else ""
        if rule is None and html:
            nxt = meta_refresh_target(html, url)
            method: HopMethod = "meta"
            if nxt is None:
                nxt, method = js_redirect_target(html, url), "js"
            if nxt is None and browser_fallback is not None:
                nxt, method = browser_fallback(url), "browser"
            if nxt is not None:
                if not step(nxt, method):
                    return res
                continue

        res.fetched = True
        res.ats = rule.name if rule else "unknown"
        if html and looks_expired(html):
            res.status = ApplyStatus.expired
        else:
            res.status = ApplyStatus.live
            if rule is not None:
                res.final_url = rule.canonical(url)
        return res


# ─── apply_link rows ───────────────────────────────────────────────────────


def _start_urls(conn: sqlite3.Connection, group_id: int) -> list[str]:
    """Source-provided apply links first (e.g. USAJOBS ApplyURI), then posting URLs; canonical
    member first within each."""
    members = group_members(conn, group_id)
    urls: list[str] = []
    for col in ("apply_url", "url"):
        for m in members:
            u = m[col]
            if u and is_http_url(u) and u not in urls:
                urls.append(u)
    return urls[:MAX_CANDIDATES]


def _row_to_link(row: sqlite3.Row) -> ApplyLink:
    return ApplyLink(
        job_group_id=row["job_group_id"],
        start_url=row["start_url"],
        final_url=row["final_url"],
        chain=[ApplyHop.model_validate(h) for h in json.loads(row["chain"])],
        ats=row["ats"],
        employer_host=row["employer_host"],
        status=ApplyStatus(row["status"]),
        resolved_at=from_iso(row["resolved_at"]),
        verified_at=from_iso(row["verified_at"]) if row["verified_at"] else None,
        error=row["error"],
    )


def get_apply_link(conn: sqlite3.Connection, group_id: int) -> ApplyLink | None:
    row = conn.execute("SELECT * FROM apply_link WHERE job_group_id = ?", (group_id,)).fetchone()
    return _row_to_link(row) if row else None


def _save(conn: sqlite3.Connection, link: ApplyLink) -> None:
    chain = json.dumps([h.model_dump() for h in link.chain])
    with _txn(conn):
        conn.execute(
            "INSERT INTO apply_link (job_group_id, start_url, final_url, chain, ats, "
            "employer_host, status, resolved_at, verified_at, error) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(job_group_id) DO UPDATE SET start_url = excluded.start_url, "
            "final_url = excluded.final_url, chain = excluded.chain, ats = excluded.ats, "
            "employer_host = excluded.employer_host, status = excluded.status, "
            "resolved_at = excluded.resolved_at, verified_at = excluded.verified_at, "
            "error = excluded.error",
            (
                link.job_group_id,
                link.start_url,
                link.final_url,
                chain,
                link.ats,
                link.employer_host,
                link.status.value,
                to_iso(link.resolved_at),
                to_iso(link.verified_at) if link.verified_at else None,
                link.error,
            ),
        )


def resolve_group(
    conn: sqlite3.Connection,
    group_id: int,
    *,
    now: datetime,
    ctx_factory: CtxFactory,
    browser_fallback: BrowserFallback | None = None,
) -> ApplyLink:
    """Resolve a job group's apply link and upsert its ``apply_link`` row.

    Start URLs are tried in order until one reaches ``live``; otherwise the most informative
    result wins (expired > blocked > unresolved).
    """
    starts = _start_urls(conn, group_id)
    if not starts:
        raise ValueError(f"job_group {group_id} has no member with a usable URL")
    best: Resolution | None = None
    for start in starts:
        if is_board_host(host_of(start)) and unwrap(start) is None:
            # No context at all: not even a robots.txt request goes to a board host.
            res = Resolution(start, ApplyStatus.unresolved, start, error=NO_FETCH_ERROR)
        else:
            ctx = ctx_factory(start)
            try:
                res = resolve_url(start, ctx, browser_fallback=browser_fallback)
            finally:
                ctx.close()
        if best is None or _RANK[res.status] < _RANK[best.status]:
            best = res
        if res.status == ApplyStatus.live:
            break
    assert best is not None
    link = ApplyLink(
        job_group_id=group_id,
        start_url=best.start_url,
        final_url=best.final_url,
        chain=best.chain,
        ats=best.ats,
        employer_host=best.employer_host,
        status=best.status,
        resolved_at=now,
        verified_at=now if best.fetched else None,
        error=best.error,
    )
    _save(conn, link)
    return link


def pending_groups(conn: sqlite3.Connection, limit: int) -> list[int]:
    """Groups that have a canonical job but no apply_link row yet, oldest first."""
    rows = conn.execute(
        "SELECT g.id FROM job_group g LEFT JOIN apply_link a ON a.job_group_id = g.id "
        "WHERE a.job_group_id IS NULL AND g.canonical_job_id IS NOT NULL ORDER BY g.id LIMIT ?",
        (limit,),
    ).fetchall()
    return [r[0] for r in rows]


def resolve_pending(
    conn: sqlite3.Connection,
    *,
    now: datetime,
    ctx_factory: CtxFactory,
    groups: Iterable[int] | Callable[[sqlite3.Connection], Iterable[int]] | None = None,
    limit: int = 100,
    browser_fallback: BrowserFallback | None = None,
) -> list[ApplyLink]:
    """Eager resolution (specs/015 "When resolution runs").

    ``groups`` selects what to resolve: explicit group ids, or a callable over the connection
    (where bucket A-C filtering plugs in). Default: every group without an apply_link row.
    Groups that already have a row are skipped either way.
    """
    if groups is None:
        ids: Iterable[int] = pending_groups(conn, limit)
    elif callable(groups):
        ids = groups(conn)
    else:
        ids = groups
    done: list[ApplyLink] = []
    for gid in ids:
        if len(done) >= limit:
            break
        if get_apply_link(conn, gid) is not None:
            continue
        try:
            done.append(
                resolve_group(
                    conn, gid, now=now, ctx_factory=ctx_factory, browser_fallback=browser_fallback
                )
            )
        except ValueError:
            continue
    if done:
        # Resolved final URLs can reveal groups that share one application target.
        from jobhunter.pipeline.dedupe_url import merge_by_apply_url

        merge_by_apply_url(conn, now=now)
    return done


def reverify(
    conn: sqlite3.Connection,
    group_id: int,
    *,
    now: datetime,
    ctx_factory: CtxFactory,
    max_age_hours: float = 24,
) -> ApplyLink | None:
    """Re-check a ``live`` link's final URL if it was verified more than ``max_age_hours`` ago.

    One GET, following at most one more redirect hop. Blocked links are never re-fetched, and
    links whose ATS is trusted without a request (USA Staffing) are left alone.
    """
    link = get_apply_link(conn, group_id)
    if link is None or link.status != ApplyStatus.live or not link.final_url:
        return link
    rule = match_ats(link.final_url)
    if rule is not None and not rule.fetch:
        return link
    if is_board_host(host_of(link.final_url)):
        return link  # never fetched (specs/017 phase 1e)
    checked = link.verified_at or link.resolved_at
    if now - checked < timedelta(hours=max_age_hours):
        return link

    url = link.final_url
    left_posting = False
    ctx = ctx_factory(url)
    try:
        resp = ctx.get(_fetchable(url), follow_redirects=False)
        if resp.is_redirect and resp.location:
            if not _http_target(resp.location):
                link.error = f"non-http destination: {resp.location[:200]}"
                _save(conn, link)
                return link
            left_posting = _left_posting(rule, url, resp.location)
            if not left_posting:
                url = resp.location
                resp = ctx.get(_fetchable(url), follow_redirects=False)
    except RobotsDisallowed as exc:
        link.status, link.error = ApplyStatus.blocked, str(exc)
        _save(conn, link)
        return link
    except FetchError as exc:
        link.error = f"{type(exc).__name__}: {exc}"
        _save(conn, link)
        return link
    finally:
        ctx.close()

    if left_posting or resp.status in EXPIRED_STATUSES:
        link.status = ApplyStatus.expired
    elif 200 <= resp.status < 300:
        expired = _is_html(resp) and looks_expired(resp.text)
        link.status = ApplyStatus.expired if expired else ApplyStatus.live
    else:
        link.error = f"HTTP {resp.status} at {url} on reverify"
        _save(conn, link)
        return link
    link.verified_at = now
    link.error = None
    _save(conn, link)
    return link


def unknown_hosts(conn: sqlite3.Connection, top: int = 20) -> list[tuple[str, int]]:
    """Most common final hosts with no ATS rule: the next rows to add to ``ATS_RULES``."""
    rows = conn.execute(
        "SELECT employer_host, COUNT(*) AS n FROM apply_link "
        "WHERE ats = 'unknown' AND employer_host IS NOT NULL "
        "GROUP BY employer_host ORDER BY n DESC, employer_host LIMIT ?",
        (top,),
    ).fetchall()
    return [(r["employer_host"], r["n"]) for r in rows]
