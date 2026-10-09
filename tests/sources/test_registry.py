"""Registry loader, policy invariants, DB sync and CLI (specs/003 "The registry", specs/010)."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from jobhunter.cli import app
from jobhunter.core.db import connect, migrate
from jobhunter.core.models import Policy, SourceClass
from jobhunter.sources.registry import enabled_sources, load_registry, sync_sources_table

# specs/010 "Open set" after live discovery (2026-10-09): UT, WI, LA, WV and OK turned out to be
# gated (login, Incapsula challenge, or a disallowing host) and are `manual`, so they are not
# enabled. MI, MP and WA have htmlconfig adapters.
OPEN_SET_KEYS = {
    "mi-mitalent",
    "mp-marianaslabor",
    "wa-worksourcewa",
    "wy-hire",
    "ky-kyjobs",
    "mt-montanaworks",
    "ny-newyork",
    "us-nlx",
    "us-usajobs",
}


@pytest.fixture(scope="module")
def rows():
    return load_registry()


def test_packaged_registry_loads(rows):
    # 54 state/territory job banks + national NLx (state null) + USAJOBS (class C).
    assert len(rows) == 56
    assert sum(r.class_ is SourceClass.A and r.state is None for r in rows) == 1
    assert sum(r.class_ is SourceClass.C for r in rows) == 1


def test_keys_unique(rows):
    keys = [r.key for r in rows]
    assert len(keys) == len(set(keys))


def test_enabled_sources_are_exactly_the_open_set(rows):
    enabled = {r.key for r in enabled_sources(rows)}
    assert enabled == OPEN_SET_KEYS


def test_no_vos_host_enabled(rows):
    # Louisiana was the one VOS host without Disallow, but it sits behind an Incapsula challenge.
    vos_enabled = {r.key for r in rows if r.family == "vos" and r.policy is Policy.enabled}
    assert vos_enabled == set()


def test_all_joblink_rows_blocked(rows):
    joblink = [r for r in rows if r.family == "joblink"]
    assert len(joblink) == 8
    assert all(r.policy is Policy.blocked for r in joblink)


def test_usajobs_row_is_sanctioned(rows):
    (usajobs,) = [r for r in rows if r.key == "us-usajobs"]
    assert usajobs.class_ is SourceClass.C
    assert usajobs.state is None
    assert usajobs.tier.value == "api"
    assert usajobs.policy is Policy.enabled
    assert usajobs.config["sanctioned_api"] is True
    assert usajobs.config["auth"] == {"email_env": "USAJOBS_EMAIL", "key_env": "USAJOBS_API_KEY"}
    assert usajobs.rate_limit.rps == 2
    assert usajobs.rate_limit.concurrency == 2


def test_wv_honours_crawl_delay_and_stale_entries_corrected(rows):
    by_key = {r.key: r for r in rows}
    assert by_key["wv-workforcewv"].rate_limit.rps == 0.1
    assert by_key["tn-jobs4tn"].entry.startswith("https://jobs4tnwfs.tn.gov/")
    assert by_key["wa-worksourcewa"].entry.startswith("https://worksource.my.site.com/")


def test_manual_rows_are_the_undeterminable_ones(rows):
    manual = {r.state for r in rows if r.policy is Policy.manual}
    assert manual == {"DC", "NH", "MO", "OH", "CO", "NJ", "UT", "WI", "LA", "WV", "OK"}


def test_robots_checked_recorded(rows):
    assert all(r.robots.checked is not None for r in rows if r.class_ is SourceClass.A)


def test_loader_names_row_and_field(tmp_path):
    bad = tmp_path / "registry.yaml"
    bad.write_text(
        '- key: "xx-bad"\n  state: "XX"\n  class: "A"\n  name: "Bad"\n  family: "vos"\n'
        '  tier: "http"\n  entry: "not a url"\n  policy: "enabled"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match=r"xx-bad.*entry"):
        load_registry(bad)


def test_loader_rejects_bad_policy(tmp_path):
    bad = tmp_path / "registry.yaml"
    bad.write_text(
        '- key: "xx-bad"\n  state: "XX"\n  class: "A"\n  name: "Bad"\n  family: "vos"\n'
        '  tier: "http"\n  entry: "https://example.org/"\n  policy: "maybe"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match=r"xx-bad.*policy"):
        load_registry(bad)


def test_loader_rejects_duplicate_keys(tmp_path):
    row = (
        '- key: "xx-dup"\n  state: "XX"\n  class: "A"\n  name: "Dup"\n  family: "vos"\n'
        '  tier: "http"\n  entry: "https://example.org/"\n'
    )
    dup = tmp_path / "registry.yaml"
    dup.write_text(row + row, encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate key 'xx-dup'"):
        load_registry(dup)


def test_sync_upserts_idempotently_and_keeps_run_status(rows):
    conn = connect(":memory:")
    migrate(conn)
    assert sync_sources_table(conn, rows) == 56
    assert sync_sources_table(conn, rows) == 56
    assert conn.execute("SELECT COUNT(*) FROM source").fetchone()[0] == 56

    ny = conn.execute("SELECT policy, status, robots_checked FROM source WHERE key='ny-newyork'")
    row = ny.fetchone()
    assert row["policy"] == "enabled"
    assert row["status"] == "ok"
    assert row["robots_checked"].startswith("2026-10-09")

    conn.execute("UPDATE source SET status='broken', status_note='timeout' WHERE key='ny-newyork'")
    sync_sources_table(conn, rows)
    kept = conn.execute("SELECT status, status_note FROM source WHERE key='ny-newyork'").fetchone()
    assert (kept["status"], kept["status_note"]) == ("broken", "timeout")

    blocked = conn.execute("SELECT status FROM source WHERE key='tx-workintexas'").fetchone()
    assert blocked["status"] == "blocked"


def test_cli_sources_list_runs():
    runner = CliRunner()
    result = runner.invoke(app, ["sources", "list"])
    assert result.exit_code == 0, result.output
    assert "tx-workintexas" in result.output
    assert "us-usajobs" in result.output


def test_cli_sources_list_enabled_and_state():
    runner = CliRunner()
    enabled = runner.invoke(app, ["sources", "list", "--enabled"])
    assert enabled.exit_code == 0
    assert "us-usajobs" in enabled.output
    assert "tx-workintexas" not in enabled.output

    ny = runner.invoke(app, ["sources", "list", "--state", "ny"])
    assert ny.exit_code == 0
    assert "ny-newyork" in ny.output
    assert "tx-workintexas" not in ny.output
