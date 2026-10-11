"""Which URLs certainly name one posting (specs/017 phase 1e).

``dedupe_url._identifies_job`` is generous: any URL with a path or query counts unless an ATS
rule says otherwise, so ``https://www.acme.example/careers`` passes. That is fine for nightly
URL merges between two ingested copies of a listing, but not where a user's click or an
automatic link rests on it: a LinkedIn Apply button pointing at an employer's careers path
would tie unrelated LinkedIn jobs to one posting. Here a URL is a posting only when an ATS rule
says so, or when it has the shape of one posting on a board jobhunter ingests from (the shapes
below were read from the sources' own URLs). The table grows like ``ats_rules.py``.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlsplit

from jobhunter.pipeline.ats_rules import host_matches, match_ats

# (host pattern, path regex, a query parameter that must be present, or None)
POSTING_URLS: tuple[tuple[str, re.Pattern[str], str | None], ...] = (
    ("www.usajobs.gov", re.compile(r"^/(?:job|GetJob/ViewDetails)/\d+/?$", re.I), None),
    ("usajobs.gov", re.compile(r"^/(?:job|GetJob/ViewDetails)/\d+/?$", re.I), None),
    ("nlx.jobsyn.org", re.compile(r"^/[0-9A-Za-z-]{20,}/?$"), None),
    ("usnlx.com", re.compile(r"^/[^/]+/[^/]+/[^/]+/job/?$"), None),
    ("*.usnlx.com", re.compile(r"^/[^/]+/[^/]+/[^/]+/job/?$"), None),
    ("usnlx.com", re.compile(r"^/viewjob\.asp$", re.I), "sjobid"),
    ("jobs.mitalent.org", re.compile(r"^/job-seeker/job-details/JobCode/\d+/?$", re.I), None),
    ("hire.wyo.gov", re.compile(r"^/job/[^/]+/?$"), None),
    ("worksource.my.site.com", re.compile(r"/job-search/job-details/?$"), "jobId"),
    ("jobs.utah.gov", re.compile(r"^/jsp/utjobs/single-job/?$"), "j"),
    ("labor.eightfold.ai", re.compile(r"^/careerhub/explore/jobs/\d+/?$"), None),
)


def is_posting_url(url: str | None) -> bool:
    """True when an ATS rule, or a known board's URL shape, says ``url`` is one posting."""
    if not url:
        return False
    try:
        p = urlsplit(url.strip())
    except ValueError:
        return False
    if p.scheme not in ("http", "https") or not p.hostname:
        return False
    rule = match_ats(url)
    if rule is not None:
        return rule.is_posting(url)
    host = p.hostname.lower()
    query = {k.lower() for k, v in parse_qsl(p.query, keep_blank_values=True) if v}
    for pattern, path, param in POSTING_URLS:
        if (
            host_matches(host, pattern)
            and path.search(p.path)
            and (param is None or param.lower() in query)
        ):
            return True
    return False
