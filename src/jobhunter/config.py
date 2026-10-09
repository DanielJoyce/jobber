"""Settings, paths and policy flags (specs/002-architecture.md#configuration).

Three layers, most specific wins: defaults in code, then the TOML file
(``$JOBHUNTER_CONFIG`` or ``~/.config/jobhunter/config.toml``), then the ``overrides``
mapping passed by the caller. Nested sections are deep-merged, so overriding one key
leaves its siblings alone. Lists are replaced wholesale, not concatenated.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

CONFIG_ENV_VAR = "JOBHUNTER_CONFIG"
DEFAULT_CONFIG_PATH = Path("~/.config/jobhunter/config.toml")


class Paths(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data_dir: Path = Path("data")
    cache_dir: Path = Path("data/cache")
    db_path: Path = Path("data/jobhunter.db")
    profile_dir: Path = Path("profile")
    resume_path: Path = Path("resume")


class Fetch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    respect_robots: bool = True
    default_rps: float = 0.2
    global_concurrency: int = 8
    user_agent_contact: str = "<you>@example.com"
    timeout_s: float = 20.0
    max_retries: int = 5


class OpenAICompat(BaseModel):
    """A generic OpenAI-compatible endpoint (``screen_scorer = "openai-compat:<model>"``).

    The API key is read from the environment variable named by ``api_key_env``; an empty name
    means no Authorization header (some local servers). Prices default to 0, meaning unknown.
    """

    model_config = ConfigDict(extra="forbid")

    base_url: str = "http://localhost:8000"
    api_key_env: str = "OPENAI_COMPAT_API_KEY"
    input_usd_per_mtok: float = Field(default=0.0, ge=0)
    output_usd_per_mtok: float = Field(default=0.0, ge=0)
    max_concurrency: int = Field(default=2, ge=1)
    json_schema: bool = True  # try response_format json_schema first


class Scoring(BaseModel):
    model_config = ConfigDict(extra="forbid")

    screen_scorer: str = "anthropic:claude-haiku-4-5"
    deep_scorer: str = "anthropic:claude-opus-5"
    daily_cap_usd: float = 2.0
    weekly_cap_usd: float = 10.0
    deep_shortlist: int = 40
    openai_compat: OpenAICompat = Field(default_factory=OpenAICompat)


class Mail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    alerts_address: str = "<you>+jobs@gmail.com"
    label: str = "jobhunter/alerts"
    fallback_sender_domains: list[str] = Field(default_factory=list)


class Console(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str = "127.0.0.1"
    port: int = 8808


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paths: Paths = Field(default_factory=Paths)
    fetch: Fetch = Field(default_factory=Fetch)
    scoring: Scoring = Field(default_factory=Scoring)
    mail: Mail = Field(default_factory=Mail)
    console: Console = Field(default_factory=Console)

    @property
    def user_agent(self) -> str:
        return f"jobhunter/0.1 (personal job search; {self.fetch.user_agent_contact})"


def resolve_path(path: str | Path, base: Path | None = None) -> Path:
    """Expand ``~`` and resolve a relative path against ``base`` (default: the cwd)."""
    p = Path(path).expanduser()
    if p.is_absolute():
        return p
    return (base if base is not None else Path.cwd()) / p


def _config_file(config_path: Path | None) -> Path:
    if config_path is not None:
        return Path(config_path).expanduser()
    env_value = os.environ.get(CONFIG_ENV_VAR)
    if env_value:
        return Path(env_value).expanduser()
    return DEFAULT_CONFIG_PATH.expanduser()


def _deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = dict(base)
    for key, value in override.items():
        current = merged.get(key)
        if isinstance(value, Mapping) and isinstance(current, Mapping):
            merged[key] = _deep_merge(current, value)
        else:
            merged[key] = value
    return merged


ENV_FILE_VAR = "JOBHUNTER_ENV_FILE"


def env_files() -> list[Path]:
    """Candidate .env files, highest precedence first.

    ``$JOBHUNTER_ENV_FILE``, then ``./.env`` in the working directory, then ``~/.env``.
    """
    files: list[Path] = []
    if explicit := os.environ.get(ENV_FILE_VAR):
        files.append(Path(explicit).expanduser())
    files += [Path.cwd() / ".env", Path.home() / ".env"]
    return files


def load_env_files() -> list[Path]:
    """Load secrets such as ``USAJOBS_API_KEY`` from .env files into ``os.environ``.

    Real environment variables always win, and an earlier file wins over a later one,
    because nothing already set is overridden. Returns the files that were read.
    Values are never logged.
    """
    from dotenv import load_dotenv

    loaded: list[Path] = []
    for path in env_files():
        if path.is_file():
            load_dotenv(path, override=False)
            loaded.append(path)
    return loaded


def load_settings(
    config_path: Path | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> Settings:
    """Build Settings from code defaults, the TOML file, then ``overrides``.

    An explicit ``config_path`` wins over ``$JOBHUNTER_CONFIG``, which wins over the
    default location. A missing file is not an error. Unknown keys raise
    ``pydantic.ValidationError`` naming the key (e.g. ``fetch.bogus``).
    """
    path = _config_file(config_path)
    file_data: dict[str, Any] = {}
    if path.is_file():
        with path.open("rb") as fh:
            file_data = tomllib.load(fh)
    merged = _deep_merge(file_data, overrides or {})
    return Settings.model_validate(merged)
