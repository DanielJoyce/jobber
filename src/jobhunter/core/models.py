"""Pydantic models: sources, jobs, scoring, apply links.

Specs: 003 (registry, adapter contract), 004 (list stage), 005 (data model),
006 (screen output), 011 (job_locations), 014 (filter vs scoring split), 015 (apply links).
"""

from datetime import date, datetime
from enum import StrEnum
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

# ─── Enums ──────────────────────────────────────────────────────────────────


class SourceClass(StrEnum):
    A = "A"  # job bank
    B = "B"  # state employer
    C = "C"  # federal


class Tier(StrEnum):
    api = "api"
    feed = "feed"
    http = "http"
    browser = "browser"
    manual = "manual"


class Policy(StrEnum):
    enabled = "enabled"
    blocked = "blocked"
    manual = "manual"
    disabled = "disabled"


class SourceStatus(StrEnum):
    ok = "ok"
    suspect = "suspect"
    broken = "broken"
    blocked = "blocked"
    manual = "manual"
    disabled = "disabled"


class LocationScope(StrEnum):
    single = "single"
    multi_state = "multi_state"
    nationwide = "nationwide"
    remote_us = "remote_us"
    negotiable = "negotiable"
    overseas = "overseas"
    unknown = "unknown"


class Remote(StrEnum):
    onsite = "onsite"
    hybrid = "hybrid"
    remote = "remote"
    unknown = "unknown"


class EmploymentType(StrEnum):
    full_time = "full_time"
    part_time = "part_time"
    temporary = "temporary"
    seasonal = "seasonal"
    contract = "contract"
    unknown = "unknown"


class DescriptionCompleteness(StrEnum):
    full = "full"
    partial = "partial"
    pasted = "pasted"


class Verdict(StrEnum):
    strong = "strong"
    possible = "possible"
    weak = "weak"
    mismatch = "mismatch"


class Bucket(StrEnum):
    A = "A"  # bullseye
    B = "B"  # strong
    C = "C"  # stretch up
    D = "D"  # lateral
    E = "E"  # downlevel
    F = "F"  # stale match
    G = "G"  # mismatch


class ApplyStatus(StrEnum):
    live = "live"
    expired = "expired"
    unresolved = "unresolved"
    blocked = "blocked"


# ─── Sources (registry row) ─────────────────────────────────────────────────


def _require_http_url(value: str) -> str:
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError(f"url must be an absolute http(s) URL: {value!r}")
    return value


class RateLimit(BaseModel):
    rps: float = Field(gt=0)
    concurrency: int = Field(default=1, ge=1)
    jitter: bool = False


class RobotsRecord(BaseModel):
    status: str = "unknown"
    checked: datetime | None = None


class Query(BaseModel):
    keywords: list[str] | None = None
    title: str | None = None
    onet_soc: str | None = None
    occupation_code: str | None = None
    posted_within_days: int | None = Field(default=None, ge=0)
    remote: Remote | None = None
    radius_miles: int | None = Field(default=None, ge=0)
    salary_min: int | None = Field(default=None, ge=0)
    grade_low: str | None = None
    grade_high: str | None = None


class SourceRow(BaseModel):
    """One row of registry.yaml (specs/003 "The registry")."""

    model_config = ConfigDict(populate_by_name=True)

    key: str
    state: str | None = None
    class_: SourceClass = Field(alias="class")
    name: str
    family: str
    tier: Tier
    entry: str
    verified: date | None = None
    family_signals: list[str] = Field(default_factory=list)
    config: dict[str, Any] = Field(default_factory=dict)
    queries: Literal["from_profile"] | list[Query] = "from_profile"
    pagination: dict[str, Any] = Field(default_factory=dict)
    rate_limit: RateLimit | None = None
    robots: RobotsRecord = Field(default_factory=RobotsRecord)
    policy: Policy = Policy.enabled
    expect: dict[str, Any] = Field(default_factory=dict)

    @field_validator("entry")
    @classmethod
    def _entry_is_http(cls, value: str) -> str:
        return _require_http_url(value)


# ─── Jobs (list / resolve stages) ───────────────────────────────────────────


class JobLocation(BaseModel):
    """One row of job_locations (specs/011)."""

    state: str | None = None
    city: str | None = None
    county: str | None = None
    lat: float | None = None
    lon: float | None = None
    is_primary: bool = False


class JobStub(BaseModel):
    """Output of adapter.search (specs/004 stage 2)."""

    source_key: str
    external_id: str
    title: str
    url: str
    posted_at: datetime | None = None
    closes_at: datetime | None = None
    location_raw: str | None = None
    salary_raw: str | None = None
    agency_raw: str | None = None
    description_raw: str | None = None
    # 'partial' for email-alert stubs (specs/012); the list stage stores it on insert.
    description_completeness: DescriptionCompleteness = DescriptionCompleteness.full
    needs_resolve: bool = True
    apply_url: str | None = None
    locations: list[JobLocation] = Field(default_factory=list)
    extra: dict[str, Any] = Field(default_factory=dict)

    @field_validator("url")
    @classmethod
    def _url_is_http(cls, value: str) -> str:
        return _require_http_url(value)

    @field_validator("apply_url")
    @classmethod
    def _apply_url_is_http(cls, value: str | None) -> str | None:
        return None if value is None else _require_http_url(value)


class JobDetail(JobStub):
    """Output of adapter.resolve (specs/004 stage 3). Stub fields plus parsed detail."""

    employment_type: EmploymentType = EmploymentType.unknown
    # Federal-only structured signals (specs/011), None elsewhere.
    occupation_code: str | None = None
    pay_plan: str | None = None
    grade_low: str | None = None
    grade_high: str | None = None


# ─── Fit scoring (Stage 2 screen output, specs/006) ─────────────────────────


class Dimension(BaseModel):
    score: int = Field(ge=0, le=100)
    why: str = Field(max_length=300)


class Evidence(BaseModel):
    claim: str
    quote: str  # must be verbatim from the posting; checked after parsing


class Screen(BaseModel):
    """Model output. `overall` is deliberately absent: it is computed in Python (specs/014)."""

    verdict: Verdict
    # comp and location are computed in Python, never by the model (specs/014).
    dimensions: dict[Literal["skills", "seniority", "domain"], Dimension]
    raw_skills: int = Field(ge=0, le=100)
    recency_weighted_skills: int = Field(ge=0, le=100)
    stale_skills: list[str]
    current_focus_overlap: int = Field(ge=0, le=100)
    done_with_hits: list[str]
    evidence: list[Evidence] = Field(min_length=1, max_length=6)
    blockers: list[str]
    missing_info: list[str]
    shape_flags: list[str]
    tailoring_hints: list[str]


# ─── Apply links (specs/015) ────────────────────────────────────────────────


class ApplyHop(BaseModel):
    url: str
    host: str
    method: Literal["unwrap", "3xx", "meta", "js", "browser"]
    status: int | None = None


class ApplyLink(BaseModel):
    job_group_id: int
    start_url: str
    final_url: str | None = None
    chain: list[ApplyHop] = Field(default_factory=list)
    ats: str | None = None
    employer_host: str | None = None
    status: ApplyStatus
    resolved_at: datetime
    verified_at: datetime | None = None
    error: str | None = None
