"""Run lock shared by host and containers (specs/018 Run lock, C5).

The holder runs in a separate process, as a timer job or a container would: flock is per
open file description, and the kernel must release it when that process dies.
"""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime, timedelta

import pytest

from jobhunter.core import runlock


@pytest.fixture
def holder(run_lock_holder):
    return run_lock_holder


def test_lock_file_is_keyed_on_the_database_path(tmp_path):
    assert runlock.lock_path(tmp_path / "jh.db") == tmp_path / "jh.db.run.lock"


def test_holder_writes_metadata_through_its_fd_and_clears_it_on_release(tmp_path):
    lock = runlock.RunLock(tmp_path / "jh.db", "score --submit")
    lock.acquire(None)
    data = json.loads(lock.path.read_text())
    assert data["pid"] == os.getpid()
    assert data["command"] == "score --submit"
    assert data["host"] and data["started_at"].endswith("+00:00")
    assert lock.path.stat().st_mode & 0o777 == 0o600
    lock.release()
    assert lock.path.read_text() == ""


def test_second_holder_in_another_process_is_refused_without_waiting(tmp_path, holder):
    holder(tmp_path / "jh.db")
    lock = runlock.RunLock(tmp_path / "jh.db", "score --submit")
    with pytest.raises(runlock.LockHeld) as err:
        lock.acquire(None)
    assert not isinstance(err.value, runlock.LockTimeout)
    assert err.value.holder is not None and err.value.holder.command == "run"
    assert "another jobhunter job is running (run, pid " in str(err.value)
    assert not lock.held


def test_a_different_database_has_a_different_lock(tmp_path, holder):
    holder(tmp_path / "a.db")
    lock = runlock.RunLock(tmp_path / "b.db", "run")
    assert lock.try_acquire()
    lock.release()


def test_two_fds_in_one_process_also_exclude_each_other(tmp_path):
    # The console's re-score thread and another request in the same process must not both
    # think they hold it.
    first = runlock.RunLock(tmp_path / "jh.db", "console re-score")
    assert first.try_acquire()
    assert not runlock.RunLock(tmp_path / "jh.db", "console re-score").try_acquire()
    first.release()
    assert runlock.RunLock(tmp_path / "jh.db", "x").try_acquire()


def test_kernel_releases_the_lock_when_the_holder_is_killed(tmp_path, holder):
    h = holder(tmp_path / "jh.db")
    assert not runlock.RunLock(tmp_path / "jh.db", "run").try_acquire()
    h.kill()
    lock = runlock.RunLock(tmp_path / "jh.db", "run")
    assert lock.try_acquire()  # its stale metadata does not matter: the flock is gone
    lock.release()


def test_wait_gets_the_lock_once_the_holder_finishes(tmp_path, holder, monkeypatch):
    monkeypatch.setattr(runlock, "POLL_S", 0.05)
    h = holder(tmp_path / "jh.db")
    polls: list[float] = []

    def sleep(s: float) -> None:
        polls.append(s)
        if len(polls) == 3:
            h.release()
        time.sleep(s)

    lock = runlock.RunLock(tmp_path / "jh.db", "score --collect-pending", sleep=sleep)
    lock.acquire(30)
    assert lock.held and len(polls) >= 3
    lock.release()


def test_wait_times_out(tmp_path, holder):
    holder(tmp_path / "jh.db")
    clock = [0.0]

    def sleep(s: float) -> None:
        clock[0] += s

    lock = runlock.RunLock(tmp_path / "jh.db", "run", sleep=sleep, monotonic=lambda: clock[0])
    with pytest.raises(runlock.LockTimeout, match="gave up after waiting 7200 s"):
        lock.acquire(2 * 3600)
    assert clock[0] == pytest.approx(7200)


def test_holder_older_than_six_hours_is_reported_even_without_wait(tmp_path, holder):
    holder(tmp_path / "jh.db")
    later = lambda: datetime.now(UTC) + timedelta(hours=6, minutes=5)  # noqa: E731
    for wait in (None, 3600):
        lock = runlock.RunLock(tmp_path / "jh.db", "run", clock=later, sleep=lambda s: None)
        with pytest.raises(runlock.StaleHolder, match=r"held the run lock for 6\.1 h"):
            lock.acquire(wait)
    with pytest.raises(runlock.StaleHolder):
        runlock.RunLock(tmp_path / "jh.db", "run", clock=later).try_acquire()


def test_unreadable_metadata_is_not_treated_as_stale(tmp_path):
    first = runlock.RunLock(tmp_path / "jh.db", "run")
    first.acquire(None)
    os.pwrite(first._fd, b"{half", 0)
    os.ftruncate(first._fd, 5)
    with pytest.raises(runlock.LockHeld, match="details unknown"):
        runlock.RunLock(tmp_path / "jh.db", "run").acquire(None)
    first.release()


@pytest.mark.parametrize(
    ("text", "seconds"), [("90", 90), ("90s", 90), ("30m", 1800), ("2h", 7200), ("1d", 86400)]
)
def test_parse_duration(text, seconds):
    assert runlock.parse_duration(text) == seconds


@pytest.mark.parametrize("text", ["", "h", "2 hours", "-1h", "1w"])
def test_parse_duration_rejects(text):
    with pytest.raises(ValueError):
        runlock.parse_duration(text)
