"""America's JobLink (AJL) saved-search alerts (Arizona, Arkansas, Delaware, Idaho, Illinois,
Kansas, Maine, Vermont).

JobLink's robots.txt disallows only ``/search/jobs`` and ``/search/resumes``; posting detail
paths are allowed, so these entries are resolved to the full description (specs/012).
Built against synthetic fixtures; re-validate on real alerts.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from jobhunter.mail.message import MailMessage
from jobhunter.mail.parsers.common import AlertEntry, entries

FAMILY = "joblink"
SIGNALS = re.compile(r"america'?s ?job ?link|joblink|saved search", re.I)
_DETAIL = re.compile(r"/jobs?/(?:view/)?\d{4,}(?:/|$)", re.I)


def is_job_link(url: str) -> bool:
    path = urlsplit(url).path
    return bool(_DETAIL.search(path)) and not path.lower().startswith("/search/")


def matches(msg: MailMessage) -> bool:
    return bool(SIGNALS.search(msg.body) or SIGNALS.search(msg.subject))


def parse(msg: MailMessage) -> list[AlertEntry]:
    return entries(msg.html, msg.text, is_job_link)
