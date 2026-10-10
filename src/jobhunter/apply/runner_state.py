"""CLI runner state that survives restarts (specs/017 "CLI runner", row "Runner off").

There is no general key-value table in the database, so the state lives in
``<data dir>/apply-runner.json`` (mode 0600)::

    {"off": {"reason": "...", "at": "..."} | null,
     "overage": {"at": "..."} | null}

``off``: the CLI runner is turned off (a failed init check, a non-claude.ai login) until the
user clicks **Turn back on**. ``overage``: the subscription reported paid extra usage on the
last call, so further CLI calls wait for an explicit click and are charged to the apply cap.
A corrupt file reads as "off", never as "on": the safe direction.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

STATE_FILE = "apply-runner.json"


@dataclass(frozen=True)
class RunnerState:
    off_reason: str | None = None
    off_at: str | None = None
    overage_at: str | None = None

    @property
    def off(self) -> bool:
        return self.off_reason is not None

    @property
    def overage(self) -> bool:
        return self.overage_at is not None


def state_path(data_dir: Path) -> Path:
    return Path(data_dir).expanduser() / STATE_FILE


def load(data_dir: Path) -> RunnerState:
    path = state_path(data_dir)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return RunnerState()
    except OSError as exc:
        return RunnerState(off_reason=f"cannot read {path.name}: {exc}")
    try:
        data = json.loads(raw)
        off = data.get("off") or None
        over = data.get("overage") or None
        return RunnerState(
            off_reason=str(off["reason"]) if off else None,
            off_at=str(off.get("at") or "") if off else None,
            overage_at=str(over.get("at") or "") if over else None,
        )
    except (ValueError, TypeError, KeyError, AttributeError):
        return RunnerState(off_reason=f"{path.name} is not valid; turn the runner back on")


def _write(data_dir: Path, state: RunnerState) -> None:
    path = state_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "off": {"reason": state.off_reason, "at": state.off_at} if state.off else None,
        "overage": {"at": state.overage_at} if state.overage else None,
    }
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".apply-runner.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _now(now: datetime | None) -> str:
    return (now or datetime.now(UTC)).astimezone(UTC).isoformat(timespec="seconds")


def turn_off(data_dir: Path, reason: str, now: datetime | None = None) -> RunnerState:
    cur = load(data_dir)
    state = RunnerState(off_reason=reason, off_at=_now(now), overage_at=cur.overage_at)
    _write(data_dir, state)
    return state


def turn_on(data_dir: Path) -> RunnerState:
    """**Turn back on**: clears both the off state and the overage hold."""
    state = RunnerState()
    _write(data_dir, state)
    return state


def set_overage(data_dir: Path, on: bool, now: datetime | None = None) -> RunnerState:
    cur = load(data_dir)
    if cur.overage == on:
        return cur
    state = RunnerState(
        off_reason=cur.off_reason, off_at=cur.off_at, overage_at=_now(now) if on else None
    )
    _write(data_dir, state)
    return state
