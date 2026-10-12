"""Console Restart (specs/019 Addendum B "Restart", S12b): the work registry, the route's gate,
the child preflight, re-exec and exit 42, and the unit lines.

Nothing here restarts a real console: ``os.execve``, ``os._exit``, ``logging.shutdown``,
uvicorn's server and the preflight child are stubbed, and the child's code runs in-process.
"""

from __future__ import annotations

import io
import subprocess
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from jobhunter import cli, secrets
from jobhunter import config_watch as cw
from jobhunter.config import load_settings
from jobhunter.console import config_routes
from jobhunter.console import restart as rs
from jobhunter.console.app import create_app
from jobhunter.console.work import Restarting, WorkRegistry
from jobhunter.core import db
from jobhunter.ops import preflight

NOW = datetime(2026, 10, 11, 14, 5, tzinfo=UTC)
SENTINEL = "zqxSENTINELqxz"
LOOPBACK = ("127.0.0.1", 50000)
LAN = ("192.168.1.20", 50000)
QUADLET = Path(__file__).resolve().parents[2] / "deploy" / "quadlet"


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


class Console:
    """A watched console on a temp config, with Restart's outside world stubbed."""

    def __init__(self, tmp: Path, *, allow_remote: bool = False, can_restart: bool = True):
        self.tmp = tmp
        self.cfg = tmp / "config.toml"
        self.base = (
            f'[paths]\ndata_dir = "{tmp / "data"}"\ncache_dir = "{tmp / "cache"}"\n'
            f'profile_dir = "{tmp / "profile"}"\n'
        )
        self.cfg.write_text(self.base)
        (tmp / "profile").mkdir()
        running = load_settings(self.cfg)
        db_path = running.paths.db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = db.connect(db_path)
        db.migrate(conn)
        conn.close()
        self.clock = Clock()
        self.app = create_app(
            running, lambda: db.connect(db_path), clock=lambda: NOW, allow_remote=allow_remote
        )
        self.app.state.setup_mode = "host"
        self.app.state.config_watch = cw.ConfigWatcher(
            config_path=lambda: self.cfg,
            env_candidates=lambda: [],
            clock=self.clock,
            container_mode=False,
            start_env={},
        )
        self.stopped: list[bool] = []
        self.checks: list[bool] = []
        self.check_lines: list[str] = []
        if can_restart:
            self.app.state.restart_shutdown = lambda: self.stopped.append(True)
            self.app.state.restart_kind = rs.EXEC

        self.require: list[bool] = []

        def check(*, require_settings: bool = False) -> list[str]:
            self.checks.append(True)
            self.require.append(require_settings)
            return list(self.check_lines)

        self.app.state.restart_preflight = check
        self.app.state.restart_delay = 0
        self.busy_ports: set[int] = set()
        self.app.state.port_free = lambda host, port: port not in self.busy_ports

    def client(self, peer: tuple[str, int] = LOOPBACK) -> TestClient:
        return TestClient(self.app, client=peer, follow_redirects=False)

    def restart(self, data: dict | None = None) -> tuple[Any, Any]:
        """POST Restart; when accepted (303), also GET the waiting page it points at."""
        r = self.client().post("/config/restart", data=data or {})
        if r.status_code != 303:
            return r, None
        assert r.headers["location"].startswith("/config/restarting?")
        return r, self.client().get(r.headers["location"])

    def write(self, extra: str) -> None:
        self.cfg.write_text(self.base + extra)
        self.clock.t += 3


# --- the registry ----------------------------------------------------------------------------


def test_registry_refuses_a_restart_while_work_runs_and_names_it():
    work = WorkRegistry()
    token = work.enter("a re-score")
    with work.running("a draft (packet 3)"):
        assert work.begin_restart() == ["a re-score", "a draft (packet 3)"]
    assert not work.restarting
    work.leave(token)
    assert work.begin_restart() == [] and work.restarting
    with pytest.raises(Restarting, match="console restarting"):
        work.enter("a Score now (group 1)")
    assert work.labels() == []


def test_registry_leaves_on_an_exception():
    work = WorkRegistry()
    with pytest.raises(RuntimeError), work.running("a draft"):
        raise RuntimeError("boom")
    assert work.labels() == []


def test_wait_idle_joins_work_that_ends_and_gives_up_on_work_that_does_not():
    work = WorkRegistry()
    token = work.enter("a re-score")
    threading.Timer(0.05, work.leave, args=(token,)).start()
    assert work.wait_idle(5) == []
    work.enter("stuck")
    assert work.wait_idle(0.05) == ["stuck"]  # bounded


def test_registry_check_and_flag_are_one_step():
    """No work can slip in between the check and the flag: enter and begin_restart share a
    lock, so of many racing threads either the restart is refused or every enter fails."""

    def worker(work, barrier, entered, refused) -> None:
        barrier.wait()
        try:
            entered.append(work.enter("a Score now"))
        except Restarting:
            refused.append(True)

    for _ in range(50):
        work = WorkRegistry()
        entered: list[int] = []
        refused: list[bool] = []
        barrier = threading.Barrier(5)
        threads = [
            threading.Thread(target=worker, args=(work, barrier, entered, refused))
            for _ in range(4)
        ]
        for t in threads:
            t.start()
        barrier.wait()
        accepted = work.begin_restart() == []
        for t in threads:
            t.join()
        if accepted:  # nothing entered before the flag, and every later enter was refused
            assert work.restarting and not entered and len(refused) == 4
        else:  # something entered first, so the restart was refused and nobody was
            assert entered and not refused and not work.restarting


# --- the route -------------------------------------------------------------------------------


def test_restart_is_accepted_and_stops_the_server_after_the_response(tmp_path):
    console = Console(tmp_path)
    r, page = console.restart({"next": "/inbox"})
    assert r.status_code == 303  # a GET page: reloading it never posts again
    assert console.checks == [True]  # the child check ran first
    assert console.require == [True]  # the running file had settings
    assert console.stopped == [True]  # the background task, after the response
    assert console.app.state.work.restarting
    assert page.status_code == 200 and 'id="restart-wait"' in page.text
    assert f'data-boot="{console.app.state.boot_id}"' in page.text
    assert 'data-next="/inbox"' in page.text and 'data-same="1"' in page.text
    assert f'data-giveup="{rs.GIVE_UP_S}"' in page.text
    # The waiting page has no bar: the old console's Apply/Restart buttons must not show.
    assert 'id="activity"' not in page.text and 'action="/config/restart"' not in page.text
    # Reloading the waiting page is a GET: nothing is stopped twice.
    assert console.client().get(r.headers["location"]).status_code == 200
    assert console.stopped == [True]
    # A second press while it stops: refused, nothing stopped twice.
    again = console.client().post("/config/restart")
    assert again.status_code == 409 and console.stopped == [True]


def test_paid_entry_points_get_503_once_a_restart_is_accepted(tmp_path):
    console = Console(tmp_path)
    assert console.client().post("/config/restart").status_code == 303
    with pytest.raises(Restarting):
        console.app.state.work.enter("a re-score")


@pytest.mark.parametrize("allow_remote", [False, True])
def test_a_non_loopback_peer_gets_403(tmp_path, allow_remote):
    console = Console(tmp_path, allow_remote=allow_remote)
    r = console.client(LAN).post("/config/restart")
    assert r.status_code == 403 and "only from the machine running the console" in r.text
    assert console.stopped == [] and console.checks == []
    assert not console.app.state.work.restarting


def test_a_console_that_cannot_restart_itself_says_how(tmp_path):
    console = Console(tmp_path, can_restart=False)
    r = console.client().post("/config/restart")
    assert r.status_code == 409 and "cannot restart itself" in r.text
    assert "Ctrl-C and jobhunter console" in r.text
    assert not console.app.state.work.restarting and console.checks == []


def test_a_failing_check_keeps_the_console_running(tmp_path):
    console = Console(tmp_path)
    console.check_lines = ["scoring.daily_cap_usd: Input should be a valid number"]
    r = console.client().post("/config/restart")
    assert r.status_code == 409
    assert "scoring.daily_cap_usd: Input should be a valid number" in r.text
    assert "keeps running" in r.text
    assert console.stopped == [] and not console.app.state.work.restarting


def test_registered_work_gives_409_listing_it_and_the_button_stays(tmp_path):
    console = Console(tmp_path)
    console.write("[console]\nport = 8810\n")
    token = console.app.state.work.enter("a draft (packet 7)")
    r = console.client().post("/config/restart")
    assert r.status_code == 409 and "a draft (packet 7)" in r.text
    assert "restart when it finishes" in r.text
    assert console.stopped == [] and not console.app.state.work.restarting
    bar = console.client().get("/activity").text
    assert 'action="/config/restart"' in bar
    console.app.state.work.leave(token)
    assert console.client().post("/config/restart").status_code == 303


def test_a_replaced_file_in_a_container_asks_first_and_skips_the_check(tmp_path, monkeypatch):
    console = Console(tmp_path)
    monkeypatch.setenv("JOBHUNTER_CONTAINER", "1")
    monkeypatch.setattr(
        config_routes, "current_state", lambda request, force=False: cw.State(replaced=True)
    )
    r = console.client().post("/config/restart")
    assert r.status_code == 409 and "Restart without checking" in r.text
    assert "jobhunter-ctr doctor" in r.text
    assert console.checks == [] and console.stopped == []
    r = console.client().post("/config/restart", data={"unchecked": "1"})
    assert r.status_code == 303 and console.checks == [] and console.stopped == [True]


def test_a_port_change_polls_the_new_address(tmp_path):
    console = Console(tmp_path)
    console.write("[console]\nport = 8810\n")
    _, page = console.restart()
    assert 'data-base="http://127.0.0.1:8810"' in page.text and 'data-same="0"' in page.text


def test_next_is_a_local_path_only(tmp_path):
    console = Console(tmp_path)
    for bad in ("https://evil.example/", "//evil.example/x", "javascript:alert(1)"):
        _, page = console.restart({"next": bad})
        assert 'data-next="/"' in page.text
        console.app.state.work.cancel_restart()


def test_the_waiting_page_polls_only_a_loopback_address(tmp_path):
    console = Console(tmp_path)
    for bad in ("https://evil.example:8810", "http://127.0.0.1:8810/x", "javascript:x"):
        page = console.client().get("/config/restarting", params={"base": bad, "boot": "ab"})
        assert 'data-base="http://127.0.0.1:8808"' in page.text  # this console's own
    page = console.client().get("/config/restarting", params={"boot": "<script>"})
    assert 'data-boot=""' in page.text


def test_healthz_carries_the_boot_id(tmp_path):
    console = Console(tmp_path)
    r = console.client().get("/healthz")
    assert r.headers["X-Jobhunter-Boot"] == console.app.state.boot_id
    assert set(r.json()) == {"ok", "schema_version"}


# --- the bar, Details and /setup -------------------------------------------------------------


def test_bar_offers_restart_for_a_restart_change(tmp_path):
    console = Console(tmp_path)
    console.write("[console]\nport = 8810\n")
    bar = console.client().get("/activity", headers={"HX-Current-URL": "http://x/inbox?y=1"}).text
    assert 'action="/config/restart"' in bar and "Restart console" in bar
    assert 'name="next" value="/inbox"' in bar
    assert "Ctrl-C" not in bar  # the button replaces the command
    page = console.client().get("/config/changes").text
    assert 'id="cfg-restart"' in page and "Restart console" in page


def test_bar_has_no_restart_for_a_reload_only_change(tmp_path):
    console = Console(tmp_path)
    console.write("[scoring]\ndaily_cap_usd = 9.0\n")
    bar = console.client().get("/activity").text
    assert "Apply now" in bar and 'action="/config/restart"' not in bar


def test_lan_browser_and_a_console_that_cannot_restart_get_the_command(tmp_path):
    console = Console(tmp_path, allow_remote=True)
    console.write("[console]\nport = 8810\n")
    bar = console.client(LAN).get("/activity").text
    assert 'action="/config/restart"' not in bar and "Ctrl-C and jobhunter console" in bar
    (tmp_path / "b").mkdir()
    other = Console(tmp_path / "b", can_restart=False)
    other.write("[console]\nport = 8810\n")
    bar = other.client().get("/activity").text
    assert 'action="/config/restart"' not in bar and "Ctrl-C and jobhunter console" in bar


def test_details_names_running_work_and_a_code_change(tmp_path, monkeypatch):
    from jobhunter.ops import version

    console = Console(tmp_path)
    console.write("[console]\nport = 8810\n")
    console.app.state.work.enter("a re-score")
    console.app.state.started_commit = "a" * 40
    monkeypatch.setattr(version, "build_commit", lambda env=None: ("b" * 40, "git"))
    page = console.client().get("/config/changes").text
    assert "Running now: a re-score." in page
    assert "differs from the running console" in page and "uv sync" in page


def test_setup_restart_notes_point_at_the_button(tmp_path):
    console = Console(tmp_path)
    page = console.client().get("/setup?all=1").text
    assert "else Restart, on" in page and 'href="/config/changes"' in page
    other_dir = tmp_path / "b"
    other_dir.mkdir()
    other = Console(other_dir, can_restart=False)
    page = other.client().get("/setup?all=1").text
    assert "else restart: <code>" in page


# --- the child preflight ---------------------------------------------------------------------


def run_child_code(
    monkeypatch,
    tmp_path: Path,
    config_text: str | None,
    *,
    require: bool = False,
    dotenv: str | None = None,
    db_path: Path | None = None,
) -> tuple[int, str]:
    """PREFLIGHT_CODE run in-process (no subprocess) against a temp config. ``config_text``
    None: no file. ``dotenv``: the .env file's text (it may point JOBHUNTER_CONFIG away)."""
    home = tmp_path / "home"
    home.mkdir()
    cfg = tmp_path / "child-config.toml"
    if config_text is not None:
        cfg.write_text(config_text)
    # Registered with monkeypatch first, so whatever the .env loads is removed afterwards.
    for name in ("JOBHUNTER_CONFIG", "JOBHUNTER_DB_PATH"):
        monkeypatch.setenv(name, "x")
        monkeypatch.delenv(name)
    if dotenv is None:
        monkeypatch.setenv("JOBHUNTER_CONFIG", str(cfg))
    envf = tmp_path / "child.env"
    if dotenv is not None:
        envf.write_text(dotenv)
    monkeypatch.setenv("JOBHUNTER_ENV_FILE", str(envf))
    if db_path is not None:
        monkeypatch.setenv("JOBHUNTER_DB_PATH", str(db_path))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(secrets, "LAST_REPORT", secrets.LAST_REPORT)
    monkeypatch.setattr(sys, "argv", ["-c", *([rs.REQUIRE_SETTINGS_ARG] if require else [])])
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    code = 0
    try:
        exec(compile(rs.PREFLIGHT_CODE, "<preflight>", "exec"), {"__name__": "__main__"})
    except SystemExit as exc:
        code = int(exc.code or 0)
    monkeypatch.setattr(sys, "stdout", sys.__stdout__)
    return code, out.getvalue()


def fake_run(code: int, stdout: str, stderr: str = ""):
    calls: list[dict] = []

    def run(argv, **kw):
        calls.append({"argv": argv, **kw})
        return subprocess.CompletedProcess(argv, code, stdout, stderr)

    return run, calls


def test_child_code_accepts_a_good_config(monkeypatch, tmp_path):
    code, out = run_child_code(monkeypatch, tmp_path, "[scoring]\ndaily_cap_usd = 3.0\n")
    assert code == 0
    run, calls = fake_run(code, out)
    assert rs.run_preflight({"PATH": "/usr/bin"}, tmp_path, run=run) == []
    assert calls[0]["argv"] == [sys.executable, "-c", rs.PREFLIGHT_CODE]
    assert calls[0]["env"] == {"PATH": "/usr/bin"} and calls[0]["timeout"] == 60
    assert calls[0]["cwd"] == str(tmp_path)


@pytest.mark.parametrize(
    "bad",
    [
        f'[scoring]\ndaily_cap_usd = "{SENTINEL}"\n',
        f"[scoring]\ndaily_cap_usd = {SENTINEL}\n",  # not valid TOML
        f'[fetch]\nuser_agent_contact = "{SENTINEL}"\nnot_a_key = 1\n',
    ],
)
def test_a_failing_child_gives_409_with_no_value(monkeypatch, tmp_path, bad):
    code, out = run_child_code(monkeypatch, tmp_path, bad)
    assert code == 1 and SENTINEL not in out
    # stderr (a traceback could quote anything) is never read into the answer.
    run, _ = fake_run(code, out, stderr=f"Traceback ... {SENTINEL}")
    lines = rs.run_preflight({}, tmp_path, run=run)
    assert lines and SENTINEL not in " ".join(lines)
    (tmp_path / "c").mkdir()
    console = Console(tmp_path / "c")
    console.app.state.restart_preflight = lambda **kw: rs.run_preflight({}, tmp_path, run=run)
    r = console.client().post("/config/restart")
    assert r.status_code == 409 and SENTINEL not in r.text and console.stopped == []


def test_child_output_that_is_not_ours_is_a_generic_failure(tmp_path):
    run, _ = fake_run(1, f"something printed {SENTINEL}\n")
    assert rs.run_preflight({}, tmp_path, run=run) == ["the check failed (exit 1)"]
    run, _ = fake_run(0, "")
    assert rs.run_preflight({}, tmp_path, run=run) == ["the check failed (exit 0)"]

    def slow(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 60)

    assert rs.run_preflight({}, tmp_path, run=slow) == ["the check did not finish in 60 s"]


def test_a_missing_module_names_uv_sync(monkeypatch, tmp_path):
    import builtins

    real_import = builtins.__import__

    def no_console(name, *args, **kwargs):
        if name == "jobhunter.console.app":
            raise ModuleNotFoundError("No module named 'newdep'", name="newdep")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_console)
    code, out = run_child_code(monkeypatch, tmp_path, "")
    run, _ = fake_run(code, out)
    assert rs.run_preflight({}, tmp_path, run=run) == ["missing module newdep; run uv sync"]


# --- after the server stops: re-exec or exit 42 ----------------------------------------------


def test_host_re_execs_with_the_same_argv_and_the_pristine_env(monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(rs.logging, "shutdown", lambda *a: None)
    monkeypatch.setattr(sys, "orig_argv", ["/usr/bin/python3", "/venv/bin/jobhunter", "console"])
    monkeypatch.setattr(rs, "PRISTINE_ENV", {"HOME": "/home/x", "PATH": "/usr/bin"})
    monkeypatch.setenv("ONLY_FROM_DOTENV", "loaded-later")
    rs.after_serve(
        how=rs.EXEC,
        leftover=lambda t: [],
        drain=1,
        execve=lambda *a: calls.append(a),
        exit_=lambda c: pytest.fail("no exit on the host"),
    )
    (path, argv, env) = calls[0]
    assert path == sys.executable
    assert argv == [sys.executable, "/venv/bin/jobhunter", "console"]
    assert env == {"HOME": "/home/x", "PATH": "/usr/bin"} and "ONLY_FROM_DOTENV" not in env


def test_unit_mode_exits_42(monkeypatch):
    codes: list[int] = []
    joined: list[float] = []
    monkeypatch.setattr(rs.logging, "shutdown", lambda *a: None)
    rs.after_serve(
        how=rs.EXIT,
        leftover=lambda t: joined.append(t) or [],
        drain=30,
        execve=lambda *a: pytest.fail("no re-exec in a container"),
        exit_=codes.append,
    )
    assert codes == [42] == [rs.EXIT_RESTART] and joined == [30]


def test_kind_by_mode(monkeypatch):
    monkeypatch.delenv("JOBHUNTER_CONTAINER", raising=False)
    monkeypatch.delenv(rs.RESTART_EXIT_VAR, raising=False)
    assert rs.kind() == rs.EXEC and rs.drain_seconds() == 200
    monkeypatch.setenv("JOBHUNTER_CONTAINER", "1")
    assert rs.kind() is None and rs.drain_seconds() == 30  # unit not opted in: no button
    monkeypatch.setenv(rs.RESTART_EXIT_VAR, "42")
    assert rs.kind() == rs.EXIT


class FakeServer:
    """uvicorn.Server stand-in: ``press`` makes a request to /config/restart's shutdown."""

    def __init__(self, config, *, started: bool = True, press: bool = True):
        self.config = config
        self.started = False
        self._started = started
        self._press = press
        self.should_exit = False

    def run(self):
        self.started = self._started
        if self._press and self.config.app.state.restart_shutdown is not None:
            self.config.app.state.restart_shutdown()


def fake_uvicorn(**server_kw):
    def config(app, **kw):
        return SimpleNamespace(app=app, **kw)

    return SimpleNamespace(Config=config, Server=lambda c: FakeServer(c, **server_kw))


def fake_app():
    return SimpleNamespace(
        state=SimpleNamespace(
            work=WorkRegistry(),
            restart_requested=False,
            restart_shutdown=None,
            restart_kind=None,
            started_commit=None,
        )
    )


@pytest.fixture
def stubbed_exits(monkeypatch):
    from jobhunter.ops import version

    seen: dict[str, list] = {"exit": [], "exec": []}
    monkeypatch.setattr(rs.os, "_exit", lambda code: seen["exit"].append(code))
    monkeypatch.setattr(rs.os, "execve", lambda *a: seen["exec"].append(a))
    monkeypatch.setattr(rs.logging, "shutdown", lambda *a: None)
    monkeypatch.setattr(version, "build_commit", lambda env=None: ("c" * 40, "git"))
    return seen


def test_serve_in_an_opted_in_container_exits_42(monkeypatch, stubbed_exits):
    monkeypatch.setenv("JOBHUNTER_CONTAINER", "1")
    monkeypatch.setenv(rs.RESTART_EXIT_VAR, "42")
    app_ = fake_app()
    cli._serve(app_, "127.0.0.1", 8808, fake_uvicorn())
    assert app_.state.restart_requested and stubbed_exits == {"exit": [42], "exec": []}


def test_serve_in_a_container_without_the_unit_lines_has_no_restart(monkeypatch, stubbed_exits):
    monkeypatch.setenv("JOBHUNTER_CONTAINER", "1")
    monkeypatch.delenv(rs.RESTART_EXIT_VAR, raising=False)
    app_ = fake_app()
    cli._serve(app_, "127.0.0.1", 8808, fake_uvicorn())
    assert app_.state.restart_shutdown is None and app_.state.restart_kind is None
    assert stubbed_exits == {"exit": [], "exec": []}


def test_a_server_that_never_started_does_not_exit_42(monkeypatch, stubbed_exits):
    import typer

    monkeypatch.setenv("JOBHUNTER_CONTAINER", "1")
    monkeypatch.setenv(rs.RESTART_EXIT_VAR, "42")
    app_ = fake_app()
    with pytest.raises(typer.Exit) as exc:
        cli._serve(app_, "127.0.0.1", 8808, fake_uvicorn(started=False))
    assert exc.value.exit_code == 3 != rs.EXIT_RESTART
    assert stubbed_exits["exit"] == []


def test_serve_without_a_restart_just_returns(monkeypatch, stubbed_exits):
    monkeypatch.delenv("JOBHUNTER_CONTAINER", raising=False)
    app_ = fake_app()
    cli._serve(app_, "127.0.0.1", 8808, fake_uvicorn(press=False))
    assert stubbed_exits == {"exit": [], "exec": []}


def test_the_drain_is_capped_in_containers(monkeypatch, stubbed_exits):
    seen: list[dict] = []

    def config(app, **kw):
        seen.append(kw)
        return SimpleNamespace(app=app, **kw)

    uv = SimpleNamespace(Config=config, Server=lambda c: FakeServer(c, press=False))
    monkeypatch.delenv("JOBHUNTER_CONTAINER", raising=False)
    cli._serve(fake_app(), "127.0.0.1", 8808, uv)
    monkeypatch.setenv("JOBHUNTER_CONTAINER", "1")
    cli._serve(fake_app(), "127.0.0.1", 8808, uv)
    assert [k["timeout_graceful_shutdown"] for k in seen] == [200, 30]


def test_console_command_re_execs_without_the_env_file_values(monkeypatch, tmp_path, stubbed_exits):
    """End to end through the CLI callback: the snapshot is taken before .env is loaded."""
    import uvicorn

    probe = "JOBHUNTER_S12B_PROBE_ONLY_IN_DOTENV"
    monkeypatch.setenv(probe, "x")
    monkeypatch.delenv(probe)  # teardown removes it again after .env loads it
    dotenv = tmp_path / "test.env"
    dotenv.write_text(f"{probe}=from-the-file\n")
    monkeypatch.setenv("JOBHUNTER_ENV_FILE", str(dotenv))
    monkeypatch.setenv("JOBHUNTER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("JOBHUNTER_CONTAINER", raising=False)
    monkeypatch.setattr(uvicorn, "Server", lambda c: FakeServer(c))
    monkeypatch.setattr(sys, "orig_argv", [sys.executable, "/venv/bin/jobhunter", "console"])
    result = CliRunner().invoke(cli.app, ["console"])
    assert result.exit_code == 0, result.output
    import os

    assert os.environ.get(probe) == "from-the-file"  # this process loaded it
    (path, argv, env) = stubbed_exits["exec"][0]
    assert path == sys.executable and argv == [sys.executable, "/venv/bin/jobhunter", "console"]
    assert probe not in env  # the new process reads .env afresh
    assert env["JOBHUNTER_ENV_FILE"] == str(dotenv)


# --- the unit lines --------------------------------------------------------------------------


def unit_sections(text: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    current = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1]
            sections.setdefault(current, [])
        else:
            sections.setdefault(current, []).append(line)
    return sections


def test_base_console_unit_has_the_restart_lines():
    u = unit_sections((QUADLET / "jobhunter-console.container").read_text())
    assert "Environment=JOBHUNTER_RESTART_EXIT=42" in u["Container"]
    assert "SuccessExitStatus=42" in u["Service"] and "RestartForceExitStatus=42" in u["Service"]
    assert "Restart=on-failure" in u["Service"]
    for other in preflight.UNITS:
        if other != preflight.CONSOLE:
            assert "RESTART_EXIT" not in (QUADLET / f"{other}.container").read_text()


def test_preflight_writes_the_restart_dropin_with_the_same_sections(tmp_path):
    units = tmp_path / "systemd"
    plan = preflight.plan_dropins(units, [], [])
    path = preflight.restart_dropin_path(units)
    assert path.name == "45-jobhunter-restart.conf"
    assert path.parent.name == "jobhunter-console.container.d"
    assert plan.write[path] == preflight.restart_dropin_text()
    u = unit_sections(plan.write[path])
    base = unit_sections((QUADLET / "jobhunter-console.container").read_text())
    assert u["Container"] == ["Environment=JOBHUNTER_RESTART_EXIT=42"]
    assert u["Service"] == ["SuccessExitStatus=42", "RestartForceExitStatus=42"]
    assert set(u["Container"]) <= set(base["Container"])
    assert set(u["Service"]) <= set(base["Service"])
    preflight.apply_dropins(plan)
    again = preflight.plan_dropins(units, [], [])
    assert path in again.keep and path not in again.remove  # kept, never pruned


# --- B1: never boot on the defaults -----------------------------------------------------------


@pytest.mark.parametrize("text", ["", "# only a comment\n", None])
def test_the_child_refuses_an_empty_or_missing_file_when_the_running_one_had_settings(
    monkeypatch, tmp_path, text
):
    code, out = run_child_code(monkeypatch, tmp_path, text, require=True)
    run, _ = fake_run(code, out)
    lines = rs.run_preflight({}, tmp_path, run=run, require_settings=True)
    assert code == 1 and lines and ("missing" in lines[0] or "empty" in lines[0])


def test_without_settings_at_start_an_empty_file_is_fine(monkeypatch, tmp_path):
    code, _ = run_child_code(monkeypatch, tmp_path, "", require=False)
    assert code == 0


def test_a_dotenv_that_points_the_config_at_a_missing_file_is_refused(monkeypatch, tmp_path):
    missing = tmp_path / "moved" / "config.toml"
    code, out = run_child_code(
        monkeypatch, tmp_path, "[scoring]\ndaily_cap_usd = 3.0\n",
        require=True, dotenv=f"JOBHUNTER_CONFIG={missing}\n",
    )  # fmt: skip
    assert code == 1 and "check JOBHUNTER_CONFIG" in out and str(missing) not in out


def test_an_unusable_database_path_is_refused(monkeypatch, tmp_path):
    folder = tmp_path / "db-is-a-folder"
    folder.mkdir()
    code, out = run_child_code(monkeypatch, tmp_path, "", db_path=folder)
    assert code == 1 and "paths.db_path is not a file" in out and str(folder) not in out


def test_the_route_refuses_while_config_toml_is_empty_or_missing(tmp_path):
    console = Console(tmp_path)
    console.write("[scoring]\ndaily_cap_usd = 9.0\n")
    console.cfg.write_text("")
    console.clock.t += 3
    r, _ = console.restart()
    assert r.status_code == 409 and "config.toml is empty" in r.text
    assert console.checks == [] and console.stopped == []
    assert 'action="/config/restart"' not in console.client().get("/activity").text
    console.cfg.unlink()
    console.clock.t += 3
    r, _ = console.restart()
    assert r.status_code == 409 and "config.toml is missing" in r.text
    assert console.stopped == [] and not console.app.state.work.restarting


def test_the_bar_hides_restart_while_the_file_has_errors(tmp_path):
    console = Console(tmp_path)
    console.write('[console]\nport = 8810\n[scoring]\ndaily_cap_usd = "x"\n')
    bar = console.client().get("/activity").text
    assert "config.toml has errors" in bar and 'action="/config/restart"' not in bar
    r, _ = console.restart()
    assert r.status_code == 409 and "scoring.daily_cap_usd" in r.text


# --- N1: a moved host or port ----------------------------------------------------------------


def test_a_non_loopback_host_is_refused_without_allow_remote(tmp_path):
    console = Console(tmp_path)
    console.write('[console]\nhost = "192.0.2.7"\n')
    r, _ = console.restart()
    assert r.status_code == 409 and "console.host: not a loopback address" in r.text
    assert "192.0.2.7" not in r.text and console.stopped == []


def test_a_moved_port_already_in_use_is_refused(tmp_path):
    console = Console(tmp_path)
    console.busy_ports = {8810}
    console.write("[console]\nport = 8810\n")
    r, _ = console.restart()
    assert r.status_code == 409 and "console.port: that port is already in use" in r.text
    assert console.checks == [] and console.stopped == []


def test_port_free_sees_a_bound_port():
    import socket

    with socket.socket() as held:
        held.bind(("127.0.0.1", 0))
        held.listen()
        port = held.getsockname()[1]
        assert not rs.port_free("127.0.0.1", port)
    assert rs.port_free("127.0.0.1", port)


# --- N2, N3: Ctrl-C, the post-serve join, cancel ---------------------------------------------


def test_ctrl_c_during_an_accepted_restart_exits_instead(monkeypatch, stubbed_exits):
    monkeypatch.delenv("JOBHUNTER_CONTAINER", raising=False)

    class Interrupted(FakeServer):
        def run(self):
            super().run()  # the restart was accepted
            raise KeyboardInterrupt  # then Ctrl-C while it drained

    uv = SimpleNamespace(
        Config=lambda app, **kw: SimpleNamespace(app=app, **kw), Server=lambda c: Interrupted(c)
    )
    app_ = fake_app()
    cli._serve(app_, "127.0.0.1", 8808, uv)
    assert app_.state.restart_requested
    assert stubbed_exits == {"exit": [], "exec": []}


def test_the_post_serve_join_waits_for_work_before_re_exec(monkeypatch, stubbed_exits):
    monkeypatch.delenv("JOBHUNTER_CONTAINER", raising=False)
    app_ = fake_app()
    seen_at_exec: list[list[str]] = []
    monkeypatch.setattr(rs.os, "execve", lambda *a: seen_at_exec.append(app_.state.work.labels()))

    class Late(FakeServer):
        def run(self):
            token = app_.state.work.enter("a draft (packet 1)")  # left over at shutdown
            threading.Timer(0.1, app_.state.work.leave, args=(token,)).start()
            super().run()

    uv = SimpleNamespace(
        Config=lambda app, **kw: SimpleNamespace(app=app, **kw), Server=lambda c: Late(c)
    )
    cli._serve(app_, "127.0.0.1", 8808, uv)
    assert seen_at_exec == [[]]  # joined before the re-exec


def test_cancel_restart_lets_work_start_again():
    work = WorkRegistry()
    assert work.begin_restart() == []
    with pytest.raises(Restarting):
        work.enter("a re-score")
    work.cancel_restart()
    assert not work.restarting
    with work.running("a re-score"):
        assert work.labels() == ["a re-score"]


def test_a_failure_after_accepting_cancels_the_restart(tmp_path, monkeypatch):
    console = Console(tmp_path)

    def broken(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(config_routes, "urlencode", broken)
    with pytest.raises(RuntimeError):
        console.client().post("/config/restart")
    assert not console.app.state.work.restarting and console.stopped == []
    with console.app.state.work.running("a re-score"):
        pass


# --- P1 ------------------------------------------------------------------------------------


def test_startup_log_never_quotes_a_key_shaped_scorer_spec(tmp_path, caplog):
    from jobhunter.config import Paths, Scoring, Settings

    key = "sk-or-v1-" + "a" * 40
    settings = Settings(
        paths=Paths(data_dir=tmp_path / "d", cache_dir=tmp_path / "c", profile_dir=tmp_path),
        scoring=Scoring(screen_scorer=key),
    )
    with caplog.at_level("WARNING"):
        create_app(settings, lambda: db.connect(tmp_path / "x.db"), clock=lambda: NOW)
    text = caplog.text
    assert "scoring.screen_scorer" in text and key not in text and "sk-or" not in text
