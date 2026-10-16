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
    home = Path.home()  # a tmp dir under the isolation fixture
    assert s.paths.data_dir == home / ".local/share/jobhunter"
    assert s.paths.cache_dir == home / ".cache/jobhunter"
    assert s.paths.db_path == home / ".local/share/jobhunter/jobhunter.db"
    assert s.paths.profile_dir == home / ".local/share/jobhunter/profile"
    assert s.paths.resume_path == home / ".local/share/jobhunter/resume"
    assert s.fetch.respect_robots is True
    assert s.fetch.default_rps == 0.2
    assert s.fetch.global_concurrency == 8
    assert s.fetch.timeout_s == 20.0
    assert s.fetch.max_retries == 5
    assert s.scoring.screen_scorer == "anthropic:claude-haiku-5-5"
    assert s.scoring.deep_scorer == "anthropic:claude-opus-5-5"
    assert s.apply.api_model == "claude-opus-5-5"
    assert s.apply.api_entailment_model == "claude-haiku-5-5"
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
    assert s.paths.data_dir == Path.home() / ".local/share/jobhunter"


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


def test_fetch_hosts_from_toml_merges_with_builtin_defaults(tmp_path):
    cfg = _write(
        tmp_path / "config.toml",
        '[fetch.hosts."Jobs.Example.org"]\nrps = 0.5\n\n[fetch.hosts."usnlx.com"]\nrps = 0.2\n',
    )
    fetch = load_settings(config_path=cfg).fetch
    assert fetch.host_rps("jobs.example.org") == 0.5  # keys are lowercased
    assert fetch.host_rps("usnlx.com") == 0.2  # the user's entry beats the built-in one
    assert fetch.host_rps("nlx.jobsyn.org") == 1.0  # untouched built-in stays


@pytest.mark.parametrize(
    ("body", "match"),
    [
        ('[fetch.hosts."a.example.org"]\nrps = 0\n', "greater than 0"),
        ('[fetch.hosts."a.example.org"]\nrps = -1\n', "greater than 0"),
        ('[fetch.hosts."a.example.org"]\nrps = 2.5\n', "less than or equal to 2"),
        ('[fetch.hosts."a.example.org"]\nrps = "fast"\n', "rps"),
        ('[fetch.hosts."a.example.org"]\nrps = 1\nburst = 3\n', "burst"),
        ('[fetch.hosts."a.example.org"]\n', "rps"),
        ('[fetch.hosts."https://a.example.org"]\nrps = 1\n', "bare hostname"),
        ('[fetch.hosts."a.example.org:8443"]\nrps = 1\n', "bare hostname"),
        ('[fetch.hosts."*.example.org"]\nrps = 1\n', "bare hostname"),
        ('[fetch.hosts."a.example.org/x"]\nrps = 1\n', "bare hostname"),
        (
            '[fetch.hosts."A.example.org"]\nrps = 1\n[fetch.hosts."a.example.org"]\nrps = 1\n',
            "twice",
        ),
        ("[fetch]\nhosts = 3\n", "hosts"),
    ],
)
def test_fetch_hosts_rejects_bad_entries(tmp_path, body, match):
    cfg = _write(tmp_path / "config.toml", body)
    with pytest.raises(ValidationError, match=match):
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


GOOD_SCORERS = [
    "anthropic:claude-haiku-4-5",
    "openrouter:openai/gpt-oss-120b",
    "jev:typesafe/jev-1.13",
    "decisions:typesafe/jev-1.13",
    "local:qwen3-8b",
    "local:jev:latest",
    "openai-compat:llama-3.1-70b",
]


def _screen(tmp_path, spec, deep="anthropic:claude-opus-5"):
    cfg = _write(
        tmp_path / "c.toml", f'[scoring]\nscreen_scorer = "{spec}"\ndeep_scorer = "{deep}"\n'
    )
    return load_settings(config_path=cfg).scoring


@pytest.mark.parametrize("spec", GOOD_SCORERS)
def test_scorer_spec_accepted(tmp_path, spec):
    scoring = _screen(tmp_path, spec)
    assert scoring.screen_scorer == spec
    assert scoring.scorer_problems() == {}


@pytest.mark.parametrize(
    ("spec", "needle"),
    [
        ("openrouter:jev:typesafe/jev-1.13", 'use "jev:typesafe/jev-1.13"'),
        ("openroute:jev:typesafe/jev-1.13", 'use "jev:typesafe/jev-1.13"'),
        ("openroute:openai/gpt-oss-120b", "did you mean 'openrouter'"),
        ("jev:", "no model"),
        ("claude-haiku-4-5", 'write "anthropic:claude-haiku-4-5"'),
    ],
)
def test_scorer_spec_reported_not_fatal(tmp_path, spec, needle):
    scoring = _screen(tmp_path, spec)  # load_settings must not raise
    msg = scoring.scorer_problems()["screen_scorer"]
    assert needle in msg
    assert f'screen_scorer "{spec}"' in msg


def test_scorer_error_for_user_value_mentions_key(tmp_path):
    msg = _screen(tmp_path, "openrouter:jev:typesafe/jev-1.13").scorer_problems()["screen_scorer"]
    assert "OPENROUTER_API_KEY" in msg


def test_deep_scorer_must_be_anthropic(tmp_path):
    problems = _screen(tmp_path, "local:x", deep="jev:typesafe/jev-1.13").scorer_problems()
    assert "screen_scorer" not in problems
    assert "anthropic:<model>" in problems["deep_scorer"]


def test_documented_scorer_examples_parse():
    import re

    from jobhunter.config import SCORER_PROVIDERS, check_scorer_spec

    root = Path(__file__).resolve().parent.parent
    for name in ("config.example.toml", "src/jobhunter/console/help/scorers.md"):
        text = (root / name).read_text()
        specs = re.findall(r'(screen|deep)_scorer\s*=\s*"([^"]+)"', text)
        assert specs, name
        for key, spec in specs:
            check_scorer_spec(spec, name, anthropic_only=key == "deep")
        if name.endswith("scorers.md"):  # the README section moved here (019 S11b)
            tokens = re.findall(r"`([a-z-]+):<", text)
        else:
            tokens = re.findall(r"^#\s+([a-z-]+):<", text, re.M)
        assert set(tokens) == set(SCORER_PROVIDERS), name


def test_cli_scorer_override_rejected(tmp_path):
    from typer.testing import CliRunner

    from jobhunter.cli import app

    res = CliRunner().invoke(app, ["score", "--submit", "--scorer", "openrouter:jev:x"])
    assert res.exit_code == 2
    assert 'use "jev:x"' in res.output
    assert "Traceback" not in res.output


def test_cli_bad_configured_scorer_is_one_line(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from jobhunter.cli import app

    cfg = _write(
        tmp_path / "c.toml", '[scoring]\nscreen_scorer = "openroute:jev:typesafe/jev-1.13"\n'
    )
    monkeypatch.setenv(CONFIG_ENV_VAR, str(cfg))
    res = CliRunner().invoke(app, ["score", "--submit"])
    assert res.exit_code == 2
    assert 'use "jev:typesafe/jev-1.13"' in res.output
    assert "Traceback" not in res.output
    assert len(res.output.strip().splitlines()) == 1


def test_cli_pending_modes_ignore_a_bad_screen_scorer(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from typer.testing import CliRunner

    from jobhunter import cli
    from jobhunter.scoring import rescore, screen

    cfg = _write(tmp_path / "c.toml", '[scoring]\nscreen_scorer = "openroute:x"\n')
    monkeypatch.setenv(CONFIG_ENV_VAR, str(cfg))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "unused-test-key")
    monkeypatch.setattr("jobhunter.scoring.profile.load_profile", lambda *a, **k: SimpleNamespace())
    calls = []
    monkeypatch.setattr(screen, "collect_pending", lambda *a, **k: calls.append("collect") or {})
    monkeypatch.setattr(rescore, "drain_pending", lambda *a, **k: calls.append("drain") or {})
    for flag in ("--collect-pending", "--rescore-pending"):
        res = CliRunner().invoke(cli.app, ["score", flag])
        assert "scorer error" not in res.output, res.output
        assert res.exit_code == 0, res.output
    assert calls == ["collect", "drain"]
    res = CliRunner().invoke(cli.app, ["score", "--rescore-pending", "--scorer", "jev:"])
    assert res.exit_code == 2 and "scorer error" in res.output
