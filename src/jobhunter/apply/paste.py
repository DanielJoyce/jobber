"""New packet from a URL or pasted text (specs/017 "New packet from a URL or text").

``insert_pasted_posting`` writes a ``paste-manual`` job group whose one job sits at stage
``normalized`` with its group already set, so nothing in the nightly run picks it up: grouping
takes only ungrouped jobs, the prefilter only grouped ones (and never ``paste-manual``), and the
screen never selects a manual source unless the group is named (core/manual_sources). It is
scored only by Score this group now (``apply/score.py``). Unlike ``detail.paste_description``
it never bumps ``description_rev``, so it never queues a re-score.

``fetch_posting`` is the optional **Fetch posting text** button. It goes through
``FetchContext``, so robots.txt decides; a refusal or an empty page means the user pastes.
"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from selectolax.lexbor import LexborHTMLParser

from jobhunter.apply import packets
from jobhunter.console.detail import MAX_PASTE_CHARS, pasted_html
from jobhunter.core import db
from jobhunter.core.fetch import FetchError, RobotsDisallowed
from jobhunter.core.manual_sources import PASTE_MANUAL
from jobhunter.core.textnorm import html_to_text
from jobhunter.pipeline.ats_rules import host_of, is_http_url, match_ats, unwrap
from jobhunter.pipeline.board_ids import board_key, is_board_host
from jobhunter.pipeline.dedupe import _refresh_group, normalize_employer
from jobhunter.pipeline.dedupe_url import _identifies_job, normalize_apply_url
from jobhunter.pipeline.dedupe_xstate import normalize_title
from jobhunter.pipeline.locations import apply_job_locations
from jobhunter.pipeline.normalize import normalize_job
from jobhunter.pipeline.posting_urls import is_posting_url

SYNTHETIC_PREFIX = "paste:"  # job.url when no URL was given; never shown or opened
MIN_FETCHED_CHARS = 200  # less than this is a JavaScript shell or an error page: paste instead
_MAX_UNWRAP = 3

CtxFactory = Callable[[str], Any]


class PasteError(ValueError):
    """Bad New packet input; the message is shown on the form."""


# ─── URLs ───────────────────────────────────────────────────────────────────


def direct_board_url(url: str) -> str:
    """See ``_direct_board_url``; any malformed URL becomes a PasteError for the form."""
    try:
        out = _direct_board_url(url)
        urlsplit(out).port  # noqa: B018  (raises on an out-of-range port)
    except PasteError:
        raise
    except ValueError as exc:
        raise PasteError(f"that URL is not valid ({exc})") from exc
    return out


def _direct_board_url(url: str) -> str:
    """The URL to store and fetch: tracking wrappers unwrapped, embed URLs made direct.

    Greenhouse ``/embed/job_app?for=<board>&token=<id>`` becomes ``/<board>/jobs/<id>`` (robots
    disallows ``/embed/``); an Ashby ``.../embed`` page becomes the posting it embeds.
    """
    url = url.strip()
    for _ in range(_MAX_UNWRAP):
        inner = unwrap(url)
        if inner is None:
            break
        url = inner
    if not is_http_url(url):
        raise PasteError("the URL must start with http:// or https://")
    p = urlsplit(url)
    rule = match_ats(url)
    query = dict(parse_qsl(p.query, keep_blank_values=True))
    if rule is not None and rule.name == "greenhouse" and "/embed/" in p.path + "/":
        board, token = query.get("for"), query.get("token")
        if board and token and p.path.rstrip("/").endswith("/embed/job_app"):
            return urlunsplit(("https", p.netloc, f"/{board}/jobs/{token}", "", ""))
    if rule is not None and rule.name == "ashby":
        path = re.sub(r"/embed/?$", "", p.path)
        kept = [(k, v) for k, v in parse_qsl(p.query) if "embed" not in k.lower()]
        return urlunsplit(("https", p.netloc, path, urlencode(kept), ""))
    return url


def is_synthetic(url: str | None) -> bool:
    return bool(url) and str(url).startswith(SYNTHETIC_PREFIX)


# ─── duplicates ─────────────────────────────────────────────────────────────


SAME_URL = "same URL"  # a URL that identifies one posting
SAME_BOARD_ID = "same board id"  # LinkedIn / Indeed job id (specs/017 1e)
SAME_SITE_URL = "same URL, not one posting"  # a careers home or board page
SAME_EMPLOYER_TITLE = "same employer and title"
_STRONG = (SAME_URL, SAME_BOARD_ID)
_ORDER = {SAME_BOARD_ID: 0, SAME_URL: 1, SAME_SITE_URL: 2, SAME_EMPLOYER_TITLE: 3}


@dataclass
class Duplicate:
    group_id: int
    title: str
    employer: str
    why: str  # one of the SAME_* reasons above
    packet_id: int | None = None

    @property
    def by_url(self) -> bool:
        """The same posting for sure (one-posting URL or board id): no "create anyway"."""
        return self.why in _STRONG


def _key(url: str | None) -> str | None:
    """``normalize_apply_url``, or None for a URL it cannot parse (an out-of-range port)."""
    try:
        return normalize_apply_url(url) if url else None
    except ValueError:
        return None


def _url_matches(conn: sqlite3.Connection, key: str) -> set[int]:
    """Groups whose posting, page or apply URLs normalize to ``key``.

    A key that names no single posting (a careers home, a board home) is matched against
    posting and page URLs only: an apply destination of that shape (a LinkedIn Apply button
    that goes to the careers home) says nothing about which posting it was.
    """
    host = host_of(key)
    # Apply destinations count only when they certainly name one posting (posting_urls): a
    # careers path that several board jobs Apply to says nothing about which posting it was.
    one = is_posting_url(key)
    rows = conn.execute(
        "SELECT j.job_group_id AS gid, j.url, j.page_url, j.apply_url FROM job j "
        "WHERE j.job_group_id IS NOT NULL AND (instr(lower(j.url), ?) > 0 "
        "OR instr(lower(coalesce(j.apply_url, '')), ?) > 0 "
        "OR instr(lower(coalesce(j.page_url, '')), ?) > 0)",
        (host, host, host),
    ).fetchall()
    found = {
        int(r[0]) for r in rows if key in (_key(r[1]), _key(r[2])) or (one and key == _key(r[3]))
    }
    if one:
        for r in conn.execute(
            "SELECT a.job_group_id, a.start_url, a.final_url FROM apply_link a "
            "WHERE instr(lower(a.start_url), ?) > 0 "
            "OR instr(lower(coalesce(a.final_url, '')), ?) > 0",
            (host, host),
        ):
            if key in (_key(r[1]), _key(r[2])):
                found.add(int(r[0]))
    return found


def board_groups(conn: sqlite3.Connection, key: str) -> list[int]:
    """Groups holding this board job id: through ``job_board_ref`` (to the job's current
    group; a job not grouped yet is skipped), then through stored board URLs."""
    board, _, bid = key.partition(":")
    found: list[int] = []
    for r in conn.execute(
        "SELECT j.job_group_id FROM job_board_ref r JOIN job j ON j.id = r.job_id "
        "WHERE r.board = ? AND r.board_id = ? AND j.job_group_id IS NOT NULL",
        (board, bid),
    ):
        found.append(int(r[0]))
    for r in conn.execute(
        "SELECT job_group_id, url, apply_url, page_url FROM job WHERE job_group_id IS NOT NULL "
        "AND (instr(url, ?) > 0 OR instr(coalesce(apply_url, ''), ?) > 0 "
        "OR instr(coalesce(page_url, ''), ?) > 0)",
        (bid, bid, bid),
    ):
        if key in (board_key(r[1]), board_key(r[2]), board_key(r[3])):
            found.append(int(r[0]))
    return list(dict.fromkeys(found))


def find_duplicates(
    conn: sqlite3.Connection,
    url: str | None,
    employer: str,
    title: str,
    *,
    urls: tuple[str | None, ...] = (),
    board_keys: tuple[str, ...] = (),
) -> list[Duplicate]:
    """Existing groups for this posting, the surest match first.

    By board id (LinkedIn / Indeed, through ``job_board_ref`` and stored board URLs), by
    normalized URL (``url`` and any ``urls``: a capture's chosen and page URLs; a URL that
    names no single posting, such as a careers home, is a weaker match), or by employer and
    title. ``board_keys`` are added to those found in the URLs themselves.
    """
    found: dict[int, str] = {}

    def note(gid: int, why: str) -> None:
        if gid not in found or _ORDER[why] < _ORDER[found[gid]]:
            found[gid] = why

    all_urls = [u for u in (url, *urls) if u]
    keys = {k for u in all_urls if (k := board_key(u))} | set(board_keys)
    for k in sorted(keys):
        for gid in board_groups(conn, k):
            note(gid, SAME_BOARD_ID)
    for key in dict.fromkeys(k for u in all_urls if (k := _key(u))):
        why = SAME_URL if _identifies_job(key) else SAME_SITE_URL
        for gid in _url_matches(conn, key):
            note(gid, why)
    emp, tit = normalize_employer(employer), normalize_title(title)
    if emp and tit:
        for r in conn.execute(
            "SELECT g.id, j.title, j.employer, j.agency_raw FROM job_group g "
            "JOIN job j ON j.id = g.canonical_job_id"
        ):
            if normalize_title(r["title"]) == tit and (
                normalize_employer(r["employer"] or r["agency_raw"]) == emp
            ):
                note(int(r["id"]), SAME_EMPLOYER_TITLE)
    out: list[Duplicate] = []
    for gid, why in found.items():
        row = conn.execute(
            "SELECT j.title, j.employer, j.agency_raw FROM job_group g "
            "JOIN job j ON j.id = g.canonical_job_id WHERE g.id = ?",
            (gid,),
        ).fetchone()
        if row is None:
            continue
        out.append(
            Duplicate(
                gid,
                row["title"],
                row["employer"] or row["agency_raw"] or "employer not stated",
                why,
                packets.live_packet_id(conn, gid),
            )
        )
    out.sort(key=lambda d: (_ORDER[d.why], d.group_id))
    return out


# ─── writes ─────────────────────────────────────────────────────────────────


def _iso(now: datetime) -> str:
    return now.astimezone(UTC).isoformat()


def _ensure_source(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO source (key, class, name, family, tier, entry, policy, status) "
        "VALUES (?, 'C', 'Pasted posting', 'manual', 'manual', 'console', 'manual', 'manual')",
        (PASTE_MANUAL,),
    )


def insert_pasted_posting(
    conn: sqlite3.Connection,
    *,
    url: str | None,
    text: str | None,
    employer: str,
    title: str,
    now: datetime,
    salary_raw: str | None = None,
    location_raw: str | None = None,
    posted_at: str | None = None,
    closes_at: str | None = None,
    employment_type: str | None = None,
    page_url: str | None = None,
    apply_url: str | None = None,
    locate: bool = False,
) -> int:
    """Write the ``paste-manual`` group (no transaction of its own); returns the group id.

    The job's ``url`` is the given URL, or a placeholder the caller replaces with
    ``paste:<packet_id>``. An ``apply_link`` row (unresolved: the URL as given, no request
    made) is written when a URL was given; ``apply_url`` (a capture's decoded off-site
    destination) is preferred for it. The optional fields are a capture's mapped facts
    (specs/017 1e); ``locate`` writes ``job_locations`` now instead of at the nightly run.
    The group is scored only on request (``job_group.score_on_request``).
    """
    employer, title = employer.strip(), title.strip()
    if not employer or not title:
        raise PasteError("employer and title are both required")
    text = (text or "").strip()
    url = direct_board_url(url) if url and url.strip() else None
    if not url and not text:
        raise PasteError("give the posting URL or paste the posting text (or both)")
    at = _iso(now)
    _ensure_source(conn)
    jid = int(
        conn.execute(
            "INSERT INTO job (source_key, external_id, url, title, employer, description_raw, "
            "description_completeness, needs_resolve, stage, first_seen_at, last_seen_at, "
            "salary_raw, location_raw, posted_at, closes_at, employment_type, page_url, "
            "apply_url) VALUES (?, ?, ?, ?, ?, ?, ?, 0, 'normalized', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                PASTE_MANUAL,
                uuid.uuid4().hex,
                url or f"{SYNTHETIC_PREFIX}pending",
                title,
                employer,
                pasted_html(text[:MAX_PASTE_CHARS]) if text else None,
                "pasted" if text else "partial",
                at,
                at,
                salary_raw,
                location_raw,
                posted_at,
                closes_at,
                employment_type or "unknown",
                page_url if page_url and page_url != url else None,
                apply_url,
            ),
        ).lastrowid
        or 0
    )
    normalize_job(conn, jid)
    if locate:
        apply_job_locations(conn, jid)
    # 'manual' keeps the URL and cross-state merges away from it, like the email-manual groups.
    gid = int(
        conn.execute(
            "INSERT INTO job_group (canonical_job_id, member_count, method, created_at, "
            "score_on_request) VALUES (?, 1, 'manual', ?, 1)",
            (jid, at),
        ).lastrowid
        or 0
    )
    conn.execute("UPDATE job SET job_group_id = ? WHERE id = ?", (gid, jid))
    start = apply_url or url
    if start:
        rule = match_ats(start)
        conn.execute(
            "INSERT INTO apply_link (job_group_id, start_url, final_url, chain, ats, "
            "employer_host, status, resolved_at) VALUES (?, ?, NULL, '[]', ?, ?, 'unresolved', ?)",
            (gid, start, rule.name if rule else None, host_of(start), at),
        )
    return gid


def create_pasted_packet(
    conn: sqlite3.Connection,
    *,
    url: str | None,
    text: str | None,
    employer: str,
    title: str,
    now: datetime,
) -> tuple[int, int]:
    """New packet for a pasted posting, in one transaction: (group id, packet id)."""
    with db.transaction(conn):
        gid = insert_pasted_posting(
            conn, url=url, text=text, employer=employer, title=title, now=now
        )
        pid = packets.prepare_in_txn(conn, gid, now)
        conn.execute(
            "UPDATE job SET url = ? WHERE job_group_id = ? AND url = ?",
            (f"{SYNTHETIC_PREFIX}{pid}", gid, f"{SYNTHETIC_PREFIX}pending"),
        )
    return gid, pid


def store_posting_text(conn: sqlite3.Connection, group_id: int, text: str, now: datetime) -> None:
    """The packet page's "paste the posting text" for a group that has none.

    Unlike ``detail.paste_description`` it does not bump ``description_rev``: a packet needs the
    text for tailoring, and that must not queue a paid re-score.
    """
    with db.transaction(conn):
        store_posting_text_in_txn(conn, group_id, text, now)


_FILLABLE = ("salary_raw", "location_raw", "posted_at", "closes_at")


def store_posting_text_in_txn(
    conn: sqlite3.Connection,
    group_id: int,
    text: str,
    now: datetime,
    fields: dict[str, str | None] | None = None,
) -> None:
    """``store_posting_text`` inside the caller's transaction (a capture's Add this description,
    specs/017 1e). ``fields`` (salary_raw, location_raw, posted_at, closes_at, employment_type)
    fill only columns that are still empty."""
    text = text.strip()
    if not text:
        raise PasteError("paste the posting text")
    row = conn.execute(
        "SELECT canonical_job_id FROM job_group WHERE id = ?", (group_id,)
    ).fetchone()
    if row is None or row[0] is None:
        raise KeyError(f"no such job group: {group_id}")
    jid = row[0]
    conn.execute(
        "UPDATE job SET description_raw = ?, description_completeness = 'pasted', "
        "needs_resolve = 0, last_seen_at = ? WHERE id = ?",
        (pasted_html(text[:MAX_PASTE_CHARS]), _iso(now), jid),
    )
    fields = fields or {}
    for col in _FILLABLE:
        if fields.get(col):
            conn.execute(
                f"UPDATE job SET {col} = ? WHERE id = ? AND coalesce({col}, '') = ''",
                (fields[col], jid),
            )
    if fields.get("employment_type"):
        conn.execute(
            "UPDATE job SET employment_type = ? WHERE id = ? AND employment_type = 'unknown'",
            (fields["employment_type"], jid),
        )
    normalize_job(conn, jid)
    if fields.get("location_raw"):
        apply_job_locations(conn, jid)
    _refresh_group(conn, group_id)


# ─── Fetch posting text ─────────────────────────────────────────────────────


@dataclass
class Fetched:
    text: str
    title: str | None = None
    employer: str | None = None


_TITLE_AT = re.compile(r"^(?:job application for\s+)?(?P<title>.+?)\s+at\s+(?P<emp>.+)$", re.I)
_DROP = ("script", "style", "noscript", "nav", "header", "footer", "form", "svg", "template")


def guess_from_page_title(page_title: str | None) -> tuple[str | None, str | None]:
    """(title, employer) from a page title such as "Job Application for X at Acme"."""
    if not page_title:
        return None, None
    t = " ".join(page_title.split())
    m = _TITLE_AT.match(t)
    if m:
        return m.group("title").strip(), m.group("emp").strip()
    return t or None, None


def guess_employer_from_url(url: str) -> str | None:
    """The board slug of a Greenhouse, Lever, Ashby or Workable posting URL ("acme-corp" ->
    "Acme Corp")."""
    rule = match_ats(url)
    if rule is None or rule.name not in ("greenhouse", "lever", "ashby", "workable"):
        return None
    if rule.name == "workable" and not rule.is_posting(url):
        return None  # /j/<code> share links and /api/ widget URLs name no account
    seg = urlsplit(url).path.strip("/").split("/")[0]
    return seg.replace("-", " ").replace("_", " ").title() if seg else None


def _json_ld_posting(tree: LexborHTMLParser) -> dict[str, Any] | None:
    for node in tree.css('script[type="application/ld+json"]'):
        try:
            data = json.loads(node.text() or "")
        except ValueError:
            continue
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            items = data.get("@graph", [data])
        else:
            continue  # a scalar (null, a string): a site bug, not a posting
        for item in items if isinstance(items, list) else []:
            if isinstance(item, dict) and item.get("@type") == "JobPosting":
                return item
    return None


def extract_posting(html: str) -> Fetched:
    """Posting text, title and employer from a posting page (JSON-LD first, then the page)."""
    tree = LexborHTMLParser(html)
    ld = _json_ld_posting(tree)
    page_title = tree.css_first("title")
    title, employer = guess_from_page_title(page_title.text() if page_title else None)
    if ld is not None:
        org = ld.get("hiringOrganization")
        if isinstance(org, dict) and isinstance(org.get("name"), str):
            employer = org["name"].strip() or employer
        if isinstance(ld.get("title"), str):
            title = ld["title"].strip() or title
        if isinstance(ld.get("description"), str):
            text = html_to_text(ld["description"])
            if len(text) >= MIN_FETCHED_CHARS:
                return Fetched(text, title, employer)
    for tag in _DROP:
        for node in tree.css(tag):
            node.decompose()
    body = tree.css_first("main") or tree.css_first("article") or tree.body
    text = html_to_text(body.html) if body is not None else ""
    return Fetched(text, title, employer)


def fetch_posting(ctx_factory: CtxFactory, url: str) -> Fetched:
    """Fetch and extract a posting page through ``FetchContext`` (robots.txt respected).

    Raises PasteError with a message for the form: robots refusal, fetch failure, or a page
    with no posting text (many boards render it with JavaScript).
    """
    url = direct_board_url(url)
    host = host_of(url)
    if is_board_host(host):
        # LinkedIn and Indeed are never requested, not even robots.txt (specs/017 1e).
        raise PasteError(
            f"jobhunter does not fetch {host}; paste the posting text, or send the page "
            "from the browser extension"
        )
    if "/embed/" in urlsplit(url).path:
        raise PasteError(f"{host} does not allow fetching embed pages; paste the posting text")
    try:
        with closing(ctx_factory(url)) as ctx:
            resp = ctx.get(url)
    except RobotsDisallowed as exc:
        raise PasteError(
            f"{host}'s robots.txt does not allow fetching this page; paste the posting text"
        ) from exc
    except FetchError as exc:
        raise PasteError(f"could not fetch {host} ({exc}); paste the posting text") from exc
    if resp.status >= 400:
        raise PasteError(f"{host} answered {resp.status}; paste the posting text")
    got = extract_posting(resp.text)
    if len(got.text) < MIN_FETCHED_CHARS:
        raise PasteError(
            f"{host} returned no posting text (it may load it with JavaScript); "
            "paste the posting text"
        )
    got.employer = got.employer or guess_employer_from_url(url)
    return got
