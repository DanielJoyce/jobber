"""sources verify tests. httpx.MockTransport only: no network."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from functools import partial

import httpx
import pytest
from typer.testing import CliRunner

from jobhunter import cli
from jobhunter.config import Settings
from jobhunter.core import db
from jobhunter.core.fetch import FetchContext, reset_shared_state
from jobhunter.core.models import SourceRow
from jobhunter.sources import verify as vf
from jobhunter.sources.registry import sync_sources_table

NOW = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)
GOOD_BUNDLE = b"bundle-v1"
GOOD_SHA = hashlib.sha256(GOOD_BUNDLE).hexdigest()
OPEN_ROBOTS = "User-agent: *\nDisallow: /admin/\n"
VOS_PAGE = "<html>Geographic Solutions <a href='/vosnet/Default.aspx'>x</a> __VIEWSTATE</html>"
JOBLINK_PAGE = "<html><script src='/packs/js/4743-860615d0fe49e3040bc6.js'></script></html>"


@pytest.fixture(autouse=True)
def _state(monkeypatch):
    # The real bundle sha is only knowable on the network; tests substitute a fixture bundle.
    monkeypatch.setattr(vf, "JOBLINK_BUNDLE_SHA_PREFIX", GOOD_SHA[:16])
    reset_shared_state()
    yield
    reset_shared_state()


class Clock:
    t = 0.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


class Site:
    """Serves one host: robots text, entry page, bundle; records requests."""

    def __init__(self, robots=OPEN_ROBOTS, page=VOS_PAGE, status=200, bundle=GOOD_BUNDLE):
        self.robots, self.page, self.status, self.bundle = robots, page, status, bundle
        self.paths: list[str] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.paths.append(path)
        if path == "/robots.txt":
            return httpx.Response(200, text=self.robots)
        if path.startswith("/packs/js/"):
            return httpx.Response(200, content=self.bundle)
        return httpx.Response(self.status, text=self.page)


def src(key="xx-vos", family="vos", entry="https://jobs.example.gov/vosnet/Default.aspx", **kw):
    data = {
        "key": key,
        "state": "XX",
        "class": "A",
        "name": key,
        "family": family,
        "tier": "http",
        "entry": entry,
        "family_signals": ["Geographic Solutions", "__VIEWSTATE"],
        "robots": {"status": "partial", "checked": "2026-10-09"},
        "policy": "enabled",
    } | kw
    return SourceRow.model_validate(data)


JOBLINK_ROBOTS = "User-agent: *\nDisallow: /search/jobs\n"


def joblink(**kw):
    return src(
        **{
            "key": "yy-joblink",
            "family": "joblink",
            "entry": "https://joblink.example.gov/",
            "family_signals": ["identical bundle sha 2c0d5d29e49fd986 (per 003/010)"],
            "policy": "blocked",
        }
        | kw
    )


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.migrate(c)
    yield c
    c.close()


@pytest.fixture
def settings(tmp_path):
    return Settings.model_validate({"paths": {"cache_dir": str(tmp_path / "cache")}})


def factory(site: Site, conn, settings):
    def make(row):
        clock = Clock()
        return FetchContext(
            row,
            settings,
            conn,
            transport=httpx.MockTransport(site.handle),
            clock=clock,
            sleep=clock.sleep,
        )

    return make


def run_one(row, site, conn, settings, **kw):
    sync_sources_table(conn, [row])
    report = vf.verify_all(conn, [row], now=NOW, ctx_factory=factory(site, conn, settings), **kw)
    return report.results[0], report


def db_row(conn, key):
    return conn.execute("SELECT * FROM source WHERE key = ?", (key,)).fetchone()


# ─── verify_source / classification ─────────────────────────────────────────


def test_unchanged_source_is_ok(conn, settings):
    site = Site()
    res, report = run_one(src(), site, conn, settings)
    assert res.classification == "ok", res.problems
    assert res.http_status == 200
    assert res.bytes == len(VOS_PAGE)
    assert {"vosnet", "geographic_solutions", "viewstate"} <= set(res.signals)
    assert res.robots.status == "partial"
    assert res.robots.disallowed == ["/admin/"]
    assert res.robots.sha256 == hashlib.sha256(OPEN_ROBOTS.encode()).hexdigest()
    assert report.clean


def test_missing_family_signal_is_drift(conn, settings):
    site = Site(page="<html>Welcome to the new jobs portal</html>")
    res, report = run_one(src(), site, conn, settings)
    assert res.classification == "drift"
    assert any("geographic_solutions" in p for p in res.problems)
    assert not report.clean


def test_new_signal_for_other_platform_is_drift(conn, settings):
    site = Site(page=VOS_PAGE + " myworkdayjobs.com")
    res, _ = run_one(src(), site, conn, settings)
    assert res.classification == "drift"
    assert any("workday" in p for p in res.problems)


def test_non_2xx_entry_is_broken(conn, settings):
    res, _ = run_one(src(), Site(status=404), conn, settings)
    assert res.classification == "broken"
    assert res.http_status == 404


def test_robots_hash_change_with_new_search_disallow_is_blocked(conn, settings):
    row = joblink(policy="enabled")
    sync_sources_table(conn, [row])
    first = Site(page=JOBLINK_PAGE)
    r1 = vf.verify_all(conn, [row], now=NOW, ctx_factory=factory(first, conn, settings))
    assert r1.results[0].classification == "ok", r1.results[0].problems
    old_hash = db_row(conn, row.key)["robots_hash"]
    reset_shared_state()
    second = Site(page=JOBLINK_PAGE, robots="User-agent: *\nDisallow: /search/jobs\n")
    r2 = vf.verify_all(conn, [row], now=NOW, ctx_factory=factory(second, conn, settings))
    res = r2.results[0]
    assert res.classification == "blocked"
    assert any("/search/jobs" in p for p in res.problems)
    assert any("hash changed" in n for n in res.notes)
    assert db_row(conn, row.key)["robots_hash"] != old_hash
    assert db_row(conn, row.key)["status"] == "blocked"


def test_robots_hash_change_without_allowance_change_is_note_only(conn, settings):
    row = src()
    sync_sources_table(conn, [row])
    vf.verify_all(conn, [row], now=NOW, ctx_factory=factory(Site(), conn, settings))
    reset_shared_state()
    site = Site(robots=OPEN_ROBOTS + "Disallow: /tmp/\n")
    res = vf.verify_all(conn, [row], now=NOW, ctx_factory=factory(site, conn, settings)).results[0]
    assert res.classification == "ok"
    assert any("hash changed" in n and "unchanged" in n for n in res.notes)


def test_entry_disallowed_by_robots_is_not_fetched(conn, settings):
    site = Site(robots="User-agent: *\nDisallow: /\n")
    res, _ = run_one(src(), site, conn, settings)
    assert res.classification == "blocked"
    assert not res.entry_fetched
    assert set(site.paths) == {"/robots.txt"}
    assert db_row(conn, "xx-vos")["robots_status"] == "disallow_all"


def test_known_closed_source_stays_ok_and_is_not_fetched(conn, settings):
    row = src(policy="blocked", robots={"status": "disallow_all", "checked": "2026-10-09"})
    site = Site(robots="User-agent: *\nDisallow: /\n")
    res, report = run_one(row, site, conn, settings)
    assert res.classification == "ok"
    assert set(site.paths) == {"/robots.txt"}
    assert report.clean


def test_joblink_bundle_match_is_ok(conn, settings):
    site = Site(page=JOBLINK_PAGE, robots=JOBLINK_ROBOTS)
    res, _ = run_one(joblink(), site, conn, settings)
    assert res.classification == "ok", res.problems
    assert res.bundle_checked
    assert res.bundle_sha == GOOD_SHA


def test_joblink_bundle_hash_mismatch_is_drift(conn, settings):
    site = Site(page=JOBLINK_PAGE, robots=JOBLINK_ROBOTS, bundle=b"bundle-v2-upgraded")
    res, report = run_one(joblink(), site, conn, settings)
    assert res.classification == "drift"
    assert any("bundle" in p for p in res.problems)
    assert not report.clean
    # policy-owned status is left alone; the note records why
    cur = db_row(conn, "yy-joblink")
    assert cur["status"] == "blocked"
    assert cur["status_note"].startswith("verify: ")


# ─── verify_all DB writes ───────────────────────────────────────────────────


def test_db_columns_written(conn, settings):
    run_one(src(), Site(), conn, settings)
    cur = db_row(conn, "xx-vos")
    assert cur["robots_status"] == "partial"
    assert cur["robots_hash"] == hashlib.sha256(OPEN_ROBOTS.encode()).hexdigest()
    assert cur["robots_checked"] == NOW.isoformat()
    assert cur["verified_at"] == "2026-10-12"
    assert cur["status"] == "ok"


def test_drift_sets_suspect_and_clears_when_resolved(conn, settings):
    row = src()
    run_one(row, Site(page="<html>moved</html>"), conn, settings)
    cur = db_row(conn, "xx-vos")
    assert cur["status"] == "suspect"
    assert "geographic_solutions" in cur["status_note"]
    reset_shared_state()
    vf.verify_all(conn, [row], now=NOW, ctx_factory=factory(Site(), conn, settings))
    cur = db_row(conn, "xx-vos")
    assert (cur["status"], cur["status_note"]) == ("ok", None)


def test_never_downgrades_runner_broken(conn, settings):
    row = src()
    sync_sources_table(conn, [row])
    conn.execute(
        "UPDATE source SET status = 'broken', status_note = 'runner: 3 failures' WHERE key = ?",
        (row.key,),
    )
    vf.verify_all(
        conn, [row], now=NOW, ctx_factory=factory(Site(page="<html>moved</html>"), conn, settings)
    )
    cur = db_row(conn, row.key)
    assert (cur["status"], cur["status_note"]) == ("broken", "runner: 3 failures")
    assert cur["verified_at"] == "2026-10-12"


def test_broken_entry_writes_broken(conn, settings):
    run_one(src(), Site(status=500), conn, settings)
    assert db_row(conn, "xx-vos")["status"] == "broken"


def test_state_filter(conn, settings):
    a, b = src(), src(key="zz-vos", state="ZZ")
    sync_sources_table(conn, [a, b])
    site = Site()
    report = vf.verify_all(
        conn, [a, b], now=NOW, states={"ZZ"}, ctx_factory=factory(site, conn, settings)
    )
    assert [r.key for r in report.results] == ["zz-vos"]


# ─── CLI ────────────────────────────────────────────────────────────────────

runner = CliRunner()


def cli_setup(monkeypatch, tmp_path, site, rows):
    settings = Settings.model_validate(
        {"paths": {"cache_dir": str(tmp_path / "cache"), "db_path": str(tmp_path / "t.db")}}
    )
    clock = Clock()
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    monkeypatch.setattr(cli, "load_registry", lambda: rows)
    monkeypatch.setattr(
        vf,
        "FetchContext",
        partial(
            FetchContext, transport=httpx.MockTransport(site.handle), clock=clock, sleep=clock.sleep
        ),
    )


def test_cli_exit_zero_when_clean(monkeypatch, tmp_path):
    cli_setup(monkeypatch, tmp_path, Site(), [src()])
    result = runner.invoke(cli.app, ["sources", "verify"])
    assert result.exit_code == 0, result.output
    assert "xx-vos" in result.output
    assert "1 ok" in result.output


def test_cli_exit_nonzero_on_drift_and_json(monkeypatch, tmp_path):
    cli_setup(monkeypatch, tmp_path, Site(page="<html>moved</html>"), [src()])
    result = runner.invoke(cli.app, ["sources", "verify", "--json"])
    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["clean"] is False
    assert data["counts"]["drift"] == 1
    entry = data["results"][0]
    assert entry["key"] == "xx-vos"
    assert entry["classification"] == "drift"
    assert entry["http_status"] == 200
    assert entry["robots"]["status"] == "partial"


def test_cli_text_lists_drift_summary(monkeypatch, tmp_path):
    cli_setup(monkeypatch, tmp_path, Site(status=503), [src()])
    result = runner.invoke(cli.app, ["sources", "verify"])
    assert result.exit_code == 1
    assert "[broken]" in result.output
    assert "registry.yaml is not edited" in result.output
