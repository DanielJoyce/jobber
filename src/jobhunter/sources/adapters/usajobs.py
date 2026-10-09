"""USAJOBS adapter (specs/011-federal-and-geography.md).

A keyed, documented API. The search response carries the full description, so stubs are built
with ``needs_resolve=False`` and ``resolve`` never touches the network.
"""

from __future__ import annotations

import math
import os
from collections.abc import Iterator
from datetime import UTC, datetime
from html import escape
from typing import TYPE_CHECKING, Any

from jobhunter.core.geo import normalize_state
from jobhunter.core.models import JobDetail, JobLocation, JobStub, Query, SourceRow

if TYPE_CHECKING:
    from jobhunter.core.fetch import FetchContext
    from jobhunter.scoring.profile import Profile

DEFAULT_RESULTS_PER_PAGE = 500
MAX_DATE_POSTED_DAYS = 60
MAX_PAGES = 100

# RateIntervalCode -> display label.
INTERVALS: dict[str, str] = {
    "PA": "per year",
    "PH": "per hour",
    "PM": "per month",
    "PW": "per week",
    "PD": "per day",
    "BW": "per biweekly pay period",
    "WC": "without compensation",
}
# The subset that fits job.salary_period; BW/WC are recorded in salary_raw only.
INTERVAL_PERIOD: dict[str, str] = {
    "PA": "year",
    "PH": "hour",
    "PM": "month",
    "PW": "week",
    "PD": "day",
}
_US_NAMES = {"united states", "united states of america", "us", "usa"}


class UsajobsAuthMissing(RuntimeError):
    """The env vars holding the USAJOBS registered email / API key are unset."""

    def __init__(self, names: list[str]) -> None:
        super().__init__(
            "USAJOBS credentials missing: set environment variable(s) " + ", ".join(names)
        )
        self.names = names


def _auth_headers(src: SourceRow) -> dict[str, str]:
    auth = src.config.get("auth") or {}
    email_env = auth.get("email_env", "USAJOBS_EMAIL")
    key_env = auth.get("key_env", "USAJOBS_API_KEY")
    email, key = os.environ.get(email_env), os.environ.get(key_env)
    missing = [n for n, v in ((email_env, email), (key_env, key)) if not v]
    if missing:
        raise UsajobsAuthMissing(missing)
    return {"Host": "data.usajobs.gov", "User-Agent": str(email), "Authorization-Key": str(key)}


def _date_posted_days(query: Query, since: datetime, now: datetime) -> int:
    if since.tzinfo is None:
        since = since.replace(tzinfo=UTC)
    days = math.ceil((now - since).total_seconds() / 86400)
    if query.posted_within_days is not None:
        days = min(days, query.posted_within_days)
    return max(0, min(MAX_DATE_POSTED_DAYS, days))


def build_params(
    src: SourceRow, query: Query, since: datetime, page: int, now: datetime | None = None
) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if query.keywords:
        params["Keyword"] = " ".join(query.keywords)
    if query.title:
        params["PositionTitle"] = query.title
    if query.occupation_code:
        params["JobCategoryCode"] = query.occupation_code
    if query.salary_min is not None:
        params["RemunerationMinimumAmount"] = query.salary_min
    if query.grade_low:
        params["PayGradeLow"] = query.grade_low
    if query.grade_high:
        params["PayGradeHigh"] = query.grade_high
    params["DatePosted"] = _date_posted_days(query, since, now or datetime.now(UTC))
    params["ResultsPerPage"] = int(src.config.get("results_per_page", DEFAULT_RESULTS_PER_PAGE))
    params["Page"] = page
    params["SortField"] = "opendate"
    params["SortDirection"] = "desc"
    return params


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def _money(value: Any) -> str | None:
    try:
        return f"${float(str(value).replace(',', '')):,.0f}"
    except (TypeError, ValueError):
        return None


def format_salary(remuneration: list[dict[str, Any]]) -> str | None:
    if not remuneration:
        return None
    r = remuneration[0]
    lo, hi = _money(r.get("MinimumRange")), _money(r.get("MaximumRange"))
    code = r.get("RateIntervalCode") or ""
    label = INTERVALS.get(code, r.get("Description") or code)
    amount = f"{lo} - {hi}" if lo and hi and lo != hi else (lo or hi)
    if not amount:
        return label or None
    return f"{amount} {label}".strip()


def _text_block(heading: str, body: Any) -> str:
    if not body:
        return ""
    if isinstance(body, list):
        items = [str(b) for b in body if b]
        inner = "<ul>" + "".join(f"<li>{escape(i)}</li>" for i in items) + "</ul>" if items else ""
    else:
        paras = [p.strip() for p in str(body).splitlines() if p.strip()]
        inner = "".join(f"<p>{escape(p)}</p>" for p in paras)
    return f"<h2>{escape(heading)}</h2>{inner}" if inner else ""


def build_description(desc: dict[str, Any]) -> str | None:
    details = (desc.get("UserArea") or {}).get("Details") or {}
    html = "".join(
        [
            _text_block("Summary", details.get("JobSummary")),
            _text_block("Major Duties", details.get("MajorDuties")),
            _text_block("Qualifications", desc.get("QualificationSummary")),
            _text_block("Requirements", details.get("Requirements")),
            _text_block("Education", details.get("Education")),
        ]
    )
    return html or None


def _locations(desc: dict[str, Any]) -> list[JobLocation]:
    out: list[JobLocation] = []
    seen: set[tuple[str | None, str | None]] = set()
    for loc in desc.get("PositionLocation") or []:
        country = (loc.get("CountryCode") or "").strip().casefold()
        domestic = not country or country in _US_NAMES
        state = normalize_state(loc.get("CountrySubDivisionCode")) if domestic else None
        city = loc.get("CityName") or loc.get("LocationName")
        if (state, city) in seen:
            continue
        seen.add((state, city))
        out.append(
            JobLocation(
                state=state,
                city=city,
                lat=loc.get("Latitude"),
                lon=loc.get("Longitude"),
                is_primary=not out,
            )
        )
    return out


def _first_code(items: list[dict[str, Any]] | None) -> str | None:
    return next((i["Code"] for i in items or [] if i.get("Code")), None)


def stub_from_item(src: SourceRow, item: dict[str, Any]) -> JobStub:
    desc = item["MatchedObjectDescriptor"]
    details = (desc.get("UserArea") or {}).get("Details") or {}
    remun = desc.get("PositionRemuneration") or []
    apply_uris = desc.get("ApplyURI") or []
    r0 = remun[0] if remun else {}
    return JobStub(
        source_key=src.key,
        external_id=str(item["MatchedObjectId"]),
        title=desc["PositionTitle"],
        url=desc["PositionURI"],
        apply_url=apply_uris[0] if apply_uris else None,
        agency_raw=desc.get("OrganizationName"),
        posted_at=_parse_dt(desc.get("PublicationStartDate")),
        closes_at=_parse_dt(desc.get("ApplicationCloseDate")),
        location_raw=desc.get("PositionLocationDisplay"),
        salary_raw=format_salary(remun),
        description_raw=build_description(desc),
        needs_resolve=False,
        locations=_locations(desc),
        extra={
            "department": desc.get("DepartmentName"),
            "occupation_code": _first_code(desc.get("JobCategory")),
            "pay_plan": _first_code(desc.get("JobGrade")),
            "grade_low": details.get("LowGrade"),
            "grade_high": details.get("HighGrade"),
            "telework_eligible": details.get("TeleworkEligible"),
            "remote_indicator": details.get("RemoteIndicator"),
            "salary_min": r0.get("MinimumRange"),
            "salary_max": r0.get("MaximumRange"),
            "rate_interval_code": r0.get("RateIntervalCode"),
            "salary_period": INTERVAL_PERIOD.get(r0.get("RateIntervalCode") or ""),
            "schedule": [s.get("Name") for s in desc.get("PositionSchedule") or []],
        },
    )


class UsajobsAdapter:
    family = "usajobs"

    def search(
        self, src: SourceRow, query: Query, since: datetime, ctx: FetchContext
    ) -> Iterator[JobStub]:
        headers = _auth_headers(src)
        if since.tzinfo is None:
            since = since.replace(tzinfo=UTC)
        page, total_pages = 1, 1
        while page <= min(total_pages, MAX_PAGES):
            params = build_params(src, query, since, page)
            body = ctx.get(src.entry, params=params, headers=headers).json()
            result = body.get("SearchResult") or {}
            total_pages = int(((result.get("UserArea") or {}).get("NumberOfPages")) or 1)
            for item in result.get("SearchResultItems") or []:
                stub = stub_from_item(src, item)
                if stub.posted_at is not None and stub.posted_at < since:
                    return
                yield stub
            page += 1

    def resolve(self, stub: JobStub, ctx: FetchContext) -> JobDetail:
        extra = stub.extra
        return JobDetail(
            **stub.model_dump(),
            occupation_code=extra.get("occupation_code"),
            pay_plan=extra.get("pay_plan"),
            grade_low=extra.get("grade_low"),
            grade_high=extra.get("grade_high"),
        )


def queries_from_profile(profile: Profile) -> list[Query]:
    """Profile search net -> USAJOBS queries (specs/011 "Registry row").

    The salary floor is deliberately NOT sent as RemunerationMinimumAmount. USAJOBS
    filters on a posting's minimum, so a $110k-$150k job would vanish against a $125k
    floor. Queries are a broad net (specs/006 Stage 0); the prefilter compares the floor
    against the top of the stated range instead.
    """
    queries = [Query(**q.model_dump(exclude_none=True)) for q in profile.queries]
    seen = {q.title.casefold() for q in queries if q.title}
    for title in profile.target_titles:
        if title.casefold() not in seen:
            seen.add(title.casefold())
            queries.append(Query(title=title))
    return queries
