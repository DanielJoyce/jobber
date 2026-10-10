"""Board ids and the board-host no-fetch list (specs/017 phase 1e). No network: MockTransport."""
# ruff: noqa: F811  (pytest fixtures imported from test_applylink)

from __future__ import annotations

from datetime import timedelta

import pytest
from test_applylink import (  # noqa: F401  (fixtures)
    GH,
    NOW,
    Server,
    _fresh_shared_state,
    add_group,
    factory,
    server,
    settings,
)

from jobhunter.core.models import ApplyStatus
from jobhunter.pipeline import applylink
from jobhunter.pipeline.applylink import NO_FETCH_ERROR, get_apply_link, resolve_group, reverify
from jobhunter.pipeline.board_ids import board_key, is_board_host
from jobhunter.pipeline.dedupe_url import normalize_apply_url

LI = "https://www.linkedin.com/jobs/view/4012345678/"


# ─── board_key vectors ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("url", "key"),
    [
        ("https://www.linkedin.com/jobs/view/4012345678/", "linkedin:4012345678"),
        ("https://www.linkedin.com/jobs/view/4012345678", "linkedin:4012345678"),
        (
            "https://www.linkedin.com/jobs/view/senior-platform-engineer-at-acme-4012345678",
            "linkedin:4012345678",
        ),
        ("https://www.linkedin.com/comm/jobs/view/4012345678/?trk=eml", "linkedin:4012345678"),
        ("https://linkedin.com/jobs/view/4012345678?trk=x", "linkedin:4012345678"),
        ("https://de.linkedin.com/jobs/view/4012345678", "linkedin:4012345678"),
        (
            "https://www.linkedin.com/jobs/search/?currentJobId=4012345678&keywords=sre",
            "linkedin:4012345678",
        ),
        (
            "https://www.linkedin.com/jobs/collections/recommended/?currentJobId=4012345678",
            "linkedin:4012345678",
        ),
        ("https://www.indeed.com/viewjob?jk=0a1b2c3d4e5f6789", "indeed:0a1b2c3d4e5f6789"),
        ("https://www.indeed.com/jobs?q=sre&vjk=0a1b2c3d4e5f6789", "indeed:0a1b2c3d4e5f6789"),
        ("https://uk.indeed.com/viewjob?jk=0A1B2C3D4E5F6789", "indeed:0a1b2c3d4e5f6789"),
        ("https://ca.indeed.com/rc/clk?jk=0a1b2c3d4e5f6789&from=x", "indeed:0a1b2c3d4e5f6789"),
        ("https://www.indeed.co.uk/viewjob?jk=0a1b2c3d4e5f6789", "indeed:0a1b2c3d4e5f6789"),
        # non-matches
        ("https://www.linkedin.com/jobs/search/?keywords=sre", None),
        ("https://www.linkedin.com/in/some-profile-123456789", None),
        ("https://www.linkedin.com/company/acme/", None),
        ("https://www.indeed.com/jobs?q=sre", None),
        ("https://www.indeed.com/cmp/Acme", None),
        ("https://notlinkedin.com/jobs/view/4012345678", None),
        ("https://linkedin.com.evil.example/jobs/view/4012345678", None),
        ("https://boards.greenhouse.io/acme/jobs/4012345678", None),
        ("not a url", None),
        (None, None),
    ],
)
def test_board_key_vectors(url, key):
    assert board_key(url) == key


@pytest.mark.parametrize(
    ("host", "board"),
    [
        ("www.linkedin.com", True),
        ("linkedin.com", True),
        ("uk.indeed.com", True),
        ("www.indeed.co.uk", True),
        ("indeed.com", True),
        ("jobs.lever.co", False),
        ("notindeed.com", False),
        ("linkedin.com.example", False),
    ],
)
def test_board_hosts(host, board):
    assert is_board_host(host) is board


def test_normalize_apply_url_is_unchanged_for_board_urls():
    # Nightly URL merges keep their keys: board ids are a separate key, not a new normalization.
    assert (
        normalize_apply_url("https://www.linkedin.com/jobs/view/4012345678/?trk=abc&refId=1")
        == "https://www.linkedin.com/jobs/view/4012345678"
    )
    assert (
        normalize_apply_url("https://www.indeed.com/viewjob?jk=0a1b2c3d4e5f6789&from=serp")
        == "https://www.indeed.com/viewjob?from=serp&jk=0a1b2c3d4e5f6789"
    )


# ─── no-fetch list ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url", [LI, "https://uk.indeed.com/viewjob?jk=0a1b2c3d4e5f6789", "https://indeed.com/x"]
)
def test_resolve_group_makes_no_request_to_a_board_host(conn, factory, server: Server, url):
    gid = add_group(conn, (url, None))
    made: list[str] = []

    def counting(u):
        made.append(u)
        return factory(u)

    link = resolve_group(conn, gid, now=NOW, ctx_factory=counting)
    assert link.status == ApplyStatus.unresolved and link.error == NO_FETCH_ERROR
    assert made == [] and server.requests == []  # robots.txt included


def test_a_linkedin_redirect_wrapper_is_decoded_locally_and_the_ats_fetched(
    conn, factory, server: Server
):
    server.page(GH)
    wrapped = (
        "https://www.linkedin.com/redir?url=https%3A%2F%2Fboards.greenhouse.io%2Facme%2Fjobs%2F567"
    )
    gid = add_group(conn, (wrapped, None))
    link = resolve_group(conn, gid, now=NOW, ctx_factory=factory)
    assert link.status == ApplyStatus.live
    assert "linkedin.com" not in server.hosts(robots=True)


def test_a_redirect_into_a_board_host_stops_without_a_request(conn, factory, server: Server):
    server.redirect("https://careers.acme.example/j/1", LI)
    gid = add_group(conn, ("https://careers.acme.example/j/1", None))
    link = resolve_group(conn, gid, now=NOW, ctx_factory=factory)
    assert link.error == NO_FETCH_ERROR and link.status == ApplyStatus.unresolved
    assert "www.linkedin.com" not in server.hosts(robots=True)


def test_reverify_never_requests_a_board_host(conn, factory, server: Server):
    gid = add_group(conn, (LI, None))
    conn.execute(
        "INSERT INTO apply_link (job_group_id, start_url, final_url, chain, status, "
        "resolved_at, verified_at) VALUES (?, ?, ?, '[]', 'live', ?, ?)",
        (gid, LI, LI, NOW.isoformat(), NOW.isoformat()),
    )
    link = reverify(conn, gid, now=NOW + timedelta(days=3), ctx_factory=factory)
    assert link is not None and link.status == ApplyStatus.live
    assert server.requests == []
    assert get_apply_link(conn, gid).verified_at == NOW


def test_no_fetch_error_is_exported():
    assert applylink.NO_FETCH_ERROR.startswith("board host")


def test_reverify_never_follows_a_redirect_into_a_board_host(conn, factory, server: Server):
    start = "https://careers.acme.example/j/77"
    server.page(start)
    gid = add_group(conn, (start, None))
    assert resolve_group(conn, gid, now=NOW, ctx_factory=factory).status == ApplyStatus.live
    server.redirect(start, LI)
    link = reverify(conn, gid, now=NOW + timedelta(days=2), ctx_factory=factory)
    assert link is not None and link.error == NO_FETCH_ERROR
    assert "www.linkedin.com" not in server.hosts(robots=True)
