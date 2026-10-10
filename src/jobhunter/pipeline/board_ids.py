"""LinkedIn and Indeed: job ids in their URLs, and the no-fetch list (specs/017 phase 1e).

jobhunter never requests a page on a board host, by any path, not even robots.txt: the apply
link resolver and its refresh skip them (``pipeline/applylink.py``), and a destination found in
a board page's Apply control is decoded locally (``ats_rules.unwrap``). ``board_key`` reduces a
board URL to its job id, so the same posting seen as ``/jobs/view/123``, ``/jobs/view/a-b-123``
and ``/jobs/search?currentJobId=123`` meets on one key (``linkedin:123``).

Lives in ``pipeline`` (not ``apply/capture.py``, which re-exports it) because nightly grouping
(``dedupe.group_pending``) uses it too.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlsplit

LINKEDIN = "linkedin"
INDEED = "indeed"
BOARDS = (LINKEDIN, INDEED)

_LINKEDIN_HOST = re.compile(r"(?:^|\.)linkedin\.com$")
# indeed.com and its subdomains (uk.indeed.com, ca.indeed.com, ...), plus the older country
# domains (indeed.co.uk, indeed.com.au, indeed.de, ...).
_INDEED_HOST = re.compile(r"(?:^|\.)indeed\.(?:com|co\.[a-z]{2}|com\.[a-z]{2}|[a-z]{2})$")
_LINKEDIN_VIEW = re.compile(r"^/(?:comm/)?jobs/view/(?:[^/]*?-)?(\d{5,})/?$")
_LINKEDIN_LIST = ("/jobs/search", "/jobs/collections")
_DIGITS = re.compile(r"^\d{5,}$")
_INDEED_ID = re.compile(r"^[0-9a-zA-Z]{8,40}$")


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def board_of_host(host: str) -> str | None:
    """'linkedin' or 'indeed' for any of their hosts, else None."""
    host = host.lower().rstrip(".")
    if _LINKEDIN_HOST.search(host):
        return LINKEDIN
    if _INDEED_HOST.search(host):
        return INDEED
    return None


def board_of(url: str | None) -> str | None:
    return board_of_host(_host(url)) if url else None


def is_board_host(host: str) -> bool:
    """On the no-fetch list: jobhunter makes no request to this host."""
    return board_of_host(host) is not None


def board_id(url: str | None) -> tuple[str, str] | None:
    """(board, job id) for a LinkedIn or Indeed job URL, or None."""
    if not url:
        return None
    try:
        p = urlsplit(url.strip())
    except ValueError:
        return None
    if p.scheme not in ("http", "https"):
        return None
    board = board_of_host(p.hostname or "")
    if board is None:
        return None
    query = dict(parse_qsl(p.query, keep_blank_values=True))
    path = p.path.rstrip("/") or "/"
    if board == LINKEDIN:
        m = _LINKEDIN_VIEW.match(path)
        if m:
            return LINKEDIN, m.group(1)
        if path.startswith(_LINKEDIN_LIST):
            jid = query.get("currentJobId", "")
            if _DIGITS.match(jid):
                return LINKEDIN, jid
        return None
    for name in ("jk", "vjk"):
        jid = query.get(name, "")
        if _INDEED_ID.match(jid):
            return INDEED, jid.lower()
    return None


def board_key(url: str | None) -> str | None:
    """``linkedin:<id>`` / ``indeed:<id>`` for a board job URL, else None."""
    got = board_id(url)
    return f"{got[0]}:{got[1]}" if got else None
