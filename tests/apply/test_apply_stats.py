"""``jobhunter apply stats`` (specs/017 "Phase 2 gate"): counts packets ready on the four public
ATSs, Workable included, and capture_log once phase 1e's table exists. Read only; synthetic."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from typer.testing import CliRunner

from jobhunter.apply import answers, paste, stats
from jobhunter.cli import app
from jobhunter.core import db

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
URLS = {
    "greenhouse": "https://boards.greenhouse.io/acme/jobs/4012345",
    "lever": "https://jobs.lever.co/acme/0f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b",
    "workable": "https://apply.workable.com/acme/j/1A2B3C4D5E/",
    "workday": "https://acme.wd5.myworkdayjobs.com/en-US/Acme/job/Denver-CO/Analyst_R-1",
}


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "jh.db")
    db.migrate(c)
    yield c
    c.close()


def packet(conn, n, url, *, status="ready", app_status=None):
    _g, pid = paste.create_pasted_packet(
        conn, url=url, text="Synthetic posting.", employer=f"Synthetic {n}", title="Eng", now=NOW
    )
    conn.execute("UPDATE application_packet SET status = ? WHERE id = ?", (status, pid))
    if app_status:
        conn.execute(
            "UPDATE application SET status = ?, applied_at = ? WHERE id = "
            "(SELECT application_id FROM application_packet WHERE id = ?)",
            (app_status, NOW.isoformat(), pid),
        )
    return pid


def test_counts_ready_packets_by_ats_and_the_gate(conn):
    packet(conn, 1, URLS["greenhouse"])
    packet(conn, 2, URLS["greenhouse"], app_status="applied")
    packet(conn, 3, URLS["lever"])
    packet(conn, 4, URLS["workable"])
    packet(conn, 5, URLS["workday"])
    packet(conn, 6, None)
    packet(conn, 7, URLS["lever"], status="draft")
    s = stats.compute(conn, NOW)
    assert s.packets == {"ready": 6, "draft": 1}
    assert s.ready_by_ats == {"greenhouse": 2, "lever": 1, "workable": 1, "workday": 1, "other": 1}
    assert s.ready_on_public_ats == 4
    assert s.sent_with_packet == 1
    text = s.format()
    assert "Ready on the four public ATSs: 4 (phase 2 gate: 8)" in text
    assert "  workable: 1" in text
    assert "Captures: 0" in text  # phase 1e's table, empty


def test_saved_answers_are_counted_never_shown(conn):
    pid = packet(conn, 1, URLS["lever"])
    answers.save_question_answer(conn, pid, "Why us?", "SECRET-ANSWER-TEXT")
    s = stats.compute(conn, NOW)
    assert s.saved_answers == {"user": 1}
    assert "SECRET-ANSWER-TEXT" not in s.format()


def test_captures_are_counted_from_capture_log(conn):
    rows = [
        ("a1", "capture", "jobs.example.com", "jsonld", "added"),
        ("a2", "capture", "jobs.example.com", "page", "previewed"),
        ("a3", "capture", "careers.example.org", "page", "added"),
        ("a4", "link", "www.example.net", None, "linked"),
    ]
    for action, route, host, method, outcome in rows:
        conn.execute(
            "INSERT INTO capture_log (action_id, route, captured_at, host, method, outcome) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (action, route, NOW.isoformat(), host, method, outcome),
        )
    s = stats.compute(conn, NOW)
    assert s.captures == {"added": 2, "previewed": 1, "linked": 1}
    assert s.capture_methods == {"jsonld": 1, "page": 2}
    assert s.capture_hosts == {"jobs.example.com": 1, "careers.example.org": 1}
    assert "Captures: 4 (added 2, linked 1, previewed 1)" in s.format()


def test_days_window(conn):
    old = packet(conn, 1, URLS["lever"])
    conn.execute(
        "UPDATE application_packet SET created_at = ? WHERE id = ?",
        ((NOW - timedelta(days=40)).isoformat(), old),
    )
    packet(conn, 2, URLS["lever"])
    assert sum(stats.compute(conn, NOW, days=28).packets.values()) == 1
    assert sum(stats.compute(conn, NOW).packets.values()) == 2


def test_cli(tmp_path, monkeypatch):
    db_path = tmp_path / "jh.db"
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'[paths]\ndb_path = "{db_path}"\nprofile_dir = "{tmp_path / "p"}"\n')
    monkeypatch.setenv("JOBHUNTER_CONFIG", str(cfg))
    c = db.connect(db_path)
    db.migrate(c)
    packet(c, 1, URLS["workable"])
    c.close()
    r = CliRunner().invoke(app, ["apply", "stats"])
    assert r.exit_code == 0, r.output
    assert "Packets: 1 (ready 1)" in r.output and "workable: 1" in r.output


def test_sent_with_a_packet_counts_rejected_ones_and_never_drafts_or_abandoned(conn):
    packet(conn, 1, URLS["lever"], app_status="rejected")  # sent, later rejected
    packet(conn, 2, URLS["lever"], app_status="no_response")
    packet(conn, 3, URLS["lever"], status="draft", app_status="applied")  # sent something else
    packet(conn, 4, URLS["lever"], status="abandoned", app_status="applied")
    pid = packet(conn, 5, URLS["lever"], status="draft")
    conn.execute(
        "UPDATE application SET resume_version = ? WHERE id = "
        "(SELECT application_id FROM application_packet WHERE id = ?)",
        (f"packet:{pid}/resume/v1", pid),
    )  # recorded by attach_sent_packet when it was ready
    assert stats.compute(conn, NOW).sent_with_packet == 3


def test_without_a_capture_log_table_stats_still_work(conn):
    conn.execute("DROP TABLE capture_log")  # an older schema
    assert "Captures: no capture_log table yet" in stats.compute(conn, NOW).format()
