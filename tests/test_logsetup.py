"""Process-wide logging setup (bug 85e102e): level precedence, stderr output, quiet libraries,
uvicorn alignment, and the CLI wiring. No network."""

from __future__ import annotations

import logging

import pytest
from typer.testing import CliRunner

from jobhunter import logsetup
from jobhunter.cli import app
from jobhunter.config import Settings


@pytest.fixture(autouse=True)
def _no_level_env(monkeypatch):
    monkeypatch.delenv(logsetup.LEVEL_ENV_VAR, raising=False)


def test_default_is_info():
    assert logsetup.resolve_level() == logging.INFO


def test_config_then_env_then_flags(monkeypatch):
    assert logsetup.resolve_level(config_level="WARNING") == logging.WARNING
    monkeypatch.setenv(logsetup.LEVEL_ENV_VAR, "debug")
    assert logsetup.resolve_level(config_level="WARNING") == logging.DEBUG
    assert logsetup.resolve_level(quiet=1, config_level="WARNING") == logging.INFO
    monkeypatch.setenv(logsetup.LEVEL_ENV_VAR, "nonsense")  # ignored, falls to config
    assert logsetup.resolve_level(config_level="ERROR") == logging.ERROR


def test_verbose_and_quiet_step_and_clamp():
    assert logsetup.resolve_level(verbose=1) == logging.DEBUG
    assert logsetup.resolve_level(verbose=5) == logging.DEBUG
    assert logsetup.resolve_level(quiet=1) == logging.WARNING
    assert logsetup.resolve_level(quiet=9) == logging.ERROR


def test_config_section_validates_and_defaults():
    assert Settings().logging.level == "INFO"
    assert Settings.model_validate({"logging": {"level": "debug"}}).logging.level == "DEBUG"
    with pytest.raises(ValueError):
        Settings.model_validate({"logging": {"level": "loud"}})


def test_configure_writes_timestamped_lines_to_stderr(capsys):
    logsetup.configure(logging.INFO)
    logging.getLogger("jobhunter.pipeline.runner").info("stage list done")
    logging.getLogger("jobhunter.pipeline.runner").debug("per-item detail")
    err = capsys.readouterr().err
    assert "INFO" in err and "jobhunter.pipeline.runner: stage list done" in err
    assert err[:4].isdigit()  # starts with a timestamp
    assert "per-item detail" not in err


def test_configure_twice_does_not_double_lines(capsys):
    logsetup.configure(logging.INFO)
    logsetup.configure(logging.INFO)
    logging.getLogger("jobhunter.x").info("once")
    assert capsys.readouterr().err.count("once") == 1


def test_third_party_loggers_stay_at_warning_even_at_debug():
    logsetup.configure(logging.DEBUG)
    for name in ("httpx", "anthropic", "urllib3"):
        assert logging.getLogger(name).level == logging.WARNING
    logsetup.configure(logging.ERROR)
    assert logging.getLogger("httpx").level == logging.ERROR


def test_uvicorn_loggers_share_the_root_handler_and_access_stays_info(capsys):
    logsetup.configure(logging.WARNING)
    assert logging.getLogger("uvicorn.access").level == logging.INFO
    assert logging.getLogger("uvicorn.access").propagate
    assert logging.getLogger("uvicorn.error").level == logging.WARNING
    logging.getLogger("uvicorn.access").info('127.0.0.1 - "GET /help HTTP/1.1" 200')
    assert "GET /help" in capsys.readouterr().err
    cfg = logsetup.uvicorn_log_config()
    assert cfg["disable_existing_loggers"] is False
    assert all(lg["handlers"] == [] for lg in cfg["loggers"].values())


def test_spec_label_hides_key_shaped_specs():
    assert logsetup.spec_label("anthropic:claude-haiku-5-5") == "anthropic:claude-haiku-5-5"
    secret = "sk-ant-api03-" + "a1B2c3D4" * 8
    label = logsetup.spec_label(f"openai-compat:{secret}")
    assert secret not in label and label.startswith("(hidden value ")


def test_throttle_logs_info_once_per_interval_else_debug(caplog):
    caplog.set_level(logging.DEBUG)
    clock = iter([1.0, 2.0, 31.0])
    t = logsetup.Throttle(logging.getLogger("jobhunter.t"), seconds=30)
    t._time = lambda: next(clock)
    t.last = 0.0
    levels = []
    for _ in range(3):
        t.tick("n=%d", t.n)
        levels.append(caplog.records[-1].levelno)
    assert levels == [logging.DEBUG, logging.DEBUG, logging.INFO]


def test_cli_env_var_sets_the_level(monkeypatch):
    monkeypatch.setenv(logsetup.LEVEL_ENV_VAR, "DEBUG")
    result = CliRunner().invoke(app, ["version"])
    assert result.exit_code == 0, result.output
    assert logging.getLogger().level == logging.DEBUG


def test_cli_flags_and_config(tmp_path, monkeypatch):
    cfg = tmp_path / "config.toml"
    cfg.write_text('[logging]\nlevel = "WARNING"\n')
    monkeypatch.setenv("JOBHUNTER_CONFIG", str(cfg))
    assert CliRunner().invoke(app, ["version"]).exit_code == 0
    assert logging.getLogger().level == logging.WARNING
    assert CliRunner().invoke(app, ["-v", "version"]).exit_code == 0
    assert logging.getLogger().level == logging.INFO
    assert CliRunner().invoke(app, ["-vv", "version"]).exit_code == 0
    assert logging.getLogger().level == logging.DEBUG
    assert CliRunner().invoke(app, ["-q", "version"]).exit_code == 0
    assert logging.getLogger().level == logging.ERROR


def test_cli_without_settings_defaults_to_info():
    assert CliRunner().invoke(app, ["version"]).exit_code == 0
    assert logging.getLogger().level == logging.INFO


def test_invalid_env_level_warns_once_without_echoing_it(monkeypatch, caplog):
    monkeypatch.setenv(logsetup.LEVEL_ENV_VAR, "LOUDSENTINEL")
    with caplog.at_level(logging.WARNING):
        assert CliRunner().invoke(app, ["version"]).exit_code == 0
    warns = [r.getMessage() for r in caplog.records if "ignoring" in r.getMessage()]
    assert len(warns) == 1 and "LOUDSENTINEL" not in warns[0]


def test_override_source_names_what_beats_the_config(monkeypatch):
    assert logsetup.override_source() == ""
    assert logsetup.override_source(verbose=1) == "the command line (-v/-q)"
    monkeypatch.setenv(logsetup.LEVEL_ENV_VAR, "DEBUG")
    assert logsetup.override_source() == logsetup.LEVEL_ENV_VAR


def test_config_changes_marks_logging_level_no_effect_when_overridden(monkeypatch):
    from jobhunter import config_watch

    monkeypatch.setattr(logsetup, "OVERRIDE", logsetup.LEVEL_ENV_VAR)
    old = Settings()
    new = Settings.model_validate({"logging": {"level": "DEBUG"}})
    (change,) = [
        c for c in config_watch.diff(old, new, ["logging.level"]) if c.field == "logging.level"
    ]
    assert change.class_text == "no effect: set on JOBHUNTER_LOG_LEVEL"
