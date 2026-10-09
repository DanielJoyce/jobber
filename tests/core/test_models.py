"""Model validation tests (specs/003, 004, 006, 015). No network."""

from datetime import date

import pytest
from pydantic import ValidationError

from jobhunter.core.models import (
    ApplyStatus,
    Bucket,
    DescriptionCompleteness,
    EmploymentType,
    JobDetail,
    JobStub,
    LocationScope,
    Policy,
    Query,
    Remote,
    Screen,
    SourceClass,
    SourceRow,
    SourceStatus,
    Tier,
    Verdict,
)

REGISTRY_ROW = {
    "key": "tx-workintexas",
    "state": "TX",
    "class": "A",
    "name": "WorkInTexas",
    "family": "vos",
    "tier": "http",
    "entry": "https://www.workintexas.com/vosnet/Default.aspx",
    "verified": "2026-10-09",
    "family_signals": ["/vosnet/", "__VIEWSTATE", "Geographic Solutions"],
    "config": {
        "session_entry": "/vosnet/Default.aspx",
        "search_path": "/vosnet/joblist/joblist.aspx",
    },
    "queries": "from_profile",
    "pagination": {"kind": "postback", "max_pages": 25},
    "rate_limit": {"rps": 0.2, "concurrency": 1, "jitter": True},
    "robots": {"status": "unknown", "checked": None},
    "policy": "enabled",
    "expect": {"min_jobs_per_week": 100},
}


def _stub(**overrides):
    data = {
        "source_key": "tx-workintexas",
        "external_id": "12345",
        "title": "Systems Administrator",
        "url": "https://www.workintexas.com/vosnet/job/12345",
    }
    data.update(overrides)
    return data


def _screen(**overrides):
    data = {
        "verdict": "strong",
        "dimensions": {
            "skills": {"score": 85, "why": "Linux fleet operations match."},
            "seniority": {"score": 70, "why": "Mid-level scope."},
            "domain": {"score": 60, "why": "Infrastructure, adjacent to platform work."},
        },
        "raw_skills": 88,
        "recency_weighted_skills": 80,
        "stale_skills": [],
        "current_focus_overlap": 75,
        "done_with_hits": [],
        "evidence": [{"claim": "Runs Linux servers", "quote": "administer Linux servers"}],
        "blockers": [],
        "missing_info": ["salary not stated"],
        "shape_flags": [],
        "tailoring_hints": ["Lead with Kubernetes work"],
    }
    data.update(overrides)
    return data


def test_source_row_round_trips_registry_dict_with_class_alias():
    row = SourceRow.model_validate(REGISTRY_ROW)

    assert row.class_ is SourceClass.A
    assert row.tier is Tier.http
    assert row.policy is Policy.enabled
    assert row.verified == date(2026, 10, 9)
    assert row.rate_limit is not None and row.rate_limit.jitter is True
    assert row.queries == "from_profile"

    dumped = row.model_dump(mode="json", by_alias=True)
    assert dumped["class"] == "A"
    assert "class_" not in dumped
    assert SourceRow.model_validate(dumped) == row


def test_source_row_accepts_query_list():
    row = SourceRow.model_validate(
        {**REGISTRY_ROW, "queries": [{"keywords": ["linux"], "remote": "remote"}]}
    )
    assert isinstance(row.queries, list)
    assert isinstance(row.queries[0], Query)
    assert row.queries[0].remote is Remote.remote


def test_source_row_rejects_unknown_class():
    with pytest.raises(ValidationError):
        SourceRow.model_validate({**REGISTRY_ROW, "class": "Z"})


@pytest.mark.parametrize("url", ["https://example.com/job/1", "http://example.com/job/1"])
def test_job_stub_accepts_http_urls(url):
    assert JobStub.model_validate(_stub(url=url)).url == url


@pytest.mark.parametrize("url", ["ftp://example.com/job/1", "example.com/job/1", "javascript:x"])
def test_job_stub_rejects_non_http_urls(url):
    with pytest.raises(ValidationError):
        JobStub.model_validate(_stub(url=url))


def test_job_stub_apply_url_validated_when_present():
    stub = JobStub.model_validate(_stub(apply_url="https://employer.example/apply"))
    assert stub.apply_url == "https://employer.example/apply"
    assert JobStub.model_validate(_stub()).apply_url is None
    with pytest.raises(ValidationError):
        JobStub.model_validate(_stub(apply_url="mailto:jobs@example.com"))


def test_job_stub_defaults():
    stub = JobStub.model_validate(_stub())
    assert stub.needs_resolve is True
    assert stub.locations == []
    assert stub.extra == {}


def test_job_detail_extends_stub_with_federal_fields():
    detail = JobDetail.model_validate(
        _stub(
            description_raw="<p>Administer Linux servers.</p>",
            employment_type="full_time",
            description_completeness="partial",
            occupation_code="2210",
            pay_plan="GS",
            grade_low="12",
            grade_high="13",
        )
    )
    assert detail.employment_type is EmploymentType.full_time
    assert detail.description_completeness is DescriptionCompleteness.partial
    assert detail.occupation_code == "2210"
    assert isinstance(detail, JobStub)


def test_screen_accepts_model_output():
    screen = Screen.model_validate(_screen())
    assert screen.verdict is Verdict.strong
    assert set(screen.dimensions) == {"skills", "seniority", "domain"}


def test_screen_rejects_comp_dimension():
    bad = _screen(
        dimensions={
            **_screen()["dimensions"],
            "comp": {"score": 50, "why": "computed in Python, not by the model"},
        }
    )
    with pytest.raises(ValidationError):
        Screen.model_validate(bad)


def test_screen_rejects_score_over_100():
    bad = _screen(
        dimensions={
            "skills": {"score": 101, "why": "too high"},
            "seniority": {"score": 70, "why": "ok"},
            "domain": {"score": 60, "why": "ok"},
        }
    )
    with pytest.raises(ValidationError):
        Screen.model_validate(bad)


def test_screen_has_no_overall_field():
    assert "overall" not in Screen.model_fields


def test_screen_requires_at_least_one_evidence_item():
    with pytest.raises(ValidationError):
        Screen.model_validate(_screen(evidence=[]))


def test_enum_values_match_spec():
    assert [m.value for m in SourceClass] == ["A", "B", "C"]
    assert [m.value for m in Tier] == ["api", "feed", "http", "browser", "manual"]
    assert [m.value for m in Policy] == ["enabled", "blocked", "manual", "disabled"]
    assert [m.value for m in SourceStatus] == [
        "ok",
        "suspect",
        "broken",
        "blocked",
        "manual",
        "disabled",
    ]
    assert [m.value for m in LocationScope] == [
        "single",
        "multi_state",
        "nationwide",
        "remote_us",
        "negotiable",
        "overseas",
        "unknown",
    ]
    assert [m.value for m in Remote] == ["onsite", "hybrid", "remote", "unknown"]
    assert [m.value for m in EmploymentType] == [
        "full_time",
        "part_time",
        "temporary",
        "seasonal",
        "contract",
        "unknown",
    ]
    assert [m.value for m in DescriptionCompleteness] == ["full", "partial", "pasted"]
    assert [m.value for m in Verdict] == ["strong", "possible", "weak", "mismatch"]
    assert [m.value for m in Bucket] == ["A", "B", "C", "D", "E", "F", "G"]
    assert [m.value for m in ApplyStatus] == ["live", "expired", "unresolved", "blocked"]
    assert str(Tier.http) == "http"
