"""Wyoming at Work adapter (hire.wyo.gov; specs/003-sources-and-adapters.md#4-nextjs-custom).

``hire.wyo.gov`` is the "Career Edge" Next.js platform. The ``/home`` shell only references
``/api/workflow``; the job board itself is the ``employer-jobseeker`` micro-frontend
(``/job/search`` redirects to ``/employer-jobseeker/job/search``) whose page bundle calls an
anonymous JSON API (found 2026-10-09 in ``_next/static`` chunks; no login, no cookie):

- search: ``GET https://hire.wyo.gov/employer-api/api/jobs/search`` with ``skip`` and ``limit``
  (both required integers; the site offers 10, 30 or 50), ``sort=desc`` (newest first, by
  ``subValues.createdAt``), ``searchText`` (title/company/keyword), ``location`` (city),
  ``postDate`` (``MM/DD/YYYY`` lower bound, the site's "Date Posted" filter) and optionally
  ``jobSource=internal`` (hides the NLx-fed listings). Response:
  ``{"statusCode", "data": [job...], "totalRecords"}``. Each listing carries the full
  description (``"Job Description"``) but only a ``redirectCompanyWebsite`` flag for apply.
- detail: ``GET .../employer-api/api/jobs/search/<_id>``: the same posting with
  ``jobDetails["Job Description"]`` and ``howToApplyJob.redirectCompanyWebsite`` (the apply
  link). The human-facing page is ``https://hire.wyo.gov/job/<_id>``.

About two thirds of the board is NLx-fed (``source: "NLX"``); the rest are Wyoming Workforce
Services employer postings (``source: "case manager"``). Dedupe collapses overlap with the
national NLx source.

Robots: ``hire.wyo.gov/robots.txt`` is not a robots file (the Next.js HTML shell, 200), so the
host is treated as absent/allowed (specs/008). Only ``FetchContext`` is used for I/O.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from jobhunter.core.models import (
    EmploymentType,
    JobDetail,
    JobLocation,
    JobStub,
    Query,
    SourceRow,
)

if TYPE_CHECKING:
    from jobhunter.core.fetch import FetchContext

SITE = "https://hire.wyo.gov"
API_BASE = f"{SITE}/employer-api/api"
DEFAULT_PAGE_SIZE = 30  # the site's own choices are 10, 30, 50
DEFAULT_MAX_PAGES = 20
# A watermark before this means "no watermark": do not send it as a postDate filter.
_MIN_WATERMARK = datetime(2000, 1, 1, tzinfo=UTC)


# ─── query building ─────────────────────────────────────────────────────────


def search_text(query: Query) -> str:
    """Query -> ``searchText``: the title followed by any keywords, space-joined."""
    parts = [query.title or ""]
    parts.extend(query.keywords or [])
    return " ".join(p.strip() for p in parts if p and p.strip())


def api_base(src: SourceRow) -> str:
    return str(src.config.get("api_base", API_BASE)).rstrip("/")


def build_params(
    src: SourceRow, query: Query, since: datetime, skip: int, page_size: int
) -> dict[str, Any]:
    params: dict[str, Any] = {"skip": skip, "limit": page_size, "sort": "desc"}
    text = search_text(query)
    if text:
        params["searchText"] = text
    location = src.config.get("location")
    if location:
        params["location"] = location
    if since >= _MIN_WATERMARK:
        params["postDate"] = since.strftime("%m/%d/%Y")
    if src.config.get("job_source"):
        params["jobSource"] = src.config["job_source"]
    return params


# ─── parsing ────────────────────────────────────────────────────────────────


def _ms(value: Any) -> datetime | None:
    """Epoch milliseconds (int or digit string) -> aware UTC datetime."""
    try:
        ms = int(value)
    except (TypeError, ValueError):
        return None
    if ms <= 0:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _http(url: Any) -> str | None:
    if isinstance(url, str) and url.strip().lower().startswith(("http://", "https://")):
        return url.strip()
    return None


def _worksites(item: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for entry in (item.get("subValues") or {}).get("location") or []:
        loc = (entry or {}).get("location") if isinstance(entry, dict) else None
        if isinstance(loc, dict) and (loc.get("city") or loc.get("state")):
            out.append(loc)
    return out


def _locations(item: dict[str, Any]) -> list[JobLocation]:
    return [
        JobLocation(
            state=loc.get("state") or None,
            city=(loc.get("city") or "").strip() or None,
            is_primary=i == 0,
        )
        for i, loc in enumerate(_worksites(item))
    ]


def _location_raw(item: dict[str, Any]) -> str | None:
    sites = _worksites(item)
    if not sites:
        return None
    first = sites[0]
    return ", ".join(p for p in ((first.get("city") or "").strip(), first.get("state")) if p)


def _employment_type(value: Any) -> EmploymentType:
    text = str(value or "").lower().replace("_", "-")
    if "full" in text:
        return EmploymentType.full_time
    if "part" in text:
        return EmploymentType.part_time
    if "season" in text:
        return EmploymentType.seasonal
    if "temp" in text:
        return EmploymentType.temporary
    if "contract" in text:
        return EmploymentType.contract
    return EmploymentType.unknown


def _salary_raw(item: dict[str, Any]) -> str | None:
    sub = item.get("subValues") or {}
    if sub.get("doNotDisplaySalaryRequirement"):
        return None
    low, high = sub.get("minimumSalary") or 0, sub.get("maximumSalary") or 0
    if not low and not high:
        return None
    unit = f" per {str(sub['salaryInterval']).lower()}" if sub.get("salaryInterval") else ""
    span = f"{low:g}-{high:g}" if low and high and high != low else f"{(high or low):g}"
    return f"${span}{unit}"


def job_url(job_id: str) -> str:
    return f"{SITE}/job/{job_id}"


def freshness(item: dict[str, Any]) -> datetime | None:
    """The key the API sorts by (``subValues.createdAt``): when the board listed the job."""
    return _ms((item.get("subValues") or {}).get("createdAt"))


def stub_from_item(src: SourceRow, item: dict[str, Any]) -> JobStub:
    job_id = str(item["_id"])
    card = item.get("cardDetails") or {}
    desc = item.get("Job Description") or ""
    return JobStub(
        source_key=src.key,
        external_id=job_id,
        title=(card.get("jobName") or "").strip(),
        url=job_url(job_id),
        posted_at=_ms(item.get("jobCreatedAt")) or freshness(item),
        closes_at=_ms((item.get("subValues") or {}).get("jobExpiryDate")),
        location_raw=_location_raw(item),
        salary_raw=_salary_raw(item),
        agency_raw=(card.get("companyName") or "").strip() or None,
        description_raw=desc if desc.strip() else None,
        # The listing has the description but not the apply link: resolve fills it in.
        needs_resolve=True,
        locations=_locations(item),
        extra={
            "job_id": job_id,
            "origin": item.get("source"),
            "external": bool(item.get("isExternal")),
            "job_type": (item.get("subValues") or {}).get("jobType"),
        },
    )


def apply_link(item: dict[str, Any]) -> str | None:
    """The employer's own application link: the detail's redirect, else the company site."""
    how = item.get("howToApplyJob") or {}
    company = item.get("companyDetails") or {}
    return _http(how.get("redirectCompanyWebsite")) or _http(company.get("companyWebsite"))


# ─── adapter ────────────────────────────────────────────────────────────────


class WyomingAdapter:
    family = "wyo"

    def search(
        self, src: SourceRow, query: Query, since: datetime, ctx: FetchContext
    ) -> Iterator[JobStub]:
        if since.tzinfo is None:
            since = since.replace(tzinfo=UTC)
        endpoint = f"{api_base(src)}/jobs/search"
        page_size = int(src.config.get("page_size", DEFAULT_PAGE_SIZE))
        max_pages = int(src.pagination.get("max_pages", DEFAULT_MAX_PAGES))
        skip, pages = 0, 0
        while pages < max_pages:
            body = ctx.get(
                endpoint,
                params=build_params(src, query, since, skip, page_size),
                headers={"Accept": "application/json"},
            ).json()
            pages += 1
            jobs = body.get("data") or []
            # Newest first by listing time: skip stale rows and stop at the first page that
            # holds nothing newer than the watermark.
            fresh = 0
            for item in jobs:
                if not item.get("_id") or item.get("isHidden"):
                    continue
                listed = freshness(item)
                if listed is not None and listed < since:
                    continue
                fresh += 1
                yield stub_from_item(src, item)
            skip += len(jobs)
            if not jobs or fresh == 0 or skip >= int(body.get("totalRecords") or 0):
                return

    def resolve(self, stub: JobStub, ctx: FetchContext) -> JobDetail:
        job_id = stub.extra.get("job_id") or stub.external_id
        body = ctx.get(
            f"{API_BASE}/jobs/search/{job_id}", headers={"Accept": "application/json"}
        ).json()
        item = body.get("data") or {}
        details = item.get("jobDetails") or {}
        card = item.get("cardDetails") or {}
        sub = item.get("subValues") or {}
        html = (details.get("Job Description") or "").strip() or None
        data = stub.model_dump()
        data.update(
            description_raw=html or stub.description_raw,
            apply_url=apply_link(item) or stub.apply_url,
            needs_resolve=False,
            title=(card.get("jobName") or "").strip() or stub.title,
            agency_raw=(card.get("companyName") or "").strip() or stub.agency_raw,
            location_raw=_location_raw(item) or stub.location_raw,
            salary_raw=_salary_raw(item) or stub.salary_raw,
            closes_at=_ms(sub.get("jobExpiryDate")) or stub.closes_at,
            locations=_locations(item) or stub.locations,
            employment_type=_employment_type(sub.get("jobType")),
        )
        return JobDetail(**data)
