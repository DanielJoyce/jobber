"""``jobhunter container preflight`` (specs/018-containers.md, ticket C13, decision D9).

Runs on the host before switching to the Quadlet deployment, and again after any podman
secret is created, replaced or removed. It checks what the containers need (podman, the
bind-mount sources, SELinux labels, no host timers, data-dir-relative paths in the database,
host code and image at the same schema, private ``.env`` files, no leftover Gmail token file),
then writes one Quadlet drop-in per existing secret per unit following the least-privilege
matrix, removes the drop-ins it wrote for secrets that are gone, and writes the deployment
marker (``<data dir>/deployment.toml``) that switches off automatic migration (C12).

Drop-in layout (C4's base units plug into it)::

    <unit dir>/<unit>.container                  base unit, from deploy/quadlet/ (C4)
    <unit dir>/<unit>.container.d/50-<secret>.conf   one per secret, written here

``<unit dir>`` defaults to ``~/.config/containers/systemd``. A drop-in is ours when its first
line is :data:`HEADER`; nothing else in those directories is ever modified or deleted.

Only secret *names* are handled: ``podman secret ls`` names, podman secret names in
``Secret=``, file paths in ``Environment=``. No value is read, written or printed.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jobhunter.ops import podman

HEADER = "# written by jobhunter container preflight; removed by it when the secret is gone"
DROPIN_PREFIX = "50-"
DROPIN_SUFFIX = ".conf"
IMAGE = "localhost/jobhunter:current"
IMAGE_TIMEOUT_SECONDS = 120
SECRETS_DIR = "/run/secrets"
CONTAINER_UID = 1000
CONTAINER_FILE_T = "container_file_t"

CONSOLE = "jobhunter-console"
RUN = "jobhunter-ctr-run"
COLLECT = "jobhunter-ctr-collect"
RESCORE = "jobhunter-ctr-rescore"
VERIFY = "jobhunter-ctr-verify"
BACKUP = "jobhunter-ctr-backup"
# The one-off wrapper script (C15); not a Quadlet unit, so no drop-ins, but it has a column.
WRAPPER = "jobhunter-ctr"
UNITS = (CONSOLE, RUN, COLLECT, RESCORE, VERIFY, BACKUP)
SERVICES = tuple(f"{u}.service" for u in UNITS)
CTR_TIMERS = (
    "jobhunter-ctr-run.timer",
    "jobhunter-ctr-collect.timer",
    "jobhunter-ctr-verify.timer",
)

# Bind-mount sources the base units name, relative to %h (specs/018 Quadlet, D3).
DATA_SOURCE = Path(".local/share/jobhunter")
CACHE_SOURCE = Path(".cache/jobhunter")
CONFIG_SOURCE = Path(".config/jobhunter/config.toml")
# The container sees these (the units set the five JOBHUNTER_* path variables to them).
CONTAINER_PATHS = {
    "db_path": "jobhunter.db",
    "profile_dir": "profile",
    "resume_path": "resume",
}

ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SECRET_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")

OK = "ok"
WARN = "warn"
FAIL = "FAIL"

# --- the least-privilege matrix (specs/018 "Which container gets which secret") ------------

_SCORERS = frozenset({CONSOLE, RUN, RESCORE, WRAPPER})
MATRIX: dict[str, frozenset[str]] = {
    "jobhunter_anthropic": frozenset({CONSOLE, RUN, COLLECT, RESCORE, WRAPPER}),
    "jobhunter_openrouter": _SCORERS,
    "jobhunter_openai_compat": _SCORERS,
    "jobhunter_llama": _SCORERS,
    "jobhunter_usajobs_key": frozenset({RUN, VERIFY, WRAPPER}),
    "jobhunter_usajobs_email": frozenset({RUN, VERIFY, WRAPPER}),
    "jobhunter_google_client": frozenset({RUN, WRAPPER}),
    "jobhunter_gmail_token": frozenset({RUN, WRAPPER}),
}
GMAIL_TOKEN_SECRET = "jobhunter_gmail_token"


@dataclass(frozen=True)
class SecretSpec:
    """One podman secret: where it is mounted and which variable names the file."""

    secret: str
    target: str  # absolute path inside the container
    env: str | None  # None: the variable name is configured empty, so the secret is unused
    units: frozenset[str]
    note: str = ""  # why ``env`` is None or invalid

    @property
    def usable(self) -> bool:
        return self.env is not None and not self.note


def _api_env(name: str | None, what: str) -> tuple[str | None, str]:
    """``<name>_FILE`` for a configured key variable, or ``(None, why)``."""
    if not name:
        return None, f"{what} is empty in config, so this secret is not used"
    if not ENV_NAME_RE.match(name):
        return None, f"{what} {name!r} is not a valid environment variable name"
    return f"{name}_FILE", ""


def usajobs_auth(rows: Iterable[Any] | None = None) -> tuple[str | None, str | None]:
    """``(key_env, email_env)`` of the USAJOBS registry row (the packaged registry)."""
    if rows is None:
        try:
            from jobhunter.sources.registry import load_registry

            rows = load_registry()
        except Exception:
            rows = []
    for row in rows:
        key = getattr(row, "key", "") or ""
        cfg = getattr(row, "config", None) or {}
        auth = cfg.get("auth") if isinstance(cfg, Mapping) else None
        if "usajobs" in key and isinstance(auth, Mapping):
            return auth.get("key_env"), auth.get("email_env")
    return "USAJOBS_API_KEY", "USAJOBS_EMAIL"


def secret_matrix(settings: Any, registry_rows: Iterable[Any] | None = None) -> list[SecretSpec]:
    """Every secret jobhunter knows, with variable names taken from config (C13): each
    scorer's ``api_key_env`` and the registry's ``key_env``/``email_env``."""
    scoring = settings.scoring
    key_env, email_env = usajobs_auth(registry_rows)
    rows: list[tuple[str, str, str | None, str]] = [
        ("jobhunter_anthropic", "anthropic_api_key", "ANTHROPIC_API_KEY_FILE", ""),
        (
            "jobhunter_openrouter",
            "openrouter_api_key",
            *_api_env(scoring.openrouter.api_key_env, "scoring.openrouter.api_key_env"),
        ),
        (
            "jobhunter_openai_compat",
            "openai_compat_api_key",
            *_api_env(scoring.openai_compat.api_key_env, "scoring.openai_compat.api_key_env"),
        ),
        (
            "jobhunter_llama",
            "llama_api_key",
            *_api_env(scoring.local.api_key_env, "scoring.local.api_key_env"),
        ),
        ("jobhunter_usajobs_key", "usajobs_api_key", *_api_env(key_env, "registry key_env")),
        ("jobhunter_usajobs_email", "usajobs_email", *_api_env(email_env, "registry email_env")),
        (
            "jobhunter_google_client",
            "google_client_secret.json",
            "JOBHUNTER_GOOGLE_CLIENT_SECRETS",
            "",
        ),
        (GMAIL_TOKEN_SECRET, "gmail_token", "JOBHUNTER_GMAIL_TOKEN_FILE", ""),
    ]
    return [
        SecretSpec(secret, f"{SECRETS_DIR}/{target}", env, MATRIX[secret], note)
        for secret, target, env, note in rows
    ]


def dropin_text(spec: SecretSpec) -> str:
    return (
        f"{HEADER}\n"
        "[Container]\n"
        f"Secret={spec.secret},type=mount,target={spec.target},"
        f"uid={CONTAINER_UID},gid={CONTAINER_UID},mode=0400\n"
        f"Environment={spec.env}={spec.target}\n"
    )


def wrapper_specs(specs: Iterable[SecretSpec], existing: Iterable[str]) -> list[SecretSpec]:
    """The secrets the one-off wrapper ``jobhunter-ctr`` (C15) mounts: its matrix column,
    limited to secrets that exist."""
    have = set(existing)
    return [s for s in specs if WRAPPER in s.units and s.usable and s.secret in have]


# --- drop-in planning ----------------------------------------------------------------------


def default_unit_dir(home: Path | None = None) -> Path:
    return (home if home is not None else Path.home()) / ".config" / "containers" / "systemd"


def dropin_dir(unit_dir: Path, unit: str) -> Path:
    return unit_dir / f"{unit}.container.d"


def dropin_path(unit_dir: Path, unit: str, secret: str) -> Path:
    return dropin_dir(unit_dir, unit) / f"{DROPIN_PREFIX}{secret}{DROPIN_SUFFIX}"


def is_ours(path: Path) -> bool:
    """A regular file (not a symlink) whose first line is :data:`HEADER`."""
    try:
        if path.is_symlink() or not path.is_file():
            return False
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            return fh.readline().rstrip("\n") == HEADER
    except OSError:
        return False


@dataclass
class DropinPlan:
    write: dict[Path, str] = field(default_factory=dict)  # new or changed
    keep: list[Path] = field(default_factory=list)  # ours and already right
    remove: list[Path] = field(default_factory=list)  # ours, secret gone or no longer wanted
    conflict: list[Path] = field(default_factory=list)  # someone else's file at our name

    @property
    def changes(self) -> bool:
        return bool(self.write or self.remove)


def plan_dropins(
    unit_dir: Path, specs: Iterable[SecretSpec], existing: Iterable[str]
) -> DropinPlan:
    """What to write and remove so each unit names exactly the existing secrets it may use."""
    have = set(existing)
    wanted: dict[Path, str] = {}
    for spec in specs:
        if not spec.usable or spec.secret not in have:
            continue
        for unit in UNITS:
            if unit in spec.units:
                wanted[dropin_path(unit_dir, unit, spec.secret)] = dropin_text(spec)
    plan = DropinPlan()
    for path, text in sorted(wanted.items()):
        if path.is_symlink() or (path.exists() and not is_ours(path)):
            plan.conflict.append(path)
            continue
        try:
            current = path.read_text(encoding="utf-8") if path.exists() else None
        except OSError:
            current = None
        if current == text:
            plan.keep.append(path)
        else:
            plan.write[path] = text
    for unit in UNITS:
        directory = dropin_dir(unit_dir, unit)
        if not directory.is_dir():
            continue
        for path in sorted(directory.iterdir()):
            if path not in wanted and path.suffix == DROPIN_SUFFIX and is_ours(path):
                plan.remove.append(path)
    return plan


def our_dropins(unit_dir: Path) -> list[Path]:
    """Every drop-in preflight wrote, in any of the jobhunter unit directories."""
    found: list[Path] = []
    for unit in UNITS:
        directory = dropin_dir(unit_dir, unit)
        if directory.is_dir():
            found += [p for p in sorted(directory.iterdir()) if is_ours(p)]
    return found


def apply_dropins(plan: DropinPlan) -> None:
    for path, text in plan.write.items():
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    for path in plan.remove:
        if is_ours(path):  # checked again right before deleting
            path.unlink()


# --- individual checks ---------------------------------------------------------------------


@dataclass(frozen=True)
class Line:
    status: str  # OK, WARN or FAIL
    text: str

    def render(self) -> str:
        return f"{self.status:<4}  {self.text}"


def selinux_type(path: Path) -> str | None:
    """The SELinux type of ``path`` (not following symlinks), or None without labels."""
    try:
        raw = os.getxattr(path, "security.selinux", follow_symlinks=False)
    except (OSError, AttributeError):
        return None
    parts = raw.rstrip(b"\x00").decode("ascii", "replace").split(":")
    return parts[2] if len(parts) > 2 else None


def unlabelled_files(
    root: Path, label_of: Callable[[Path], str | None] = selinux_type, limit: int = 20
) -> tuple[bool, list[Path], int]:
    """``(checked, examples, total)``: files under ``root`` whose type is not
    container_file_t, once ``root`` itself carries that label. ``checked`` is False when the
    directory is not labelled yet (podman labels it at first start) or has no labels."""
    if label_of(root) != CONTAINER_FILE_T:
        return False, [], 0
    examples: list[Path] = []
    total = 0
    for dirpath, dirnames, filenames in os.walk(root):
        for name in [*dirnames, *filenames]:
            path = Path(dirpath) / name
            kind = label_of(path)
            if kind is not None and kind != CONTAINER_FILE_T:
                total += 1
                if len(examples) < limit:
                    examples.append(path)
    return True, examples, total


def host_timers_enabled(systemd_user_dir: Path, timers: Iterable[str]) -> list[str]:
    """Host timers (``jobhunter schedule install``) enabled: a ``*.wants/<timer>`` link."""
    enabled: list[str] = []
    if not systemd_user_dir.is_dir():
        return enabled
    for timer in timers:
        if any(
            (wants / timer).exists() or (wants / timer).is_symlink()
            for wants in systemd_user_dir.glob("*.wants")
        ):
            enabled.append(timer)
    return enabled


def image_version(prefix: list[str], image: str = IMAGE) -> tuple[dict[str, Any] | None, str]:
    """``jobhunter version --json`` from the image, or ``(None, why)``. Never pulls, never
    has network, never mounts anything: the image's own code schema and commit only."""
    argv = [*prefix, "run", "--rm", "--pull=never", "--network=none", image, "version", "--json"]
    try:
        done = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=IMAGE_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None, f"`podman run {image} version` timed out"
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"`podman run {image} version` failed ({type(exc).__name__})"
    if done.returncode != 0:
        tail = done.stderr.strip().splitlines()[-1:] or [""]
        return None, f"`podman run {image} version` exited {done.returncode}: {tail[0][:200]}"
    try:
        info = json.loads(done.stdout)
    except ValueError:
        return None, f"`podman run {image} version --json` did not print JSON"
    if not isinstance(info, dict) or not isinstance(info.get("code_schema"), int):
        return None, f"`podman run {image} version --json` has no code_schema"
    return info, ""


# --- the whole preflight -------------------------------------------------------------------


@dataclass
class Context:
    """Everything preflight looks at, resolved by the caller (the CLI) so tests can point it
    at a temporary home."""

    home: Path
    unit_dir: Path
    settings: Any
    data_dir: Path  # host's resolved data dir
    cache_dir: Path
    db_path: Path
    profile_dir: Path
    resume_path: Path
    config_file: Path  # the host's config file (JOBHUNTER_CONFIG or XDG)
    gmail_token_file: Path  # the host's local token file (~/.config/jobhunter/...)
    env_files: list[Path]
    host_version: Any  # jobhunter.ops.version.VersionInfo
    absolute_paths: int | None  # None: no database yet
    registry_rows: list[Any] | None = None
    systemd_user_dir: Path | None = None
    label_of: Callable[[Path], str | None] = selinux_type
    probe: Callable[[], tuple[list[str], list[str]] | None] = podman.probe
    read_image_version: Callable[[list[str]], tuple[dict[str, Any] | None, str]] = image_version


@dataclass
class Result:
    lines: list[Line] = field(default_factory=list)
    plan: DropinPlan | None = None
    existing: list[str] = field(default_factory=list)
    marker: Path | None = None
    marker_present: bool = False

    def add(self, status: str, text: str) -> None:
        self.lines.append(Line(status, text))

    @property
    def failed(self) -> bool:
        return any(line.status == FAIL for line in self.lines)


def _same(a: Path, b: Path) -> bool:
    return os.path.realpath(a) == os.path.realpath(b)


def _bind_sources(ctx: Context, res: Result, dry_run: bool) -> None:
    from jobhunter.xdg import mkdir_private

    data_src = ctx.home / DATA_SOURCE
    cache_src = ctx.home / CACHE_SOURCE
    config_src = ctx.home / CONFIG_SOURCE
    for what, src in (("data", data_src), ("cache", cache_src)):
        if src.is_dir():
            res.add(OK, f"{what} dir {src} exists")
        elif src.exists():
            res.add(FAIL, f"{what} dir {src} exists but is not a directory")
            continue
        elif dry_run:
            res.add(WARN, f"{what} dir {src} is missing; would create it (0700)")
            continue
        else:
            mkdir_private(src)
            res.add(OK, f"{what} dir {src} created (0700)")
        _owner(src, what + " dir", res)
    if config_src.is_file():
        res.add(OK, f"config file {config_src} exists")
        _owner(config_src, "config file", res)
    elif config_src.exists():
        res.add(FAIL, f"config file {config_src} exists but is not a regular file")
    elif dry_run:
        res.add(WARN, f"config file {config_src} is missing; would create it empty (0600)")
    else:
        mkdir_private(config_src.parent)
        os.close(os.open(config_src, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
        res.add(OK, f"config file {config_src} created empty (0600)")

    # The units mount fixed sources; the host must use the same files or the containers see
    # another database.
    if not _same(ctx.data_dir, data_src):
        res.add(
            FAIL,
            f"host data dir is {ctx.data_dir} but the units mount {data_src} at /data; "
            "move the data there (jobhunter migrate-paths) or drop the [paths] / "
            "JOBHUNTER_DATA_DIR / XDG_DATA_HOME override",
        )
    if not _same(ctx.db_path, ctx.data_dir / CONTAINER_PATHS["db_path"]):
        res.add(
            FAIL,
            f"host database is {ctx.db_path}; the containers use "
            f"<data dir>/{CONTAINER_PATHS['db_path']}. Drop the db_path override",
        )
    for key, value in (("profile_dir", ctx.profile_dir), ("resume_path", ctx.resume_path)):
        if not _same(value, ctx.data_dir / CONTAINER_PATHS[key]):
            res.add(
                WARN,
                f"host {key} is {value}; the containers use <data dir>/{CONTAINER_PATHS[key]}",
            )
    if not _same(ctx.cache_dir, cache_src):
        res.add(WARN, f"host cache dir is {ctx.cache_dir}; the units mount {cache_src}")
    if not _same(ctx.config_file, config_src):
        res.add(
            WARN,
            f"host config file is {ctx.config_file}; the units mount {config_src}, so the "
            "containers read that one",
        )


def _owner(path: Path, what: str, res: Result) -> None:
    try:
        uid = path.stat().st_uid
    except OSError:
        return
    if uid != os.getuid():
        res.add(
            FAIL,
            f"{what} {path} is owned by uid {uid}, not you (uid {os.getuid()}); "
            "the containers map only your uid",
        )


def _labels(ctx: Context, res: Result) -> None:
    for what, root in (("data", ctx.home / DATA_SOURCE), ("cache", ctx.home / CACHE_SOURCE)):
        if not root.is_dir():
            continue
        checked, examples, total = unlabelled_files(root, ctx.label_of)
        if not checked and ctx.label_of(root) is None:
            res.add(OK, f"SELinux: no label on {root} (SELinux off?); label check skipped")
        elif not checked:
            res.add(
                OK,
                f"SELinux: {what} dir not labelled {CONTAINER_FILE_T} yet "
                "(podman labels it at the first start, :z)",
            )
        elif not total:
            res.add(OK, f"SELinux: every file under {root} is {CONTAINER_FILE_T}")
        else:
            shown = ", ".join(str(p) for p in examples)
            more = f" and {total - len(examples)} more" if total > len(examples) else ""
            res.add(
                WARN,
                f"SELinux: {total} file(s) under {root} are not {CONTAINER_FILE_T} (moved in "
                f"with mv?): {shown}{more}. Fix with: chcon -R -t {CONTAINER_FILE_T} {root}",
            )


def _timers(ctx: Context, res: Result) -> None:
    from jobhunter.ops import schedule

    user_dir = ctx.systemd_user_dir or schedule.unit_dir(ctx.home)
    enabled = host_timers_enabled(user_dir, schedule.TIMERS)
    if enabled:
        res.add(
            FAIL,
            f"host timers still enabled ({', '.join(enabled)}); they would run beside the "
            "containers. Run: jobhunter schedule uninstall",
        )
    else:
        res.add(OK, "no host timers from `jobhunter schedule install` are enabled")


def _paths_in_db(ctx: Context, res: Result) -> None:
    if ctx.absolute_paths is None:
        res.add(OK, "no database yet, so no stored paths to check")
    elif ctx.absolute_paths:
        res.add(
            FAIL,
            f"{ctx.absolute_paths} absolute or escaping path(s) in the database; the "
            "containers cannot open them. Run: jobhunter db backup && "
            "jobhunter db relativize-paths --apply",
        )
    else:
        res.add(OK, "every path stored in the database is relative to the data dir")


def _versions(ctx: Context, prefix: list[str], res: Result) -> None:
    host = ctx.host_version
    info, why = ctx.read_image_version(prefix)
    if info is None:
        res.add(
            FAIL,
            f"cannot read the image's version: {why}. Build it: podman build --build-arg "
            "BUILD_COMMIT=$(git rev-parse HEAD) -t localhost/jobhunter:$(git rev-parse "
            "--short HEAD) . && podman tag localhost/jobhunter:$(git rev-parse --short HEAD) "
            f"{IMAGE}",
        )
        return
    image_schema = info["code_schema"]
    if image_schema != host.code_schema:
        res.add(
            FAIL,
            f"schema skew: host code has migration {host.code_schema}, image {IMAGE} has "
            f"{image_schema}. Rebuild the image from this checkout (or update the checkout) so "
            "they match",
        )
    else:
        res.add(OK, f"host code and image agree on schema {image_schema}")
    if host.db_state == "pending":
        res.add(
            FAIL,
            f"database {host.db_path} is at schema {host.db_schema}, behind the code "
            f"({host.code_schema}); nothing migrates on its own after the switch. "
            "Run: jobhunter db migrate",
        )
    elif host.db_state == "newer":
        res.add(FAIL, f"database {host.db_path} is newer than this code ({host.code_schema})")
    elif host.db_state in ("missing", "new"):
        res.add(WARN, f"database {host.db_path} has no schema yet; run on the host: jobhunter init")
    elif host.db_state == "unreadable":
        res.add(FAIL, f"database {host.db_path} could not be read")
    elif host.db_state == "current":
        res.add(OK, f"database at schema {host.db_schema}")
    image_commit = info.get("commit")
    if host.commit and image_commit and host.commit != image_commit:
        res.add(
            WARN,
            f"host checkout is at {host.commit[:12]}, image built from {str(image_commit)[:12]}; "
            "same schema, so it runs, but rebuild to pick up code changes",
        )
    elif not image_commit:
        res.add(WARN, "image does not record its build commit (built without BUILD_COMMIT)")


def _env_files(ctx: Context, res: Result) -> None:
    from jobhunter import secrets

    notes = secrets.env_file_warnings(ctx.env_files)
    for note in notes:
        res.add(WARN, note.removeprefix("warning: "))
    if ctx.env_files and not notes:
        res.add(OK, ".env files being read are private (0600)")


def _secrets(ctx: Context, specs: list[SecretSpec], existing: list[str], res: Result) -> None:
    have = set(existing)
    known = [s.secret for s in specs]
    present = [n for n in known if n in have]
    absent = [n for n in known if n not in have]
    res.add(OK, f"podman secrets found: {', '.join(present) or 'none'}")
    if absent:
        res.add(OK, f"not created (their drop-ins are skipped): {', '.join(absent)}")
    for spec in specs:
        if spec.secret in have and spec.note:
            res.add(WARN, f"{spec.secret}: {spec.note}; no drop-in written")
    if GMAIL_TOKEN_SECRET in have and (
        ctx.gmail_token_file.exists() or ctx.gmail_token_file.is_symlink()
    ):
        res.add(
            WARN,
            f"a host Gmail token file is still there ({ctx.gmail_token_file}) although the "
            f"podman secret {GMAIL_TOKEN_SECRET} exists. If the secret works, remove it: "
            f"rm {ctx.gmail_token_file}",
        )


def _base_units(ctx: Context, res: Result) -> None:
    missing = [u for u in UNITS if not (ctx.unit_dir / f"{u}.container").is_file()]
    if missing:
        res.add(
            WARN,
            f"base units not installed in {ctx.unit_dir}: {', '.join(missing)}. "
            f"Copy them: cp deploy/quadlet/*.container {ctx.unit_dir}/",
        )
    else:
        res.add(OK, f"base units found in {ctx.unit_dir}")


def run(
    ctx: Context,
    *,
    dry_run: bool = False,
    confirm_marker: Callable[[Path], bool] = lambda marker: False,
) -> Result:
    """Run every check, then (no FAIL, not ``dry_run``) write drop-ins and the marker.

    ``confirm_marker`` is asked before the deployment marker is first written; it returns
    True for ``--yes`` or an interactive yes.
    """
    from jobhunter.core import db

    res = Result()
    found = ctx.probe()
    if found is None:
        res.add(FAIL, f"podman is not reachable (tried {podman.TRIED})")
        prefix: list[str] | None = None
    else:
        prefix, res.existing = found
        res.add(OK, f"podman reachable ({' '.join(prefix)})")

    _bind_sources(ctx, res, dry_run)
    _labels(ctx, res)
    _timers(ctx, res)
    _paths_in_db(ctx, res)
    if prefix is not None:
        _versions(ctx, prefix, res)
    _env_files(ctx, res)
    specs = secret_matrix(ctx.settings, ctx.registry_rows)
    _base_units(ctx, res)
    if prefix is not None:
        _secrets(ctx, specs, res.existing, res)
        res.plan = plan_dropins(ctx.unit_dir, specs, res.existing)
        for path in res.plan.conflict:
            res.add(WARN, f"{path} exists and was not written by preflight; left alone")

    res.marker = ctx.data_dir / db.DEPLOYMENT_MARKER
    res.marker_present = db.deployment_marker(ctx.data_dir) is not None
    if res.failed or res.plan is None:
        return res

    verb = "would " if dry_run else ""
    for path in res.plan.write:
        res.add(OK, f"{verb}write {path}")
    for path in res.plan.remove:
        res.add(OK, f"{verb}remove {path} (its secret is gone or no longer used)")
    if not res.plan.changes:
        res.add(OK, f"drop-ins up to date ({len(res.plan.keep)} in place)")
    if not dry_run:
        apply_dropins(res.plan)

    if res.marker_present:
        res.add(OK, f"deployment marker {res.marker} present (no automatic migration)")
    elif dry_run:
        res.add(OK, f"would ask to write the deployment marker {res.marker}")
    elif confirm_marker(res.marker):
        write_marker(res.marker)
        res.marker_present = True
        res.add(OK, f"wrote deployment marker {res.marker}: no command migrates on its own now")
    else:
        res.add(
            WARN,
            f"deployment marker {res.marker} not written; rerun with --yes to switch. Until "
            "then host commands still migrate the database on their own",
        )
    return res


def write_marker(path: Path) -> None:
    text = (
        "# written by jobhunter container preflight (specs/018 C12, C13). While this file\n"
        "# exists no jobhunter command migrates the database on its own: run\n"
        "# `jobhunter upgrade` (or `jobhunter db migrate`). Remove it with\n"
        "# `jobhunter container preflight --leave` to go back to the host install.\n"
        'mode = "container"\n'
    )
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)


def next_steps(res: Result) -> list[str]:
    if res.failed:
        return ["refused: fix the FAIL lines above and rerun; no drop-in or marker was written."]
    steps = ["next:"]
    if res.plan is not None and res.plan.changes:
        steps.append("  systemctl --user daemon-reload")
        steps.append("  systemctl --user restart jobhunter-console")
    else:
        steps.append("  systemctl --user daemon-reload   # if you just copied the base units")
        steps.append("  systemctl --user start jobhunter-console")
    steps.append(f"  systemctl --user enable --now {' '.join(CTR_TIMERS)}")
    steps.append("after any podman secret change: rerun this, then daemon-reload and restart")
    return steps


# --- going back to the host install --------------------------------------------------------


def active_units(
    run_cmd: Callable[[list[str]], subprocess.CompletedProcess] | None = None,
) -> list[str] | None:
    """Services and timers of the container deployment that are active, or None when
    systemctl cannot be asked."""
    names = [*SERVICES, *CTR_TIMERS]
    argv = ["systemctl", "--user", "is-active", *names]
    try:
        if run_cmd is not None:
            done = run_cmd(argv)
        else:
            done = subprocess.run(argv, capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    states = done.stdout.split()
    if len(states) != len(names):
        return None
    return [
        n for n, s in zip(names, states, strict=True) if s in ("active", "activating", "reloading")
    ]


def leave(unit_dir: Path, data_dir: Path, *, dry_run: bool = False) -> list[str]:
    """Remove the drop-ins preflight wrote and the deployment marker."""
    from jobhunter.core import db

    verb = "would remove" if dry_run else "removed"
    lines: list[str] = []
    for path in our_dropins(unit_dir):
        if not dry_run:
            path.unlink()
        lines.append(f"{verb} {path}")
    marker = db.deployment_marker(data_dir)
    if marker is not None:
        if not dry_run:
            marker.unlink()
        lines.append(f"{verb} {marker} (host commands migrate on their own again)")
    if not lines:
        lines.append("nothing to remove: no preflight drop-ins and no deployment marker")
    return lines
