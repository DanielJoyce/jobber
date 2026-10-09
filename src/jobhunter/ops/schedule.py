"""systemd --user timers for the nightly run (specs/002-architecture.md#process-model).

Templates live in ``units/`` as package data. Rendering substitutes ``@EXEC@``, ``@REPO@`` and
``@PATH@`` with absolute paths resolved at install time. Nothing here installs or enables
anything unless the caller asks (``install(..., enable=True)``); tests and ``--dry-run`` only
render text.
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

TIMERS = ("jobhunter-run.timer", "jobhunter-collect.timer", "jobhunter-verify.timer")
UNIT_FILES = (
    "jobhunter-run.service",
    "jobhunter-run.timer",
    "jobhunter-collect.service",
    "jobhunter-collect.timer",
    "jobhunter-verify.service",
    "jobhunter-verify.timer",
    "jobhunter-notify@.service",
)
SYSTEM_PATH_TAIL = "/usr/local/bin:/usr/bin:/bin"
LINGER_HINT = "optional, to keep timers running while logged out: loginctl enable-linger $USER"


class ScheduleError(Exception):
    """Something the user must fix before the units can be rendered or run."""


@dataclass(frozen=True)
class Install:
    jobhunter: Path  # absolute path to the jobhunter executable (the project venv's)
    repo: Path  # WorkingDirectory: holds data/, profile/, .env and pyproject.toml
    path: str  # PATH for the units: the venv bin first, then the system dirs


def _find_repo() -> Path:
    for candidate in (Path.cwd(), Path(__file__).resolve().parents[3]):
        pyproject = candidate / "pyproject.toml"
        if not pyproject.is_file():
            continue
        try:
            name = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["name"]
        except (OSError, KeyError, tomllib.TOMLDecodeError):
            continue
        if name == "jobhunter":
            return candidate.absolute()
    raise ScheduleError("cannot find the jobhunter checkout; run this from the repository root")


def default_install() -> Install:
    """Resolve the paths the units need from the running interpreter and the checkout."""
    # Not resolve()d: the venv's bin/ entry is what carries the venv's site-packages.
    exe = Path(sys.executable).parent / "jobhunter"
    if not exe.is_file():
        found = shutil.which("jobhunter")
        if found is None:
            raise ScheduleError("jobhunter executable not found; run `uv sync` first")
        exe = Path(found)
    exe = exe.absolute()
    return Install(jobhunter=exe, repo=_find_repo(), path=f"{exe.parent}:{SYSTEM_PATH_TAIL}")


def _systemd_word(value: str) -> str:
    """Quote a value for a systemd ExecStart/Environment field if it contains whitespace."""
    return f'"{value}"' if any(ch.isspace() for ch in value) else value


def render_units(inst: Install) -> dict[str, str]:
    """Return ``{unit file name: text}`` for every unit in ``UNIT_FILES``."""
    src = resources.files("jobhunter.ops") / "units"
    values = {
        "@EXEC@": _systemd_word(str(inst.jobhunter)),
        "@REPO@": str(inst.repo),  # WorkingDirectory= takes the raw path, no quoting
        "@PATH@": inst.path,
    }
    rendered: dict[str, str] = {}
    for name in UNIT_FILES:
        text = (src / name).read_text(encoding="utf-8")
        for placeholder, value in values.items():
            text = text.replace(placeholder, value)
        rendered[name] = text
    return rendered


def unit_dir(home: Path | None = None) -> Path:
    return (home if home is not None else Path.home()) / ".config" / "systemd" / "user"


def enable_commands() -> list[list[str]]:
    return [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", *TIMERS],
    ]


def _shown(cmd: list[str]) -> str:
    return "  " + shlex.join(cmd)


def install(
    inst: Install,
    *,
    home: Path | None = None,
    dry_run: bool = False,
    enable: bool = False,
) -> str:
    """Write the units to the user unit directory. Returns the report to print.

    ``dry_run`` renders and prints everything but writes nothing. ``enable`` runs the systemctl
    commands; without it they are only printed for the user to run.
    """
    if dry_run and enable:
        raise ScheduleError("--dry-run and --enable cannot be combined")
    rendered = render_units(inst)
    target = unit_dir(home)
    lines: list[str] = []
    if dry_run:
        for name, text in rendered.items():
            lines += [f"# {target / name}", text.rstrip("\n"), ""]
        lines.append("would run (not run in dry-run):")
        lines += [_shown(c) for c in enable_commands()]
        lines += ["nothing written (dry run)", LINGER_HINT]
        return "\n".join(lines)

    target.mkdir(parents=True, exist_ok=True)
    for name, text in rendered.items():
        (target / name).write_text(text, encoding="utf-8")
        lines.append(f"wrote {target / name}")
    if enable:
        for cmd in enable_commands():
            try:
                subprocess.run(cmd, check=True)
            except FileNotFoundError as exc:
                raise ScheduleError("systemctl not found; units written but not enabled") from exc
            except subprocess.CalledProcessError as exc:
                raise ScheduleError(f"command failed: {shlex.join(cmd)}") from exc
        lines.append("enabled timers: " + " ".join(TIMERS))
    else:
        lines.append("not run (pass --enable to run these):")
        lines += [_shown(c) for c in enable_commands()]
    lines.append(LINGER_HINT)
    return "\n".join(lines)


def status() -> str:
    """Read-only: ``systemctl --user list-timers 'jobhunter-*'``."""
    cmd = ["systemctl", "--user", "list-timers", "jobhunter-*"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        raise ScheduleError("systemctl not found") from exc
    out = (proc.stdout + proc.stderr).rstrip()
    if proc.returncode != 0:
        raise ScheduleError(out or f"exit {proc.returncode}")
    return out


def uninstall(*, home: Path | None = None) -> str:
    """Disable the timers, remove the unit files this package wrote, and reload systemd."""
    lines: list[str] = []
    try:
        subprocess.run(["systemctl", "--user", "disable", "--now", *TIMERS], check=False)
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    except FileNotFoundError as exc:
        raise ScheduleError("systemctl not found; remove the unit files by hand") from exc
    target = unit_dir(home)
    for name in UNIT_FILES:
        path = target / name
        if path.is_file():
            path.unlink()
            lines.append(f"removed {path}")
    if not lines:
        lines.append("no jobhunter unit files found")
    return "\n".join(lines)
