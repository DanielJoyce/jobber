"""RFC 9309 robots.txt parsing (our own implementation, no urllib.robotparser)."""

from __future__ import annotations

from pathlib import Path

import pytest

from jobhunter.core.fetch.robots import rules_from_response

H = "https://example.test"


def rules(text: str, status: int = 200):
    return rules_from_response(H, status, text)


def test_src_never_imports_robotparser():
    src = Path(__file__).resolve().parents[2] / "src"
    offenders = [
        p
        for p in src.rglob("*.py")
        if "import" in p.read_text()
        and "urllib.robotparser" in p.read_text()
        and any(
            ln.lstrip().startswith(("import", "from")) and "robotparser" in ln
            for ln in p.read_text().splitlines()
        )
    ]
    assert offenders == []


def test_groups_for_same_agent_are_merged():
    r = rules(
        "User-agent: jobhunter\nDisallow: /a\n\nUser-agent: other\nDisallow: /x\n\n"
        "User-agent: JobHunter\nDisallow: /b\n"
    )
    assert not r.allows(H + "/a")
    assert not r.allows(H + "/b/c")
    assert r.allows(H + "/x")
    assert r.disallowed_paths_for_us() == ["/a", "/b"]


def test_consecutive_user_agents_share_rules():
    r = rules("User-agent: other\nUser-agent: jobhunter\nDisallow: /shared\n")
    assert not r.allows(H + "/shared")


def test_agent_is_case_insensitive_and_product_token_only():
    r = rules("User-agent: JOBHUNTER/2.0\nDisallow: /a\n")
    assert not r.allows(H + "/a")


def test_star_fallback_and_specific_overrides_star():
    r = rules("User-agent: *\nDisallow: /\n")
    assert not r.allows(H + "/x")
    r = rules("User-agent: *\nDisallow: /\n\nUser-agent: jobhunter\nDisallow: /private\n")
    assert r.allows(H + "/x")
    assert not r.allows(H + "/private/1")


def test_no_matching_group_allows_all():
    r = rules("User-agent: other\nDisallow: /\n")
    assert r.verdict == "parsed"
    assert r.allows(H + "/anything")
    assert r.disallowed_paths_for_us() == []


def test_longest_match_wins_and_allow_wins_ties():
    r = rules("User-agent: *\nDisallow: /a/\nAllow: /a/b\n")
    assert r.allows(H + "/a/b/c")
    assert not r.allows(H + "/a/c")
    r = rules("User-agent: *\nDisallow: /p\nAllow: /p\n")
    assert r.allows(H + "/p")
    r = rules("User-agent: *\nAllow: /p\nDisallow: /p\n")
    assert r.allows(H + "/p")
    r = rules("User-agent: *\nDisallow: /*.pdf\nAllow: /d/\n")
    assert not r.allows(H + "/d/x.pdf")  # longer disallow beats shorter allow


def test_wildcards_and_anchor():
    r = rules("User-agent: *\nDisallow: /*feed/\nDisallow: /*.pdf$\n")
    assert not r.allows(H + "/jobs/feed/")
    assert not r.allows(H + "/a.pdf")
    assert r.allows(H + "/a.pdf?x=1")
    assert r.allows(H + "/jobs/?q=feed")


def test_percent_encoding_normalised():
    r = rules("User-agent: *\nDisallow: /caf%C3%A9\nDisallow: /a%7eb\nDisallow: /sp ace\n")
    assert not r.allows(H + "/café")
    assert not r.allows(H + "/caf%c3%a9")
    assert not r.allows(H + "/a~b")
    assert not r.allows(H + "/a%7Eb")
    r = rules("User-agent: *\nDisallow: /café\n")
    assert not r.allows(H + "/caf%C3%A9")
    r = rules("User-agent: *\nDisallow: /a%2Fb\n")
    assert r.allows(H + "/a/b")
    assert not r.allows(H + "/a%2fb")


def test_query_is_part_of_the_matched_target():
    r = rules("User-agent: *\nDisallow: /*?session=\n")
    assert not r.allows(H + "/x?session=1")
    assert r.allows(H + "/x?other=1")


def test_empty_disallow_disallows_nothing():
    r = rules("User-agent: *\nDisallow:\n")
    assert r.allows(H + "/x")
    assert r.disallowed_paths_for_us() == []


def test_crawl_delay_per_group_with_star_fallback():
    r = rules("User-agent: *\nCrawl-delay: 10\n\nUser-agent: jobhunter\nCrawl-delay: 2.5\n")
    assert r.crawl_delay() == 2.5
    assert rules("User-agent: *\nCrawl-delay: 10\n").crawl_delay() == 10.0
    assert rules("User-agent: *\nDisallow: /x\n").crawl_delay() is None
    assert rules("User-agent: *\nCrawl-delay: soon\n").crawl_delay() is None


def test_blank_lines_do_not_split_a_group():
    r = rules("User-agent: jobhunter\n\nDisallow: /a\n\n\nDisallow: /b\n")
    assert not r.allows(H + "/a")
    assert not r.allows(H + "/b")


def test_commented_directives_ignored():
    r = rules("User-agent: *\n# Disallow: /\nDisallow: /x # trailing\n")
    assert r.allows(H + "/y")
    assert not r.allows(H + "/x")
    r = rules("# User-agent: *\n# Disallow: /\n")
    assert r.allows(H + "/anything")


def test_rules_before_any_user_agent_are_ignored():
    assert rules("Disallow: /\nUser-agent: *\nDisallow: /x\n").allows(H + "/y")


def test_robots_txt_always_allowed():
    r = rules("User-agent: *\nDisallow: /\n")
    assert r.allows(H + "/robots.txt")
    assert rules("", 401).allows(H + "/robots.txt") is False  # status handling unchanged


def test_sitemaps_collected():
    r = rules("Sitemap: https://example.test/s.xml\nUser-agent: *\nDisallow: /x\n")
    assert r.sitemaps() == ["https://example.test/s.xml"]
    assert not r.allows(H + "/x")


@pytest.mark.parametrize(
    ("status", "verdict"),
    [
        (404, "allow_all"),
        (410, "allow_all"),
        (429, "allow_all"),
        (401, "deny_all"),
        (403, "deny_all"),
        (500, "deny_all"),
        (503, "deny_all"),
        (302, "deny_all"),
    ],
)
def test_status_codes(status, verdict):
    r = rules("User-agent: *\nDisallow: /\n", status)
    assert r.verdict == verdict
    assert r.allows(H + "/x") is (verdict == "allow_all")


def test_empty_file_allows_all():
    r = rules("  \n")
    assert r.verdict == "allow_all"
