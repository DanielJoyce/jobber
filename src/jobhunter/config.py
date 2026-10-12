"""Settings, paths and policy flags (specs/002-architecture.md#configuration).

Four layers, most specific wins: defaults in code, then the TOML file
(``$JOBHUNTER_CONFIG`` or ``$XDG_CONFIG_HOME/jobhunter/config.toml``), then the
``JOBHUNTER_*_DIR``/``_PATH`` environment variables for ``[paths]``, then the ``overrides``
mapping passed by the caller. Nested sections are deep-merged, so overriding one key
leaves its siblings alone. Lists are replaced wholesale, not concatenated.

Default locations follow the XDG base directory spec (``jobhunter.xdg``): user data below
``$XDG_DATA_HOME/jobhunter``, the fetch cache below ``$XDG_CACHE_HOME/jobhunter``. There is no
fallback to the old repo-relative locations (``./data``, ``./profile``, ``./resume``): an
explicit setting wins, otherwise the XDG default. ``jobhunter.legacy_data`` stops commands
while an unmigrated old database exists, and ``jobhunter migrate-paths`` moves it.
"""

from __future__ import annotations

import difflib
import hashlib
import logging
import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field as dc_field
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator

from jobhunter import container
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


def default_config_path() -> Path:
    return config_home() / CONFIG_FILE_NAME


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
        """Field -> where its value came from: "explicit", "default", "default (XDG_...)" or
        "under data_dir"; ``load_settings`` refines "explicit" to "config file",
        "env JOBHUNTER_..." or "override"."""
        return dict(self._sources)

    @model_validator(mode="after")
    def _resolve_defaults(self) -> Paths:
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


# Scorer spec providers, the one canonical list (scoring.scorers and scoring.decisions use it).
DECISIONS_PROVIDERS = ("jev", "decisions")
CHAT_PROVIDERS = ("openrouter", "openai-compat", "local")
SCORER_PROVIDERS = ("anthropic", *CHAT_PROVIDERS, *DECISIONS_PROVIDERS)


def check_scorer_spec(spec: str, field: str = "scorer", *, anthropic_only: bool = False) -> None:
    """Raise ValueError with an actionable message when ``spec`` is not a usable scorer spec.

    Bare names are rejected: downstream code keys on the ``anthropic:`` prefix, so a bare
    model name used to slip through and break the batch path.
    """
    provider, sep, model = spec.partition(":")
    known = ", ".join(SCORER_PROVIDERS)
    inner, inner_sep, rest = model.partition(":")
    if not sep:
        raise ValueError(
            f'{field} "{spec}": no provider; write "anthropic:{spec}" (known: {known})'
        )
    if provider not in SCORER_PROVIDERS:
        if inner_sep and inner in DECISIONS_PROVIDERS and rest:
            hint = f'; use "{inner}:{rest}"'
        elif near := difflib.get_close_matches(provider, SCORER_PROVIDERS, n=1):
            hint = f"; did you mean {near[0]!r}?"
        else:
            hint = ""
        raise ValueError(f'{field} "{spec}": unknown provider {provider!r}{hint} (known: {known})')
    if not model.strip():
        raise ValueError(f'{field} "{spec}": no model after "{provider}:"')
    if anthropic_only and provider != "anthropic":
        raise ValueError(f'{field} "{spec}": the deep pass needs "anthropic:<model>"')
    if provider == "anthropic":
        # Lazy: scorers imports this module. A model missing from the price table would be
        # reserved and recorded at the wrong price, so it is refused up front (bug df51dff).
        from jobhunter.scoring.scorers import PRICING_PER_MTOK

        if model not in PRICING_PER_MTOK:
            listed = ", ".join(sorted(PRICING_PER_MTOK))
            raise ValueError(
                f'{field} "{spec}": no price for {model!r}, so spend caps cannot see its cost '
                f"(priced: {listed})"
            )
    # OpenRouter slugs are author/model, so a leading provider name is a mistake. Local and
    # openai-compat model names are free-form ("jev:latest" can be legitimate).
    if provider == "openrouter" and inner_sep and inner in SCORER_PROVIDERS:
        if inner in DECISIONS_PROVIDERS and rest:
            raise ValueError(
                f'{field} "{spec}": {inner.capitalize()} is its own provider; use '
                f'"{inner}:{rest}" (it still uses OPENROUTER_API_KEY)'
            )
        raise ValueError(f'{field} "{spec}": the model name starts with the provider {inner!r}')


# The value an invalid ``models.toml`` spending key leaves in its field. It is not a usable spec
# (``check_scorer_spec`` refuses the provider), so a path that forgot ``scorer_problems`` still
# cannot build a scorer from it, and nothing falls back to another provider.
BLOCKED_SPEC = "blocked:models.toml"


def blocked_message(field: str, reason: str = "") -> str:
    """The one-line message for a blocked spending key (shown in the run summary)."""
    why = f": {reason}" if reason else ""
    return f"models.toml {field} is invalid{why} (see /models or `jobhunter models show`)"


class Scoring(BaseModel):
    model_config = ConfigDict(extra="forbid")

    screen_scorer: str = "anthropic:claude-haiku-5-5"
    deep_scorer: str = "anthropic:claude-opus-5-5"
    daily_cap_usd: float = 2.0
    weekly_cap_usd: float = 10.0
    deep_shortlist: int = 40
    # A job at an employer that rejected you for a different role within this many days is
    # still scored, with that fact in the prompt (specs/006 "Employer rejections").
    employer_rejection_days: int = Field(default=90, ge=0)
    openai_compat: OpenAICompat = Field(default_factory=OpenAICompat)
    openrouter: OpenRouter = Field(default_factory=OpenRouter)
    local: Local = Field(default_factory=Local)

    # {field: message} for a scorer that ``models.toml`` sets to an invalid value, or every
    # scorer when the file cannot be read (specs/020 Storage). Filled by ``load_settings``.
    _blocked: dict[str, str] = PrivateAttr(default_factory=dict)

    def scorer_problems(self) -> dict[str, str]:
        """{field: message} for each scorer that must not run: a malformed spec, or one that
        ``models.toml`` blocks (fail closed: an invalid spending key is never replaced by
        config.toml's value or the default). Loading never fails on these: the run stage,
        console and CLI report them where they matter (scoring fails soft)."""
        out: dict[str, str] = {}
        for name, only in (("screen_scorer", False), ("deep_scorer", True)):
            if name in self._blocked:
                out[name] = self._blocked[name]
                continue
            if getattr(self, name) == BLOCKED_SPEC:
                out[name] = blocked_message(name)
                continue
            try:
                check_scorer_spec(getattr(self, name), name, anthropic_only=only)
            except ValueError as exc:
                out[name] = str(exc)
        return out


class Mail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    alerts_address: str = "<you>+jobs@gmail.com"
    label: str = "jobhunter/alerts"
    fallback_sender_domains: list[str] = Field(default_factory=list)
    # Exact sender addresses (``from:<address>`` filters), for sites that also send personal mail
    # and so cannot be routed by domain, e.g. LinkedIn's job-alert sender (specs/019 S9).
    fallback_senders: list[str] = Field(default_factory=list)
    # Google OAuth client secrets JSON (Desktop app), kept outside the repo. The env var
    # JOBHUNTER_GOOGLE_CLIENT_SECRETS overrides it.
    client_secrets_path: Path = Field(
        default_factory=lambda: (
            Path(container.GOOGLE_CLIENT_JSON)
            if container.is_container()
            else config_home() / "google_client_secret.json"
        )
    )


class Console(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str = "127.0.0.1"
    port: int = 8808
    # The port the browser connects to, when it differs from ``port`` (a container publishing
    # 127.0.0.1:8808 to another inside port). The extension API accepts only a loopback Host on
    # this port (specs/017 "Local API security"). None: ``port``.
    public_port: int | None = Field(default=None, ge=1, le=65535)


class Capture(BaseModel):
    """The extension's Send to jobhunter (specs/017 phase 1e)."""

    model_config = ConfigDict(extra="forbid")

    # Hosts where the extension never reads a page (checked in the worker before injecting).
    # A suffix match: "example.com" covers "jobs.example.com".
    disabled_hosts: list[str] = Field(default_factory=list)


class Apply(BaseModel):
    """Assisted apply drafting (specs/017 "Targeted resume", "CLI runner").

    ``runner`` picks what a Generate click runs: ``cli`` (the Claude Code CLI on the user's
    subscription, the default) or ``api`` (the ``anthropic`` SDK on any credential it accepts:
    ``ANTHROPIC_API_KEY``, ``ANTHROPIC_AUTH_TOKEN``, a profile; see ``scorers.credential_for``).
    With ``cli``, a failed run only *offers* the API run; it never switches by itself.
    Two model keys because a CLI alias and an API model id are different names.
    ``daily_cap_usd`` caps packet API spend only and is separate from ``[scoring]``'s caps.
    """

    model_config = ConfigDict(extra="forbid")

    runner: Literal["cli", "api"] = "cli"
    cli_model: str = "opus"
    api_model: str = "claude-opus-5-5"
    effort: Literal["low", "medium", "high"] = "medium"
    daily_cap_usd: float = Field(default=1.00, ge=0)
    # Advisory Haiku pass: do the cited lines support each generated line? (never clears a
    # badge). Haiku gets no effort setting on either runner (Haiku 4.5 rejects one); on the API
    # the check runs with thinking off (scorers.thinking_off).
    entailment_check: bool = True
    cli_entailment_model: str = "haiku"
    api_entailment_model: str = "claude-haiku-5-5"


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paths: Paths = Field(default_factory=Paths)
    fetch: Fetch = Field(default_factory=Fetch)
    scoring: Scoring = Field(default_factory=Scoring)
    apply: Apply = Field(default_factory=Apply)
    mail: Mail = Field(default_factory=Mail)
    console: Console = Field(default_factory=Console)
    capture: Capture = Field(default_factory=Capture)

    # The models.toml layers this was built with (``load_layers``); None when built directly.
    _models: Any = PrivateAttr(default=None)

    @property
    def models_layers(self) -> Layers | None:
        """How ``load_settings`` layered models.toml into this, for ``/models``, ``jobhunter
        models`` and the drafting notice. None for Settings built without ``load_settings``."""
        return self._models

    @property
    def models_notices(self) -> list[str]:
        """One line per [apply] key in models.toml that was invalid and fell back."""
        return list(self._models.notices) if self._models is not None else []

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

    ``$JOBHUNTER_ENV_FILE``, then ``./.env`` in the working directory, then ``~/.env``. In
    container mode only ``$JOBHUNTER_ENV_FILE``.
    """
    files: list[Path] = []
    if explicit := os.environ.get(ENV_FILE_VAR):
        files.append(Path(explicit).expanduser())
    if container.is_container():
        return files  # container mode: only the explicit file (specs/018 C1)
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


# ─── models.toml (specs/020-model-selection.md#storage) ─────────────────────

MODELS_FILE_NAME = "models.toml"
# The only keys models.toml may hold, with the catalog use each model key is judged under
# (None: the field's own type is the whole check). Caps, api_key_env and endpoints stay in
# config.toml.
MODEL_KEYS: dict[str, dict[str, str | None]] = {
    "scoring": {"screen_scorer": "screen", "deep_scorer": "deep"},
    "apply": {
        "runner": None,
        "cli_model": "draft_cli",
        "api_model": "draft_api",
        "effort": None,
        "entailment_check": None,
        "cli_entailment_model": "check_cli",
        "api_entailment_model": "check_api",
    },
}
# Keys that spend on their own (the nightly run, re-scores, the deep pass): invalid means
# blocked. The [apply] keys fall back instead: every draft is a click that shows its cost.
SPENDING_KEYS = ("screen_scorer", "deep_scorer")
_CHOICES = {"runner": ("cli", "api"), "effort": ("low", "medium", "high")}
# A value may be echoed in a message only when it looks like a scorer spec or model id
# (019 B: never a value except SHOWN_VALUES), and never when it looks like a key.
_SHOWABLE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}")
# A key prefix at the start or after a provider or path separator ("openrouter:sk-or-...").
_SECRET_LOOKING = re.compile(
    r"(?i)(?:^|[:/])(?:sk-|sk_|key-|ghp_|xox|bearer)|secret|token|password"
)


def shown_value(value: object) -> str | None:
    """``value`` when it may appear in a message (a scorer spec or model id), else None."""
    if not isinstance(value, str) or not _SHOWABLE.fullmatch(value):
        return None
    if _SECRET_LOOKING.search(value):
        return None
    return value


def models_file(data_dir: Path) -> Path:
    """``<data dir>/models.toml``: the console-managed layer over config.toml (specs/020)."""
    return resolve_path(data_dir) / MODELS_FILE_NAME


@dataclass(frozen=True)
class ModelsProblem:
    """One thing wrong in models.toml. ``kind``: "broken" (the file cannot be read as TOML),
    "invalid" (a known key with a bad value: ignored, and blocked when it spends) or "unknown"
    (a key this version does not know: ignored with a warning). The message never holds a
    value other than a scorer spec or model id."""

    kind: str
    key: str | None  # "scoring.screen_scorer"; None for the whole file
    message: str


@dataclass(eq=False)  # merged._models points back here: no recursive field compare
class Layers:
    """What ``load_layers`` found: each layer, what it set, what is wrong, and the result.

    ``base`` is built from the code defaults, config.toml, the environment and ``overrides``,
    without models.toml. ``layer`` is {section: {key: value}} for the valid keys models.toml
    set, ``invalid`` the same for the known keys it set to a bad value (raw, not for display),
    ``blocked`` {field: message} for the spending keys that are blocked, ``notices`` one line
    per [apply] key that fell back. ``config_keys`` are the (section, key) pairs config.toml
    set, for "where did this value come from". ``digest`` is the SHA-256 of the file's bytes
    (of b"" when there is no file), which every write must quote.
    """

    path: Path
    exists: bool
    digest: str
    base: Settings
    merged: Settings
    layer: dict[str, dict[str, Any]] = dc_field(default_factory=dict)
    invalid: dict[str, dict[str, Any]] = dc_field(default_factory=dict)
    problems: list[ModelsProblem] = dc_field(default_factory=list)
    blocked: dict[str, str] = dc_field(default_factory=dict)
    notices: list[str] = dc_field(default_factory=list)
    config_keys: frozenset[tuple[str, str]] = frozenset()
    override_keys: frozenset[tuple[str, str]] = frozenset()

    def __eq__(self, other: object) -> bool:
        # Not base/merged: merged._models points back here, and Settings compares its private
        # state, so comparing them would recurse. The file's content decides equality.
        if not isinstance(other, Layers):
            return NotImplemented
        mine = (self.path, self.exists, self.digest, self.layer, self.invalid, self.problems)
        theirs = (other.path, other.exists, other.digest, other.layer, other.invalid,
                  other.problems)  # fmt: skip
        return mine == theirs and (self.blocked, self.notices, self.config_keys) == (
            other.blocked,
            other.notices,
            other.config_keys,
        )

    __hash__ = None  # type: ignore[assignment]

    @property
    def broken(self) -> bool:
        return any(p.kind == "broken" for p in self.problems)

    def source(self, section: str, key: str) -> str:
        """Where the effective value of ``section.key`` came from: "override", "models.toml",
        "models.toml (invalid)", "config.toml" or "default"."""
        if (section, key) in self.override_keys:
            return "override"
        if key in self.layer.get(section, {}):
            return "models.toml"
        if key in self.invalid.get(section, {}) or (section == "scoring" and key in self.blocked):
            return "models.toml (invalid)"
        if (section, key) in self.config_keys:
            return "config.toml"
        return "default"


def check_model_key(section: str, key: str, value: object) -> str | None:
    """Why ``value`` is not valid for ``section.key`` in models.toml, or None.

    The field's type, the scorer spec syntax (the deep pass Anthropic only), an Anthropic
    model's price-table entry and the catalog's per-use rules (``catalog.use_rule_problem``).
    Never echoes a value that is not a scorer spec or model id.
    """
    use = MODEL_KEYS[section][key]
    if key == "entailment_check":
        return None if isinstance(value, bool) else "must be true or false"
    if key in _CHOICES:
        choices = _CHOICES[key]
        if isinstance(value, str) and value in choices:
            return None
        return "must be one of " + ", ".join(f'"{c}"' for c in choices)
    if not isinstance(value, str):
        return "must be a string"
    if shown_value(value) is None:  # looks like a key, or has characters no model id has
        return "not a usable model name"
    assert use is not None
    from jobhunter.scoring import catalog  # lazy: catalog imports this module

    reason = catalog.use_rule_problem(use, value, key)
    if reason is None:
        return None
    if shown_value(value) is None:
        return "not a usable model name"
    return reason


_WARNED: set[tuple[str, str]] = set()


def _warn_once(path: Path, digest: str, problems: list[ModelsProblem]) -> None:
    """Log the problems in one version of the file once per process, not on every load."""
    if not problems or (str(path), digest) in _WARNED:
        return
    _WARNED.add((str(path), digest))
    for p in problems:
        logger.warning("%s: %s", path, p.message)


def read_models_file(path: Path) -> tuple[bool, str, dict[str, Any] | None, list[ModelsProblem]]:
    """(exists, digest, parsed table or None when broken, problems). Never raises."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return False, hashlib.sha256(b"").hexdigest(), {}, []
    except OSError as exc:
        msg = f"{MODELS_FILE_NAME} cannot be read ({type(exc).__name__})"
        return True, "", None, [ModelsProblem("broken", None, msg)]
    digest = hashlib.sha256(raw).hexdigest()
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        where = ""
        if isinstance(exc, tomllib.TOMLDecodeError) and getattr(exc, "lineno", None):
            where = f" at line {exc.lineno}"
        msg = f"{MODELS_FILE_NAME} is not valid TOML{where}"
        return True, digest, None, [ModelsProblem("broken", None, msg)]
    return True, digest, data, []


def _key_label(name: object) -> str:
    text = str(name)
    if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", text) and not _SECRET_LOOKING.search(text):
        return text
    return "<a key>"


def _split_layer(
    data: dict[str, Any] | None,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], list[ModelsProblem], set[str]]:
    """(valid, invalid, problems, sections that cannot be read) from a parsed models.toml."""
    valid: dict[str, dict[str, Any]] = {}
    invalid: dict[str, dict[str, Any]] = {}
    problems: list[ModelsProblem] = []
    unreadable: set[str] = set()
    if data is None:
        return valid, invalid, problems, set(MODEL_KEYS)
    for section, table in data.items():
        if section not in MODEL_KEYS:
            label = _key_label(section)
            problems.append(
                ModelsProblem("unknown", label, f"unknown section or key {label!r}; ignored")
            )
            continue
        if not isinstance(table, dict):
            unreadable.add(section)
            problems.append(
                ModelsProblem("invalid", section, f"[{section}] must be a table; ignored")
            )
            continue
        for key, value in table.items():
            dotted = f"{section}.{_key_label(key)}"
            if key not in MODEL_KEYS[section]:
                problems.append(
                    ModelsProblem("unknown", dotted, f"unknown key {dotted!r}; ignored")
                )
                continue
            reason = check_model_key(section, key, value)
            if reason is None:
                valid.setdefault(section, {})[key] = value
            else:
                invalid.setdefault(section, {})[key] = value
                problems.append(ModelsProblem("invalid", dotted, f"{dotted}: {reason}"))
    return valid, invalid, problems, unreadable


def _keys(table: Mapping[str, Any]) -> frozenset[tuple[str, str]]:
    return frozenset(
        (section, key)
        for section, inner in table.items()
        if isinstance(inner, Mapping)
        for key in inner
    )


def load_layers(
    config_path: Path | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> Layers:
    """Build Settings layer by layer: defaults, config.toml, the environment, models.toml,
    then ``overrides`` (specs/020 Storage).

    The data dir is settled first, from everything but models.toml, and
    ``<data dir>/models.toml`` is read from there. Each of its keys is validated on its own; a
    valid key applies. An unknown key is ignored with a warning. An invalid ``screen_scorer`` or
    ``deep_scorer``, or a file that is not TOML, **blocks** that use: the field is set to
    ``BLOCKED_SPEC`` and ``scorer_problems()`` reports it, so nothing spends on config.toml's
    value or the default instead. An invalid [apply] key is ignored, and config.toml's value or
    the default applies, with a line in ``notices``. ``overrides`` (tests) beat the file.
    """
    path = config_file(config_path)
    file_data: dict[str, Any] = {}
    if path.is_file():
        with path.open("rb") as fh:
            file_data = tomllib.load(fh)
    env_paths = {f: v for f, var in PATH_ENV_VARS.items() if (v := os.environ.get(var))}
    lower = _deep_merge(file_data, {"paths": env_paths} if env_paths else {})
    overrides = overrides or {}
    base = _validate(_deep_merge(lower, overrides), file_data, env_paths, overrides)

    mpath = models_file(base.paths.data_dir)
    exists, digest, data, problems = read_models_file(mpath)
    valid, invalid, key_problems, unreadable = _split_layer(data)
    problems = problems + key_problems
    override_keys = _keys(overrides)
    config_keys = _keys(file_data)
    broken_reason = next((p.message for p in problems if p.kind == "broken"), "")

    blocked: dict[str, str] = {}
    for key in SPENDING_KEYS:
        if ("scoring", key) in override_keys:
            continue
        if "scoring" in unreadable:
            blocked[key] = blocked_message(key, broken_reason or "[scoring] is not a table")
        elif key in invalid.get("scoring", {}):
            problem = next(p for p in problems if p.key == f"scoring.{key}")
            blocked[key] = blocked_message(key, problem.message.removeprefix(f"scoring.{key}: "))

    notices: list[str] = []
    if "apply" in unreadable:
        if any(("apply", k) not in override_keys for k in MODEL_KEYS["apply"]):
            reason = broken_reason or "models.toml's [apply] is not a table"
            notices.append(f"{reason}; drafting uses the config.toml values or defaults")
    else:
        for key in invalid.get("apply", {}):
            if ("apply", key) in override_keys:
                continue
            value = getattr(base.apply, key)
            fallback = shown_value(str(value).lower() if isinstance(value, bool) else value)
            where = "config.toml" if ("apply", key) in config_keys else "the default"
            notices.append(
                f"models.toml's apply.{key} is invalid; using {fallback or 'the value'} "
                f"from {where}"
            )

    layered = _deep_merge(lower, valid)
    if blocked:
        layered = _deep_merge(layered, {"scoring": dict.fromkeys(blocked, BLOCKED_SPEC)})
    merged = _validate(_deep_merge(layered, overrides), file_data, env_paths, overrides)
    merged.scoring._blocked = dict(blocked)
    layers = Layers(
        path=mpath,
        exists=exists,
        digest=digest,
        base=base,
        merged=merged,
        layer=valid,
        invalid=invalid,
        problems=problems,
        blocked=blocked,
        notices=notices,
        config_keys=config_keys,
        override_keys=override_keys,
    )
    merged._models = layers
    _warn_once(mpath, digest, problems)
    return layers


def _validate(
    merged: dict[str, Any],
    file_data: Mapping[str, Any],
    env_paths: Mapping[str, str],
    overrides: Mapping[str, Any],
) -> Settings:
    merged = dict(merged)
    merged.setdefault("paths", {})
    settings = Settings.model_validate(merged)
    labels = settings.paths._sources
    for name in PATH_FIELDS:
        if name in (overrides.get("paths") or {}):
            labels[name] = "override"
        elif name in env_paths:
            labels[name] = f"env {PATH_ENV_VARS[name]}"
        elif name in (file_data.get("paths") or {}):
            labels[name] = "config file"
    return settings


def load_settings(
    config_path: Path | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> Settings:
    """Build Settings from code defaults, the TOML file, the environment, ``models.toml`` in
    the data dir, then ``overrides`` (``load_layers`` has the details).

    An explicit ``config_path`` wins over ``$JOBHUNTER_CONFIG``, which wins over the
    default location. A missing file is not an error. Unknown keys raise
    ``pydantic.ValidationError`` naming the key (e.g. ``fetch.bogus``). The ``[paths]``
    keys can also come from ``JOBHUNTER_DATA_DIR`` and friends (``PATH_ENV_VARS``), which
    beat the file. A broken ``models.toml`` never raises: it blocks the spending uses.
    """
    return load_layers(config_path, overrides).merged


# --- config watch classification (specs/019 Addendum B, "Which settings reload") -------------
#
# Every leaf field of ``Settings`` is in exactly one of RELOADABLE and RESTART_FIELDS
# (tests/test_config_watch.py enforces it). A field missing from both still classifies as
# restart (``reload_class``), so a new field is never swapped in place until someone looks.

RELOADABLE: frozenset[str] = frozenset(
    {
        # read per FetchContext
        "fetch.respect_robots",
        "fetch.default_rps",
        "fetch.user_agent_contact",
        "fetch.timeout_s",
        "fetch.max_retries",
        # scorers are built per run
        "scoring.screen_scorer",
        "scoring.deep_scorer",
        "scoring.daily_cap_usd",
        "scoring.weekly_cap_usd",
        "scoring.deep_shortlist",
        "scoring.employer_rejection_days",
        "scoring.openai_compat.input_usd_per_mtok",
        "scoring.openai_compat.output_usd_per_mtok",
        "scoring.openai_compat.max_concurrency",
        "scoring.openai_compat.json_schema",
        "scoring.openai_compat.jobs_per_request",
        "scoring.openai_compat.max_input_tokens_per_request",
        "scoring.openai_compat.est_cost_per_request_usd",
        "scoring.openrouter.title",
        "scoring.openrouter.referer",
        "scoring.openrouter.provider",
        "scoring.openrouter.max_concurrency",
        "scoring.openrouter.json_schema",
        "scoring.openrouter.jobs_per_request",
        "scoring.openrouter.max_input_tokens_per_request",
        "scoring.openrouter.est_cost_per_request_usd",
        "scoring.local.timeout_s",
        "scoring.local.max_concurrency",
        "scoring.local.json_schema",
        "scoring.local.jobs_per_request",
        "scoring.local.max_input_tokens_per_request",
        # read per call
        "apply.runner",
        "apply.cli_model",
        "apply.api_model",
        "apply.effort",
        "apply.daily_cap_usd",
        "apply.entailment_check",
        "apply.cli_entailment_model",
        "apply.api_entailment_model",
        # read on use
        "mail.alerts_address",
        "mail.label",
        "mail.fallback_sender_domains",
        "mail.fallback_senders",
        "mail.client_secrets_path",
        # read per request
        "capture.disabled_hosts",
    }
)

RESTART_FIELDS: frozenset[str] = frozenset(
    {
        "paths.data_dir",
        "paths.cache_dir",
        "paths.db_path",
        "paths.profile_dir",
        "paths.resume_path",
        "console.host",
        "console.port",
        "console.public_port",
        "fetch.global_concurrency",  # the process-wide semaphore is sized once
        # secrets are resolved once, at start, for the names config gave then
        "scoring.openai_compat.api_key_env",
        "scoring.openrouter.api_key_env",
        "scoring.local.api_key_env",
        "scoring.openrouter.catalog_cache",
        # An endpoint goes with its credential, which is resolved at start and may rotate
        # unseen (a podman secret's *_FILE, the process environment): a new host must never
        # get the running key, so a scorer endpoint changes only with a restart.
        "scoring.openai_compat.base_url",
        "scoring.openrouter.base_url",
        "scoring.local.base_url",
        "scoring.local.runtime",  # picks the default endpoint
    }
)

# Fields whose old and new values may be shown on /config/changes. Everything else shows
# "changed" only: personal values (contact, alerts address, sender lists), URLs (a key can sit
# in one), paths, the OpenRouter provider, title and referer. Default is hidden.
SHOWN_VALUES: frozenset[str] = frozenset(
    {
        "fetch.respect_robots",
        "fetch.default_rps",
        "fetch.timeout_s",
        "fetch.max_retries",
        "fetch.global_concurrency",
        "scoring.screen_scorer",
        "scoring.deep_scorer",
        "scoring.daily_cap_usd",
        "scoring.weekly_cap_usd",
        "scoring.deep_shortlist",
        "scoring.employer_rejection_days",
        "scoring.openai_compat.input_usd_per_mtok",
        "scoring.openai_compat.output_usd_per_mtok",
        "scoring.openai_compat.max_concurrency",
        "scoring.openai_compat.json_schema",
        "scoring.openai_compat.jobs_per_request",
        "scoring.openai_compat.max_input_tokens_per_request",
        "scoring.openai_compat.est_cost_per_request_usd",
        "scoring.openrouter.max_concurrency",
        "scoring.openrouter.json_schema",
        "scoring.openrouter.jobs_per_request",
        "scoring.openrouter.max_input_tokens_per_request",
        "scoring.openrouter.est_cost_per_request_usd",
        "scoring.local.runtime",
        "scoring.local.timeout_s",
        "scoring.local.max_concurrency",
        "scoring.local.json_schema",
        "scoring.local.jobs_per_request",
        "scoring.local.max_input_tokens_per_request",
        "apply.runner",
        "apply.cli_model",
        "apply.api_model",
        "apply.effort",
        "apply.daily_cap_usd",
        "apply.entailment_check",
        "apply.cli_entailment_model",
        "apply.api_entailment_model",
        "mail.label",
        "console.port",
        "console.public_port",
    }
)

# Shown as a count of entries, never the entries.
SHOWN_AS_COUNT: frozenset[str] = frozenset({"capture.disabled_hosts"})
# Shown only when old and new both look like an environment variable name.
NAME_VALUED: frozenset[str] = frozenset(
    {
        "scoring.openai_compat.api_key_env",
        "scoring.openrouter.api_key_env",
        "scoring.local.api_key_env",
    }
)


def leaf_fields(model: type[BaseModel] = Settings, prefix: str = "") -> list[str]:
    """Every leaf field of ``model`` as a dotted path (``scoring.openrouter.referer``). A field
    whose type is itself a model is a section, not a leaf."""
    out: list[str] = []
    for name, info in model.model_fields.items():
        ann = info.annotation
        path = f"{prefix}{name}"
        if isinstance(ann, type) and issubclass(ann, BaseModel):
            out.extend(leaf_fields(ann, f"{path}."))
        else:
            out.append(path)
    return out


def reload_class(field: str) -> str:
    """``reload`` for a field on RELOADABLE, else ``restart``."""
    return "reload" if field in RELOADABLE else "restart"


def field_value(settings: Settings, field: str) -> Any:
    obj: Any = settings
    for part in field.split("."):
        obj = getattr(obj, part)
    return obj
