"""Reaching host podman from wherever jobhunter runs (specs/018-containers.md, C7 and C13).

The probe chain is shared by ``mail auth --podman-secret`` (C7) and ``container preflight``
(C13): plain ``podman``, then the host-escape commands of toolbox and distrobox. Only secret
*names* are ever read back (``podman secret ls --format {{.Name}}``); no function here reads,
prints or passes a secret value.
"""

from __future__ import annotations

import shutil
import subprocess

PROBE_TIMEOUT_SECONDS = 20
# Tried in order; the first whose `podman secret ls` succeeds wins. The last three are the
# host-escape chain for toolbox and distrobox (flatpak-spawn, host-spawn, distrobox-host-exec).
PODMAN_CHAINS: tuple[tuple[str, ...], ...] = (
    ("podman",),
    ("flatpak-spawn", "--host", "podman"),
    ("host-spawn", "podman"),
    ("distrobox-host-exec", "podman"),
)
TRIED = "podman, flatpak-spawn --host podman, host-spawn podman, distrobox-host-exec podman"


def secret_names(prefix: list[str]) -> list[str] | None:
    """Names from `podman secret ls`, or None when that command does not work."""
    try:
        done = subprocess.run(
            [*prefix, "secret", "ls", "--format", "{{.Name}}"],
            capture_output=True,
            text=True,
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return [line.strip() for line in done.stdout.splitlines() if line.strip()]


def probe() -> tuple[list[str], list[str]] | None:
    """The working podman command prefix and the existing secret names, or None."""
    for chain in PODMAN_CHAINS:
        if shutil.which(chain[0]) is None:
            continue
        if chain[0] == "distrobox-host-exec" and shutil.which("host-spawn") is None:
            # Without host-spawn it would download one from GitHub and sudo-install it, with
            # no prompt, when stdout is captured. Never trigger that from a probe.
            continue
        names = secret_names(list(chain))
        if names is not None:
            return list(chain), names
    return None
