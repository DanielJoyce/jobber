"""The help mechanism: rendering, links, heading ids, generated blocks, routes (specs/019 A)."""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from jobhunter import console
from jobhunter.config import Paths, Settings
from jobhunter.console import help as helpdocs
from jobhunter.console.app import NAV, create_app
from jobhunter.core import db

# --- slug ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Step 1: Google client JSON", "step-1-google-client-json"),
        ("What `/setup` is", "what-setup-is"),
        ("Under_score & dash-es!", "under_score--dash-es"),
        ("  Trim me ", "trim-me"),
        ("Résumé", "résumé"),
    ],
)
def test_slug_matches_github(text, expected):
    assert helpdocs.slug(text) == expected


def test_slug_repeats_get_numeric_suffixes():
    seen: dict[str, int] = {}
    assert [helpdocs.slug("Setup", seen) for _ in range(3)] == ["setup", "setup-1", "setup-2"]
    seen = {}
    assert helpdocs.slug("a", seen) == "a"
    assert helpdocs.slug("a-1", seen) == "a-1"
    assert helpdocs.slug("a", seen) == "a-2"  # skips the id a literal heading already took


# --- rendering -----------------------------------------------------------------------------


def test_html_is_escaped_not_passed_through():
    page = helpdocs.render_text("# T\n\n<script>alert(1)</script> and <b>x</b>\n")
    assert "<script>" not in page.html and "<b>" not in page.html
    assert "&lt;script&gt;" in page.html


def test_dangerous_link_schemes_are_refused():
    page = helpdocs.render_text(
        "[a](javascript:alert(1)) [b](vbscript:x) [c](file:///etc/passwd)\n"
    )
    assert "href=" not in page.html  # shown as plain text, never a link


def test_marker_lines_are_removed_not_shown():
    text = (
        "# T\n\n<!-- jh:begin demo -->\n\n| a | b |\n|---|---|\n| 1 | 2 |\n"
        "<!-- jh:end demo -->\n\nafter\n"
    )
    page = helpdocs.render_text(text)
    assert "jh:" not in page.html and "&lt;!--" not in page.html
    assert page.html.count("<tr>") == 2  # header and one body row: no marker row under the table
    assert "<p>after</p>" in page.html


def test_other_stray_html_comments_are_shown_escaped():
    page = helpdocs.render_text("# T\n\n<!-- note -->\n")
    assert "&lt;!-- note --&gt;" in page.html


def test_headings_get_github_ids_and_are_collected():
    page = helpdocs.render_text("# T\n\n## Step 1: Do it\n\n### `code` bit\n\n## Step 1: Do it\n")
    assert 'id="step-1-do-it"' in page.html and 'id="step-1-do-it-1"' in page.html
    assert [(h.level, h.text, h.id) for h in page.headings] == [
        (1, "T", "t"),
        (2, "Step 1: Do it", "step-1-do-it"),
        (3, "code bit", "code-bit"),
        (2, "Step 1: Do it", "step-1-do-it-1"),
    ]


def test_tables_scroll_in_their_own_box():
    page = helpdocs.render_text("| a | b |\n|---|---|\n| 1 | 2 |\n")
    assert '<div class="help-table"><table>' in page.html
    assert page.html.count("<div") == page.html.count("</div>")


@pytest.mark.parametrize(
    ("href", "expected"),
    [
        ("email-alerts.md#step-3-gmail-token", "/help/email-alerts#step-3-gmail-token"),
        ("containers.md", "/help/containers"),
        ("../../../../specs/018-containers.md", f"{helpdocs.GITHUB_BLOB}specs/018-containers.md"),
        (
            "../../../../specs/018-containers.md#mechanism",
            f"{helpdocs.GITHUB_BLOB}specs/018-containers.md#mechanism",
        ),
        ("../../../../deploy/quadlet/", f"{helpdocs.GITHUB_BLOB}deploy/quadlet"),
        ("not-a-topic.md", f"{helpdocs.GITHUB_BLOB}src/jobhunter/console/help/not-a-topic.md"),
        (
            "../../../../specs/018-containers.md?plain=1#L40",
            f"{helpdocs.GITHUB_BLOB}specs/018-containers.md?plain=1#L40",
        ),
        ("#a-heading", "#a-heading"),
        ("/setup", "/setup"),
        ("/prefs#sec-pay", "/prefs#sec-pay"),
        ("https://example.com/x", "https://example.com/x"),
        ("mailto:a@example.com", "mailto:a@example.com"),
        (
            "../../../../../outside.md",
            "../../../../../outside.md",
        ),  # left alone; drift test fails it
    ],
)
def test_rewrite_href(href, expected):
    assert helpdocs.rewrite_href(href) == expected


def test_links_are_rewritten_in_rendered_pages():
    page = helpdocs.render_text("[a](email-alerts.md#x) [b](../../../../README.md) [c](/setup)\n")
    assert 'href="/help/email-alerts#x"' in page.html
    assert f'href="{helpdocs.GITHUB_BLOB}README.md"' in page.html
    assert 'href="/setup"' in page.html


def test_render_cache_follows_the_file(tmp_path, monkeypatch):
    f = tmp_path / "getting-started.md"
    f.write_text("# One\n")
    monkeypatch.setattr(helpdocs, "HELP_DIR", tmp_path)
    helpdocs._cache.pop("getting-started", None)
    first = helpdocs.render("getting-started")
    assert helpdocs.render("getting-started") is first  # cached
    f.write_text("# Two\n")
    import os

    st = f.stat()
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000))
    assert "Two" in helpdocs.render("getting-started").html
    helpdocs._cache.pop("getting-started", None)


# --- generated blocks ----------------------------------------------------------------------

BLOCKS = {"demo": lambda: "| a |\n|---|\n| 1 |\n"}


def test_regenerate_fills_a_block_with_blank_lines_around_markers(tmp_path):
    f = tmp_path / "x.md"
    f.write_text("# T\ntext\n<!-- jh:begin demo -->\nold\n<!-- jh:end demo -->\nafter\n")
    assert helpdocs.stale_files(tmp_path, BLOCKS) == [f]
    assert helpdocs.regenerate(tmp_path, BLOCKS) == [f]
    assert f.read_text() == (
        "# T\ntext\n\n<!-- jh:begin demo -->\n\n| a |\n|---|\n| 1 |\n\n<!-- jh:end demo -->\n\n"
        "after\n"
    )
    assert helpdocs.stale_files(tmp_path, BLOCKS) == []
    assert helpdocs.regenerate(tmp_path, BLOCKS) == []  # idempotent


def test_regenerated_table_renders_as_a_table_with_no_marker_row(tmp_path):
    f = tmp_path / "x.md"
    f.write_text("# T\n<!-- jh:begin demo -->\n<!-- jh:end demo -->\n")
    helpdocs.regenerate(tmp_path, BLOCKS)
    html = helpdocs.render_text(f.read_text()).html
    assert html.count("<tr>") == 2 and "jh:" not in html


def test_two_blocks_in_one_file(tmp_path):
    f = tmp_path / "x.md"
    f.write_text(
        "<!-- jh:begin demo -->\n<!-- jh:end demo -->\n"
        "<!-- jh:begin demo -->\n<!-- jh:end demo -->\n"
    )
    helpdocs.regenerate(tmp_path, BLOCKS)
    assert f.read_text().count("| 1 |") == 2
    assert helpdocs.stale_files(tmp_path, BLOCKS) == []


@pytest.mark.parametrize(
    "text",
    [
        "<!-- jh:begin demo -->\n",
        "<!-- jh:end demo -->\n",
        "<!-- jh:begin demo -->\n<!-- jh:end other -->\n",
        "<!-- jh:begin demo -->\n<!-- jh:begin demo -->\n<!-- jh:end demo -->\n",
        "<!-- jh:begin unknown -->\n<!-- jh:end unknown -->\n",
    ],
)
def test_broken_blocks_are_errors(tmp_path, text):
    (tmp_path / "x.md").write_text(text)
    with pytest.raises(helpdocs.BlockError):
        helpdocs.regenerate(tmp_path, BLOCKS)


def test_markers_inside_a_fenced_example_are_not_blocks(tmp_path):
    f = tmp_path / "x.md"
    f.write_text("```\n<!-- jh:begin demo -->\n```\n")
    assert helpdocs.regenerate(tmp_path, BLOCKS) == []


# --- the fixed topic list ------------------------------------------------------------------


def test_every_topic_has_a_file_and_every_file_a_topic():
    files = {p.stem for p in helpdocs.HELP_DIR.glob("*.md")}
    assert files == {t.slug for t in helpdocs.TOPICS}
    assert helpdocs.TOPICS[0].slug == "getting-started"


# --- routes --------------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path):
    path = tmp_path / "t.db"
    settings = Settings(paths=Paths(profile_dir=tmp_path / "profile", db_path=path))
    return TestClient(create_app(settings, lambda: db.connect(path)))


def test_help_is_last_in_the_nav():
    assert NAV[-1] == ("/help", "Help")


def test_index_links_section_headings_with_their_ids(client, monkeypatch):
    real = helpdocs.render

    def fake(topic):
        if topic == "scorers":
            return helpdocs.render_text("# S\n\n## Pick one\n\n### The key\n\n#### deep\n")
        return real(topic)

    monkeypatch.setattr(helpdocs, "render", fake)
    r = client.get("/help")
    assert 'href="/help/scorers#pick-one"' in r.text
    assert 'href="/help/scorers#the-key"' in r.text
    assert "#deep" not in r.text and 'href="/help/scorers#s"' not in r.text


def test_index_lists_every_topic_and_its_sections(client):
    r = client.get("/help")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    for t in helpdocs.TOPICS:
        assert f'href="/help/{t.slug}"' in r.text and t.title in r.text
    assert 'href="/help/getting-started"' in r.text and "New here?" in r.text
    assert 'aria-current="page"' in r.text and ">Help</a>" in r.text


@pytest.mark.parametrize("topic", [t.slug for t in helpdocs.TOPICS])
def test_each_topic_renders(client, topic):
    r = client.get(f"/help/{topic}")
    assert r.status_code == 200
    assert "help-topics" in r.text  # the phone-width <details> topic list
    assert "help-side" in r.text  # the desktop sidebar
    assert "<h1" in r.text
    assert "help.css" in r.text


def test_topic_page_shows_the_file_text(client):
    r = client.get("/help/nightly-runs")
    assert "jobhunter schedule install" in r.text
    assert 'class="help-table"' in r.text


@pytest.mark.parametrize(
    "path",
    ["/help/nope", "/help/..%2f..%2fREADME", "/help/getting-started.md", "/help/__init__"],
)
def test_unlisted_topics_are_404(client, path):
    assert client.get(path).status_code == 404


def test_help_works_with_no_profile_no_resume_no_database_rows(client, tmp_path):
    assert not (tmp_path / "profile").exists()
    assert client.get("/help").status_code == 200
    assert client.get("/help/getting-started").status_code == 200


def test_help_works_while_the_database_answers_503(tmp_path):
    path = tmp_path / "t.db"
    settings = Settings(paths=Paths(profile_dir=tmp_path / "profile", db_path=path))
    calls = {"n": 0}

    def factory():
        calls["n"] += 1
        if calls["n"] > 1:  # the boot connection works; every later request is refused
            raise db.SchemaError("database is newer than this jobhunter")
        return db.connect(path)

    c = TestClient(create_app(settings, factory))
    assert c.get("/healthz").status_code == 503  # the premise
    assert c.get("/help").status_code == 200
    assert c.get("/help/getting-started").status_code == 200


@pytest.mark.parametrize("path", ["/help", "/help/getting-started", "/help/nope"])
def test_non_loopback_host_is_refused(client, path):
    r = client.get(path, headers={"host": "evil.example:8808"})
    assert r.status_code == 403 and "loopback" in r.text


def test_every_page_has_theme_tokens_and_no_horizontal_overflow_rules():
    css = (console.app.STATIC_DIR / "help.css").read_text()
    assert "var(--text-primary)" in css and "var(--border)" in css
    assert "@media (max-width: 760px)" in css
    assert re.search(r"\.help-table\s*{[^}]*overflow-x:\s*auto", css)
    assert re.search(r"\.help-page pre\s*{[^}]*overflow-x:\s*auto", css)
    assert not re.search(r"#[0-9a-fA-F]{3,6}\b", css)  # no hard-coded colours: dark mode follows
