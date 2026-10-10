"""Score this group now (specs/017 "New packet from a URL or text").

The only way a pasted posting is scored, and an explicit, user-confirmed paid action: the
console shows the Stage 2 estimate first and posts back that estimate's token; the CLI
(``jobhunter score --group``) prints it and asks. Nothing here is called by a nightly run.

On confirm it records a ``prefilter_result`` with ``passed = 1`` and reason ``user-requested``
at the current ``filter_version`` (the user's choice bypasses the state and salary rules),
moves the job to ``prefiltered``, and screens this one group (``only = {group}``) against the
scoring caps (``scoring.daily_cap_usd`` / ``weekly_cap_usd``). Over the cap it is refused and
nothing is written. If the scorer writes no score, the prefilter row and stage are put back,
so the group is never left waiting where something else could pay for it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from jobhunter.config import Scoring
from jobhunter.core import db
from jobhunter.pipeline.listing import from_iso, to_iso
from jobhunter.scoring import rescore, screen
from jobhunter.scoring.profile import Profile
from jobhunter.scoring.scorers import FitScorer, scorer_from_string

logger = logging.getLogger(__name__)

USER_REQUESTED = "user-requested"
CLAIM_TTL = timedelta(minutes=15)
_BEFORE_PREFILTERED = ("listed", "resolved", "normalized", "grouped")

ScorerFactory = Callable[[str], FitScorer]
ClientFactory = Callable[[], Any]


class ScoreRefused(Exception):
    """Not run; the message says why. Nothing was written or spent."""


class EstimateChanged(ScoreRefused):
    """The estimate differs from the one the user confirmed: show the new one and ask again."""

    def __init__(self, est: Estimate) -> None:
        super().__init__(
            f"The estimate changed since you confirmed it: now ${est.estimated_usd:.4f} with "
            f"{est.scorer}. Nothing was run; check it and press Score this group now again."
        )
        self.estimate = est


@dataclass
class Estimate:
    group_id: int
    scorer: str
    cost_per_job: float
    cost_source: str
    estimated_usd: float
    remaining_usd: float
    filter_version: str
    scoring_version: str
    refusal: str | None = None

    @property
    def batch(self) -> bool:
        return rescore.is_anthropic(self.scorer)

    @property
    def token(self) -> str:
        """Identifies this exact estimate; the confirm form posts it back."""
        blob = json.dumps(
            [
                self.group_id,
                self.scorer,
                self.filter_version,
                self.scoring_version,
                f"{self.estimated_usd:.6f}",
            ]
        )
        return hashlib.sha256(blob.encode()).hexdigest()[:16]


def _canonical(conn: sqlite3.Connection, group_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT j.* FROM job_group g JOIN job j ON j.id = g.canonical_job_id WHERE g.id = ?",
        (group_id,),
    ).fetchone()


def is_scored(conn: sqlite3.Connection, group_id: int) -> bool:
    """A screen or deep score exists for the group's current description."""
    return (
        conn.execute(
            "SELECT 1 FROM fit_score f JOIN job_group g ON g.id = f.job_group_id "
            "WHERE g.id = ? AND f.input_rev = g.description_rev LIMIT 1",
            (group_id,),
        ).fetchone()
        is not None
    )


def in_open_batch(conn: sqlite3.Connection, group_id: int) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM score_batch_item i JOIN score_batch b ON b.id = i.batch_id "
            "WHERE i.job_group_id = ? AND b.collected_at IS NULL LIMIT 1",
            (group_id,),
        ).fetchone()
        is not None
    )


def estimate(
    conn: sqlite3.Connection,
    profile: Profile,
    scoring: Scoring,
    group_id: int,
    now: datetime,
    spec: str | None = None,
) -> Estimate:
    """What scoring this one group would cost, and why it would be refused. Read-only."""
    spec = spec or scoring.screen_scorer
    per_job, source = rescore.cost_per_job(conn, spec)
    est = Estimate(
        group_id=group_id,
        scorer=spec,
        cost_per_job=per_job,
        cost_source=source,
        estimated_usd=per_job,
        remaining_usd=screen.remaining_budget(conn, scoring, now),
        filter_version=profile.filter_version,
        scoring_version=profile.scoring_version,
    )
    job = _canonical(conn, group_id)
    if job is None:
        est.refusal = "no such job group"
    elif not (job["description_text"] or "").strip():
        est.refusal = "this posting has no description text yet; paste it first"
    elif is_scored(conn, group_id):
        est.refusal = "this posting is already scored"
    elif in_open_batch(conn, group_id):
        est.refusal = "this posting is in a submitted batch; it is scored when that is collected"
    elif (claim := _claim_refusal(conn, job["id"], now)) is not None:
        est.refusal = claim
    elif est.estimated_usd > est.remaining_usd:
        est.refusal = (
            f"estimated ${est.estimated_usd:.4f} is over the remaining spend cap "
            f"(${max(est.remaining_usd, 0):.2f}); raise scoring.daily_cap_usd / weekly_cap_usd "
            "in config.toml"
        )
    return est


@dataclass
class Outcome:
    status: str  # 'scored' or 'submitted'
    detail: str
    cost_usd: float = 0.0


def _claim_refusal(conn: sqlite3.Connection, job_id: int, now: datetime) -> str | None:
    """Why a new Score now must wait, or None.

    A Score now marks the job with a ``user-requested`` prefilter row and removes it again if
    it scores nothing (including on Ctrl-C). A row from the last ``CLAIM_TTL`` with no score is
    a run still going, or one whose process was killed; the message says when to retry.
    """
    row = conn.execute(
        "SELECT reasons, evaluated_at FROM prefilter_result WHERE job_id = ?", (job_id,)
    ).fetchone()
    if row is None or json.loads(row["reasons"] or "[]") != [USER_REQUESTED]:
        return None
    try:
        started = from_iso(row["evaluated_at"])
    except ValueError:
        return None
    if now - started >= CLAIM_TTL:
        return None
    retry = (started + CLAIM_TTL).astimezone()
    return (
        f"a Score now for this posting started at {started.astimezone():%H:%M} and has not "
        f"finished. If it is still running, its result will show here; if it was stopped, "
        f"you can retry after {retry:%H:%M}"
    )


def _snapshot(conn: sqlite3.Connection, job_id: int) -> tuple[sqlite3.Row | None, str]:
    pre = conn.execute("SELECT * FROM prefilter_result WHERE job_id = ?", (job_id,)).fetchone()
    stage = conn.execute("SELECT stage FROM job WHERE id = ?", (job_id,)).fetchone()[0]
    return pre, stage


def _restore(conn: sqlite3.Connection, job_id: int, snap: tuple[sqlite3.Row | None, str]) -> None:
    pre, stage = snap
    with db.transaction(conn):
        conn.execute("DELETE FROM prefilter_result WHERE job_id = ?", (job_id,))
        if pre is not None:
            conn.execute(
                "INSERT INTO prefilter_result (job_id, passed, reasons, filter_version, "
                "evaluated_at) VALUES (?, ?, ?, ?, ?)",
                (job_id, pre["passed"], pre["reasons"], pre["filter_version"], pre["evaluated_at"]),
            )
        conn.execute(
            "UPDATE job SET stage = ? WHERE id = ? AND stage = 'prefiltered'", (stage, job_id)
        )


def score_now(
    conn: sqlite3.Connection,
    profile: Profile,
    scoring: Scoring,
    group_id: int,
    *,
    token: str | None,
    now: datetime,
    scorer_factory: ScorerFactory | None = None,
    client_factory: ClientFactory | None = None,
) -> Outcome:
    """Score one group the user chose. ``token`` must match the estimate built now.

    Raises ScoreRefused (nothing written, nothing spent) or EstimateChanged. A scorer failure
    raises ScoreRefused after restoring the prefilter row and stage.
    """
    est = estimate(conn, profile, scoring, group_id, now)
    if est.refusal:
        raise ScoreRefused(est.refusal)
    if not token or token != est.token:
        raise EstimateChanged(est)
    job = _canonical(conn, group_id)
    assert job is not None
    with db.transaction(conn):
        # The claim: under the write lock, re-check and mark the group as being scored, so a
        # second confirm (another tab, the CLI) cannot pay for the same group in parallel.
        if is_scored(conn, group_id):
            raise ScoreRefused("this posting is already scored")
        if in_open_batch(conn, group_id):
            raise ScoreRefused("this posting is in a submitted batch")
        if (msg := _claim_refusal(conn, job["id"], now)) is not None:
            raise ScoreRefused(msg)
        snap = _snapshot(conn, job["id"])
        conn.execute(
            "INSERT OR REPLACE INTO prefilter_result (job_id, passed, reasons, filter_version, "
            "evaluated_at) VALUES (?, 1, ?, ?, ?)",
            (job["id"], json.dumps([USER_REQUESTED]), profile.filter_version, to_iso(now)),
        )
        marks = ",".join("?" * len(_BEFORE_PREFILTERED))
        conn.execute(
            f"UPDATE job SET stage = 'prefiltered' WHERE id = ? AND stage IN ({marks})",
            (job["id"], *_BEFORE_PREFILTERED),
        )

    def remaining() -> float:
        return screen.remaining_budget(conn, scoring, now)

    spec = est.scorer
    try:
        if est.batch:
            if client_factory is None:
                import anthropic

                client_factory = anthropic.Anthropic
            batch_id = screen.submit_batch(
                conn,
                client_factory(),
                profile,
                limit=1,
                now=now,
                scorer=spec,
                remaining_usd=remaining,
                group_ids=[group_id],
                rejection_days=scoring.employer_rejection_days,
            )
            if batch_id:
                return Outcome(
                    "submitted",
                    f"submitted batch {batch_id}; it is scored when the batch is collected "
                    "(nightly at 03:30, or `jobhunter score --collect-pending`)",
                )
            why = "nothing was submitted (spend cap, or the posting is not eligible)"
        else:
            make = scorer_factory or (lambda s: scorer_from_string(s, scoring=scoring))
            res = screen.score_sync(
                conn,
                make(spec),
                profile,
                limit=1,
                now=now,
                remaining_usd=remaining,
                group_ids=[group_id],
                rejection_days=scoring.employer_rejection_days,
            )
            if res.written or res.duplicate:
                return Outcome("scored", f"scored with {spec}", res.cost_usd)
            why = (
                "the scorer returned no usable score"
                if res.submitted
                else "nothing was sent (spend cap, or the posting is not eligible)"
            )
    except Exception as exc:  # restored below; the caller shows the message
        logger.warning("score group %s failed: %s", group_id, exc)
        why = f"scoring failed: {exc}"
    except BaseException:
        # Ctrl-C during `score --group`, or shutdown: release the claim so a retry can run.
        _restore(conn, job["id"], snap)
        raise
    _restore(conn, job["id"], snap)
    raise ScoreRefused(f"{why}; nothing is left waiting to be scored")
