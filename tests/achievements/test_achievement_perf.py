"""Performance budget (specs/021 "How unlocks are computed"): the read phase under 250 ms on
a synthetic database ten times the user's (200 applications, 3,000 labels, 2,000 events), and
the fingerprint cheap."""

from __future__ import annotations

import time
from datetime import timedelta

from achv_world import NOW, TZ, iso

from jobhunter.achievements import engine


def build(world) -> None:
    conn = world.conn
    conn.execute("BEGIN")
    start = NOW - timedelta(days=200)
    groups = [world.group() for _ in range(3000)]
    for i, gid in enumerate(groups):
        label = "not_interesting" if i % 3 else "interesting"
        conn.execute(
            "INSERT INTO label (job_group_id, label, labeled_at) VALUES (?, ?, ?)",
            (gid, label, iso(start + timedelta(hours=i * 1.5))),
        )
    statuses = [
        "interested",
        "applied",
        "acknowledged",
        "screening",
        "interview",
        "rejected",
        "no_response",
        "applied",
        "withdrawn",
        "offer",
    ]
    for a in range(200):
        cur = conn.execute(
            "INSERT INTO application (job_group_id, status, created_at, updated_at) "
            "VALUES (?, 'applied', ?, ?)",
            (groups[a], iso(NOW), iso(NOW)),
        )
        app_id = cur.lastrowid
        base = start + timedelta(days=a * 0.9)
        for e in range(10):
            at = iso(base + timedelta(days=e * 2))
            conn.execute(
                "INSERT INTO application_event (application_id, at, status, note, source, "
                "recorded_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    app_id,
                    at,
                    statuses[e],
                    "a note" if e % 2 else None,
                    "email" if e == 1 else "manual",
                    at,
                ),
            )
        conn.execute(
            "INSERT INTO followup_log (application_id, at, kind, due_at) VALUES (?, ?, ?, ?)",
            (app_id, iso(base), "planned", (base + timedelta(days=7)).date().isoformat()),
        )
        conn.execute(
            "INSERT INTO contact (application_id, name, created_at) VALUES (?, 'x', ?)",
            (app_id, iso(base)),
        )
    for r in range(60):
        conn.execute(
            "INSERT INTO rejection (received_at, source, created_at, state) "
            "VALUES (?, 'email', ?, 'confirmed')",
            (iso(start + timedelta(days=r * 3)), iso(start + timedelta(days=r * 3))),
        )
    conn.execute("COMMIT")


def best_of(n: int, fn) -> float:
    times = []
    for _ in range(n):
        t = time.perf_counter()
        fn()
        times.append(time.perf_counter() - t)
    return min(times)


def test_read_phase_and_fingerprint_within_budget(world):
    build(world)
    assert world.conn.execute("SELECT COUNT(*) FROM application_event").fetchone()[0] == 2000
    comp = engine.compute(world.conn, NOW, TZ)
    assert comp.series["search.decide"].progress > 0  # the rules really ran over the data
    assert comp.series["apply.momentum"].progress > 50
    read = best_of(3, lambda: engine.compute(world.conn, NOW, TZ))
    fp = best_of(5, lambda: engine.fingerprint(world.conn, NOW, TZ))
    assert read < 0.25, f"read phase {read * 1000:.0f} ms"
    assert fp < 0.01, f"fingerprint {fp * 1000:.1f} ms"


def test_today_strip_warm_call_is_cheap_and_cold_call_is_one_read(world):
    build(world)
    gate = engine.Gate(tz=TZ)
    t = time.perf_counter()
    cold = engine.today_strip(gate, world.conn, NOW)
    cold_s = time.perf_counter() - t
    assert cold is not None and cold.earned > 0 and cold.rhythm is not None
    warm = best_of(5, lambda: engine.today_strip(gate, world.conn, NOW))
    assert engine.today_strip(gate, world.conn, NOW) == cold
    assert warm < 0.05, f"warm strip {warm * 1000:.1f} ms"
    assert cold_s < 1.0, f"cold strip {cold_s * 1000:.0f} ms"
