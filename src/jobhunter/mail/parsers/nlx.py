"""National Labor Exchange (NLx / DirectEmployers) job alerts.

Postings link to ``https://<site>/<location>/<title>/<GUID>/job/`` (the same URL the NLx
adapter stores), so dedupe-first usually matches an NLx job already collected in full.
Built against synthetic fixtures; re-validate on real alerts.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from jobhunter.mail.message import MailMessage
from jobhunter.mail.parsers.common import AlertEntry, entries

FAMILY = "nlx"
DOMAINS = ("usnlx.com", "jobsyn.org", "dejobs.org", "directemployers.org")
SIGNALS = re.compile(r"usnlx\.com|national labor exchange|jobsyn\.org", re.I)
_DETAIL = re.compile(r"/[0-9A-F]{32}/job/?$", re.I)
_REDIRECT = re.compile(r"^/[0-9A-F]{32}\d*/?$", re.I)


def host_ok(host: str) -> bool:
    return any(host == d or host.endswith("." + d) for d in DOMAINS)


def is_job_link(url: str) -> bool:
    parts = urlsplit(url)
    if not host_ok((parts.hostname or "").lower()):
        return False
    return bool(_DETAIL.search(parts.path) or _REDIRECT.match(parts.path))


def matches(msg: MailMessage) -> bool:
    return host_ok(msg.sender_domain) or bool(SIGNALS.search(msg.body))


def parse(msg: MailMessage) -> list[AlertEntry]:
    return entries(msg.html, msg.text, is_job_link)
