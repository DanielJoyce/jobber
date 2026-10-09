"""/prefs never dead-ends: lenient loading, a skeleton profile, and resume upload.

Nothing here logs file contents. Writes go through ``jobhunter.scoring.profile`` helpers
(atomic, 0600) so the profile and resume stay private.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from jobhunter.config import Settings, resolve_path
from jobhunter.scoring.profile import (
    PREFERENCES_FILE,
    Profile,
    ProfileError,
    load_profile_for,
    salvage_preferences,
    validate_profile_data,
)

MAX_RESUME_BYTES = 1024 * 1024
TEXT_SUFFIXES = (".md", ".markdown", ".txt")
CONVERT_HINT = "export your resume as Markdown (.md) or plain text (.txt) and upload that"

# examples/preferences.example.yaml with every personal-looking placeholder cleared: the
# numbers that are sensible defaults stay, the prose and place names do not.
SKELETON: dict[str, Any] = {
    "target_titles": [],
    "hard": {
        "states_allowed": "all",
        "states_excluded": [],
        "relocation_ok": False,
        "remote_ok": True,
        "remote_only": False,
        "salary_floor": None,
        "salary_target": None,
        "employment_types_excluded": ["seasonal", "temporary"],
        "requires_i_lack": [],
        "title_exclusions": ["intern", "volunteer", "student trainee"],
        "hide_unstated_salary": False,
    },
    "soft": {
        "weights": {
            "skills": 0.30,
            "seniority": 0.20,
            "domain": 0.20,
            "comp": 0.15,
            "location": 0.15,
        },
        "state_ranking": [],
        "remote_bonus": 5,
    },
    "current_focus": {"since": None, "doing": None, "want_more_of": [], "done_with": []},
    "narrative": {"want": None, "avoid": None, "dealbreakers_soft": None, "context": None},
    "queries": [],
    "buckets": {},
}


def skeleton_profile() -> Profile:
    return validate_profile_data(SKELETON, base=Profile())


@dataclass
class PrefsState:
    """What /prefs can show. ``kind``: ok, resume (resume problem only), missing, broken."""

    kind: str
    profile: Profile
    profile_dir: Path
    prefs_file: Path
    errors: dict[str, str] = field(default_factory=dict)  # dotted model path -> message
    yaml_error: str | None = None
    raw_text: str | None = None
    resume_error: str | None = None
    resume_target: str = ""

    @property
    def writes_whole_file(self) -> bool:
        return self.kind in ("missing", "broken")


def lenient_state(settings: Settings, text: str | None = None) -> PrefsState:
    """Load the profile as far as possible. Never raises ``ProfileError``.

    ``text`` overrides the file's content (the raw YAML editor re-rendering what was typed);
    the resume is not checked in that case.
    """
    profile_dir = resolve_path(settings.paths.profile_dir)
    prefs_file = profile_dir / PREFERENCES_FILE
    state = PrefsState("ok", Profile(), profile_dir, prefs_file)
    if text is None:
        if not prefs_file.exists():
            state.kind = "missing"
            state.profile = skeleton_profile()
            state.resume_target = str(resume_target(settings, state.profile))
            state.resume_error = _resume_absent(state.resume_target)
            return state
        try:
            text = prefs_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            state.kind = "broken"
            state.yaml_error = f"cannot read file: {exc}"
            state.raw_text = ""
            return state
        check_resume = True
    else:
        check_resume = False
    state.raw_text = text
    try:
        profile, errors = salvage_preferences(text)
    except ProfileError as exc:
        state.kind = "broken"
        state.yaml_error = exc.reason
        return state
    state.profile = profile
    state.resume_target = str(resume_target(settings, profile))
    if errors:
        state.kind = "broken"
        state.errors = errors
        state.resume_error = _resume_absent(state.resume_target) if check_resume else None
        return state
    if check_resume:
        try:
            full = load_profile_for(settings)
        except ProfileError as exc:
            state.kind = "resume"
            state.resume_error = str(exc)
        else:
            state.profile = full
    return state


def _resume_absent(target: str) -> str | None:
    """A message when no readable resume exists at ``target`` (a file or a folder)."""
    path = Path(target)
    if path.is_file() or (path.is_dir() and any(path.glob("*.md"))):
        return None
    return f"resume not found at {target}"


# ─── resume upload ──────────────────────────────────────────────────────────


class UploadError(ValueError):
    """The upload was refused; the message is shown to the user."""

    def __init__(self, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.status = status


def clean_upload_name(filename: str) -> str:
    """Basename only, safe characters only, always ending in ``.md``."""
    base = re.split(r"[\\/]", filename or "")[-1]
    stem = base.rsplit(".", 1)[0] if "." in base else base
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip(".-_")[:80]
    return f"{stem or 'resume'}.md"


def check_upload(filename: str, data: bytes) -> str:
    """Return the decoded text, or raise ``UploadError``."""
    lower = (filename or "").lower()
    if lower.endswith((".pdf", ".docx", ".doc", ".rtf", ".odt")):
        raise UploadError(f"Can't read that file type here: {CONVERT_HINT}.", 415)
    if not lower.endswith(TEXT_SUFFIXES):
        raise UploadError(f"Only .md or .txt files are accepted: {CONVERT_HINT}.", 415)
    if len(data) > MAX_RESUME_BYTES:
        raise UploadError(
            "That file is over 1 MB. A resume in text form should be far smaller.", 413
        )
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise UploadError("The file is not UTF-8 text. Save it as UTF-8 and try again.") from exc
    if "\x00" in text:
        raise UploadError(f"That doesn't look like a text file: {CONVERT_HINT}.")
    if not text.strip():
        raise UploadError("The file is empty.")
    return text.replace("\r\n", "\n")


def resume_target(settings: Settings, profile: Profile) -> Path:
    """Where an uploaded resume goes: the profile's own resume_path, else the configured one.

    A path that is a directory (or has no file suffix) is a resume folder; otherwise it is
    the resume file itself.
    """
    if profile.resume_path:
        profile_dir = resolve_path(settings.paths.profile_dir)
        location = resolve_path(profile.resume_path, base=profile_dir)
        if not location.exists() and not Path(profile.resume_path).expanduser().is_absolute():
            for base in (profile_dir.resolve().parent, Path.cwd()):
                alt = resolve_path(profile.resume_path, base=base)
                if alt.exists():
                    return alt
        return location
    return resolve_path(settings.paths.resume_path)


def _private_dir(path: Path) -> None:
    if not path.exists():
        path.mkdir(parents=True, mode=0o700)
        os.chmod(path, 0o700)


def store_resume(target: Path, filename: str, text: str, now: datetime) -> Path:
    """Write the resume, moving what it replaces into ``.previous/`` (history is kept)."""
    stamp = now.strftime("%Y%m%dT%H%M%S")
    if target.is_dir() or (not target.exists() and not target.suffix):
        folder, dest = target, target / clean_upload_name(filename)
        _private_dir(folder)
        old = sorted(p for p in folder.glob("*.md") if p.is_file())
    else:
        folder, dest = target.parent, target
        _private_dir(folder)
        old = [target] if target.is_file() else []
    if old:
        previous = folder / ".previous"
        _private_dir(previous)
        for path in old:
            moved = previous / f"{path.stem}.{stamp}{path.suffix}"
            n = 1
            while moved.exists():
                moved = previous / f"{path.stem}.{stamp}-{n}{path.suffix}"
                n += 1
            shutil.move(str(path), moved)
    fd, tmp_name = tempfile.mkstemp(dir=folder, prefix=".resume.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)
    return dest


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
