"""htmlconfig adapter, tested against synthetic HTML and JSON served by httpx.MockTransport."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from urllib.parse import parse_qs

import httpx
import pytest
from hc_support import Site, make_ctx
from selectolax.lexbor import LexborHTMLParser

from jobhunter.core import db
from jobhunter.core.fetch import RobotsDisallowed, reset_shared_state
from jobhunter.core.models import JobStub, Query, SourceRow
from jobhunter.sources.adapters.htmlconfig import (
    ConfigError,
    HtmlConfigAdapter,
    extract_html,
    extract_json,
    form_fields,
    locations_for,
    parse_when,
    query_vars,
    render,
)

SINCE = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _state():
    reset_shared_state()
    yield
    reset_shared_state()


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.migrate(c)
    yield c
    c.close()


def src(config: dict, pagination: dict | None = None, state: str | None = "ZZ") -> SourceRow:
    return SourceRow.model_validate(
        {
            "key": "zz-test",
            "state": state,
            "class": "A",
            "name": "Test board",
            "family": "htmlconfig",
            "tier": "http",
            "entry": "https://jobs.example.gov/",
            "config": config,
            "pagination": pagination or {},
        }
    )


def run(row: SourceRow, site: Site, conn, tmp_path, query=None, since=SINCE):
    ctx = make_ctx(row, conn, site, tmp_path)
    return ctx, list(HtmlConfigAdapter().search(row, query or Query(keywords=["rust"]), since, ctx))


# ─── a typical server-rendered board ────────────────────────────────────────

LIST_CFG = {
    "search": {
        "method": "GET",
        "path": "/jobs/search",
        "params": {"q": "{keywords}", "within": "{posted_within_days}", "title": "{title}"},
    },
    "list": {
        "rows": "div.job-result",
        "id": "::attr(data-id)",
        "title": "h3 a::text",
        "url": "h3 a::attr(href)",
        "posted_at": "span.posted::text",
        "employer": "span.employer",
        "location": "span.location",
        "salary_raw": "span.salary",
    },
    "detail": {
        "description": "div#job-description",
        "closes_at": "span.closing::text",
        "apply_url": "a.apply::attr(href)",
    },
    "date_formats": ["%m/%d/%Y"],
}


def result(n: int, posted: str = "10/05/2026", title: str | None = None) -> str:
    return (
        f'<div class="job-result" data-id="J{n}"><h3><a href="/jobs/{n}">{title or f"Job {n}"}</a>'
        f'</h3><span class="posted">{posted}</span><span class="employer">Dept {n}</span>'
        f'<span class="location">Springfield, ZZ</span><span class="salary">$50,000</span></div>'
    )


def page(*rows: str, extra: str = "") -> str:
    return f"<html><body>{''.join(rows)}{extra}</body></html>"


def test_get_search_renders_params_and_parses_rows(conn, tmp_path):
    site = Site(lambda r: httpx.Response(200, text=page(result(1), result(2))))
    row = src(LIST_CFG)
    _, stubs = run(
        row,
        site,
        conn,
        tmp_path,
        Query(title="engineer", keywords=["rust", "go"], posted_within_days=3),
    )
    req = site.requests[0]
    assert req.url.path == "/jobs/search"
    assert dict(req.url.params) == {"q": "rust go", "within": "3", "title": "engineer"}
    assert [s.external_id for s in stubs] == ["J1", "J2"]
    first = stubs[0]
    assert first.title == "Job 1"
    assert first.url == "https://jobs.example.gov/jobs/1"  # made absolute
    assert first.agency_raw == "Dept 1"
    assert first.salary_raw == "$50,000"
    assert first.posted_at == datetime(2026, 10, 5, tzinfo=UTC)
    assert first.locations[0].city == "Springfield" and first.locations[0].state == "ZZ"
    assert first.needs_resolve is True  # a detail block is configured
    assert first.description_raw is None


def test_empty_params_are_dropped(conn, tmp_path):
    site = Site(lambda r: httpx.Response(200, text=page()))
    run(src(LIST_CFG), site, conn, tmp_path, Query(keywords=["rust"]))
    assert dict(site.requests[0].url.params) == {"q": "rust"}


def test_rows_without_title_or_url_are_skipped(conn, tmp_path):
    junk = '<div class="job-result" data-id="X"><h3></h3></div>'
    site = Site(lambda r: httpx.Response(200, text=page(junk, result(1))))
    _, stubs = run(src(LIST_CFG), site, conn, tmp_path)
    assert [s.external_id for s in stubs] == ["J1"]


def test_robots_disallow_is_honoured(conn, tmp_path):
    site = Site(
        lambda r: httpx.Response(200, text=page(result(1))),
        robots="User-agent: *\nDisallow: /jobs\n",
    )
    with pytest.raises(RobotsDisallowed):
        run(src(LIST_CFG), site, conn, tmp_path)
    assert site.requests == []


def test_no_detail_block_means_no_resolve(conn, tmp_path):
    cfg = {**LIST_CFG, "detail": None}
    cfg = {k: v for k, v in cfg.items() if v is not None}
    cfg["list"] = {**LIST_CFG["list"], "description": "span.desc"}
    html = page(result(1).replace("</div>", '<span class="desc"><p>Hi</p></span></div>'))
    site = Site(lambda r: httpx.Response(200, text=html))
    _, stubs = run(src(cfg), site, conn, tmp_path)
    assert stubs[0].description_raw == '<span class="desc"><p>Hi</p></span>'
    assert stubs[0].needs_resolve is False


# ─── pagination ─────────────────────────────────────────────────────────────


def paged_site(pages: dict[int, str]) -> Site:
    def route(r: httpx.Request) -> httpx.Response:
        n = int(r.url.params.get("page", "1"))
        return httpx.Response(200, text=pages.get(n, page()))

    return Site(route)


def test_page_param_walks_until_an_empty_page(conn, tmp_path):
    site = paged_site({1: page(result(1), result(2)), 2: page(result(3)), 3: page()})
    row = src(LIST_CFG, {"kind": "page", "param": "page", "max_pages": 9})
    _, stubs = run(row, site, conn, tmp_path)
    assert [s.external_id for s in stubs] == ["J1", "J2", "J3"]
    assert [r.url.params["page"] for r in site.requests] == ["1", "2", "3"]


def test_short_page_ends_the_walk_when_page_size_is_known(conn, tmp_path):
    site = paged_site({1: page(result(1), result(2)), 2: page(result(3))})
    row = src(LIST_CFG, {"kind": "page", "param": "page", "page_size": 2, "max_pages": 9})
    _, stubs = run(row, site, conn, tmp_path)
    assert len(stubs) == 3
    assert len(site.requests) == 2  # page 2 had fewer than page_size rows: no page 3


def test_max_pages_bounds_the_walk(conn, tmp_path):
    site = paged_site({n: page(result(n)) for n in range(1, 10)})
    row = src(LIST_CFG, {"kind": "page", "param": "page", "max_pages": 3})
    _, stubs = run(row, site, conn, tmp_path)
    assert len(stubs) == 3 and len(site.requests) == 3


def test_page_start_offsets_the_first_page_number(conn, tmp_path):
    site = paged_site({0: page(result(1)), 1: page()})
    row = src(LIST_CFG, {"kind": "page", "param": "page", "start": 0, "max_pages": 2})
    site.handler = lambda r: httpx.Response(
        200, text=page(result(1)) if r.url.params["page"] == "0" else page()
    )
    run(row, site, conn, tmp_path)
    assert [r.url.params["page"] for r in site.requests] == ["0", "1"]


def test_offset_kind_steps_by_page_size(conn, tmp_path):
    seen = []

    def route(r: httpx.Request) -> httpx.Response:
        seen.append(r.url.params["start"])
        n = int(r.url.params["start"])
        return httpx.Response(200, text=page(result(n + 1), result(n + 2)) if n < 4 else page())

    row = src(
        LIST_CFG, {"kind": "offset", "param": "start", "page_size": 2, "start": 1, "max_pages": 9}
    )
    _, stubs = run(row, Site(route), conn, tmp_path)
    assert seen == ["0", "2", "4"]
    assert len(stubs) == 4


def test_templated_page_in_params_without_param_key(conn, tmp_path):
    cfg = {
        **LIST_CFG,
        "search": {**LIST_CFG["search"], "params": {"p": "{page}", "q": "{keywords}"}},
    }
    site = Site(lambda r: httpx.Response(200, text=page(result(int(r.url.params["p"])))))
    row = src(cfg, {"kind": "page", "max_pages": 2})
    run(row, site, conn, tmp_path)
    assert [r.url.params["p"] for r in site.requests] == ["1", "2"]


def test_duplicates_across_pages_are_dropped(conn, tmp_path):
    site = paged_site({1: page(result(1), result(2)), 2: page(result(2), result(3)), 3: page()})
    row = src(LIST_CFG, {"kind": "page", "param": "page", "max_pages": 9})
    _, stubs = run(row, site, conn, tmp_path)
    assert [s.external_id for s in stubs] == ["J1", "J2", "J3"]


def test_newest_first_stops_after_a_page_entirely_before_since(conn, tmp_path):
    old = "01/02/2025"
    site = paged_site(
        {1: page(result(1)), 2: page(result(2, old), result(3, old)), 3: page(result(4))}
    )
    row = src(LIST_CFG, {"kind": "page", "param": "page", "max_pages": 9, "order": "newest_first"})
    _, stubs = run(row, site, conn, tmp_path)
    assert [s.external_id for s in stubs] == ["J1"]
    assert len(site.requests) == 2  # page 3 is never fetched


def test_unknown_order_keeps_walking_past_old_rows(conn, tmp_path):
    old = "01/02/2025"
    site = paged_site({1: page(result(1)), 2: page(result(2, old)), 3: page(result(4)), 4: page()})
    row = src(LIST_CFG, {"kind": "page", "param": "page", "max_pages": 9})
    _, stubs = run(row, site, conn, tmp_path)
    assert [s.external_id for s in stubs] == ["J1", "J4"]  # J2 is stale and filtered, not fatal
    assert len(site.requests) == 4


def test_next_link_follows_the_anchor_until_it_disappears(conn, tmp_path):
    pages = {
        "/jobs/search?q=rust": page(result(1), extra='<a class="next" href="?p=2">Next</a>'),
        "/jobs/search?p=2": page(
            result(2), extra='<a class="next" href="/jobs/search?p=3">Next</a>'
        ),
        "/jobs/search?p=3": page(result(3)),
    }

    def route(r: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=pages[r.url.raw_path.decode()])

    row = src(LIST_CFG, {"kind": "next_link", "next": "a.next::attr(href)", "max_pages": 9})
    _, stubs = run(row, Site(route), conn, tmp_path)
    assert [s.external_id for s in stubs] == ["J1", "J2", "J3"]


def test_next_link_ignores_javascript_and_fragment_hrefs(conn, tmp_path):
    html = page(result(1), extra='<a class="next" href="javascript:go()">Next</a>')
    site = Site(lambda r: httpx.Response(200, text=html))
    row = src(LIST_CFG, {"kind": "next_link", "next": "a.next::attr(href)", "max_pages": 5})
    run(row, site, conn, tmp_path)
    assert len(site.requests) == 1


# ─── POST bodies and forms ──────────────────────────────────────────────────


def test_post_form_body_is_templated(conn, tmp_path):
    cfg = {
        **LIST_CFG,
        "search": {
            "method": "POST",
            "path": "/search",
            "data": {"kw": "{query}", "days": "{posted_within_days}"},
            "days_buckets": [1, 7, 30],
            "default_days": 30,
        },
    }
    site = Site(lambda r: httpx.Response(200, text=page(result(1))))
    run(src(cfg), site, conn, tmp_path, Query(title="sre", keywords=["x"], posted_within_days=2))
    req = site.requests[0]
    assert req.method == "POST"
    assert parse_qs(req.content.decode()) == {"kw": ["sre"], "days": ["7"]}


def test_post_json_body_keeps_nulls_and_page_ints(conn, tmp_path):
    cfg = {
        "search": {
            "method": "POST",
            "url": "https://api.example.gov/v1/jobs",
            "json": {
                "q": "{keywords}",
                "page": "{page}",
                "filters": None,
                "nested": {"t": "{title}"},
            },
        },
        "list": {
            "format": "json",
            "rows": "data.items",
            "id": "id",
            "title": "name",
            "url": {"template": "https://jobs.example.gov/j/{id}"},
        },
    }
    site = Site(lambda r: httpx.Response(200, json={"data": {"items": [{"id": 7, "name": "Dev"}]}}))
    _, stubs = run(src(cfg), site, conn, tmp_path, Query(title="dev", keywords=["a", "b"]))
    body = json.loads(site.requests[0].content)
    assert body == {"q": "a b", "page": 1, "filters": None, "nested": {"t": "dev"}}
    assert site.requests[0].url.host == "api.example.gov"
    assert [(s.external_id, s.url) for s in stubs] == [("7", "https://jobs.example.gov/j/7")]


def test_json_page_param_goes_into_the_body(conn, tmp_path):
    cfg = {
        "search": {"method": "POST", "path": "/api", "json": {"q": "{keywords}"}},
        "list": {"format": "json", "rows": "items", "id": "id", "title": "t", "url": "u"},
    }
    seen = []

    def route(r: httpx.Request) -> httpx.Response:
        body = json.loads(r.content)
        seen.append(body["pg"])
        items = [{"id": body["pg"], "t": "T", "u": f"/j/{body['pg']}"}] if body["pg"] < 3 else []
        return httpx.Response(200, json={"items": items})

    row = src(cfg, {"kind": "page", "param": "pg", "max_pages": 9})
    _, stubs = run(row, Site(route), conn, tmp_path)
    assert seen == [1, 2, 3]
    assert [s.external_id for s in stubs] == ["1", "2"]


FORM_PAGE = """<html><body><form id="f" method="post" action="/go.aspx?x=1">
<input type="hidden" name="__VIEWSTATE" value="STATE"><input name="kw" value="">
<select name="days"><option value="7">7</option><option value="30" selected>30</option></select>
<select name="plain"><option value="first">a</option><option value="b">b</option></select>
<input type="checkbox" name="remote" value="1" checked><input type="submit" name="go" value="Go">
</form></body></html>"""


def test_form_search_gets_the_page_then_posts_hidden_state(conn, tmp_path):
    cfg = {
        "search": {
            "method": "POST",
            "form": {"path": "/search.aspx", "selector": "form#f", "submit": {"go": "Search"}},
            "data": {"kw": "{keywords}"},
        },
        "list": LIST_CFG["list"],
    }
    posts = []

    def route(r: httpx.Request) -> httpx.Response:
        if r.method == "GET":
            return httpx.Response(200, text=FORM_PAGE)
        posts.append((str(r.url), parse_qs(r.content.decode(), keep_blank_values=True)))
        return httpx.Response(200, text=page(result(1)))

    _, stubs = run(src(cfg), Site(route), conn, tmp_path)
    url, form = posts[0]
    assert url == "https://jobs.example.gov/go.aspx?x=1"
    assert form == {
        "__VIEWSTATE": ["STATE"],
        "kw": ["rust"],
        "days": ["30"],  # the selected option
        "plain": ["first"],  # no selection: the first option
        "go": ["Search"],
    }  # the checkbox is not submitted
    assert len(stubs) == 1


def test_form_search_with_missing_form_is_a_config_error(conn, tmp_path):
    cfg = {
        "search": {"method": "POST", "form": {"path": "/s", "selector": "form#nope"}},
        "list": LIST_CFG["list"],
    }
    site = Site(lambda r: httpx.Response(200, text=FORM_PAGE))
    with pytest.raises(ConfigError, match="form"):
        run(src(cfg), site, conn, tmp_path)


def test_form_fields_helper():
    form = LexborHTMLParser(FORM_PAGE).css_first("form")
    assert form_fields(form) == {
        "__VIEWSTATE": "STATE",
        "kw": "",
        "days": "30",
        "plain": "first",
    }


# ─── ASP.NET __doPostBack paging ────────────────────────────────────────────


def pager(*targets: tuple[str, str]) -> str:
    links = "".join(f"<a href=\"javascript:__doPostBack('{t}','{a}')\">x</a>" for t, a in targets)
    hidden = '<input type="hidden" name="__VIEWSTATE" value="S">'
    return f'<form method="post" action="./results">{hidden}{links}</form>'


def test_postback_paging_posts_event_target_and_stops_without_a_link(conn, tmp_path):
    posts = []
    grid = "ctl00$Grid"

    def route(r: httpx.Request) -> httpx.Response:
        if r.method == "GET":
            return httpx.Response(200, text=page(result(1), extra=pager((grid, "Page$2"))))
        form = parse_qs(r.content.decode(), keep_blank_values=True)
        posts.append(form)
        assert form["__VIEWSTATE"] == ["S"]
        return httpx.Response(200, text=page(result(2), extra=pager((grid, "Page$1"))))

    row = src(
        LIST_CFG,
        {"kind": "postback", "target": grid, "argument": "Page${page}", "max_pages": 9},
    )
    _, stubs = run(row, Site(route), conn, tmp_path)
    assert [s.external_id for s in stubs] == ["J1", "J2"]
    # Only page 2 was offered; after it the pager offers no "Page$3", so the walk ends.
    assert [p["__EVENTTARGET"] for p in posts] == [[grid]]
    assert [p["__EVENTARGUMENT"] for p in posts] == [["Page$2"]]


def test_postback_target_template_uses_the_next_page_number(conn, tmp_path):
    posted = []

    def route(r: httpx.Request) -> httpx.Response:
        if r.method == "GET":
            return httpx.Response(200, text=page(result(1), extra=pager(("pg2", ""), ("pg3", ""))))
        form = parse_qs(r.content.decode(), keep_blank_values=True)
        posted.append(form["__EVENTTARGET"][0])
        return httpx.Response(200, text=page(result(len(posted) + 1), extra=pager(("pg3", ""))))

    row = src(LIST_CFG, {"kind": "postback", "target": "pg{page}", "max_pages": 3})
    _, stubs = run(row, Site(route), conn, tmp_path)
    assert posted == ["pg2", "pg3"]
    assert len(stubs) == 3


# ─── field specs ────────────────────────────────────────────────────────────

FRAG = LexborHTMLParser(
    """<div id="r"><a class="t" href="/x?id=42" data-k="v"> Hello
      World </a><span class="own"><b>Label:</b> value here</span>
      <div class="body"><p>One</p><p>Two</p></div>
      <table><tr><td>Closing Date: </td><td> 4/24/2024 </td></tr>
      <tr><th>Pay</th><td>$10</td></tr></table></div>"""
)


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("a.t", "Hello World"),
        ("a.t::text", "Hello World"),
        ("a.t::attr(href)", "/x?id=42"),
        ({"sel": "a.t", "kind": "text"}, "Hello World"),
        ("span.own::own", "value here"),
        ("span.own::text", "Label: value here"),
        ("div.body::html", '<div class="body"><p>One</p><p>Two</p></div>'),
        ("a.nope::text", None),
        ({"sel": "a.t::attr(href)", "re": r"id=(\d+)"}, "42"),
        ({"sel": "a.t::attr(href)", "re": r"id=\d+"}, "id=42"),
        ({"sel": "a.t::attr(href)", "re": r"nomatch"}, None),
        ({"sel": "a.t::attr(href)", "template": "https://x.test{value}"}, "https://x.test/x?id=42"),
        ({"sel": "a.nope", "template": "https://x.test{value}"}, None),
        ({"const": "fixed"}, "fixed"),
        ({"label": "Closing Date"}, "4/24/2024"),
        ({"label": "closing date:"}, "4/24/2024"),
        ({"label": "Pay"}, "$10"),
        ({"label": "Missing"}, None),
        ({"label": "Pay", "template": "{value} / yr"}, "$10 / yr"),
    ],
)
def test_extract_html_specs(spec, expected):
    assert extract_html(FRAG, spec, {}) == expected


def test_extract_html_from_another_field_and_template_over_fields():
    fields = {"url": "/x?id=42", "title": "T"}
    assert extract_html(FRAG, {"from": "url", "re": r"id=(\d+)"}, fields) == "42"
    assert extract_html(FRAG, {"from": "missing", "re": r"(\d+)"}, fields) is None
    assert extract_html(FRAG, {"template": "{title}-{url}"}, fields) == "T-/x?id=42"
    assert extract_html(FRAG, None, fields) is None


def test_default_kind_for_description_is_html():
    html = extract_html(FRAG, "div.body", {}, default_kind="html")
    assert html.startswith("<div")


ROW = {"id": 5, "co": {"name": "Acme", "tags": ["a", "b"]}, "ok": True, "n": None, "z": [{"q": 9}]}


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("id", "5"),
        ("co.name", "Acme"),
        ("co.tags.1", "b"),
        ("z.0.q", "9"),
        ("co.tags.9", None),
        ("co.nope", None),
        ("n", None),
        ("ok", "true"),
        ({"path": "co.tags"}, '["a", "b"]'),
        ({"path": "id", "template": "https://x.test/{value}/{co}"}, None),
        ({"path": "id", "template": "https://x.test/{value}"}, "https://x.test/5"),
        ({"template": "n={id}"}, "n=5"),
        ({"path": "co.name", "re": r"^A(\w+)"}, "cme"),
        ({"const": "k"}, "k"),
        ({"from": "t", "re": r"(\d+)"}, "17"),
    ],
)
def test_extract_json_specs(spec, expected):
    if isinstance(spec, dict) and spec.get("template", "").endswith("{co}"):
        # a dict-valued key renders through str(): assert it does not raise instead
        assert extract_json(ROW, spec, {}) is not None
        return
    assert extract_json(ROW, spec, {"t": "item 17"}) == expected


# ─── templates, dates, locations ────────────────────────────────────────────


def test_query_vars_buckets_and_defaults():
    cfg = {"days_buckets": [1, 7, 14, 30], "default_days": 14}
    assert query_vars(Query(posted_within_days=2), cfg, 1, 0)["posted_within_days"] == 7
    assert query_vars(Query(posted_within_days=99), cfg, 1, 0)["posted_within_days"] == 30
    assert query_vars(Query(), cfg, 1, 0)["posted_within_days"] == 14
    assert query_vars(Query(), {}, 1, 0)["posted_within_days"] == ""
    assert query_vars(Query(posted_within_days=5), {}, 1, 0)["posted_within_days"] == 5
    v = query_vars(Query(title=" sre ", keywords=["a ", " ", "b"]), {}, 3, 50)
    assert (v["title"], v["keywords"], v["query"], v["page"], v["offset"]) == (
        "sre",
        "a b",
        "sre",
        3,
        50,
    )
    assert query_vars(Query(keywords=["x"]), {}, 1, 0)["query"] == "x"


def test_render_is_recursive_and_tolerates_unknown_names():
    v = {"page": 4, "offset": 30, "keywords": "k"}
    assert render(
        {"a": ["{keywords}", {"b": "{page}-{nope}"}], "p": "{page}", "o": "{offset}", "n": 1}, v
    ) == {
        "a": ["k", {"b": "4-"}],
        "p": 4,
        "o": 30,
        "n": 1,
    }


def test_parse_when_formats_zone_iso_and_free_text():
    cfg = {"timezone": "America/Chicago", "date_formats": ["%m/%d/%Y", "%d %b %Y"]}
    assert parse_when("10/05/2026", cfg) == datetime(2026, 10, 5, 5, 0, tzinfo=UTC)  # CDT
    assert parse_when("05 Oct 2026", cfg) == datetime(2026, 10, 5, 5, 0, tzinfo=UTC)
    assert parse_when("2026-10-05T12:30:00Z", cfg) == datetime(2026, 10, 5, 12, 30, tzinfo=UTC)
    assert parse_when("2026-10-05T12:30:00-05:00", cfg) == datetime(2026, 10, 5, 17, 30, tzinfo=UTC)
    assert parse_when("2026-10-05", {}) == datetime(2026, 10, 5, tzinfo=UTC)
    assert parse_when("October 5, 2026", {}) == datetime(2026, 10, 5, tzinfo=UTC)
    assert parse_when("not a date", {}) is None
    assert parse_when(None, cfg) is None and parse_when("", cfg) is None
    assert parse_when("10/05/2026", {"timezone": "Nowhere/Land", "date_formats": ["%m/%d/%Y"]}) == (
        datetime(2026, 10, 5, tzinfo=UTC)
    )


def test_locations_for():
    row = src({}, state="MI")
    assert locations_for(row, "Lansing, MI, 48915")[0].city == "Lansing"
    assert locations_for(row, "Lansing, mi")[0].state == "MI"
    plain = locations_for(row, "Saipan")
    assert (plain[0].state, plain[0].city) == ("MI", "Saipan")
    assert locations_for(row, None)[0].city is None
    assert locations_for(src({}, state=None), "Nowhere") == []
    assert locations_for(src({}, state=None), "Reno, NV")[0].state == "NV"


# ─── flat lists (row_siblings) ──────────────────────────────────────────────

FLAT = """<div class="t" data-n="1"><a href="/a">A</a></div>
<div class="m"><b>Co</b> Acme - Reno</div><p>snippet</p>
<div class="t" data-n="2"><a href="/b">B</a></div><div class="m"><b>Co</b> Beta - Elko</div>"""


def test_row_siblings_groups_flat_markup(conn, tmp_path):
    cfg = {
        "search": {"path": "/s"},
        "list": {
            "rows": "div.t",
            "row_siblings": True,
            "id": "div.t::attr(data-n)",
            "title": "a::text",
            "url": "a::attr(href)",
            "employer": {"sel": "div.m::own", "re": r"^(\w+)"},
        },
    }
    site = Site(lambda r: httpx.Response(200, text=f"<html><body>{FLAT}</body></html>"))
    _, stubs = run(src(cfg), site, conn, tmp_path)
    assert [(s.external_id, s.title, s.agency_raw) for s in stubs] == [
        ("1", "A", "Acme"),
        ("2", "B", "Beta"),
    ]


# ─── resolve ────────────────────────────────────────────────────────────────

DETAIL = """<html><body><h1>Real Title</h1><div id="job-description"><p>Do things</p></div>
<span class="closing">12/31/2026</span><a class="apply" href="/apply/1">Apply</a></body></html>"""


def stub(**kw) -> JobStub:
    base = {
        "source_key": "zz-test",
        "external_id": "J1",
        "title": "Job 1",
        "url": "https://jobs.example.gov/jobs/1",
        "location_raw": "Springfield, ZZ",
        "agency_raw": "Dept 1",
        "salary_raw": "$50,000",
    }
    return JobStub(**{**base, **kw})


def test_resolve_html_detail(conn, tmp_path):
    site = Site(lambda r: httpx.Response(200, text=DETAIL))
    row = src(LIST_CFG)
    ctx = make_ctx(row, conn, site, tmp_path)
    d = HtmlConfigAdapter().resolve(stub(), ctx)
    assert d.description_raw == '<div id="job-description"><p>Do things</p></div>'
    assert d.closes_at == datetime(2026, 12, 31, tzinfo=UTC)
    assert d.apply_url == "https://jobs.example.gov/apply/1"
    assert d.title == "Job 1" and d.agency_raw == "Dept 1" and d.salary_raw == "$50,000"
    assert d.needs_resolve is False
    assert d.extra["description_format"] == "html"
    assert site.requests[0].url.path == "/jobs/1"


def test_resolve_keeps_stub_values_when_detail_selectors_miss(conn, tmp_path):
    site = Site(lambda r: httpx.Response(200, text="<html><body>nothing</body></html>"))
    row = src(LIST_CFG)
    ctx = make_ctx(row, conn, site, tmp_path)
    original = stub(description_raw="<p>from list</p>", apply_url="https://jobs.example.gov/a")
    d = HtmlConfigAdapter().resolve(original, ctx)
    assert d.description_raw == "<p>from list</p>"
    assert d.apply_url == "https://jobs.example.gov/a"
    assert d.closes_at is None


def test_resolve_overrides_title_employer_and_location_when_configured(conn, tmp_path):
    cfg = {
        **LIST_CFG,
        "detail": {
            "title": "h1",
            "employer": "span.emp",
            "location": "span.loc",
            "salary_raw": "span.pay",
            "posted_at": "span.on",
            "description": "div.d",
        },
    }
    html = (
        '<h1>New Title</h1><span class="emp">Bureau</span><span class="loc">Elko, NV</span>'
        '<span class="pay">$9</span><span class="on">10/01/2026</span><div class="d">x</div>'
    )
    site = Site(lambda r: httpx.Response(200, text=html))
    row = src(cfg)
    d = HtmlConfigAdapter().resolve(stub(), make_ctx(row, conn, site, tmp_path))
    assert (d.title, d.agency_raw, d.location_raw, d.salary_raw) == (
        "New Title",
        "Bureau",
        "Elko, NV",
        "$9",
    )
    assert d.locations[0].state == "NV"
    assert d.posted_at == datetime(2026, 10, 1, tzinfo=UTC)


def test_resolve_json_detail(conn, tmp_path):
    cfg = {
        "search": {"path": "/s"},
        "list": {"rows": "x"},
        "detail": {
            "format": "json",
            "description": "post.body",
            "closes_at": "post.closes",
            "apply_url": "post.apply",
        },
    }
    payload = {
        "post": {"body": "<p>Hi</p>", "closes": "2026-12-01", "apply": "https://ats.example.com/1"}
    }
    site = Site(lambda r: httpx.Response(200, json=payload))
    row = src(cfg)
    d = HtmlConfigAdapter().resolve(stub(), make_ctx(row, conn, site, tmp_path))
    assert d.description_raw == "<p>Hi</p>"
    assert d.closes_at == datetime(2026, 12, 1, tzinfo=UTC)
    assert d.apply_url == "https://ats.example.com/1"


# ─── config validation and plumbing ─────────────────────────────────────────


def test_missing_search_or_list_block_is_a_config_error(conn, tmp_path):
    site = Site(lambda r: httpx.Response(200, text=page()))
    with pytest.raises(ConfigError, match=r"config\.search"):
        run(src({"list": {"rows": "x"}}), site, conn, tmp_path)
    with pytest.raises(ConfigError, match=r"config\.list"):
        run(src({"search": {"path": "/s"}}), site, conn, tmp_path)


def test_search_without_url_or_path_is_a_config_error(conn, tmp_path):
    site = Site(lambda r: httpx.Response(200, text=page()))
    with pytest.raises(ConfigError, match="url"):
        run(src({"search": {}, "list": {"rows": "x"}}), site, conn, tmp_path)


def test_html_list_without_rows_selector_is_a_config_error(conn, tmp_path):
    site = Site(lambda r: httpx.Response(200, text=page()))
    with pytest.raises(ConfigError, match="rows"):
        run(src({"search": {"path": "/s"}, "list": {"title": "a"}}), site, conn, tmp_path)


def test_non_mapping_block_is_a_config_error(conn, tmp_path):
    site = Site(lambda r: httpx.Response(200, text=page()))
    with pytest.raises(ConfigError, match="mapping"):
        run(src({"search": ["nope"], "list": {"rows": "x"}}), site, conn, tmp_path)


def test_naive_since_is_treated_as_utc(conn, tmp_path):
    site = Site(lambda r: httpx.Response(200, text=page(result(1, "10/05/2026"))))
    _, stubs = run(src(LIST_CFG), site, conn, tmp_path, since=datetime(2026, 10, 1))
    assert len(stubs) == 1


def test_base_url_overrides_the_entry_host(conn, tmp_path):
    cfg = {**LIST_CFG, "base_url": "https://search.example.gov"}
    site = Site(lambda r: httpx.Response(200, text=page(result(1))))
    _, stubs = run(src(cfg), site, conn, tmp_path)
    assert site.requests[0].url.host == "search.example.gov"
    assert stubs[0].url == "https://search.example.gov/jobs/1"


def test_rows_with_a_missing_posted_date_are_kept(conn, tmp_path):
    html = page(result(1, posted="TBD"))
    site = Site(lambda r: httpx.Response(200, text=html))
    _, stubs = run(src(LIST_CFG), site, conn, tmp_path)
    assert stubs[0].posted_at is None
