"""``jobhunter sources verify``: re-probe each registry entry and diff against what we recorded.

Specs: 003#breakage-detection (family-signal drift) and 010 (evidence behind the family calls).
Meant to run weekly; catches a state migrating off a platform before an adapter silently
returns zero jobs.

Every request goes through FetchContext (robots, rate limits, retries). registry.yaml is never
written: drift is reported for a human to update. Only the ``source`` table's robots/verified
columns and (for drift or breakage) ``status``/``status_note`` are written.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Callable, Iterable
from contextlib import AbstractContextManager
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Literal
from urllib.parse import unquote, urlsplit

from jobhunter.config import Settings
from jobhunter.core.db import transaction
from jobhunter.core.fetch import FetchContext, FetchError, RobotsDisallowed
from jobhunter.core.fetch.robots import RobotsRules, origin_of, rules_from_response
from jobhunter.core.models import Policy, SourceRow, Tier

Classification = Literal["ok", "drift", "broken", "blocked"]

# The JobLink family ships a byte-identical bundle (specs/003, 010). A different hash on one
# host while the rest still match means that state was upgraded early.
JOBLINK_BUNDLE_PATH = "/packs/js/4743-860615d0fe49e3040bc6.js"
JOBLINK_BUNDLE_SHA_PREFIX = "2c0d5d29e49fd986"

# name -> (regex over lowercased body + final URL, family it indicates or None)
SIGNALS: dict[str, tuple[re.Pattern[str], str | None]] = {
    "vosnet": (re.compile(r"/vosnet/"), "vos"),
    "geographic_solutions": (re.compile(r"geographic solutions"), "vos"),
    "viewstate": (re.compile(r"__viewstate"), "vos"),
    "virtual_onestop": (re.compile(r"virtual one-?stop"), "vos"),
    "usnlx": (re.compile(r"usnlx"), "nlx"),
    "packs_js": (re.compile(r"/packs/js/"), None),
    "next_data": (re.compile(r"__next_data__"), None),
    "jobs2web": (re.compile(r"jobs2web"), "jobs2web"),
    "workday": (re.compile(r"myworkdayjobs|workday"), "workday"),
    "taleo": (re.compile(r"taleo"), "taleo"),
    "neogov": (re.compile(r"neogov"), "neogov"),
    "salesforce": (re.compile(r"sfdc|salesforce|my\.site\.com|force\.com"), "sfdc"),
    "wordpress": (re.compile(r"wp-content|wordpress"), "wordpress"),
}

# Paths whose robots allowance matters per family, besides the entry URL.
FAMILY_PROBE_PATHS: dict[str, tuple[str, ...]] = {
    "joblink": ("/search/jobs",),
    "vos": ("/vosnet/",),
    "nlx": ("/jobs/",),
}

MAX_PATHS_IN_SUMMARY = 6


@dataclass
class RobotsSummary:
    status: str  # open | partial | disallow_all | absent | unknown
    sha256: str | None
    disallowed: list[str] = field(default_factory=list)
    detail: str = ""

    @property
    def text(self) -> str:
        if self.status == "partial":
            shown = ", ".join(self.disallowed[:MAX_PATHS_IN_SUMMARY])
            more = len(self.disallowed) - MAX_PATHS_IN_SUMMARY
            return f"partial: {shown}" + (f" (+{more} more)" if more > 0 else "")
        return self.status


@dataclass
class VerifyResult:
    key: str
    state: str | None
    entry: str
    classification: Classification = "ok"
    http_status: int | None = None
    final_url: str | None = None
    bytes: int | None = None
    signals: list[str] = field(default_factory=list)
    robots: RobotsSummary | None = None
    entry_fetched: bool = False
    bundle_sha: str | None = None
    bundle_checked: bool = False
    problems: list[str] = field(default_factory=list)  # reasons for the classification
    notes: list[str] = field(default_factory=list)  # informational, never change it

    @property
    def note(self) -> str:
        return "; ".join(self.problems or self.notes)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["robots"] = None if self.robots is None else {**d["robots"], "text": self.robots.text}
        return d


# ─── detection ───────────────────────────────────────────────────────────


def detect_signals(text: str, *extra: str) -> list[str]:
    hay = "\n".join([text.lower(), *(e.lower() for e in extra)])
    return [name for name, (pat, _) in SIGNALS.items() if pat.search(hay)]


def recorded_signals(row: SourceRow) -> set[str]:
    """Canonical names for the free-text ``family_signals`` recorded in registry.yaml."""
    found: set[str] = set()
    for text in row.family_signals:
        found.update(detect_signals(text))
        if "bundle sha" in text.lower():
            found.add("bundle")
    return found


def summarize_robots(origin: str, status: int, body: bytes) -> tuple[RobotsSummary, RobotsRules]:
    text = body.decode("utf-8", errors="replace")
    rules = rules_from_response(origin, status, text)
    digest = hashlib.sha256(body).hexdigest() if status == 200 else None
    if rules.verdict == "allow_all":
        name = "absent" if status != 200 else "open"
        return RobotsSummary(name, digest, [], rules.reason), rules
    if rules.verdict == "deny_all":
        name = "disallow_all" if status in (401, 403) else "unknown"
        return RobotsSummary(name, digest, [], rules.reason), rules
    disallowed = [unquote(p) for p in rules.disallowed_paths_for_us()]
    if not rules.allows(origin + "/") and not rules.allows(origin + "/zz-probe-path"):
        return RobotsSummary("disallow_all", digest, disallowed, "robots.txt"), rules
    return RobotsSummary("partial" if disallowed else "open", digest, disallowed, ""), rules


def _probe_paths(row: SourceRow) -> list[str]:
    paths = [urlsplit(row.entry).path or "/"]
    paths += [p for p in FAMILY_PROBE_PATHS.get(row.family, ()) if p not in paths]
    return paths


# ─── probing ─────────────────────────────────────────────────────────────


def _fetch_robots_txt(ctx: FetchContext, origin: str) -> tuple[int, bytes]:
    """robots.txt itself is exempt from the robots check (it is how rules are learned)."""
    resp = ctx._request("GET", f"{origin}/robots.txt", check_robots=False, raise_on_denied=False)
    return resp.status, resp.content


def verify_source(
    row: SourceRow, ctx: FetchContext, *, prior_robots_hash: str | None = None
) -> VerifyResult:
    """Probe one source and diff the observations against the registry. No DB access.

    ``prior_robots_hash`` is the robots.txt sha256 recorded by the previous verify, if any.
    """
    res = VerifyResult(key=row.key, state=row.state, entry=row.entry)
    origin = origin_of(row.entry)
    expected_closed = row.policy is not Policy.enabled or row.robots.status == "disallow_all"

    # robots.txt first: it decides whether the entry may be fetched at all.
    rules: RobotsRules | None = None
    try:
        rstatus, rbody = _fetch_robots_txt(ctx, origin)
        if 300 <= rstatus < 400:
            res.robots = RobotsSummary("unknown", None, [], f"redirect {rstatus} not followed")
            res.notes.append(f"robots.txt redirects ({rstatus}); not followed")
        else:
            res.robots, rules = summarize_robots(origin, rstatus, rbody)
    except FetchError as exc:
        res.robots = RobotsSummary("unknown", None, [], str(exc))
        res.notes.append(f"robots.txt unreachable: {exc}")

    if row.tier is Tier.api:
        res.notes.append("api tier: entry not probed")
    else:
        try:
            resp = ctx.get(row.entry)
            res.entry_fetched = True
            res.http_status = resp.status
            res.final_url = resp.final_url
            res.bytes = len(resp.content)
            res.signals = detect_signals(resp.text, resp.final_url)
            if not 200 <= resp.status < 300:
                _escalate(res, "broken", f"entry returned HTTP {resp.status}")
        except RobotsDisallowed:
            if expected_closed:
                res.notes.append("entry disallowed by robots (expected); not fetched")
            else:
                _escalate(res, "blocked", "robots.txt now disallows the entry URL; not fetched")
        except FetchError as exc:
            res.http_status = getattr(exc, "status", None)
            _escalate(res, "broken", f"entry fetch failed: {exc}")

    _diff_signals(row, res)
    _diff_robots(row, res, rules, prior_robots_hash)
    if row.family == "joblink":
        _check_bundle(ctx, res, origin)
    return res


_ORDER = {"ok": 0, "drift": 1, "broken": 2, "blocked": 3}


def _escalate(res: VerifyResult, level: Classification, why: str) -> None:
    res.problems.append(why)
    if _ORDER[level] > _ORDER[res.classification]:
        res.classification = level


def _diff_signals(row: SourceRow, res: VerifyResult) -> None:
    if not res.entry_fetched or res.http_status is None or not 200 <= res.http_status < 300:
        return
    recorded = recorded_signals(row) - {"bundle"}
    seen = set(res.signals)
    missing = sorted(recorded - seen)
    if missing:
        _escalate(res, "drift", f"family signal missing: {', '.join(missing)}")
    if recorded:
        # A signal that points at a different platform than the row's family is a migration.
        foreign = sorted(n for n in seen - recorded if SIGNALS[n][1] not in (None, row.family))
        if foreign:
            _escalate(res, "drift", f"new signal(s) for another platform: {', '.join(foreign)}")
        harmless = sorted(seen - recorded - set(foreign))
        if harmless:
            res.notes.append(f"new signal(s): {', '.join(harmless)}")


def _diff_robots(
    row: SourceRow, res: VerifyResult, rules: RobotsRules | None, prior_hash: str | None
) -> None:
    summary = res.robots
    if summary is None or summary.status == "unknown":
        return
    recorded_status = row.robots.status
    if recorded_status not in ("unknown", "n/a") and summary.status != recorded_status:
        res.notes.append(f"robots status {recorded_status} -> {summary.status}")
    hash_changed = bool(prior_hash and summary.sha256 and prior_hash != summary.sha256)
    if rules is None:
        return
    origin = origin_of(row.entry)
    closed_now = [p for p in _probe_paths(row) if not rules.allows(origin + p)]
    # Allowance before is inferred from policy: enabled means search and detail were allowed,
    # blocked means they were not (the old robots.txt text is not kept, only its hash).
    if row.policy is Policy.enabled and closed_now:
        _escalate(res, "blocked", f"robots.txt now disallows: {', '.join(closed_now)}")
    elif row.policy is Policy.blocked and summary.status in ("open", "absent"):
        _escalate(res, "drift", f"robots.txt is now {summary.status}; policy is blocked")
    elif (
        row.policy is Policy.blocked
        and recorded_status != "disallow_all"
        and summary.status != "disallow_all"
        and not closed_now
    ):
        _escalate(res, "drift", "robots.txt now allows the probed paths; policy is blocked")
    if hash_changed:
        res.notes.append(
            "robots.txt hash changed; allowance of "
            + ", ".join(_probe_paths(row))
            + (" changed" if closed_now and row.policy is Policy.enabled else " unchanged")
        )


def _check_bundle(ctx: FetchContext, res: VerifyResult, origin: str) -> None:
    try:
        resp = ctx.get(origin + JOBLINK_BUNDLE_PATH)
    except RobotsDisallowed:
        res.notes.append("joblink bundle disallowed by robots; not fetched")
        return
    except FetchError as exc:
        _escalate(res, "drift", f"joblink bundle fetch failed: {exc}")
        return
    res.bundle_checked = True
    if not 200 <= resp.status < 300:
        _escalate(res, "drift", f"joblink bundle returned HTTP {resp.status} (upgraded?)")
        return
    res.bundle_sha = hashlib.sha256(resp.content).hexdigest()
    if not res.bundle_sha.startswith(JOBLINK_BUNDLE_SHA_PREFIX):
        _escalate(
            res,
            "drift",
            f"joblink bundle sha {res.bundle_sha[:16]} != {JOBLINK_BUNDLE_SHA_PREFIX} "
            "(state upgraded?)",
        )


# ─── whole-registry run ──────────────────────────────────────────────────

_SEVERITY = {"ok": 0, "suspect": 1, "blocked": 2, "broken": 3}
_STATUS_FOR: dict[str, str] = {"drift": "suspect", "broken": "broken", "blocked": "blocked"}
_POLICY_OWNED = ("manual", "disabled", "blocked")
NOTE_PREFIX = "verify: "


@dataclass
class VerifyReport:
    results: list[VerifyResult] = field(default_factory=list)

    def by_class(self, *classes: str) -> list[VerifyResult]:
        return [r for r in self.results if r.classification in classes]

    @property
    def problems(self) -> list[VerifyResult]:
        return self.by_class("drift", "broken", "blocked")

    @property
    def clean(self) -> bool:
        return not self.problems

    def to_json(self) -> str:
        return json.dumps(
            {
                "clean": self.clean,
                "counts": {c: len(self.by_class(c)) for c in ("ok", "drift", "broken", "blocked")},
                "results": [r.to_dict() for r in self.results],
            },
            indent=2,
        )


def filter_rows(rows: Iterable[SourceRow], states: set[str] | None) -> list[SourceRow]:
    """All policies; national/federal rows (state None) pass with no filter or with US."""
    return [r for r in rows if states is None or (r.state or "US").upper() in states]


def _probe_row(row: SourceRow) -> SourceRow:
    """FetchContext refuses blocked/disabled policies. Verify still reads robots.txt and any
    entry robots allows, so it probes through an enabled-policy copy (robots still apply)."""
    return row.model_copy(update={"policy": Policy.enabled})


def verify_all(
    conn: sqlite3.Connection,
    rows: Iterable[SourceRow],
    *,
    now: datetime,
    states: set[str] | None = None,
    settings: Settings | None = None,
    ctx_factory: Callable[[SourceRow], AbstractContextManager[FetchContext]] | None = None,
    on_result: Callable[[VerifyResult], None] | None = None,
) -> VerifyReport:
    """Verify rows in turn, write the source columns, return the report."""
    if ctx_factory is None:
        cfg = settings or Settings()

        def ctx_factory(src: SourceRow) -> AbstractContextManager[FetchContext]:
            return FetchContext(src, cfg, conn)

    report = VerifyReport()
    for row in filter_rows(rows, states):
        found = conn.execute("SELECT robots_hash FROM source WHERE key = ?", (row.key,)).fetchone()
        prior = found["robots_hash"] if found else None
        try:
            with ctx_factory(_probe_row(row)) as ctx:
                res = verify_source(row, ctx, prior_robots_hash=prior)
        except FetchError as exc:  # never let one source abort the sweep
            res = VerifyResult(key=row.key, state=row.state, entry=row.entry)
            _escalate(res, "broken", f"verify failed: {exc}")
        _write(conn, row, res, now)
        report.results.append(res)
        if on_result is not None:
            on_result(res)
    return report


def _write(conn: sqlite3.Connection, row: SourceRow, res: VerifyResult, now: datetime) -> None:
    cur = conn.execute(
        "SELECT status, status_note FROM source WHERE key = ?", (row.key,)
    ).fetchone()
    if cur is None:
        return  # not synced yet; the runner's sync_sources_table creates rows
    sets = ["verified_at = :verified"]
    params: dict[str, Any] = {"key": row.key, "verified": now.date().isoformat()}
    robots = res.robots
    if robots is not None and robots.status != "unknown":
        sets += ["robots_status = :rs", "robots_checked = :rc", "robots_hash = :rh"]
        params |= {"rs": robots.status, "rc": now.isoformat(), "rh": robots.sha256}
    status, note = cur["status"], cur["status_note"]
    ours = bool(note) and note.startswith(NOTE_PREFIX)
    wanted = _STATUS_FOR.get(res.classification)
    if wanted is not None:
        if status in _POLICY_OWNED:
            sets.append("status_note = :note")
            params["note"] = NOTE_PREFIX + res.note
        elif ours or _SEVERITY.get(status, 0) <= _SEVERITY[wanted]:
            # Never replace a stronger status (e.g. the runner's broken) with a weaker one.
            sets += ["status = :st", "status_note = :note"]
            params |= {"st": wanted, "note": NOTE_PREFIX + res.note}
    elif ours:
        sets.append("status_note = NULL")
        if status not in _POLICY_OWNED:
            sets.append("status = 'ok'")
    with transaction(conn):
        conn.execute(f"UPDATE source SET {', '.join(sets)} WHERE key = :key", params)


# ─── text output ─────────────────────────────────────────────────────────


def format_row(r: VerifyResult) -> str:
    http = str(r.http_status) if r.http_status is not None else "-"
    size = str(r.bytes) if r.bytes is not None else "-"
    robots = r.robots.status if r.robots else "-"
    return f"{r.key:<24}{r.classification:<9}{http:<6}{size:>8}  {robots:<14}{r.note}"


def format_summary(report: VerifyReport) -> str:
    classes = ("ok", "drift", "broken", "blocked")
    counts = ", ".join(f"{len(report.by_class(c))} {c}" for c in classes)
    lines = ["", f"{len(report.results)} sources verified: {counts}"]
    if report.problems:
        lines.append("needs a human look (registry.yaml is not edited automatically):")
        for r in report.problems:
            for why in r.problems:
                lines.append(f"  {r.key}: [{r.classification}] {why}")
    return "\n".join(lines)
