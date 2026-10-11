"""``jobhunter-ctr``: one jobhunter command in a one-off container (spec 018, C15).

After the switch to containers, commands that are not a unit (``mail status|match|setup|
sample``, ``secrets status``, ``sources list``, ``db migrate`` from ``jobhunter upgrade``) run
in a throwaway ``podman run --rm`` on ``localhost/jobhunter:current`` with the same mounts,
user namespace and path variables as the Quadlet units (spec 018 *Quadlet*), plus the secrets
in the matrix's ``jobhunter-ctr`` column that exist in ``podman secret ls`` (podman refuses a
container naming a missing secret).

This module is the one place that knows those options. :func:`run_argv` builds the argv for
Python callers (``jobhunter upgrade``); :func:`render_script` writes the same options into the
``~/.local/bin/jobhunter-ctr`` shell script that ``jobhunter container preflight`` (C13), or
``jobhunter ctr-script --write`` until then, installs.

One-off containers carry the label :data:`ONEOFF_LABEL` and no name, so ``podman ps -a
--filter name=jobhunter-ctr`` stays empty (``--rm``) while ``jobhunter upgrade`` can still see
one that is running.
"""

from __future__ import annotations

import os
import shlex
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jobhunter.mail.auth import PODMAN_CHAINS
from jobhunter.ops.proc import Runner

IMAGE_REPO = "localhost/jobhunter"
IMAGE = f"{IMAGE_REPO}:current"
ONEOFF_LABEL_KEY = "io.jobhunter.oneoff"
ONEOFF_LABEL = f"{ONEOFF_LABEL_KEY}=1"
USERNS = "keep-id:uid=1000,gid=1000"
SCRIPT_HEADER = "# jobhunter-ctr: written by jobhunter (specs/018-containers.md, C15)"
SECRETS_DIR = "/run/secrets"
PROBE_TIMEOUT_S = 20.0


class CtrError(RuntimeError):
    """podman is unreachable, or the wrapper script cannot be written."""


@dataclass(frozen=True)
class Layout:
    """Host side of the bind mounts. Container side is fixed (D3): /data, /cache and the
    single file /config/config.toml, read-only."""

    data_dir: Path
    cache_dir: Path
    config_file: Path
    image: str = IMAGE

    def volumes(self) -> list[str]:
        return [
            f"{self.data_dir}:/data:z",
            f"{self.cache_dir}:/cache:z",
            f"{self.config_file}:/config/config.toml:ro,z",
        ]


# The five path variables every unit sets (D3), overriding any host [paths] in config.toml.
PATH_ENV = (
    ("JOBHUNTER_DATA_DIR", "/data"),
    ("JOBHUNTER_DB_PATH", "/data/jobhunter.db"),
    ("JOBHUNTER_PROFILE_DIR", "/data/profile"),
    ("JOBHUNTER_RESUME_PATH", "/data/resume"),
    ("JOBHUNTER_CACHE_DIR", "/cache"),
)


@dataclass(frozen=True)
class SecretMount:
    """One podman secret mounted as a file, and the variable that points the code at it."""

    secret: str  # podman secret name
    target: str  # file inside the container
    env: str  # variable set to ``target``: ``<NAME>_FILE`` for keys, a path variable otherwise

    def podman_args(self) -> list[str]:
        return [
            "--secret",
            f"{self.secret},type=mount,target={self.target},uid=1000,gid=1000,mode=0400",
            "--env",
            f"{self.env}={self.target}",
        ]


def _usajobs_auth() -> tuple[str, str]:
    """The registry's ``key_env``/``email_env`` for USAJOBS (defaults if it cannot be read)."""
    key, email = "USAJOBS_API_KEY", "USAJOBS_EMAIL"
    try:
        from importlib import resources

        from ruamel.yaml import YAML

        text = resources.files("jobhunter.sources").joinpath("registry.yaml").read_text("utf-8")
        rows = YAML(typ="safe").load(text)
    except Exception:
        return key, email
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, Mapping) or row.get("id") != "usajobs":
            continue
        auth = (row.get("config") or {}).get("auth") or {}
        if isinstance(auth, Mapping):
            key = auth.get("key_env") if isinstance(auth.get("key_env"), str) else key
            email = auth.get("email_env") if isinstance(auth.get("email_env"), str) else email
    return key, email


def ctr_secret_mounts(settings: Any | None = None) -> list[SecretMount]:
    """Every secret in the ``jobhunter-ctr`` column of spec 018's matrix (all of them), with
    variable names from config (``api_key_env``) and the registry, not a fixed list."""
    scoring = getattr(settings, "scoring", None)

    def key_env(section: str, default: str) -> str:
        return getattr(getattr(scoring, section, None), "api_key_env", None) or default

    usajobs_key, usajobs_email = _usajobs_auth()
    return [
        SecretMount(
            "jobhunter_anthropic", f"{SECRETS_DIR}/anthropic_api_key", "ANTHROPIC_API_KEY_FILE"
        ),
        SecretMount(
            "jobhunter_openrouter",
            f"{SECRETS_DIR}/openrouter_api_key",
            f"{key_env('openrouter', 'OPENROUTER_API_KEY')}_FILE",
        ),
        SecretMount(
            "jobhunter_openai_compat",
            f"{SECRETS_DIR}/openai_compat_api_key",
            f"{key_env('openai_compat', 'OPENAI_COMPAT_API_KEY')}_FILE",
        ),
        SecretMount(
            "jobhunter_llama",
            f"{SECRETS_DIR}/llama_api_key",
            f"{key_env('local', 'LLAMA_API_KEY')}_FILE",
        ),
        SecretMount(
            "jobhunter_usajobs_key", f"{SECRETS_DIR}/usajobs_api_key", f"{usajobs_key}_FILE"
        ),
        SecretMount(
            "jobhunter_usajobs_email", f"{SECRETS_DIR}/usajobs_email", f"{usajobs_email}_FILE"
        ),
        SecretMount(
            "jobhunter_google_client",
            f"{SECRETS_DIR}/google_client_secret.json",
            "JOBHUNTER_GOOGLE_CLIENT_SECRETS",
        ),
        SecretMount(
            "jobhunter_gmail_token", f"{SECRETS_DIR}/gmail_token", "JOBHUNTER_GMAIL_TOKEN_FILE"
        ),
    ]


def present(mounts: Iterable[SecretMount], existing: Iterable[str]) -> list[SecretMount]:
    """Only the mounts whose podman secret exists."""
    names = set(existing)
    return [m for m in mounts if m.secret in names]


def option_groups(layout: Layout, mounts: Sequence[SecretMount] = ()) -> list[list[str]]:
    """``podman run`` options shared by the script and :func:`run_argv`, one flag with its
    value per group (no image, no -i/-t)."""
    groups = [["--rm"], ["--label", ONEOFF_LABEL], ["--userns", USERNS], ["--tz", "local"]]
    groups += [["--volume", volume] for volume in layout.volumes()]
    groups += [["--env", f"{name}={value}"] for name, value in PATH_ENV]
    for mount in mounts:
        args = mount.podman_args()
        groups += [args[i : i + 2] for i in range(0, len(args), 2)]
    return groups


def run_options(layout: Layout, mounts: Sequence[SecretMount] = ()) -> list[str]:
    return [arg for group in option_groups(layout, mounts) for arg in group]


def run_argv(
    podman: Sequence[str],
    layout: Layout,
    command: Sequence[str],
    mounts: Sequence[SecretMount] = (),
) -> list[str]:
    """``podman run`` of ``jobhunter <command>`` in a one-off container, non-interactive."""
    return [*podman, "run", *run_options(layout, mounts), layout.image, *command]


def find_podman(runner: Runner) -> list[str]:
    """The first working podman prefix in the host-escape chain (``podman``, ``flatpak-spawn
    --host podman``, ``host-spawn podman``, ``distrobox-host-exec podman``)."""
    for chain in PODMAN_CHAINS:
        if runner.which(chain[0]) is None:
            continue
        if chain[0] == "distrobox-host-exec" and runner.which("host-spawn") is None:
            continue  # it would download and sudo-install host-spawn without asking
        probe = runner.run(
            [*chain, "version", "--format", "{{.Client.Version}}"], timeout=PROBE_TIMEOUT_S
        )
        if probe.ok:
            return list(chain)
    raise CtrError(
        "podman is not reachable (tried podman, flatpak-spawn --host podman, host-spawn podman, "
        "distrobox-host-exec podman). Run this on the host, or from a shell that can reach it."
    )


def host_prefix(podman: Sequence[str]) -> list[str]:
    """The escape prefix of a podman chain (``[]`` on the host), for running ``systemctl``."""
    return list(podman[:-1])


def secret_names(runner: Runner, podman: Sequence[str]) -> list[str]:
    """Names from ``podman secret ls`` (names only; values are never read)."""
    done = runner.run([*podman, "secret", "ls", "--format", "{{.Name}}"], timeout=PROBE_TIMEOUT_S)
    if not done.ok:
        raise CtrError(f"podman secret ls failed: {done.tail() or f'exit {done.returncode}'}")
    return [line.strip() for line in done.stdout.splitlines() if line.strip()]


def render_script(layout: Layout, mounts: Sequence[SecretMount] = ()) -> str:
    """The ``jobhunter-ctr`` shell script (POSIX sh). podman is found at run time through the
    same host-escape chain, so the script works from the host and from a toolbox."""
    q = shlex.quote
    lines = [" ".join(q(arg) for arg in group) for group in option_groups(layout, mounts)]
    secrets = ", ".join(m.secret for m in mounts) or "none"
    mkdirs = f"{q(str(layout.data_dir))} {q(str(layout.cache_dir))}"
    joined = " \\\n  ".join(lines)
    return f"""#!/bin/sh
{SCRIPT_HEADER}
# Regenerate instead of editing: jobhunter container preflight (or jobhunter ctr-script --write).
# Runs one jobhunter command in a one-off container with the run unit's mounts, user namespace
# and path variables. Secrets mounted: {secrets}.
# Usage: jobhunter-ctr mail status | jobhunter-ctr secrets status | jobhunter-ctr db migrate
set -eu

podman_cmd() {{
  if command -v podman >/dev/null 2>&1; then
    podman "$@"
  elif command -v flatpak-spawn >/dev/null 2>&1; then
    flatpak-spawn --host podman "$@"
  elif command -v host-spawn >/dev/null 2>&1; then
    host-spawn podman "$@"
  else
    echo "jobhunter-ctr: podman not found (tried podman, flatpak-spawn --host, host-spawn)" >&2
    exit 127
  fi
}}

if [ "$#" -eq 0 ]; then
  echo "usage: jobhunter-ctr <jobhunter command> [args...]" >&2
  exit 2
fi

# podman refuses a missing bind source; the units do the same in ExecStartPre.
mkdir -p -m 0700 {mkdirs}

tty_flag=
if [ -t 0 ] && [ -t 1 ]; then
  tty_flag=-t
fi

podman_cmd run -i $tty_flag \\
  {joined} \\
  {q(layout.image)} "$@"
"""


def is_ours(path: Path) -> bool:
    try:
        with Path(path).open(encoding="utf-8", errors="replace") as fh:
            head = [fh.readline() for _ in range(3)]
    except OSError:
        return False
    return any(line.rstrip("\n") == SCRIPT_HEADER for line in head)


def default_script_path() -> Path:
    return Path.home() / ".local" / "bin" / "jobhunter-ctr"


def write_script(path: Path, text: str) -> Path:
    """Write the wrapper at ``path`` (0755), atomically. Refuses to replace a file that this
    module did not write (no header line)."""
    path = Path(path)
    if path.exists() and not is_ours(path):
        raise CtrError(f"{path} exists and was not written by jobhunter; move it aside first")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, 0o755)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path
