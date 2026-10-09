"""Profile loading and versioning (specs/006 "The profile spec", specs/014 "Source of truth").

``profile/preferences.yaml`` plus the resume is the user's profile. Two hashes version it:

- ``filter_version`` covers the free, Python-computed fields (hard, soft, buckets, queries,
  target_titles). Changing them re-applies instantly.
- ``scoring_version`` covers what the model reads (resume text, current_focus, narrative).
  Changing it costs money to re-apply.

Comments and key order in the YAML never reach either hash: both are computed from the
validated model, not from the file text.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from jobhunter.config import resolve_path
from jobhunter.core.geo import normalize_state
from jobhunter.core.models import EmploymentType, Remote

logger = logging.getLogger(__name__)

PREFERENCES_FILE = "preferences.yaml"
WEIGHT_TOLERANCE = 0.01
DEFAULT_WEIGHTS: dict[str, float] = {
    "skills": 0.30,
    "seniority": 0.20,
    "domain": 0.20,
    "comp": 0.15,
    "location": 0.15,
}
# Top-level keys the loader understands. Anything else is ignored with a warning.
_YAML_KEYS = frozenset(
    {
        "resume_path",
        "target_titles",
        "hard",
        "soft",
        "current_focus",
        "narrative",
        "queries",
        "buckets",
    }
)
_SINCE_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


class ProfileError(Exception):
    """A profile file or resume could not be loaded or validated.

    The message names the file path and, where known, the dotted field.
    """

    def __init__(
        self, message: str, *, path: Path | str | None = None, field: str | None = None
    ) -> None:
        self.path = Path(path) if path is not None else None
        self.field = field
        self.reason = message
        where = str(self.path) if self.path is not None else "profile"
        if field:
            where = f"{where}: {field}"
        super().__init__(f"{where}: {message}")


# ─── Value helpers ──────────────────────────────────────────────────────────


def _state(value: str) -> str:
    """Normalize a state name or code to its USPS code (specs/011)."""
    code = normalize_state(str(value))
    if code is None:
        raise ValueError(f"not a US state code or name: {value!r}")
    return code


# ─── Nested models (strict: unknown keys are errors) ────────────────────────


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Home(_Strict):
    city: str | None = None
    state: str | None = None

    @field_validator("state")
    @classmethod
    def _normalize(cls, value: str | None) -> str | None:
        return None if value is None else _state(value)


class SalaryFloor(_Strict):
    amount: float | None = Field(default=None, ge=0)
    period: Literal["year", "month", "hour"] = "year"


class SalaryTarget(_Strict):
    amount: float = Field(ge=0)
    period: Literal["year", "month", "hour"] = "year"


class Hard(_Strict):
    """Machine-checkable constraints. Stage 1 may reject on these (specs/006)."""

    home: Home | None = None
    states_allowed: Literal["all"] | list[str] = "all"
    states_excluded: list[str] = Field(default_factory=list)
    relocation_ok: bool = False
    remote_ok: bool = False
    remote_only: bool = False
    max_commute_miles: int | None = Field(default=None, ge=0)
    salary_floor: SalaryFloor | None = None
    salary_target: SalaryTarget | None = None
    employment_types_excluded: list[EmploymentType] = Field(default_factory=list)
    requires_i_lack: list[str] = Field(default_factory=list)
    max_travel_pct: int | None = Field(default=None, ge=0, le=100)
    title_exclusions: list[str] = Field(default_factory=list)
    hide_unstated_salary: bool = False

    @field_validator("states_allowed", mode="before")
    @classmethod
    def _allowed(cls, value: Any) -> Any:
        if isinstance(value, str):
            if value.strip().casefold() == "all":
                return "all"
            raise ValueError("must be 'all' or a list of state codes")
        if isinstance(value, list):
            return [_state(item) for item in value]
        return value

    @field_validator("states_excluded")
    @classmethod
    def _excluded(cls, value: list[str]) -> list[str]:
        return [_state(item) for item in value]


class Weights(_Strict):
    skills: float = Field(ge=0)
    seniority: float = Field(ge=0)
    domain: float = Field(ge=0)
    comp: float = Field(ge=0)
    location: float = Field(ge=0)

    @model_validator(mode="after")
    def _sums_to_one(self) -> Weights:
        total = self.skills + self.seniority + self.domain + self.comp + self.location
        if abs(total - 1.0) > WEIGHT_TOLERANCE:
            raise ValueError(f"weights must sum to 1.0 (within 0.01), got {total:.3f}")
        return self


class Soft(_Strict):
    """Weighted preferences. Only the model and the Python comp/location scorers read these."""

    weights: Weights = Field(default_factory=lambda: Weights(**DEFAULT_WEIGHTS))
    state_ranking: list[str] = Field(default_factory=list)
    remote_bonus: int = 0

    @field_validator("state_ranking")
    @classmethod
    def _ranking(cls, value: list[str]) -> list[str]:
        return [_state(item) for item in value]


class CurrentFocus(_Strict):
    """Recency signal (specs/006 "Bucket F exists because of recency")."""

    since: str | None = None
    doing: str | None = None
    want_more_of: list[str] = Field(default_factory=list)
    done_with: list[str] = Field(default_factory=list)

    @field_validator("since", mode="before")
    @classmethod
    def _since(cls, value: Any) -> Any:
        # YAML may parse an unquoted 2023-01-01 as a date; the spec wants YYYY-MM.
        if isinstance(value, date):
            value = value.strftime("%Y-%m")
        if isinstance(value, str) and not _SINCE_RE.match(value):
            raise ValueError(f"must be YYYY-MM, got {value!r}")
        return value


class Narrative(_Strict):
    """Free prose the model reads. The part keywords cannot express (specs/006)."""

    want: str | None = None
    avoid: str | None = None
    dealbreakers_soft: str | None = None
    context: str | None = None


class ProfileQuery(_Strict):
    """A source-side search net entry (specs/006 Stage 0)."""

    keywords: list[str] = Field(default_factory=list)
    title: str | None = None
    onet_soc: str | None = None
    occupation_code: str | None = None
    posted_within_days: int | None = Field(default=None, ge=0)
    remote: Remote | None = None


# ─── Profile ────────────────────────────────────────────────────────────────


class Profile(BaseModel):
    """The validated profile. ``filter_version`` and ``scoring_version`` are derived."""

    # Unknown top-level keys are stripped by the loader, with a warning, before validation.
    model_config = ConfigDict(extra="ignore")

    resume_path: str | None = None
    target_titles: list[str] = Field(default_factory=list)
    hard: Hard = Field(default_factory=Hard)
    soft: Soft = Field(default_factory=Soft)
    current_focus: CurrentFocus | None = None
    narrative: Narrative = Field(default_factory=Narrative)
    queries: list[ProfileQuery] = Field(default_factory=list)
    buckets: dict[str, float] | None = None

    # Set by load_profile, not read from YAML.
    source_file: Path | None = None
    resume_file: Path | None = None
    resume_text: str = ""
    load_warnings: tuple[str, ...] = ()

    @property
    def filter_version(self) -> str:
        """sha256 over canonical JSON of the free (Python-computed) fields."""
        payload = {
            "hard": self.hard.model_dump(mode="json"),
            "soft": self.soft.model_dump(mode="json"),
            "buckets": self.buckets,
            "queries": [q.model_dump(mode="json") for q in self.queries],
            "target_titles": self.target_titles,
        }
        return _sha256(_canonical(payload))

    @property
    def scoring_version(self) -> str:
        """sha256 over the resume text and canonical JSON of (current_focus, narrative)."""
        payload = {
            "resume": self.resume_text,
            "current_focus": (
                None if self.current_focus is None else self.current_focus.model_dump(mode="json")
            ),
            "narrative": self.narrative.model_dump(mode="json"),
        }
        return _sha256(_canonical(payload))


def _canonical(payload: Any) -> str:
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ─── Loading ────────────────────────────────────────────────────────────────


def _plain(value: Any) -> Any:
    """Convert ruamel round-trip types to plain Python, keeping dates as they are."""
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if isinstance(value, bool):  # before int: bool is an int subclass
        return bool(value)
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return float(value)
    if isinstance(value, str):
        return str(value)
    return value


def _clean(value: Any) -> Any:
    """Treat null, empty strings and empty containers as unset (the field default applies)."""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            cleaned = _clean(item)
            if cleaned is None or cleaned in ("", [], {}):
                continue
            out[key] = cleaned
        return out
    if isinstance(value, list):
        items = [_clean(item) for item in value]
        return [item for item in items if item is not None and item != ""]
    if isinstance(value, str):
        stripped = value.strip()
        return stripped if stripped else None
    return value


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ProfileError("file not found", path=path) from exc
    except OSError as exc:
        raise ProfileError(f"cannot read file: {exc.strerror}", path=path) from exc
    try:
        data = YAML(typ="rt").load(text)
    except YAMLError as exc:
        raise ProfileError(f"invalid YAML: {exc}", path=path) from exc
    if data is None:
        return {}
    if not isinstance(data, Mapping):
        raise ProfileError("top level must be a mapping", path=path)
    return _plain(data)


def _validation_error(path: Path, exc: ValidationError) -> ProfileError:
    first = exc.errors()[0]
    field = ".".join(str(part) for part in first["loc"]) or None
    message = str(first["msg"]).removeprefix("Value error, ")
    return ProfileError(message, path=path, field=field)


def _resolve_resume(
    profile_dir: Path, prefs_file: Path, yaml_value: str | None, argument: Path | None
) -> Path:
    if argument is not None:
        location = resolve_path(argument)
    elif yaml_value:
        location = resolve_path(yaml_value, base=profile_dir)
    else:
        raise ProfileError(
            "no resume: pass resume_path or set resume_path in preferences.yaml",
            path=prefs_file,
            field="resume_path",
        )

    if location.is_dir():
        candidates = sorted(p for p in location.glob("*.md") if p.is_file())
        if not candidates:
            raise ProfileError("resume directory has no .md file", path=location)
        if len(candidates) > 1:
            names = ", ".join(p.name for p in candidates)
            raise ProfileError(
                f"resume directory must hold exactly one .md file, found {len(candidates)}: "
                f"{names}",
                path=location,
            )
        return candidates[0]
    if not location.is_file():
        raise ProfileError("resume file not found", path=location)
    return location


def _read_resume(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ProfileError(f"cannot read resume: {exc.strerror}", path=path) from exc
    if not text.strip():
        raise ProfileError("resume is empty", path=path)
    return text


def load_profile(profile_dir: Path, resume_path: Path | None = None) -> Profile:
    """Load ``preferences.yaml`` from ``profile_dir`` and the resume text.

    The resume is ``resume_path`` if given, else the YAML's ``resume_path`` (relative to
    ``profile_dir``). A directory means the single ``.md`` file inside it.
    Unknown top-level keys are ignored and recorded in ``Profile.load_warnings``.
    """
    profile_dir = Path(profile_dir).expanduser()
    prefs_file = profile_dir / PREFERENCES_FILE

    raw = _read_yaml(prefs_file)
    unknown = [key for key in raw if key not in _YAML_KEYS]
    warnings = tuple(f"{prefs_file}: unknown top-level key {key!r} ignored" for key in unknown)
    for message in warnings:
        logger.warning(message)
    known = {key: value for key, value in raw.items() if key in _YAML_KEYS}

    try:
        profile = Profile.model_validate(_clean(known))
    except ValidationError as exc:
        raise _validation_error(prefs_file, exc) from exc

    resume_file = _resolve_resume(profile_dir, prefs_file, profile.resume_path, resume_path)
    resume_text = _read_resume(resume_file)

    return profile.model_copy(
        update={
            "source_file": prefs_file,
            "resume_file": resume_file,
            "resume_text": resume_text,
            "load_warnings": warnings,
        }
    )


# ─── Model input ────────────────────────────────────────────────────────────


def scoring_inputs(profile: Profile) -> str:
    """The text block for the cached system prompt (specs/006 "Prompt construction").

    Resume, current focus, done-with and narrative only. No salary, state, weight or
    other filter data: those stay out of the model input (specs/014).
    """
    sections: list[str] = ["## Resume", profile.resume_text.strip()]

    focus = profile.current_focus
    if focus is not None:
        lines = ["## Current focus"]
        if focus.since:
            lines.append(f"Since: {focus.since}")
        if focus.doing:
            lines.append(f"Doing: {focus.doing}")
        if focus.want_more_of:
            lines.append("Want more of: " + "; ".join(focus.want_more_of))
        if focus.done_with:
            lines.append("Done with (do not surface): " + "; ".join(focus.done_with))
        sections.append("\n".join(lines))

    narrative = profile.narrative
    narrative_lines = ["## Narrative"]
    for label, text in (
        ("Want", narrative.want),
        ("Avoid", narrative.avoid),
        ("Dealbreakers", narrative.dealbreakers_soft),
        ("Context", narrative.context),
    ):
        if text:
            narrative_lines.append(f"### {label}\n{text}")
    if len(narrative_lines) > 1:
        sections.append("\n".join(narrative_lines))

    return "\n\n".join(sections) + "\n"
