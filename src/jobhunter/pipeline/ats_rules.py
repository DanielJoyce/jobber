"""Apply-link rule tables (specs/015-apply-links.md#resolution): data, plus tiny matchers.

The tables, all meant to grow as new chains show up (`jobhunter applylinks unknown`):

- ``REDIRECTORS``: tracking hosts that carry the destination in a query parameter. Unwrapped
  locally, with no request.
- ``ATS_RULES``: applicant tracking systems the resolver stops at, and how each one's posting URL
  becomes its apply URL.
- ``OTHER_ATS``: more applicant tracking systems, known by host only (no canonical apply URL
  yet). With ``ATS_RULES`` this is the one list of ATS hosts that ranks a group's candidate
  links (bug 334d9eb); ``ats_name`` reads both.
- ``TRACKER_HOSTS``: click trackers and redirectors that are followed, never a destination.
- ``AGGREGATOR_HOSTS``: job boards and state job banks, ranked last. Class A sources' own hosts
  count too (``pipeline/apply_rank.py``).
- ``LOGIN_*``: login and single-sign-on walls, which are never the posting.
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


# Known by host only: ranked as an employer's ATS, but the resolver follows them like any page
# and keeps the final URL as reached. Move a row into ATS_RULES when it gets a canonical rule.
OTHER_ATS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("jobvite", ("jobs.jobvite.com", "*.jobvite.com")),
    ("bamboohr", ("*.bamboohr.com",)),
    ("recruitee", ("*.recruitee.com",)),
    ("breezy", ("*.breezy.hr",)),
    ("teamtailor", ("*.teamtailor.com",)),
    ("jazzhr", ("*.applytojob.com",)),
    ("successfactors", ("*.successfactors.com", "*.successfactors.eu")),
    ("oracle", ("*.oraclecloud.com",)),
    ("ultipro", ("recruiting.ultipro.com", "recruiting2.ultipro.com")),
    ("paylocity", ("recruiting.paylocity.com",)),
    ("adp", ("workforcenow.adp.com", "myjobs.adp.com")),
    ("dayforce", ("*.dayforcehcm.com",)),
    ("rippling", ("ats.rippling.com",)),
    ("pinpoint", ("*.pinpointhq.com",)),
    ("personio", ("*.jobs.personio.de", "*.jobs.personio.com")),
    ("gem", ("jobs.gem.com",)),
    ("cornerstone", ("*.csod.com",)),
    ("paycom", ("*.paycomonline.net",)),
    ("comeet", ("www.comeet.com",)),
    ("dover", ("app.dover.com",)),
    ("infor", ("*.inforcloudsuite.com",)),
)


def ats_name(url: str) -> str | None:
    """The ATS a URL is on, from ``ATS_RULES`` or ``OTHER_ATS``, or None."""
    rule = match_ats(url)
    if rule is not None:
        return rule.name
    host = host_of(url)
    for name, patterns in OTHER_ATS:
        if host_in(host, patterns):
            return name
    return None


# ─── host classes for ranking (bug 334d9eb) ────────────────────────────────

# Trackers that are followed (3xx or unwrapped) and judged by where they land. A chain that ends
# on one of these never reached the employer.
TRACKER_HOSTS: tuple[str, ...] = (
    "aplitrak.com",
    "*.aplitrak.com",
    "*.appcast.io",
    "*.jobs2web.com",
    "*.recruitics.com",
    "*.joveo.com",
    "*.pandologic.com",
    "*.clickcastnow.com",
)


def _apex_and_subdomains(*domains: str) -> tuple[str, ...]:
    return tuple(p for d in domains for p in (d, f"*.{d}"))


# THE list of job boards, aggregators and state job banks: ranked last for apply links
# (``apply_rank``), and never taken for the employer's page by a capture (``apply/capture``).
# Hosts of class A sources (the state job banks in the registry) are added at run time from the
# source table (``apply_rank.HostClasses``). LinkedIn and Indeed are in ``board_ids`` (never
# requested) and count as boards everywhere too.
AGGREGATOR_HOSTS: tuple[str, ...] = (
    # national boards and aggregators
    *_apex_and_subdomains(
        "monster.com",
        "ziprecruiter.com",
        "glassdoor.com",
        "simplyhired.com",
        "careerbuilder.com",
        "dice.com",
        "talent.com",
        "jooble.org",
        "adzuna.com",
        "builtin.com",
        "wellfound.com",
        "angel.co",
        "jobright.ai",
        "hiring.cafe",
        "otta.com",
        "welcometothejungle.com",
        "remoteok.com",
        "weworkremotely.com",
        "flexjobs.com",
        "snagajob.com",
        "lensa.com",
        "jobgether.com",
        "himalayas.app",
        "usajobs.gov",
        "clearancejobs.com",
        "theladders.com",
        "getwork.com",
    ),
    # DirectEmployers / NLx mirrors (state "jobs" sites, employer-branded dejobs.org mirrors)
    *_apex_and_subdomains("usnlx.com", "nlx.org", "dejobs.org", "jobsyn.org"),
    # state job banks seen as apply links in NLx and state feeds
    "jobs.utah.gov",
    *_apex_and_subdomains("mitalent.org", "ohiomeansjobs.com", "connectingcolorado.com"),
    "ohiomeansjobs.ohio.gov",
    "worksource.my.site.com",
    *_apex_and_subdomains("worksourcewa.com"),
    "jobs.connectingcolorado.gov",
    "labor.eightfold.ai",
)
# Not aggregators (they host employers' own postings, and NEOGOV is an ATS), but their
# og:site_name names the site, not the employer (``apply/capture``).
SITE_NAMED_HOSTS: tuple[str, ...] = _apex_and_subdomains("governmentjobs.com", "schooljobs.com")


def host_in(host: str, patterns: tuple[str, ...]) -> bool:
    return any(host_matches(host, p) for p in patterns)


# ─── login and single-sign-on walls (bug 334d9eb) ──────────────────────────

# First host label of an identity provider or login site: id.utah.gov, login.dts.utah.gov,
# sso.example.org, accounts.google.com, auth.example.com.
LOGIN_HOST_LABELS = frozenset(
    {"id", "idp", "login", "logon", "signin", "sso", "auth", "oauth", "accounts", "cas"}
)
LOGIN_HOSTS: tuple[str, ...] = (
    "*.okta.com",
    "*.auth0.com",
    "login.microsoftonline.com",
    "*.b2clogin.com",
    "id.me",
)
# A path segment that is a login page (``/login``, ``/jsp/utjobs/login.do``,
# ``/sso/login.aspx``, ``/signin``), an SSO or SAML endpoint, or an OAuth authorize step.
_LOGIN_PATH = re.compile(
    r"(?:^|/)(?:log-?in|log-?on|sign-?in|sso|saml2?|cas)(?:\.[a-z]{2,5})?(?:/|$)"
    r"|/oauth2?/(?:v\d(?:\.\d)?/)?authori[sz]e"
    r"|/protocol/openid-connect/auth",
    re.I,
)


def is_login_wall(url: str | None) -> bool:
    """A login or single-sign-on page: never the posting, whatever status it answers with."""
    if not url:
        return False
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
    except ValueError:
        return False
    if "." in host and host.split(".", 1)[0] in LOGIN_HOST_LABELS:
        return True
    if host_in(host, LOGIN_HOSTS):
        return True
    rule = match_ats(url)
    if rule is not None and rule.is_posting(url):
        return False  # an ATS's own apply step on the posting path (iCIMS .../jobs/1/x/login)
    return bool(_LOGIN_PATH.search(parts.path))


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
