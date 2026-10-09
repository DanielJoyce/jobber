"""Geographic Solutions "Virtual OneStop" (VOS) job alerts: the "Virtual Recruiter".

About two dozen state job banks run VOS (``/vosnet/`` URLs). Their robots.txt is
``Disallow: /``, so a VOS alert is the only legitimate view of those jobs and the detail link
is never fetched (specs/012). Built against synthetic fixtures; re-validate on real alerts.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from jobhunter.mail.message import MailMessage
from jobhunter.mail.parsers.common import AlertEntry, entries

FAMILY = "vos"
SIGNALS = re.compile(r"/vosnet/|virtual recruiter|virtual one ?stop|geographic solutions", re.I)
_DETAIL = re.compile(r"jobdetails?\.aspx|/jobbanks/|/vosnet/.*job", re.I)
_JOB_ID = re.compile(r"(?:^|[?&])(?:jobid|job_id|enc|id)=", re.I)


def is_job_link(url: str) -> bool:
    parts = urlsplit(url)
    path = parts.path
    return bool(_DETAIL.search(path)) and (
        "jobdetail" in path.lower() or bool(_JOB_ID.search(parts.query))
    )


def matches(msg: MailMessage) -> bool:
    return bool(SIGNALS.search(msg.body) or SIGNALS.search(msg.subject))


def parse(msg: MailMessage) -> list[AlertEntry]:
    return entries(msg.html, msg.text, is_job_link)
