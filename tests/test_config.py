from pathlib import Path

import pytest
from pydantic import ValidationError

from jobhunter.config import CONFIG_ENV_VAR, Settings, load_settings, resolve_path


@pytest.fixture(autouse=True)
def _no_env_config(monkeypatch):
    monkeypatch.delenv(CONFIG_ENV_VAR, raising=False)


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_defaults_when_no_file(tmp_path):
    s = load_settings(config_path=tmp_path / "missing.toml")
    assert isinstance(s, Settings)
    assert s.paths.data_dir == Path("data")
    assert s.paths.cache_dir == Path("data/cache")
    assert s.paths.db_path == Path("data/jobhunter.db")
    assert s.paths.profile_dir == Path("profile")
    assert s.paths.resume_path == Path("resume")
    assert s.fetch.respect_robots is True
    assert s.fetch.default_rps == 0.2
    assert s.fetch.global_concurrency == 8
    assert s.fetch.timeout_s == 20.0
    assert s.fetch.max_retries == 5
    assert s.scoring.screen_scorer == "anthropic:claude-haiku-4-5"
    assert s.scoring.deep_scorer == "anthropic:claude-opus-5"
    assert s.scoring.daily_cap_usd == 2.0
    assert s.scoring.weekly_cap_usd == 10.0
    assert s.scoring.deep_shortlist == 40
    assert s.mail.label == "jobhunter/alerts"
    assert s.mail.fallback_sender_domains == []
    assert s.console.host == "127.0.0.1"
    assert s.console.port == 8808


def test_toml_overrides_one_nested_key_keeps_siblings(tmp_path):
    cfg = _write(tmp_path / "config.toml", "[fetch]\ndefault_rps = 0.5\n")
    s = load_settings(config_path=cfg)
    assert s.fetch.default_rps == 0.5
    assert s.fetch.respect_robots is True
    assert s.fetch.global_concurrency == 8
    assert s.fetch.max_retries == 5
    assert s.paths.data_dir == Path("data")


def test_overrides_dict_wins_over_toml(tmp_path):
    cfg = _write(tmp_path / "config.toml", "[scoring]\ndeep_shortlist = 10\ndaily_cap_usd = 3.0\n")
    s = load_settings(config_path=cfg, overrides={"scoring": {"deep_shortlist": 5}})
    assert s.scoring.deep_shortlist == 5
    assert s.scoring.daily_cap_usd == 3.0
    assert s.scoring.weekly_cap_usd == 10.0


def test_env_var_selects_config_file(tmp_path, monkeypatch):
    cfg = _write(tmp_path / "env.toml", "[console]\nport = 9000\n")
    monkeypatch.setenv(CONFIG_ENV_VAR, str(cfg))
    s = load_settings()
    assert s.console.port == 9000
    assert s.console.host == "127.0.0.1"


def test_explicit_path_beats_env_var(tmp_path, monkeypatch):
    env_cfg = _write(tmp_path / "env.toml", "[console]\nport = 9000\n")
    arg_cfg = _write(tmp_path / "arg.toml", "[console]\nport = 9100\n")
    monkeypatch.setenv(CONFIG_ENV_VAR, str(env_cfg))
    assert load_settings(config_path=arg_cfg).console.port == 9100


def test_unknown_key_raises_naming_the_key(tmp_path):
    cfg = _write(tmp_path / "config.toml", "[fetch]\nbogus = 1\n")
    with pytest.raises(ValidationError, match="bogus"):
        load_settings(config_path=cfg)


def test_unknown_top_level_section_raises(tmp_path):
    cfg = _write(tmp_path / "config.toml", "[spend]\nmonthly_cap_usd = 1\n")
    with pytest.raises(ValidationError, match="spend"):
        load_settings(config_path=cfg)


def test_user_agent_format(tmp_path):
    cfg = _write(tmp_path / "config.toml", '[fetch]\nuser_agent_contact = "tester@example.com"\n')
    s = load_settings(config_path=cfg)
    assert s.user_agent == "jobhunter/0.1 (personal job search; tester@example.com)"


def test_resolve_path_relative_to_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert resolve_path("data/x.db") == tmp_path / "data" / "x.db"
    assert resolve_path("/abs/path") == Path("/abs/path")
    assert resolve_path("sub", base=Path("/base")) == Path("/base/sub")
