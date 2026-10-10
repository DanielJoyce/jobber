"""XDG default locations, provenance (no fallback to the old layout), and `jobhunter paths`.
Synthetic."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from jobhunter.cli import app
from jobhunter.config import Paths, Settings, load_settings
from jobhunter.mail import auth
from jobhunter.xdg import cache_home, config_home, data_home

runner = CliRunner()


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


# ─── no fallback to the old layout ──────────────────────────────────────────


def test_old_relative_data_is_never_used_as_a_path(old_layout, monkeypatch):
    """Resolution is explicit setting, else XDG default: an old ./data is not a fallback."""
    root = old_layout()
    monkeypatch.chdir(root)
    s = load_settings()
    assert s.paths.db_path == data_home() / "jobhunter.db"
    assert s.paths.profile_dir == data_home() / "profile"
    assert s.paths.resume_path == data_home() / "resume"
    assert s.paths.cache_dir == cache_home()
    assert all(src.startswith("default") for src in s.paths.sources.values())


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


def test_paths_command_reports_unmigrated_old_data(old_layout, monkeypatch):
    root = old_layout()
    monkeypatch.chdir(root)
    result = runner.invoke(app, ["paths"])
    assert result.exit_code == 0, result.output  # paths always works
    assert "stopped: your data is still in" in result.output
    assert str(root / "data" / "jobhunter.db") in result.output
    info = json.loads(runner.invoke(app, ["paths", "--json"]).output)
    assert info["old_layout"]["state"] == "unmigrated"
    assert info["old_layout"]["unmigrated"] == [str(root / "data" / "jobhunter.db")]
    assert not data_home().exists()


def test_paths_command_with_no_old_data_says_nothing_about_it():
    info = json.loads(runner.invoke(app, ["paths", "--json"]).output)
    assert info["old_layout"] == {"state": "ok", "unmigrated": [], "stale": [], "message": ""}
