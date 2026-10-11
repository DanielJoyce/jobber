"""Spec 018 C8: the image workflow and its smoke script.

The real job runs only on GitHub Actions (it needs podman). Here the script is driven with a
fake ``podman`` and ``curl`` on PATH to prove its checks fail when they should, in particular
that a failing ``podman inspect`` or ``podman logs`` fails the run instead of passing an
empty grep, and that a leaked secret value fails it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
SMOKE = REPO / "scripts" / "ci-image-smoke.sh"
WORKFLOW = REPO / ".github" / "workflows" / "image.yml"
COMMIT = "abc1234"

FAKE_PODMAN = r"""#!/usr/bin/env bash
# Canned podman. FAIL_ON=<subcommand> makes that subcommand exit 1; LEAK=inspect|logs|status
# makes that output contain the secret value read by `secret create`.
sub=$1
args="$*"
store="${FAKE_DIR}/secret"
[ "$sub" = "${FAIL_ON:-}" ] && exit 1
case "$sub" in
  image)
    case "$args" in
      *Size*) echo "${FAKE_SIZE:-100000000}" ;;
      *) echo "${FAKE_REVISION:-abc1234}" ;;
    esac ;;
  secret) case "$2" in create) tr -d '\r\n' >"$store";; esac ;;
  inspect) echo "{\"Name\":\"$2\"}"
           [ "${LEAK:-}" = inspect ] && cat "$store" ;;
  logs) echo "ANTHROPIC_API_KEY  set (file /run/secrets/anthropic_key)"
        [ "${LEAK:-}" = logs ] && cat "$store" ;;
  run)
    case "$args" in
      *"--name jh-smoke-secret"*) ;;
      *" -d "*) ;;
      *"--help"*) echo "Usage: jobhunter" ;;
      *"--entrypoint id"*) echo 1000 ;;
      *" version"*) echo "commit: ${FAKE_COMMIT:-abc1234} (build)" ;;
      *" paths"*)
        printf 'mode: container\ndata_dir /data\ncache_dir /cache\n'
        printf 'config_file /config/config.toml\n' ;;
      *"ls -A /app"*) echo .venv ;;
      *"find /app"*) printf '%s' "${FAKE_FOUND:-}" ;;
      *"find /data"*) printf '%s' "${FAKE_MOUNT:-}" ;;
      *"ls /data"*) echo jobhunter.db ;;
    esac ;;
  exec) echo 200 ;;
  *) ;;
esac
exit 0
"""

FAKE_CURL = r"""#!/usr/bin/env bash
case "$*" in *"Host: evil"*) printf 403;; *) printf "${FAKE_HEALTH:-200}";; esac
"""


def run_smoke(tmp_path: Path, **env: str) -> subprocess.CompletedProcess[str]:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in (("podman", FAKE_PODMAN), ("curl", FAKE_CURL)):
        f = bindir / name
        f.write_text(body)
        f.chmod(0o755)
    (tmp_path / "fake").mkdir()
    full = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "FAKE_DIR": str(tmp_path / "fake"),
        "SMOKE_WAIT": "1",
        **env,
    }
    return subprocess.run(
        ["bash", str(SMOKE), "localhost/jobhunter:test", COMMIT],
        env=full,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


needs_bash = pytest.mark.skipif(shutil.which("bash") is None, reason="bash missing")


@needs_bash
def test_smoke_passes_when_everything_is_right(tmp_path: Path) -> None:
    result = run_smoke(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "all image smoke checks passed" in result.stdout


@needs_bash
@pytest.mark.parametrize("sub", ["inspect", "logs", "image", "run", "secret", "volume"])
def test_failing_podman_command_fails_the_script(tmp_path: Path, sub: str) -> None:
    result = run_smoke(tmp_path, FAIL_ON=sub)
    assert result.returncode != 0
    assert "all image smoke checks passed" not in result.stdout


@needs_bash
@pytest.mark.parametrize("where", ["inspect", "logs"])
def test_secret_value_in_inspect_or_logs_fails(tmp_path: Path, where: str) -> None:
    result = run_smoke(tmp_path, LEAK=where)
    assert result.returncode != 0
    assert "secret value" in result.stderr


@needs_bash
@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"FAKE_SIZE": "450000001"}, "size budget"),
        ({"FAKE_REVISION": "other"}, "revision label"),
        ({"FAKE_COMMIT": "other00"}, "build commit"),
        ({"FAKE_FOUND": "/app/.env"}, "personal or secret files"),
        ({"FAKE_HEALTH": "000"}, "/healthz"),
        ({"FAKE_MOUNT": "/data/stray.db\n"}, "mount points are not empty"),
    ],
)
def test_smoke_checks_catch_regressions(tmp_path: Path, env: dict[str, str], message: str) -> None:
    result = run_smoke(tmp_path, **env)
    assert result.returncode != 0
    assert message in result.stderr


def test_smoke_never_pipes_podman_into_grep() -> None:
    """A pipeline would hide a podman failure from set -e unless pipefail catches it; capture
    output first instead."""
    text = SMOKE.read_text()
    assert "set -euo pipefail" in text
    for line in text.splitlines():
        code = line.split("#", 1)[0]
        assert not ("podman" in code and "| grep" in code), line


def test_workflow_shape() -> None:
    doc = yaml.safe_load(WORKFLOW.read_text())
    jobs = doc["jobs"]
    assert jobs["lint"]["runs-on"].startswith("ubuntu")
    build = jobs["build-and-smoke"]
    assert build["runs-on"].startswith("ubuntu")
    assert build["needs"] == "lint"
    steps = "\n".join(str(s.get("run", "")) + str(s.get("uses", "")) for s in build["steps"])
    assert "podman build" in steps
    assert "BUILD_COMMIT=" in steps
    assert "localhost/jobhunter:current" in steps
    assert "ci-image-smoke.sh" in steps
    assert "hadolint" in str(jobs["lint"])
    assert "push" not in steps.replace("pull_request", "")  # local build only, no registry
    # No secrets of any kind in the workflow.
    assert "secrets." not in WORKFLOW.read_text()
    assert doc["permissions"] == {"contents": "read"}
