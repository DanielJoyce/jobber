"""Secret resolution and validation (specs/018-containers.md, "``*_FILE`` resolution", C2).

At CLI start, after the ``.env`` files are loaded:

1. For each known secret name ``N`` with ``N_FILE`` set, read that file. ``N`` set in the real
   environment too is a conflict; ``N`` that came from a ``.env`` file loses to the file, with a
   warning.
2. Every known secret, from any source, is validated: surrounding whitespace is stripped; empty
   means unset; whitespace or a control character inside, or a missing, unreadable, empty or
   over 64 KiB file, means invalid.
3. An invalid secret is removed from ``os.environ`` with a one-line warning. Nothing invalid is
   ever sent: httpx puts a header value with a stray CR, LF, space or tab into its exception
   text, which several places log or store.

Messages name variables, files and reasons only; never a value, a prefix or a length.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MAX_FILE_BYTES = 64 * 1024

# Names that exist without any config: the SDK's own variable and the registry/scorer defaults.
DEFAULT_NAMES = (
    "ANTHROPIC_API_KEY",
    "OPENROUTER_API_KEY",
    "OPENAI_COMPAT_API_KEY",
    "LLAMA_API_KEY",
    "USAJOBS_API_KEY",
    "USAJOBS_EMAIL",
)

# State values
ENV = "env"
FILE = "file"
DOTENV = ".env"
UNSET = "unset"
INVALID = "invalid"


@dataclass(frozen=True)
class SecretState:
    name: str
    state: str  # ENV, FILE, DOTENV, UNSET or INVALID
    detail: str = ""  # a path for FILE and DOTENV, a reason for INVALID

    def describe(self) -> str:
        if self.state == UNSET:
            return "unset"
        if self.state == ENV:
            return "set (env)"
        if self.state == INVALID:
            return f"invalid ({self.detail})"
        return f"set ({self.state} {self.detail})"


def clean(value: str) -> tuple[str | None, str | None]:
    """``(value, None)`` when usable, ``(None, None)`` when empty (unset), ``(None, reason)``
    when invalid. The reason never contains any part of the value."""
    stripped = value.strip()
    if not stripped:
        return None, None
    for ch in stripped:
        if ch.isspace():
            return None, "contains whitespace"
        if ord(ch) < 32 or 127 <= ord(ch) < 160:
            return None, "contains a control character"
        if not 33 <= ord(ch) <= 126:
            # httpx encodes header values as ASCII; its error would name the character
            return None, "contains a non-ASCII character"
    return stripped, None


def looks_like_path(raw: str) -> bool:
    """Only an absolute path, ``~...`` or ``./...`` is ever shown, so a key pasted into
    ``N_FILE`` by mistake is not echoed."""
    return raw.startswith(("/", "~", "./"))


def _read_secret_file(path: Path) -> tuple[str | None, str | None]:
    """The raw text of ``path`` or ``(None, reason)``."""
    try:
        with path.open("rb") as fh:
            data = fh.read(MAX_FILE_BYTES + 1)
    except FileNotFoundError:
        return None, "file is missing"
    except OSError:
        return None, "file is unreadable"
    if len(data) > MAX_FILE_BYTES:
        return None, "file is larger than 64 KiB"
    try:
        return data.decode("utf-8-sig"), None
    except UnicodeDecodeError:
        return None, "file is not valid UTF-8"


def known_names(settings: Any | None = None, registry_auth: Iterable[str] = ()) -> list[str]:
    """The default names plus every ``api_key_env`` in ``settings`` and the given registry
    ``key_env``/``email_env`` names, in a stable order without duplicates."""
    names = list(DEFAULT_NAMES)
    if settings is not None:
        scoring = settings.scoring
        for section in (scoring.openai_compat, scoring.openrouter, scoring.local):
            names.append(section.api_key_env)
    names.extend(registry_auth)
    seen: set[str] = set()
    out: list[str] = []
    for name in names:
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out


def registry_auth_names() -> list[str]:
    """``key_env`` and ``email_env`` of every packaged registry row's ``config.auth``."""
    try:
        from importlib import resources

        from ruamel.yaml import YAML

        text = resources.files("jobhunter.sources").joinpath("registry.yaml").read_text("utf-8")
        rows = YAML(typ="safe").load(text)
    except Exception:
        return []
    names: list[str] = []
    for row in rows if isinstance(rows, list) else []:
        auth = ((row or {}).get("config") or {}).get("auth") if isinstance(row, Mapping) else None
        if isinstance(auth, Mapping):
            for key in ("key_env", "email_env"):
                if isinstance(auth.get(key), str):
                    names.append(auth[key])
    return names


def resolve(
    names: Iterable[str],
    real_env: set[str],
    dotenv_origin: Mapping[str, Path],
    warn: Callable[[str], None],
    environ: dict[str, str] | None = None,
) -> list[SecretState]:
    """Resolve ``N_FILE``, validate every name and update ``environ`` (default ``os.environ``).

    ``real_env`` is the set of names that were in the environment before any ``.env`` file was
    loaded; ``dotenv_origin`` maps a name to the ``.env`` file it came from. Never raises.
    """
    env = os.environ if environ is None else environ
    results: list[SecretState] = []
    for name in names:
        file_var = f"{name}_FILE"
        raw_file = env.get(file_var, "").strip()
        current, current_reason = clean(env[name]) if name in env else (None, None)
        if raw_file:
            path = Path(raw_file).expanduser()
            if name in real_env and name in env and (current or current_reason):
                # set in the real environment, valid or not: never pick one silently
                env.pop(name, None)
                reason = f"{name} and {file_var} are both set"
                warn(f"warning: {name} ignored: {reason}")
                results.append(SecretState(name, INVALID, reason))
                continue
            if not looks_like_path(raw_file):
                env.pop(name, None)
                reason = f"value of {file_var} is not a file path"
                warn(f"warning: {name} ignored: {reason}")
                results.append(SecretState(name, INVALID, reason))
                continue
            if name in dotenv_origin and name in env:
                warn(
                    f"warning: {name} in {dotenv_origin[name]} is overridden by {file_var} ({path})"
                )
            text, reason = _read_secret_file(path)
            value = None
            if reason is None:
                value, reason = clean(text or "")
                if value is None and reason is None:
                    reason = "file is empty"
            if value is None:
                env.pop(name, None)
                warn(f"warning: {name} ignored: {reason} ({file_var}={path})")
                results.append(SecretState(name, INVALID, reason or "invalid"))
            else:
                env[name] = value
                results.append(SecretState(name, FILE, str(path)))
            continue
        if name not in env:
            results.append(SecretState(name, UNSET))
        elif current_reason is not None:
            env.pop(name, None)
            warn(f"warning: {name} ignored: value {current_reason}")
            results.append(SecretState(name, INVALID, current_reason))
        elif current is None:
            env.pop(name, None)  # an empty placeholder counts as unset
            results.append(SecretState(name, UNSET))
        else:
            env[name] = current
            if name in dotenv_origin and name not in real_env:
                results.append(SecretState(name, DOTENV, str(dotenv_origin[name])))
            else:
                results.append(SecretState(name, ENV))
    return results


def dotenv_origins(files: Iterable[Path], real_env: set[str]) -> dict[str, Path]:
    """Name -> the first ``.env`` file (in precedence order) that defines it, for names that
    were not in the real environment. Values are read only to learn which keys exist."""
    from dotenv import dotenv_values

    origin: dict[str, Path] = {}
    for path in files:
        try:
            keys = dotenv_values(path).keys()
        except Exception:
            continue
        for key in keys:
            if key not in real_env and key not in origin:
                origin[key] = path
    return origin


def env_file_warnings(files: Iterable[Path]) -> list[str]:
    """One line per ``.env`` file that group or others can read, with the chmod to fix it."""
    notes: list[str] = []
    for path in files:
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
        except OSError:
            continue
        if mode & 0o077:
            notes.append(
                f"warning: {path} is readable by group or others (mode {mode:04o}); "
                f"run: chmod 600 {path}"
            )
    return notes


@dataclass
class Report:
    states: list[SecretState]
    env_files: list[Path]


def startup(
    settings_loader: Callable[[], Any],
    files_loader: Callable[[], list[Path]],
    warn: Callable[[str], None],
) -> Report:
    """The CLI-start sequence: record the real environment, load ``.env`` files, resolve."""
    real_env = set(os.environ)
    files = files_loader()  # .env first: it may set JOBHUNTER_CONFIG or a path variable
    origin = dotenv_origins(files, real_env)
    try:
        settings = settings_loader()
    except Exception:
        settings = None
        warn("warning: config could not be loaded; only the default secret names are checked")
    names = known_names(settings, registry_auth_names())
    return Report(resolve(names, real_env, origin, warn), files)
