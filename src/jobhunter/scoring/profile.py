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
import io
import json
import logging
import os
import re
import tempfile
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
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.error import CommentMark, YAMLError
from ruamel.yaml.scalarstring import LiteralScalarString
from ruamel.yaml.tokens import CommentToken

from jobhunter.config import Settings, resolve_path
from jobhunter.core.geo import normalize_state
from jobhunter.core.models import EmploymentType, Remote
from jobhunter.xdg import mkdir_private

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
# Top-level keys another loader owns. The profile never reads them (so a bad entry there can
# never stop scoring or the nightly run) and does not warn about them: ``answers:`` is
# Application answers, loaded on its own by ``apply/answers.py`` (specs/017).
SEPARATE_KEYS = frozenset({"answers"})
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


class ProfileValidationError(ProfileError):
    """The profile failed model validation. ``errors`` maps every dotted field to its message.

    ``field`` / ``reason`` describe the first error, as for any ``ProfileError``.
    """

    def __init__(self, errors: dict[str, str], *, path: Path | str | None = None) -> None:
        self.errors = dict(errors)
        field, message = next(iter(self.errors.items()), ("", "invalid profile"))
        super().__init__(message, path=path, field=field or None)

    @classmethod
    def from_pydantic(cls, path: Path | str | None, exc: ValidationError) -> ProfileValidationError:
        errors: dict[str, str] = {}
        for err in exc.errors():
            field = ".".join(str(part) for part in err["loc"])
            errors.setdefault(field, str(err["msg"]).removeprefix("Value error, "))
        return cls(errors, path=path)


class ProfileConflict(ProfileError):
    """The preferences file changed on disk since it was read; the save was refused."""


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


_TOP_KEY = re.compile(r"^([A-Za-z_][\w-]*)\s*:")


def without_separate_blocks(text: str) -> str:
    """``text`` with its top-level ``SEPARATE_KEYS`` blocks (``answers:``) cut out, line by
    line, before the profile parses it. Anything inside them, even YAML the parser rejects
    (a duplicate key from a hand-added answer, a tab), can then never stop the profile load,
    scoring or the nightly run; apply/answers.py reads that block on its own."""
    out: list[str] = []
    skipping = False
    for line in text.splitlines(keepends=True):
        top = _TOP_KEY.match(line)
        if top:
            skipping = top.group(1) in SEPARATE_KEYS
        elif skipping and line.strip() and not line[0].isspace() and line[0] not in "#-":
            skipping = False  # a top-level line that is not a key (a document marker, say)
        if not skipping:
            out.append(line)
    return "".join(out)


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ProfileError("file not found", path=path) from exc
    except OSError as exc:
        raise ProfileError(f"cannot read file: {exc.strerror}", path=path) from exc
    try:
        data = YAML(typ="rt").load(without_separate_blocks(text))
    except YAMLError as exc:
        raise ProfileError(f"invalid YAML: {exc}", path=path) from exc
    if data is None:
        return {}
    if not isinstance(data, Mapping):
        raise ProfileError("top level must be a mapping", path=path)
    return _plain(data)


def _resolve_resume(
    profile_dir: Path, prefs_file: Path, yaml_value: str | None, argument: Path | None
) -> Path:
    if argument is not None:
        location = resolve_path(argument)
    elif yaml_value:
        # Relative paths are tried against the profile directory first, then its parent
        # (the repo root, where resume/ lives next to profile/), then the working dir.
        location = resolve_path(yaml_value, base=profile_dir)
        if not location.exists() and not Path(yaml_value).expanduser().is_absolute():
            for base in (profile_dir.resolve().parent, Path.cwd()):
                alt = resolve_path(yaml_value, base=base)
                if alt.exists():
                    location = alt
                    break
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


def load_profile_for(settings: Settings) -> Profile:
    """Load the user's profile the same way at every entry point.

    The profile's own ``resume_path`` wins; the configured ``paths.resume_path`` is only
    the fallback when preferences.yaml doesn't name a resume. (Preferring the configured
    default made tests with their own temporary profiles silently read the real resume.)
    """
    profile_dir = resolve_path(settings.paths.profile_dir)
    try:
        return load_profile(profile_dir)
    except ProfileError as exc:
        if exc.field != "resume_path" or "no resume" not in str(exc):
            raise
    return load_profile(profile_dir, resolve_path(settings.paths.resume_path))


def load_profile(
    profile_dir: Path, resume_path: Path | None = None, *, require_resume: bool = True
) -> Profile:
    """Load ``preferences.yaml`` from ``profile_dir`` and the resume text.

    The resume is ``resume_path`` if given, else the YAML's ``resume_path`` (relative to
    ``profile_dir``). A directory means the single ``.md`` file inside it.
    Unknown top-level keys are ignored and recorded in ``Profile.load_warnings``.
    With ``require_resume=False`` a missing resume is not an error (``resume_text`` is empty).
    """
    profile_dir = Path(profile_dir).expanduser()
    return _load_file(
        profile_dir, profile_dir / PREFERENCES_FILE, resume_path, require_resume=require_resume
    )


def _load_file(
    profile_dir: Path, prefs_file: Path, resume_path: Path | None, *, require_resume: bool = True
) -> Profile:
    """Load and validate ``prefs_file`` (normally ``profile_dir/preferences.yaml``).

    With ``require_resume=False`` the resume is not read at all (the console lets you fix
    preferences while the resume is missing); ``resume_text`` is then empty.
    """
    raw = _read_yaml(prefs_file)
    unknown = [key for key in raw if key not in _YAML_KEYS and key not in SEPARATE_KEYS]
    warnings = tuple(f"{prefs_file}: unknown top-level key {key!r} ignored" for key in unknown)
    for message in warnings:
        logger.warning(message)
    known = {key: value for key, value in raw.items() if key in _YAML_KEYS}

    try:
        profile = Profile.model_validate(_clean(known))
    except ValidationError as exc:
        raise ProfileValidationError.from_pydantic(prefs_file, exc) from exc

    if not require_resume:
        return profile.model_copy(update={"source_file": prefs_file, "load_warnings": warnings})
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


# ─── Writing (specs/014 "Source of truth stays a file") ─────────────────────

EDITABLE_KEYS = _YAML_KEYS


def preferences_mtime_ns(profile_dir: Path) -> int:
    """``st_mtime_ns`` of ``preferences.yaml``; the page sends it back to detect hand edits."""
    return (Path(profile_dir).expanduser() / PREFERENCES_FILE).stat().st_mtime_ns


def profile_data(profile: Profile) -> dict[str, Any]:
    """The YAML-backed fields as plain JSON-able data, with empty blocks normalized.

    ``current_focus`` None becomes an empty focus and ``buckets`` None an empty dict, so two
    profiles that differ only in an absent vs empty block compare equal.
    """
    data = profile.model_dump(mode="json", include=set(EDITABLE_KEYS))
    if data.get("current_focus") is None:
        data["current_focus"] = CurrentFocus().model_dump(mode="json")
    if data.get("buckets") is None:
        data["buckets"] = {}
    return data


def validate_profile_data(data: Mapping[str, Any], *, base: Profile) -> Profile:
    """Validate plain profile data (as from :func:`profile_data`) without touching disk.

    File and resume fields are carried over from ``base``. Raises ``ProfileValidationError``.
    """
    known = {key: value for key, value in data.items() if key in EDITABLE_KEYS}
    try:
        profile = Profile.model_validate(_clean(json.loads(json.dumps(known))))
    except ValidationError as exc:
        raise ProfileValidationError.from_pydantic(base.source_file, exc) from exc
    return profile.model_copy(
        update={
            "source_file": base.source_file,
            "resume_file": base.resume_file,
            "resume_text": base.resume_text,
            "load_warnings": base.load_warnings,
        }
    )


_KEY_LINE_RE = re.compile(r"^[^#\-\s][^#]*:\s*(#.*)?$")


def _guess_indent(text: str) -> tuple[int, int]:
    """(mapping indent, block-sequence dash offset) as the user wrote them; default (2, 0)."""
    mapping: int | None = None
    offset: int | None = None
    parent: int | None = None  # indent of the previous line when it opened a block ("key:")
    for line in text.splitlines():
        stripped = line.lstrip(" ")
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(stripped)
        if parent is not None:
            if stripped == "-" or stripped.startswith("- "):
                if offset is None:
                    offset = indent - parent
            elif indent > parent and mapping is None:
                mapping = indent - parent
        parent = indent if _KEY_LINE_RE.match(stripped) else None
        if mapping is not None and offset is not None:
            break
    return mapping or 2, max(offset or 0, 0)


def _yaml_writer(text: str) -> YAML:
    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True
    yaml.width = 4096  # never re-wrap long lines the user wrote
    mapping, offset = _guess_indent(text)
    yaml.indent(mapping=mapping, sequence=offset + 2, offset=offset)
    return yaml


def _to_yaml(value: Any, old: Any = None) -> Any:
    """Plain value -> ruamel node, keeping the old node's flow style for lists and maps.

    New lists of scalars are written in flow style (``[CO, WA]``), as in the spec examples.
    Multi-line strings become literal blocks. Map entries whose value is None are dropped.
    """
    if isinstance(value, Mapping):
        node = CommentedMap()
        for key, item in value.items():
            if item is not None:
                node[str(key)] = _to_yaml(item)
        if isinstance(old, CommentedMap) and old.fa.flow_style():
            node.fa.set_flow_style()
        return node
    if isinstance(value, list | tuple):
        seq = CommentedSeq([_to_yaml(item) for item in value])
        flow = old.fa.flow_style() if isinstance(old, CommentedSeq) else None
        scalars = all(not isinstance(item, Mapping | list | tuple) for item in value)
        if flow or (flow is None and scalars):
            seq.fa.set_flow_style()
        return seq
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and "\n" in value.strip():
        return LiteralScalarString(value.strip("\n") + "\n")
    return value


def _update_seq_of_maps(old: CommentedSeq, value: list[Any]) -> None:
    """Rewrite a block list of mappings in place, so comments between items survive (ruamel
    keeps them on the items; a new list would drop them)."""
    for i, item in enumerate(value):
        if i >= len(old):
            old.append(_to_yaml(item))
            continue
        cur = old[i]
        if not isinstance(cur, CommentedMap) or not isinstance(item, Mapping):
            if cur != item:
                old[i] = _to_yaml(item, cur)
            continue
        for key in [k for k in cur if item.get(k) is None]:
            del cur[key]
        for key, val in item.items():
            if val is not None and cur.get(key) != val:
                cur[key] = _to_yaml(val, cur.get(key))
    while len(old) > len(value):
        del old[len(old) - 1]  # del old[-1] shifts ruamel item comments


# Where ruamel keeps the comment that follows a node: index 2 of a mapping key's entry,
# index 0 of a sequence item's.
_TAIL_SLOT = {CommentedMap: 2, CommentedSeq: 0}


def _last_holder(node: Any) -> tuple[Any, Any] | None:
    """The block container and key/index of the document's last scalar: the end-of-file
    comments are attached there."""
    holder = None
    while isinstance(node, CommentedMap | CommentedSeq) and len(node):
        if holder is not None and node.fa.flow_style():
            break
        key = list(node)[-1] if isinstance(node, CommentedMap) else len(node) - 1
        holder = (node, key)
        node = node[key]
    return holder


def _take_tail(doc: CommentedMap) -> str | None:
    """Detach the comment lines after the document's last value (keeping its inline
    comment where it is). Returns them, or None."""
    holder = _last_holder(doc)
    if holder is None:
        return None
    node, key = holder
    slot = _TAIL_SLOT[type(node)]
    entry = node.ca.items.get(key)
    token = entry[slot] if entry and len(entry) > slot else None
    if token is None or "\n" not in token.value:
        return None
    first, _, rest = token.value.partition("\n")
    if not rest.strip():
        return None
    if first.strip():
        token.value = first + "\n"
    else:
        entry[slot] = None
    return rest


def _put_tail(doc: CommentedMap, rest: str) -> None:
    holder = _last_holder(doc)
    if holder is None:
        doc.yaml_set_start_comment(rest.strip("\n"))
        return
    node, key = holder
    slot = _TAIL_SLOT[type(node)]
    entry = node.ca.items.setdefault(key, [None, None, None, None])
    token = entry[slot]
    if token is not None:
        token.value = token.value.rstrip("\n") + "\n" + rest
    else:
        entry[slot] = CommentToken("\n" + rest, CommentMark(0), None)


def _set_path(doc: CommentedMap, path: str, value: Any) -> None:
    """Set (or, for None, delete) a dotted path in a round-trip document."""
    parts = path.split(".")
    node: CommentedMap = doc
    parents: list[tuple[CommentedMap, str]] = []
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, CommentedMap):
            if value is None:
                return  # deleting under a missing parent: nothing to do
            child = CommentedMap()
            node[part] = child
        parents.append((node, part))
        node = child
    leaf = parts[-1]
    old = node.get(leaf)
    if (
        isinstance(old, CommentedSeq)
        and not old.fa.flow_style()
        and isinstance(value, list)
        and value
    ):
        _update_seq_of_maps(old, value)
        return
    if value is not None:
        node[leaf] = _to_yaml(value, old)
        return
    if leaf in node:
        del node[leaf]
    # Prune maps the deletion left empty, so no `salary_target: {}` lingers.
    for parent, key in reversed(parents):
        if len(parent[key]):
            break
        del parent[key]


def save_profile_changes(
    profile_dir: Path,
    changes: Mapping[str, Any],
    *,
    expected_mtime_ns: int,
    resume_path: Path | None = None,
    require_resume: bool = True,
) -> Profile:
    """Apply ``{dotted.path: value}`` to ``preferences.yaml`` and return the reloaded profile.

    Round-trips with ruamel so comments, key order and flow style survive; ``None`` removes a
    key. The new text goes to a temp file in the same directory, is validated exactly as
    :func:`load_profile` would, and only then replaces the original (``os.replace``), so a
    failed save leaves the file untouched. Raises ``ProfileConflict`` when the file's mtime no
    longer matches ``expected_mtime_ns`` and ``ProfileValidationError`` (fields as dotted
    paths) when the result does not validate.
    """
    profile_dir = Path(profile_dir).expanduser()
    prefs_file = profile_dir / PREFERENCES_FILE
    try:
        stat = prefs_file.stat()
    except FileNotFoundError as exc:
        raise ProfileError("file not found", path=prefs_file) from exc
    if stat.st_mtime_ns != expected_mtime_ns:
        raise ProfileConflict("changed on disk since the page was loaded", path=prefs_file)

    text = prefs_file.read_text(encoding="utf-8")
    yaml = _yaml_writer(text)
    try:
        doc = yaml.load(text)
    except YAMLError as exc:
        raise ProfileError(f"invalid YAML: {exc}", path=prefs_file) from exc
    if doc is None:
        doc = CommentedMap()
    if not isinstance(doc, CommentedMap):
        raise ProfileError("top level must be a mapping", path=prefs_file)
    # End-of-file comments ride on the last value; keep them at the end whatever changes.
    tail = _take_tail(doc)
    for path, value in changes.items():
        _set_path(doc, path, value)
    if tail is not None:
        _put_tail(doc, tail)
    buf = io.StringIO()
    yaml.dump(doc, buf)

    fd, tmp_name = tempfile.mkstemp(dir=profile_dir, prefix=".preferences.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(buf.getvalue())
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, stat.st_mode & 0o7777)
        try:
            _load_file(profile_dir, tmp, resume_path, require_resume=require_resume)
        except ProfileValidationError as exc:
            raise ProfileValidationError(exc.errors, path=prefs_file) from exc
        except ProfileError as exc:
            if exc.path != tmp:
                raise
            # Report against the real file, not the temp name.
            raise ProfileError(exc.reason, path=prefs_file, field=exc.field) from exc
        if prefs_file.stat().st_mtime_ns != expected_mtime_ns:
            raise ProfileConflict("changed on disk while saving", path=prefs_file)
        os.replace(tmp, prefs_file)
    finally:
        tmp.unlink(missing_ok=True)
    if not require_resume:
        return _load_file(profile_dir, prefs_file, resume_path, require_resume=False)
    return load_profile(profile_dir, resume_path)


# ─── Creating and repairing the file (the console never dead-ends) ──────────

NEW_FILE_HEADER = (
    "Preferences for jobhunter. Edit here or on the console's Preferences page.\n"
    "Comments you add are kept when the page saves.\n"
)


def _atomic_private_write(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` via a temp file in the same folder; the file ends up 0600."""
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def backup_preferences(profile_dir: Path, now: str) -> Path | None:
    """Copy an existing preferences file to ``preferences.yaml.<now>.bak`` (0600)."""
    prefs_file = Path(profile_dir).expanduser() / PREFERENCES_FILE
    if not prefs_file.exists():
        return None
    bak = prefs_file.with_name(f"{PREFERENCES_FILE}.{now}.bak")
    n = 1
    while bak.exists():
        bak = prefs_file.with_name(f"{PREFERENCES_FILE}.{now}-{n}.bak")
        n += 1
    fd = os.open(bak, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(prefs_file.read_bytes())
    return bak


def write_preferences_data(
    profile_dir: Path,
    data: Mapping[str, Any],
    *,
    backup_stamp: str,
    separate: Mapping[str, Any] | None = None,
) -> Path:
    """Validate ``data`` and write it as a whole new ``preferences.yaml`` (atomic, 0600).

    Used when there is no file or the existing one is unreadable; the old file, if any, is
    copied to a timestamped ``.bak`` first. The profile dir is created 0700 if missing.
    ``separate`` carries ``SEPARATE_KEYS`` blocks (``answers``), validated by their own owner
    before this call. Raises ``ProfileValidationError`` before touching anything when
    ``data`` is invalid.
    """
    profile_dir = Path(profile_dir).expanduser()
    validate_profile_data(data, base=Profile())
    doc = CommentedMap()
    for key, value in _clean(json.loads(json.dumps(dict(data)))).items():
        if key in EDITABLE_KEYS:
            doc[key] = _to_yaml(value)
    for key, value in _clean(json.loads(json.dumps(dict(separate or {})))).items():
        if key in SEPARATE_KEYS and value:
            doc[key] = _to_yaml(value)
    doc.yaml_set_start_comment(NEW_FILE_HEADER.rstrip("\n"))
    yaml = _yaml_writer("")
    buf = io.StringIO()
    yaml.dump(doc, buf)
    return write_preferences_text(profile_dir, buf.getvalue(), backup_stamp=backup_stamp)


def write_preferences_text(profile_dir: Path, text: str, *, backup_stamp: str) -> Path:
    """Write raw YAML ``text`` as ``preferences.yaml`` (caller has validated it)."""
    profile_dir = Path(profile_dir).expanduser()
    mkdir_private(profile_dir)  # each directory it creates is 0700, the data dir too
    backup_preferences(profile_dir, backup_stamp)
    prefs_file = profile_dir / PREFERENCES_FILE
    _atomic_private_write(prefs_file, text if text.endswith("\n") else text + "\n")
    return prefs_file


def salvage_preferences(text: str) -> tuple[Profile, dict[str, str]]:
    """Read as much of ``text`` as validates. Returns (profile, errors keyed by dotted field).

    Invalid fields are dropped one at a time and reported, so the form can show every
    readable value next to the errors. Raises ``ProfileError`` for YAML that does not parse
    or whose top level is not a mapping.
    """
    try:
        raw = YAML(typ="rt").load(without_separate_blocks(text))
    except YAMLError as exc:
        raise ProfileError(f"invalid YAML: {exc}") from exc
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise ProfileError("top level must be a mapping")
    data: dict[str, Any] = {k: v for k, v in _plain(raw).items() if k in _YAML_KEYS}
    errors: dict[str, str] = {}
    for _ in range(50):
        try:
            return Profile.model_validate(_clean(data)), errors
        except ValidationError as exc:
            progressed = False
            for err in exc.errors():
                loc = list(err["loc"])
                field = ".".join(str(p) for p in loc)
                errors.setdefault(field, str(err["msg"]).removeprefix("Value error, "))
                progressed |= _drop_path(data, loc) or _drop_path(data, loc[:1])
            if not progressed:
                break
    return Profile(), errors


def _drop_path(data: Any, loc: list[Any]) -> bool:
    """Remove the value at ``loc`` (dict keys / list indexes) from ``data``; False if absent."""
    node = data
    for part in loc[:-1]:
        try:
            node = node[part]
        except (KeyError, IndexError, TypeError):
            return False
    try:
        del node[loc[-1]]
    except (KeyError, IndexError, TypeError):
        return False
    return True
