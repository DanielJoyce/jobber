"""ATS canonicalization and redirector unwrap tables (specs/015 step 2 and 4). Pure functions."""

from __future__ import annotations

import pytest

from jobhunter.pipeline.ats_rules import host_matches, match_ats, unwrap

WD = "https://acme.wd5.myworkdayjobs.com/en-US/Acme/job/Albany-NY/Analyst_R-1234"
GH = "https://boards.greenhouse.io/acme/jobs/567"
LEVER = "https://jobs.lever.co/acme/0f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b"
ASHBY = "https://jobs.ashbyhq.com/acme/0f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b"
NEOGOV = "https://www.governmentjobs.com/careers/acme/jobs/4567890/analyst"
USAS = "https://apply.usastaffing.gov/Application/Apply?AnnouncementNumber=X-1&JobId=1"


@pytest.mark.parametrize(
    ("url", "ats", "canonical"),
    [
        (WD, "workday", WD + "/apply"),
        (WD + "/apply", "workday", WD + "/apply"),
        (WD + "/apply/applyManually", "workday", WD + "/apply/applyManually"),
        (WD + "?source=appcast", "workday", WD + "/apply?source=appcast"),
        ("https://acme.wd5.myworkdayjobs.com/en-US/Acme", "workday", None),
        (GH, "greenhouse", GH + "#app"),
        (GH + "#app", "greenhouse", GH + "#app"),
        (
            "https://job-boards.greenhouse.io/acme/jobs/567",
            "greenhouse",
            "https://job-boards.greenhouse.io/acme/jobs/567#app",
        ),
        ("https://boards.greenhouse.io/acme", "greenhouse", None),
        (LEVER, "lever", LEVER + "/apply"),
        (LEVER + "/apply", "lever", LEVER + "/apply"),
        (ASHBY, "ashby", ASHBY + "/application"),
        (ASHBY + "/application", "ashby", ASHBY + "/application"),
        ("https://careers-acme.icims.com/jobs/1234/analyst/job", "icims", None),
        ("https://jobs.smartrecruiters.com/Acme/743999", "smartrecruiters", None),
        ("https://acme.taleo.net/careersection/2/jobdetail.ftl?job=123", "taleo", None),
        (NEOGOV, "neogov", NEOGOV + "/apply"),
        (NEOGOV + "/apply", "neogov", NEOGOV + "/apply"),
        (USAS, "usastaffing", None),
    ],
)
def test_ats_canonical(url, ats, canonical):
    rule = match_ats(url)
    assert rule is not None and rule.name == ats
    # None: canonical is the URL as given (apply is on-page, or not a posting).
    assert rule.canonical(url) == (canonical or url)


def test_unknown_host_has_no_rule():
    assert match_ats("https://careers.acme.example/jobs/1") is None
    assert match_ats("https://myworkdayjobs.com.evil.example/job/1") is None


def test_host_matches_wildcard_is_subdomain_only():
    assert host_matches("acme.wd5.myworkdayjobs.com", "*.myworkdayjobs.com")
    assert not host_matches("myworkdayjobs.com", "*.myworkdayjobs.com")
    assert not host_matches("evilmyworkdayjobs.com", "*.myworkdayjobs.com")


@pytest.mark.parametrize(
    ("url", "dest"),
    [
        (
            "https://click.appcast.io/track/abc?cs=x&url=https%3A%2F%2Fexample.com%2Fj%2F1",
            "https://example.com/j/1",
        ),
        ("https://acme.jobs2web.com/r?url=https://example.com/j/2", "https://example.com/j/2"),
        ("https://www.google.com/url?q=https://example.com/j/3&sa=D", "https://example.com/j/3"),
        # double-encoded
        (
            "https://click.appcast.io/t?dest=https%253A%252F%252Fexample.com%252Fj%252F4",
            "https://example.com/j/4",
        ),
        # generic rule on an unlisted host
        (
            "https://track.example.net/go?id=9&target=https://example.com/j/5",
            "https://example.com/j/5",
        ),
        ("https://track.example.net/go?r=http://example.com/j/6", "http://example.com/j/6"),
    ],
)
def test_unwrap(url, dest):
    assert unwrap(url) == dest


@pytest.mark.parametrize(
    "url",
    [
        "https://track.example.net/go?id=9",
        "https://track.example.net/go?url=/relative/path",
        "https://track.example.net/go?u=javascript:alert(1)",
        # never unwrap an ATS URL, even with a generic-looking param
        GH + "?redirect=https://example.com/elsewhere",
    ],
)
def test_unwrap_declines(url):
    assert unwrap(url) is None
