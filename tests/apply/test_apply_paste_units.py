"""URL rewriting and page extraction for New packet (specs/017). Pure functions, no network."""

from __future__ import annotations

import json

import pytest

from jobhunter.apply import paste

GH = "https://boards.greenhouse.io/acme/jobs/4012345"
ASHBY = "https://jobs.ashbyhq.com/acme/0f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://boards.greenhouse.io/embed/job_app?for=acme&token=4012345", GH),
        (
            "https://job-boards.greenhouse.io/embed/job_app?for=acme&token=7",
            "https://job-boards.greenhouse.io/acme/jobs/7",
        ),
        (ASHBY + "/embed?version=2", ASHBY + "?version=2"),
        (ASHBY + "?ashby_embed=true&utm_source=x", ASHBY + "?utm_source=x"),
        (GH, GH),
        (
            "https://click.appcast.io/track/abc?url=https%3A%2F%2Fboards.greenhouse.io%2Facme"
            "%2Fjobs%2F4012345",
            GH,
        ),
    ],
)
def test_direct_board_url(raw, expected):
    assert paste.direct_board_url(raw) == expected


def test_direct_board_url_rejects_non_http():
    with pytest.raises(paste.PasteError):
        paste.direct_board_url("javascript:alert(1)")


def test_page_title_guesses():
    assert paste.guess_from_page_title("Job Application for Data Analyst at Acme Corp") == (
        "Data Analyst",
        "Acme Corp",
    )
    assert paste.guess_from_page_title("Careers") == ("Careers", None)
    assert paste.guess_employer_from_url(GH) == "Acme"
    assert paste.guess_employer_from_url("https://example.com/jobs/1") is None


def test_extract_prefers_json_ld_job_posting():
    desc = "<p>" + "Run the synthetic fleet. " * 20 + "</p>"
    ld = {
        "@type": "JobPosting",
        "title": "SRE",
        "description": desc,
        "hiringOrganization": {"name": "Acme"},
    }
    html = (
        "<html><head><title>x</title><script type='application/ld+json'>"
        f"{json.dumps(ld)}</script></head><body><p>nav junk</p></body></html>"
    )
    got = paste.extract_posting(html)
    assert (got.title, got.employer) == ("SRE", "Acme")
    assert "Run the synthetic fleet." in got.text and "nav junk" not in got.text


def test_extract_drops_scripts_and_navigation():
    html = (
        "<html><body><nav>Home Jobs</nav><script>var x=1</script>"
        "<main><p>The real posting.</p></main><footer>Copyright</footer></body></html>"
    )
    assert paste.extract_posting(html).text == "The real posting."
