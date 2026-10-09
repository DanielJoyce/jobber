"""Fallback for unrecognised alert senders: links whose URL looks like a posting."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from jobhunter.mail.message import MailMessage
from jobhunter.mail.parsers.common import AlertEntry, entries

FAMILY = "generic"
_PATH = re.compile(
    r"/jobs?/|/careers?/|/postings?/|/vacanc|/openings?/|/positions?/|/requisitions?/|"
    r"jobdetail|job_detail|job-detail|/opportunit",
    re.I,
)
_QUERY = re.compile(r"(?:^|&)(?:job_?id|jobid|req(?:uisition)?_?id|posting_?id|jid)=", re.I)
_NOT_JOB = re.compile(
    r"unsubscribe|preferences|/search/?$|/alerts?(?:/|$)|/account|/login|/signin|privacy|"
    r"\.(?:png|jpe?g|gif|css|js)$",
    re.I,
)


def is_job_link(url: str) -> bool:
    parts = urlsplit(url)
    target = parts.path + ("?" + parts.query if parts.query else "")
    if _NOT_JOB.search(target):
        return False
    if parts.path.rstrip("/") in ("", "/jobs", "/careers", "/job") and not parts.query:
        return False  # a board's home or listing page, not one posting
    return bool(_PATH.search(parts.path) or _QUERY.search(parts.query))


def matches(msg: MailMessage) -> bool:
    return True


def parse(msg: MailMessage) -> list[AlertEntry]:
    return entries(msg.html, msg.text, is_job_link)
