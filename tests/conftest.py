"""Test-wide guards.

No test may reach the network: only loopback connections (the e2e server, mocked
transports) are allowed. A test that genuinely needs a real service must be marked
``live`` and is skipped unless JOBHUNTER_LIVE_TESTS=1 is set, so paid endpoints are
never called by an ordinary ``pytest`` run.

Why this exists (note for future maintainers, human or model): the scorers call paid
APIs (OpenRouter/TypeSafe Jev, Anthropic) on the user's own prepaid credits, and the
scrapers hit real government sites under polite rate limits. The user explicitly asked
that tests never spend credits unless deliberately run. Agents run `pytest` constantly,
so a single unmocked call in a test would burn money and hammer sites on every run.
Do not weaken, bypass or delete this guard; mock with httpx.MockTransport instead, or
mark a test `live` if it truly needs the real service.

User files: no test may read the developer's real profile, resume, data or config either.
Those files live in the repo (profile/, resume/, data/, config.toml, .env: all gitignored),
in the XDG locations ($XDG_CONFIG_HOME, $XDG_DATA_HOME and $XDG_CACHE_HOME below
``jobhunter``, by default ~/.config, ~/.local/share and ~/.cache) and in ~/.env. Code under
test resolves its defaults against the working directory and the home directory, so an
unisolated test silently reads them. That
is how console tests passed locally on the developer's real resume and failed in CI, where
no such file exists. Tests that read the real resume or profile both leaked personal data
into test behavior and made CI and local results diverge, which hid that CI failure
locally. The ``_isolate_user_files`` fixture points HOME at a fresh tmp dir, clears
JOBHUNTER_CONFIG and JOBHUNTER_ENV_FILE, and makes any open of those paths raise. Tests
needing settings files or a resume create them under tmp_path. Do not weaken this guard.
"""

from __future__ import annotations

import builtins
import io
import ipaddress
import os
import socket
import sqlite3
import subprocess
from pathlib import Path

import pytest

LIVE_ENV = "JOBHUNTER_LIVE_TESTS"
_LOOPBACK_NAMES = {"localhost", "localhost.localdomain", "ip6-localhost"}


def _is_loopback(host: object) -> bool:
    if not isinstance(host, str):
        return False
    if host in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class NetworkBlocked(RuntimeError):
    """Raised when a test tries to reach a non-loopback host."""


@pytest.fixture(autouse=True)
def _block_network(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    if request.node.get_closest_marker("live"):
        if os.environ.get(LIVE_ENV) != "1":
            pytest.skip(f"live test: set {LIVE_ENV}=1 and run with -m live")
        return

    real_connect = socket.socket.connect
    real_getaddrinfo = socket.getaddrinfo

    def guarded_connect(self: socket.socket, address: object) -> object:
        if self.family == socket.AF_UNIX:
            return real_connect(self, address)
        host = address[0] if isinstance(address, tuple) else address
        if not _is_loopback(host):
            raise NetworkBlocked(f"test tried to connect to {host!r}; mark it `live` if intended")
        return real_connect(self, address)

    def guarded_getaddrinfo(host: object, *args: object, **kwargs: object) -> object:
        if host is not None and not _is_loopback(host):
            raise NetworkBlocked(f"test tried to resolve {host!r}; mark it `live` if intended")
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)


# ─── User-file isolation ────────────────────────────────────────────────────

# Resolved at import time, before any test patches HOME.
_REPO = Path(__file__).resolve().parent.parent
_REAL_HOME = Path(os.path.realpath(os.path.expanduser("~")))
_USER_FILE_NAMES = ("profile", "resume", "data", "config.toml", ".env")


def _main_checkout() -> Path:
    """The primary checkout's root. A git worktree keeps its gitignored profile/, resume/
    and data/ in the main checkout, so those are protected as well."""
    try:
        common = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=_REPO,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return _REPO
    return Path(common).resolve().parent


def _real_xdg_dirs() -> set[str]:
    """The developer's real config, data and cache dirs: the defaults below the real home and
    wherever $XDG_*_HOME pointed at import time (before any fixture patches the environment)."""
    dirs: set[str] = set()
    for var, fallback in (
        ("XDG_CONFIG_HOME", ".config"),
        ("XDG_DATA_HOME", ".local/share"),
        ("XDG_CACHE_HOME", ".cache"),
    ):
        dirs.add(os.path.realpath(_REAL_HOME / fallback / "jobhunter"))
        value = os.environ.get(var, "")
        if value and os.path.isabs(value):
            dirs.add(os.path.realpath(os.path.join(value, "jobhunter")))
    return dirs


_PROTECTED = tuple(
    sorted(
        {
            os.path.realpath(root / name)
            for root in {_REPO, _main_checkout()}
            for name in _USER_FILE_NAMES
        }
        | {
            os.path.realpath(_REAL_HOME / ".env"),
        }
        | _real_xdg_dirs()
    )
)
_REAL_IO_OPEN = io.open
_REAL_SQLITE_CONNECT = sqlite3.connect


class UserFileAccess(RuntimeError):
    """Raised when a test opens one of the developer's real user files."""


def _is_protected(target: str) -> bool:
    real = os.path.realpath(target)
    return any(real == p or real.startswith(p + os.sep) for p in _PROTECTED)


def _guarded_open(file: object, *args: object, **kwargs: object) -> object:
    if isinstance(file, str | bytes | os.PathLike):
        target = os.fsdecode(file)
        if _is_protected(target):
            raise UserFileAccess(
                f"test tried to open the developer's real user file {target!r}; "
                "use a tmp_path copy instead (see tests/conftest.py)"
            )
    return _REAL_IO_OPEN(file, *args, **kwargs)  # type: ignore[call-overload]


def _guarded_sqlite_connect(database: object, *args: object, **kwargs: object) -> object:
    # sqlite opens files in C, bypassing io.open, so the real database needs its own check.
    if isinstance(database, str | bytes | os.PathLike):
        target = os.fsdecode(database)
        if target.startswith("file:"):
            target = target[len("file:") :].split("?", 1)[0]
        if target and target != ":memory:" and _is_protected(target):
            raise UserFileAccess(
                f"test tried to open the developer's real user file {target!r}; "
                "use a tmp_path copy instead (see tests/conftest.py)"
            )
    return _REAL_SQLITE_CONNECT(database, *args, **kwargs)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _isolate_user_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    # A fresh dir outside tmp_path, so it cannot collide with a test's own tmp_path/"home".
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    # Old-layout data (./data/jobhunter.db) is looked for in the cwd and in the main checkout
    # of the repository the package runs from, which is the developer's real one. A test must
    # see neither: commands would stop on the real database, or tests would depend on it.
    monkeypatch.chdir(home)
    monkeypatch.setattr("jobhunter.legacy_data.main_checkout", lambda: None)
    monkeypatch.delenv("JOBHUNTER_CONFIG", raising=False)
    monkeypatch.delenv("JOBHUNTER_ENV_FILE", raising=False)
    # Defaults resolve below HOME (a tmp dir) unless a test sets these on purpose.
    for var in (
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
        "JOBHUNTER_DATA_DIR",
        "JOBHUNTER_CACHE_DIR",
        "JOBHUNTER_DB_PATH",
        "JOBHUNTER_PROFILE_DIR",
        "JOBHUNTER_RESUME_PATH",
        "JOBHUNTER_CONTAINER",
        "JOBHUNTER_PUBLISHED_LOOPBACK_ONLY",
        "JOBHUNTER_GMAIL_TOKEN_FILE",
    ):
        monkeypatch.delenv(var, raising=False)
    # Path.open, read_text and read_bytes all reach io.open; builtins.open is a separate
    # name for the same function, so both are patched.
    monkeypatch.setattr(io, "open", _guarded_open)
    monkeypatch.setattr(builtins, "open", _guarded_open)
    monkeypatch.setattr(sqlite3, "connect", _guarded_sqlite_connect)


def build_old_layout(root: Path) -> Path:
    """A synthetic checkout in the pre-XDG layout, shaped like a real one: a WAL database with
    the full schema and some rows, data/backups, data/cache, a stray file in data/, profile/
    and a resume folder with a name containing spaces and a private .previous/. Modes match
    what the old code left (0755 folders, 0644 files). No personal data."""
    from jobhunter.core import db

    data = root / "data"
    conn = db.connect(data / "jobhunter.db")
    db.migrate(conn)
    conn.execute("CREATE TABLE synthetic_note (v TEXT)")
    conn.executemany("INSERT INTO synthetic_note VALUES (?)", [(f"row {i}",) for i in range(50)])
    conn.close()
    (data / "backups").mkdir()
    backup = sqlite3.connect(data / "backups" / "pre-synthetic.db")
    backup.execute("CREATE TABLE t (v TEXT)")
    backup.execute("INSERT INTO t VALUES ('synthetic backup')")
    backup.commit()
    backup.close()
    (data / "cache" / "ab" / "cd").mkdir(parents=True)
    (data / "cache" / "ab" / "cd" / "abcdef.gz").write_bytes(b"synthetic cached page")
    (data / "openrouter_models.json").write_text("{}\n", encoding="utf-8")
    (root / "profile").mkdir()
    (root / "profile" / "preferences.yaml").write_text("hard: {}\n", encoding="utf-8")
    resume = root / "resume"
    (resume / ".previous").mkdir(parents=True)
    (resume / "Synthetic Person - Resume.md").write_text("# Synthetic Person\n", encoding="utf-8")
    (resume / ".previous" / "old.md").write_text("# Older\n", encoding="utf-8")
    for path in [root / "data", root / "profile", resume, *root.rglob("*")]:
        if path.is_dir():
            os.chmod(path, 0o755)
        else:
            os.chmod(path, 0o644)
    os.chmod(resume / ".previous", 0o700)
    os.chmod(resume / "Synthetic Person - Resume.md", 0o600)
    return root


@pytest.fixture
def old_layout(tmp_path: Path):
    """Factory: ``old_layout(dir)`` builds the pre-XDG layout in ``dir`` (default
    ``tmp_path/"checkout"``) and returns it."""

    def make(root: Path | None = None) -> Path:
        root = root if root is not None else tmp_path / "checkout"
        root.mkdir(parents=True, exist_ok=True)
        return build_old_layout(root)

    return make


# ─── The real `claude` binary is never run ──────────────────────────────────
#
# Packet drafting runs the Claude Code CLI on the user's subscription (specs/017 "CLI runner").
# A test that executed the real binary would spend the user's subscription allowance (or, if
# the environment were wrong, API credit), and would send test data to Anthropic. So the
# runner's only binary lookup (``cli_runner._lookup``) sees nothing but fake binaries a test
# installed with the ``fake_claude`` fixture, and its only process launch
# (``cli_runner._spawn``) refuses any executable outside those fake directories. A route test
# that forgets the fake therefore finds no CLI and cannot exec one. ``live`` tests are exempt
# (they are deselected and need JOBHUNTER_LIVE_TESTS=1). Do not weaken this guard.


@pytest.fixture(autouse=True)
def _loopback_test_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """TestClient talks to the console the way a browser on this machine does.

    The console refuses any non-loopback ``Host`` on every request (DNS rebinding, specs/017
    phase 1d), and Starlette's TestClient sends ``Host: testserver`` by default. Default it to
    ``http://127.0.0.1:8808`` instead; a test that wants another Host passes ``base_url``.
    """
    from starlette.testclient import TestClient

    original = TestClient.__init__

    def init(self, app, base_url: str = "http://127.0.0.1:8808", *args, **kwargs):
        original(self, app, base_url, *args, **kwargs)

    monkeypatch.setattr(TestClient, "__init__", init)


class RealClaudeBlocked(RuntimeError):
    """Raised when a test tries to launch a `claude` that is not the test's fake."""


@pytest.fixture(autouse=True)
def _no_real_claude(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):
    import shutil

    from jobhunter.apply import cli_runner

    allowed: list[Path] = []
    if request.node.get_closest_marker("live"):
        yield allowed
        return
    real_spawn = cli_runner._spawn

    def lookup(name: str) -> str | None:
        for d in allowed:
            found = shutil.which(name, path=str(d))
            if found:
                return found
        return None

    def spawn(argv, **kwargs):
        exe = Path(os.path.realpath(argv[0]))
        if not any(exe.is_relative_to(os.path.realpath(d)) for d in allowed):
            raise RealClaudeBlocked(
                f"test tried to launch {argv[0]!r}; install the fake_claude fixture "
                "(tests/conftest.py). The real claude CLI is never run by tests"
            )
        return real_spawn(argv, **kwargs)

    monkeypatch.setattr(cli_runner, "_lookup", lookup)
    monkeypatch.setattr(cli_runner, "_spawn", spawn)
    cli_runner.reset_auth_cache()
    yield allowed
    cli_runner.reset_auth_cache()


_FAKE_CLAUDE = r"""#!{python}
# Fake `claude` for tests (tests/conftest.py, fake_claude fixture). Records each invocation
# (argv, cwd, environment, stdin) and replays a scenario written by the test.
import json, os, pathlib, sys, time

here = pathlib.Path(__file__).resolve().parent
scen = json.loads((here / "scenario.json").read_text())
args = sys.argv[1:]
record = {{"argv": args, "cwd": os.getcwd(), "env": dict(os.environ)}}
if args[:2] == ["auth", "status"]:
    record["stdin"] = ""
    with open(here / "calls.jsonl", "a") as fh:
        fh.write(json.dumps(record) + "\n")
    print(json.dumps(scen["auth"]), flush=True)
    sys.exit(scen.get("auth_exit", 0))
record["stdin"] = sys.stdin.read()
counter = here / "counter"
n = int(counter.read_text()) if counter.exists() else 0
counter.write_text(str(n + 1))
with open(here / "calls.jsonl", "a") as fh:
    fh.write(json.dumps(record) + "\n")
runs = scen["runs"]
run = runs[min(n, len(runs) - 1)]
for step in run["steps"]:
    if "sleep" in step:
        time.sleep(step["sleep"])
    elif "touch" in step:
        (here / step["touch"]).write_text("reached")
    elif "raw" in step:
        print(step["raw"], flush=True)
    else:
        print(json.dumps(step["line"]), flush=True)
if run.get("stderr"):
    print(run["stderr"], file=sys.stderr, flush=True)
sys.exit(run.get("exit", 0))
"""

AUTH_OK = {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty"}


class FakeClaude:
    """A fake ``claude`` first on ``PATH``. ``runs`` is a list of runs, each
    ``{"steps": [{"line": {...}} | {"sleep": s} | {"touch": name} | {"raw": text}],
    "exit": code}``; call n replays ``runs[n]`` (the last one repeats)."""

    def __init__(self, root: Path) -> None:
        self.dir = root
        self.path = root / "claude"
        self.auth: dict = dict(AUTH_OK)
        self.runs: list[dict] = []

    def set(self, runs: list[dict] | None = None, auth: dict | None = None) -> FakeClaude:
        import json
        import sys

        if runs is not None:
            self.runs = runs
        if auth is not None:
            self.auth = auth
        (self.dir / "counter").unlink(missing_ok=True)  # a new scenario starts at run 0
        self.path.write_text(_FAKE_CLAUDE.format(python=sys.executable), encoding="utf-8")
        self.path.chmod(0o755)
        (self.dir / "scenario.json").write_text(
            json.dumps({"runs": self.runs or [{"steps": []}], "auth": self.auth}),
            encoding="utf-8",
        )
        return self

    def calls(self, *, auth: bool | None = False) -> list[dict]:
        """Recorded invocations; ``auth`` False skips `auth status`, True keeps only those."""
        import json

        log = self.dir / "calls.jsonl"
        if not log.exists():
            return []
        out = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        if auth is None:
            return out
        return [c for c in out if (c["argv"][:2] == ["auth", "status"]) == auth]

    def reached(self, marker: str) -> bool:
        return (self.dir / marker).exists()


@pytest.fixture
def fake_claude(
    _no_real_claude: list[Path], tmp_path_factory: pytest.TempPathFactory, monkeypatch
) -> FakeClaude:
    root = tmp_path_factory.mktemp("fakebin")
    fake = FakeClaude(root).set()
    _no_real_claude.append(root)
    monkeypatch.setenv("PATH", f"{root}{os.pathsep}{os.environ.get('PATH', '')}")
    return fake


# The recorded shape of `claude -p --output-format stream-json --verbose --json-schema` output
# (init, rate_limit_event, assistant, user, result) with "__STRUCTURED__" where the structured
# output goes. Built by hand in the shape Claude Code 2.1.x emits: capturing a real transcript
# is a model call on the user's subscription, which no test or agent makes unasked. The opt-in
# live smoke test (tests/apply/test_cli_live.py) checks the same envelope against the real CLI.
CLAUDE_STREAM = Path(__file__).parent / "fixtures" / "claude_cli" / "stream_ok.jsonl"


def claude_run(
    structured,
    *,
    overage: bool = False,
    init: dict | None = None,
    result: dict | None = None,
    exit: int = 0,
    model: str | None = None,
) -> dict:
    """One fake-claude run replaying the recorded transcript with ``structured`` as output.
    ``model`` renames the serving model everywhere the transcript names it."""
    import json

    raw = CLAUDE_STREAM.read_text(encoding="utf-8")
    if model is not None:
        raw = raw.replace('"claude-opus-5"', json.dumps(model))
    lines = [json.loads(x) for x in raw.splitlines()]
    for line in lines:
        if line["type"] == "system":
            line.update(init or {})
        if line["type"] == "rate_limit_event" and overage:
            line["rate_limit_info"]["isUsingOverage"] = True
        if line["type"] == "assistant":
            line["message"]["content"][0]["input"] = structured
        if line["type"] == "result":
            line["structured_output"] = structured
            line.update(result or {})
    return {"steps": [{"line": line} for line in lines], "exit": exit}


@pytest.fixture
def claude_stream():
    return claude_run
