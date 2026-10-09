"""Locations stage: job_locations rows and location_scope (specs/011 "blending problem").

Jobs and locations are many-to-many. Structured locations from the adapter win; otherwise
``location_raw`` is parsed; otherwise the source's own state is the fallback. Unrecognized
text becomes a ``parse_warnings`` entry, never a guessed state.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterable, Sequence
from typing import Any

from jobhunter.core import geo
from jobhunter.core.models import JobLocation, LocationScope
from jobhunter.core.textnorm import detect_remote
from jobhunter.pipeline.listing import _txn

WARNING_PREFIX = "locations: "
REMOTE_SCOPES = (LocationScope.remote_us, LocationScope.nationwide, LocationScope.negotiable)

# Phrases safe to match anywhere (location_raw, title, description).
_NATIONWIDE_PHRASE = re.compile(
    r"\banywhere\s+in\s+(?:the\s+)?(?:u\.?s\.?a?|united\s+states)(?![a-z])"
    r"|\bmultiple\s+locations\s+nationwide\b|\bnationwide\s+(?:locations|positions?)\b",
    re.I,
)
# Bare "nationwide" is only trusted in the short fields; in prose it means "nationwide network".
_NATIONWIDE_WORD = re.compile(r"\bnationwide\b", re.I)
_NEGOTIABLE = re.compile(
    r"\blocations?\s+(?:is\s+|are\s+)?negotiable\b|\bnegotiable\s+(?:after\s+selection|location)"
    r"|\bduty\s+station\s+negotiable\b",
    re.I,
)
# Segments that carry a scope signal rather than a place; skipped without a warning.
_SIGNAL_SEGMENT = re.compile(
    r"^(?:nationwide|remote|telework|teleworker?|virtual|work\s+from\s+home|anywhere(?:\s+in.*)?|"
    r"(?:multiple|various|many)\s+locations?(?:\s+nationwide)?|many\s+vacancies.*|"
    r"locations?\s+negotiable|negotiable.*|(?:u\.?s\.?a?|united\s+states)(?:\s+remote)?)$",
    re.I,
)
_REMOTE_PAREN = re.compile(r"\([^)]*\b(?:remote|telework|hybrid|virtual)\b[^)]*\)", re.I)
_REMOTE_PREFIX = re.compile(r"^(?:remote|telework|virtual)\s*[-\u2013:/]\s*", re.I)
_SPLIT = re.compile(r"[;|\n]+")

_COUNTRY_NAMES = (
    "Afghanistan, Australia, Austria, Bahrain, Belgium, Brazil, Canada, China, Colombia, Cuba, "
    "Cyprus, Czech Republic, Denmark, Djibouti, Dominican Republic, Egypt, El Salvador, England, "
    "Ethiopia, France, Germany, Great Britain, Greece, Honduras, Hungary, India, Iraq, Ireland, "
    "Israel, Italy, Japan, Jordan, Kenya, Korea, Kuwait, Mexico, Netherlands, New Zealand, "
    "Nigeria, Norway, Pakistan, Panama, Peru, Philippines, Poland, Portugal, Qatar, "
    "Republic of Korea, Romania, Russia, Saudi Arabia, Scotland, Singapore, South Africa, "
    "South Korea, Spain, Switzerland, Taiwan, Thailand, Turkey, UK, Ukraine, "
    "United Arab Emirates, United Kingdom, Wales"
)
_COUNTRIES = {geo._key(c) for c in _COUNTRY_NAMES.split(",")}


def _is_foreign(text: str) -> bool:
    """True if the last comma-separated part (or the whole text) names a non-US country."""
    return geo._key(text.rsplit(",", 1)[-1]) in _COUNTRIES


def _field(job: Any, key: str) -> str | None:
    try:
        value = job[key]
    except (KeyError, IndexError):
        return None
    return value if isinstance(value, str) else None


def _clean_segment(seg: str) -> str:
    seg = _REMOTE_PAREN.sub("", seg)
    seg = _REMOTE_PREFIX.sub("", seg.strip())
    return " ".join(seg.split()).strip(" -\u2013,")


def _from_structured(
    stub_locations: Iterable[JobLocation], warnings: list[str]
) -> list[JobLocation]:
    out: list[JobLocation] = []
    for loc in stub_locations:
        if loc.state is None and loc.city is None:
            continue  # synthetic row; re-derived from text signals
        plain = loc.model_copy(update={"is_primary": False})
        if loc.state is None:
            out.append(plain)
            continue
        code = geo.normalize_state(loc.state)
        if code is not None:
            out.append(plain.model_copy(update={"state": code}))
        elif _is_foreign(loc.state) or (loc.city and _is_foreign(loc.city)):
            out.append(plain.model_copy(update={"state": None}))
        else:
            warnings.append(f"{WARNING_PREFIX}unrecognized state {loc.state[:40]!r}")
    return out


def _from_text(raw: str | None, warnings: list[str]) -> list[JobLocation]:
    out: list[JobLocation] = []
    if not raw or not raw.strip():
        return out
    for piece in _SPLIT.split(raw):
        seg = _clean_segment(piece)
        if not seg or _SIGNAL_SEGMENT.match(seg):
            continue
        if _is_foreign(seg):
            out.append(JobLocation(state=None, city=seg))
            continue
        city, code = geo.parse_city_state(seg)
        if code is not None:
            out.append(JobLocation(state=code, city=city))
            continue
        bare = geo.normalize_state(seg)
        if bare is not None:
            out.append(JobLocation(state=bare))
            continue
        warnings.append(f"{WARNING_PREFIX}unrecognized location {seg[:60]!r}")
    return out


def _dedupe(locs: Iterable[JobLocation]) -> list[JobLocation]:
    seen: set[tuple[str, str]] = set()
    out: list[JobLocation] = []
    for loc in locs:
        key = (loc.state or "", (loc.city or "").casefold())
        if key not in seen:
            seen.add(key)
            out.append(loc)
    return out


def _signals(title: str, loc_raw: str, description: str) -> LocationScope | None:
    """Text-signal scope, strongest first: nationwide > negotiable > remote_us."""
    short = f"{title}\n{loc_raw}"
    full = f"{short}\n{description}"
    if _NATIONWIDE_PHRASE.search(full) or _NATIONWIDE_WORD.search(short):
        return LocationScope.nationwide
    if _NEGOTIABLE.search(full):
        return LocationScope.negotiable
    if detect_remote(title, loc_raw, None) == "remote":
        return LocationScope.remote_us
    return None


def derive_locations(
    job: Any,
    stub_locations: Sequence[JobLocation] | None,
    source_state: str | None,
) -> tuple[list[JobLocation], LocationScope, list[str]]:
    """Return (rows, scope, warnings) for one job.

    ``job`` is a row or mapping with ``title``, ``location_raw`` and ``description_text`` (or
    ``description_raw``). Warnings carry the ``locations: `` prefix.
    """
    warnings: list[str] = []
    title = _field(job, "title") or ""
    loc_raw = _field(job, "location_raw") or ""
    description = _field(job, "description_text") or _field(job, "description_raw") or ""

    locs = _dedupe(_from_structured(stub_locations or (), warnings))
    if not locs:
        locs = _dedupe(_from_text(loc_raw, warnings))

    signal = _signals(title, loc_raw, description)

    if not locs:
        fallback = geo.by_usps(source_state) if source_state else None
        if fallback is not None:
            locs = [JobLocation(state=fallback.usps)]
            if signal is None and loc_raw.strip():
                warnings.append(
                    f"{WARNING_PREFIX}no usable location in {loc_raw[:60]!r}; "
                    f"fell back to source state {fallback.usps}"
                )
        elif signal is None and loc_raw.strip():
            warnings.append(f"{WARNING_PREFIX}no usable location in {loc_raw[:60]!r}")

    states = {loc.state for loc in locs if loc.state}
    if signal is not None:
        scope = signal
    elif not locs:
        scope = LocationScope.unknown
    elif not states:
        scope = LocationScope.overseas
    elif len(states) > 1:
        scope = LocationScope.multi_state
    else:
        scope = LocationScope.single

    rows = list(locs)
    if scope in REMOTE_SCOPES:
        rows.append(JobLocation(state=None, city=None))  # synthetic "anywhere" row
    if rows:
        rows[0] = rows[0].model_copy(update={"is_primary": True})
    return rows, scope, warnings


def _merge_warnings(existing: str | None, new: list[str]) -> str | None:
    old: list[str] = json.loads(existing) if existing else []
    merged = [w for w in old if not w.startswith(WARNING_PREFIX)] + new
    return json.dumps(merged) if merged else None


def load_locations(conn: sqlite3.Connection, job_id: int) -> list[JobLocation]:
    rows = conn.execute(
        "SELECT state, city, county, lat, lon, is_primary FROM job_locations "
        "WHERE job_id = ? ORDER BY is_primary DESC, id",
        (job_id,),
    ).fetchall()
    return [
        JobLocation(
            state=r["state"],
            city=r["city"],
            county=r["county"],
            lat=r["lat"],
            lon=r["lon"],
            is_primary=bool(r["is_primary"]),
        )
        for r in rows
    ]


def apply_locations(
    conn: sqlite3.Connection, *, limit: int | None = None, force: bool = False
) -> int:
    """Derive and store job_locations + location_scope; return jobs processed.

    Targets normalized-or-later jobs whose scope is still 'unknown' (``force``: all of them).
    Rows the list stage stored from adapter-structured locations are the structured input.
    Each job's rows are replaced atomically.
    """
    where = "j.stage NOT IN ('listed', 'resolved')"
    if not force:
        where += " AND j.location_scope = 'unknown'"
    sql = (
        "SELECT j.*, s.state AS source_state FROM job j "
        f"LEFT JOIN source s ON s.key = j.source_key WHERE {where} ORDER BY j.id"
    )
    params: tuple[int, ...] = ()
    if limit is not None:
        sql += " LIMIT ?"
        params = (limit,)
    jobs = conn.execute(sql, params).fetchall()
    with _txn(conn):
        for job in jobs:
            stored = load_locations(conn, job["id"])
            rows, scope, warnings = derive_locations(job, stored, job["source_state"])
            conn.execute("DELETE FROM job_locations WHERE job_id = ?", (job["id"],))
            conn.executemany(
                "INSERT INTO job_locations (job_id, state, city, county, lat, lon, is_primary) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (job["id"], r.state, r.city, r.county, r.lat, r.lon, int(r.is_primary))
                    for r in rows
                ],
            )
            conn.execute(
                "UPDATE job SET location_scope = ?, parse_warnings = ? WHERE id = ?",
                (scope.value, _merge_warnings(job["parse_warnings"], warnings), job["id"]),
            )
    return len(jobs)


def jobs_in_state(
    conn: sqlite3.Connection, usps: str, *, include_remote: bool = False
) -> list[sqlite3.Row]:
    """Jobs with a location in ``usps``; optionally also remote_us/nationwide/negotiable jobs."""
    cond = "EXISTS (SELECT 1 FROM job_locations l WHERE l.job_id = j.id AND l.state = ?)"
    params: list[str] = [usps.strip().upper()]
    if include_remote:
        cond += f" OR j.location_scope IN ({','.join('?' for _ in REMOTE_SCOPES)})"
        params += [s.value for s in REMOTE_SCOPES]
    return conn.execute(
        f"SELECT j.* FROM job j WHERE {cond} ORDER BY j.posted_at DESC, j.id", params
    ).fetchall()


def _label(loc: JobLocation) -> str:
    return ", ".join(p for p in (loc.city, loc.state) if p)


def location_summary(locations: Sequence[JobLocation], scope: LocationScope | str) -> str:
    """One-line display: "Denver, CO +13 more"; a scope label when nothing is enumerated."""
    scope = LocationScope(scope)
    named = [loc for loc in locations if loc.state or loc.city]
    if named:
        ordered = sorted(named, key=lambda loc: not loc.is_primary)  # stable; primary first
        text = _label(ordered[0])
        if len(ordered) > 1:
            text += f" +{len(ordered) - 1} more"
        return text
    return {
        LocationScope.nationwide: "Nationwide",
        LocationScope.remote_us: "Remote (US)",
        LocationScope.negotiable: "Location negotiable",
        LocationScope.overseas: "Overseas",
    }.get(scope, "Unknown")
