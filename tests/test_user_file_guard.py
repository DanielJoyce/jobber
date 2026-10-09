"""The user-file guard in conftest.py: real profile, resume, data and config stay unreadable."""

from __future__ import annotations

import os
import pwd
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def _real_home() -> Path:
    # HOME is patched during tests, so ask the password database for the real one.
    return Path(pwd.getpwuid(os.getuid()).pw_dir)


def test_opening_repo_resume_raises():
    with pytest.raises(RuntimeError, match="real user file"):
        open(REPO / "resume" / "anything.md", encoding="utf-8")  # noqa: SIM115


def test_read_text_on_repo_profile_raises():
    with pytest.raises(RuntimeError, match="real user file"):
        (REPO / "profile" / "preferences.yaml").read_text(encoding="utf-8")


def test_repo_data_and_config_raise():
    with pytest.raises(RuntimeError, match="real user file"):
        (REPO / "data" / "jobhunter.db").read_bytes()
    with pytest.raises(RuntimeError, match="real user file"):
        open(REPO / "config.toml", "rb")  # noqa: SIM115


def test_real_home_config_and_env_raise():
    with pytest.raises(RuntimeError, match="real user file"):
        open(_real_home() / ".config" / "jobhunter" / "config.toml", "rb")  # noqa: SIM115
    with pytest.raises(RuntimeError, match="real user file"):
        open(_real_home() / ".env", encoding="utf-8")  # noqa: SIM115


def test_tmp_files_are_fine(tmp_path):
    f = tmp_path / "profile" / "preferences.yaml"
    f.parent.mkdir()
    f.write_text("hard: {}\n", encoding="utf-8")
    assert f.read_text(encoding="utf-8") == "hard: {}\n"


def test_home_is_a_tmp_dir(tmp_path_factory):
    home = Path(os.environ["HOME"]).resolve()
    assert home != _real_home().resolve()
    assert tmp_path_factory.getbasetemp().resolve() in home.parents


def test_jobhunter_env_vars_are_unset():
    assert "JOBHUNTER_CONFIG" not in os.environ
    assert "JOBHUNTER_ENV_FILE" not in os.environ
