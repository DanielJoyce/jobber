"""Measure a scorer on already-ingested jobs without storing anything (specs/016).

``run_bench`` scores N prefiltered job groups through any ``FitScorer.score_one`` and reports
speed (first request vs the rest, which shows the prompt-prefix cache), schema-valid rate and
evidence-quote verification. The connection is switched to ``query_only``, so no ``fit_score``,
``llm_spend`` or any other row can be written.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from jobhunter.scoring.profile import Profile
from jobhunter.scoring.scorers import FitScorer, _usage_int
from jobhunter.scoring.screen import (
    ScreenError,
    _posting_haystack,
    _row,
    _summary_for,
    build_packed_request,
    build_score_request,
    evidence_checks,
    pack_items,
    parse_packed,
    parse_result,
    posting_text,
)

_GROUPS = """
SELECT g.id AS group_id, j.*
FROM job_group g
JOIN job j ON j.id = g.canonical_job_id
JOIN prefilter_result p ON p.job_id = j.id AND p.passed = 1 AND p.filter_version = ?
ORDER BY j.posted_at IS NULL, j.posted_at DESC, g.id
LIMIT ?
"""


@dataclass
class BenchReport:
    scorer: str
    requested: int
    seconds: list[float] = field(default_factory=list)  # one per request, in order
    errored: int = 0
    schema_valid: int = 0
    evidence_total: int = 0
    evidence_verified: int = 0
    output_tokens: int = 0
    timed_output_seconds: float = 0.0  # seconds of requests that reported output tokens
    cost_usd: float = 0.0
    night_hours: float = 8.0
    jobs_per_request: int = 1
    jobs_sent: int = 0  # packed mode: jobs across all requests

    @property
    def packed(self) -> bool:
        return self.jobs_per_request > 1

    @property
    def attempted(self) -> int:
        return self.jobs_sent if self.packed else len(self.seconds)

    @property
    def seconds_per_job(self) -> float | None:
        return sum(self.seconds) / self.jobs_sent if self.jobs_sent else None

    @property
    def cost_per_job(self) -> float | None:
        return self.cost_usd / self.jobs_sent if self.jobs_sent else None

    @property
    def first_s(self) -> float | None:
        return self.seconds[0] if self.seconds else None

    @property
    def subsequent_s(self) -> float | None:
        rest = self.seconds[1:]
        return sum(rest) / len(rest) if rest else None

    @property
    def tokens_per_s(self) -> float | None:
        if self.output_tokens <= 0 or self.timed_output_seconds <= 0:
            return None
        return self.output_tokens / self.timed_output_seconds

    @property
    def schema_valid_rate(self) -> float | None:
        return self.schema_valid / self.attempted if self.attempted else None

    @property
    def evidence_rate(self) -> float | None:
        return self.evidence_verified / self.evidence_total if self.evidence_total else None

    @property
    def jobs_per_night(self) -> int | None:
        per_job = self.subsequent_s if self.subsequent_s is not None else self.first_s
        if self.packed and self.seconds_per_job:
            per_job = self.seconds_per_job
        if not per_job or per_job <= 0:
            return None
        return int(self.night_hours * 3600 / per_job)

    def format(self) -> str:
        def f(v: float | None, spec: str, unit: str = "") -> str:
            return "n/a" if v is None else f"{v:{spec}}{unit}"

        def pct(v: float | None) -> str:
            return "n/a" if v is None else f"{v * 100:.0f}%"

        lines = [
            f"scorer: {self.scorer}",
            f"jobs scored: {self.attempted} of {self.requested} requested "
            f"({self.errored} errored); nothing was written to the database",
            f"first job: {f(self.first_s, '.1f', ' s')} (cold prefix cache)",
            f"later jobs: {f(self.subsequent_s, '.1f', ' s')} each (prefix cache warm)",
            f"output speed: {f(self.tokens_per_s, '.1f', ' tokens/s')} "
            "(completion tokens over whole-request time, as reported by the server)",
            f"schema-valid: {self.schema_valid}/{self.attempted} ({pct(self.schema_valid_rate)})",
            f"evidence quotes verified: {self.evidence_verified}/{self.evidence_total} "
            f"({pct(self.evidence_rate)})",
        ]
        if self.packed:
            lines[1] = (
                f"jobs scored: {self.attempted} of {self.requested} requested in "
                f"{len(self.seconds)} packed requests of up to {self.jobs_per_request} "
                f"({self.errored} jobs errored); nothing was written to the database"
            )
            lines[2] = f"first request: {f(self.first_s, '.1f', ' s')} (cold prefix cache)"
            lines[3] = f"later requests: {f(self.subsequent_s, '.1f', ' s')} each"
            mean_req = sum(self.seconds) / len(self.seconds) if self.seconds else None
            lines.append(
                f"per job: {f(self.seconds_per_job, '.1f', ' s')} "
                f"(per request: {f(mean_req, '.1f', ' s')})"
            )
        if self.cost_usd:
            lines.append(f"cost: ${self.cost_usd:.4f}")
            if self.packed:
                lines.append(
                    f"cost per job: ${self.cost_per_job or 0:.4f} (per request: "
                    f"${self.cost_usd / max(len(self.seconds), 1):.4f})"
                )
        nights = self.jobs_per_night
        if nights is None:
            lines.append("projection: not enough data")
        else:
            lines.append(
                f"projection: about {nights} jobs in a {self.night_hours:g}-hour night "
                "at the steady-state speed"
            )
        return "\n".join(lines)


def run_bench(
    conn: sqlite3.Connection,
    scorer: FitScorer,
    profile: Profile,
    *,
    n: int,
    night_hours: float = 8.0,
    clock: Callable[[], float] = time.monotonic,
    jobs_per_request: int | None = None,
) -> BenchReport:
    """Score up to ``n`` prefiltered groups, one at a time, writing nothing."""
    conn.execute("PRAGMA query_only = ON")
    try:
        groups = conn.execute(_GROUPS, (profile.filter_version, n)).fetchall()
        report = BenchReport(scorer=scorer.name, requested=n, night_hours=night_hours)
        per_request = jobs_per_request or int(getattr(scorer, "jobs_per_request", 1) or 1)
        if per_request > 1:
            return _run_packed(conn, scorer, profile, groups, report, per_request, clock)
        for g in groups:
            job: dict[str, Any] = _row(g)
            request = build_score_request(
                job, _summary_for(conn, g), profile, custom_id=f"g{g['group_id']}"
            )
            start = clock()
            result = scorer.score_one(request)
            elapsed = clock() - start
            report.seconds.append(elapsed)
            if result.status != "succeeded":
                report.errored += 1
                continue
            report.cost_usd += scorer.cost(result.usage, batch=False)
            out_tokens = _usage_int(result.usage, "output_tokens")
            if out_tokens:
                report.output_tokens += out_tokens
                report.timed_output_seconds += elapsed
            try:
                screen = parse_result(result)
            except ScreenError:
                continue
            report.schema_valid += 1
            checks = evidence_checks(screen, _posting_haystack(job))
            report.evidence_total += len(checks)
            report.evidence_verified += sum(checks)
        return report
    finally:
        conn.execute("PRAGMA query_only = OFF")


def _run_packed(
    conn: sqlite3.Connection,
    scorer: FitScorer,
    profile: Profile,
    groups: list[sqlite3.Row],
    report: BenchReport,
    per_request: int,
    clock: Callable[[], float],
) -> BenchReport:
    """Packed variant: one request per pack; per-job figures are derived from the totals."""
    report.jobs_per_request = per_request
    by_id = {f"g{g['group_id']}": g for g in groups}
    texts = [(f"g{g['group_id']}", posting_text(_row(g), _summary_for(conn, g))) for g in groups]
    packs = pack_items(
        texts,
        profile,
        jobs_per_request=per_request,
        max_input_tokens=int(getattr(scorer, "max_input_tokens", 40_000)),
    )
    for pack in packs:
        ids = [cid for cid, _ in pack]
        report.jobs_sent += len(ids)
        start = clock()
        result = scorer.score_one(build_packed_request(pack, profile))
        elapsed = clock() - start
        report.seconds.append(elapsed)
        if result.status != "succeeded":
            report.errored += len(ids)
            continue
        report.cost_usd += scorer.cost(result.usage, batch=False)
        out_tokens = _usage_int(result.usage, "output_tokens")
        if out_tokens:
            report.output_tokens += out_tokens
            report.timed_output_seconds += elapsed
        parsed = parse_packed(result, ids)
        for cid, screen in parsed.screens.items():
            report.schema_valid += 1
            checks = evidence_checks(screen, _posting_haystack(_row(by_id[cid])))
            report.evidence_total += len(checks)
            report.evidence_verified += sum(checks)
    return report
