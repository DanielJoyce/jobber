"""XDG base directories for jobhunter (specs/002-architecture.md#where-files-live).

Config lives in ``$XDG_CONFIG_HOME/jobhunter`` (default ``~/.config/jobhunter``), user data
(resume, profile, database, backups) in ``$XDG_DATA_HOME/jobhunter`` (default
``~/.local/share/jobhunter``) and the fetch cache in ``$XDG_CACHE_HOME/jobhunter`` (default
``~/.cache/jobhunter``). Per the XDG spec a relative or empty ``$XDG_*`` value is ignored.
Everything is read from the environment at call time, never at import, so tests that patch
``HOME`` or ``XDG_*`` see the patched values.
"""

from __future__ import annotations

import os
from pathlib import Path

from jobhunter import container

APP_DIR = "jobhunter"

CONFIG_HOME_VAR = "XDG_CONFIG_HOME"
DATA_HOME_VAR = "XDG_DATA_HOME"
CACHE_HOME_VAR = "XDG_CACHE_HOME"

# kind -> (environment variable, default below $HOME)
_BASES = {
    "config": (CONFIG_HOME_VAR, Path(".config")),
    "data": (DATA_HOME_VAR, Path(".local/share")),
    "cache": (CACHE_HOME_VAR, Path(".cache")),
}


def xdg_base(kind: str) -> Path:
    """The XDG base directory for ``kind`` ("config", "data" or "cache")."""
    var, fallback = _BASES[kind]
    value = os.environ.get(var, "")
    if value and Path(value).is_absolute():
        return Path(value)
    return Path.home() / fallback


def config_home() -> Path:
    if container.is_container():
        return Path(container.CONFIG_DIR)
    return xdg_base("config") / APP_DIR


def data_home() -> Path:
    if container.is_container():
        return Path(container.DATA_DIR)
    return xdg_base("data") / APP_DIR


def cache_home() -> Path:
    if container.is_container():
        return Path(container.CACHE_DIR)
    return xdg_base("cache") / APP_DIR


def mkdir_private(path: Path) -> Path:
    """Create ``path`` and any missing parents. Every directory created here is owner-only
    (0700); existing ones are left as they are. User data (database, profile, resume,
    backups) lives below these, and the home directory itself is often world-readable."""
    path = Path(path)
    missing: list[Path] = []
    probe = path
    while not probe.exists() and probe.parent != probe:
        missing.append(probe)
        probe = probe.parent
    for directory in reversed(missing):
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            continue  # created by another process meanwhile
        os.chmod(directory, 0o700)  # mkdir's mode is masked by the umask
    return path


def xdg_source(kind: str) -> str:
    """How the base for ``kind`` was chosen: the ``$XDG_*`` variable or the home default."""
    if container.is_container():
        return container.SOURCE_LABEL
    var, _ = _BASES[kind]
    value = os.environ.get(var, "")
    return f"default ({var})" if value and Path(value).is_absolute() else "default"
