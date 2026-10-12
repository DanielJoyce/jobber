"""`jobhunter doctor` (specs/019, ticket S2): output, exit codes, the guard exemptions, the
read-only database, --containers with fixed preflight ids, and that nothing leaks a value.
Synthetic data only; no network, no container, no subprocess."""

from __future__ import annotations

import ast
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from jobhunter import cli, container, legacy_data
from jobhunter.cli import app
from jobhunter.console.app import create_app
from jobhunter.core import db
from jobhunter.ops import doctor, preflight
from test_readiness import (
    FAKE_KEY,
    SENTINEL,
    make_db,
    make_settings,
    sentinel_setup,
    write_prefs,
    write_resume,
)

runner = CliRunner()


def write_config(tmp_path: Path, monkeypatch, *, contact: bool = True, extra: str = "") -> Path:
    data = tmp_path / "data"
    text = (
        f'[paths]\ndata_dir = "{data}"\ndb_path = "{data / "jobhunter.db"}"\n'
        f'profile_dir = "{data / "profile"}"\nresume_path = "{data / "resume"}"\n'
        f'cache_dir = "{tmp_path / "cache"}"\n'
    )
    if contact:
        text += '[fetch]\nuser_agent_contact = "jobs@example.org"\n'
    cfg = tmp_path / "config.toml"
    cfg.write_text(text + extra, encoding="utf-8")
    monkeypatch.setenv("JOBHUNTER_CONFIG", str(cfg))
    return cfg


@pytest.fixture
def no_key(monkeypatch):
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE"):
        monkeypatch.delenv(name, raising=False)


def complete_setup(tmp_path, monkeypatch):
    write_config(tmp_path, monkeypatch)
    settings = make_settings(tmp_path)
    write_prefs(settings)
    write_resume(settings)
    make_db(settings).close()
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_KEY)
    return settings


def run_doctor(*args: str):
    return runner.invoke(app, ["doctor", *args])


def digest(root: Path) -> dict[str, str]:
    """File hashes below ``root`` without SQLite's -wal/-shm sidecars (which a read-only
    connection may create and which hold no data)."""
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file() and not p.name.endswith(("-wal", "-shm"))
    }


# --- output and exit codes ---------------------------------------------------------------------


def test_fresh_install_exits_1_and_names_the_missing_essentials(tmp_path, monkeypatch, no_key):
    write_config(tmp_path, monkeypatch, contact=False)
    result = run_doctor()
    assert result.exit_code == 1, result.output
    lines = result.output.splitlines()
    assert "Basics" in lines and "Profile" in lines and "Scoring" in lines
    assert any(ln.lstrip().startswith("BROKEN") and "contact" in ln.lower() for ln in lines)
    assert any(ln.lstrip().startswith("MISSING") and "Screen scorer" in ln for ln in lines)
    assert any(ln.lstrip().startswith("MISSING") and "Resume" in ln for ln in lines)
    assert "essentials failing" in lines[-1] and "jobhunter doctor -v" in lines[-1]


def test_complete_setup_exits_0_even_with_recommended_items_open(tmp_path, monkeypatch):
    complete_setup(tmp_path, monkeypatch)
    result = run_doctor("--json")
    assert result.exit_code == 0, result.output
    rows = {r["id"]: r for r in json.loads(result.output)}
    # recommended rows are open (no timers, no backups) and still do not change the exit
    open_recommended = [
        r
        for r in rows.values()
        if r["level"] == "recommended" and r["status"] in ("missing", "broken")
    ]
    assert open_recommended
    assert not [r for r in rows.values() if r["level"] == "essential" and r["status"] != "ok"]
    plain = run_doctor()
    assert plain.exit_code == 0
    assert "all essentials ok" in plain.output.splitlines()[-1]


def test_plain_lists_essentials_and_problems_but_verbose_lists_every_row(tmp_path, monkeypatch):
    complete_setup(tmp_path, monkeypatch)
    plain = run_doctor().output
    verbose = run_doctor("-v").output
    assert "Settings file" in plain  # an essential, listed though ok
    assert "PDF export" not in plain and "Extension pairing" not in plain
    for title in ("PDF export", "Extension pairing"):
        assert title in verbose
    assert "unlocks:" in verbose and "from:" in verbose
    assert "help: /help/" in verbose
    assert "help: /help/" not in plain
    assert "jobhunter.db-wal" in verbose  # the WAL sidecar note
    assert len(verbose.splitlines()) > len(plain.splitlines())


def test_json_is_the_check_list(tmp_path, monkeypatch):
    complete_setup(tmp_path, monkeypatch)
    result = run_doctor("--json")
    rows = json.loads(result.output)
    ids = [r["id"] for r in rows]
    assert ids[:3] == ["config.file", "data.layout", "db.schema"]
    assert {"id", "group", "level", "status", "due", "title", "detail", "unlocks", "fix"} <= set(
        rows[0]
    )
    assert "data.layout" not in doctor.readiness.CHECK_IDS  # doctor-only


def test_group_filters_rows_and_exit_code(tmp_path, monkeypatch, no_key):
    write_config(tmp_path, monkeypatch)
    settings = make_settings(tmp_path)
    write_prefs(settings)  # no resume: Profile fails; Basics is fine
    basics = run_doctor("--group", "basics", "--json")
    assert {r["group"] for r in json.loads(basics.output)} == {"Basics"}
    assert basics.exit_code == 0
    profile = run_doctor("--group", "PROFILE", "--json")
    assert {r["group"] for r in json.loads(profile.output)} == {"Profile"}
    assert profile.exit_code == 1
    assert run_doctor("--group", "email-alerts", "--json").exit_code == 0


def test_unknown_group_and_unavailable_options_are_usage_errors(tmp_path, monkeypatch):
    write_config(tmp_path, monkeypatch)
    bad = run_doctor("--group", "nope")
    assert bad.exit_code == 2 and "unknown group" in bad.output
    assert run_doctor("--no-such-option").exit_code == 2


# --- guards, read-only ---------------------------------------------------------------------------


def test_runs_with_old_layout_data_and_does_not_create_anything(tmp_path, monkeypatch, no_key):
    write_config(tmp_path, monkeypatch)
    cwd = tmp_path / "checkout"
    (cwd / "data").mkdir(parents=True)
    (cwd / "data" / "jobhunter.db").write_bytes(b"old")
    monkeypatch.chdir(cwd)
    monkeypatch.setattr(legacy_data, "main_checkout", lambda: None)
    # explicit [paths] db_path makes this an "allowed" case; drop it to get the refusal
    cfg = tmp_path / "config.toml"
    cfg.write_text('[fetch]\nuser_agent_contact = "jobs@example.org"\n', encoding="utf-8")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    blocked = runner.invoke(app, ["apply", "stats"])
    assert blocked.exit_code == 1 and "migrate-paths" in blocked.output
    assert "jobhunter doctor" in blocked.output  # the refusal points at doctor
    result = run_doctor("--json")
    assert result.exit_code == 1, result.output
    rows = {r["id"]: r for r in json.loads(result.output)}
    assert rows["data.layout"]["status"] == "broken"
    assert rows["data.layout"]["level"] == "essential"
    assert not (tmp_path / "xdg").exists()


def test_pending_schema_with_marker_is_reported_and_nothing_is_written(tmp_path, monkeypatch):
    settings = complete_setup(tmp_path, monkeypatch)
    real = db._load_migrations()
    older = real[-3][0]
    path = Path(settings.paths.db_path)
    path.unlink()
    monkeypatch.setattr(db, "_load_migrations", lambda: [m for m in real if m[0] <= older])
    conn = db.connect(path)
    db.migrate(conn)
    conn.close()
    monkeypatch.setattr(db, "_load_migrations", lambda: real)
    (path.parent / "deployment.toml").write_text('mode = "container"\n')
    before = digest(tmp_path)
    result = run_doctor("--json")
    rows = {r["id"]: r for r in json.loads(result.output)}
    assert rows["db.schema"]["status"] == "broken"
    assert result.exit_code == 1
    after = digest(tmp_path)
    assert after == before
    assert not (path.parent / "backups").exists()
    # and a host install without the marker is not migrated by doctor either
    (path.parent / "deployment.toml").unlink()
    before = digest(tmp_path)
    run_doctor()
    assert digest(tmp_path) == before
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == older
    conn.close()


def test_newer_database_is_reported_not_refused(tmp_path, monkeypatch):
    settings = complete_setup(tmp_path, monkeypatch)
    raw = sqlite3.connect(settings.paths.db_path)
    raw.execute("INSERT INTO schema_version VALUES (9999, 'future', 't')")
    raw.commit()
    raw.close()
    result = run_doctor("--json")
    rows = {r["id"]: r for r in json.loads(result.output)}
    assert rows["db.schema"]["status"] == "broken"
    assert "newer" in rows["db.schema"]["detail"]
    assert result.exit_code == 1
    assert "Traceback" not in result.output


def test_broken_config_is_a_row_naming_only_the_exception_type(tmp_path, monkeypatch, no_key):
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'[fetch]\nuser_agent_contact = "{SENTINEL}\n[[[\n', encoding="utf-8")
    monkeypatch.setenv("JOBHUNTER_CONFIG", str(cfg))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    for args in ([], ["--json"], ["-v"]):
        result = run_doctor(*args)
        assert result.exit_code == 1, result.output
        assert "Traceback" not in result.output
        assert SENTINEL not in result.output
    rows = {r["id"]: r for r in json.loads(run_doctor("--json").stdout)}
    assert rows["config.file"]["status"] == "broken"
    assert "TOMLDecodeError" in rows["config.file"]["detail"]
    assert not (tmp_path / "xdg").exists()


def test_doctor_is_in_both_exemption_sets():
    assert "doctor" in cli.UNGUARDED_COMMANDS
    assert "doctor" in cli.MIGRATION_EXEMPT_COMMANDS


# --- containers ---------------------------------------------------------------------------


def fake_result(lines, marker=False):
    res = preflight.Result()
    for status, text, pid in lines:
        res.add(status, text, id=pid)
    res.marker_present = marker
    return res


@pytest.fixture
def stub_preflight(monkeypatch):
    calls: list[dict] = []
    holder = {"result": fake_result([])}

    def fake_run(ctx, **kwargs):
        calls.append(kwargs)
        return holder["result"]

    monkeypatch.setattr(preflight, "run", fake_run)
    monkeypatch.setattr(cli, "build_preflight_context", lambda *a, **k: object())
    return calls, holder


def test_plain_doctor_never_runs_preflight(tmp_path, monkeypatch, stub_preflight):
    complete_setup(tmp_path, monkeypatch)
    calls, _ = stub_preflight
    run_doctor()
    run_doctor("--json")
    assert calls == []


def test_containers_maps_ids_with_fixed_messages(tmp_path, monkeypatch, stub_preflight):
    complete_setup(tmp_path, monkeypatch)
    calls, holder = stub_preflight
    holder["result"] = fake_result(
        [
            (preflight.OK, "podman reachable (podman)", "podman"),
            (preflight.OK, f"dir {SENTINEL} exists", "bind_sources"),
            (preflight.WARN, f"stderr {SENTINEL}", "bind_sources"),
            (preflight.FAIL, f"quadlet said {SENTINEL}", "quadlet"),
            (preflight.WARN, f"mystery {SENTINEL}", ""),
            (preflight.FAIL, f"made up {SENTINEL}", "not_an_id"),
        ]
    )
    text = run_doctor("--containers")
    assert calls == [{"dry_run": True}]
    assert "Container preflight (dry run)" in text.output
    assert SENTINEL in text.output  # terminal-only free text
    result = run_doctor("--containers", "--json")
    assert SENTINEL not in result.output  # --json carries fixed messages only
    rows = {r["id"]: r for r in json.loads(result.output)}
    assert rows["preflight.podman"]["status"] == "ok"
    assert rows["preflight.bind_sources"]["status"] == "broken"  # worst line wins
    assert rows["preflight.bind_sources"]["level"] == "recommended"  # no marker
    assert rows["preflight.quadlet"]["status"] == "broken"
    # an unknown or missing id keeps its severity under preflight_other, never ok
    assert rows["preflight.preflight_other"]["status"] == "broken"
    assert result.exit_code == 0  # no marker: every preflight row is recommended


def test_preflight_fail_is_essential_when_the_marker_exists(tmp_path, monkeypatch, stub_preflight):
    complete_setup(tmp_path, monkeypatch)
    _, holder = stub_preflight
    holder["result"] = fake_result(
        [
            (preflight.FAIL, "x", "quadlet"),
            (preflight.WARN, "y", "labels"),
            (preflight.OK, "z", "marker"),
        ],
        marker=True,
    )
    result = run_doctor("--containers", "--json")
    rows = {r["id"]: r for r in json.loads(result.output)}
    assert rows["preflight.quadlet"]["level"] == "essential"
    assert rows["preflight.labels"]["level"] == "recommended"  # a WARN is never essential
    assert result.exit_code == 1
    holder["result"] = fake_result([(preflight.WARN, "y", "labels")], marker=True)
    assert run_doctor("--containers", "--json").exit_code == 0


def test_containers_replaces_the_unknown_placeholder_row(tmp_path, monkeypatch, stub_preflight):
    settings = complete_setup(tmp_path, monkeypatch)
    (Path(settings.paths.data_dir) / "deployment.toml").write_text("x\n")  # host-managing
    plain = {r["id"]: r for r in json.loads(run_doctor("--json").stdout)}
    assert plain["containers.deployment"]["status"] == "unknown"
    both = {r["id"]: r for r in json.loads(run_doctor("--containers", "--json").output)}
    assert "containers.deployment" not in both


def test_containers_is_refused_inside_a_container(tmp_path, monkeypatch, stub_preflight):
    complete_setup(tmp_path, monkeypatch)
    calls, _ = stub_preflight
    monkeypatch.setenv("JOBHUNTER_CONTAINER", "1")
    result = run_doctor("--containers")
    assert result.exit_code == 2
    assert calls == []


def test_every_preflight_add_call_sets_a_known_id():
    tree = ast.parse(Path(preflight.__file__).read_text(encoding="utf-8"))
    seen = set()
    count = 0
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "res"
        ):
            count += 1
            ids = [k.value.value for k in node.keywords if k.arg == "id"]
            assert len(ids) == 1, ast.get_source_segment(Path(preflight.__file__).read_text(), node)
            seen.add(ids[0])
    assert count > 20
    assert seen == set(preflight.PREFLIGHT_IDS)


def test_build_preflight_context_is_a_dry_helper(tmp_path, monkeypatch):
    complete_setup(tmp_path, monkeypatch)
    before = digest(tmp_path)
    ctx = cli.build_preflight_context()
    assert isinstance(ctx, preflight.Context)
    assert digest(tmp_path) == before
    assert not (Path.home() / preflight.CONFIG_SOURCE).exists()  # no file created for the mount


# --- refusal messages point at doctor ----------------------------------------------------------


def test_console_503_and_cli_schema_refusal_point_at_doctor(tmp_path, monkeypatch):
    from jobhunter.config import Paths, Settings

    path = tmp_path / "t.db"
    settings = Settings(paths=Paths(profile_dir=tmp_path / "p", db_path=path))
    n = {"n": 0}

    def factory():
        n["n"] += 1
        if n["n"] > 1:
            raise db.SchemaError("database is newer than this jobhunter")
        return db.connect(path)

    client = TestClient(create_app(settings, factory))
    response = client.get("/healthz")
    assert response.status_code == 503
    assert "jobhunter doctor" in response.text
    monkeypatch.setenv("JOBHUNTER_CONTAINER", "1")
    assert container.doctor_command() == "jobhunter-ctr doctor"


def test_cli_schema_refusal_points_at_doctor(tmp_path, monkeypatch):
    settings = complete_setup(tmp_path, monkeypatch)
    raw = sqlite3.connect(settings.paths.db_path)
    raw.execute("INSERT INTO schema_version VALUES (9999, 'future', 't')")
    raw.commit()
    raw.close()
    result = runner.invoke(app, ["apply", "stats"])
    assert result.exit_code == 1
    assert "Run `jobhunter doctor` for details." in result.output


# --- no value leaks ----------------------------------------------------------------------------


def test_no_secret_contact_or_address_in_any_output(tmp_path, monkeypatch, home_dir):
    settings, env = sentinel_setup(tmp_path, home_dir)
    cfg = tmp_path / "config.toml"
    data = Path(settings.paths.data_dir)
    cfg.write_text(
        f'[paths]\ndata_dir = "{data}"\ndb_path = "{data / "jobhunter.db"}"\n'
        f'profile_dir = "{data / "profile"}"\nresume_path = "{data / "resume"}"\n'
        f'[fetch]\nuser_agent_contact = "{SENTINEL}-contact@example.org"\n'
        f'[mail]\nalerts_address = "{SENTINEL}+jobs@example.org"\n'
        '[scoring.openrouter]\napi_key_env = "MY_CUSTOM_KEY"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("JOBHUNTER_CONFIG", str(cfg))
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    outputs = []
    for args in ([], ["-v"], ["--json"], ["--group", "email alerts"], ["--group", "scoring", "-v"]):
        result = run_doctor(*args)
        assert result.exit_code in (0, 1), result.output
        outputs.append(result.output)
    joined = "\n".join(outputs)
    assert joined.count("Email alerts") > 0
    assert "set (env)" in joined  # the secrets were in play
    assert SENTINEL not in joined
    assert "jobs@example.org" not in joined
    assert "daniel" not in joined.lower()


@pytest.fixture
def home_dir() -> Path:
    import os

    return Path(os.environ["HOME"])


def test_containers_with_a_broken_config_does_not_traceback_or_echo_values(
    tmp_path, monkeypatch, no_key
):
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'[fetch]\nuser_agent_contact = "{SENTINEL}@example.org"\nbogus = 1\n')
    monkeypatch.setenv("JOBHUNTER_CONFIG", str(cfg))
    for args in (["--containers"], ["--containers", "--json"], ["--containers", "-v"]):
        result = run_doctor(*args)  # the real build_preflight_context, no stub
        assert result.exit_code == 1, result.output
        assert "Traceback" not in result.output
        assert SENTINEL not in result.output
        assert result.exception is None or isinstance(result.exception, SystemExit)
    rows = {r["id"]: r for r in json.loads(run_doctor("--containers", "--json").stdout)}
    assert "not run" in rows["preflight.preflight_other"]["detail"]


def test_a_failing_context_builder_reports_the_exception_type_only(
    tmp_path, monkeypatch, stub_preflight
):
    complete_setup(tmp_path, monkeypatch)

    def boom(*a, **k):
        raise ValueError(f"input_value={SENTINEL}")

    monkeypatch.setattr(cli, "build_preflight_context", boom)
    result = run_doctor("--containers", "--json")
    assert SENTINEL not in result.output and "Traceback" not in result.output
    assert "ValueError" in result.stdout


def test_summary_names_the_ctr_command_in_container_mode(tmp_path, monkeypatch):
    complete_setup(tmp_path, monkeypatch)
    monkeypatch.setenv("JOBHUNTER_CONTAINER", "1")
    assert "jobhunter-ctr doctor -v" in run_doctor().output.splitlines()[-1]
