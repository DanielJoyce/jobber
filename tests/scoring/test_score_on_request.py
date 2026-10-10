"""``job_group.score_on_request`` (specs/017 "Scored on request: a group flag", phase 1e).

The flag, not the canonical job's source, keeps a pasted or captured group out of every
nightly site: the prefilter, the screen's eligibility, re-score plans (built and run). A later
ingested copy joins the group (``dedupe.group_pending``) and either leaves it on request (it is
scored, being scored or applied to) or clears the flag so the nightly run scores it once.
Fake scorers only; nothing here can reach a paid API.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from test_rescore import SPEC, FakeScorer
from test_screen import DESCRIPTION, add_group, profile  # noqa: F401

from jobhunter.apply import score as group_score
from jobhunter.config import Scoring, Settings
from jobhunter.core import db
from jobhunter.pipeline import runner
from jobhunter.pipeline.dedupe import group_pending
from jobhunter.scoring import prefilter, rescore, screen

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
ISO = NOW.isoformat()
SCORING = Scoring(screen_scorer=SPEC)


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.migrate(c)
    c.execute(
        "INSERT INTO source (key, class, name, family, tier, entry, policy) VALUES "
        "('wa', 'B', 'WA', 'neogov', 'http', 'https://example.com', 'enabled'), "
        "('paste-manual', 'C', 'Pasted posting', 'manual', 'manual', 'console', 'manual')"
    )
    yield c
    c.close()


def flag(conn, gid, value=1):
    conn.execute("UPDATE job_group SET score_on_request = ? WHERE id = ?", (value, gid))


def eligible(conn, profile, **kw):  # noqa: F811
    return [
        r["group_id"] for r in screen.eligible_groups(conn, profile, scorer=SPEC, limit=50, **kw)
    ]


def captured(
    conn, *, url, text=DESCRIPTION, title="Systems Engineer 9", employer="Synthetic Agency"
):
    """A pasted / captured posting as paste.insert_pasted_posting leaves it."""
    jid = conn.execute(
        "INSERT INTO job (source_key, external_id, url, title, employer, description_text, "
        "description_completeness, location_scope, stage, first_seen_at, last_seen_at) "
        "VALUES ('paste-manual', ?, ?, ?, ?, ?, 'pasted', 'single', 'normalized', ?, ?)",
        (f"p-{url}-{title}", url, title, employer, text, ISO, ISO),
    ).lastrowid
    gid = conn.execute(
        "INSERT INTO job_group (canonical_job_id, member_count, method, created_at, "
        "score_on_request) VALUES (?, 1, 'manual', ?, 1)",
        (jid, ISO),
    ).lastrowid
    conn.execute("UPDATE job SET job_group_id = ? WHERE id = ?", (gid, jid))
    locate(conn, jid)
    return gid


def locate(conn, jid):
    conn.execute(
        "INSERT INTO job_locations (job_id, state, city, is_primary) "
        "VALUES (?, 'WA', 'Olympia', 1)",
        (jid,),
    )


def ingest(conn, *, url, text=DESCRIPTION, completeness="full", title="Systems Engineer 9",
           apply_url=None, ext="i1"):  # fmt: skip
    """An ingested job waiting for grouping (stage 'normalized', no group)."""
    jid = conn.execute(
        "INSERT INTO job (source_key, external_id, url, apply_url, title, employer, "
        "description_text, description_completeness, location_scope, salary_raw, stage, "
        "first_seen_at, last_seen_at) VALUES ('wa', ?, ?, ?, ?, 'Synthetic Agency', ?, ?, "
        "'single', '$40.00 - $50.00 hourly', 'normalized', ?, ?)",
        (ext, url, apply_url, title, text, completeness, ISO, ISO),
    ).lastrowid
    locate(conn, jid)
    return jid


def group_of(conn, jid):
    return conn.execute("SELECT job_group_id FROM job WHERE id = ?", (jid,)).fetchone()[0]


def g(conn, gid):
    return conn.execute("SELECT * FROM job_group WHERE id = ?", (gid,)).fetchone()


def score_row(conn, gid):
    conn.execute(
        "INSERT INTO fit_score (job_group_id, tier, model, prompt_version, scoring_version, "
        "verdict, overall, dimensions, evidence, created_at) "
        "VALUES (?, 'screen', 'm', 'p', 's', 'strong', 80, '{}', '[]', ?)",
        (gid, ISO),
    )


def daily(conn, profile, monkeypatch):  # noqa: F811
    """The daily run's free stages plus score, with a recording fake scorer."""
    seen: list[str] = []

    class Recording(FakeScorer):
        def submit(self, requests):
            seen.extend(r.custom_id for r in requests)
            return super().submit(requests)

    monkeypatch.setattr(
        "jobhunter.scoring.scorers.scorer_from_string", lambda spec, scoring=None: Recording()
    )
    runner.run_pipeline(
        conn,
        Settings.model_validate({"scoring": {"screen_scorer": SPEC}}),
        [],
        stages=["normalize", "dedupe", "prefilter", "score"],
        profile_loader=lambda: profile,
        now=NOW,
    )
    return seen


def spend(conn):
    return conn.execute("SELECT COUNT(*) FROM llm_spend").fetchone()[0]


# ─── every nightly site skips a flagged group whose canonical job is ingested ─


def test_flagged_group_with_an_ingested_canonical_is_skipped_everywhere(conn, profile):  # noqa: F811
    plain = add_group(conn, 1, profile)
    flagged = add_group(conn, 2, profile, filter_version="old")  # an ingested full canonical
    flag(conn, flagged)
    # prefilter: never evaluated (a user-requested pass is never overturned, either)
    counts = prefilter.run_prefilter(conn, profile, now=NOW, force=True)
    assert counts["evaluated"] == 1
    assert (
        conn.execute(
            "SELECT filter_version FROM prefilter_result WHERE job_id = "
            "(SELECT canonical_job_id FROM job_group WHERE id = ?)",
            (flagged,),
        ).fetchone()[0]
        == "old"
    )
    conn.execute(
        "UPDATE prefilter_result SET filter_version = ? WHERE job_id = "
        "(SELECT canonical_job_id FROM job_group WHERE id = ?)",
        (profile.filter_version, flagged),
    )
    # screen eligibility: only when named
    assert eligible(conn, profile) == [plain]
    assert eligible(conn, profile, new_only=True) == [plain]
    assert eligible(conn, profile, group_ids=[flagged]) == [flagged]
    # re-score plan building
    plan = rescore.make_plan(conn, profile, SCORING, "all", SPEC, NOW)
    assert plan.group_ids == [plain]
    # a plan whose stored ids include a group flagged after it was built: the run drops it
    assert rescore.without_on_request(conn, [plain, flagged]) == [plain]


def test_rescore_run_never_pays_for_a_group_flagged_after_the_plan(conn, profile):  # noqa: F811
    plain = add_group(conn, 1, profile)
    other = add_group(conn, 2, profile)
    plan = rescore.make_plan(conn, profile, SCORING, "all", SPEC, NOW)
    assert sorted(plan.group_ids) == sorted([plain, other])
    flag(conn, other)  # captured meanwhile
    rid = rescore.create_request(conn, profile, plan, NOW)
    scorer = FakeScorer()
    rescore.run_request(
        conn, rid, profile, SCORING, scorer=SPEC, now=lambda: NOW,
        scorer_factory=lambda spec: scorer,
    )  # fmt: skip
    scored = {r[0] for r in conn.execute("SELECT job_group_id FROM fit_score")}
    assert scored == {plain}


def test_migration_backfills_the_flag_for_manual_canonicals(monkeypatch):
    real = db._load_migrations
    monkeypatch.setattr(db, "_load_migrations", lambda: [m for m in real() if m[0] <= 29])
    c = db.connect(":memory:")
    db.migrate(c)
    c.execute(
        "INSERT INTO source (key, class, name, family, tier, entry, policy) VALUES "
        "('wa', 'B', 'WA', 'x', 'http', 'https://example.com', 'enabled'), "
        "('paste-manual', 'C', 'P', 'manual', 'manual', 'console', 'manual'), "
        "('email-manual', 'C', 'E', 'manual', 'manual', 'gmail', 'manual')"
    )
    for n, src in ((1, "wa"), (2, "paste-manual"), (3, "email-manual")):
        c.execute(
            "INSERT INTO job (id, source_key, external_id, url, title, first_seen_at, "
            "last_seen_at) VALUES (?, ?, ?, 'https://example.com/x', 'T', ?, ?)",
            (n, src, str(n), ISO, ISO),
        )
        c.execute(
            "INSERT INTO job_group (id, canonical_job_id, method, created_at) "
            "VALUES (?, ?, 'manual', ?)",
            (n, n, ISO),
        )
    monkeypatch.setattr(db, "_load_migrations", real)
    assert {31, 32, 33} <= set(db.migrate(c))
    got = dict(c.execute("SELECT id, score_on_request FROM job_group").fetchall())
    assert got == {1: 0, 2: 1, 3: 1}
    assert c.execute("PRAGMA foreign_key_check").fetchall() == []
    for table in ("job_board_ref", "capture_log"):
        assert c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    with pytest.raises(Exception, match="CHECK"):
        c.execute("UPDATE job_group SET score_on_request = 2 WHERE id = 1")


# ─── a later ingest of a captured posting ───────────────────────────────────


def test_full_copy_joins_an_unscored_capture_and_the_daily_run_scores_it_once(
    conn,
    profile,  # noqa: F811
    monkeypatch,
):
    gid = captured(conn, url="https://careers.example.org/jobs/9001")
    jid = ingest(conn, url="https://careers.example.org/jobs/9001?utm_source=x")
    seen = daily(conn, profile, monkeypatch)
    assert group_of(conn, jid) == gid
    assert g(conn, gid)["score_on_request"] == 0
    assert seen == [f"g{gid}"]
    # once: a second daily run sends nothing
    assert daily(conn, profile, monkeypatch) == []


def test_copy_joins_a_scored_capture_and_the_daily_run_spends_nothing(
    conn,
    profile,  # noqa: F811
    monkeypatch,
):
    gid = captured(conn, url="https://careers.example.org/jobs/9001")
    score_row(conn, gid)
    jid = ingest(conn, url="https://careers.example.org/jobs/9001")
    seen = daily(conn, profile, monkeypatch)
    assert group_of(conn, jid) == gid
    row = g(conn, gid)
    assert row["score_on_request"] == 1
    assert row["canonical_job_id"] == jid  # the fuller ingested text is canonical for display
    assert seen == [] and spend(conn) == 0


@pytest.mark.parametrize("hold", ["application", "claim", "batch"])
def test_copy_joining_a_capture_being_scored_or_applied_to_keeps_the_flag(conn, hold):
    gid = captured(conn, url="https://careers.example.org/jobs/9001")
    pasted = g(conn, gid)["canonical_job_id"]
    if hold == "application":
        conn.execute(
            "INSERT INTO application (job_group_id, status, created_at, updated_at) "
            "VALUES (?, 'preparing', ?, ?)",
            (gid, ISO, ISO),
        )
    elif hold == "claim":
        conn.execute(
            "INSERT INTO prefilter_result (job_id, passed, reasons, filter_version, "
            "evaluated_at) VALUES (?, 1, ?, 'v', ?)",
            (pasted, json.dumps(["user-requested"]), (NOW - timedelta(minutes=3)).isoformat()),
        )
    else:
        conn.execute(
            "INSERT INTO score_batch (id, tier, model, prompt_version, scoring_version, "
            "submitted_at, request_count) VALUES ('b1', 'screen', 'm', 'p', 's', ?, 1)",
            (ISO,),
        )
        conn.execute(
            "INSERT INTO score_batch_item (batch_id, custom_id, job_group_id) "
            "VALUES ('b1', 'g', ?)",
            (gid,),
        )
    jid = ingest(conn, url="https://careers.example.org/jobs/9001")
    res = group_pending(conn, now=NOW)
    assert group_of(conn, jid) == gid and res.on_request_kept == 1
    assert g(conn, gid)["score_on_request"] == 1


def test_partial_copy_joins_an_unscored_capture_and_score_it_still_works(conn, profile):  # noqa: F811
    gid = captured(conn, url="https://careers.example.org/jobs/9001")
    pasted = g(conn, gid)["canonical_job_id"]
    conn.execute(
        "INSERT INTO application (job_group_id, status, created_at, updated_at) "
        "VALUES (?, 'interested', ?, ?)",
        (gid, ISO, ISO),
    )
    jid = ingest(conn, url="https://careers.example.org/jobs/9001", completeness="partial",
                 text="A short alert snippet.")  # fmt: skip
    group_pending(conn, now=NOW)
    assert group_of(conn, jid) == gid
    assert g(conn, gid)["canonical_job_id"] == pasted  # pasted text beats a partial snippet
    est = group_score.estimate(conn, profile, SCORING, gid, NOW)
    assert est.refusal is None
    scorer = FakeScorer()
    out = group_score.score_now(
        conn, profile, SCORING, gid, token=est.token, now=NOW, scorer_factory=lambda s: scorer
    )
    assert out.status == "scored" and scorer.calls == 1


def test_board_id_joins_through_job_board_ref_to_any_group(conn, profile):  # noqa: F811
    gid = add_group(conn, 1, profile)  # an ingested group (an earlier alert, say)
    canonical = g(conn, gid)["canonical_job_id"]
    conn.execute(
        "INSERT INTO job_board_ref (board, board_id, job_id, seen_at) "
        "VALUES ('linkedin', '4012345678', ?, ?)",
        (canonical, ISO),
    )
    jid = ingest(conn, url="https://www.linkedin.com/comm/jobs/view/4012345678/?trk=eml",
                 text="Different alert text entirely.", title="Other words")  # fmt: skip
    res = group_pending(conn, now=NOW)
    assert group_of(conn, jid) == gid and res.board == 1


def test_board_id_from_a_stored_url_joins_too(conn):
    gid = captured(
        conn, url="https://www.linkedin.com/jobs/view/senior-engineer-at-acme-4012345678"
    )
    jid = ingest(conn, url="https://www.linkedin.com/jobs/search/?currentJobId=4012345678",
                 text="Unrelated.", title="Unrelated")  # fmt: skip
    group_pending(conn, now=NOW)
    assert group_of(conn, jid) == gid


def test_a_careers_home_url_shared_by_two_postings_joins_neither(conn):
    a = captured(conn, url="https://careers.example.org/", text="Posting A text.", title="A")
    b = captured(conn, url="https://careers.example.org/", text="Posting B text.", title="B")
    jid = ingest(conn, url="https://careers.example.org/", text="Posting C text.", title="C")
    group_pending(conn, now=NOW)
    assert group_of(conn, jid) not in (a, b)


def test_employer_and_title_alone_never_join(conn):
    gid = captured(conn, url="https://careers.example.org/jobs/1", text="One text here.")
    jid = ingest(conn, url="https://other.example.net/jobs/2", text="Completely other text.")
    group_pending(conn, now=NOW)
    assert group_of(conn, jid) != gid


def test_near_duplicate_joins_an_nlx_shaped_copy(conn):
    gid = captured(conn, url="https://careers.example.org/jobs/9001")
    jid = ingest(conn, url="https://usnlx.com/synthetic/abcdef0123456789", ext="nlx1")
    group_pending(conn, now=NOW)
    assert group_of(conn, jid) == gid


# ─── Link them keeps the flag the group had (review of 5aed9f6) ────────────


def test_link_keeps_an_applied_capture_on_request_after_a_copy_became_canonical(
    conn,
    profile,  # noqa: F811
    monkeypatch,
):
    from jobhunter.apply.capture import link_same_job
    from jobhunter.core import db as core_db

    gid = captured(conn, url="https://careers.example.org/jobs/9001")
    conn.execute(
        "INSERT INTO application (job_group_id, status, created_at, updated_at) "
        "VALUES (?, 'preparing', ?, ?)",
        (gid, ISO, ISO),
    )
    jid = ingest(conn, url="https://careers.example.org/jobs/9001")
    group_pending(conn, now=NOW)
    assert g(conn, gid)["score_on_request"] == 1 and g(conn, gid)["canonical_job_id"] == jid
    other = captured(conn, url="https://www.linkedin.com/jobs/view/4000000001/",
                     text="LinkedIn copy of the posting.", title="LinkedIn copy")  # fmt: skip
    with core_db.transaction(conn):
        kept = link_same_job(conn, gid, other, NOW)
    assert kept == gid and g(conn, kept)["score_on_request"] == 1
    assert daily(conn, profile, monkeypatch) == [] and spend(conn) == 0


def test_link_into_an_unscored_ingested_group_lets_the_nightly_run_score_it(
    conn,
    profile,  # noqa: F811
    monkeypatch,
):
    from jobhunter.apply.capture import link_same_job
    from jobhunter.core import db as core_db

    alert = ingest(conn, url="https://www.linkedin.com/comm/jobs/view/4000000002/",
                   text="A short alert snippet.", completeness="partial", ext="alert")  # fmt: skip
    group_pending(conn, now=NOW)
    alert_group = conn.execute("SELECT job_group_id FROM job WHERE id = ?", (alert,)).fetchone()[0]
    cap_gid = captured(conn, url="https://boards.greenhouse.io/acme/jobs/4012345")
    with core_db.transaction(conn):
        kept = link_same_job(conn, alert_group, cap_gid, NOW)
    assert kept == alert_group and g(conn, kept)["score_on_request"] == 0
    canonical = g(conn, kept)["canonical_job_id"]
    stage = conn.execute("SELECT stage, source_key FROM job WHERE id = ?", (canonical,)).fetchone()
    assert tuple(stage) == ("grouped", "paste-manual")  # pasted text beats the alert snippet
    assert daily(conn, profile, monkeypatch) == [f"g{kept}"]


def test_a_claim_on_any_member_refuses_a_second_score_now(conn, profile):  # noqa: F811
    gid = captured(conn, url="https://careers.example.org/jobs/9001")
    est = group_score.estimate(conn, profile, SCORING, gid, NOW)
    group_score.start(conn, profile, SCORING, gid, token=est.token, now=NOW)
    jid = ingest(conn, url="https://careers.example.org/jobs/9001")
    group_pending(conn, now=NOW)
    assert g(conn, gid)["canonical_job_id"] == jid  # the claim is on the other member
    again = group_score.estimate(conn, profile, SCORING, gid, NOW)
    assert again.refusal and "has not finished" in again.refusal
