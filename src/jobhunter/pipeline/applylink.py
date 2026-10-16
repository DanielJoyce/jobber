"""Apply-link resolver (specs/015-apply-links.md).

Walks from the best source-provided link to the employer's application: tracking links are
unwrapped locally (``ats_rules.unwrap``), everything else is followed one hop at a time through
``FetchContext`` (3xx ``Location``, ``<meta http-equiv=refresh>``, literal ``window.location``
assignments), so every hop gets its own robots.txt check and rate limit. A 3xx hop is read
from its status and Location alone (``FetchContext.get_hop``); only a page the walk inspects
(meta refresh, script redirect, login form, closed-posting text) is downloaded. The walk stops at
a known ATS, which is canonicalized to its apply URL, or at the first page that goes nowhere.

The chain stored in ``apply_link.chain`` lists the hops *after* ``start_url``; each hop's
``status`` is the HTTP status of fetching it (None when it was never fetched: unwrapped through,
robots-disallowed, or not reached).

A login or single-sign-on page is never the posting: the walk stops before it, and the result is
``blocked`` with the URL before the wall (``LOGIN_ERROR``). A group resolves each member's start
URL (up to ``MAX_FETCH_STARTS`` of them) and keeps the best by where it ends
(``pipeline/apply_rank.py``); a link set by hand is never replaced (bug 334d9eb).
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
    TransientFetchError,
    fetch_context_for_url,
)
from jobhunter.core.models import ApplyHop, ApplyLink, ApplyStatus
from jobhunter.core.textnorm import html_to_text
from jobhunter.pipeline.apply_rank import TIER_LABEL, HostClasses, HostTier
from jobhunter.pipeline.ats_rules import (
    EXPIRED_PHRASES,
    EXPIRED_STATUSES,
    AtsRule,
    ats_name,
    host_of,
    is_http_url,
    is_login_wall,
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
# Start URLs per group that may cost requests (board starts with nothing to unwrap cost none
# and are not counted). Each one goes through FetchContext's per-host rate limits.
MAX_FETCH_STARTS = 6
# A page with a password field and less text than this is a login page, not a posting.
LOGIN_PAGE_MAX_TEXT = 2500
LOGIN_ERROR = "login wall"
USER_WHY = "set by you"
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
    login: bool = False  # stopped at a login or single-sign-on wall
    # A failure that may pass (429, 5xx, a transport error, a host held by Retry-After, an
    # unreachable robots.txt): the walk says nothing about where the link really goes.
    transient: bool = False

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


def looks_like_login(html: str) -> bool:
    """A sign-in page at a URL that does not say so: a password field and little else. A
    posting with a sign-in box in its header has far more text than a login page."""
    tree = LexborHTMLParser(html)
    if not any(
        (node.attributes.get("type") or "").strip().lower() == "password"
        for node in tree.css("input")
    ):
        return False
    return len(" ".join(html_to_text(html).split())) < LOGIN_PAGE_MAX_TEXT


def _stop_at_login(res: Resolution, before: str, wall: str) -> None:
    """Stop the walk at a login wall: the button can only open the page before it."""
    res.status, res.final_url, res.login = ApplyStatus.blocked, before, True
    res.error = f"{LOGIN_ERROR} at {host_of(wall)}"


_ROBOTS_DOWN = re.compile(r"^robots\.txt (unreachable|returned 5\d\d)")


def robots_unavailable(exc: RobotsDisallowed) -> bool:
    """A refusal because robots.txt could not be read (unreachable, 5xx), not because it
    disallows us: FetchContext denies the whole origin for the run, which may pass."""
    return bool(_ROBOTS_DOWN.match(exc.reason))


class Deferred(Exception):
    """``resolve_group(defer_transient=True)``: a start failed in a way that may pass and
    nothing settled the group, so no row was written; try again later."""

    def __init__(self, group_id: int, errors: list[str]) -> None:
        super().__init__(f"job_group {group_id}: deferred ({'; '.join(errors)[:300]})")
        self.group_id = group_id


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
    if is_login_wall(start_url):
        _stop_at_login(res, start_url, start_url)
        return res
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
        if is_login_wall(target):
            here = match_ats(url)
            if here is not None and match_ats(target) is here:
                # The ATS's own sign-in step on the way to its application (Taleo's
                # jobapply.ftl -> login.jsf): the ATS page was reached, so it is the link.
                res.status, res.ats, res.fetched = ApplyStatus.live, here.name, True
                res.final_url = here.canonical(url)
                return False
            _stop_at_login(res, url, target)  # never requested
            return False
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
            resp = ctx.get_hop(_fetchable(url))
        except RobotsDisallowed as exc:
            res.status = ApplyStatus.blocked
            res.final_url = previous if res.chain else start_url
            res.error = str(exc)
            res.transient = robots_unavailable(exc)
            return res
        except AccessDenied as exc:
            res.error, res.final_url = str(exc), url
            set_status(exc.status)
            return res
        except FetchError as exc:  # TransientFetchError and friends
            res.error, res.final_url = str(exc), url
            res.transient = isinstance(exc, TransientFetchError)
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
            res.ats = ats_name(url) or "unknown"
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
            if looks_like_login(html):
                _stop_at_login(res, previous, url)
                return res

        res.fetched = True
        res.ats = ats_name(url) or "unknown"
        if html and looks_expired(html):
            res.status = ApplyStatus.expired
        else:
            res.status = ApplyStatus.live
            if rule is not None:
                res.final_url = rule.canonical(url)
        return res


# ─── apply_link rows ───────────────────────────────────────────────────────


@dataclass
class Candidate:
    """One distinct start URL of a group, with the members that carry it."""

    url: str
    order: int  # first-seen position: capture links, then apply links, then posting URLs
    members: set[int] = field(default_factory=set)
    seen_at: str = ""  # the latest last_seen_at among those members (ISO)
    capture: bool = False  # supplied by an extension capture linked to the group


def _usable(url: str | None) -> bool:
    return bool(url) and _http_target(url or "")


def _candidates(conn: sqlite3.Connection, group_id: int, classes: HostClasses) -> list[Candidate]:
    """A group's distinct start URLs. Capture-supplied first: a board capture's decoded
    off-site Apply destination (``job_board_ref.apply_url``) and the pages of jobs added by an
    extension capture (``capture_log``). Then source-provided apply links (e.g. USAJOBS
    ApplyURI), then posting URLs; canonical member first within each."""
    members = group_members(conn, group_id)
    by_url: dict[str, Candidate] = {}

    def add(url: str | None, member: int, seen: str | None, capture: bool) -> None:
        if not url or not _usable(url):
            return
        c = by_url.get(url)
        if c is None:
            c = by_url[url] = Candidate(url, len(by_url))
        c.members.add(member)
        c.seen_at = max(c.seen_at, seen or "")
        c.capture = c.capture or capture

    def employer_page(url: str | None) -> bool:
        # Only a page on an ATS or a host naming the employer counts as the employer's page;
        # a capture on builtin.com, wellfound.com or a state bank is just another board copy.
        return bool(url) and classes.employer_or_ats(url)

    ids = [m["id"] for m in members]
    marks = ",".join("?" * len(ids))
    seen = {m["id"]: m["last_seen_at"] for m in members}
    if ids:
        for r in conn.execute(
            f"SELECT job_id, apply_url FROM job_board_ref WHERE job_id IN ({marks}) "
            "AND apply_url IS NOT NULL ORDER BY seen_at DESC",
            ids,
        ):
            if employer_page(r[1]):
                add(r[1], r[0], seen.get(r[0]), True)
        captured = {
            r[0]
            for r in conn.execute(
                f"SELECT DISTINCT job_id FROM capture_log WHERE job_id IN ({marks}) "
                "AND outcome = 'added'",
                ids,
            )
        }
        for m in members:
            if m["id"] in captured:
                for col in ("apply_url", "url"):
                    if employer_page(m[col]):
                        add(m[col], m["id"], m["last_seen_at"], True)
    for col in ("apply_url", "url"):
        for m in members:
            add(m[col], m["id"], m["last_seen_at"], False)
    return list(by_url.values())


def _costs_requests(url: str) -> bool:
    """False for starts resolved without any request: a board host with nothing to unwrap
    (never requested), or an ATS trusted as given (USA Staffing)."""
    if is_board_host(host_of(url)) and unwrap(url) is None:
        return False
    rule = match_ats(url)
    return not (rule is not None and not rule.fetch)


def _fetch_order(cands: list[Candidate], classes: HostClasses) -> list[Candidate]:
    """Captures first; then by where the (locally unwrapped) start points: ATS, employer,
    others, trackers, boards last; within that, one start per host before a second one."""
    per_host: dict[str, int] = {}
    keyed = []
    for c in cands:
        target = c.url
        for _ in range(3):
            inner = unwrap(target)
            if inner is None:
                break
            target = inner
        host = host_of(target)
        nth = per_host.get(host, 0)
        per_host[host] = nth + 1
        keyed.append(((0 if c.capture else 1, int(classes.tier(target)), nth, c.order), c))
    return [c for _, c in sorted(keyed, key=lambda kc: kc[0])]


def _ts(iso: str) -> float:
    try:
        return from_iso(iso).timestamp() if iso else 0.0
    except ValueError:
        return 0.0


def _capture_wins(cand: Candidate, res: Resolution, classes: HostClasses) -> bool:
    """A capture of the employer's page beats any ranking unless it is closed or a wall."""
    return (
        cand.capture
        and not res.login
        and res.status != ApplyStatus.expired
        and classes.employer_or_ats(res.final_url)
    )


def _settled(cand: Candidate, res: Resolution, classes: HostClasses) -> bool:
    """Nothing later can beat this result: stop spending requests."""
    if _capture_wins(cand, res, classes):
        return True
    return res.status == ApplyStatus.live and classes.tier(res.final_url) == HostTier.ats


def rank_results(
    results: list[tuple[Candidate, Resolution]], classes: HostClasses
) -> list[tuple[Candidate, Resolution]]:
    """Best first (bug 334d9eb): never a login wall while anything else exists; a capture of
    the employer's page; a closed page only on an ATS or the employer's site counts before
    live links (a dead tracker or board copy says nothing about the job, so it goes after
    every live one); then by final host (ATS, employer, other, tracker, board); then status
    (live, expired, blocked, unresolved); then how many members reached the same final URL;
    then the most recently seen member."""
    agree: dict[str, set[int]] = {}
    for cand, res in results:
        if res.final_url and not res.login:
            agree.setdefault(normalize_url(res.final_url), set()).update(cand.members)

    def key(cr: tuple[Candidate, Resolution]) -> tuple[int, int, int, int, int, int, float, int]:
        cand, res = cr
        n = len(agree.get(normalize_url(res.final_url), ())) if res.final_url else 0
        tier = classes.tier(res.final_url)
        dead_copy = res.status == ApplyStatus.expired and tier > HostTier.employer
        return (
            int(res.login),
            0 if _capture_wins(cand, res, classes) else 1,
            int(dead_copy),
            int(tier),
            _RANK[res.status],
            -n,
            -_ts(cand.seen_at),
            cand.order,
        )

    return sorted(results, key=key)


def _why(cand: Candidate, res: Resolution, classes: HostClasses, tried: int, total: int) -> str:
    """One line for the job page: where the link came from and why it won."""
    start, end = host_of(res.start_url), host_of(res.final_url or res.start_url)
    route = f"via {start} → {end}" if start != end else end
    if res.login:
        return f"{route} · {res.error}; no other link reached the posting"
    if _capture_wins(cand, res, classes):
        reason = "from your capture of the employer's page"
    elif res.status == ApplyStatus.expired and classes.employer_or_ats(res.final_url):
        reason = "the posting is closed there"
    elif res.status == ApplyStatus.expired:
        reason = "this link no longer works, and no live copy was found"
    else:
        reason = TIER_LABEL[classes.tier(res.final_url)]
    checked = f" · {tried} of {total} links checked" if total > 1 else ""
    return f"{route} · {reason}{checked}"


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
        chosen_by=row["chosen_by"],
        why=row["why"],
    )


def get_apply_link(conn: sqlite3.Connection, group_id: int) -> ApplyLink | None:
    row = conn.execute("SELECT * FROM apply_link WHERE job_group_id = ?", (group_id,)).fetchone()
    return _row_to_link(row) if row else None


def _save(conn: sqlite3.Connection, link: ApplyLink) -> None:
    chain = json.dumps([h.model_dump() for h in link.chain])
    with _txn(conn):
        conn.execute(
            "INSERT INTO apply_link (job_group_id, start_url, final_url, chain, ats, "
            "employer_host, status, resolved_at, verified_at, error, chosen_by, why) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(job_group_id) DO UPDATE SET start_url = excluded.start_url, "
            "final_url = excluded.final_url, chain = excluded.chain, ats = excluded.ats, "
            "employer_host = excluded.employer_host, status = excluded.status, "
            "resolved_at = excluded.resolved_at, verified_at = excluded.verified_at, "
            "error = excluded.error, chosen_by = excluded.chosen_by, why = excluded.why "
            # A link the user set is never replaced automatically, even by a resolve or
            # re-verify that read the row before the user set it (bug e0d047b, review N1).
            "WHERE apply_link.chosen_by IS NOT 'user' OR excluded.chosen_by IS 'user'",
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
                link.chosen_by,
                link.why,
            ),
        )


def resolve_group(
    conn: sqlite3.Connection,
    group_id: int,
    *,
    now: datetime,
    ctx_factory: CtxFactory,
    browser_fallback: BrowserFallback | None = None,
    max_fetches: int = MAX_FETCH_STARTS,
    only_if_settled: bool = False,
    defer_transient: bool = False,
    only_if_better: bool = False,
) -> ApplyLink:
    """Resolve a job group's apply link and upsert its ``apply_link`` row.

    A link set by hand is returned untouched. Otherwise each distinct start URL is resolved,
    in ``_fetch_order``, until one settles it (a live ATS posting, or a capture of the
    employer's page) or ``max_fetches`` starts that cost requests have been tried; the results
    are ranked by ``rank_results``.

    ``only_if_settled`` (re-resolving an existing row): the new result replaces the row only
    when it is an answer (live, expired, or a real login stop), so a transient failure, such as
    an unreachable robots.txt, leaves the old row to be tried again later.

    ``defer_transient`` (the nightly pass): when any start failed transiently (see
    ``Resolution.transient``) and no start settled the group, raise :class:`Deferred` and
    write nothing, so an outage never stores a worse link in place of the one a later walk
    would find.

    ``only_if_better`` (a retry of a link that is not settled, bug c369bcc): the new result
    replaces the stored row only when it is no worse by ``link_rank``, so a retry never trades
    a live board copy for a failed fetch.
    """
    existing = get_apply_link(conn, group_id)
    if existing is not None and existing.chosen_by == "user":
        return existing
    classes = HostClasses.load(conn, group_id)
    cands = _candidates(conn, group_id, classes)
    if not cands:
        raise ValueError(f"job_group {group_id} has no member with a usable URL")
    results: list[tuple[Candidate, Resolution]] = []
    fetches = 0
    for cand in _fetch_order(cands, classes):
        start = cand.url
        if is_board_host(host_of(start)) and unwrap(start) is None:
            # No context at all: not even a robots.txt request goes to a board host.
            res = Resolution(start, ApplyStatus.unresolved, start, error=NO_FETCH_ERROR)
        else:
            if _costs_requests(start):
                if fetches >= max_fetches:
                    continue
                fetches += 1
            ctx = ctx_factory(start)
            try:
                res = resolve_url(start, ctx, browser_fallback=browser_fallback)
            finally:
                ctx.close()
        results.append((cand, res))
        if _settled(cand, res, classes):
            break
    if not results:
        raise ValueError(f"job_group {group_id}: no start URL could be tried")
    win_cand, best = rank_results(results, classes)[0]
    if defer_transient and not _settled(win_cand, best, classes):
        failed = [r.error or "transient failure" for _, r in results if r.transient]
        if failed:
            raise Deferred(group_id, failed)
    capture = _capture_wins(win_cand, best, classes)
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
        chosen_by="capture" if capture else None,
        why=_why(win_cand, best, classes, len(results), len(cands)),
    )
    answered = best.status in (ApplyStatus.live, ApplyStatus.expired) or best.login
    if only_if_settled and existing is not None and not answered:
        return existing
    if (
        only_if_better
        and existing is not None
        and existing.chosen_by != "user"
        and link_rank(link, classes) > link_rank(existing, classes)
    ):
        return existing
    _save(conn, link)
    return link


def link_rank(link: ApplyLink, classes: HostClasses) -> tuple[int, int, int]:
    """Lower is better, for replacing a stored row (bug c369bcc): an answer from the ATS or
    the employer's site (live, or closed there), then a live link elsewhere (a board copy, a
    tracker), then anything else (unresolved, a dead copy, a login wall); then by final host
    and status."""
    tier = classes.tier(link.final_url)
    on_employer = tier <= HostTier.employer and not is_login_link(link)
    if on_employer and link.status in (ApplyStatus.live, ApplyStatus.expired):
        group = 0
    elif link.status == ApplyStatus.live and not is_login_link(link):
        group = 1
    else:
        group = 2
    return (group, int(tier), _RANK[link.status])


def set_user_link(conn: sqlite3.Connection, group_id: int, url: str, now: datetime) -> ApplyLink:
    """The link the user set on the job page. Kept as given (no request is made) and never
    replaced by automatic resolution; ``clear_user_link`` undoes it."""
    url = url.strip()
    if len(url) > 2048 or any(ch.isspace() for ch in url) or not _http_target(url):
        raise ValueError("an apply link must be a full http:// or https:// address")
    link = ApplyLink(
        job_group_id=group_id,
        start_url=url,
        final_url=url,
        chain=[],
        ats=ats_name(url) or "unknown",
        employer_host=host_of(url) or None,
        status=ApplyStatus.live,
        resolved_at=now,
        chosen_by="user",
        why=USER_WHY,
    )
    _save(conn, link)
    return link


def clear_user_link(conn: sqlite3.Connection, group_id: int) -> bool:
    """Reset: drop the user's link, so the group resolves again (on demand, on the job page)."""
    with _txn(conn):
        cur = conn.execute(
            "DELETE FROM apply_link WHERE job_group_id = ? AND chosen_by = 'user'", (group_id,)
        )
    return cur.rowcount > 0


def is_login_link(link: ApplyLink) -> bool:
    """A link stopped at a login wall, or an older row that took one for the posting."""
    if link.chosen_by == "user":
        return False
    if link.error and link.error.startswith(LOGIN_ERROR):
        return True
    return is_login_wall(link.final_url) or any(is_login_wall(h.url) for h in link.chain)


def needs_reresolve(link: ApplyLink) -> bool:
    """A row written before login walls were recognized: ``live`` or ``expired`` while its
    final URL or a hop is a login wall (bug 334d9eb). Rows already ``blocked`` at a wall are
    left alone, so a group with nothing better is not fetched again every night."""
    return link.status in (ApplyStatus.live, ApplyStatus.expired) and is_login_link(link)


def login_wall_groups(conn: sqlite3.Connection, limit: int) -> list[int]:
    out: list[int] = []
    for row in conn.execute(
        "SELECT * FROM apply_link WHERE status IN ('live', 'expired') "
        "AND chosen_by IS NULL ORDER BY job_group_id"
    ):
        if needs_reresolve(_row_to_link(row)):
            out.append(row["job_group_id"])
            if len(out) >= limit:
                break
    return out


def reresolve_login_walls(
    conn: sqlite3.Connection,
    *,
    now: datetime,
    ctx_factory: CtxFactory,
    limit: int = 25,
) -> list[ApplyLink]:
    """Resolve again the groups whose stored link is a login wall taken for the posting.

    Nightly (``runner``, resolve stage): network only through ``ctx_factory``, no paid work.
    A group whose new result is no answer (a fetch failed) keeps its row and is tried again
    next time. Returns the rows that were replaced."""
    done: list[ApplyLink] = []
    for gid in login_wall_groups(conn, limit):
        link = reresolve_legacy(conn, gid, now=now, ctx_factory=ctx_factory)
        if link is not None and not needs_reresolve(link):
            done.append(link)
    return done


def reresolve_legacy(
    conn: sqlite3.Connection, group_id: int, *, now: datetime, ctx_factory: CtxFactory
) -> ApplyLink | None:
    """Resolve a group again in place of an older row that took a login wall for the posting
    (nightly, the job page, and a click on Apply). The old row stays when nothing answered."""
    try:
        return resolve_group(conn, group_id, now=now, ctx_factory=ctx_factory, only_if_settled=True)
    except ValueError:
        return get_apply_link(conn, group_id)


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


def reverify_due(link: ApplyLink, now: datetime, max_age_hours: float = 24) -> bool:
    """Whether ``reverify`` would make a request for this row: a ``live`` link last checked
    more than ``max_age_hours`` ago that is fetched at all (not set by the user, not a login
    wall, not an ATS trusted without a request, not a job board)."""
    if link.status != ApplyStatus.live or not link.final_url:
        return False
    if link.chosen_by == "user" or is_login_link(link):
        return False
    rule = match_ats(link.final_url)
    if rule is not None and not rule.fetch:
        return False
    if is_board_host(host_of(link.final_url)):
        return False  # never fetched (specs/017 phase 1e)
    checked = link.verified_at or link.resolved_at
    return now - checked >= timedelta(hours=max_age_hours)


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
    links whose ATS is trusted without a request (USA Staffing) are left alone, as are links
    the user set and login walls (never requested). A redirect to a login wall blocks the link.
    """
    link = get_apply_link(conn, group_id)
    if link is None or not reverify_due(link, now, max_age_hours):
        return link
    assert link.final_url is not None  # reverify_due
    rule = match_ats(link.final_url)
    url = link.final_url
    left_posting = False
    ctx = ctx_factory(url)
    try:
        resp = ctx.get_hop(_fetchable(url))
        if resp.is_redirect and resp.location:
            if not _http_target(resp.location):
                link.error = f"non-http destination: {resp.location[:200]}"
                _save(conn, link)
                return link
            left_posting = _left_posting(rule, url, resp.location)
            if not left_posting and is_login_wall(resp.location):
                link.status = ApplyStatus.blocked
                link.error = f"{LOGIN_ERROR} at {host_of(resp.location)}"
                _save(conn, link)
                return link
            if not left_posting and is_board_host(host_of(resp.location)):
                # A redirect into LinkedIn or Indeed is never followed (specs/017 1e).
                link.error = NO_FETCH_ERROR
                _save(conn, link)
                return link
            if not left_posting:
                url = resp.location
                resp = ctx.get_hop(_fetchable(url))
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
