"""Readiness checks: what jobhunter needs, what is missing, and how to fix it (specs/019).

One module computes every check; ``/setup``, the banner, ``jobhunter doctor`` and the snapshot
only render them (019 rule 5). This ticket (S1) is the local half: every check reads files
(``stat`` and small reads), environment variables, the loaded settings and the database
through a read-only connection, and nothing else.

Rules this module keeps (019 "Rules for every check"):

- **Local and free.** No network, no model call, no subprocess other than ``shutil.which``, no
  keyring or D-Bus, no ``chmod``, no walk of a directory tree. ``tests/test_readiness.py``
  patches ``subprocess.Popen`` and ``keyring.get_password`` to raise and runs every check.
- **Never a value.** No key, token, prefix, length, email address, account name or contact
  string in any ``Check``. Secret rows say "set (env)", "set (file /run/secrets/...)", "unset"
  by reusing ``SecretState.describe``. Paths are shown, with ``$HOME`` as ``~``. Free-form
  error text (``source.status_note``, ``run_source.error``) is mapped to fixed wording.
- **Format, not validity.** A key's presence, source and shape; a wrong prefix is a note.

Read-only database connections on a WAL database may create ``jobhunter.db-wal`` and
``jobhunter.db-shm`` next to the database (SQLite needs them to read); they hold no new data.
Doctor (S2) should say so rather than promise "writes nothing".

``cheap_only=True`` (the banner) skips the alert-subscription counts and the backup listing.
The slow inputs are cached per process: the registry's USAJOBS variable names and the lenient
profile state (keyed on file modification times).
"""

from __future__ import annotations

import functools
import json
import os
import sqlite3
import stat as stat_module
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from importlib import util as importlib_util
from pathlib import Path
from typing import Any

from jobhunter import container, secrets
from jobhunter.config import Settings, config_file, resolve_path
from jobhunter.scoring import scorers
from jobhunter.scoring.profile import ProfileError, _resolve_resume

# --- vocabulary ----------------------------------------------------------------------------

HOST = "host"
CONTAINER = "container"
HOST_MANAGING = "host-managing"
MODES = (HOST, CONTAINER, HOST_MANAGING)

ESSENTIAL = "essential"
RECOMMENDED = "recommended"
OPTIONAL = "optional"
INFO = "info"

OK = "ok"
MISSING = "missing"
BROKEN = "broken"
OPTIONAL_STATUS = "optional"
UNKNOWN = "unknown"
FAILING = (MISSING, BROKEN)

BASICS = "Basics"
PROFILE = "Profile"
SCORING = "Scoring"
SOURCES = "Sources"
EMAIL = "Email alerts"
APPLY = "Apply"
RUNNING = "Running"
GROUPS = (BASICS, PROFILE, SCORING, SOURCES, EMAIL, APPLY, RUNNING)

PLACEHOLDER_MARK = "<you>"
GMAIL_AUTH_FILE = "gmail-auth.json"
GMAIL_TESTING_DAYS = 7  # a refresh token of an app in Testing lasts 7 days
MAIL_SOURCE_KEY = "us-mailalerts"
NEWEST_BACKUP_MAX_AGE = timedelta(days=3)
LABEL_WINDOW = timedelta(days=14)
ARRIVAL_WINDOW = timedelta(days=30)
RECENT_OK = timedelta(days=2)
UNFINISHED_RUN_LIMIT = timedelta(hours=6)
RELATIVIZE_COMMAND = "jobhunter db backup && jobhunter db relativize-paths --apply"

# The fixed list of check ids (019: "stable, from a fixed list"). Routes that take a check id
# (dismiss, live checks) validate against this.
CHECK_IDS = (
    "config.file",
    "db.schema",
    "fetch.contact",
    "data.permissions",
    "profile.preferences",
    "profile.resume",
    "profile.completeness",
    "scorer.screen",
    "scorer.caps",
    "scorer.deep",
    "models.choices",
    "source.usajobs",
    "run.schedule",
    "run.last",
    "mail.client",
    "mail.address",
    "mail.token",
    "mail.label",
    "mail.subscriptions",
    "mail.parsers",
    "apply.runner",
    "apply.cap",
    "ext.pairing",
    "pdf.export",
    "secrets.valid",
    "backups.auto",
    "console.exposure",
    "containers.deployment",
)

# The help topic for each check: "topic" or "topic#heading-id" (an id that
# console/help renders; the test requires it). Only the secrets row differs by mode.
HELP_LINKS = {
    "config.file": "troubleshooting#unknown-key-in-configtoml",
    "db.schema": "troubleshooting#migrations-pending-or-database-newer-than-this-jobhunter",
    "fetch.contact": "settings-and-secrets#configtoml",
    "data.permissions": "getting-started#create-the-data-folder",
    "profile.preferences": "profile-and-resume#preferencesyaml",
    "profile.resume": "profile-and-resume#the-resume",
    "profile.completeness": "profile-and-resume#preferencesyaml",
    "scorer.screen": "scorers",
    "scorer.caps": "costs-and-credits#the-caps",
    "scorer.deep": "costs-and-credits#the-deep-pass",
    "source.usajobs": "troubleshooting#usajobs-credentials-missing",
    "run.schedule": "nightly-runs",
    "run.last": "nightly-runs",
    "mail.client": "email-alerts#step-1-google-client-json",
    "mail.address": "email-alerts#step-2-alerts-address",
    "mail.token": "email-alerts#step-3-gmail-token",
    "mail.label": "email-alerts#step-4-label-and-filter",
    "mail.subscriptions": "email-alerts#step-5-board-subscriptions",
    "mail.parsers": "email-alerts",
    "apply.runner": "drafting#the-cli-runner-log-in-to-claude-code",
    "apply.cap": "costs-and-credits#the-caps",
    "ext.pairing": "browser-extension",
    "pdf.export": "troubleshooting#pdf-export-is-unavailable",
    "secrets.valid": "settings-and-secrets#where-to-keep-secrets-on-the-host",
    "backups.auto": "nightly-runs",
    "console.exposure": "troubleshooting#403-host-is-not-a-loopback-address",
    "containers.deployment": "containers#first-run-checklist",
}
# The secrets row in the two container modes: the podman secret how-to.
HELP_LINKS_BY_MODE = {
    (mode, "secrets.valid"): "containers#secrets" for mode in (CONTAINER, HOST_MANAGING)
}


def help_for(check_id: str, mode: str) -> str:
    """The help path for a check, e.g. ``email-alerts#step-1-google-client-json``."""
    return HELP_LINKS_BY_MODE.get((mode, check_id)) or HELP_LINKS.get(check_id, "getting-started")


RESTART_COMMAND = {
    HOST: "Ctrl-C and `jobhunter console`",
    CONTAINER: "`systemctl --user restart jobhunter-console`",
    HOST_MANAGING: "`systemctl --user restart jobhunter-console`",
}


@dataclass(frozen=True)
class Fix:
    """How to fix a row: a route on /setup, a command, a link, or numbered steps.

    ``alt`` is an alternative way out ("or pick another scorer"). Nothing here is a value."""

    kind: str  # "route" | "command" | "link" | "steps"
    text: str  # the route, command, URL or one-line summary of the steps
    steps: tuple[str, ...] = ()
    alt: Fix | None = None


@dataclass(frozen=True)
class Check:
    id: str  # stable, from CHECK_IDS
    group: str  # one of GROUPS
    level: str  # essential | recommended | optional | info
    status: str  # ok | missing | broken | optional | unknown
    due: bool  # ok now, but needs action soon (a Gmail token on day 6)
    title: str
    detail: str  # fixed wording, never a value
    unlocks: str
    fix: Fix | None = None
    source: str = ""  # where the value came from: "config.toml", "env", "podman secret"
    # Additions to the 019 sketch, all defaulted: rows whose value is read once at process
    # start (rule 6), and the position in the email guide (0: not a step).
    reads_at_start: bool = False
    step: int = 0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def essentials_ok(checks: Iterable[Check]) -> bool:
    """True when no essential is missing or broken. ``unknown`` never counts as missing."""
    return not any(c.level == ESSENTIAL and c.status in FAILING for c in checks)


def failing(checks: Iterable[Check], level: str) -> list[Check]:
    return [c for c in checks if c.level == level and (c.status in FAILING or c.due)]


# --- mode ------------------------------------------------------------------------------------


def detect_mode(data_dir: Path | None = None, *, settings: Settings | None = None) -> str:
    """``container`` when ``JOBHUNTER_CONTAINER=1``; ``host-managing`` on the host when
    ``<data dir>/deployment.toml`` exists; else ``host``."""
    if container.is_container():
        return CONTAINER
    if data_dir is None and settings is not None:
        data_dir = resolve_path(settings.paths.data_dir)
    if data_dir is not None:
        from jobhunter.core import db

        if db.deployment_marker(Path(data_dir)) is not None:
            return HOST_MANAGING
    return HOST


# --- context and small helpers ---------------------------------------------------------------


@dataclass
class _Ctx:
    settings: Settings
    report: secrets.Report | None
    now: datetime
    mode: str
    registry_rows: Iterable[Any] | None
    environ: Mapping[str, str]
    home: Path
    data_dir: Path
    db_path: Path
    conn: sqlite3.Connection | None = None
    prefs: Any = None  # a PrefsState, computed once per call

    def unit_dir(self) -> Path:
        from jobhunter.ops import preflight

        return preflight.default_unit_dir(self.home)


def _show(path: Path | str, ctx: _Ctx) -> str:
    """A path as the user sees it (see ``display_path``)."""
    return display_path(path, ctx.mode, ctx.home)


def display_path(path: Path | str, mode: str, home: Path | None = None) -> str:
    """A path as the user sees it: ``$HOME`` as ``~``; in container mode the host path with the
    container path in brackets (019 "Modes")."""
    text = str(path)
    home = home if home is not None else Path.home()
    if mode == CONTAINER:
        from jobhunter.ops import preflight

        for inside, outside in (
            (container.DATA_DIR, preflight.DATA_SOURCE),
            (container.CONFIG_DIR, preflight.CONFIG_SOURCE.parent),
            (container.CACHE_DIR, preflight.CACHE_SOURCE),
        ):
            if text == inside or text.startswith(inside + "/"):
                return f"~/{outside}{text[len(inside) :]} [{text}]"
    for prefix in dict.fromkeys((str(home), os.path.realpath(home))):
        if text == prefix:
            return "~"
        if text.startswith(prefix + "/"):
            return "~" + text[len(prefix) :]
    return text


def _describe(state: secrets.SecretState, ctx: _Ctx) -> str:
    """``SecretState.describe`` with any path shown through ``_show`` ("set (.env ~/.env)")."""
    if state.state in (secrets.FILE, secrets.DOTENV):
        return f"set ({state.state} {_show(state.detail, ctx)})"
    return state.describe()


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _mtime_ns(path: Path) -> tuple[int, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return st.st_mtime_ns, st.st_size


def _is_placeholder(value: str) -> bool:
    return not value.strip() or PLACEHOLDER_MARK in value


def _query(ctx: _Ctx, sql: str, params: tuple = ()) -> list[sqlite3.Row] | None:
    """One small read-only query; ``None`` when there is no database or it cannot answer."""
    if ctx.conn is None:
        return None
    try:
        return ctx.conn.execute(sql, params).fetchall()
    except sqlite3.Error:
        return None


def _open_ro(db_path: Path) -> sqlite3.Connection | None:
    if not db_path.is_file():
        return None
    try:
        conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    conn.row_factory = sqlite3.Row
    return conn


# --- cached inputs -----------------------------------------------------------------------------


@functools.cache
def _default_usajobs_names() -> tuple[str | None, str | None]:
    """Parsing ``registry.yaml`` takes about 0.5 s, so the no-argument path is cached. A caller
    that already loaded the registry passes ``registry_rows`` instead."""
    from jobhunter.ops import preflight

    return preflight.usajobs_auth(None)


def usajobs_names(registry_rows: Iterable[Any] | None) -> tuple[str | None, str | None]:
    from jobhunter.ops import preflight

    if registry_rows is None:
        return _default_usajobs_names()
    return preflight.usajobs_auth(registry_rows)


_PREFS_CACHE: dict[tuple, tuple[tuple, Any]] = {}


def clear_caches() -> None:
    """Forget the per-process caches (tests, and a console that reloads its settings)."""
    _default_usajobs_names.cache_clear()
    _PREFS_CACHE.clear()


def _signature(settings: Settings, extra: Path | None = None) -> tuple:
    """Modification times of ``preferences.yaml``, the resume file(s) and the resume folder."""
    from jobhunter.scoring.profile import PREFERENCES_FILE

    profile_dir = resolve_path(settings.paths.profile_dir)
    resume = resolve_path(settings.paths.resume_path)
    paths = [profile_dir / PREFERENCES_FILE, resume]
    if extra is not None and extra != resume:
        paths.append(extra)
    sig: list[Any] = []
    for path in paths:
        sig.append((str(path), _mtime_ns(path)))
        try:
            if path.is_dir():
                # one level: a file added to or removed from the folder changes the folder's
                # mtime, an edit to a file changes the file's
                sig.extend((p.name, _mtime_ns(p)) for p in sorted(path.glob("*.md")))
        except OSError:
            pass
    return tuple(sig)


def prefs_state(settings: Settings) -> Any:
    """``prefs_graceful.lenient_state`` (50-60 ms), cached per process and keyed on file
    modification times."""
    from jobhunter.console import prefs_graceful

    key = (
        str(settings.paths.profile_dir),
        str(settings.paths.resume_path),
    )
    cached = _PREFS_CACHE.get(key)
    if cached is not None:
        sig, state = cached
        extra = Path(state.resume_target) if state.resume_target else None
        if sig == _signature(settings, extra):
            return state
    before = _signature(settings)
    state = prefs_graceful.lenient_state(settings)
    extra = Path(state.resume_target) if state.resume_target else None
    after = _signature(settings, extra)
    # cache only when nothing changed while it loaded, so old content never sits under a new
    # signature
    if _signature(settings) == before:
        _PREFS_CACHE[key] = (after, state)
    else:
        _PREFS_CACHE.pop(key, None)
    return state


# --- secrets ---------------------------------------------------------------------------------


def _secret_state(ctx: _Ctx, name: str) -> secrets.SecretState:
    """The startup report's state for ``name``; for a name the report does not know (custom
    registry names), the same rules applied to the environment."""
    if ctx.report is not None:
        for state in ctx.report.states:
            if state.name == name:
                return state
    if name not in ctx.environ:
        return secrets.SecretState(name, secrets.UNSET)
    value, reason = secrets.clean(ctx.environ[name])
    if reason:
        return secrets.SecretState(name, secrets.INVALID, reason)
    if value is None:
        return secrets.SecretState(name, secrets.UNSET)
    return secrets.SecretState(name, secrets.ENV)


def _dropin_present(ctx: _Ctx, secret: str, unit: str | None = None) -> bool:
    """Preflight's drop-in for the unit exists (a stat). It exists only when the podman secret
    did at the last preflight (019 "Modes")."""
    return _dropin_exists(ctx.unit_dir(), secret, unit)


def _dropin_exists(unit_dir: Path, secret: str, unit: str | None = None) -> bool:
    from jobhunter.ops import preflight

    path = preflight.dropin_path(unit_dir, unit or preflight.RUN, secret)
    try:
        return path.is_file()
    except OSError:
        return False


@dataclass(frozen=True)
class _Presence:
    kind: str  # "present" | "absent" | "invalid" | "unseen"
    detail: str
    source: str


def _presence(
    ctx: _Ctx, env_name: str, podman_secret: str | None, *, console_sees: bool = True
) -> _Presence:
    """Is ``env_name`` available where it matters, per mode. ``console_sees`` is False for
    secrets the console container never gets (USAJOBS, Gmail, the Google client)."""
    if ctx.mode == HOST_MANAGING and podman_secret:
        if _dropin_present(ctx, podman_secret):
            return _Presence("present", f"set (podman secret {podman_secret})", "podman secret")
        return _Presence("absent", "unset", "podman secret")
    state = _secret_state(ctx, env_name)
    if state.state == secrets.INVALID:
        return _Presence("invalid", f"{env_name} {_describe(state, ctx)}", "invalid")
    if state.state == secrets.UNSET:
        if ctx.mode == CONTAINER and not console_sees:
            return _Presence("unseen", "not visible from this container", "")
        return _Presence("absent", f"{env_name} unset", "")
    source = {secrets.ENV: "env", secrets.FILE: "podman secret"}.get(state.state, state.state)
    return _Presence("present", f"{env_name} {_describe(state, ctx)}", source)


def _secret_fix(
    ctx: _Ctx, var: str, podman_secret: str, *, replace: bool = False, alt: Fix | None = None
) -> Fix:
    """Where a key goes, per mode (019 "No secret is ever typed into the console")."""
    restart = RESTART_COMMAND[ctx.mode]
    if ctx.mode == HOST:
        return Fix(
            "steps",
            f"add {var} to ~/.env, then restart the console",
            (
                "[ -e ~/.env ] || install -m 600 /dev/null ~/.env",
                f"add {var}=... to ~/.env with an editor",
                f"restart the console ({restart})",
            ),
            alt,
        )
    flag = " --replace" if replace else ""
    return Fix(
        "steps",
        f"create the podman secret {podman_secret}, then run container preflight",
        (
            f"read -rs KEY && printf '%s' \"$KEY\" | podman secret create{flag} "
            f"{podman_secret} - && unset KEY",
            "jobhunter container preflight",
            "systemctl --user daemon-reload",
            "systemctl --user restart jobhunter-console",
        ),
        alt,
    )


# --- scorer credentials ------------------------------------------------------------------------

_SECRET_FOR_PROVIDER = {
    "anthropic": "jobhunter_anthropic",
    "openrouter": "jobhunter_openrouter",
    "jev": "jobhunter_openrouter",
    "decisions": "jobhunter_openrouter",
    "openai-compat": "jobhunter_openai_compat",
    "local": "jobhunter_llama",
}


def _provider(spec: str) -> str:
    provider, sep, _ = spec.partition(":")
    return provider if sep else "anthropic"


def scorer_credential(
    spec: str,
    scoring: Any,
    *,
    mode: str,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> scorers.Credential:
    """``credential_for``, except in host-managing mode where the containers' secrets decide,
    not this shell's environment: a secret counts when preflight's drop-in exists (a stat).
    Shared with ``model_access`` so the setup rows and the model rows cannot disagree."""
    cred = scorers.credential_for(spec, scoring, environ=environ, home=home)
    if mode != HOST_MANAGING or cred.status == scorers.UNKNOWN:
        return cred
    secret = _SECRET_FOR_PROVIDER.get(_provider(spec))
    if secret is None:
        return cred
    if not cred.names and cred.status == scorers.PRESENT:
        return cred  # openai-compat with an empty api_key_env: nothing to look up
    from jobhunter.ops import preflight

    if _dropin_exists(preflight.default_unit_dir(home), secret):
        return scorers.Credential(scorers.PRESENT, cred.names or (secret,))
    return scorers.Credential(scorers.MISSING, cred.names)


def secret_for_provider(spec: str) -> str | None:
    """The podman secret that carries ``spec``'s credential to the containers."""
    return _SECRET_FOR_PROVIDER.get(_provider(spec))


def _credential(ctx: _Ctx, spec: str) -> scorers.Credential:
    return scorer_credential(
        spec, ctx.settings.scoring, mode=ctx.mode, environ=ctx.environ, home=ctx.home
    )


def _scorer_row(
    ctx: _Ctx,
    *,
    check_id: str,
    title: str,
    spec: str,
    field_name: str,
    level: str,
    unlocks: str,
) -> Check:
    scoring = ctx.settings.scoring
    layers = ctx.settings.models_layers
    if layers is not None:
        where = layers.source("scoring", field_name)
        if where in ("override", "models.toml (invalid)"):
            where = "models.toml" if where != "override" else "config.toml"
    else:
        where = "config.toml" if field_name in scoring.model_fields_set else "default"
    problem = scoring.scorer_problems().get(field_name)
    if problem and field_name in scoring._blocked:
        # models.toml set it to an invalid value: the use is blocked, not fallen back
        # (specs/020 Storage). M10 adds the full "Model choices" row.
        return Check(
            check_id, SCORING, level, BROKEN, False, title, problem, unlocks,
            Fix("command", "jobhunter models show, then jobhunter models reset or set"),
            "models.toml", reads_at_start=True,
        )  # fmt: skip
    if problem and where == "default":
        where = "config.toml"  # an invalid value was written somewhere; code defaults are valid
    if problem:
        return Check(
            check_id, SCORING, level, BROKEN, False, title,
            f"{field_name} is not a valid scorer spec ({where}); fix it in {where}",
            unlocks,
            Fix("command", f"edit [scoring] in {where}"),
            where, reads_at_start=True,
        )  # fmt: skip
    cred = _credential(ctx, spec)
    provider = _provider(spec)
    var = cred.names[0] if cred.names else ""
    if cred.status == scorers.PRESENT:
        if cred.note == scorers.SDK_NOTE:
            how, source = scorers.SDK_NOTE, "SDK profile"
        elif ctx.mode == HOST_MANAGING:
            how, source = f"{var} set (podman secret)", "podman secret"
        elif var:
            presence = _presence(ctx, var, None)
            how, source = presence.detail, presence.source or "env"
        else:
            how, source = cred.note, where
        note = f"; {cred.note}" if cred.note and cred.note != scorers.SDK_NOTE and var else ""
        return Check(
            check_id, SCORING, level, OK, False, title, f"{spec}; {how}{note}", unlocks,
            None, source, reads_at_start=True,
        )  # fmt: skip
    if cred.status == scorers.UNKNOWN:
        return Check(
            check_id, SCORING, level, UNKNOWN, False, title,
            f"{spec}; {cred.note or 'only the server can say'}", unlocks,
            Fix("route", "/setup"), where, reads_at_start=True,
        )  # fmt: skip
    # missing
    state = _secret_state(ctx, var) if var else None
    invalid = state is not None and state.state == secrets.INVALID
    secret = _SECRET_FOR_PROVIDER.get(provider, "jobhunter_anthropic")
    alt = None
    if field_name == "screen_scorer":
        alt = Fix(
            "command",
            'pick another scorer in config.toml, e.g.  [scoring] screen_scorer = "jev:<model>"',
        )
    if invalid and state is not None:
        detail = f"{spec}; {var} {state.describe()}"
        status = BROKEN
    else:
        names = " or ".join(cred.names) or "a credential"
        detail = f"{spec} ({where}) is used and no credential is set ({names})"
        status = MISSING
    return Check(
        check_id, SCORING, level, status, False, title, detail, unlocks,
        _secret_fix(ctx, var or "ANTHROPIC_API_KEY", secret, replace=invalid, alt=alt),
        where, reads_at_start=True,
    )  # fmt: skip


# --- the checks --------------------------------------------------------------------------------


def _config_file(ctx: _Ctx) -> Check:
    path = config_file(None)
    if path.is_file():
        detail, source = f"read from {_show(path, ctx)}", "config.toml"
    else:
        detail, source = f"no file at {_show(path, ctx)}; defaults are in use", "default"
    return Check(
        "config.file", BASICS, ESSENTIAL, OK, False, "Settings file", detail,
        "Every command and the console",
        None, source, reads_at_start=True,
    )  # fmt: skip


def _db_schema(ctx: _Ctx) -> Check:
    from jobhunter.core import db
    from jobhunter.ops import version

    title, unlocks = "Database", "The console and every command"
    schema, state, unknown, pending = version.database_state(ctx.db_path)
    where = _show(ctx.db_path, ctx)
    marker = db.deployment_marker(ctx.data_dir) is not None
    if state == "missing":
        return Check("db.schema", BASICS, ESSENTIAL, OK, False, title,
                     f"not created yet ({where}); a command will create it", unlocks)  # fmt: skip
    if state == "new":
        return Check("db.schema", BASICS, ESSENTIAL, OK, False, title,
                     f"empty ({where}); the first command sets it up", unlocks)  # fmt: skip
    if state == "current":
        return Check("db.schema", BASICS, ESSENTIAL, OK, False, title,
                     f"schema {schema}, up to date ({where})", unlocks)  # fmt: skip
    if state == "pending":
        if marker or ctx.mode != HOST:
            return Check(
                "db.schema", BASICS, ESSENTIAL, BROKEN, False, title,
                f"schema {schema} needs {len(pending)} migration(s); commands refuse until you "
                "upgrade", unlocks, Fix("command", "jobhunter upgrade"),
            )  # fmt: skip
        return Check("db.schema", BASICS, ESSENTIAL, OK, False, title,
                     f"schema {schema} is behind; the next command migrates it after a backup",
                     unlocks)  # fmt: skip
    if state == "newer":
        return Check(
            "db.schema", BASICS, ESSENTIAL, BROKEN, False, title,
            f"schema {schema} is newer than this code ({len(unknown)} unknown migration(s))",
            unlocks, Fix("command", "jobhunter version"),
        )  # fmt: skip
    return Check("db.schema", BASICS, ESSENTIAL, BROKEN, False, title,
                 f"the database could not be read ({where})", unlocks,
                 Fix("command", "jobhunter doctor"))  # fmt: skip


def _contact(ctx: _Ctx) -> Check:
    value = ctx.settings.fetch.user_agent_contact
    explicit = "user_agent_contact" in ctx.settings.fetch.model_fields_set
    title = "Contact for the User-Agent"
    unlocks = "Honest identification to job boards (008)"
    if _is_placeholder(value):
        return Check(
            "fetch.contact", BASICS, ESSENTIAL, BROKEN, False, title,
            "still the placeholder, so every request says so; set a real contact",
            unlocks,
            Fix("steps", "add [fetch] user_agent_contact to config.toml, then restart",
                ("add to config.toml:  [fetch]  user_agent_contact = \"<you>@example.com\"",
                 f"restart the console ({RESTART_COMMAND[ctx.mode]})")),
            "default" if not explicit else "config.toml", reads_at_start=True,
        )  # fmt: skip
    return Check("fetch.contact", BASICS, ESSENTIAL, OK, False, title, "set in config.toml",
                 unlocks, None, "config.toml", reads_at_start=True)  # fmt: skip


def _permissions(ctx: _Ctx) -> Check:
    title, unlocks = "Data permissions", "Privacy of your resume, profile and database"
    loose: list[str] = []
    for path, want in ((ctx.data_dir, "0700"), (ctx.db_path, "0600")):
        try:
            mode = stat_module.S_IMODE(path.stat().st_mode)
        except OSError:
            continue
        if mode & 0o077:
            loose.append(f"{_show(path, ctx)} is {mode:04o}, should be {want}")
    if loose:
        return Check(
            "data.permissions", BASICS, RECOMMENDED, BROKEN, False, title,
            "; ".join(loose), unlocks,
            Fix("command", f"chmod 700 {_show(ctx.data_dir, ctx)} && chmod 600 "
                           f"{_show(ctx.db_path, ctx)}"),
        )  # fmt: skip
    return Check("data.permissions", BASICS, RECOMMENDED, OK, False, title,
                 "data directory and database are private", unlocks)  # fmt: skip


def _fields(errors: Mapping[str, str]) -> str:
    names = sorted(errors)
    return ", ".join(names[:6]) + (f" and {len(names) - 6} more" if len(names) > 6 else "")


def _preferences(ctx: _Ctx) -> Check:
    state = ctx.prefs
    title = "Preferences"
    unlocks = "USAJOBS queries, the prefilter, scoring, buckets and packets"
    where = _show(state.prefs_file, ctx)
    if state.kind == "missing":
        return Check(
            "profile.preferences", PROFILE, ESSENTIAL, MISSING, False, title,
            f"no preferences file at {where}", unlocks,
            Fix("route", "/setup/starter-prefs"), "profile/preferences.yaml",
        )  # fmt: skip
    if state.kind == "broken":
        if state.yaml_error and not state.errors:
            detail = f"{where} is not valid YAML or cannot be read"
        else:
            detail = f"{where} has invalid fields: {_fields(state.errors)}"
        return Check(
            "profile.preferences", PROFILE, ESSENTIAL, BROKEN, False, title, detail, unlocks,
            Fix("route", "/prefs"), "profile/preferences.yaml",
        )  # fmt: skip
    return Check("profile.preferences", PROFILE, ESSENTIAL, OK, False, title,
                 f"valid ({where})", unlocks, None, "profile/preferences.yaml")  # fmt: skip


def _try_resolve(
    profile_dir: Path, prefs_file: Path, yaml_value: str | None, argument: Path | None
) -> tuple[Path | None, str | None]:
    try:
        return _resolve_resume(profile_dir, prefs_file, yaml_value, argument), None
    except ProfileError as exc:
        return None, str(exc)


def _within(path: Path, root: Path) -> bool:
    norm = os.path.normpath
    return norm(str(path)) == norm(str(root)) or norm(str(path)).startswith(norm(str(root)) + "/")


def _resume(ctx: _Ctx) -> Check:
    """Resume resolution. Until S0 lands the console (``load_profile_for``: the YAML value
    wins) and the nightly run / ``score`` (the configured path always wins) can read different
    files; this row compares both (019 "Resume resolution differs between commands")."""
    state = ctx.prefs
    settings = ctx.settings
    title = "Resume"
    unlocks = "Scoring, the scoring version and packet drafting"
    profile_dir = state.profile_dir
    yaml_value = state.profile.resume_path or None
    configured = resolve_path(settings.paths.resume_path)
    console_file, console_err = _try_resolve(
        profile_dir, state.prefs_file, yaml_value, None if yaml_value else configured
    )
    run_file, _run_err = _try_resolve(profile_dir, state.prefs_file, yaml_value, configured)
    fix = Fix("route", "/setup/resume")
    source = "profile/preferences.yaml" if yaml_value else "paths.resume_path"

    if yaml_value and ctx.mode in (CONTAINER, HOST_MANAGING):
        absolute = Path(yaml_value).expanduser().is_absolute()
        outside = not absolute and not _within(
            Path(os.path.normpath(profile_dir / yaml_value)), ctx.data_dir
        )
        if absolute or outside:
            return Check(
                "profile.resume", PROFILE, ESSENTIAL, BROKEN, False, title,
                "resume_path in preferences.yaml is absolute or outside the data directory, "
                "which does not resolve in a container",
                unlocks,
                Fix("steps", "upload the resume again on /setup, or set resume_path relative "
                             "to the profile folder", ("upload the resume again on /setup",
                             "or set resume_path in preferences.yaml relative to the profile "
                             "folder")),
                source,
            )  # fmt: skip

    different = yaml_value and (
        (console_file is None) != (run_file is None)
        or (console_file is not None and run_file is not None
            and os.path.realpath(console_file) != os.path.realpath(run_file))
    )  # fmt: skip
    if different:
        a = _show(console_file, ctx) if console_file else "none found"
        b = _show(run_file, ctx) if run_file else "none found"
        return Check(
            "profile.resume", PROFILE, ESSENTIAL, BROKEN, False, title,
            f"the console and the nightly run read different resumes: console {a}; run {b}",
            unlocks,
            Fix("steps", "make preferences.yaml resume_path and paths.resume_path name the "
                         "same file", ("point resume_path in preferences.yaml at the same "
                         "file as paths.resume_path, or remove it from the YAML")),
            source,
        )  # fmt: skip

    err = console_err or state.resume_error
    if console_file is None:
        message = console_err or ""
        if "has no .md file" in message or "not found" in message or "no resume" in message:
            return Check(
                "profile.resume", PROFILE, ESSENTIAL, MISSING, False, title,
                f"no readable .md resume at {_show(state.resume_target or configured, ctx)}",
                unlocks, fix, source,
            )  # fmt: skip
        if "exactly one" in message:
            return Check(
                "profile.resume", PROFILE, ESSENTIAL, BROKEN, False, title,
                "the resume folder holds more than one .md file; keep exactly one", unlocks,
                fix, source,
            )  # fmt: skip
        return Check("profile.resume", PROFILE, ESSENTIAL, BROKEN, False, title,
                     "the resume cannot be read", unlocks, fix, source)  # fmt: skip
    if state.kind == "resume" and err:
        if "empty" in err:
            detail = "the resume file is empty"
        elif "cannot read" in err:
            detail = "the resume file cannot be read"
        else:
            detail = "the resume cannot be loaded"
        return Check("profile.resume", PROFILE, ESSENTIAL, BROKEN, False, title, detail,
                     unlocks, fix, source)  # fmt: skip
    return Check("profile.resume", PROFILE, ESSENTIAL, OK, False, title,
                 f"read from {_show(console_file, ctx)}", unlocks, None, source)  # fmt: skip


def _completeness(ctx: _Ctx) -> Check:
    state = ctx.prefs
    title = "Profile completeness"
    unlocks = "USAJOBS queries, salary comparison and recency weighting"
    if state.kind in ("missing", "broken"):
        return Check(
            "profile.completeness", PROFILE, RECOMMENDED, UNKNOWN, False, title,
            "waiting on a valid preferences file", unlocks, Fix("route", "/prefs"),
        )  # fmt: skip
    profile = state.profile
    gaps: list[str] = []
    if not profile.target_titles and not profile.queries:
        gaps.append("no target_titles or queries (USAJOBS fetches nothing)")
    floor = profile.hard.salary_floor
    if floor is None or floor.amount is None:
        gaps.append("no salary_floor (comp is not scored)")
    focus = profile.current_focus
    if focus is None or not (focus.doing or focus.done_with):
        gaps.append("no current_focus or done_with (little recency weighting)")
    if gaps:
        return Check(
            "profile.completeness", PROFILE, RECOMMENDED, MISSING, False, title,
            "; ".join(gaps), unlocks, Fix("route", "/prefs"), "profile/preferences.yaml",
        )  # fmt: skip
    return Check("profile.completeness", PROFILE, RECOMMENDED, OK, False, title,
                 "titles, salary floor and focus are set", unlocks, None,
                 "profile/preferences.yaml")  # fmt: skip


def _screen_scorer(ctx: _Ctx) -> Check:
    return _scorer_row(
        ctx, check_id="scorer.screen", title="Screen scorer",
        spec=ctx.settings.scoring.screen_scorer, field_name="screen_scorer", level=ESSENTIAL,
        unlocks="Fit scores and buckets for new jobs",
    )  # fmt: skip


def _models_choices(ctx: _Ctx) -> Check:
    """The 019 inventory row "Model choices": models.toml unreadable, a key it holds that is
    invalid (a spending use blocked, a drafting key fallen back), or a key this version does
    not know. Messages come from ``load_layers`` and never carry a value other than a scorer
    spec or model id (019 B)."""
    title, unlocks = "Model choices", "Choosing models on /models and `jobhunter models`"
    layers = ctx.settings.models_layers
    if layers is None or not layers.exists:
        return Check("models.choices", SCORING, RECOMMENDED, OK, False, title,
                     "no models.toml; config.toml and the defaults decide", unlocks, None,
                     "default", reads_at_start=True)  # fmt: skip
    fix = Fix("command", "jobhunter models show, then jobhunter models reset or set")
    if layers.broken or layers.blocked or layers.notices or layers.invalid:
        lines = [p.message for p in layers.problems if p.kind != "unknown"]
        lines += list(layers.blocked.values()) + layers.notices
        detail = "; ".join(dict.fromkeys(lines)) or "models.toml has invalid settings"
        return Check("models.choices", SCORING, RECOMMENDED, BROKEN, False, title, detail,
                     unlocks, fix, "models.toml", reads_at_start=True)  # fmt: skip
    unknown = [p.message for p in layers.problems if p.kind == "unknown"]
    if unknown:
        return Check("models.choices", SCORING, RECOMMENDED, OK, False, title,
                     "; ".join(unknown) + " (ignored)", unlocks, None, "models.toml",
                     reads_at_start=True)  # fmt: skip
    count = sum(len(v) for v in layers.layer.values())
    return Check("models.choices", SCORING, RECOMMENDED, OK, False, title,
                 f"models.toml sets {count} setting{'s' if count != 1 else ''}", unlocks, None,
                 "models.toml", reads_at_start=True)  # fmt: skip


def _caps(ctx: _Ctx) -> Check:
    scoring = ctx.settings.scoring
    title, unlocks = "Spend caps", "Scoring (the caps bound what scoring may spend)"
    if scoring.daily_cap_usd <= 0 or scoring.weekly_cap_usd <= 0:
        which = "daily_cap_usd" if scoring.daily_cap_usd <= 0 else "weekly_cap_usd"
        return Check(
            "scorer.caps", SCORING, ESSENTIAL, BROKEN, False, title,
            f"scoring.{which} is 0, so nothing is scored", unlocks,
            Fix("command", f"set [scoring] {which} in config.toml"), "config.toml",
            reads_at_start=True,
        )  # fmt: skip
    detail = f"${scoring.daily_cap_usd:.2f}/day, ${scoring.weekly_cap_usd:.2f}/week"
    day = ctx.now.astimezone(UTC).date().isoformat()
    from jobhunter.core.spend import SCORING_ONLY

    rows = _query(
        ctx, f"SELECT coalesce(sum(cost_usd), 0) FROM llm_spend WHERE day = ? AND {SCORING_ONLY}",
        (day,),
    )  # fmt: skip
    if rows:
        detail += f"; ${float(rows[0][0]):.2f} spent today"
    return Check("scorer.caps", SCORING, ESSENTIAL, OK, False, title, detail, unlocks, None,
                 "config.toml", reads_at_start=True)  # fmt: skip


def _deep_scorer(ctx: _Ctx) -> Check:
    anthropic_screen = _provider(ctx.settings.scoring.screen_scorer) == "anthropic"
    check = _scorer_row(
        ctx, check_id="scorer.deep", title="Deep pass",
        spec=ctx.settings.scoring.deep_scorer, field_name="deep_scorer",
        level=RECOMMENDED if anthropic_screen else OPTIONAL,
        unlocks="Stage 3 shortlist (`score --deep`)",
    )  # fmt: skip
    if check.status == MISSING and not anthropic_screen:
        return replace(check, status=OPTIONAL_STATUS)
    return check


def _usajobs(ctx: _Ctx) -> Check:
    title = "USAJOBS key"
    unlocks = "Federal jobs, the best source (011)"
    key_env, email_env = usajobs_names(ctx.registry_rows)
    wanted = [
        (key_env, "jobhunter_usajobs_key"),
        (email_env, "jobhunter_usajobs_email"),
    ]
    results: list[tuple[str, _Presence, str]] = []
    for env_name, secret in wanted:
        if not env_name:
            continue
        results.append((env_name, _presence(ctx, env_name, secret, console_sees=False), secret))
    kinds = {p.kind for _, p, _ in results}
    detail = "; ".join(p.detail for _, p, _ in results)
    sources = sorted({p.source for _, p, _ in results if p.source})
    source = ", ".join(sources)
    if "invalid" in kinds:
        bad = next(s for _, p, s in results if p.kind == "invalid")
        return Check("source.usajobs", SOURCES, RECOMMENDED, BROKEN, False, title, detail,
                     unlocks, _secret_fix(ctx, key_env or "USAJOBS_API_KEY", bad, replace=True),
                     source, reads_at_start=True)  # fmt: skip
    if kinds == {"present"}:
        return Check("source.usajobs", SOURCES, RECOMMENDED, OK, False, title, detail, unlocks,
                     None, source, reads_at_start=True)  # fmt: skip
    if "unseen" in kinds and "absent" not in kinds:
        return Check(
            "source.usajobs", SOURCES, RECOMMENDED, UNKNOWN, False, title,
            "the console container never sees USAJOBS credentials; the nightly run does",
            unlocks, Fix("command", "jobhunter-ctr doctor"), "",
        )  # fmt: skip
    absent = next((s for _, p, s in results if p.kind == "absent"), "jobhunter_usajobs_key")
    names = " and ".join(n for n, p, _ in results if p.kind == "absent")
    return Check(
        "source.usajobs", SOURCES, RECOMMENDED, MISSING, False, title,
        f"not configured: {names} unset", unlocks,
        _secret_fix(ctx, key_env or "USAJOBS_API_KEY", absent), source, reads_at_start=True,
    )  # fmt: skip


def _schedule(ctx: _Ctx) -> Check:
    from jobhunter.ops import preflight, schedule

    title, unlocks = "Scheduled runs", "Nightly ingest and scoring"
    if ctx.mode == CONTAINER:
        return Check(
            "run.schedule", SOURCES, RECOMMENDED, UNKNOWN, False, title,
            "the Quadlet timers are not visible from inside the container", unlocks,
            Fix("command", "jobhunter doctor --containers"),
        )  # fmt: skip
    user_dir = schedule.unit_dir(ctx.home)
    host_enabled = preflight.host_timers_enabled(user_dir, schedule.TIMERS)
    if ctx.mode == HOST_MANAGING:
        if host_enabled:
            return Check(
                "run.schedule", SOURCES, RECOMMENDED, BROKEN, False, title,
                "host timers are still enabled beside the container timers: "
                + ", ".join(host_enabled),
                unlocks, Fix("command", "jobhunter schedule uninstall"),
            )  # fmt: skip
        ctr = preflight.host_timers_enabled(user_dir, preflight.CTR_TIMERS)
        if ctr:
            return Check("run.schedule", SOURCES, RECOMMENDED, OK, False, title,
                         "container timers enabled: " + ", ".join(ctr), unlocks,
                         None, "systemd user timers")  # fmt: skip
        return Check(
            "run.schedule", SOURCES, RECOMMENDED, MISSING, False, title,
            "no jobhunter-ctr timers are enabled", unlocks,
            Fix("command", "jobhunter container preflight"),
        )  # fmt: skip
    if host_enabled:
        return Check("run.schedule", SOURCES, RECOMMENDED, OK, False, title,
                     "host timers enabled: " + ", ".join(host_enabled), unlocks, None,
                     "systemd user timers")  # fmt: skip
    return Check("run.schedule", SOURCES, RECOMMENDED, MISSING, False, title,
                 "no timers are enabled, so nothing runs on its own", unlocks,
                 Fix("command", "jobhunter schedule install"))  # fmt: skip


def map_error_text(text: str | None) -> str | None:
    """Fixed wording for a known failure (019 rule 4); ``None`` when unknown. Matches on the
    exception type name or fixed phrases only, and never echoes the text."""
    if not text:
        return None
    head = text.split(":", 1)[0].strip()
    lowered = text.lower()
    if "RefreshError" in text or "invalid_grant" in lowered or head == "GmailTokenExpired":
        return "Gmail token expired or revoked"
    if head == "UsajobsAuthMissing":
        return "USAJOBS credentials missing"
    if "no gmail token" in lowered:
        return "no Gmail token"
    if "client secrets file missing" in lowered:
        return "Google client secrets file missing"
    return None


def _exception_type(text: str | None) -> str:
    head = (text or "").split(":", 1)[0].strip()
    ok = head.isidentifier() and head[:1].isupper() and len(head) <= 60
    return head if ok else "an error"


def _last_run(ctx: _Ctx) -> Check:
    title, unlocks = "Last run", "Whether ingest and scoring are working"
    rows = _query(
        ctx, "SELECT id, started_at, ended_at, exit_status FROM run ORDER BY id DESC LIMIT 1"
    )  # fmt: skip
    if rows is None and ctx.conn is not None:
        return Check("run.last", SOURCES, RECOMMENDED, UNKNOWN, False, title,
                     "the database could not be read", unlocks,
                     Fix("command", "jobhunter doctor"))  # fmt: skip
    if not rows:
        return Check("run.last", SOURCES, RECOMMENDED, OK, False, title, "no run yet", unlocks,
                     Fix("command", "jobhunter run"))  # fmt: skip
    run = rows[0]
    started = _parse_ts(run["started_at"])
    when = started.astimezone().strftime("%Y-%m-%d %H:%M") if started else "unknown time"
    if run["ended_at"] is None:
        if started is not None and ctx.now - started > UNFINISHED_RUN_LIMIT:
            return Check("run.last", SOURCES, RECOMMENDED, BROKEN, False, title,
                         f"the run at {when} never finished (it crashed or was killed); see "
                         "the journal", unlocks, Fix("command", "jobhunter run"))  # fmt: skip
        return Check("run.last", SOURCES, RECOMMENDED, OK, False, title,
                     f"run at {when} has not finished", unlocks)  # fmt: skip
    if run["exit_status"] == "ok":
        return Check("run.last", SOURCES, RECOMMENDED, OK, False, title,
                     f"run at {when} finished ok", unlocks)  # fmt: skip
    bad = _query(
        ctx, "SELECT source_key, error FROM run_source WHERE run_id = ? AND status != 'ok'",
        (run["id"],),
    )  # fmt: skip
    reasons: list[str] = []
    for row in bad or []:
        mapped = map_error_text(row["error"])
        reasons.append(mapped or f"{_exception_type(row['error'])} (see /sources)")
    shown = "; ".join(dict.fromkeys(reasons)) or "a source failed (see /sources)"
    return Check("run.last", SOURCES, RECOMMENDED, BROKEN, False, title,
                 f"run at {when} exited 1: {shown}", unlocks, Fix("route", "/sources"))  # fmt: skip


# -- email alerts -------------------------------------------------------------------------------


def _mail_source(ctx: _Ctx) -> sqlite3.Row | None:
    rows = _query(
        ctx,
        "SELECT s.status AS status, s.status_note AS note, ss.last_ok_at AS last_ok_at "
        "FROM source s LEFT JOIN source_state ss ON ss.source_key = s.key WHERE s.key = ?",
        (MAIL_SOURCE_KEY,),
    )
    return rows[0] if rows else None


def _client_json(ctx: _Ctx) -> Check:
    from jobhunter.mail import auth

    title, unlocks = "Google client JSON", "Email alert ingest and `mail auth`"
    path = auth.client_secrets_path(ctx.settings)
    secret = "jobhunter_google_client"
    if ctx.mode == HOST_MANAGING:
        present = _dropin_present(ctx, secret)
        where = f"podman secret {secret}"
    else:
        try:
            present = path.is_file()
        except OSError:
            present = False
        where = _show(path, ctx)
    fix = Fix(
        "steps",
        f"create a Desktop OAuth client in Google Cloud and save its JSON as {where}",
        ("follow the Cloud console steps shown on /setup",
         f"podman secret create {secret} <file>" if ctx.mode != HOST else f"save it as {where}"),
    )  # fmt: skip
    if present:
        return Check("mail.client", EMAIL, RECOMMENDED, OK, False, title, f"found ({where})",
                     unlocks, None, "file" if ctx.mode == HOST else "podman secret",
                     step=1)  # fmt: skip
    if ctx.mode == CONTAINER:
        return Check("mail.client", EMAIL, RECOMMENDED, UNKNOWN, False, title,
                     "the console container never sees this file; the nightly run does",
                     unlocks, Fix("command", "jobhunter-ctr doctor"), step=1)  # fmt: skip
    return Check("mail.client", EMAIL, RECOMMENDED, MISSING, False, title,
                 f"not found at {where}", unlocks, fix, step=1)  # fmt: skip


def _alerts_address(ctx: _Ctx) -> Check:
    value = ctx.settings.mail.alerts_address
    title, unlocks = "Alerts address", "The Gmail filter and the address you sign up boards with"
    if _is_placeholder(value):
        return Check(
            "mail.address", EMAIL, RECOMMENDED, BROKEN, False, title,
            "still the placeholder; `mail setup` refuses it", unlocks,
            Fix("steps", "add [mail] alerts_address to config.toml, then restart",
                ('add to config.toml:  [mail]  alerts_address = "<you>+jobs@example.com"',
                 f"restart the console ({RESTART_COMMAND[ctx.mode]})")),
            "default", reads_at_start=True, step=2,
        )  # fmt: skip
    return Check("mail.address", EMAIL, RECOMMENDED, OK, False, title, "set in config.toml",
                 unlocks, None, "config.toml", reads_at_start=True, step=2)  # fmt: skip


def _read_json(path: Path) -> dict | None:
    try:
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _gmail_token(ctx: _Ctx) -> Check:
    """The Gmail token row (019 "Gmail token"). The token is never read and the keyring is never
    opened: the row comes from ``gmail-auth.json``, a ``stat`` of the token file, and the mail
    source's latest status."""
    from jobhunter.mail import auth
    from jobhunter.ops import preflight

    title, unlocks = "Gmail token", "Email alert ingest"
    fix = Fix(
        "command",
        "jobhunter mail auth"
        if ctx.mode == HOST
        else "jobhunter mail auth --podman-secret jobhunter_gmail_token",
    )
    meta = _read_json(ctx.data_dir / GMAIL_AUTH_FILE)
    issued = _parse_ts(meta.get("issued_at")) if meta else None
    storage = meta.get("storage") if meta and isinstance(meta.get("storage"), str) else ""
    src = _mail_source(ctx)
    note = src["note"] if src else None
    mapped = map_error_text(note) if src and src["status"] != "ok" else None
    last_ok = _parse_ts(src["last_ok_at"]) if src else None

    def row(status: str, detail: str, *, due: bool = False, f: Fix | None = fix) -> Check:
        return Check("mail.token", EMAIL, RECOMMENDED, status, due, title, detail, unlocks,
                     f if status != OK or due else None, storage or "", step=3)  # fmt: skip

    if mapped == "Gmail token expired or revoked":
        return row(BROKEN, mapped)

    if ctx.mode == HOST_MANAGING:
        has_token = _dropin_present(ctx, preflight.GMAIL_TOKEN_SECRET)
        unseen = False
    elif ctx.mode == CONTAINER:
        has_token = bool(ctx.environ.get(auth.TOKEN_FILE_VAR)) and auth.token_file_path().is_file()
        unseen = not has_token
    else:
        try:
            file_present = auth.token_file_path().is_file()
        except OSError:
            file_present = False
        has_token = file_present or storage == "keyring"
        unseen = False
    if not has_token:
        if mapped == "no Gmail token":
            return row(MISSING, "no Gmail token")
        if unseen:
            return row(UNKNOWN, "the console container never sees the token", f=None)
        if ctx.mode == HOST and meta is None:
            if src and src["status"] == "ok" and last_ok and ctx.now - last_ok <= RECENT_OK:
                return row(OK, "worked at the last run (stored in the keyring)")
            return row(UNKNOWN, "use Test (free), or run `jobhunter mail status`", f=None)
        return row(MISSING, "no Gmail token")

    if issued is None:
        if src and src["status"] == "ok" and last_ok and ctx.now - last_ok <= RECENT_OK:
            return row(OK, "worked at the last run (no issue date recorded)")
        return row(UNKNOWN, "use Test (free), or run `jobhunter mail status`", f=None)

    expires = issued + timedelta(days=GMAIL_TESTING_DAYS)
    if last_ok and last_ok > expires:
        return row(OK, "working past 7 days, so the app has left Testing")
    age_days = (ctx.now - issued).days  # 6 here means due tomorrow: expiry is later
    if ctx.now >= expires:
        return row(BROKEN, "token older than 7 days; the app is in Testing, re-authorize")
    if age_days >= GMAIL_TESTING_DAYS - 1:
        return row(OK, "re-auth due tomorrow", due=True)
    return row(OK, f"re-auth due about {expires.astimezone().date().isoformat()}")


def _mail_label(ctx: _Ctx) -> Check:
    title, unlocks = "Gmail label and filter", "Alerts leave your inbox and land under one label"
    fix = Fix("command", "jobhunter mail setup" if ctx.mode == HOST else "jobhunter-ctr mail setup")
    cutoff = (ctx.now - LABEL_WINDOW).astimezone(UTC).isoformat()
    rows = _query(ctx, "SELECT count(*) FROM mail_message WHERE processed_at >= ?", (cutoff,))
    if rows and rows[0][0] > 0:
        return Check("mail.label", EMAIL, RECOMMENDED, OK, False, title,
                     f"{rows[0][0]} alert(s) arrived under the label in the last 14 days",
                     unlocks, None, "mail_message", step=4)  # fmt: skip
    return Check("mail.label", EMAIL, RECOMMENDED, UNKNOWN, False, title,
                 "no alert has arrived in the last 14 days; use Test (free) or `jobhunter mail "
                 "status`", unlocks, fix, step=4)  # fmt: skip


def _subscriptions(ctx: _Ctx) -> Check:
    title = "Board alert subscriptions"
    unlocks = "Coverage of the boards that block crawlers (008)"
    fix = Fix("route", "/alerts")
    rows = _query(ctx, "SELECT source_key FROM alert_signup WHERE subscribed_at IS NOT NULL")
    if rows is None:
        return Check("mail.subscriptions", EMAIL, RECOMMENDED, UNKNOWN, False, title,
                     "no database to read", unlocks, fix, step=5)  # fmt: skip
    subscribed = {r["source_key"] for r in rows}
    if not subscribed:
        return Check("mail.subscriptions", EMAIL, RECOMMENDED, MISSING, False, title,
                     "no board alert subscriptions marked yet", unlocks, fix, step=5)  # fmt: skip
    cutoff = (ctx.now - ARRIVAL_WINDOW).astimezone(UTC).isoformat()
    got = _query(
        ctx,
        "SELECT DISTINCT origin_source_key FROM mail_message "
        "WHERE origin_source_key IS NOT NULL AND processed_at >= ?",
        (cutoff,),
    )  # fmt: skip
    receiving = {r[0] for r in got or []} & subscribed
    stale = len(subscribed) - len(receiving)
    return Check(
        "mail.subscriptions", EMAIL, RECOMMENDED, OK, False, title,
        f"{len(subscribed)} subscribed, {len(receiving)} receiving, {stale} stale", unlocks,
        fix if stale else None, "alert_signup", step=5,
    )  # fmt: skip


def _parsers(ctx: _Ctx) -> Check:
    return Check(
        "mail.parsers", EMAIL, INFO, OK, False, "Alert parsers",
        "vos, joblink, neogov, nlx and generic are read; LinkedIn (430a0e7) and Craigslist "
        "(b0ac328) alerts have no parser yet",
        "Which alert emails turn into jobs", Fix("route", "/alerts"),
    )  # fmt: skip


# -- apply and optional ---------------------------------------------------------------------------


def _runner(ctx: _Ctx) -> Check:
    from jobhunter.apply import cli_runner, runner_state

    title, unlocks = "Drafting runner", "Generate on a packet and the entailment check"
    apply_cfg = ctx.settings.apply
    if ctx.mode == CONTAINER:
        return Check(
            "apply.runner", APPLY, OPTIONAL, OPTIONAL_STATUS, False, title,
            "drafting runs on the host (`apply draft`), not in the container", unlocks,
            None, "container",
        )  # fmt: skip
    if apply_cfg.runner == "api":
        # drafting runs on the host, so the host environment decides, not the podman drop-ins
        cred = scorers.credential_for(
            "anthropic:" + apply_cfg.api_model, ctx.settings.scoring, environ=ctx.environ,
            home=ctx.home,
        )  # fmt: skip
        if cred.status == scorers.PRESENT:
            return Check("apply.runner", APPLY, RECOMMENDED, OK, False, title,
                         "runner = api; an Anthropic credential is set", unlocks, None,
                         "config.toml")  # fmt: skip
        return Check(
            "apply.runner", APPLY, RECOMMENDED, MISSING, False, title,
            "runner = api and no Anthropic credential is set", unlocks,
            _secret_fix(ctx, "ANTHROPIC_API_KEY", "jobhunter_anthropic"), "config.toml",
        )  # fmt: skip
    state = runner_state.load(ctx.data_dir)
    if cli_runner.find_binary() is None:
        return Check(
            "apply.runner", APPLY, RECOMMENDED, MISSING, False, title,
            "runner = cli and `claude` is not on PATH", unlocks,
            Fix("command", "install Claude Code, run `claude` and log in with claude.ai"),
            "config.toml",
        )  # fmt: skip
    if state.off:
        return Check(
            "apply.runner", APPLY, RECOMMENDED, BROKEN, False, title,
            "the CLI runner is turned off; turn it back on from a packet page", unlocks,
            Fix("route", "/prefs"), "apply-runner.json",
        )  # fmt: skip
    return Check("apply.runner", APPLY, RECOMMENDED, OK, False, title,
                 "runner = cli; `claude` found (login is a live check)", unlocks, None,
                 "config.toml")  # fmt: skip


def _packet_cap(ctx: _Ctx) -> Check:
    cap = ctx.settings.apply.daily_cap_usd
    detail = f"${cap:.2f}/day" if cap > 0 else "0: paid packet drafting is refused, by design"
    return Check("apply.cap", APPLY, INFO, OK, False, "Packet cap", detail,
                 "Paid packet drafting", None, "config.toml", reads_at_start=True)  # fmt: skip


def _extension(ctx: _Ctx) -> Check:
    from jobhunter.apply import ext_pairing

    title, unlocks = "Extension pairing", "Send to jobhunter from Chrome"
    paired = ext_pairing.status(ext_pairing.state_path(ctx.data_dir), ctx.now)["paired"]
    if paired:
        return Check("ext.pairing", APPLY, OPTIONAL, OK, False, title, "paired", unlocks)
    return Check("ext.pairing", APPLY, OPTIONAL, OPTIONAL_STATUS, False, title,
                 "not paired", unlocks, Fix("route", "/prefs"))  # fmt: skip


def _pdf(ctx: _Ctx) -> Check:
    title, unlocks = "PDF export", "Packet PDFs"
    fix = Fix("command", "uv sync --extra browser && uv run playwright install chromium")
    try:
        installed = importlib_util.find_spec("playwright") is not None
    except (ImportError, ValueError):
        installed = False
    if not installed:
        return Check("pdf.export", APPLY, OPTIONAL, OPTIONAL_STATUS, False, title,
                     "the browser extra is not installed", unlocks, fix)  # fmt: skip
    cache = ctx.environ.get("PLAYWRIGHT_BROWSERS_PATH") or str(ctx.home / ".cache/ms-playwright")
    try:
        have_browser = any(Path(cache).glob("chromium*"))
    except OSError:
        have_browser = False
    if not have_browser:
        return Check("pdf.export", APPLY, OPTIONAL, OPTIONAL_STATUS, False, title,
                     "Chromium is not installed", unlocks, fix)  # fmt: skip
    return Check("pdf.export", APPLY, OPTIONAL, OK, False, title,
                 "the browser extra and Chromium are installed", unlocks)  # fmt: skip


# -- running --------------------------------------------------------------------------------------


def _secrets_valid(ctx: _Ctx) -> Check:
    title, unlocks = "Secrets and .env files", "Whichever feature uses the key"
    if ctx.report is None:
        return Check("secrets.valid", RUNNING, RECOMMENDED, UNKNOWN, False, title,
                     "secrets were not resolved in this process", unlocks)  # fmt: skip
    invalid = [s for s in ctx.report.states if s.state == secrets.INVALID]
    problems = [f"{s.name} {s.describe()}" for s in invalid]
    loose: list[Path] = []
    for path in ctx.report.env_files:
        try:
            mode = stat_module.S_IMODE(path.stat().st_mode)
        except OSError:
            continue
        if mode & 0o077:
            loose.append(path)
            problems.append(f"{_show(path, ctx)} is readable by group or others ({mode:04o})")
    if problems:
        if loose:
            fix = Fix("command", f"chmod 600 {_show(loose[0], ctx)}")
        else:
            names = ", ".join(s.name for s in invalid)
            fix = Fix("command", f"correct the value or the *_FILE path of: {names}")
        return Check(
            "secrets.valid", RUNNING, RECOMMENDED, BROKEN, False, title,
            "; ".join(problems), unlocks, fix, reads_at_start=True,
        )  # fmt: skip
    return Check("secrets.valid", RUNNING, RECOMMENDED, OK, False, title,
                 "every secret is well-formed and .env files are private", unlocks,
                 reads_at_start=True)  # fmt: skip


def _backups(ctx: _Ctx) -> Check:
    from jobhunter.core import backup

    title, unlocks = "Backups", "Recovery"
    fix = Fix("command", "jobhunter db backup --auto")
    found = backup.auto_backups(backup.auto_dir(ctx.data_dir))
    if not found:
        detail = "no automatic backups" + (
            " (host mode keeps only pre-migrate backups)" if ctx.mode == HOST else ""
        )
        return Check("backups.auto", RUNNING, RECOMMENDED, MISSING, False, title, detail,
                     unlocks, fix)  # fmt: skip
    newest = found[0][0]
    age = ctx.now - newest
    if age > NEWEST_BACKUP_MAX_AGE:
        return Check("backups.auto", RUNNING, RECOMMENDED, BROKEN, False, title,
                     f"the newest automatic backup is {age.days} days old",
                     unlocks, fix)  # fmt: skip
    return Check("backups.auto", RUNNING, RECOMMENDED, OK, False, title,
                 f"newest automatic backup {newest.astimezone().strftime('%Y-%m-%d %H:%M')}",
                 unlocks)  # fmt: skip


def _exposure(ctx: _Ctx) -> Check:
    host = ctx.settings.console.host
    title, unlocks = "Console exposure", "Who can reach the console"
    if host in ("127.0.0.1", "localhost", "::1"):
        detail = "listens on loopback only"
    elif container.published_loopback_only():
        detail = "listens inside the container; published on host loopback only"
    else:
        detail = (
            "not loopback: the console has no authentication, so anyone who can reach this "
            "port can use jobhunter"
        )
    return Check("console.exposure", RUNNING, INFO, OK, False, title, detail, unlocks, None,
                 "config.toml", reads_at_start=True)  # fmt: skip


def _containers(ctx: _Ctx) -> Check:
    from jobhunter.core import dbpaths

    title, unlocks = "Container deployment", "The Quadlet units"
    if ctx.mode == HOST:
        return Check("containers.deployment", RUNNING, OPTIONAL, OPTIONAL_STATUS, False, title,
                     "not using containers", unlocks)  # fmt: skip
    fix = Fix("command", "jobhunter doctor --containers")
    if ctx.mode == CONTAINER:
        return Check("containers.deployment", RUNNING, INFO, UNKNOWN, False, title,
                     "run `jobhunter doctor --containers` on the host", unlocks, fix)  # fmt: skip
    count, error = dbpaths.absolute_path_count(ctx.db_path, ctx.data_dir)
    if error or (count or 0) > 0:
        detail = (
            "the database could not be checked for absolute paths"
            if error
            else f"{count} stored path(s) are absolute, which a container cannot resolve"
        )
        return Check("containers.deployment", RUNNING, ESSENTIAL, BROKEN, False, title, detail,
                     unlocks, Fix("command", RELATIVIZE_COMMAND))  # fmt: skip
    return Check("containers.deployment", RUNNING, ESSENTIAL, UNKNOWN, False, title,
                 "stored paths are relative; run `jobhunter doctor --containers` for the rest",
                 unlocks, fix)  # fmt: skip


# --- entry point -------------------------------------------------------------------------------

_CHECKS: tuple[tuple[Callable[[_Ctx], Check], bool], ...] = (
    # (builder, cheap): the banner's cheap_only run skips the rest
    (_config_file, True),
    (_db_schema, True),
    (_contact, True),
    (_permissions, True),
    (_preferences, True),
    (_resume, True),
    (_completeness, True),
    (_screen_scorer, True),
    (_caps, True),
    (_deep_scorer, True),
    (_models_choices, True),
    (_usajobs, True),
    (_schedule, True),
    (_last_run, True),
    (_client_json, True),
    (_alerts_address, True),
    (_gmail_token, True),
    (_mail_label, True),
    (_subscriptions, False),
    (_parsers, True),
    (_runner, True),
    (_packet_cap, True),
    (_extension, True),
    (_pdf, True),
    (_secrets_valid, True),
    (_backups, False),
    (_exposure, True),
    (_containers, True),
)


def run_checks(
    settings: Settings,
    *,
    secrets_report: secrets.Report | None,
    now: datetime,
    mode: str,
    cheap_only: bool = False,
    registry_rows: Iterable[Any] | None = None,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> list[Check]:
    """Every local check, in display order. Read-only, no network, no subprocess.

    ``registry_rows`` are the registry rows loaded once at console start: the startup
    ``secrets.Report`` cannot tell which variables are USAJOBS's, and parsing the registry per
    call costs about 0.5 s. ``environ`` and ``home`` default to the process's; tests inject.
    """
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}")
    data_dir = resolve_path(settings.paths.data_dir)
    db_path = resolve_path(settings.paths.db_path)
    ctx = _Ctx(
        settings=settings,
        report=secrets_report,
        now=now,
        mode=mode,
        registry_rows=registry_rows,
        environ=os.environ if environ is None else environ,
        home=home if home is not None else Path.home(),
        data_dir=data_dir,
        db_path=db_path,
    )
    ctx.prefs = prefs_state(settings)
    ctx.conn = _open_ro(db_path)
    try:
        return [build(ctx) for build, cheap in _CHECKS if cheap or not cheap_only]
    finally:
        if ctx.conn is not None:
            ctx.conn.close()
