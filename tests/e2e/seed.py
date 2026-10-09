"""Synthetic data for the browser tests. Built on a temp DB; never touches data/ or profile/."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from jobhunter.console import tracking
from jobhunter.core import db

PREFS = """\
hard:
  states_allowed: [CO, WA]
  remote_ok: true
soft:
  state_ranking: [CO, WA]
resume_path: resume.md
"""

QUOTE = "manage linux servers daily"
DESC = (
    "We run synthetic hosts.\n\nYou will manage Linux servers daily and lead incident "
    "response.\n\nBenefits include dental."
)


def write_profile(root: Path) -> Path:
    d = root / "profile"
    d.mkdir()
    (d / "preferences.yaml").write_text(PREFS, encoding="utf-8")
    (d / "resume.md").write_text("Synthetic Person\n- ran synthetic hosts\n", encoding="utf-8")
    return d


def dims(skills=90, raw=None, seniority=90, domain=90, stale=()):
    return {
        "skills": {"score": skills, "why": "skills reason"},
        "seniority": {"score": seniority, "why": "seniority reason"},
        "domain": {"score": domain, "why": "domain reason"},
        "raw_skills": skills if raw is None else raw,
        "recency_weighted_skills": skills,
        "stale_skills": list(stale),
        "seniority_direction": "match",
    }


A, B = dims(90), dims(70, seniority=70, domain=70)
F = dims(40, raw=85, stale=["VMware"])
G = dims(20)


def add_job(conn, n, title, d, *, states=("CO",), scope="single", salary=None, now):
    ts = now.isoformat()
    conn.execute(
        "INSERT INTO job (id, source_key, external_id, url, title, employer, description_text, "
        "salary_min, salary_max, salary_period, salary_stated, location_scope, remote, "
        "employment_type, posted_at, first_seen_at, last_seen_at) "
        "VALUES (?, 'co', ?, ?, ?, 'Acme', ?, ?, ?, 'year', ?, ?, 'onsite', 'full-time', ?, ?, ?)",
        (
            n,
            str(n),
            f"https://example.invalid/{n}",
            title,
            DESC,
            salary[0] if salary else None,
            salary[1] if salary else None,
            int(bool(salary)),
            scope,
            (now - timedelta(days=1)).isoformat(),
            ts,
            ts,
        ),
    )
    conn.execute(
        "INSERT INTO job_group (id, canonical_job_id, method, created_at) "
        "VALUES (?, ?, 'exact_hash', ?)",
        (n, n, ts),
    )
    conn.execute("UPDATE job SET job_group_id = ? WHERE id = ?", (n, n))
    for i, st in enumerate(states):
        conn.execute(
            "INSERT INTO job_locations (job_id, state, city, is_primary) VALUES (?, ?, ?, ?)",
            (n, st, "Town" if st else None, int(i == 0)),
        )
    evidence = [{"claim": "daily Linux work", "quote": QUOTE}]
    conn.execute(
        "INSERT INTO fit_score (job_group_id, tier, model, prompt_version, scoring_version, "
        "verdict, overall, dimensions, evidence, blockers, shape_flags, evidence_unverified, "
        "created_at) VALUES (?, 'screen', 'm', 'p', 's', 'strong', 0, ?, ?, '[]', '[]', 0, ?)",
        (n, json.dumps(d), json.dumps(evidence), ts),
    )


def add_link(conn, gid, status, final_url, now):
    chain = [{"url": final_url, "host": "127.0.0.1", "method": "3xx", "status": 200}]
    conn.execute(
        "INSERT INTO apply_link (job_group_id, start_url, final_url, chain, ats, employer_host, "
        "status, resolved_at, verified_at) VALUES (?, ?, ?, ?, 'generic', '127.0.0.1', ?, ?, ?)",
        (gid, final_url, final_url, json.dumps(chain), status, now.isoformat(), now.isoformat()),
    )


def seed(db_path: Path, employer_url: str) -> dict[str, int]:
    """Create and fill the DB. Returns name -> job group id (== job id)."""
    now = datetime.now(UTC)
    conn = db.connect(db_path)
    db.migrate(conn)
    conn.execute(
        "INSERT INTO source (key, class, name, family, tier, entry, policy) VALUES "
        "('co', 'A', 'Connecting Colorado', 'x', 'http', 'https://example.invalid', 'enabled')"
    )
    jobs = [
        # name, title, dims, states, scope, salary
        ("a1", "Alpha One", A, ("CO",), "single", (110_000, 130_000)),
        ("a2", "Alpha Two", A, ("CO", "WA"), "multi_state", (95_000, 115_000)),
        ("a3", "Alpha Three", A, (None,), "remote_us", (120_000, 140_000)),
        ("b1", "Bravo One", B, ("WA",), "single", (90_000, 100_000)),
        ("b2", "Bravo Two", B, ("CO",), "single", None),
        ("f1", "Foxtrot One", F, ("CO",), "single", None),
        ("f2", "Foxtrot Two", F, ("WA",), "single", None),
        ("g1", "Golf One", G, ("CO",), "single", None),
        # detail/apply targets and pipeline cards
        ("live", "Live Link", B, ("CO",), "single", None),
        ("expired", "Expired Link", B, ("CO",), "single", None),
        ("p_interested", "Pipe Interested", B, ("CO",), "single", None),
        ("p_preparing", "Pipe Preparing", B, ("CO",), "single", None),
        ("p_applied", "Pipe Applied", B, ("CO",), "single", None),
        ("p_interview", "Pipe Interview", B, ("WA",), "single", None),
        ("p_rejected", "Pipe Rejected", B, ("CO",), "single", None),
    ]
    ids: dict[str, int] = {}
    for n, (name, title, d, states, scope, sal) in enumerate(jobs, start=1):
        add_job(conn, n, title, d, states=states, scope=scope, salary=sal, now=now)
        ids[name] = n
    # Every job gets a resolved link so the detail page never resolves one on demand (that
    # would be a fetch); final URLs point at the local dummy employer page.
    for name, gid in ids.items():
        add_link(conn, gid, "expired" if name == "expired" else "live", employer_url, now)

    def application(name: str, *statuses: str) -> None:
        ts = now.isoformat()
        aid = conn.execute(
            "INSERT INTO application (job_group_id, status, created_at, updated_at) "
            "VALUES (?, 'interested', ?, ?)",
            (ids[name], ts, ts),
        ).lastrowid
        for k, st in enumerate(statuses):
            tracking.add_event(conn, aid, st, None, now - timedelta(days=10 - k))

    application("live", "interested")  # the apply click moves it to preparing
    application("p_interested", "interested")
    application("p_preparing", "interested", "preparing")
    application("p_applied", "interested", "preparing", "applied")
    application("p_interview", "interested", "preparing", "applied", "interview")
    application("p_rejected", "interested", "preparing", "applied", "rejected")
    conn.commit()
    conn.close()
    return ids


def query(db_path: Path, sql: str, params=()) -> list[sqlite3.Row]:
    conn = db.connect(db_path)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()
