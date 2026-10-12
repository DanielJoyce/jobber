"""Matching an application form to a job (specs/022 "Matching the page to a job").

:func:`match` decides which job group a frame's form belongs to, from evidence in this order;
the first that decides wins:

1. The fill session already bound this tab and frame: on an ATS, the frame's posting key equals
   the session's (both non-NULL); on Fill this page (no posting key), the worker's ``tab_id``
   equals the session's and the page URL, normalized by ``normalize_apply_url``, equals the
   session's ``start_url``. An ATS session with a *different* posting key asks: the frame now
   shows another job's form.
2. An ``ats_posting_ref`` row for the posting key (your earlier pick or add). ATS pages only.
3. On an ATS, the posting key equals the key of a group's ``apply_link`` final or start URL, a
   ``job.url``, ``job.apply_url`` or ``job.page_url``, or a ``job_board_ref.apply_url``. On Fill
   this page, the normalized page URL equals one of those URLs normalized the same way, and
   ``capture.identifies_posting`` says the URL names one posting. Decides when exactly one
   group matches; several ask.
4. An ``apply_click`` in the last 24 hours whose final URL has this posting key: exactly one
   group decides.
5. Anything else never decides: you pick, or Add this job.

The **posting key** is the ATS plus its posting id, the same for every variant of one posting:
Greenhouse board and job id (an embed's ``for`` and ``token`` mapped by
``paste.direct_board_url``), Lever company and posting id, Ashby organization and job id,
Workable account and shortcode, Workday tenant, site and requisition id. Employer and title
never match.

``tab_id`` always comes from the service worker (``sender.tab.id`` for a content script, the
tab bound to the panel or popup window for an extension page). Nothing the page sends is ever
read as a tab id.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from urllib.parse import parse_qsl, urlsplit

from jobhunter.apply import ats_forms, capture, paste
from jobhunter.pipeline.ats_rules import match_ats
from jobhunter.pipeline.dedupe_url import normalize_apply_url
from jobhunter.pipeline.listing import from_iso, to_iso

CLICK_WINDOW = timedelta(hours=24)
MAX_CANDIDATES = 10

RULE_SESSION = 1
RULE_POSTING_REF = 2
RULE_URL = 3
RULE_CLICK = 4
RULE_NONE = 5

# ─── posting keys ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PostingKey:
    ats: str
    parts: tuple[str, ...]
    needle: str  # the posting id: a cheap LIKE filter before keys are compared

    @property
    def key(self) -> str:
        return ":".join((self.ats, *self.parts))


_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_GH_POSTING = re.compile(r"^/([^/]+)/jobs/(\d+)(?:/|$)")
_LEVER_POSTING = re.compile(rf"^/([^/]+)/({_UUID})(?:/|$)")
_ASHBY_POSTING = re.compile(rf"^/([^/]+)/({_UUID})(?:/|$)")
_ASHBY_BOARD = re.compile(r"^/([^/]+)(?:/embed)?/?$")
_WORKABLE_POSTING = re.compile(r"^/([^/]+)/j/([0-9A-Za-z]+)(?:/|$)")
_LOCALE = re.compile(r"^[a-z]{2}(?:-[A-Za-z]{2})?$")
_WORKDAY_STEPS = frozenset({"apply", "applymanually", "autofillwithresume", "usemylastapplication"})


def _direct(url: str) -> str | None:
    """Tracking wrappers unwrapped and embeds made direct (``paste.direct_board_url``)."""
    try:
        return paste.direct_board_url(url)
    except (paste.PasteError, ValueError):
        return None


def posting_key(url: str | None) -> PostingKey | None:
    """The posting key of a URL on a supported ATS, or None (another site, or an ATS page that
    names no one posting: a board home, search, or a sign-in page)."""
    if not url or not isinstance(url, str):
        return None
    direct = _direct(url)
    if direct is None:
        return None
    rule = match_ats(direct)
    if rule is None or rule.name not in ats_forms.SUPPORTED_ATS:
        return None
    try:
        parts = urlsplit(direct)
    except ValueError:
        return None
    path = parts.path
    if rule.name == "greenhouse":
        m = _GH_POSTING.match(path)
        if m and m.group(1).lower() != "embed":
            board, job = m.group(1).lower(), m.group(2)
            return PostingKey("greenhouse", (board, job), job)
        return None
    if rule.name == "lever":
        m = _LEVER_POSTING.match(path.lower())
        return PostingKey("lever", (m.group(1), m.group(2)), m.group(2)) if m else None
    if rule.name == "ashby":
        low = path.lower()
        m = _ASHBY_POSTING.match(low)
        if m:
            return PostingKey("ashby", (m.group(1), m.group(2)), m.group(2))
        # An employer page's embed names the job in ashby_jid.
        b = _ASHBY_BOARD.match(low)
        jid = dict(parse_qsl(parts.query)).get("ashby_jid", "").lower()
        if b and re.fullmatch(_UUID, jid):
            return PostingKey("ashby", (b.group(1), jid), jid)
        return None
    if rule.name == "workable":
        m = _WORKABLE_POSTING.match(path)
        if m:
            code = m.group(2).upper()
            return PostingKey("workable", (m.group(1).lower(), code), code)
        return None
    if rule.name == "workday":
        return _workday_key(parts.hostname or "", path)
    return None


def _workday_key(host: str, path: str) -> PostingKey | None:
    """``<tenant>.wdN.myworkdayjobs.com/[locale/]<site>/job/<location>/<title>_<req>[/apply...]``
    or ``wdN.myworkdaysite.com/[locale/]recruiting/<tenant>/<site>/job/...``; ``details`` in place
    of ``job`` too. The requisition id is the part of the last posting segment after ``_``."""
    segs = [s for s in path.split("/") if s]
    if segs and _LOCALE.match(segs[0]):
        segs = segs[1:]
    host = host.lower()
    if host.endswith(".myworkdaysite.com"):
        if len(segs) < 3 or segs[0].lower() != "recruiting":
            return None
        tenant, segs = segs[1], segs[2:]
    else:
        tenant = host.split(".", 1)[0]
    if len(segs) < 3 or segs[1].lower() not in ("job", "details"):
        return None
    site, rest = segs[0], segs[2:]
    posting: list[str] = []
    for s in rest:
        if s.lower() in _WORKDAY_STEPS:
            break
        posting.append(s)
    if not posting:
        return None
    slug = posting[-1]
    req = slug.rsplit("_", 1)[-1] if "_" in slug else slug
    if not req:
        return None
    req = req.lower()
    return PostingKey("workday", (tenant.lower(), site.lower(), req), req)


# ─── results ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Candidate:
    group_id: int
    title: str
    employer: str


@dataclass
class MatchResult:
    decided: bool
    rule: int  # 1-4 decided it; 5 decided nothing
    kind: str  # 'ats' or 'generic'
    group_id: int | None = None
    application_id: int | None = None
    ats: str | None = None
    variant: str | None = None
    posting_key: str | None = None
    page_url: str | None = None  # normalize_apply_url of the frame URL
    identifies_posting: bool = False
    session_id: int | None = None  # the session that decided, or that asked (mismatch)
    reason: str = ""
    candidates: list[Candidate] = field(default_factory=list)


# ─── matching ──────────────────────────────────────────────────────────────


def _like(needle: str) -> str:
    return "%" + needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _key_of(url: str | None) -> str | None:
    pk = posting_key(url)
    return pk.key if pk else None


def _norm(url: str | None) -> str | None:
    try:
        return normalize_apply_url(url) if url else None
    except ValueError:
        return None


def _url_rows(conn: sqlite3.Connection, needle: str) -> list[tuple[int, str]]:
    """(group id, URL) for every stored posting URL of a group containing ``needle``."""
    like = _like(needle)
    out: list[tuple[int, str]] = []
    for r in conn.execute(
        "SELECT job_group_id, url, apply_url, page_url FROM job WHERE job_group_id IS NOT NULL "
        "AND (url LIKE :l ESCAPE '\\' OR apply_url LIKE :l ESCAPE '\\' "
        "OR page_url LIKE :l ESCAPE '\\')",
        {"l": like},
    ):
        out.extend((r[0], u) for u in r[1:] if u)
    for r in conn.execute(
        "SELECT job_group_id, start_url, final_url FROM apply_link "
        "WHERE start_url LIKE :l ESCAPE '\\' OR final_url LIKE :l ESCAPE '\\'",
        {"l": like},
    ):
        out.extend((r[0], u) for u in r[1:] if u)
    for r in conn.execute(
        "SELECT j.job_group_id, b.apply_url FROM job_board_ref b JOIN job j ON j.id = b.job_id "
        "WHERE j.job_group_id IS NOT NULL AND b.apply_url LIKE :l ESCAPE '\\'",
        {"l": like},
    ):
        out.append((r[0], r[1]))
    return out


def _groups_by_key(conn: sqlite3.Connection, pk: PostingKey) -> list[int]:
    found = {gid for gid, url in _url_rows(conn, pk.needle) if _key_of(url) == pk.key}
    return sorted(found)


def _groups_by_url(conn: sqlite3.Connection, page: str) -> list[int]:
    host = urlsplit(page).hostname or ""
    if not host:
        return []
    found = {gid for gid, url in _url_rows(conn, host) if _norm(url) == page}
    return sorted(found)


def _groups_by_click(conn: sqlite3.Connection, pk: PostingKey, now: datetime) -> list[int]:
    since = now - CLICK_WINDOW
    found: set[int] = set()
    for gid, at, url in conn.execute(
        "SELECT job_group_id, at, final_url FROM apply_click WHERE final_url LIKE ? ESCAPE '\\'",
        (_like(pk.needle),),
    ):
        try:
            when = from_iso(at)
        except (TypeError, ValueError):
            continue
        if when >= since and _key_of(url) == pk.key:
            found.add(gid)
    return sorted(found)


def _ref_group(conn: sqlite3.Connection, key: str) -> int | None:
    row = conn.execute(
        "SELECT j.job_group_id FROM ats_posting_ref r JOIN job j ON j.id = r.job_id "
        "WHERE r.posting_key = ?",
        (key,),
    ).fetchone()
    return row[0] if row and row[0] is not None else None


def _session(
    conn: sqlite3.Connection, session_id: int | None, tab_id: int | None
) -> sqlite3.Row | tuple | None:
    """The session named by id, else the newest open session the worker bound to this tab."""
    cols = (
        "SELECT s.id, s.ats, s.posting_key, s.start_url, s.tab_id, a.job_group_id "
        "FROM fill_session s JOIN application a ON a.id = s.application_id "
    )
    if session_id is not None:
        return conn.execute(cols + "WHERE s.id = ?", (session_id,)).fetchone()
    if tab_id is not None:
        return conn.execute(
            cols + "WHERE s.tab_id = ? AND s.finished_at IS NULL ORDER BY s.id DESC LIMIT 1",
            (tab_id,),
        ).fetchone()
    return None


def _candidates(conn: sqlite3.Connection, gids: list[int]) -> list[Candidate]:
    out: list[Candidate] = []
    for gid in gids[:MAX_CANDIDATES]:
        row = conn.execute(
            "SELECT j.title, j.employer FROM job_group g "
            "JOIN job j ON j.id = COALESCE(g.canonical_job_id, "
            "(SELECT MIN(id) FROM job WHERE job_group_id = g.id)) WHERE g.id = ?",
            (gid,),
        ).fetchone()
        if row is not None:
            out.append(Candidate(gid, row[0] or "", row[1] or ""))
    return out


def _application(conn: sqlite3.Connection, gid: int) -> int | None:
    row = conn.execute("SELECT id FROM application WHERE job_group_id = ?", (gid,)).fetchone()
    return row[0] if row else None


def match(
    conn: sqlite3.Connection,
    frame_url: str,
    tab_id: int | None,
    session_id: int | None = None,
    *,
    now: datetime,
) -> MatchResult:
    """Which group the form in ``frame_url`` belongs to (see the module docstring).

    ``tab_id`` is the worker's: ``sender.tab.id`` for a content script, the tab bound to the
    panel or popup window for an extension page. Never a value the page supplied."""
    ats = ats_forms.ats_of(frame_url)
    pk = posting_key(frame_url) if ats else None
    variant = ats_forms.variant_for(frame_url) if ats else None
    page = _norm(frame_url)
    res = MatchResult(
        decided=False,
        rule=RULE_NONE,
        kind="ats" if ats else "generic",
        ats=ats or "generic",
        variant=variant.name if variant else None,
        posting_key=pk.key if pk else None,
        page_url=page,
        identifies_posting=bool(page) and capture.identifies_posting(frame_url),
    )

    def decide(rule: int, gid: int, reason: str, sid: int | None = None) -> MatchResult:
        res.decided, res.rule, res.group_id, res.reason = True, rule, gid, reason
        res.application_id = _application(conn, gid)
        res.session_id = sid
        res.candidates = _candidates(conn, [gid])
        return res

    def ask(reason: str, gids: list[int]) -> MatchResult:
        res.reason = reason
        res.candidates = _candidates(conn, gids)
        return res

    # 1. The session bound to this tab and frame.
    s = _session(conn, session_id, tab_id)
    mismatch_gid: int | None = None
    if s is not None:
        sid, s_ats, s_key, s_start, s_tab, s_gid = s[0], s[1], s[2], s[3], s[4], s[5]
        if res.kind == "ats":
            if s_key is not None and pk is not None and s_key == pk.key:
                return decide(RULE_SESSION, s_gid, "this tab's fill session", sid)
            if s_key is not None and pk is not None:
                mismatch_gid, res.session_id = s_gid, sid
        elif (
            s_ats == "generic"
            and s_key is None
            and tab_id is not None
            and s_tab == tab_id
            and page is not None
            and s_start == page
        ):
            return decide(RULE_SESSION, s_gid, "this tab's fill session", sid)

    if res.kind == "ats":
        if pk is None:
            return ask("this page names no one posting", [])
        ref = _ref_group(conn, pk.key)
        by_url = _groups_by_key(conn, pk)
        clicks = _groups_by_click(conn, pk, now)
        if mismatch_gid is not None:
            # The session is for another posting: this frame shows another job's form.
            others = [g for g in (ref, *by_url, *clicks) if g is not None]
            gids = list(dict.fromkeys([*others, mismatch_gid]))
            return ask("this form is for a different posting than the session's", gids)
        # 2. Your earlier pick or add.
        if ref is not None:
            return decide(RULE_POSTING_REF, ref, "your earlier pick for this posting")
        # 3. A stored URL of exactly one group has this posting key.
        if len(by_url) == 1:
            return decide(RULE_URL, by_url[0], "a saved link to this posting")
        if len(by_url) > 1:
            return ask("several jobs link to this posting", by_url)
        # 4. Exactly one recent Open application click.
        if len(clicks) == 1:
            return decide(RULE_CLICK, clicks[0], "you opened it from jobhunter today")
        return ask("no saved job links to this posting", clicks)

    # Fill this page: rule 3 on the normalized URL, deciding only for one posting.
    if page is None:
        return ask("not a web page", [])
    by_url = _groups_by_url(conn, page)
    if not res.identifies_posting:
        return ask("this address does not name one posting", by_url)
    if len(by_url) == 1:
        return decide(RULE_URL, by_url[0], "a saved link to this page")
    if len(by_url) > 1:
        return ask("several jobs link to this page", by_url)
    return ask("no saved job links to this page", [])


# ─── your pick ─────────────────────────────────────────────────────────────


class PickError(ValueError):
    pass


@dataclass(frozen=True)
class PickResult:
    group_id: int
    job_id: int
    posting_key: str | None  # None: a page with no posting key; nothing is remembered
    written: bool


def pick(
    conn: sqlite3.Connection,
    frame_url: str,
    group_id: int,
    *,
    set_by: str = "pick",
    now: datetime,
) -> PickResult:
    """Remember your pick (or Add this job) for the frame's posting key: ``ats_posting_ref``
    names a job of the group, so the nightly merges carry it. The newest pick wins. A page with
    no posting key (Fill this page) remembers nothing."""
    if set_by not in ("pick", "add"):
        raise PickError(f"set_by must be pick or add, not {set_by!r}")
    jobs = [
        r[0]
        for r in conn.execute(
            "SELECT id FROM job WHERE job_group_id = ? ORDER BY id", (group_id,)
        ).fetchall()
    ]
    if not jobs:
        raise PickError(f"group {group_id} does not exist")
    pk = posting_key(frame_url) if ats_forms.ats_of(frame_url) else None
    if pk is None:
        return PickResult(group_id, jobs[0], None, False)
    canonical = _canonical(conn, group_id)
    job_id = _job_for(conn, jobs, pk) or (canonical if canonical in jobs else jobs[0])
    conn.execute(
        "INSERT INTO ats_posting_ref (posting_key, job_id, set_by, seen_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(posting_key) DO UPDATE SET job_id = excluded.job_id, "
        "set_by = excluded.set_by, seen_at = excluded.seen_at",
        (pk.key, job_id, set_by, to_iso(now)),
    )
    conn.commit()
    return PickResult(group_id, job_id, pk.key, True)


def _job_for(conn: sqlite3.Connection, jobs: list[int], pk: PostingKey) -> int | None:
    """The group's job whose own URL has this posting key, if one does."""
    for jid in jobs:
        row = conn.execute(
            "SELECT url, apply_url, page_url FROM job WHERE id = ?", (jid,)
        ).fetchone()
        if any(_key_of(u) == pk.key for u in row if u):
            return jid
    return None


def _canonical(conn: sqlite3.Connection, group_id: int) -> int | None:
    row = conn.execute(
        "SELECT canonical_job_id FROM job_group WHERE id = ?", (group_id,)
    ).fetchone()
    return row[0] if row and row[0] is not None else None
