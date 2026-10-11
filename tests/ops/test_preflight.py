"""specs/018 C13: `jobhunter container preflight` against a fake podman on PATH.

The fake records every argv. It is only ever asked for secret *names* (`secret ls`) and for
the image's `version --json`; no secret value exists anywhere in these tests.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from jobhunter.cli import app
from jobhunter.core import db
from jobhunter.ops import preflight as pf

runner = CliRunner()
ISO = "2026-10-10T12:00:00+00:00"
ALL_SECRETS = sorted(pf.MATRIX)

FAKE_PODMAN = """#!{python}
import json, os, sys
argv = sys.argv[1:]
with open(os.environ["FAKE_PODMAN_LOG"], "a") as fh:
    fh.write(json.dumps(argv) + "\\n")
if argv[:2] == ["secret", "ls"]:
    if os.environ.get("FAKE_PODMAN_LS_FAIL"):
        sys.exit(125)
    print(os.environ.get("FAKE_PODMAN_SECRETS", "").replace(",", "\\n"))
    sys.exit(0)
if argv[:1] == ["run"]:
    if os.environ.get("FAKE_PODMAN_RUN_FAIL"):
        print("Error: localhost/jobhunter:current: image not known", file=sys.stderr)
        sys.exit(125)
    print(json.dumps({{
        "package": "0.1.0",
        "commit": os.environ.get("FAKE_IMAGE_COMMIT", "abc123"),
        "code_schema": int(os.environ["FAKE_IMAGE_SCHEMA"]),
    }}))
    sys.exit(0)
sys.exit(2)
"""

FAKE_SYSTEMCTL = """#!/bin/sh
# is-active NAME...: one state per name, from $FAKE_ACTIVE (names that are active)
shift 2
for n in "$@"; do
  case " $FAKE_ACTIVE " in *" $n "*) echo active ;; *) echo inactive ;; esac
done
"""


@pytest.fixture
def host(tmp_path, monkeypatch):
    home = Path(os.environ["HOME"])  # conftest: a fresh tmp home
    bindir = tmp_path / "bin"
    bindir.mkdir()
    exe = bindir / "podman"
    exe.write_text(FAKE_PODMAN.format(python=sys.executable))
    exe.chmod(0o755)
    sysctl = bindir / "systemctl"
    sysctl.write_text(FAKE_SYSTEMCTL)
    sysctl.chmod(0o755)
    log = tmp_path / "podman.jsonl"
    monkeypatch.setenv("PATH", str(bindir))
    monkeypatch.setenv("FAKE_PODMAN_LOG", str(log))
    monkeypatch.setenv("FAKE_IMAGE_SCHEMA", str(db.code_version()))
    monkeypatch.setenv("FAKE_PODMAN_SECRETS", ",".join(ALL_SECRETS))
    data = home / ".local" / "share" / "jobhunter"
    conn = db.connect(data / "jobhunter.db")
    db.migrate(conn)
    conn.close()
    units = home / ".config" / "containers" / "systemd"
    units.mkdir(parents=True)
    for unit in pf.UNITS:
        (units / f"{unit}.container").write_text(_sketch_unit(unit, home))

    class H:
        pass

    h = H()
    h.home, h.data, h.units, h.tmp = home, data, units, tmp_path

    def calls():
        return [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []

    h.calls = calls
    return h


def _sketch_unit(unit: str, home: Path) -> str:
    oneshot = unit != pf.CONSOLE
    return (
        "[Unit]\n"
        f"Description={unit}\n"
        "[Container]\n"
        "Image=localhost/jobhunter:current\n"
        f"ContainerName={unit}\n"
        f"Volume={home}/.local/share/jobhunter:/data:z\n"
        f"Exec={'run --wait 4h' if oneshot else 'console'}\n"
        "[Service]\n" + ("Type=oneshot\n" if oneshot else "Restart=on-failure\n")
    )


def preflight(*args: str):
    return runner.invoke(app, ["container", "preflight", *args])


def dropins(units: Path) -> dict[str, set[str]]:
    """unit -> secret names in its drop-ins (parsed from Secret= lines)."""
    out: dict[str, set[str]] = {}
    for unit in pf.UNITS:
        d = units / f"{unit}.container.d"
        names: set[str] = set()
        if d.is_dir():
            for f in d.iterdir():
                for line in f.read_text().splitlines():
                    if line.startswith("Secret="):
                        names.add(line.removeprefix("Secret=").split(",")[0])
        out[unit] = names
    return out


def test_all_secrets_follow_the_matrix(host):
    r = preflight("--yes")
    assert r.exit_code == 0, r.output
    got = dropins(host.units)
    scorer_keys = {
        "jobhunter_anthropic",
        "jobhunter_openrouter",
        "jobhunter_openai_compat",
        "jobhunter_llama",
    }
    usajobs = {"jobhunter_usajobs_key", "jobhunter_usajobs_email"}
    gmail = {"jobhunter_google_client", "jobhunter_gmail_token"}
    assert got[pf.CONSOLE] == scorer_keys  # never Gmail, never USAJOBS
    assert got[pf.RUN] == scorer_keys | usajobs | gmail
    assert got[pf.COLLECT] == {"jobhunter_anthropic"}
    assert got[pf.RESCORE] == scorer_keys
    assert got[pf.VERIFY] == usajobs
    assert got[pf.BACKUP] == set()


def test_dropin_golden_text(host):
    assert preflight("--yes").exit_code == 0
    run_d = host.units / "jobhunter-ctr-run.container.d"
    text = (run_d / "50-jobhunter_openrouter.conf").read_text()
    assert text == (
        "# written by jobhunter container preflight; removed by it when the secret is gone\n"
        "[Container]\n"
        "Secret=jobhunter_openrouter,type=mount,target=/run/secrets/openrouter_api_key,"
        "uid=1000,gid=1000,mode=0400\n"
        "Environment=OPENROUTER_API_KEY_FILE=/run/secrets/openrouter_api_key\n"
    )
    gmail = host.units / "jobhunter-ctr-run.container.d" / "50-jobhunter_gmail_token.conf"
    assert "Environment=JOBHUNTER_GMAIL_TOKEN_FILE=/run/secrets/gmail_token" in gmail.read_text()
    client = host.units / "jobhunter-ctr-run.container.d" / "50-jobhunter_google_client.conf"
    assert (
        "Environment=JOBHUNTER_GOOGLE_CLIENT_SECRETS=/run/secrets/google_client_secret.json"
        in client.read_text()
    )


def test_dropins_name_only_secrets_that_exist(host, monkeypatch):
    present = {"jobhunter_openrouter", "jobhunter_usajobs_key", "unrelated_secret"}
    monkeypatch.setenv("FAKE_PODMAN_SECRETS", ",".join(sorted(present)))
    r = preflight("--yes")
    assert r.exit_code == 0, r.output
    named = set().union(*dropins(host.units).values())
    assert named == {"jobhunter_openrouter", "jobhunter_usajobs_key"}
    assert "unrelated_secret" not in named
    assert "not created" in r.output and "jobhunter_gmail_token" in r.output


def test_env_names_come_from_config(host, monkeypatch, tmp_path):
    cfg = host.home / ".config" / "jobhunter" / "config.toml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(
        '[scoring.openrouter]\napi_key_env = "MY_ROUTER_KEY"\n[scoring.local]\napi_key_env = ""\n'
    )
    r = preflight("--yes")
    assert r.exit_code == 0, r.output
    console_d = host.units / "jobhunter-console.container.d"
    text = (console_d / "50-jobhunter_openrouter.conf").read_text()
    assert "Environment=MY_ROUTER_KEY_FILE=/run/secrets/openrouter_api_key" in text
    assert not (host.units / "jobhunter-console.container.d" / "50-jobhunter_llama.conf").exists()
    assert "jobhunter_llama: scoring.local.api_key_env is empty" in r.output


def test_registry_rows_set_usajobs_names():
    from types import SimpleNamespace

    auth = {"key_env": "UJ_KEY", "email_env": "UJ_MAIL"}
    row = SimpleNamespace(key="us-usajobs", config={"auth": auth})

    from jobhunter.config import Settings

    specs = {s.secret: s for s in pf.secret_matrix(Settings(), [row])}
    assert specs["jobhunter_usajobs_key"].env == "UJ_KEY_FILE"
    assert specs["jobhunter_usajobs_email"].env == "UJ_MAIL_FILE"
    # the packaged registry gives the documented defaults
    packaged = {s.secret: s for s in pf.secret_matrix(Settings())}
    assert packaged["jobhunter_usajobs_key"].env == "USAJOBS_API_KEY_FILE"
    assert packaged["jobhunter_usajobs_email"].env == "USAJOBS_EMAIL_FILE"


def test_bad_env_name_is_not_written(host, monkeypatch):
    cfg = host.home / ".config" / "jobhunter" / "config.toml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text('[scoring.openrouter]\napi_key_env = "X\\nExecStart=/bin/evil"\n')
    r = preflight("--yes")
    assert r.exit_code == 0, r.output
    for f in host.units.rglob("*.conf"):
        assert "evil" not in f.read_text()
    assert "not a valid environment variable name" in r.output


def test_stale_dropins_removed_foreign_untouched(host, monkeypatch):
    assert preflight("--yes").exit_code == 0
    run_d = host.units / "jobhunter-ctr-run.container.d"
    foreign = run_d / "10-mine.conf"
    foreign.write_text("[Container]\nEnvironment=FOO=bar\n")
    # a file of ours someone copied under another name is still ours (header line)
    monkeypatch.setenv("FAKE_PODMAN_SECRETS", "jobhunter_anthropic")
    r = preflight()
    assert r.exit_code == 0, r.output
    assert dropins(host.units)[pf.RUN] == {"jobhunter_anthropic"}
    assert foreign.read_text() == "[Container]\nEnvironment=FOO=bar\n"
    assert "remove" in r.output and "50-jobhunter_openrouter.conf" in r.output
    assert "daemon-reload" in r.output and "restart jobhunter-console" in r.output


def test_foreign_file_at_our_name_is_left_alone(host):
    d = host.units / "jobhunter-console.container.d"
    d.mkdir()
    mine = d / "50-jobhunter_anthropic.conf"
    mine.write_text("# my own\n[Container]\n")
    r = preflight("--yes")
    assert r.exit_code == 0, r.output
    assert mine.read_text() == "# my own\n[Container]\n"
    assert "was not written by preflight; left alone" in r.output


def test_rerun_is_idempotent(host):
    assert preflight("--yes").exit_code == 0
    r = preflight()
    assert r.exit_code == 0, r.output
    assert "drop-ins up to date" in r.output
    assert "marker" in r.output and "present" in r.output


def test_podman_only_asked_for_names_and_version(host):
    assert preflight("--yes").exit_code == 0
    calls = host.calls()
    assert ["secret", "ls", "--format", "{{.Name}}"] in calls
    run = [c for c in calls if c[:1] == ["run"]]
    assert run == [
        [
            "run",
            "--rm",
            "--pull=never",
            "--network=none",
            "localhost/jobhunter:current",
            "version",
            "--json",
        ]
    ]
    for c in calls:
        assert c[:2] in (["secret", "ls"], ["run", "--rm"])  # never inspect/create/rm


def test_refuses_absolute_paths(host):
    conn = db.connect(host.data / "jobhunter.db")
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute(
        "INSERT INTO attachment (application_id, kind, path, added_at) VALUES (1, 'resume', ?, ?)",
        (str(host.tmp / "Documents" / "r.pdf"), ISO),
    )
    conn.close()
    r = preflight("--yes")
    assert r.exit_code == 1
    assert "jobhunter db relativize-paths --apply" in r.output
    assert "refused" in r.output
    assert list(host.units.rglob("*.conf")) == []
    assert not (host.data / "deployment.toml").exists()


def test_reports_schema_skew(host, monkeypatch):
    monkeypatch.setenv("FAKE_IMAGE_SCHEMA", str(db.code_version() + 1))
    r = preflight("--yes")
    assert r.exit_code == 1
    assert "schema skew" in r.output
    assert f"host code has migration {db.code_version()}" in r.output
    assert list(host.units.rglob("*.conf")) == []


def test_commit_difference_only_warns(host, monkeypatch):
    from jobhunter.ops import version as ver

    monkeypatch.setattr(ver, "build_commit", lambda env=None: ("1111111111111111", "git"))
    monkeypatch.setenv("FAKE_IMAGE_COMMIT", "2222222222222222")
    r = preflight("--yes")
    assert r.exit_code == 0, r.output
    assert "image built from 222222222222" in r.output


def test_pending_database_refused(host, monkeypatch):
    raw = db.connect(host.data / "jobhunter.db")
    top = db.code_version()
    raw.execute("DELETE FROM schema_version WHERE version = ?", (top,))
    raw.close()
    r = preflight("--yes")
    assert r.exit_code == 1
    assert "jobhunter db migrate" in r.output


def test_missing_image_refused(host, monkeypatch):
    monkeypatch.setenv("FAKE_PODMAN_RUN_FAIL", "1")
    r = preflight("--yes")
    assert r.exit_code == 1
    assert "cannot read the image's version" in r.output and "podman build" in r.output


def test_podman_unreachable(host, monkeypatch, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    r = preflight("--yes")
    assert r.exit_code == 1
    assert "podman is not reachable" in r.output
    assert list(host.units.rglob("*.conf")) == []


def test_podman_secret_ls_failing_writes_nothing(host, monkeypatch):
    assert preflight("--yes").exit_code == 0
    before = sorted(host.units.rglob("*.conf"))
    monkeypatch.setenv("FAKE_PODMAN_LS_FAIL", "1")
    r = preflight()
    assert r.exit_code == 1
    assert sorted(host.units.rglob("*.conf")) == before  # nothing removed on a broken probe


def test_refuses_enabled_host_timers(host):
    wants = host.home / ".config" / "systemd" / "user" / "timers.target.wants"
    wants.mkdir(parents=True)
    (wants / "jobhunter-run.timer").symlink_to("../jobhunter-run.timer")  # dangling is fine
    r = preflight("--yes")
    assert r.exit_code == 1
    assert "jobhunter schedule uninstall" in r.output


def test_marker_needs_yes_or_confirm(host):
    r = preflight()  # CliRunner stdin is not a tty: never asks, never writes
    assert r.exit_code == 0, r.output
    assert not (host.data / "deployment.toml").exists()
    assert "rerun with --yes" in r.output
    r = preflight("--yes")
    assert r.exit_code == 0
    marker = host.data / "deployment.toml"
    assert 'mode = "container"' in marker.read_text()
    assert stat.S_IMODE(marker.stat().st_mode) == 0o600
    assert db.deployment_marker(host.data) == marker


def test_dry_run_writes_nothing(host):
    shutil.rmtree(host.home / ".cache", ignore_errors=True)
    r = preflight("--dry-run", "--yes")
    assert r.exit_code == 0, r.output
    assert "would write" in r.output
    assert list(host.units.rglob("*.conf")) == []
    assert not (host.data / "deployment.toml").exists()
    assert not (host.home / ".cache" / "jobhunter").exists()


def test_creates_bind_sources(host):
    shutil.rmtree(host.home / ".cache", ignore_errors=True)
    cfg = host.home / ".config" / "jobhunter" / "config.toml"
    assert not cfg.exists()
    r = preflight("--yes")
    assert r.exit_code == 0, r.output
    cache = host.home / ".cache" / "jobhunter"
    assert stat.S_IMODE(cache.stat().st_mode) == 0o700
    assert cfg.read_text() == ""
    assert stat.S_IMODE(cfg.stat().st_mode) == 0o600


def test_data_dir_elsewhere_refused(host, monkeypatch, tmp_path):
    other = tmp_path / "elsewhere"
    monkeypatch.setenv("JOBHUNTER_DATA_DIR", str(other))
    r = preflight("--yes")
    assert r.exit_code == 1
    assert "units mount" in r.output and "/data" in r.output


def test_gmail_token_leftover_warned_client_json_kept(host):
    cfgdir = host.home / ".config" / "jobhunter"
    cfgdir.mkdir(parents=True, exist_ok=True)
    (cfgdir / "gmail_token.json").write_text("{}")
    (cfgdir / "google_client_secret.json").write_text("{}")
    r = preflight("--yes")
    assert r.exit_code == 0, r.output
    assert f"rm {cfgdir / 'gmail_token.json'}" in r.output
    assert "google_client_secret.json" not in r.output  # never asked to remove (PM decision)
    assert (cfgdir / "gmail_token.json").exists()  # warned, never deleted


def test_gmail_token_not_warned_without_secret(host, monkeypatch):
    monkeypatch.setenv("FAKE_PODMAN_SECRETS", "jobhunter_anthropic")
    cfgdir = host.home / ".config" / "jobhunter"
    cfgdir.mkdir(parents=True, exist_ok=True)
    (cfgdir / "gmail_token.json").write_text("{}")
    r = preflight("--yes")
    assert "gmail_token.json" not in r.output


def test_env_file_mode_warned(host, monkeypatch):
    env = host.home / ".env"
    env.write_text("OPENROUTER_API_KEY=\n")
    env.chmod(0o644)
    r = preflight("--yes")
    assert r.exit_code == 0, r.output
    assert f"chmod 600 {env}" in r.output


def test_missing_base_units_warned(host):
    (host.units / "jobhunter-ctr-verify.container").unlink()
    r = preflight("--yes")
    assert r.exit_code == 0, r.output
    assert "base units not installed" in r.output and "jobhunter-ctr-verify" in r.output


def test_container_mode_refused(host, monkeypatch):
    monkeypatch.setenv("JOBHUNTER_CONTAINER", "1")
    r = preflight("--yes")
    assert r.exit_code == 2
    assert host.calls() == []


def test_leave_removes_ours_and_marker(host):
    assert preflight("--yes").exit_code == 0
    foreign = host.units / "jobhunter-console.container.d" / "10-mine.conf"
    foreign.write_text("[Container]\n")
    r = preflight("--leave")
    assert r.exit_code == 0, r.output
    assert [p.name for p in host.units.rglob("*.conf")] == ["10-mine.conf"]
    assert not (host.data / "deployment.toml").exists()
    assert "jobhunter schedule install" in r.output


def test_leave_refuses_while_units_active(host, monkeypatch):
    assert preflight("--yes").exit_code == 0
    monkeypatch.setenv("FAKE_ACTIVE", "jobhunter-console.service")
    r = preflight("--leave")
    assert r.exit_code == 1
    assert "systemctl --user stop jobhunter-console.service" in r.output
    assert (host.data / "deployment.toml").exists()


def test_unlabelled_files_listed_with_chcon(tmp_path):
    root = tmp_path / "data"
    (root / "sub").mkdir(parents=True)
    moved = root / "sub" / "moved.pdf"
    moved.write_text("x")
    (root / "ok.db").write_text("x")

    def label(p: Path) -> str:
        return "user_home_t" if p == moved else "container_file_t"

    checked, examples, total = pf.unlabelled_files(root, label)
    assert checked and total == 1 and examples == [moved]
    # not labelled yet: nothing to check
    assert pf.unlabelled_files(root, lambda p: "user_home_t") == (False, [], 0)


def test_wrapper_column_limited_to_existing():
    from jobhunter.config import Settings

    specs = pf.secret_matrix(Settings())
    names = {s.secret for s in pf.wrapper_specs(specs, ["jobhunter_llama", "other"])}
    assert names == {"jobhunter_llama"}


QUADLET = Path("/usr/libexec/podman/quadlet")


@pytest.mark.skipif(not QUADLET.exists(), reason="quadlet generator not installed")
def test_quadlet_dry_run_accepts_units_and_dropins(host, tmp_path):
    assert preflight("--yes").exit_code == 0
    runtime = Path("/tmp") / f"jh-q-{os.getpid()}"
    runtime.mkdir(mode=0o700, exist_ok=True)
    try:
        done = subprocess.run(
            [str(QUADLET), "-dryrun", "-user"],
            env={
                "QUADLET_UNIT_DIRS": str(host.units),
                "HOME": str(host.home),
                "XDG_RUNTIME_DIR": str(runtime),
                "PATH": "/usr/bin:/bin",
            },
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    finally:
        shutil.rmtree(runtime, ignore_errors=True)
    assert done.returncode == 0, done.stderr
    out = done.stdout
    console = out.split("---jobhunter-console.service---")[1].split("---jobhunter-ctr")[0]
    assert "--secret jobhunter_openrouter,type=mount,target=/run/secrets/openrouter_api_key" in (
        console
    )
    assert "jobhunter_gmail_token" not in console
    assert "OPENROUTER_API_KEY_FILE=/run/secrets/openrouter_api_key" in console
    run_unit = out.split("---jobhunter-ctr-run.service---")[1]
    assert "jobhunter_gmail_token" in run_unit
