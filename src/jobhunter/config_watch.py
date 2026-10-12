"""Config watch: notice a changed ``config.toml`` or ``.env`` in a running console (specs/019,
Addendum B, ticket S12a).

No timer and no inotify: ``GET /activity`` (the nav poll) calls ``ConfigWatcher.check`` at
most every ``THROTTLE_S`` per process. A check ``stat``s ``config.toml`` and every ``.env``
candidate; only a changed fingerprint reads a file. A changed ``config.toml`` is loaded with
the CLI's loader and diffed field by field against the **running** settings, so a comment-only
edit shows nothing. ``.env`` files are compared by key against the start-up snapshot through a
salted digest kept only in memory.

Privacy rules (the same for the page, the fragment and the logs):

- a value is shown only for a field on ``config.SHOWN_VALUES``; ``capture.disabled_hosts`` as a
  count; an ``api_key_env`` only when both names look like a name;
- a ``.env`` key name is shown only when it looks like a name and is a known secret name or
  starts with ``JOBHUNTER_``; a bare line (no ``=``) is only counted;
- errors come from pydantic's ``loc`` and ``msg`` (never ``str(exc)``), a custom validator's
  message only for a field on ``SHOWN_VALUES``, and a TOML error is "not valid TOML" with its
  line and column.

Restart itself is S12b; this module only says when one is needed.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
import time
import tomllib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from jobhunter import config, container
from jobhunter.config import Settings

logger = logging.getLogger(__name__)

THROTTLE_S = 2.0
NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
# A config key from the file is echoed in an error only when it looks like a key.
_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_TOML_POS = re.compile(r"\(at line (\d+), column (\d+)\)")
MAX_BAR_LINES = 3
RELOAD, RESTART, NO_EFFECT = "reload", "restart", "no-effect"
CLASS_TEXT = {RELOAD: "reloads", RESTART: "needs restart", NO_EFFECT: "no effect"}
# Why a no-effect change has none: a higher layer sets the field.
SET_ON_CLI, SET_ON_MODELS = "the command line", "/models"

# One random salt per process, never written or logged: a digest only says "changed".
_SALT = os.urandom(16)

Fingerprint = tuple[Any, ...]
StatFn = Callable[[Path], os.stat_result]


def fingerprint(path: Path, stat: StatFn = os.stat) -> Fingerprint:
    """``(exists, dev, ino, size, mtime_ns, ctime_ns, nlink, mode)``; ``(False,)`` if absent."""
    try:
        st = stat(path)
    except OSError:
        return (False,)
    return (
        True,
        st.st_dev,
        st.st_ino,
        st.st_size,
        st.st_mtime_ns,
        st.st_ctime_ns,
        st.st_nlink,
        st.st_mode,
    )


def _digest(name: str, value: str | None) -> str | None:
    if value is None:
        return None  # a bare line: no value at all
    return hashlib.sha256(_SALT + name.encode() + b"\0" + value.encode()).hexdigest()


def read_env_digests(path: Path) -> dict[str, str | None]:
    """Key -> salted digest of its value (None for a bare line) for one ``.env`` file. Read with
    ``dotenv_values``, which never touches ``os.environ``. Missing or unreadable: ``{}``."""
    from dotenv import dotenv_values

    try:
        if not path.is_file():
            return {}
        values = dotenv_values(path)
    except Exception:
        return {}
    return {k: _digest(k, v) for k, v in values.items()}


# The .env files as they were when this process loaded them (the CLI callback, cli.py).
_START_ENV: dict[Path, dict[str, str | None]] | None = None


def snapshot_env(files: Iterable[Path]) -> None:
    """Record the start-up digests of every ``.env`` candidate, including absent ones."""
    global _START_ENV
    _START_ENV = {Path(p): read_env_digests(Path(p)) for p in files}


def show_path(path: Path, home: Path | None = None) -> str:
    text = str(path)
    home_text = str(home if home is not None else Path.home())
    if text == home_text or text.startswith(home_text + "/"):
        return "~" + text[len(home_text) :]
    return text


# --- errors without values -------------------------------------------------------------------

_KNOWN_KEYS: frozenset[str] = frozenset(part for f in config.leaf_fields() for part in f.split("."))


def _loc(loc: Iterable[Any]) -> str:
    parts: list[str] = []
    for part in loc:
        if isinstance(part, int):
            parts.append(f"[{part}]")
        elif isinstance(part, str) and (part in _KNOWN_KEYS or _KEY_RE.match(part)):
            parts.append(("." if parts else "") + part)
        else:
            parts.append(("." if parts else "") + "(a key not shown)")
    return "".join(parts) or "config.toml"


def _quotes_input(msg: str, value: Any) -> bool:
    try:
        text = str(value)
    except Exception:
        return True
    return bool(text) and text in msg


def validation_lines(exc: ValidationError) -> list[str]:
    """One line per error: the location plus pydantic's message, or "invalid value" when the
    message may carry the input (a custom validator, or the input quoted in it) and the field is
    not on ``SHOWN_VALUES``. Never ``str(exc)``, which includes ``input_value=``."""
    lines: list[str] = []
    for err in exc.errors(include_url=False):
        loc = _loc(err.get("loc", ()))
        msg = str(err.get("msg") or "invalid value")
        kind = str(err.get("type") or "")
        custom = kind.startswith("value_error") or kind.startswith("assertion_error")
        if loc not in config.SHOWN_VALUES and (custom or _quotes_input(msg, err.get("input"))):
            msg = "invalid value"
        line = f"{loc}: {msg}"
        if line not in lines:
            lines.append(line)
    return lines


def toml_line(exc: tomllib.TOMLDecodeError) -> str:
    """Line and column only: on 3.13 the error has no ``lineno`` and its text can quote keys."""
    m = _TOML_POS.search(str(exc))
    if m:
        return f"config.toml line {m.group(1)}, column {m.group(2)}: not valid TOML"
    return "config.toml: not valid TOML"


def load_errors(load: Callable[[], Settings]) -> tuple[Settings | None, tuple[str, ...]]:
    """Run ``load`` and turn any failure into value-free lines."""
    try:
        return load(), ()
    except tomllib.TOMLDecodeError as exc:
        return None, (toml_line(exc),)
    except ValidationError as exc:
        return None, tuple(validation_lines(exc))
    except OSError as exc:
        return None, (f"config.toml could not be read ({type(exc).__name__})",)
    except Exception as exc:  # anything else: the type only
        return None, (f"config.toml could not be loaded ({type(exc).__name__})",)


# --- the diff ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class FieldChange:
    field: str
    cls: str  # RELOAD | RESTART | NO_EFFECT
    old: str | None = None  # shown text, only for allowlisted fields
    new: str | None = None
    note: str = ""  # "changed", "changed (not shown)", "3 entries, was 2"
    source: str = "config.toml"  # or "models.toml" (specs/020)
    held: str = ""  # why a reloadable field waits for a restart ("held with its API key")
    set_on: str = ""  # NO_EFFECT only: SET_ON_CLI or SET_ON_MODELS

    @property
    def class_text(self) -> str:
        if self.held:
            return f"needs restart ({self.held})"
        if self.cls == NO_EFFECT:
            return f"no effect: set on {self.set_on or SET_ON_CLI}"
        return CLASS_TEXT[self.cls]

    @property
    def models_row(self) -> str:
        """The /models row (its anchor ``#models-row-<row>``) for a model key, else ""."""
        return MODEL_ROWS.get(self.field, "")


def _fmt(value: Any) -> str:
    text = str(value) if not isinstance(value, str) else repr(value)
    return text if len(text) <= 80 else text[:79] + "\N{HORIZONTAL ELLIPSIS}"


def describe(field_name: str, old: Any, new: Any) -> tuple[str | None, str | None, str]:
    if field_name in config.SHOWN_VALUES:
        # A scorer spec or model id is shown only when it cannot be a pasted key (020).
        if any(isinstance(v, str) and config.shown_value(v) is None for v in (old, new)):
            return None, None, "changed (not shown)"
        return _fmt(old), _fmt(new), ""
    if field_name in config.SHOWN_AS_COUNT:
        n_old = len(old) if hasattr(old, "__len__") else 0
        n_new = len(new) if hasattr(new, "__len__") else 0
        return None, None, f"changed ({n_new} entries, was {n_old})"
    if field_name in config.NAME_VALUED:
        if all(isinstance(v, str) and NAME_RE.match(v) for v in (old, new)):
            return old, new, ""
        return None, None, "changed (not shown)"
    return None, None, "changed"


# Scorer sections. Their endpoints are restart fields (config.RESTART_FIELDS); the rest of a
# section waits with them while its credential side (api_key_env, or any .env file) has a
# pending change, so a section never runs half old, half new.
SCORER_SECTIONS = ("scoring.openai_compat", "scoring.openrouter", "scoring.local")
HELD_KEY = "held with its API key"
HELD_DATA_DIR = "models.toml moves with the data folder"


def _model_fields() -> list[str]:
    return [f"{s}.{k}" for s, keys in config.MODEL_KEYS.items() for k in keys]


def _row_of(name: str) -> str:
    section, key = name.split(".", 1)
    if section == "scoring":
        return "screen" if key == "screen_scorer" else "deep"
    return "check" if "entailment" in key else "drafting"


# Model key -> its /models row (model_choices.USE_OF, without importing the console side).
MODEL_ROWS: dict[str, str] = {name: _row_of(name) for name in _model_fields()}


def held_fields(running: Settings, new: Settings, env_pending: bool = False) -> dict[str, str]:
    """Reloadable fields Apply must keep at their running value, with the reason.

    - Every field of a scorer section while a ``.env`` change is pending, or while one of its
      restart fields (``api_key_env``, ``catalog_cache``, the endpoint) changed.
    - The model keys (``config.MODEL_KEYS``) when ``paths.data_dir`` changed: the new values
      come from another data folder's models.toml, which belongs with the restart.
    """
    leaves = config.leaf_fields()
    sections: set[str] = set(SCORER_SECTIONS) if env_pending else set()
    for name in leaves:
        section = name.rsplit(".", 1)[0]
        if (
            section in SCORER_SECTIONS
            and config.reload_class(name) != RELOAD
            and config.field_value(running, name) != config.field_value(new, name)
        ):
            sections.add(section)
    held = {
        n: HELD_KEY
        for n in leaves
        if config.reload_class(n) == RELOAD and n.rsplit(".", 1)[0] in sections
    }
    if running.paths.data_dir != new.paths.data_dir:
        held.update({n: HELD_DATA_DIR for n in _model_fields()})
    return held


def diff(
    running: Settings,
    new: Settings,
    overrides: Iterable[str] = (),
    *,
    env_pending: bool = False,
    models_keys: Iterable[str] = (),
) -> list[FieldChange]:
    """Every leaf field whose value differs, classified. ``overrides`` are fields set on the
    command line (``--host``/``--port``): a change to them has no effect. A held reloadable
    field (``held_fields``) is classed as restart. ``models_keys`` are the fields that come
    from models.toml (listed under that file)."""
    pinned = set(overrides)
    from_models = set(models_keys)
    held = held_fields(running, new, env_pending)
    out: list[FieldChange] = []
    for name in config.leaf_fields():
        a, b = config.field_value(running, name), config.field_value(new, name)
        if a == b:
            continue
        cls = NO_EFFECT if name in pinned else config.reload_class(name)
        why = held.get(name, "") if cls == RELOAD else ""
        if why:
            cls = RESTART
        old, shown_new, note = describe(name, a, b)
        source = "models.toml" if name in from_models else "config.toml"
        set_on = SET_ON_CLI if cls == NO_EFFECT else ""
        out.append(FieldChange(name, cls, old, shown_new, note, source, why, set_on))
    return out


def _base_of(settings: Settings) -> Settings:
    """``settings`` without models.toml: the defaults, config.toml and the environment."""
    layers = getattr(settings, "_models", None)
    return layers.base if isinstance(layers, config.Layers) else settings


def overridden(running: Settings, new: Any, listed: Iterable[str] = ()) -> list[FieldChange]:
    """config.toml edits that have no effect because models.toml sets the field (specs/020
    "Config watch"): a model key the new models.toml layer sets (or blocks) whose config.toml
    value changed. ``listed`` are fields already in the diff (models.toml changed them too)."""
    if not isinstance(new, config.Layers):
        return []
    skip = set(listed)
    old_base = _base_of(running)
    out: list[FieldChange] = []
    for name in _model_fields():
        if name in skip or name not in _layer_keys(new):
            continue
        a, b = config.field_value(old_base, name), config.field_value(new.base, name)
        if a == b:
            continue
        old, shown_new, note = describe(name, a, b)
        out.append(
            FieldChange(name, NO_EFFECT, old, shown_new, note, "config.toml", set_on=SET_ON_MODELS)
        )
    return out


# --- the layers of a model key, for Details ----------------------------------------------------

NOT_SET = "not set"
NOT_SHOWN = "(not shown)"


@dataclass(frozen=True)
class LayerRow:
    """One model key, layer by layer: what each layer says and what is in effect. Every value
    is text from ``show_layer_value``: a model id, a choice, true/false, or a placeholder."""

    field: str
    default: str
    config_toml: str
    models_toml: str
    running: str
    after: str  # what the files on disk give now: in effect after Apply

    @property
    def models_row(self) -> str:
        return MODEL_ROWS.get(self.field, "")


def show_layer_value(value: Any) -> str:
    """A model key's value as Details may show it: never anything key-shaped."""
    if value is None:
        return NOT_SET
    if value == config.BLOCKED_SPEC:
        return "blocked"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return config.shown_value(value) or NOT_SHOWN
    return NOT_SHOWN


def _defaults(section: str) -> BaseModel:
    return {"scoring": config.Scoring, "apply": config.Apply}[section]()


def layer_rows(running: Settings, new: Any, names: Iterable[str]) -> tuple[LayerRow, ...]:
    """Layer by layer for each model key in ``names``: the code default, config.toml,
    models.toml (an invalid value there is never shown, only "invalid"), the running value and
    what the files give now."""
    if not isinstance(new, config.Layers):
        return ()
    wanted = set(names)
    rows: list[LayerRow] = []
    for name in _model_fields():
        if name not in wanted:
            continue
        section, key = name.split(".", 1)
        default = show_layer_value(getattr(_defaults(section), key))
        if (section, key) in new.config_keys:
            in_config = show_layer_value(config.field_value(new.base, name))
        else:
            in_config = NOT_SET
        if key in new.layer.get(section, {}):
            in_models = show_layer_value(new.layer[section][key])
        elif section == "scoring" and key in new.blocked:
            in_models = "invalid (blocks this use)"
        elif key in new.invalid.get(section, {}):
            in_models = "invalid (ignored)"
        elif new.broken:
            in_models = "unreadable"
        else:
            in_models = NOT_SET
        rows.append(
            LayerRow(
                name,
                default,
                in_config,
                in_models,
                show_layer_value(config.field_value(running, name)),
                show_layer_value(config.field_value(new.merged, name)),
            )
        )
    return tuple(rows)


def models_errors(running: Settings, new: Any) -> tuple[str, ...]:
    """Problems in models.toml on disk that the running settings do not already have: the file
    is not TOML, or a known key has a bad value. Shown as validation errors; the running
    settings stay until the file is fixed (specs/020 "Config watch"). A problem the console
    already runs with is in effect (a blocked use stays blocked) and blocks nothing.
    The messages never carry a value other than a scorer spec or model id."""
    if not isinstance(new, config.Layers):
        return ()
    have = getattr(running, "_models", None)
    known = {p.message for p in have.problems} if isinstance(have, config.Layers) else set()
    out: list[str] = []
    for p in new.problems:
        if p.kind not in ("broken", "invalid") or p.message in known:
            continue
        line = p.message if p.message.startswith("models.toml") else f"models.toml {p.message}"
        if line not in out:
            out.append(line)
    return tuple(out)


def _set_leaf(settings: BaseModel, name: str, value: Any) -> None:
    *parents, leaf = name.split(".")
    obj: Any = settings
    for part in parents:
        obj = getattr(obj, part)
    object.__setattr__(obj, leaf, value)


def partial(running: Settings, new: Settings, *, env_pending: bool = False) -> Settings:
    """``new`` with every restart-class or held field kept at its running value (``paths`` and
    ``console`` whole), so Apply swaps in only what reloads on its own. When the data folder
    changes, the running models.toml layers stay too (``_models`` and the blocked scorers)."""
    out = new.model_copy(deep=True)
    object.__setattr__(out, "paths", running.paths)
    object.__setattr__(out, "console", running.console)
    held = held_fields(running, new, env_pending)
    for name in config.leaf_fields():
        if name.startswith(("paths.", "console.")):
            continue
        if config.reload_class(name) != RELOAD or name in held:
            _set_leaf(out, name, config.field_value(running, name))
    if running.paths.data_dir != new.paths.data_dir:
        for attr, src, dst in (
            ("_models", running, out),
            ("_blocked", running.scoring, out.scoring),
        ):
            private = getattr(src, "__pydantic_private__", None) or {}
            if attr in private:
                dst.__pydantic_private__[attr] = private[attr]
    return out


@dataclass(frozen=True)
class EnvChange:
    file: str  # shown with $HOME as ~
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    changed: tuple[str, ...] = ()
    hidden: int = 0  # names that do not pass the shown-name rule
    bare: int = 0  # lines that are not NAME=value

    @property
    def count(self) -> int:
        return len(self.added) + len(self.removed) + len(self.changed) + self.hidden + self.bare


def env_diff(
    path: Path,
    before: Mapping[str, str | None],
    after: Mapping[str, str | None],
    known: Iterable[str],
    home: Path | None = None,
) -> EnvChange | None:
    known_set = set(known)

    def shown(name: str) -> bool:
        return bool(NAME_RE.match(name)) and (name in known_set or name.startswith("JOBHUNTER_"))

    added: list[str] = []
    removed: list[str] = []
    changed: list[str] = []
    hidden = bare = 0
    for name in sorted(set(before) | set(after)):
        a, b = before.get(name, "<absent>"), after.get(name, "<absent>")
        if a == b:
            continue
        if (name in after and after[name] is None) or (name in before and before[name] is None):
            bare += 1  # a bare line: the "name" may be a pasted value
            continue
        if not shown(name):
            hidden += 1
            continue
        (added if name not in before else removed if name not in after else changed).append(name)
    change = EnvChange(
        show_path(path, home), tuple(added), tuple(removed), tuple(changed), hidden, bare
    )
    return change if change.count else None


# --- the state shown to the user ---------------------------------------------------------------


@dataclass(frozen=True)
class State:
    fields: tuple[FieldChange, ...] = ()
    env: tuple[EnvChange, ...] = ()
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()  # scorer problems of the new file (Apply is not blocked)
    models_errors: tuple[str, ...] = ()  # new problems in models.toml: Apply waits for a fix
    layers: tuple[LayerRow, ...] = ()  # model keys in the change, layer by layer (Details)
    replaced: bool = False  # container: config.toml may have been replaced on the host
    key: str = ""  # what Dismiss remembers: the new file's hash plus the .env state
    new_settings: Settings | None = field(default=None, compare=False, repr=False)

    def count(self, cls: str) -> int:
        return sum(1 for f in self.fields if f.cls == cls)

    @property
    def reload_count(self) -> int:
        return self.count(RELOAD)

    @property
    def restart_count(self) -> int:
        return self.count(RESTART)

    @property
    def can_apply(self) -> bool:
        return (
            self.reload_count > 0
            and not self.errors
            and not self.models_errors
            and self.new_settings is not None
        )

    @property
    def needs_restart(self) -> bool:
        return self.restart_count > 0 or bool(self.env) or self.replaced

    @property
    def pending(self) -> bool:
        return bool(self.fields or self.env or self.errors or self.models_errors or self.replaced)

    @property
    def all_errors(self) -> tuple[str, ...]:
        return self.errors + self.models_errors

    @property
    def bar_errors(self) -> tuple[str, ...]:
        return self.all_errors[:MAX_BAR_LINES]

    @property
    def set_on_models(self) -> tuple[FieldChange, ...]:
        return tuple(f for f in self.fields if f.cls == NO_EFFECT and f.set_on == SET_ON_MODELS)


EMPTY = State()


@dataclass
class _Loaded:
    fp: Fingerprint  # config.toml
    digest: str | None  # sha256 of config.toml's bytes, None when absent or unreadable
    settings: Settings | None
    errors: tuple[str, ...]
    models_fp: Fingerprint = (False,)
    models_digest: str | None = None
    models_keys: frozenset[str] = frozenset()  # fields models.toml sets (specs/020)
    layers: Any = None  # the config.Layers loaded, when the loader returns layers


def _layer_keys(layers: Any) -> frozenset[str]:
    """Dotted fields whose effective value comes from models.toml: its valid keys, and the
    spending keys it blocks. An invalid [apply] key falls back to config.toml, so an edit there
    is a config.toml change."""
    if not isinstance(layers, config.Layers):
        return frozenset()
    keys = {f"{s}.{k}" for s, d in layers.layer.items() for k in d}
    keys |= {f"scoring.{k}" for k in layers.blocked}
    return frozenset(keys)


def _sets_nothing(table: dict[str, Any]) -> bool:
    """True when ``table`` holds no value at any depth: only empty tables such as a bare
    ``[scoring]`` header, which parse to ``{"scoring": {}}``, and no keys."""
    return all(isinstance(v, dict) and _sets_nothing(v) for v in table.values())


def _is_blank(path: Path) -> bool:
    """True when ``path`` reads as TOML that sets no keys (empty, whitespace, only comments,
    or only table headers)."""
    try:
        return _sets_nothing(tomllib.loads(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return False


class ConfigWatcher:
    """One per console process. Thread-safe; every dependency is injectable for tests."""

    def __init__(
        self,
        *,
        config_path: Callable[[], Path] | None = None,
        env_candidates: Callable[[], list[Path]] | None = None,
        loader: Callable[[Path], Any] | None = None,
        models_path: Callable[[Settings], Path] | None = None,
        stat: StatFn = os.stat,
        clock: Callable[[], float] = time.monotonic,
        container_mode: bool | None = None,
        cli_overrides: Iterable[str] = (),
        start_env: Mapping[Path, Mapping[str, str | None]] | None = None,
        known_names: Callable[[Settings], Iterable[str]] | None = None,
        home: Path | None = None,
    ) -> None:
        self._config_path = config_path or (lambda: config.config_file(None))
        self._env_candidates = env_candidates or config.env_files
        # load_layers: the same merge the CLI's load_settings does, models.toml included, so the
        # diff compares like with like and Apply keeps models.toml's values (specs/020).
        self._loader = loader or (lambda path: config.load_layers(path))
        self._models_path = models_path or (lambda s: config.models_file(s.paths.data_dir))
        self._stat = stat
        self._clock = clock
        self._container = container.is_container() if container_mode is None else container_mode
        self.cli_overrides = frozenset(cli_overrides)
        self._known_names = known_names or _default_known_names
        self._home = home
        self._lock = threading.Lock()
        self._last_at: float | None = None
        self._state: State = EMPTY
        self._logged_key: str | None = None
        # No settings baseline: the first check loads and diffs. The start fingerprint and hash
        # serve only the container replaced-file rule.
        path = self._config_path()
        self._start_fp = fingerprint(path, stat)
        self._start_digest = _file_digest(path)
        # A file that set something at start and now sets nothing (empty, or only comments)
        # is a save in progress or a mistake, never a change back to the defaults.
        self._start_has_settings = bool(self._start_fp[0]) and not _is_blank(path)
        self._loaded: _Loaded | None = None
        self._replaced = False
        if start_env is None:
            start_env = _START_ENV
        if start_env is None:
            start_env = {p: read_env_digests(p) for p in self._env_candidates()}
        self._env_start: dict[Path, dict[str, str | None]] = {
            Path(p): dict(v) for p, v in start_env.items()
        }
        self._env_fp: dict[Path, Fingerprint] = {}
        self._env_now: dict[Path, dict[str, str | None]] = {}
        self._dismissed: set[str] = set()
        self.applied_at: datetime | None = None
        self.applied_count = 0
        # models.toml (020 M8) is fingerprinted with config.toml; its changes are diffed
        # layer by layer, an overridden config.toml edit is "no effect: set on /models", and
        # Apply and every /models write take ``app.state.settings_lock``.

    # -- public --------------------------------------------------------------------------

    @property
    def start_has_settings(self) -> bool:
        """``config.toml`` set something when the console started (S12b's Restart refuses a
        new process that would find it missing or empty)."""
        return self._start_has_settings

    def check(self, running: Settings, *, force: bool = False) -> State:
        """The current state against ``running``. Stats files at most once per ``THROTTLE_S``
        unless ``force`` (Apply re-runs the check on what is on disk now)."""
        with self._lock:
            now = self._clock()
            if not force and self._last_at is not None and now - self._last_at < THROTTLE_S:
                return self._state
            self._last_at = now
            self._refresh_config(running)
            self._refresh_env()
            self._state = self._compose(running)
            self._log(self._state)
            return self._state

    def dismiss(self, key: str) -> None:
        with self._lock:
            if key:
                self._dismissed.add(key)

    @property
    def last_state(self) -> State:
        """The state of the last check, without stat-ing anything."""
        with self._lock:
            return self._state

    def is_dismissed(self, state: State) -> bool:
        return bool(state.key) and state.key in self._dismissed

    def mark_applied(self, count: int, when: datetime) -> None:
        """After Apply the running settings changed, so an old dismissal no longer describes
        the same diff: forget them all."""
        with self._lock:
            self.applied_at, self.applied_count = when, count
            self._dismissed.clear()

    # -- internals -----------------------------------------------------------------------

    def _refresh_config(self, running: Settings) -> None:
        path = self._config_path()
        fp = fingerprint(path, self._stat)
        mpath = self._models_path(running)
        mfp = fingerprint(mpath, self._stat)
        prev = self._loaded
        if prev is not None and prev.fp == fp and prev.models_fp == mfp:
            return
        digest = _file_digest(path) if fp[0] else None
        mdigest = _file_digest(mpath) if mfp[0] else None
        prev_fp = prev.fp if prev is not None else self._start_fp
        prev_digest = prev.digest if prev is not None else self._start_digest
        if self._container and fp[0]:
            same_inode = prev_fp[0] and prev_fp[1:3] == fp[1:3]
            if fp[6] == 0:
                self._replaced = True  # the pinned inode was unlinked on the host
            elif (
                same_inode
                and fp[5] != prev_fp[5]  # ctime moved
                and fp[3] == prev_fp[3]  # same size
                and fp[4] == prev_fp[4]  # same mtime
                and fp[7] == prev_fp[7]  # same mode: not a chmod
                and (digest in (None, UNREADABLE) or digest == prev_digest)
            ):
                self._replaced = True  # renamed to a backup, then a new file saved by rename
        if (
            prev is not None
            and digest is not None
            and digest == prev.digest
            and mdigest == prev.models_digest
        ):
            # A touch or chmod: adopt the new fingerprints, nothing to load.
            self._loaded = _Loaded(
                fp, digest, prev.settings, prev.errors, mfp, mdigest, prev.models_keys, prev.layers
            )
            return
        start = self._start_fp
        if start[0] and not fp[0]:
            # Loading would give the defaults (higher caps, other scorers): not a change to offer.
            errors: tuple[str, ...] = ("config.toml is missing; save it again",)
            self._loaded = _Loaded(fp, digest, None, errors, mfp, mdigest)
            return
        if fp[0] and path.is_dir():
            # config.toml replaced by a directory: unreadable, never the defaults.
            errors = ("config.toml could not be read (IsADirectoryError)",)
            self._loaded = _Loaded(fp, digest, None, errors, mfp, mdigest)
            return
        if self._start_has_settings and fp[0] and _is_blank(path):
            errors = ("config.toml is empty; save it again",)
            self._loaded = _Loaded(fp, digest, None, errors, mfp, mdigest)
            return
        loaded, errors = load_errors(lambda: self._loader(path))
        settings = loaded.merged if isinstance(loaded, config.Layers) else loaded
        layers = loaded if isinstance(loaded, config.Layers) else None
        self._loaded = _Loaded(
            fp, digest, settings, errors, mfp, mdigest, _layer_keys(loaded), layers
        )

    def _refresh_env(self) -> None:
        paths = list(dict.fromkeys([*self._env_start, *map(Path, self._env_candidates())]))
        for path in paths:
            fp = fingerprint(path, self._stat)
            if self._env_fp.get(path) == fp and path in self._env_now:
                continue
            self._env_fp[path] = fp
            self._env_now[path] = read_env_digests(path) if fp[0] else {}

    def _compose(self, running: Settings) -> State:
        loaded = self._loaded
        assert loaded is not None
        known = list(self._known_names(running))
        env: list[EnvChange] = []
        for path, now_digests in self._env_now.items():
            change = env_diff(path, self._env_start.get(path, {}), now_digests, known, self._home)
            if change is not None:
                env.append(change)
        fields: tuple[FieldChange, ...] = ()
        warnings: tuple[str, ...] = ()
        m_errors: tuple[str, ...] = ()
        rows: tuple[LayerRow, ...] = ()
        if loaded.settings is not None:
            models_keys = loaded.models_keys | _layer_keys(getattr(running, "_models", None))
            changed = diff(
                running,
                loaded.settings,
                self.cli_overrides,
                env_pending=bool(env),
                models_keys=models_keys,
            )
            listed = [f.field for f in changed]
            fields = tuple(changed + overridden(running, loaded.layers, listed))
            m_errors = models_errors(running, loaded.layers)
            if fields:
                warnings = scorer_warnings(loaded.settings)
            rows = layer_rows(running, loaded.layers, [f.field for f in fields])
        # models.toml enters the key by what it changes, not by its bytes: the console's own
        # writes (/models) rewrite the file but are already running, so they neither move an
        # Apply's key ("changed again") nor bring back a dismissed notice (specs/020).
        models_part = (
            sorted(
                (f.field, repr(config.field_value(loaded.settings, f.field)))
                for f in fields
                if f.source == "models.toml"
            )
            if loaded.settings is not None
            else loaded.models_digest,
            m_errors,
        )
        key_src = repr(
            (
                loaded.digest,
                models_part,
                sorted((str(p), sorted(d.items())) for p, d in self._env_now.items()),
            )
        )
        return State(
            fields=fields,
            env=tuple(env),
            errors=loaded.errors,
            warnings=warnings,
            models_errors=m_errors,
            layers=rows,
            replaced=self._replaced,
            key=hashlib.sha256(_SALT + key_src.encode()).hexdigest()[:32],
            new_settings=loaded.settings,
        )

    def _log(self, state: State) -> None:
        if state.key == self._logged_key or not state.pending:
            self._logged_key = state.key
            return
        self._logged_key = state.key
        for line in state.errors:
            logger.warning("config watch: config.toml has errors: %s", line)
        for line in state.models_errors:
            logger.warning("config watch: models.toml has errors: %s", line)
        if state.fields:
            logger.info(
                "config watch: config.toml changed: %s",
                ", ".join(f"{f.field} ({f.class_text})" for f in state.fields),
            )
        for change in state.env:
            logger.info(
                "config watch: %s changed: %d keys (%s)",
                change.file,
                change.count,
                ", ".join((*change.added, *change.removed, *change.changed)) or "names not shown",
            )
        if state.replaced:
            logger.warning("config watch: config.toml may have been replaced on the host")


def scorer_warnings(settings: Settings) -> tuple[str, ...]:
    """``scorer_problems`` messages, which quote the spec; a spec that may be a pasted key is
    named by field only."""
    out: list[str] = []
    for name, message in settings.scoring.scorer_problems().items():
        spec = getattr(settings.scoring, name, "")
        if config.shown_value(spec) is None:
            out.append(f"{name}: not a usable scorer spec (the value is not shown)")
        else:
            out.append(message)
    return tuple(out)


UNREADABLE = "unreadable"


def _file_digest(path: Path) -> str | None:
    """SHA-256 of the file's bytes; None when it is absent; ``UNREADABLE`` when it exists but
    cannot be read, so a file that goes from absent to unreadable is a change, not a touch."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except FileNotFoundError:
        return None
    except OSError:
        return UNREADABLE


_registry_names: list[str] | None = None


def _default_known_names(settings: Settings) -> list[str]:
    from jobhunter import secrets

    global _registry_names
    if _registry_names is None:
        _registry_names = secrets.registry_auth_names()
    return secrets.known_names(settings, _registry_names)


# --- the generated help block ------------------------------------------------------------------


def reload_table() -> str:
    """The "which settings reload" table for help/settings-and-secrets.md (block
    ``config-reload``): one row per TOML table, by the class most of its keys share, naming the
    keys that differ."""
    sections: dict[str, list[str]] = {}
    for name in config.leaf_fields():
        sections.setdefault(name.rsplit(".", 1)[0], []).append(name)
    lines = ["| Table in config.toml | After you save a change |", "|---|---|"]
    for section, names in sections.items():
        n_reload = sum(1 for n in names if config.reload_class(n) == RELOAD)
        main = RELOAD if n_reload * 2 >= len(names) else RESTART
        others = [n.rsplit(".", 1)[1] for n in names if config.reload_class(n) != main]
        text = _when(main)
        if others:
            keys = ", ".join(f"`{k}`" for k in others)
            text += f"; except {keys}: {_when(RESTART if main == RELOAD else RELOAD).lower()}"
        lines.append(f"| `[{section}]` | {text} |")
    lines.append(f"| `.env` files, `*_FILE` and `JOBHUNTER_*` variables | {_when(RESTART)} |")
    return "\n".join(lines)


def _when(cls: str) -> str:
    return "Apply now (reloads in place)" if cls == RELOAD else "Restart the console"
