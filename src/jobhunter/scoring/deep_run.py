"""A confirmed deep pass run from the console (specs/020 "Deep pass from the console", M11).

The plan lists exactly which groups would be deep-scored, what that is estimated to cost, the
worst case for one call, and the spend ceiling. The confirm form posts the plan's token back;
a run starts only when the plan built at click time is the same one, and it runs only the
confirmed group ids, each at most once, through ``deep.deep_score`` (the CLI's own path).

Admission: a call starts only when **both** the scoring cap left and what is left of the
ceiling still cover ``deep.worst_case_usd``. So no run passes the cap or its ceiling when a
report runs long.

Nothing here spends on its own: ``make_plan`` reads only; ``run_request`` is called by the
console's ``DeepManager`` after the confirm POST.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from jobhunter.config import Scoring
from jobhunter.core import rejections
from jobhunter.pipeline.listing import to_iso
from jobhunter.pipeline.locations import load_job_group_locations
from jobhunter.scoring import catalog, deep, screen
from jobhunter.scoring import rescore as rs
from jobhunter.scoring.buckets import compute_row
from jobhunter.scoring.profile import Profile
from jobhunter.scoring.scorers import MISSING, credential_for

logger = logging.getLogger(__name__)

MODES = ("next", "rerun", "group")
N_CHOICES = (5, 10, 20, 40)
DEFAULT_N = 10
LIVE = ("running",)
ClientFactory = Callable[[], Any]


class DeepRunError(Exception):
    """A deep run could not start."""


class DeepBusy(DeepRunError):
    def __init__(self, request_id: int) -> None:
        super().__init__(f"A deep pass (#{request_id}) is already running.")
        self.request_id = request_id


class DeepRefused(DeepRunError):
    """Nothing runs, for the reason given."""


class DeepPlanChanged(DeepRefused):
    """The plan built at click time differs from the one confirmed."""

    def __init__(self, plan: DeepPlan) -> None:
        super().__init__(
            "The plan changed since you looked (new scores, a profile or model change, or "
            "a deep pass already ran): nothing was run. Check the new plan and confirm again."
        )
        self.plan = plan


def model_label(scorer: str) -> str:
    """``anthropic:claude-opus-5-5`` -> ``Claude Opus 5.5``; otherwise the spec itself."""
    entry = catalog.ANTHROPIC_MODELS.get(scorer.partition(":")[2])
    return entry.label if entry else scorer


@dataclass
class Listed:
    group_id: int
    label: str


@dataclass
class DeepPlan:
    mode: str
    scorer: str
    n: int
    scoring_version: str
    group_ids: list[int] = field(default_factory=list)
    listed: list[Listed] = field(default_factory=list)
    per_job_usd: float | None = None
    per_job_source: str = ""
    estimated_usd: float = 0.0
    worst_usd: float = 0.0
    max_usd: float = 0.0
    daily_left: float = 0.0
    weekly_left: float = 0.0
    daily_cap: float = 0.0
    weekly_cap: float = 0.0
    fit_now: int = 0
    screen_per_job_usd: float | None = None
    refusal: str | None = None
    note: str | None = None

    @property
    def total(self) -> int:
        return len(self.group_ids)

    @property
    def remaining_usd(self) -> float:
        return min(self.daily_left, self.weekly_left)

    @property
    def model(self) -> str:
        return model_label(self.scorer)

    @property
    def times_screen(self) -> int | None:
        """About how many times the screen's per-job cost one deep job is."""
        if not self.per_job_usd or not self.screen_per_job_usd:
            return None
        return max(1, round(self.per_job_usd / self.screen_per_job_usd))

    @property
    def token(self) -> str:
        """Identifies this exact plan; the confirm form posts it back."""
        blob = json.dumps(
            [
                "deep",
                self.mode,
                self.scorer,
                self.scoring_version,
                self.group_ids,
                f"{self.max_usd:.2f}",
            ]
        )
        return hashlib.sha256(blob.encode()).hexdigest()[:16]


def cap_text(
    daily_cap: float, weekly_cap: float, daily_left: float, weekly_left: float, worst: float
) -> str:
    """Why no deep call can start under the caps, naming the cap that binds.

    A cap set below one call's worst case can never admit a call, so it says to raise it
    rather than to wait.
    """
    if daily_cap < worst:
        return (
            f"Your daily cap ${daily_cap:.2f} is below one call's worst case ${worst:.2f}; "
            f"raise [scoring] daily_cap_usd to at least ${worst:.2f}."
        )
    if weekly_cap < worst:
        return (
            f"Your weekly cap ${weekly_cap:.2f} is below one call's worst case ${worst:.2f}; "
            f"raise [scoring] weekly_cap_usd to at least ${worst:.2f}."
        )
    if weekly_left < daily_left:
        name, key, cap, left = "weekly", "weekly_cap_usd", weekly_cap, weekly_left
        when = "when older spend leaves the seven-day window"
    else:
        name, key, cap, left = "daily", "daily_cap_usd", daily_cap, daily_left
        when = "tomorrow"
    return (
        f"Only ${max(left, 0):.2f} of your {name} cap (${cap:.2f}) is left, below one call's "
        f"worst case ${worst:.2f}, so no deep call can start. A call starts only when the cap "
        f"still covers its worst case. Try again {when}, or raise [scoring] {key}."
    )


def cap_message(
    conn: sqlite3.Connection, scoring: Scoring, now: datetime, scorer: str | None = None
) -> str:
    """``cap_text`` for the caps as they stand now, for ``scorer`` (else the deep scorer)."""
    return cap_text(
        scoring.daily_cap_usd,
        scoring.weekly_cap_usd,
        screen.remaining_daily_budget(conn, scoring.daily_cap_usd, now),
        screen.weekly_remaining(conn, scoring.weekly_cap_usd, now),
        deep.worst_case_usd(scorer or scoring.deep_scorer),
    )


def fits(remaining: float, per_job: float, worst: float, n: int) -> int:
    """How many of ``n`` calls the cap admits now, each costing ``per_job`` and each admitted
    only while ``worst`` still fits."""
    if n <= 0 or remaining < worst:
        return 0
    if per_job <= 0:
        return n
    return min(n, int((remaining - worst) / per_job + 1e-9) + 1)


def _label(conn: sqlite3.Connection, profile: Profile, group_id: int) -> str:
    job = screen._canonical_job(conn, group_id)
    if job is None:
        return f"Group {group_id}"
    name = f"{job['employer'] or 'Unknown employer'}, {job['title'] or 'Untitled'}"
    row = deep._latest_screen(conn, group_id, profile)
    if row is None:
        return name
    try:
        locations = load_job_group_locations(conn, job["id"])
        fit = compute_row(row, job, locations, profile)
    except Exception:  # a label must never break the panel
        return name
    return f"{name} ({fit.bucket.value})"


def _deep_models(conn: sqlite3.Connection, group_id: int, profile: Profile) -> list[str]:
    return [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT model FROM fit_score WHERE job_group_id = ? AND tier = 'deep' "
            "AND prompt_version = ? AND scoring_version = ?",
            (group_id, deep.DEEP_PROMPT_VERSION, profile.scoring_version),
        )
    ]


def make_plan(
    conn: sqlite3.Connection,
    profile: Profile,
    scoring: Scoring,
    *,
    mode: str,
    n: int,
    now: datetime,
    group_id: int | None = None,
) -> DeepPlan:
    """The plan for a deep run. Reads only: no network, no write, no client."""
    scorer = scoring.deep_scorer
    mode = mode if mode in MODES else "next"
    n = 1 if mode == "group" else max(1, min(int(n), scoring.deep_shortlist))
    plan = DeepPlan(mode=mode, scorer=scorer, n=n, scoring_version=profile.scoring_version)
    plan.daily_cap, plan.weekly_cap = scoring.daily_cap_usd, scoring.weekly_cap_usd
    plan.daily_left = screen.remaining_daily_budget(conn, scoring.daily_cap_usd, now)
    plan.weekly_left = screen.weekly_remaining(conn, scoring.weekly_cap_usd, now)
    plan.worst_usd = deep.worst_case_usd(scorer)
    est = catalog.deep_estimate(conn, scorer)
    plan.per_job_usd, plan.per_job_source = est.usd, est.source
    plan.screen_per_job_usd = catalog.screen_estimate(conn, scoring.screen_scorer, scoring).usd

    if problem := scoring.scorer_problems().get("deep_scorer"):
        plan.refusal = problem
        return plan
    if credential_for(scorer, scoring).status == MISSING:
        plan.refusal = "No Anthropic API credential found: the deep pass cannot run."
        return plan
    if est.usd is None:
        plan.refusal = f"No price for {scorer}: its cost could not be capped, so it cannot run."
        return plan

    if mode == "next":
        plan.group_ids = deep.shortlist(conn, profile, n, scorer=scorer)
        if not plan.group_ids:
            plan.note = "Every screened job near the top already has a deep report."
    elif mode == "rerun":
        plan.group_ids = deep.rerun_list(conn, profile, n, scorer=scorer)
        if not plan.group_ids:
            plan.note = (
                f"None of your top groups has a report from a model other than {plan.model}."
            )
    else:
        assert group_id is not None
        if screen._canonical_job(conn, group_id) is None:
            plan.refusal = "No such job."
            return plan
        if group_id in rejections.rejected_group_ids(conn):
            plan.refusal = "The employer already rejected this posting; it is not deep-scored."
            return plan
        if deep.stored_report(conn, group_id, scorer, profile) is not None:
            plan.note = f"This job already has a deep report from {plan.model}; nothing to do."
        else:
            others = [m for m in _deep_models(conn, group_id, profile) if m != scorer]
            if others:
                plan.note = (
                    f"This job has a deep report from {model_label(others[0])}; this run "
                    f"adds one from {plan.model}. The old report is kept."
                )
            plan.group_ids = [group_id]

    plan.listed = [Listed(g, _label(conn, profile, g)) for g in plan.group_ids]
    plan.estimated_usd = round(est.usd * plan.total, 4)
    # Without the worst-case term, a ceiling built from a $0.05 estimate could never admit
    # one call that can cost $0.83, and a one-group run would never start.
    plan.max_usd = round(rs.max_usd_for(plan.estimated_usd) + plan.worst_usd, 2)
    plan.fit_now = fits(plan.remaining_usd, est.usd, plan.worst_usd, plan.total)
    if plan.total and plan.remaining_usd < plan.worst_usd:
        plan.refusal = cap_text(
            plan.daily_cap, plan.weekly_cap, plan.daily_left, plan.weekly_left, plan.worst_usd
        )
    return plan


# ─── Requests ───────────────────────────────────────────────────────────────


def create_request(conn: sqlite3.Connection, plan: DeepPlan, now: datetime) -> int:
    """A running request carrying the confirmed plan. There is no pending state."""
    at = to_iso(now)
    cur = conn.execute(
        "INSERT INTO deep_request (requested_at, mode, scorer, scoring_version, group_ids, "
        "estimated_usd, max_usd, status, total, started_at, heartbeat_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?, ?, ?)",
        (
            at,
            plan.mode,
            plan.scorer,
            plan.scoring_version,
            json.dumps(plan.group_ids),
            plan.estimated_usd,
            plan.max_usd,
            plan.total,
            at,
            at,
        ),
    )
    return int(cur.lastrowid or 0)


def get_request(conn: sqlite3.Connection, request_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM deep_request WHERE id = ?", (request_id,)).fetchone()


def running_request(conn: sqlite3.Connection, now: datetime) -> sqlite3.Row | None:
    """The live run. A row with a stale heartbeat is marked failed first (writes only then)."""
    stale = "WHERE status = 'running' AND coalesce(heartbeat_at, started_at, '') < ?"
    cutoff = to_iso(now - rs.STALE_AFTER)
    if conn.execute(f"SELECT 1 FROM deep_request {stale} LIMIT 1", (cutoff,)).fetchone():
        conn.execute(
            "UPDATE deep_request SET status = 'failed', done_at = ?, "
            f"error = 'interrupted: no progress for 15 minutes' {stale}",
            (to_iso(now), cutoff),
        )
    return conn.execute(
        "SELECT * FROM deep_request WHERE status = 'running' ORDER BY id LIMIT 1"
    ).fetchone()


def request_cancel(conn: sqlite3.Connection, request_id: int, now: datetime) -> bool:
    """Ask a running deep pass to stop after the call in flight."""
    cur = conn.execute(
        "UPDATE deep_request SET cancel_requested_at = ? "
        "WHERE id = ? AND status = 'running' AND cancel_requested_at IS NULL",
        (to_iso(now), request_id),
    )
    return cur.rowcount > 0


def _cancel_asked(conn: sqlite3.Connection, request_id: int) -> bool:
    row = conn.execute(
        "SELECT cancel_requested_at FROM deep_request WHERE id = ?", (request_id,)
    ).fetchone()
    return row is not None and row[0] is not None


@dataclass
class _Progress:
    request_id: int
    done: int = 0
    scored: int = 0
    spent: float = 0.0
    notes: list[str] = field(default_factory=list)

    def save(self, conn: sqlite3.Connection, now: datetime, **extra: Any) -> bool:
        """Write progress while this run still holds the row; False once it does not."""
        sets = {
            "done": self.done,
            "scored": self.scored,
            "spent_usd": round(self.spent, 6),
            "note": "; ".join(self.notes) or None,
            "heartbeat_at": to_iso(now),
            **extra,
        }
        cur = conn.execute(
            f"UPDATE deep_request SET {', '.join(f'{k} = ?' for k in sets)} "
            "WHERE id = ? AND status = 'running'",
            (*sets.values(), self.request_id),
        )
        return cur.rowcount > 0


def run_request(
    conn: sqlite3.Connection,
    request_id: int,
    profile: Profile,
    scoring: Scoring,
    *,
    now: Callable[[], datetime],
    client_factory: ClientFactory | None = None,
) -> None:
    """Run a confirmed deep request: its own group ids, in order, each at most once.

    Stops before a call whose worst case would not fit under the cap or the ceiling, on
    Cancel (after the call in flight), and on any API error (an expired key, no credit, a
    rate limit after the SDK's retries: none of these is about one group). A refusal, a
    cut-off report or bad output for one group is recorded by ``deep_score`` and the run
    moves on. Errors are stored as fixed text or the exception type, never the message.
    """
    import anthropic

    row = get_request(conn, request_id)
    if row is None or row["status"] != "running":
        return
    group_ids = [int(g) for g in json.loads(row["group_ids"])]
    scorer, max_usd = row["scorer"], float(row["max_usd"])
    worst = deep.worst_case_usd(scorer)
    pr = _Progress(request_id)
    status, error = "done", None
    try:
        client = (client_factory or anthropic.Anthropic)()
    except Exception as exc:
        logger.warning("deep pass #%s: no client: %s", request_id, type(exc).__name__)
        pr.save(conn, now(), status="failed", error=type(exc).__name__, done_at=to_iso(now()))
        return
    with rs.Heartbeat(conn, request_id, now, table="deep_request"):
        for gid in group_ids:
            if _cancel_asked(conn, request_id):
                status = "canceled"
                break
            if max_usd - pr.spent < worst:
                pr.notes.append(
                    f"stopped before the ceiling: the next call could cost up to ${worst:.2f}, "
                    f"and ${max(max_usd - pr.spent, 0):.2f} of the ${max_usd:.2f} ceiling is left"
                )
                break

            def left(spent: float = pr.spent) -> float:
                # The tighter of the scoring cap and what is left of the confirmed ceiling.
                return min(screen.remaining_budget(conn, scoring, now()), max_usd - spent)

            try:
                res = deep.deep_score(
                    conn,
                    client,
                    profile,
                    gid,
                    now=now(),
                    scorer=scorer,
                    remaining_usd=left,
                    rejection_days=scoring.employer_rejection_days,
                )
            except anthropic.APIError as exc:
                status, error = "failed", f"API error: {type(exc).__name__}"
                logger.warning("deep pass #%s group %s: %s", request_id, gid, error)
                break
            except Exception as exc:
                status, error = "failed", type(exc).__name__
                logger.exception("deep pass #%s group %s failed", request_id, gid)
                break
            if res.status == "capped":
                pr.notes.append(f"spend cap reached: {cap_message(conn, scoring, now(), scorer)}")
                break
            pr.done += 1
            pr.spent += res.cost_usd
            if res.status == "scored":
                pr.scored += 1
            if res.cost_usd > worst:
                pr.notes.append(
                    f"group {gid} cost ${res.cost_usd:.2f}, above the ${worst:.2f} worst case "
                    "(a refusal answered by a fallback bills both attempts)"
                )
            if not pr.save(conn, now()):
                return  # marked interrupted elsewhere; never turn that back into done
    pr.save(conn, now(), status=status, error=error, done_at=to_iso(now()))
