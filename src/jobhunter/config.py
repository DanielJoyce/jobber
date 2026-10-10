"""Settings, paths and policy flags (specs/002-architecture.md#configuration).

Four layers, most specific wins: defaults in code, then the TOML file
(``$JOBHUNTER_CONFIG`` or ``$XDG_CONFIG_HOME/jobhunter/config.toml``), then the
``JOBHUNTER_*_DIR``/``_PATH`` environment variables for ``[paths]``, then the ``overrides``
mapping passed by the caller. Nested sections are deep-merged, so overriding one key
leaves its siblings alone. Lists are replaced wholesale, not concatenated.

Default locations follow the XDG base directory spec (``jobhunter.xdg``): user data below
``$XDG_DATA_HOME/jobhunter``, the fetch cache below ``$XDG_CACHE_HOME/jobhunter``. The old
repo-relative locations (``./data``, ``./profile``, ``./resume``) are still used, with a
one-time warning, when they exist and the new location does not, until ``jobhunter
migrate-paths`` moves them.
"""

from __future__ import annotations

import logging
import os
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, ValidationInfo, model_validator

from jobhunter.xdg import cache_home, config_home, data_home, xdg_source

logger = logging.getLogger(__name__)

CONFIG_ENV_VAR = "JOBHUNTER_CONFIG"
CONFIG_FILE_NAME = "config.toml"

PATH_FIELDS = ("data_dir", "cache_dir", "db_path", "profile_dir", "resume_path")
PATH_ENV_VARS = {
    "data_dir": "JOBHUNTER_DATA_DIR",
    "cache_dir": "JOBHUNTER_CACHE_DIR",
    "db_path": "JOBHUNTER_DB_PATH",
    "profile_dir": "JOBHUNTER_PROFILE_DIR",
    "resume_path": "JOBHUNTER_RESUME_PATH",
}
DB_FILE_NAME = "jobhunter.db"
# Where each default lived before the XDG move, relative to the directory jobhunter was run from.
LEGACY_RELATIVE = {
    "data_dir": Path("data"),
    "cache_dir": Path("data/cache"),
    "db_path": Path("data/jobhunter.db"),
    "profile_dir": Path("profile"),
    "resume_path": Path("resume"),
}
LEGACY = "legacy"  # the source label of a field that fell back to the old location
LEGACY_CONTEXT = "legacy_fallback"  # validation-context key; False turns the fallback off


def default_config_path() -> Path:
    return config_home() / CONFIG_FILE_NAME


def legacy_locations(base: Path | None = None) -> dict[str, Path]:
    """The pre-XDG default locations, absolute, relative to ``base`` (default: the cwd)."""
    root = base if base is not None else Path.cwd()
    return {k: root / v for k, v in LEGACY_RELATIVE.items()}


class Paths(BaseModel):
    """Where user files live. Unset fields get XDG defaults (see the module docstring).

    ``data_dir`` holds the database, ``profile_dir``, ``resume_path`` and backups; the three
    derive from it unless set. ``cache_dir`` (the raw fetch cache) is separate, under
    ``$XDG_CACHE_HOME``. ``sources`` says where each value came from.
    """

    model_config = ConfigDict(extra="forbid")

    data_dir: Path = Field(default_factory=data_home)
    cache_dir: Path = Field(default_factory=cache_home)
    db_path: Path = Field(default_factory=lambda: data_home() / DB_FILE_NAME)
    profile_dir: Path = Field(default_factory=lambda: data_home() / "profile")
    resume_path: Path = Field(default_factory=lambda: data_home() / "resume")

    _sources: dict[str, str] = PrivateAttr(default_factory=dict)

    @property
    def sources(self) -> dict[str, str]:
        """Field -> where its value came from: "explicit", "default", "default (XDG_...)",
        "under data_dir" or "legacy"; ``load_settings`` refines "explicit" to "config file",
        "env JOBHUNTER_..." or "override"."""
        return dict(self._sources)

    @model_validator(mode="after")
    def _resolve_defaults(self, info: ValidationInfo) -> Paths:
        context = info.context if isinstance(info.context, dict) else {}
        legacy_ok = bool(context.get(LEGACY_CONTEXT, True))
        explicit = set(self.model_fields_set)
        sources: dict[str, str] = {}

        def put(field: str, value: Path, source: str) -> None:
            object.__setattr__(self, field, value)
            sources[field] = source

        for field in PATH_FIELDS:
            if field in explicit:
                put(field, Path(getattr(self, field)).expanduser(), "explicit")
        if "data_dir" not in explicit:
            put("data_dir", data_home(), xdg_source("data"))
        data_dir = self.data_dir
        derived = sources["data_dir"] if "data_dir" not in explicit else "under data_dir"
        for field, name in (
            ("db_path", DB_FILE_NAME),
            ("profile_dir", "profile"),
            ("resume_path", "resume"),
        ):
            if field not in explicit:
                put(field, data_dir / name, derived)
        if "cache_dir" not in explicit:
            put("cache_dir", cache_home(), xdg_source("cache"))

        if legacy_ok and "data_dir" not in explicit:
            old = legacy_locations()
            for field in ("db_path", "profile_dir", "resume_path", "cache_dir"):
                if field in explicit:
                    continue
                if not getattr(self, field).exists() and old[field].exists():
                    put(field, old[field], LEGACY)
            if sources["db_path"] == LEGACY:
                put("data_dir", old["data_dir"], LEGACY)
        self._sources = sources
        return self


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
    # Packed requests (specs/016): judge this many jobs per request. 1 = one job per request.
    jobs_per_request: int = Field(default=1, ge=1, le=16)
    # A pack is split when its estimated input tokens (chars / 4) would exceed this.
    max_input_tokens_per_request: int = Field(default=40_000, ge=1000)
    est_cost_per_request_usd: float = Field(default=0.0, ge=0)  # 0: use 0.002 per job


class OpenRouter(BaseModel):
    """``screen_scorer = "openrouter:<model-slug>"``: the openai-compat scorer preset (specs/016).

    The resume goes in every request, so providers that retain prompts are excluded by default
    (``provider.data_collection = "deny"``) and structured output is enforced
    (``require_parameters``). Cost comes from the response when OpenRouter reports it, else from
    the public model catalog cached at ``catalog_cache`` (refreshed at most daily).
    """

    model_config = ConfigDict(extra="forbid")

    base_url: str = "https://openrouter.ai/api/v1"
    api_key_env: str = "OPENROUTER_API_KEY"
    title: str = "jobhunter"  # X-Title
    referer: str = ""  # HTTP-Referer; omitted when empty
    provider: dict[str, Any] = Field(
        default_factory=lambda: {"require_parameters": True, "data_collection": "deny"}
    )
    max_concurrency: int = Field(default=2, ge=1)
    json_schema: bool = True
    # Packed requests (specs/016): judge this many jobs per request. 1 = one job per request.
    jobs_per_request: int = Field(default=1, ge=1, le=16)
    # A pack is split when its estimated input tokens (chars / 4) would exceed this.
    max_input_tokens_per_request: int = Field(default=40_000, ge=1000)
    est_cost_per_request_usd: float = Field(default=0.04, ge=0)  # spend-cap estimate
    # Empty: <paths.cache_dir>/openrouter_models.json (filled in by Settings).
    catalog_cache: Path = Path()


class Local(BaseModel):
    """``screen_scorer = "local:<model>"``: a model served from this machine (specs/016).

    ``runtime`` picks the default URL: llama.cpp's ``llama-server`` on 8080, Ollama's
    OpenAI-compatible API on 11434. ``base_url`` overrides it. Local inference is CPU-bound,
    so requests run one at a time and wait much longer than a cloud call.
    """

    model_config = ConfigDict(extra="forbid")

    runtime: Literal["llama.cpp", "ollama"] = "llama.cpp"
    base_url: str = ""  # empty: the runtime's loopback default
    # Env var holding the server's API key (set by scripts/setup-local-llm.sh in ~/.env). When
    # it is unset, no Authorization header is sent: Ollama and key-less servers need none.
    api_key_env: str = "LLAMA_API_KEY"
    timeout_s: float = Field(default=900.0, gt=0)
    max_concurrency: int = Field(default=1, ge=1)
    json_schema: bool = True
    # Packed requests (specs/016): judge this many jobs per request. 1 = one job per request.
    jobs_per_request: int = Field(default=1, ge=1, le=16)
    # A pack is split when its estimated input tokens (chars / 4) would exceed this.
    max_input_tokens_per_request: int = Field(default=40_000, ge=1000)


class Scoring(BaseModel):
    model_config = ConfigDict(extra="forbid")

    screen_scorer: str = "anthropic:claude-haiku-4-5"
    deep_scorer: str = "anthropic:claude-opus-5"
    daily_cap_usd: float = 2.0
    weekly_cap_usd: float = 10.0
    deep_shortlist: int = 40
    # A job at an employer that rejected you for a different role within this many days is
    # still scored, with that fact in the prompt (specs/006 "Employer rejections").
    employer_rejection_days: int = Field(default=90, ge=0)
    openai_compat: OpenAICompat = Field(default_factory=OpenAICompat)
    openrouter: OpenRouter = Field(default_factory=OpenRouter)
    local: Local = Field(default_factory=Local)


class Mail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    alerts_address: str = "<you>+jobs@gmail.com"
    label: str = "jobhunter/alerts"
    fallback_sender_domains: list[str] = Field(default_factory=list)
    # Google OAuth client secrets JSON (Desktop app), kept outside the repo. The env var
    # JOBHUNTER_GOOGLE_CLIENT_SECRETS overrides it.
    client_secrets_path: Path = Field(
        default_factory=lambda: config_home() / "google_client_secret.json"
    )


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

    @model_validator(mode="after")
    def _derive_catalog_cache(self) -> Settings:
        router = self.scoring.openrouter
        if "catalog_cache" not in router.model_fields_set:
            catalog = self.paths.cache_dir / "openrouter_models.json"
            object.__setattr__(router, "catalog_cache", catalog)
        return self

    @property
    def user_agent(self) -> str:
        return f"jobhunter/0.1 (personal job search; {self.fetch.user_agent_contact})"


def resolve_path(path: str | Path, base: Path | None = None) -> Path:
    """Expand ``~`` and resolve a relative path against ``base`` (default: the cwd)."""
    p = Path(path).expanduser()
    if p.is_absolute():
        return p
    return (base if base is not None else Path.cwd()) / p


def config_file(config_path: Path | None) -> Path:
    if config_path is not None:
        return Path(config_path).expanduser()
    env_value = os.environ.get(CONFIG_ENV_VAR)
    if env_value:
        return Path(env_value).expanduser()
    return default_config_path()


def config_file_source() -> str:
    """Where the config file location came from, for ``jobhunter paths``."""
    if os.environ.get(CONFIG_ENV_VAR):
        return f"env {CONFIG_ENV_VAR}"
    return xdg_source("config")


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


_warned_legacy: set[str] = set()


def _warn_legacy_once(paths: Paths) -> None:
    old = sorted(str(getattr(paths, f)) for f, src in paths.sources.items() if src == LEGACY)
    key = "\n".join(old)
    if not old or key in _warned_legacy:
        return
    _warned_legacy.add(key)
    logger.warning(
        "using the old repo-relative location(s): %s. Defaults moved to %s and %s; "
        "run `jobhunter migrate-paths` (a dry run) to see how to move them.",
        ", ".join(old),
        data_home(),
        cache_home(),
    )


def load_settings(
    config_path: Path | None = None,
    overrides: Mapping[str, Any] | None = None,
    *,
    legacy_fallback: bool = True,
) -> Settings:
    """Build Settings from code defaults, the TOML file, the environment, then ``overrides``.

    An explicit ``config_path`` wins over ``$JOBHUNTER_CONFIG``, which wins over the
    default location. A missing file is not an error. Unknown keys raise
    ``pydantic.ValidationError`` naming the key (e.g. ``fetch.bogus``). The ``[paths]``
    keys can also come from ``JOBHUNTER_DATA_DIR`` and friends (``PATH_ENV_VARS``), which
    beat the file. ``legacy_fallback=False`` ignores the old repo-relative locations
    (``migrate-paths`` uses it to find where files should go).
    """
    path = config_file(config_path)
    file_data: dict[str, Any] = {}
    if path.is_file():
        with path.open("rb") as fh:
            file_data = tomllib.load(fh)
    env_paths = {f: v for f, var in PATH_ENV_VARS.items() if (v := os.environ.get(var))}
    merged = _deep_merge(file_data, {"paths": env_paths} if env_paths else {})
    merged = _deep_merge(merged, overrides or {})
    # Validated (not default-factory built) so the legacy_fallback context reaches Paths.
    merged.setdefault("paths", {})
    settings = Settings.model_validate(merged, context={LEGACY_CONTEXT: legacy_fallback})
    labels = settings.paths._sources
    for field in PATH_FIELDS:
        if field in ((overrides or {}).get("paths") or {}):
            labels[field] = "override"
        elif field in env_paths:
            labels[field] = f"env {PATH_ENV_VARS[field]}"
        elif field in (file_data.get("paths") or {}):
            labels[field] = "config file"
    if legacy_fallback:
        _warn_legacy_once(settings.paths)
    return settings
