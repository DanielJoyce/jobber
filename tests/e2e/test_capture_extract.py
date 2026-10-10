"""capture/extract.js on hand-written fixture pages (specs/017 1e "Tests (1e)": Extraction).

The script is evaluated in Chromium on synthetic pages served by ``context.route`` (no
network: every other request is aborted and recorded). On every page a main-world
MutationObserver and ``outerHTML`` before and after prove the script changed nothing. Each
result is validated by the console's own request model and run through ``capture.analyse``.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlparse

import pytest

from jobhunter.apply import capture
from jobhunter.apply.capture_models import Facts

pytestmark = pytest.mark.e2e

REPO = Path(__file__).resolve().parents[2]
EXTRACT = (REPO / "extension" / "capture" / "extract.js").read_text(encoding="utf-8")
PAGES = REPO / "tests" / "fixtures" / "capture" / "pages"
EMP = "https://jobs.example-employer.test"
LI = "https://www.linkedin.com"
GH_EMBED = "https://boards.greenhouse.io/embed/job_app?for=embedsynthetic&token=4012345"
MUTATIONS = """
window.__mutations = 0;
window.__mo = new MutationObserver((records) => { window.__mutations += records.length; });
window.__mo.observe(document, {subtree: true, childList: true, attributes: true,
                               characterData: true});
"""
RESET = "() => { window.__mo.takeRecords(); window.__mutations = 0; }"
COUNT = "() => window.__mutations + window.__mo.takeRecords().length"
APPLY = {
    "easy": (
        '<button data-apply aria-label="Easy Apply to Senior Platform Engineer">Easy Apply</button>'
    ),
    "direct": '<a data-apply href="https://boards.greenhouse.io/boardsynthetic/jobs/7001?gh_src=li">Apply</a>',
    "redir": (
        '<a data-apply href="https://www.linkedin.com/redir?url=https%3A%2F%2Fboards.greenhouse.io'
        '%2Fboardsynthetic%2Fjobs%2F7001">Apply</a>'
    ),
    "button": '<button data-apply aria-label="Apply on company website">Apply</button>',
    "indeed": "<button data-apply>Apply on company site</button>",
}


def page_html(name: str) -> str:
    return (PAGES / name).read_text(encoding="utf-8")


def board(variant: str) -> str:
    return page_html("board.html").replace("<!--APPLY-->", APPLY[variant])


@pytest.fixture(scope="module")
def site(pw):
    served: dict[str, str] = {}
    blocked: list[str] = []
    seen: list[str] = []
    browser = pw.chromium.launch(args=["--host-resolver-rules=MAP * ~NOTFOUND"])
    ctx = browser.new_context()
    ctx.add_init_script(MUTATIONS)

    def router(route):
        url = route.request.url.split("#", 1)[0]
        seen.append(url)
        if url in served:
            route.fulfill(status=200, content_type="text/html", body=served[url])
        elif urlparse(url).hostname == "boards.greenhouse.io":
            route.fulfill(status=200, content_type="text/html",
                          body="<p>IFRAME-SECRET-TEXT from the embedded form</p>")  # fmt: skip
        else:
            blocked.append(url)
            route.abort()

    ctx.route("**/*", router)

    class Site:
        def open(self, url: str, html: str):
            served[url] = html
            page = ctx.new_page()
            page.goto(url)
            return page

        def extract(self, page):
            page.evaluate(RESET)
            before = page.evaluate("document.documentElement.outerHTML")
            result = page.evaluate(EXTRACT)
            assert page.evaluate("document.documentElement.outerHTML") == before
            assert page.evaluate(COUNT) == 0
            facts = Facts.model_validate({**result, "trigger": "popup"})
            return result, facts

    yield Site()
    ctx.close()
    browser.close()
    assert not blocked, f"unexpected requests: {blocked}"


def test_jsonld_posting(site):
    page = site.open(f"{EMP}/careers/1001", page_html("jsonld.html"))
    result, facts = site.extract(page)
    assert (
        len(result["jsonld"]) == 1
        and json.loads(result["jsonld"][0])["title"] == "Platform Engineer"
    )
    assert result["canonical"] == f"{EMP}/careers/1001"
    assert "Hi, Pat" not in result["page_text"]
    a = capture.analyse(facts)
    assert a.add_at_once and a.employer == "Synthetic Widgets" and "Terraform" in a.description


def test_graph_posting_and_broken_jsonld_ignored(site):
    page = site.open(f"{EMP}/careers/2002", page_html("graph.html"))
    result, facts = site.extract(page)
    assert len(result["jsonld"]) == 1
    assert capture.analyse(facts).title == "Data Engineer"


def test_microdata_posting(site):
    page = site.open(f"{EMP}/careers/micro", page_html("microdata.html"))
    result, facts = site.extract(page)
    names = {m["name"] for m in result["microdata"]}
    assert {"title", "description", "employmentType", "datePosted"} <= names
    a = capture.analyse(facts)
    assert a.desc_source == "microdata" and a.title == "Support Analyst"


def test_page_text_skips_furniture_hidden_text_and_form_values(site):
    page = site.open(f"{EMP}/plain/sre", page_html("plain.html"))
    page.fill("#typed", "typed-value@example.com")
    result, facts = site.extract(page)
    text = result["page_text"]
    assert "Keep the synthetic services up and fast." in text
    for absent in ("Hi, Pat", "HIDDEN-DISPLAY-NONE", "HIDDEN-SR-ONLY", "typed-value",
                   "must never be captured", "Sam Synthetic", "Sign out"):  # fmt: skip
        assert absent not in text, absent
    assert result["jsonld"] == [] and result["selection"] == ""
    a = capture.analyse(facts)
    assert a.desc_source == "page" and not a.add_at_once


def test_a_selected_paragraph_is_reported(site):
    page = site.open(f"{EMP}/plain/sel", page_html("plain.html"))
    page.evaluate(
        "() => { const r = document.createRange(); r.selectNodeContents(document.getElementById"
        "('para')); const s = getSelection(); s.removeAllRanges(); s.addRange(r); }"
    )
    result, facts = site.extract(page)
    assert result["selection"].startswith("Keep the synthetic services")
    assert capture.analyse(facts).selection  # an icon selection: previewed with both choices


def test_a_selection_inside_a_textarea_is_ignored(site):
    page = site.open(f"{EMP}/plain/textarea", page_html("plain.html"))
    page.focus("#notes")
    page.evaluate("() => document.getElementById('notes').select()")
    result, _ = site.extract(page)
    assert result["selection"] == ""


def test_embedded_greenhouse_iframe_is_reported_not_read(site):
    page = site.open(f"{EMP}/careers/embedded", page_html("iframe.html"))
    page.wait_for_load_state("load")
    result, facts = site.extract(page)
    assert GH_EMBED in result["iframes"]
    assert "IFRAME-SECRET-TEXT" not in json.dumps(result)
    offer = capture.analyse(facts).fetch
    assert (
        offer is not None
        and offer.url == "https://boards.greenhouse.io/embedsynthetic/jobs/4012345"
    )


def test_a_2_mb_description_is_truncated(site):
    big = "Synthetic duty. " * 140_000  # about 2.2 MB
    html = (
        "<!doctype html><title>Big</title><script type='application/ld+json'>"
        + json.dumps({"@type": "JobPosting", "title": "Big", "description": big})
        + f"</script><main><p>{big}</p></main>"
    )
    page = site.open(f"{EMP}/careers/big", html)
    result, _ = site.extract(page)
    assert len(result["page_text"]) == 100_000 and "page_text" in result["truncated"]
    assert result["jsonld"] == [] and result["jsonld_dropped"] == 1


def test_two_postings(site):
    page = site.open(f"{EMP}/careers/openings", page_html("two_postings.html"))
    result, facts = site.extract(page)
    assert len(result["jsonld"]) == 2
    assert capture.analyse(facts).ambiguous


def test_a_stale_single_page_board(site):
    page = site.open(f"{EMP}/search?jobId=501", page_html("spa.html"))
    _, first = site.extract(page)
    assert capture.analyse(first).add_at_once
    page.evaluate("() => document.getElementById('next').click()")  # the user's own click
    result, facts = site.extract(page)
    assert result["url"] == f"{EMP}/search?jobId=502"
    a = capture.analyse(facts)
    assert a.stale and not a.add_at_once


@pytest.mark.parametrize(
    ("variant", "kind", "href"),
    [
        ("easy", "easy_apply", None),
        ("direct", "offsite", "https://boards.greenhouse.io/boardsynthetic/jobs/7001?gh_src=li"),
        ("redir", "offsite", "https://www.linkedin.com/redir?url=https%3A%2F%2Fboards.greenhouse"
                             ".io%2Fboardsynthetic%2Fjobs%2F7001"),
        ("button", "offsite", None),
    ],
)  # fmt: skip
def test_linkedin_apply_control_is_read_without_touching_it(site, variant, kind, href):
    url = f"{LI}/jobs/view/401234500{['easy', 'direct', 'redir', 'button'].index(variant)}/"
    page = site.open(url, board(variant))
    result, facts = site.extract(page)
    ctl = result["apply_control"]
    assert ctl["kind"] == kind and ctl["href"] == href
    assert page.evaluate("window.__applyEvents") == 0
    a = capture.analyse(facts)
    if variant in ("direct", "redir"):
        assert a.destination == "https://boards.greenhouse.io/boardsynthetic/jobs/7001"
    else:
        assert a.destination is None


def test_indeed_apply_on_company_site(site):
    page = site.open("https://www.indeed.com/viewjob?jk=0a1b2c3d4e5f6789", board("indeed"))
    result, facts = site.extract(page)
    assert result["apply_control"]["kind"] == "offsite"
    assert capture.analyse(facts).board_key == "indeed:0a1b2c3d4e5f6789"
    assert page.evaluate("window.__applyEvents") == 0


def test_no_apply_control_off_board(site):
    page = site.open(f"{EMP}/careers/1001b", page_html("jsonld.html"))
    result, _ = site.extract(page)
    assert result["apply_control"] is None


def test_the_apply_control_is_read_in_the_job_details_not_the_filter_bar(site):
    pill = (
        '<div class="search-filters"><button aria-label="Easy Apply filter.">Easy Apply'
        "</button></div>"
    )
    body = board("direct").replace("<main>", "<main>" + pill, 1)
    page = site.open(f"{LI}/jobs/search/?currentJobId=4012345099", body)
    result, _ = site.extract(page)
    ctl = result["apply_control"]
    assert ctl["kind"] == "offsite"
    assert ctl["href"] == "https://boards.greenhouse.io/boardsynthetic/jobs/7001?gh_src=li"


def test_text_inside_display_contents_wrappers_is_read(site):
    html = (
        "<!doctype html><title>Wrapped</title><main><div style='display: contents'>"
        "<h1>Data Engineer</h1><p>Build the synthetic pipelines every day.</p></div>"
        "<p>Outside wrapper.</p></main>"
    )
    page = site.open(f"{EMP}/careers/contents", html)
    result, _ = site.extract(page)
    assert "Build the synthetic pipelines" in result["page_text"]
    assert "Outside wrapper." in result["page_text"]


def test_capture_leaves_the_selection_and_scroll_alone(site):
    tall = page_html("plain.html").replace("</main>", "<div style='height:3000px'></div></main>")
    page = site.open(f"{EMP}/plain/scroll", tall)
    page.evaluate(
        "() => { const r = document.createRange(); r.selectNodeContents(document.getElementById"
        "('para')); const s = getSelection(); s.removeAllRanges(); s.addRange(r); "
        "window.scrollTo(0, 400); }"
    )
    state = "() => [String(getSelection()), getSelection().rangeCount, scrollX, scrollY]"
    before = page.evaluate(state)
    site.extract(page)
    assert page.evaluate(state) == before
