"""pipeline.fanout: bounded pool, in-order hand-back, barriers, isolation, Ctrl-C."""

from __future__ import annotations

import threading
import time

import pytest

from jobhunter.pipeline.fanout import FetchCancelled, Stopper, run_ordered


def test_hands_back_in_item_order_whatever_finishes_first():
    done: list[int] = []
    run_ordered(
        [3, 2, 1, 0],
        lambda n: time.sleep(n * 0.02) or n,
        lambda item, out: done.append(out.value),
        workers=4,
        stopper=Stopper(),
    )
    assert done == [3, 2, 1, 0]


def test_at_most_workers_run_at_once():
    lock = threading.Lock()
    now = peak = 0

    def work(_):
        nonlocal now, peak
        with lock:
            now += 1
            peak = max(peak, now)
        time.sleep(0.02)
        with lock:
            now -= 1

    run_ordered(range(10), work, lambda *_: None, workers=3, stopper=Stopper())
    assert peak == 3


def test_one_item_raising_is_handed_back_and_the_rest_run():
    outs = {}

    def work(n):
        if n == 1:
            raise RuntimeError("boom")
        return n * 10

    run_ordered(range(4), work, lambda i, o: outs.update({i: o}), workers=2, stopper=Stopper())
    assert isinstance(outs[1].error, RuntimeError)
    assert [outs[i].value for i in (0, 2, 3)] == [0, 20, 30]


def test_inline_item_runs_alone_on_the_calling_thread_after_earlier_items():
    log: list[tuple[str, object]] = []
    running = set()
    lock = threading.Lock()

    def work(item):
        with lock:
            running.add(item)
            log.append(("start", item, frozenset(running), threading.current_thread().name))
        time.sleep(0.01)
        with lock:
            running.discard(item)
        return item

    handed: list[str] = []
    run_ordered(
        ["a", "b", "X", "c", "d"],
        work,
        lambda i, o: handed.append(i),
        workers=4,
        stopper=Stopper(),
        inline=lambda i: i == "X",
    )
    assert handed == ["a", "b", "X", "c", "d"]
    x = next(e for e in log if e[1] == "X")
    assert x[2] == frozenset({"X"})  # nothing else in flight
    assert x[3] == threading.current_thread().name
    starts = [e[1] for e in log]
    assert starts.index("X") > max(starts.index("a"), starts.index("b"))
    assert starts.index("X") < min(starts.index("c"), starts.index("d"))


def test_single_worker_runs_everything_on_the_calling_thread():
    names = set()
    run_ordered(
        range(3),
        lambda _: names.add(threading.current_thread().name),
        lambda *_: None,
        workers=1,
        stopper=Stopper(),
    )
    assert names == {threading.current_thread().name}


def test_interrupt_stops_waiting_workers_and_skips_queued_items():
    stopper = Stopper()
    started: list[int] = []

    def work(n):
        started.append(n)
        if n == 0:
            return n
        stopper.sleep(60)  # a rate-limit wait; must end at once on Ctrl-C
        return n

    def on_done(item, out):
        raise KeyboardInterrupt  # Ctrl-C arriving while the run thread waits

    t0 = time.monotonic()
    with pytest.raises(KeyboardInterrupt):
        run_ordered(range(10), work, on_done, workers=2, stopper=stopper)
    assert time.monotonic() - t0 < 5
    assert stopper.stopped
    assert len(started) <= 3  # queued items never started


def test_stopper_sleep_and_check():
    s = Stopper()
    s.sleep(0)
    s.check()
    s.stop()
    with pytest.raises(FetchCancelled):
        s.check()
    with pytest.raises(FetchCancelled):
        s.sleep(60)
    assert not isinstance(FetchCancelled(), Exception)  # adapters' except Exception misses it
