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

import copy
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
from jobhunter.pipeline.apply_hints import (
    MAX_HINTS,
    ats_posting,
    description_hints,
    match_kind,
    page_apply_hrefs,
)
from jobhunter.pipeline.apply_rank import TIER_LABEL, HostClasses, HostTier
from jobhunter.pipeline.ats_rules import (
    EXPIRED_PHRASES,
    EXPIRED_STATUSES,
    TRACKER_HOSTS,
    AtsRule,
    ats_name,
    host_in,
    host_of,
    is_http_url,
    is_login_wall,
    match_ats,
    unwrap,
)
from jobhunter.pipeline.board_ids import is_board_host
from jobhunter.pipeline.dedupe import group_members
from jobhunter.pipeline.listing import _txn, from_iso, to_iso
from jobhunter.sources.adapters.nlx import page_apply_urls as nlx_page_apply_urls
from jobhunter.sources.adapters.nlx import page_guid as nlx_page_guid

if TYPE_CHECKING:
    from jobhunter.config import Settings

MAX_HOPS = 8
MAX_META_DELAY_S = 10
# Start URLs per group that may cost requests (board starts with nothing to unwrap cost none
# and are not counted). Each one goes through FetchContext's per-host rate limits.
MAX_FETCH_STARTS = 6
# Bump when the walk learns to reach links it could not before (a new hop kind, a new board
# step): tries counted by an older walk stop counting, so groups not resolved yet get a full
# set of tries with the better walk (``apply_attempts``, bug c369bcc).
# 2: description hints and the board Apply-button step (bug 3bab68d).
WALK_VERSION = 2
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
    start_status: int | None = None  # HTTP status of fetching start_url itself
    # A board's Apply button led to the job's own ATS or employer page, but the walk kept the
    # board copy (a wall or an error there): the job's posting exists, so no fallback runs.
    own_posting_seen: bool = False

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


def _own_sign_in(from_url: str, to_url: str) -> bool:
    """A page on an ATS sending to that ATS's own sign-in page (Workday ``/login``, Taleo
    ``login.jsf``; ``AtsRule.sign_in``): a step on the way to applying, so the ATS page was
    reached and is the link (bug 5c1d738). A login-looking board slug (``/cas``, ``/sso``)
    never counts."""
    rule = match_ats(from_url)
    return rule is not None and rule.is_sign_in(to_url)


def _left_posting(rule: AtsRule | None, from_url: str, to_url: str) -> bool:
    """An ATS posting redirecting to a non-posting page on the same ATS (e.g. Greenhouse's
    ``/acme?error=true``, or a board whose slug is ``cas``) means the posting is gone. Its own
    sign-in page (``_own_sign_in``) does not: that is a step on the way to applying."""
    return (
        rule is not None
        and rule.is_posting(from_url)
        and match_ats(to_url) is rule
        and not rule.is_posting(to_url)
        and not rule.is_sign_in(to_url)
    )


def _off_site(url: str, target: str) -> bool:
    """A different site: neither host is the other or a subdomain of it."""
    a, b = host_of(url), host_of(target)
    return bool(b) and a != b and not a.endswith("." + b) and not b.endswith("." + a)


_BUTTON_TIERS = (HostTier.ats, HostTier.employer, HostTier.tracker)
# Job boards whose own posting is where you apply: USAJOBS is the federal entry point (bug
# c369bcc), and its pages link questionnaire previews that look like Apply buttons.
NO_BUTTON_HOSTS: tuple[str, ...] = ("usajobs.gov", "www.usajobs.gov")
# Per-group memo of button walks (resolve_group passes one dict to every start's walk), so the
# redirector start and the posting-page start of one NLx job do not walk the same chain twice.
ButtonCache = dict[str, object]


class _TransientButton(Exception):
    """The board's button could not be read for a reason that may pass (429, 5xx, an
    unreadable robots.txt): the board copy stands, but the walk is marked transient."""


def _button_ok(url: str, href: str, classes: HostClasses) -> bool:
    """An Apply target worth following off a board: another site, not a login page, not an ATS
    link trusted without a request, and on an ATS, the employer's site or a tracker."""
    try:
        if not _off_site(url, href) or is_login_wall(href):
            return False
        rule = match_ats(href)
        if rule is not None and not rule.fetch:
            return False
        return classes.tier(href) in _BUTTON_TIERS or unwrap(href) is not None
    except ValueError:  # a malformed URL, e.g. one wrapping "http://[object Object]"
        return False


def board_apply_targets(
    html: str,
    url: str,
    ctx: FetchContext,
    classes: HostClasses | None = None,
    cache: ButtonCache | None = None,
) -> list[str]:
    """Where a job board copy's Apply button goes (bug 3bab68d), best first.

    The first off-site Apply link in the page that points at an ATS, the employer's site or
    a click tracker (a state job bank's "Apply for benefits" never counts). For an NLx or
    dejobs.org shell page, the posting's ``other.application_link`` when it qualifies the same
    way, then its ``link`` (``de.jobsyn.org``, which redirects to the employer); one request
    through ``ctx``, remembered in ``cache``. Raises :class:`_TransientButton` when that
    request fails in a way that may pass."""
    classes = classes or HostClasses()
    out: list[str] = []
    for href in page_apply_hrefs(html, url):
        if _button_ok(url, href, classes):
            out.append(href)
            break
    key = f"nlx-json:{nlx_page_guid(url) or url}"  # one JSON per posting, any NLx page
    if cache is not None and key in cache:
        app_link, link = cache[key]  # type: ignore[misc]
    else:
        try:
            app_link, link = nlx_page_apply_urls(url, ctx)
        except RobotsDisallowed as exc:
            if robots_unavailable(exc):
                raise _TransientButton(str(exc)) from exc
            app_link = link = None
        except TransientFetchError as exc:
            raise _TransientButton(str(exc)) from exc
        except FetchError:
            app_link = link = None
        if cache is not None:
            cache[key] = (app_link, link)
    if app_link and _button_ok(url, app_link, classes):
        out.append(app_link)
    try:
        if link and _off_site(url, link) and not is_login_wall(link):
            out.append(link)
    except ValueError:
        pass
    return list(dict.fromkeys(out))


def _reached_posting(res: Resolution, classes: HostClasses) -> bool:
    """A button walk that got somewhere better than the board copy: live off the job boards,
    not a wall; or the posting found closed on its ATS or the employer's site (a 404 or
    closed text there is a real answer: the job is closed, review of PR 188)."""
    final = res.final_url or res.start_url
    if res.login:
        return False
    if res.status == ApplyStatus.live:
        return not classes.is_aggregator(host_of(final))
    return res.status == ApplyStatus.expired and classes.employer_or_ats(final)


def resolve_url(
    start_url: str,
    ctx: FetchContext,
    *,
    max_hops: int = MAX_HOPS,
    browser_fallback: BrowserFallback | None = None,
    classes: HostClasses | None = None,
    button_cache: ButtonCache | None = None,
    _seen: set[str] | None = None,
) -> Resolution:
    """Follow one start URL to its destination. Never raises for fetch failures.

    A page on a job board or aggregator (``classes.is_aggregator``) is not the employer's
    posting: when it has an Apply button that leaves the board, the walk follows it
    (``board_apply_targets``; a hop with ``via="button"``). The board copy stays the result
    unless that walk reaches a live page off the boards: a login wall, an error, a closed page
    or another board never replaces it. A passing failure on the way marks the result
    ``transient`` so the nightly pass tries again instead of storing the board copy."""
    boards = classes or HostClasses()
    res = Resolution(start_url=start_url, status=ApplyStatus.unresolved, final_url=start_url)
    if is_login_wall(start_url):
        _stop_at_login(res, start_url, start_url)
        return res
    seen = _seen if _seen is not None else set()
    seen.add(normalize_url(start_url))
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
            if here is not None and _own_sign_in(url, target):
                # The ATS's own sign-in step on the way to its application (Taleo's
                # jobapply.ftl -> login.jsf): the ATS page was reached, so it is the link.
                # The hop stays in the chain; is_login_link knows it for what it is.
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
        else:
            res.start_status = status

    def follow_button(html: str) -> bool:
        """Walk the board's Apply button; True when ``res`` now holds that walk's result."""
        try:
            targets = board_apply_targets(html, url, ctx, boards, button_cache)
        except _TransientButton as exc:
            res.transient, res.error = True, f"Apply button not read: {exc}"[:300]
            return False
        for target in targets:
            key = normalize_url(target)
            if key in seen or not _http_target(target):
                continue
            left = max_hops - len(res.chain) - 1
            if left < 0:
                return False
            cached = button_cache.get(f"walk:{key}") if button_cache is not None else None
            if isinstance(cached, Resolution):
                sub = copy.deepcopy(cached)
            else:
                sub = resolve_url(
                    target,
                    ctx,
                    max_hops=left,
                    browser_fallback=browser_fallback,
                    classes=boards,
                    button_cache=button_cache,
                    _seen=set(seen),
                )
                if button_cache is not None:
                    button_cache[f"walk:{key}"] = copy.deepcopy(sub)
            if _reached_posting(sub, boards):
                hop = ApplyHop(
                    url=target,
                    host=host_of(target),
                    method="meta",
                    via="button",
                    status=sub.start_status,
                )
                res.chain.append(hop)
                res.chain.extend(sub.chain)
                res.status, res.final_url, res.ats = sub.status, sub.final_url, sub.ats
                res.fetched, res.error = sub.fetched, sub.error
                return True
            if boards.employer_or_ats(sub.final_url or sub.start_url):
                # The job's own posting was found (behind a wall, or failing): the board copy
                # stands, but no link from the text may replace it.
                res.own_posting_seen = True
            if sub.transient:
                res.transient = True
                res.error = (sub.error or "transient failure after the Apply button")[:300]
                return False
        return False

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
            if (
                boards.is_aggregator(host_of(url))
                and not host_in(host_of(url), NO_BUTTON_HOSTS)
                and len(res.chain) < max_hops
                and follow_button(html)
            ):
                return res

        res.fetched = True
        res.final_url = url
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
    # From the posting's text (bug 3bab68d): "matched" names this job (an id or the title),
    # "fallback" is a cued ATS posting that does not and is tried only when nothing else
    # reached an ATS or the employer's site.
    hint: Literal["matched", "fallback"] | None = None
    # A matched hint that shares an id number with the job's own links (``match_kind``), not
    # just its title words: only such a link may stand in on a bad night (bug 5c1d738).
    by_id: bool = False


def _usable(url: str | None) -> bool:
    return bool(url) and _http_target(url or "")


def _candidates(conn: sqlite3.Connection, group_id: int, classes: HostClasses) -> list[Candidate]:
    """A group's distinct start URLs. Capture-supplied first: a board capture's decoded
    off-site Apply destination (``job_board_ref.apply_url``) and the pages of jobs added by an
    extension capture (``capture_log``). Then source-provided apply links (e.g. USAJOBS
    ApplyURI), then posting URLs, then apply links named in the members' descriptions
    (``_description_starts``); canonical member first within each."""
    members = group_members(conn, group_id)
    by_url: dict[str, Candidate] = {}

    def add(
        url: str | None,
        member: int,
        seen: str | None,
        capture: bool,
        hint: Literal["matched", "fallback"] | None = None,
        by_id: bool = False,
    ) -> None:
        if not url or not _usable(url):
            return
        c = by_url.get(url)
        if c is None:
            c = by_url[url] = Candidate(url, len(by_url), hint=hint, by_id=by_id)
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
    for m in members:
        for url, kind, by_id in _description_starts(m, classes):
            if url not in by_url:  # a source's own link keeps its standing
                add(url, m["id"], m["last_seen_at"], False, kind, by_id)
    return list(by_url.values())


def _unwrapped(url: str) -> str | None:
    """The destination a tracking link carries (up to three layers), else the URL itself;
    None for a malformed one. Stored as the start, so a wrapper's other parameters (an Outlook
    safelink's data=, which names the sender) never are."""
    try:
        for _ in range(3):
            inner = unwrap(url)
            if inner is None:
                break
            url = inner
        return url if _usable(url) else None
    except ValueError:  # urlsplit rejects e.g. "http://[object Object]"
        return None


def _description_starts(
    member: sqlite3.Row, classes: HostClasses
) -> list[tuple[str, Literal["matched", "fallback"], bool]]:
    """A member's description links that may be starts (bug 3bab68d, review of PR 188).

    Each link is graded by where it points once unwrapped. Never a tracker, an ATS link
    trusted without a request (a USA Staffing questionnaire preview), or an ATS home or list
    page. A link that names this job (``matches_job``) on an ATS posting or the employer's
    site is ``matched``: an ordinary start. A cued ATS posting that does not name it (a stale
    template link, another opening) is a ``fallback``. Everything else is dropped: uncued
    links, and employer pages that do not name the job (careers home pages). When two links
    both seem to name the job, neither is trusted as matched. The third value says a matched
    link shares an id number with the job (``match_kind``), not just its title words."""
    own = [member["url"], member["apply_url"], member["external_id"]]
    kept: list[tuple[str, str | None, bool]] = []  # (url, match kind, cued)
    for h in description_hints(member["description_raw"], member["url"]):
        url = _unwrapped(h.url)
        if url is None or host_in(host_of(url), TRACKER_HOSTS):
            continue
        rule = match_ats(url)
        if rule is not None and not rule.fetch:
            continue
        tier = classes.tier(url)
        if tier == HostTier.ats and not ats_posting(url):
            continue
        if tier not in (HostTier.ats, HostTier.employer):
            continue
        kind = match_kind(url, title=member["title"], own=own)
        if kind is None and not (h.cued and tier == HostTier.ats):
            continue
        kept.append((url, kind, h.cued))
    if sum(1 for _, kind, _ in kept if kind) > 1:
        kept = [(u, None, c) for u, _, c in kept if c and classes.tier(u) == HostTier.ats]
    out: list[tuple[str, Literal["matched", "fallback"], bool]] = []
    for url, kind, _ in kept[:MAX_HINTS]:
        out.append((url, "matched" if kind else "fallback", kind == "id"))
    return out


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
    every live one); then by final host (ATS, employer, other, tracker, board); the job's own
    links before links from its text (bug 3bab68d); then status
    (live, expired, blocked, unresolved); then how many members reached the same final URL;
    then the most recently seen member."""
    agree: dict[str, set[int]] = {}
    for cand, res in results:
        if res.final_url and not res.login:
            agree.setdefault(normalize_url(res.final_url), set()).update(cand.members)

    def key(
        cr: tuple[Candidate, Resolution],
    ) -> tuple[int, int, int, int, int, int, int, float, int]:
        cand, res = cr
        n = len(agree.get(normalize_url(res.final_url), ())) if res.final_url else 0
        tier = classes.tier(res.final_url)
        dead_copy = res.status == ApplyStatus.expired and tier > HostTier.employer
        return (
            int(res.login),
            0 if _capture_wins(cand, res, classes) else 1,
            int(dead_copy),
            int(tier),
            # A link from the posting's text never beats the job's own link to the same tier,
            # live or closed (review of PR 188).
            int(cand.hint is not None),
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
    if cand.hint is not None:
        reason += " · from a link in the posting's text"
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
        from_hint=bool(row["from_hint"]),
    )


def get_apply_link(conn: sqlite3.Connection, group_id: int) -> ApplyLink | None:
    row = conn.execute("SELECT * FROM apply_link WHERE job_group_id = ?", (group_id,)).fetchone()
    return _row_to_link(row) if row else None


def _save(conn: sqlite3.Connection, link: ApplyLink) -> None:
    # ``via`` only when set: chains stay as they were for every hop that is not a button.
    chain = json.dumps(
        [h.model_dump(exclude={"via"} if h.via is None else None) for h in link.chain]
    )
    with _txn(conn):
        conn.execute(
            "INSERT INTO apply_link (job_group_id, start_url, final_url, chain, ats, "
            "employer_host, status, resolved_at, verified_at, error, chosen_by, why, "
            "from_hint) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(job_group_id) DO UPDATE SET start_url = excluded.start_url, "
            "final_url = excluded.final_url, chain = excluded.chain, ats = excluded.ats, "
            "employer_host = excluded.employer_host, status = excluded.status, "
            "resolved_at = excluded.resolved_at, verified_at = excluded.verified_at, "
            "error = excluded.error, chosen_by = excluded.chosen_by, why = excluded.why, "
            "from_hint = excluded.from_hint "
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
                int(link.from_hint),
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
    own = [c for c in cands if c.hint != "fallback"]
    fallbacks = [c for c in cands if c.hint == "fallback"]
    cache: ButtonCache = {}

    def walk(start: str) -> Resolution:
        if is_board_host(host_of(start)) and unwrap(start) is None:
            # No context at all: not even a robots.txt request goes to a board host.
            return Resolution(start, ApplyStatus.unresolved, start, error=NO_FETCH_ERROR)
        ctx = ctx_factory(start)
        try:
            return resolve_url(
                start,
                ctx,
                browser_fallback=browser_fallback,
                classes=classes,
                button_cache=cache,
            )
        finally:
            ctx.close()

    results: list[tuple[Candidate, Resolution]] = []
    fetches = 0
    for cand in _fetch_order(own, classes):
        if _costs_requests(cand.url):
            if fetches >= max_fetches:
                continue
            fetches += 1
        res = walk(cand.url)
        results.append((cand, res))
        if _settled(cand, res, classes):
            break
    # Fallbacks (cued ATS postings that do not name this job) only when no start of the job's
    # own reached an ATS or the employer's site (live, closed, failing or behind a sign-in,
    # directly or through a board's Apply button), none is a live board copy of the posting,
    # and nothing failed in a way that may pass. Only a live ATS posting from one is kept
    # (reviews of PR 188).
    fallback_failed: list[str] = []
    own_reached = any(
        classes.employer_or_ats(r.final_url) or r.own_posting_seen for _, r in results
    )
    board_copy = any(
        r.status == ApplyStatus.live
        and not r.login
        and classes.tier(r.final_url) == HostTier.aggregator
        for _, r in results
    ) or (
        # a retry: the board copy stored by an earlier walk counts too (review round 3)
        existing is not None
        and existing.status == ApplyStatus.live
        and not existing.from_hint
        and not is_login_link(existing)
        and classes.tier(existing.final_url) == HostTier.aggregator
    )
    if (
        fallbacks
        and not own_reached
        and not board_copy
        and not any(r.transient for _, r in results)
    ):
        for cand in fallbacks[:MAX_HINTS]:
            res = walk(cand.url)
            if (
                res.status == ApplyStatus.live
                and classes.tier(res.final_url) == HostTier.ats
                and ats_posting(res.final_url or "")
            ):
                results.append((cand, res))
                break
            if res.transient:
                fallback_failed.append(res.error or "transient failure")
    if not results:
        raise ValueError(f"job_group {group_id}: no start URL could be tried")
    win_cand, best = rank_results(results, classes)[0]
    # A live answer on the employer's site is kept even when another start failed: it is
    # settled for retries (apply_attempts), so deferring it would only throw it away (c369bcc).
    on_employer = (
        best.status == ApplyStatus.live
        and not best.login
        and classes.tier(best.final_url) <= HostTier.employer
    )
    if defer_transient:
        failed = [r.error or "transient failure" for _, r in results if r.transient]
        own_failed = [r for c, r in results if r.transient and c.hint is None]
        if own_failed and win_cand.hint is not None and not (on_employer and win_cand.by_id):
            # A link from the text never stands in for the job's own link on a bad night,
            # unless it names the job by id number and is live on the employer's site or ATS
            # (c369bcc keeps such an answer). Title words alone also match a same-title
            # opening elsewhere (bug 5c1d738).
            raise Deferred(group_id, failed)
        if (
            (failed or fallback_failed)
            and not on_employer
            and not _settled(win_cand, best, classes)
        ):
            raise Deferred(group_id, failed + fallback_failed)
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
        from_hint=win_cand.hint == "fallback",
    )
    answered = best.status in (ApplyStatus.live, ApplyStatus.expired) or best.login
    if only_if_settled and existing is not None and not answered:
        return existing
    if not only_if_better:
        _save(conn, link)
        return link
    # Compare with the row as it is now, under the write lock: another walk, a capture or a
    # re-verify may have stored a better link while this one ran (c369bcc review).
    with _txn(conn):
        current = get_apply_link(conn, group_id)
        if current is not None and _keeps(current, link, classes):
            return current
        _save(conn, link)
    return link


def _keeps(current: ApplyLink, new: ApplyLink, classes: HostClasses) -> bool:
    """``only_if_better``: whether the stored row stays. A link the user set or a capture
    supplied is never replaced by a ranked one; otherwise the new link must be no worse."""
    if current.chosen_by == "user":
        return True
    if current.chosen_by == "capture" and new.chosen_by != "capture":
        return True
    if (
        new.from_hint
        and not current.from_hint
        and current.status == ApplyStatus.live
        and not is_login_link(current)
    ):
        return True  # a link to another opening never replaces a live link to this job
    return link_rank(new, classes) > link_rank(current, classes)


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


def ends_at_own_sign_in(link: ApplyLink) -> bool:
    """A walk that reached an ATS page which sent it on to that ATS's own sign-in page
    (``_own_sign_in``): the stored link is the ATS page, and the last hop is the sign-in
    step, never requested (bug 5c1d738)."""
    if not link.chain or not link.final_url:
        return False
    before = link.chain[-2].url if len(link.chain) >= 2 else link.start_url
    last = link.chain[-1].url
    return _own_sign_in(before, last) and match_ats(link.final_url) is match_ats(last)


def is_login_link(link: ApplyLink) -> bool:
    """A link stopped at a login wall, or an older row that took one for the posting. The
    ATS's own sign-in step after the posting (``ends_at_own_sign_in``) is not a wall: the
    link is the posting."""
    if link.chosen_by == "user":
        return False
    if link.error and link.error.startswith(LOGIN_ERROR):
        return True
    hops = link.chain[:-1] if ends_at_own_sign_in(link) else link.chain
    return is_login_wall(link.final_url) or any(is_login_wall(h.url) for h in hops)


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
            if _own_sign_in(url, resp.location):
                # The walk's rule: the ATS's own sign-in step means the posting is there
                # (bug 5c1d738). The sign-in page is never requested.
                link.status, link.verified_at, link.error = ApplyStatus.live, now, None
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
