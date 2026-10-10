"""Apply-link rule tables (specs/015-apply-links.md#resolution): data, plus tiny matchers.

Three tables, all meant to grow as new chains show up (`jobhunter applylinks unknown`):

- ``REDIRECTORS``: tracking hosts that carry the destination in a query parameter. Unwrapped
  locally, with no request.
- ``ATS_RULES``: applicant tracking systems the resolver stops at, and how each one's posting URL
  becomes its apply URL.
- ``EXPIRED_PHRASES``: text on a final page that means the posting is closed.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import parse_qsl, unquote, urlsplit, urlunsplit


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def host_matches(host: str, pattern: str) -> bool:
    """``*.example.com`` matches any subdomain (not the apex); anything else matches exactly."""
    host = host.lower()
    if pattern.startswith("*."):
        return host.endswith(pattern[1:])
    return host == pattern


def is_http_url(value: str) -> bool:
    parts = urlsplit(value)
    return parts.scheme in ("http", "https") and bool(parts.netloc)


# ─── redirectors ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Redirector:
    host: str  # host pattern, see host_matches
    params: tuple[str, ...]  # destination parameters, tried in order
    path_prefix: str = ""  # only unwrap under this path


REDIRECTORS: tuple[Redirector, ...] = (
    Redirector("click.appcast.io", ("url", "dest", "u")),
    Redirector("*.appcast.io", ("url", "dest", "u")),
    Redirector("*.jobs2web.com", ("url", "dest", "redirect")),
    Redirector("www.google.com", ("q", "url"), "/url"),
    Redirector("l.facebook.com", ("u",), "/l.php"),
    Redirector("lm.facebook.com", ("u",), "/l.php"),
    Redirector("*.safelinks.protection.outlook.com", ("url",)),
    Redirector("www.linkedin.com", ("url",), "/redir"),
    Redirector("out.reddit.com", ("url",)),
)

# Any host (except a known ATS): a parameter with one of these names whose value is an absolute
# http(s) URL is taken as the destination.
GENERIC_DEST_PARAMS: tuple[str, ...] = (
    "url",
    "dest",
    "destination",
    "target",
    "u",
    "redirect",
    "redirect_url",
    "r",
)


def _as_url(value: str) -> str | None:
    value = value.strip()
    # Some trackers double-encode: one parse_qsl decode leaves "https%3A%2F%2F...".
    for _ in range(3):
        if is_http_url(value):
            return value
        if "%" not in value:
            return None
        value = unquote(value)
    return value if is_http_url(value) else None


def unwrap(url: str) -> str | None:
    """The destination a tracking link carries in its query, or None. Never makes a request."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if not parts.query or match_ats(url) is not None:
        return None
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    lowered = {k.lower(): v for k, v in query.items()}
    for r in REDIRECTORS:
        if host_matches(host, r.host) and parts.path.startswith(r.path_prefix):
            for name in r.params:
                if name in lowered and (dest := _as_url(lowered[name])):
                    return dest
    for name in GENERIC_DEST_PARAMS:
        if name in lowered and (dest := _as_url(lowered[name])):
            return dest
    return None


# ─── applicant tracking systems ────────────────────────────────────────────


def _append_segment(segment: str) -> Callable[[str], str]:
    """Canonicalizer: posting URL + ``/segment`` (never twice), fragment dropped."""

    def canon(url: str) -> str:
        p = urlsplit(url)
        path = p.path.rstrip("/")
        if path.endswith(f"/{segment}") or f"/{segment}/" in path:
            return urlunsplit((p.scheme, p.netloc, path, p.query, ""))
        return urlunsplit((p.scheme, p.netloc, f"{path}/{segment}", p.query, ""))

    return canon


def _with_fragment(fragment: str) -> Callable[[str], str]:
    def canon(url: str) -> str:
        p = urlsplit(url)
        return urlunsplit((p.scheme, p.netloc, p.path, p.query, fragment))

    return canon


def _as_is(url: str) -> str:
    return url


@dataclass(frozen=True)
class AtsRule:
    name: str
    hosts: tuple[str, ...]
    # A posting (vs. a board home or search page). Only postings are canonicalized.
    posting: re.Pattern[str]
    canonicalize: Callable[[str], str]
    # False: trust the URL as given and make no request (USA Staffing via USAJOBS ApplyURI).
    fetch: bool = True

    def is_posting(self, url: str) -> bool:
        return bool(self.posting.search(urlsplit(url).path))

    def canonical(self, url: str) -> str:
        return self.canonicalize(url) if self.is_posting(url) else url


ATS_RULES: tuple[AtsRule, ...] = (
    AtsRule(
        "workday",
        ("*.myworkdayjobs.com", "*.myworkdaysite.com"),
        re.compile(r"/(?:job|details)/[^/]+"),
        _append_segment("apply"),
    ),
    AtsRule(
        "greenhouse",
        ("boards.greenhouse.io", "job-boards.greenhouse.io", "job-boards.eu.greenhouse.io"),
        re.compile(r"^/[^/]+/jobs/\d+"),
        _with_fragment("app"),
    ),
    AtsRule(
        "lever",
        ("jobs.lever.co", "jobs.eu.lever.co"),
        re.compile(r"^/[^/]+/[0-9a-f-]{20,}"),
        _append_segment("apply"),
    ),
    AtsRule(
        "ashby",
        ("jobs.ashbyhq.com",),
        re.compile(r"^/[^/]+/[0-9a-f-]{20,}"),
        _append_segment("application"),
    ),
    # Postings live at /<account>/j/<shortcode>/, the form at .../apply/ (specs/017, 1d).
    AtsRule(
        "workable",
        ("apply.workable.com",),
        re.compile(r"^/[^/]+/j/[0-9A-Za-z]+"),
        _append_segment("apply"),
    ),
    AtsRule("icims", ("*.icims.com",), re.compile(r"/jobs/\d+"), _as_is),
    AtsRule(
        "smartrecruiters",
        ("jobs.smartrecruiters.com",),
        re.compile(r"^/[^/]+/\d+"),
        _as_is,
    ),
    AtsRule("taleo", ("*.taleo.net",), re.compile(r"jobdetail|requisition", re.I), _as_is),
    AtsRule(
        "neogov",
        ("governmentjobs.com", "www.governmentjobs.com", "schooljobs.com", "www.schooljobs.com"),
        re.compile(r"/jobs/\d+"),
        _append_segment("apply"),
    ),
    AtsRule("usastaffing", ("apply.usastaffing.gov",), re.compile(r"."), _as_is, fetch=False),
)


def match_ats(url: str) -> AtsRule | None:
    host = host_of(url)
    for rule in ATS_RULES:
        if any(host_matches(host, pattern) for pattern in rule.hosts):
            return rule
    return None


# ─── closed postings ───────────────────────────────────────────────────────

EXPIRED_STATUSES = frozenset({404, 410})

EXPIRED_PHRASES: tuple[str, ...] = (
    "no longer accepting applications",
    "this job has expired",
    "this job posting has expired",
    "position has been filled",
    "job is no longer available",
    "position is no longer available",
    "posting is no longer available",
    "this posting has closed",
    "this job posting is closed",
    "this job is closed",
)
