"""Alert parsers and family detection on saved .eml fixtures. No network, no mailbox.

The fixtures are synthetic (fictional employers): real alert formats are re-validated with
``jobhunter mail sample`` once subscriptions arrive.
"""

from __future__ import annotations

import base64
import email
import email.policy
from pathlib import Path

import pytest

from jobhunter.mail.message import from_eml, from_gmail, load_eml, scrub
from jobhunter.mail.parsers import detect_family, parse_message, row_for_host
from jobhunter.mail.parsers import generic as generic_parser
from jobhunter.mail.parsers.common import entries_from_links
from jobhunter.sources.registry import load_registry

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "mail"


@pytest.fixture(scope="module")
def rows():
    return load_registry()


def parsed(rows, rel: str):
    return parse_message(load_eml(FIX / rel), rows)


# ─── per family: detection + N entries ─────────────────────────────────────

CASES = [
    # fixture, family, origin row, entries
    ("vos/employflorida_virtual_recruiter.eml", "vos", "fl-employflorida", 3),
    ("vos/workintexas_plaintext.eml", "vos", "tx-workintexas", 2),
    ("joblink/illinoisjoblink_saved_search.eml", "joblink", "il-illinoisjoblink", 3),
    ("neogov/governmentjobs_job_alert.eml", "neogov", None, 2),
    ("nlx/usnlx_job_alert.eml", "nlx", "us-nlx", 2),
    ("generic/unknown_board.eml", "generic", None, 2),
    ("generic/pacareerlink_alert.eml", "generic", "pa-pacareerlink", 2),
]


@pytest.mark.parametrize(("rel", "family", "origin", "n"), CASES, ids=[c[0] for c in CASES])
def test_family_and_entry_count(rows, rel, family, origin, n):
    det, found = parsed(rows, rel)
    assert det.family == family
    assert (det.origin.key if det.origin else None) == origin
    assert len(found) == n
    for e in found:
        assert e.title and e.url.startswith("https://")
        assert "unsubscribe" not in e.url.lower()


def test_every_fixture_is_covered():
    names = {c[0] for c in CASES}
    on_disk = {str(p.relative_to(FIX)) for p in FIX.rglob("*.eml")}
    assert on_disk == names


def test_vos_alert_fields_and_state(rows):
    det, found = parsed(rows, "vos/employflorida_virtual_recruiter.eml")
    assert det.origin is not None and det.origin.state == "FL"
    first, second, _ = found
    assert first.title == "Systems Administrator II"
    assert first.employer == "Gulfside Widget Cooperative"
    assert first.location_raw == "Tallahassee, FL"
    assert first.posted_raw == "10/07/2026"
    assert "jobdetails.aspx?enc=" in first.url and "&amp;" not in first.url
    assert second.salary_raw == "$52,000 - $64,000 per year"


def test_vos_plaintext_alert(rows):
    _, found = parsed(rows, "vos/workintexas_plaintext.eml")
    assert [e.title for e in found] == ["Database Administrator", "Cloud Infrastructure Specialist"]
    assert found[0].employer == "Lone Star Fictional Freight LLC"
    assert found[0].location_raw == "Austin, TX"
    assert found[1].salary_raw == "$95,000 - $115,000 per year"


def test_joblink_cards_with_snippets(rows):
    _, found = parsed(rows, "joblink/illinoisjoblink_saved_search.eml")
    assert [e.url.rsplit("/", 1)[1] for e in found] == ["7712345", "7712399", "7712401"]
    assert found[0].employer == "Prairie State Fictional Insurance Co."
    assert found[0].location_raw == "Springfield, IL"
    assert found[0].snippet and "container platform" in found[0].snippet
    assert found[2].snippet is None
    # the saved-search link (/search/jobs, robots-disallowed) is never an entry
    assert all("/search/" not in e.url for e in found)


def test_neogov_labelled_rows(rows):
    _, found = parsed(rows, "neogov/governmentjobs_job_alert.eml")
    gis = found[0]
    assert gis.title == "GIS Analyst"
    assert gis.employer == "Example County (Fictional)"
    assert gis.location_raw == "Salem, OR"
    assert gis.salary_raw == "$62,000.00 - $80,000.00 Annually"
    assert gis.closes_raw and gis.closes_raw.startswith("10/31/2026")


def test_nlx_alert_links_are_detail_urls(rows):
    _, found = parsed(rows, "nlx/usnlx_job_alert.eml")
    assert found[0].url.endswith("/0123456789ABCDEF0123456789ABCDEF/job/")
    assert found[1].employer == "Example Mountain Logistics"
    assert found[1].location_raw == "Boise, ID"


def test_generic_splits_middot_fields_and_skips_navigation(rows):
    _, found = parsed(rows, "generic/unknown_board.eml")
    assert [e.title for e in found] == ["Maintenance Planner", "IT Support Lead"]
    assert found[0].employer == "Example Valley Water Board"
    assert found[0].location_raw == "Denver, CO"
    assert found[1].location_raw == "Remote"


def test_generic_bare_city_and_help_desk_title(rows):
    _, found = parsed(rows, "generic/pacareerlink_alert.eml")
    assert found[0].title == "Help Desk Technician"  # "help" alone is navigation; this is not
    assert found[1].location_raw == "Pittsburgh"


# ─── detection details ─────────────────────────────────────────────────────


def test_row_for_host_prefers_exact_then_parent(rows):
    assert row_for_host("usnlx.com", rows).key == "us-nlx"
    assert row_for_host("kyjobs.usnlx.com", rows).key == "ky-kyjobs"
    assert row_for_host("mail.employflorida.com", rows).key == "fl-employflorida"
    assert row_for_host("www.employflorida.com", rows).key == "fl-employflorida"
    assert row_for_host("illinois.gov", rows).key == "il-illinoisjoblink"
    assert row_for_host("example.org", rows) is None


def test_detection_by_links_when_sender_is_unknown(rows):
    msg = load_eml(FIX / "vos/employflorida_virtual_recruiter.eml")
    msg.sender = "bulk@mailer.example.net"
    det = detect_family(msg, rows)
    assert det.family == "vos" and det.origin is not None and det.origin.state == "FL"


def test_detection_by_body_signals(rows):
    msg = load_eml(FIX / "vos/employflorida_virtual_recruiter.eml")
    msg.sender = "bulk@mailer.example.net"
    msg.html = msg.html.replace("employflorida.com", "board.example.net")
    det = detect_family(msg, rows)
    assert det.family == "vos" and det.origin is None and det.reason == "body signals"


def test_family_parser_finding_nothing_falls_back_to_generic(rows):
    msg = load_eml(FIX / "generic/unknown_board.eml")
    msg.html += "<p>Powered by Virtual Recruiter</p>"  # a VOS signal, but no VOS links
    det, found = parse_message(msg, rows)
    assert det.family == "vos" and len(found) == 2


def test_generic_link_test():
    ok = generic_parser.is_job_link
    assert ok("https://jobs.example.org/jobs/123-analyst")
    assert ok("https://example.org/apply?jobId=77")
    assert not ok("https://example.org/jobs")
    assert not ok("https://example.org/unsubscribe?u=1")
    assert not ok("https://example.org/logo.png")


def test_entries_from_links_handles_empty_html():
    assert entries_from_links("", generic_parser.is_job_link) == []


# ─── message decoding ──────────────────────────────────────────────────────


def _gmail_resource(raw: bytes, mid: str) -> dict:
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    parts = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        data = base64.urlsafe_b64encode(part.get_content().encode()).decode().rstrip("=")
        parts.append({"mimeType": part.get_content_type(), "body": {"data": data}})
    headers = [{"name": k, "value": str(v)} for k, v in msg.items()]
    return {
        "id": mid,
        "historyId": "42",
        "payload": {"mimeType": "multipart/alternative", "headers": headers, "parts": parts},
    }


def test_gmail_and_eml_decode_the_same(rows):
    raw = (FIX / "joblink/illinoisjoblink_saved_search.eml").read_bytes()
    a = from_eml(raw, "m1")
    b = from_gmail(_gmail_resource(raw, "m1"))
    assert (a.sender, a.subject, a.sender_domain) == (b.sender, b.subject, b.sender_domain)
    assert a.html.strip() == b.html.strip() and a.text.strip() == b.text.strip()
    assert b.history_id == "42" and b.date == a.date
    assert len(parse_message(b, rows)[1]) == 3


def test_scrub_replaces_address_variants():
    text = (
        "To: Some.One+jobs@Example.com; cc some.one@example.com; "
        "link?e=some.one%2Bjobs%40example.com&x=some.one+alerts@example.com other@example.com"
    )
    out = scrub(text, ["some.one+jobs@example.com"])
    assert "some.one" not in out.lower()
    assert "other@example.com" in out


def test_shared_platform_host_matches_no_row():
    # governmentjobs.com serves many states' boards; a sender on it must not be
    # attributed to whichever state's row happens to come first.
    from jobhunter.core.models import SourceRow
    from jobhunter.mail.parsers import row_for_host

    def row(key: str, state: str, entry: str) -> SourceRow:
        return SourceRow.model_validate(
            {
                "key": key,
                "state": state,
                "class": "B",
                "name": key,
                "family": "neogov",
                "tier": "http",
                "entry": entry,
                "policy": "blocked",
            }
        )

    rows = [
        row("ak-employer", "AK", "https://www.governmentjobs.com/careers/alaska"),
        row("co-employer", "CO", "https://www.governmentjobs.com/careers/colorado"),
        row("fl-employflorida", "FL", "https://www.employflorida.com/vosnet/Default.aspx"),
    ]
    assert row_for_host("governmentjobs.com", rows) is None
    assert row_for_host("mail.governmentjobs.com", rows) is None
    assert row_for_host("employflorida.com", rows).key == "fl-employflorida"
