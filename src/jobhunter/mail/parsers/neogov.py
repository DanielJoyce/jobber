"""NEOGOV job alerts (governmentjobs.com / schooljobs.com "Job Interest" notifications).

Many state and local agencies post through NEOGOV. Detail pages live under
``/careers/<agency>/jobs/<id>/<slug>``; whether they may be fetched is decided by
FetchContext's robots check at resolve time. Built against synthetic fixtures; re-validate.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from jobhunter.mail.message import MailMessage
from jobhunter.mail.parsers.common import AlertEntry, entries

FAMILY = "neogov"
DOMAINS = ("governmentjobs.com", "schooljobs.com", "neogov.com", "neogov.net")
SIGNALS = re.compile(r"governmentjobs\.com|schooljobs\.com|neogov", re.I)
_DETAIL = re.compile(r"/careers/[^/]+/(?:[^/]+/)?jobs/\d+", re.I)


def host_ok(host: str) -> bool:
    return any(host == d or host.endswith("." + d) for d in DOMAINS)


def is_job_link(url: str) -> bool:
    parts = urlsplit(url)
    return host_ok((parts.hostname or "").lower()) and bool(_DETAIL.search(parts.path))


def matches(msg: MailMessage) -> bool:
    return host_ok(msg.sender_domain) or bool(SIGNALS.search(msg.body))


def parse(msg: MailMessage) -> list[AlertEntry]:
    return entries(msg.html, msg.text, is_job_link)
