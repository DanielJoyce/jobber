"""XDG default locations, provenance, the legacy fallback, and `jobhunter paths`. Synthetic."""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from jobhunter import config as cfg
from jobhunter.cli import app
from jobhunter.config import Paths, Settings, load_settings
from jobhunter.mail import auth
from jobhunter.xdg import cache_home, config_home, data_home

runner = CliRunner()


@pytest.fixture(autouse=True)
def _reset_warned():
    cfg._warned_legacy.clear()
    yield
    cfg._warned_legacy.clear()


def _legacy_tree(cwd: Path) -> None:
    """The pre-XDG layout, as a synthetic checkout would have it."""
    (cwd / "data").mkdir()
    sqlite3.connect(cwd / "data" / "jobhunter.db").close()
    (cwd / "profile").mkdir()
    (cwd / "profile" / "preferences.yaml").write_text("hard: {}\n", encoding="utf-8")
    (cwd / "resume").mkdir()
    (cwd / "resume" / "me.md").write_text("Synthetic Person\n", encoding="utf-8")


# ─── defaults ───────────────────────────────────────────────────────────────


def test_defaults_are_xdg_not_repo_relative(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    home = Path.home()
    s = load_settings()
    assert s.paths.db_path == home / ".local/share/jobhunter/jobhunter.db"
    assert s.paths.cache_dir == home / ".cache/jobhunter"
    assert not s.paths.db_path.is_relative_to(Path.cwd())
    assert config_home() == home / ".config/jobhunter"
    assert set(s.paths.sources.values()) == {"default"}


def test_xdg_environment_variables_move_the_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "d"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "c"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    s = load_settings()
    assert s.paths.data_dir == tmp_path / "d/jobhunter"
    assert s.paths.db_path == tmp_path / "d/jobhunter/jobhunter.db"
    assert s.paths.profile_dir == tmp_path / "d/jobhunter/profile"
    assert s.paths.resume_path == tmp_path / "d/jobhunter/resume"
    assert s.paths.cache_dir == tmp_path / "c/jobhunter"
    assert s.scoring.openrouter.catalog_cache == tmp_path / "c/jobhunter/openrouter_models.json"
    assert s.paths.sources["db_path"] == "default (XDG_DATA_HOME)"
    assert s.paths.sources["cache_dir"] == "default (XDG_CACHE_HOME)"
    assert config_home() == tmp_path / "cfg/jobhunter"
    assert auth.token_file_path() == tmp_path / "cfg/jobhunter/gmail_token.json"


def test_config_file_is_read_from_xdg_config_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    (tmp_path / "jobhunter").mkdir()
    (tmp_path / "jobhunter" / "config.toml").write_text("[fetch]\ndefault_rps = 0.7\n")
    assert load_settings().fetch.default_rps == 0.7


def test_relative_xdg_value_is_ignored(monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", "relative/dir")
    assert data_home() == Path.home() / ".local/share/jobhunter"
    assert cache_home() == Path.home() / ".cache/jobhunter"


def test_data_dir_override_moves_what_derives_from_it(tmp_path):
    cfg_file = tmp_path / "c.toml"
    cfg_file.write_text(f'[paths]\ndata_dir = "{tmp_path / "x"}"\n')
    s = load_settings(config_path=cfg_file)
    assert s.paths.db_path == tmp_path / "x/jobhunter.db"
    assert s.paths.profile_dir == tmp_path / "x/profile"
    assert s.paths.sources["db_path"] == "under data_dir"
    assert s.paths.sources["data_dir"] == "config file"
    assert s.paths.cache_dir == Path.home() / ".cache/jobhunter"  # cache is not data


# ─── precedence and provenance ──────────────────────────────────────────────


def test_explicit_config_beats_default_and_env_beats_config(tmp_path, monkeypatch):
    cfg_file = tmp_path / "c.toml"
    cfg_file.write_text(f'[paths]\ndb_path = "{tmp_path / "file.db"}"\n')
    s = load_settings(config_path=cfg_file)
    assert s.paths.db_path == tmp_path / "file.db"
    assert s.paths.sources["db_path"] == "config file"
    assert s.paths.sources["profile_dir"] == "default"

    monkeypatch.setenv("JOBHUNTER_DB_PATH", str(tmp_path / "env.db"))
    s = load_settings(config_path=cfg_file)
    assert s.paths.db_path == tmp_path / "env.db"
    assert s.paths.sources["db_path"] == "env JOBHUNTER_DB_PATH"

    s = load_settings(config_path=cfg_file, overrides={"paths": {"db_path": tmp_path / "o.db"}})
    assert s.paths.db_path == tmp_path / "o.db"
    assert s.paths.sources["db_path"] == "override"


def test_each_env_variable_sets_its_field(tmp_path, monkeypatch):
    for var, name in (
        ("JOBHUNTER_DATA_DIR", "data_dir"),
        ("JOBHUNTER_CACHE_DIR", "cache_dir"),
        ("JOBHUNTER_PROFILE_DIR", "profile_dir"),
        ("JOBHUNTER_RESUME_PATH", "resume_path"),
    ):
        monkeypatch.setenv(var, str(tmp_path / name))
    s = load_settings()
    for name in ("data_dir", "cache_dir", "profile_dir", "resume_path"):
        assert getattr(s.paths, name) == tmp_path / name
    assert s.paths.sources["resume_path"] == "env JOBHUNTER_RESUME_PATH"


def test_constructed_paths_keep_explicit_values_and_expand_home(tmp_path):
    p = Paths(db_path=Path("~/x.db"), profile_dir=tmp_path / "p")
    assert p.db_path == Path.home() / "x.db"
    assert p.profile_dir == tmp_path / "p"
    assert p.sources["db_path"] == "explicit"
    assert p.resume_path == Path.home() / ".local/share/jobhunter/resume"


def test_explicit_catalog_cache_is_kept(tmp_path):
    s = Settings.model_validate({"scoring": {"openrouter": {"catalog_cache": str(tmp_path / "m")}}})
    assert s.scoring.openrouter.catalog_cache == tmp_path / "m"


# ─── legacy fallback ────────────────────────────────────────────────────────


def test_old_relative_data_keeps_working_with_one_warning(tmp_path, monkeypatch, caplog):
    monkeypatch.chdir(tmp_path)
    _legacy_tree(tmp_path)
    with caplog.at_level(logging.WARNING, logger="jobhunter.config"):
        s = load_settings()
        load_settings()  # the second load does not warn again
    assert s.paths.db_path == tmp_path / "data/jobhunter.db"
    assert s.paths.profile_dir == tmp_path / "profile"
    assert s.paths.resume_path == tmp_path / "resume"
    assert s.paths.data_dir == tmp_path / "data"
    assert s.paths.sources["db_path"] == "legacy"
    warnings = [r.getMessage() for r in caplog.records if "migrate-paths" in r.getMessage()]
    assert len(warnings) == 1
    assert str(tmp_path / "data/jobhunter.db") in warnings[0]


def test_new_location_wins_once_it_exists(tmp_path, monkeypatch, caplog):
    monkeypatch.chdir(tmp_path)
    _legacy_tree(tmp_path)
    new_db = data_home() / "jobhunter.db"
    new_db.parent.mkdir(parents=True)
    sqlite3.connect(new_db).close()
    with caplog.at_level(logging.WARNING, logger="jobhunter.config"):
        s = load_settings()
    assert s.paths.db_path == new_db
    assert s.paths.sources["db_path"] == "default"
    assert s.paths.profile_dir == tmp_path / "profile"  # not migrated yet: still legacy
    assert "profile" in caplog.text and "jobhunter.db" not in caplog.text


def test_no_legacy_files_means_no_fallback_and_no_warning(tmp_path, monkeypatch, caplog):
    monkeypatch.chdir(tmp_path)
    with caplog.at_level(logging.WARNING, logger="jobhunter.config"):
        s = load_settings()
    assert s.paths.db_path == data_home() / "jobhunter.db"
    assert "migrate-paths" not in caplog.text


def test_explicit_paths_never_fall_back(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _legacy_tree(tmp_path)
    s = load_settings(overrides={"paths": {"db_path": tmp_path / "mine.db", "data_dir": tmp_path}})
    assert s.paths.db_path == tmp_path / "mine.db"
    assert s.paths.profile_dir == tmp_path / "profile"  # data_dir/profile, which exists anyway
    assert "legacy" not in s.paths.sources.values()


def test_legacy_data_is_found_from_the_checkout_when_run_from_another_directory(
    tmp_path, monkeypatch
):
    """Running from ~ must not miss the checkout's data and start an empty database."""
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    _legacy_tree(checkout)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setattr(cfg, "source_checkout", lambda: checkout)
    s = load_settings()
    assert s.paths.db_path == checkout / "data/jobhunter.db"
    assert s.paths.sources["db_path"] == "legacy"
    assert s.paths.profile_dir == checkout / "profile"
    # a command that opens the database uses the real one and creates nothing at the new place
    from jobhunter.core import db

    conn = db.connect(s.paths.db_path)
    conn.close()
    assert not data_home().exists()


def test_cwd_legacy_data_wins_over_the_checkout(tmp_path, monkeypatch):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    _legacy_tree(checkout)
    here = tmp_path / "here"
    here.mkdir()
    _legacy_tree(here)
    monkeypatch.chdir(here)
    monkeypatch.setattr(cfg, "source_checkout", lambda: checkout)
    assert load_settings().paths.db_path == here / "data/jobhunter.db"


def test_fallback_can_be_disabled(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _legacy_tree(tmp_path)
    s = load_settings(legacy_fallback=False)
    assert s.paths.db_path == data_home() / "jobhunter.db"


# ─── jobhunter paths ────────────────────────────────────────────────────────


def test_paths_command_reports_source_and_existence(tmp_path, monkeypatch):
    monkeypatch.setenv("JOBHUNTER_DB_PATH", str(tmp_path / "e.db"))
    (tmp_path / "e.db").write_bytes(b"")
    result = runner.invoke(app, ["paths", "--json"])
    assert result.exit_code == 0, result.output
    info = json.loads(result.output)
    assert info["db_path"] == {
        "path": str(tmp_path / "e.db"),
        "source": "env JOBHUNTER_DB_PATH",
        "exists": True,
    }
    assert info["profile_dir"]["source"] == "default"
    assert info["profile_dir"]["exists"] is False
    assert info["profile_dir"]["path"].endswith(".local/share/jobhunter/profile")
    assert info["cache_dir"]["path"].endswith(".cache/jobhunter")
    assert info["config_file"]["path"].endswith(".config/jobhunter/config.toml")
    assert set(info) >= {"config_file", "data_dir", "db_path", "profile_dir", "resume_path"}


def test_paths_command_text_marks_missing_and_config_source(tmp_path, monkeypatch):
    c = tmp_path / "c.toml"
    c.write_text(f'[paths]\nresume_path = "{tmp_path / "r"}"\n')
    monkeypatch.setenv("JOBHUNTER_CONFIG", str(c))
    result = runner.invoke(app, ["paths"])
    assert result.exit_code == 0, result.output
    lines = {line.split()[0]: line for line in result.output.splitlines() if line.strip()}
    assert "[config file]" in lines["resume_path"] and "missing" in lines["resume_path"]
    assert "exists" in lines["config_file"] and "[env JOBHUNTER_CONFIG]" in lines["config_file"]


def test_paths_command_flags_legacy(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _legacy_tree(tmp_path)
    result = runner.invoke(app, ["paths"])
    assert "[legacy]" in result.output
    assert "migrate-paths" in result.output
