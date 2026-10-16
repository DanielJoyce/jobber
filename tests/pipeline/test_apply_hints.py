"""Apply links named in a posting's own text, and a job page's Apply button (bug 3bab68d)."""

from __future__ import annotations

from pathlib import Path

import pytest

from jobhunter.pipeline.apply_hints import (
    Hint,
    ats_posting,
    description_hints,
    matches_job,
    page_apply_hrefs,
)

FIX = Path(__file__).parent.parent / "fixtures" / "applylink"
CARESOURCE = (
    "https://caresource.wd1.myworkdayjobs.com/en-US/CareSource/job/Remote/"
    "Digital-Product-Manager-II_R13743-1/apply"
)


def urls(raw: str, base: str | None = None) -> list[str]:
    return [h.url for h in description_hints(raw, base)]


# NLx descriptions arrive as Markdown and are stored as <p> text with the link syntax kept,
# Markdown escapes (\_) and a double-escaped ampersand included: the real shape in the DB.
NLX_BOILERPLATE = (
    "<p>Ready to do the most impactful work of your career? At[Coinbase]"
    "(https://www.coinbase.com/?utm\\_campaign=x&amp;amp;utm\\_source=coinbase), we are "
    "uncompromising.</p>"
    "<p>By submitting your application, you agree to[ arbitration of disputes]"
    "(https://www.coinbase.com/legal/application-arbitration-agreement).</p>"
    "<p>Know your rights: workplace discrimination is illegal "
    "(https://careers.google.com/jobs/dist/legal/EEOC\\_KnowYourRights\\_10\\_20.pdf)</p>"
)


def test_markdown_apply_link_unescaped_and_cued():
    raw = NLX_BOILERPLATE + (
        "<p>[Apply here](https://caresource.wd1.myworkdayjobs.com/en-US/CareSource/job/Remote/"
        "Digital-Product-Manager-II\\_R13743-1/apply)</p>"
    )
    assert description_hints(raw) == [Hint(CARESOURCE, cued=True)]


# Each of these says "apply" (or another cue) right at the link, so only the boilerplate
# filter keeps it out. Shapes from real postings in the user's data.
@pytest.mark.parametrize(
    "raw",
    [
        # accommodation page, cued by "to apply"
        "<p>To request a reasonable accommodation to apply, visit [our accommodations page]"
        "(https://www.mongodb.com/accommodations).</p>",
        # privacy notice before applying
        "<p>Please review our privacy notice before applying at https://www.nestle.com/jobs/x</p>",
        # accessibility named after the link, in the same sentence
        "<p>Applied endeavors to make our careers site "
        "(https://www.appliedmaterials.com/us/en/careers.html) accessible to all users.</p>",
        # terms by clicking Apply
        "<p>By clicking Apply Now you agree to the terms at https://www.roberthalf.com/us/en/x</p>",
        # a guide, not an application
        "<p>[Applicant Tips on How to Apply](https://www.youtube.com/watch?v=Y8sOb10qYXQ)</p>",
        # a talent pool, cued by "apply"
        "<p>Apply to join our talent pool: https://acme.wd5.myworkdayjobs.com/x/job/y/Pool_R1</p>",
    ],
)
def test_cued_boilerplate_is_never_a_hint(raw):
    assert urls(raw) == []


def test_how_to_apply_heading_still_cues_the_link():
    raw = "<p>How to Apply: Apply online at https://www.acme.com/careers/jobs/123</p>"
    assert description_hints(raw) == [Hint("https://www.acme.com/careers/jobs/123", cued=True)]


def test_typed_url_after_apply_cue():
    raw = (
        "<p>10% domestic travel required.</p><p>Apply at https://careers.agcocorp.com/</p>"
        "<p>Solutions for Every Season</p>"
    )
    assert urls(raw) == ["https://careers.agcocorp.com/"]


def test_typed_url_in_bold_after_must_apply_online():
    raw = (
        "This position is a member of Class A retirement under SDRS.<br> <br>Must apply online: "
        "<b>https://gen-acme-trn.inforcloudsuite.com/hcm/xmlhttp/shorturl.do?key=42J</b> <br> "
        "You must apply online, emailed resumes are not accepted."
    )
    assert urls(raw) == ["https://gen-acme-trn.inforcloudsuite.com/hcm/xmlhttp/shorturl.do?key=42J"]


def test_url_on_its_own_line_takes_a_cue_only_after_a_colon():
    assert urls("<p>To apply:</p><p>https://acme.com/careers/1</p>") == [
        "https://acme.com/careers/1"
    ]
    assert urls("<p>We value applicants.</p><p>https://acme.com/careers/1</p>") == []


def test_html_anchor_after_apply_cue():
    raw = (
        '<p><strong><span>Apply at </span></strong><a href="https://www.governmentjobs.com/'
        'jobs/5445693-0/attorney"><strong><span>https://www.governmentjobs.com/jobs/5445693-0/'
        "attorney</span></strong></a></p>"
    )
    assert urls(raw) == ["https://www.governmentjobs.com/jobs/5445693-0/attorney"]


def test_html_anchor_text_apply_and_relative_href():
    raw = '<p>Interested? <a href="/apply/123">Apply now</a></p>'
    assert urls(raw, "https://jobs.acme.com/posting/123") == ["https://jobs.acme.com/apply/123"]


def test_uncued_ats_posting_is_kept_uncued_but_not_its_home():
    raw = (
        "<p>See https://boards.greenhouse.io/acme/jobs/567 for details. Our board: "
        "https://boards.greenhouse.io/acme</p>"
    )
    assert description_hints(raw) == [Hint("https://boards.greenhouse.io/acme/jobs/567", False)]


def test_unrelated_typed_url_is_not_a_hint():
    assert urls("<p>Learn about us at https://www.acme.com/about</p>") == []


def test_every_hint_is_returned_best_first():
    """No cap here: the resolver filters first, then keeps three."""
    raw = (
        "<p>See https://boards.greenhouse.io/acme/jobs/567.</p>"
        "<p>Apply online: https://acme.com/careers/567</p>"
        '<p><a href="https://jobs.lever.co/acme/0123456789abcdef0123">Apply</a></p>'
        '<p><a href="https://jobs.lever.co/acme/0123456789abcdef0124">Apply</a></p>'
        '<p><a href="https://jobs.lever.co/acme/0123456789abcdef0125">Apply</a></p>'
    )
    assert urls(raw) == [
        "https://jobs.lever.co/acme/0123456789abcdef0123",
        "https://jobs.lever.co/acme/0123456789abcdef0124",
        "https://jobs.lever.co/acme/0123456789abcdef0125",
        "https://acme.com/careers/567",
        "https://boards.greenhouse.io/acme/jobs/567",
    ]


@pytest.mark.parametrize("href", ["http://[object Object]", "https://[", "//[x"])
def test_malformed_hrefs_are_skipped(href):
    raw = f'<p><a href="{href}">Share</a> Apply at <a href="/jobs/1">here</a></p>'
    assert urls(raw, "https://www.acme.com/posting") == ["https://www.acme.com/jobs/1"]
    page = f'<a href="{href}">Apply</a><a href="https://boards.greenhouse.io/a/jobs/1">Apply</a>'
    assert page_apply_hrefs(page, "https://jobs.utah.gov/x") == [
        "https://boards.greenhouse.io/a/jobs/1"
    ]


def test_ats_posting():
    assert ats_posting("https://boards.greenhouse.io/acme/jobs/567")
    assert not ats_posting("https://boards.greenhouse.io/acme")
    assert not ats_posting("https://okgov.wd1.myworkdayjobs.com/okgovjobs?hiringCompany=7a8")
    # Hosts known only by name: list and careers pages are never postings.
    for url in (
        "https://acme.bamboohr.com/",
        "https://acme.bamboohr.com/careers",
        "https://acme.bamboohr.com/careers/12",
        "https://jobs.jobvite.com/acme",
        "https://acme.applytojob.com/apply",
        "https://ats.rippling.com/acme/jobs",
        "https://performancemanager4.successfactors.com/doc/custom/x/FDC.png",
        "https://css-x-prd.inforcloudsuite.com/hcm/Jobs/form/JobBoard",
    ):
        assert not ats_posting(url), url
    # Infor's per-posting short link and the posting page it opens.
    assert ats_posting("https://gen-x-trn.inforcloudsuite.com/hcm/xmlhttp/shorturl.do?key=4XQ")
    assert ats_posting(
        "https://css-x-prd.inforcloudsuite.com:443/hcm/Jobs/navigation/"
        "JobPosting[JobPostingSet](1,37669,1).JobPostingDisplayNav"
    )
    assert not ats_posting("https://www.acme.com/jobs/1")


def test_matches_job_by_title_words_or_shared_id():
    own = ["https://jobs.myflorida.com/job/PENSACOLA-ATTORNEY-60062091-FL-32501/1047953500/"]
    gj = "https://www.governmentjobs.com/jobs/{}"
    assert matches_job(gj.format("5449533-0/attorney"), title="Attorney", own=own)
    assert not matches_job(gj.format("4754958/journal-writer-editor"), title="Attorney", own=own)
    aide = "District Legislative Aide (District 35)"
    assert matches_job(
        gj.format("5445206-0/district-legislative-aide-district-35"), title=aide, own=own
    )
    assert not matches_job(
        gj.format("4910438/district-legislative-aide-district-36"), title=aide, own=own
    )
    # A shared number is enough, whatever the path says.
    assert matches_job(
        "https://acme.icims.com/jobs/3925/x/job", title="Architect", own=["2026-3925"]
    )


def test_levels_and_letters_must_match():
    ii = "https://caresource.wd1.myworkdayjobs.com/x/job/Remote/Digital-Product-Manager-II_R13743-1"
    i = "https://caresource.wd1.myworkdayjobs.com/x/job/Remote/Digital-Product-Manager-I_R13744-1"
    assert not matches_job(ii, title="Digital Product Manager I", own=[])
    assert not matches_job(i, title="Digital Product Manager II", own=[])
    assert matches_job(i, title="Digital Product Manager I", own=[])
    hr = "https://www.governmentjobs.com/jobs/1234567/hr-specialist"
    assert not matches_job(hr, title="IT Specialist", own=[])


@pytest.mark.parametrize(
    ("hint", "own"),
    [
        # a year shared with the job's own id
        ("https://acme.icims.com/jobs/4100/2026-summer-intern/job", "2026-3925"),
        # Maryland's R3=0001 suffix
        (
            "https://www.governmentjobs.com/jobs/0001/attorney",
            "https://www.jobapscloud.com/MD/sup/bulpreview.asp?b=&R1=12&R2=001991&R3=0001",
        ),
        # a zip code after a state code
        (
            "https://www.governmentjobs.com/jobs/32399/attorney",
            "https://jobs.myflorida.com/job/TALLAHASSEE-ANALYST-FL-32399/1047953500/",
        ),
    ],
    ids=["year", "padded", "zip"],
)
def test_numbers_that_are_not_ids_never_match(hint, own):
    assert not matches_job(hint, title="Software Engineer", own=[own])


def test_sibling_requisition_on_the_jobs_own_host_does_not_match():
    own = ["https://maine.wd5.myworkdayjobs.com/en-US/Executive/details/Revenue-Agent_R26-03941"]
    sibling = "https://maine.wd5.myworkdayjobs.com/en-US/Executive/details/Revenue-Agent_R26-03937"
    assert not matches_job(sibling, title="Revenue Agent", own=own)
    assert matches_job(own[0] + "?q=x", title="Revenue Agent", own=own)


def test_talent_pool_does_not_match_the_job():
    pool = (
        "https://iheartmedia.wd5.myworkdayjobs.com/External_iHM/job/San-Antonio-TX-Stone-Oak/"
        "XMLNAME----Introduce-Yourself-and-Join-Our-Talent-Pool----_Req37495"
    )
    assert not matches_job(pool, title="Full-Stack Software Developer", own=["Req39448"])


def test_page_apply_button_off_site_first_and_uncapped():
    html = (
        '<nav><a href="/apply-tips">How to apply</a><a href="/login">Sign in</a></nav>'
        '<a class="btn" href="/jobs/9/save">Save</a>'
        '<a href="/jobs/9/apply-here">Apply on this site</a>'
        '<a href="https://www.monster.com/j/9">Apply on Monster</a>'
        '<a href="https://www.ziprecruiter.com/j/9">Apply on ZipRecruiter</a>'
        '<a href="https://snap.example.gov/apply">Apply for food assistance</a>'
        '<a class="apply-button" href="https://acme.wd5.myworkdayjobs.com/x/job/y/z_1">Go</a>'
        '<a href="/privacy">Applicant privacy</a>'
        '<a href="https://www.acme.com/accommodations">Request an accommodation to apply</a>'
    )
    assert page_apply_hrefs(html, "https://jobs.state.example.gov/jobs/9") == [
        "https://www.monster.com/j/9",
        "https://www.ziprecruiter.com/j/9",
        "https://snap.example.gov/apply",
        "https://acme.wd5.myworkdayjobs.com/x/job/y/z_1",
        "https://jobs.state.example.gov/jobs/9/apply-here",
    ]


def test_dejobs_shell_has_no_button():
    html = (FIX / "dejobs_shell.html").read_text()
    url = "https://mongodb.dejobs.org/san-francisco-ca/x/186B454139E84E9EB77922F3507BEF9A/job/"
    assert page_apply_hrefs(html, url) == []
