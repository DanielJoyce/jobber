"""Container mode (specs/018-containers.md, C1).

Switched on only by ``JOBHUNTER_CONTAINER=1`` (set in the image). Never auto-detected: toolbox
and distrobox also have /run/.containerenv and keep the host layout. Read from the environment
at call time so tests can patch it.
"""

from __future__ import annotations

import os

CONTAINER_VAR = "JOBHUNTER_CONTAINER"
PUBLISHED_LOOPBACK_ONLY_VAR = "JOBHUNTER_PUBLISHED_LOOPBACK_ONLY"

CONFIG_DIR = "/config"
DATA_DIR = "/data"
CACHE_DIR = "/cache"
GOOGLE_CLIENT_JSON = "/run/secrets/google_client_secret.json"
SOURCE_LABEL = "container default"


def is_container() -> bool:
    return os.environ.get(CONTAINER_VAR) == "1"


def published_loopback_only() -> bool:
    """The unit publishes the port on host loopback only, so a 0.0.0.0 bind is acceptable.
    Honoured in container mode only."""
    return is_container() and os.environ.get(PUBLISHED_LOOPBACK_ONLY_VAR) == "1"


def refusal(command: str, host_command: str | None = None) -> str:
    """The message for a command that only works on the host."""
    host_command = host_command or f"jobhunter {command}"
    return (
        f"error: `jobhunter {command}` does not run in container mode "
        f"({CONTAINER_VAR}=1). Run it on the host: {host_command}"
    )
