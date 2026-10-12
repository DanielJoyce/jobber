"""Stored drafting runs, ``draft_run`` (specs/022 "Drafting in the background").

``generator.start`` writes a row once it holds the document's ``inflight`` claim, and
``generator.run_claimed`` sets the outcome. The state of the latest run of a document is:

- ``drafting``: no outcome, and the claim is still held (by a console thread, or by
  ``jobhunter apply draft`` in another process);
- ``stopped``: no outcome and the claim is free. The process that ran it ended mid-run (a
  console restart), so it never will have one;
- ``done`` or ``failed``: the outcome.

Nothing here runs a model or reads the network.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from jobhunter.apply import inflight
from jobhunter.core import db

DRAFTING = "drafting"
STOPPED = "stopped"
DONE = "done"
FAILED = "failed"
NONE = "none"

MAX_ERROR = 500

_STOPPED = (
    "The draft did not finish: the process running it stopped (a console restart, or an "
    "interrupted jobhunter apply draft). Nothing was saved."
)


def stopped_message(runner: str) -> str:
    """What a stopped run says. A subscription run can have been charged if the plan was on
    paid extra usage, so no run is called free; logged usage is on the Costs page."""
    if runner == "cli":
        return (
            f"{_STOPPED} On your subscription it used no API credit unless your plan was on "
            "paid extra usage; any charge already logged shows on the Costs page."
        )
    return f"{_STOPPED} Any API charge already logged shows on the Costs page."


def begin(
    conn: sqlite3.Connection, packet_id: int, kind: str, qkey: str, runner: str, now: datetime
) -> int:
    with db.transaction(conn):
        cur = conn.execute(
            "INSERT INTO draft_run (packet_id, kind, question_key, runner, started_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (packet_id, kind, qkey, runner, now.isoformat()),
        )
    return int(cur.lastrowid or 0)


def finish(
    conn: sqlite3.Connection,
    run_id: int,
    now: datetime,
    *,
    doc_id: int | None = None,
    error: str | None = None,
) -> None:
    outcome = FAILED if error is not None else DONE
    with db.transaction(conn):
        conn.execute(
            "UPDATE draft_run SET outcome = ?, finished_at = ?, doc_id = ?, error = ? "
            "WHERE id = ? AND outcome IS NULL",
            (outcome, now.isoformat(), doc_id, (error or "")[:MAX_ERROR] or None, run_id),
        )


@dataclass
class Run:
    id: int
    packet_id: int
    kind: str
    question_key: str
    runner: str
    started_at: str
    finished_at: str | None
    outcome: str | None
    doc_id: int | None
    error: str | None
    state: str

    @property
    def stopped_message(self) -> str:
        return stopped_message(self.runner)


def _row_run(row: sqlite3.Row, state: str) -> Run:
    return Run(
        id=row["id"],
        packet_id=row["packet_id"],
        kind=row["kind"],
        question_key=row["question_key"],
        runner=row["runner"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        outcome=row["outcome"],
        doc_id=row["doc_id"],
        error=row["error"],
        state=state,
    )


def _state(row: sqlite3.Row, data_dir: Path, live: Callable[[int], bool] | None) -> str:
    if row["outcome"] is not None:
        return row["outcome"]
    if live is not None and live(row["id"]):
        return DRAFTING
    key = f"{row['packet_id']}-{row['kind']}-{row['question_key']}"
    return DRAFTING if inflight.held(data_dir, key) else STOPPED


def latest(
    conn: sqlite3.Connection,
    data_dir: Path,
    packet_id: int,
    kind: str,
    qkey: str = "",
    *,
    live: Callable[[int], bool] | None = None,
) -> Run | None:
    """The latest run of one document, with its state. ``live(run_id)`` answers for runs this
    process is drafting, so their claim is not probed."""
    row = conn.execute(
        "SELECT * FROM draft_run WHERE packet_id = ? AND kind = ? AND question_key = ? "
        "ORDER BY id DESC LIMIT 1",
        (packet_id, kind, qkey),
    ).fetchone()
    if row is None:
        return None
    return _row_run(row, _state(row, data_dir, live))


def unfinished(
    conn: sqlite3.Connection,
    data_dir: Path,
    packet_id: int,
    *,
    live: Callable[[int], bool] | None = None,
) -> list[Run]:
    """The packet's documents whose latest run has no outcome: drafting, or stopped with no
    newer version of that document since (Generate again, an edit or the base resume clears
    a stopped run)."""
    rows = conn.execute(
        "SELECT r.* FROM draft_run r WHERE r.packet_id = ? AND r.outcome IS NULL AND r.id = "
        "(SELECT max(id) FROM draft_run x WHERE x.packet_id = r.packet_id AND x.kind = r.kind "
        "AND x.question_key = r.question_key) ORDER BY r.id",
        (packet_id,),
    ).fetchall()
    out = []
    for r in rows:
        run = _row_run(r, _state(r, data_dir, live))
        if run.state == STOPPED and _newer_version(conn, run):
            continue
        out.append(run)
    return out


def _newer_version(conn: sqlite3.Connection, run: Run) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM packet_document WHERE packet_id = ? AND kind = ? AND question_key = ? "
            "AND created_at > ? LIMIT 1",
            (run.packet_id, run.kind, run.question_key, run.started_at),
        ).fetchone()
        is not None
    )
