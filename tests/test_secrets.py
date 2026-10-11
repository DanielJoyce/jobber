"""*_FILE secrets, validation and `jobhunter secrets status` (specs/018 C2). Fake values only."""

from __future__ import annotations

import logging
import os
import sqlite3

import httpx
import pytest
from typer.testing import CliRunner

from jobhunter import secrets
from jobhunter.cli import app
from jobhunter.config import ENV_FILE_VAR, OpenRouter
from jobhunter.scoring.decisions import DecisionsScorer
from jobhunter.scoring.scorers import ScorerError

runner = CliRunner()
FAKE = "fake-key-9f3a1c77d2e0b8"
ALL_NAMES = (*secrets.DEFAULT_NAMES,)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ALL_NAMES:
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(f"{name}_FILE", raising=False)


def status() -> tuple[int, str, str]:
    r = runner.invoke(app, ["secrets", "status"])
    return r.exit_code, r.stdout, r.stderr


def line_for(out: str, name: str) -> str:
    return next(ln for ln in out.splitlines() if ln.startswith(name + " "))


# ---- file resolution ----------------------------------------------------------------


def test_file_secret_is_resolved_into_the_environment(tmp_path, monkeypatch):
    f = tmp_path / "k"
    f.write_text(FAKE + "\n")
    monkeypatch.setenv("OPENROUTER_API_KEY_FILE", str(f))
    code, out, _ = status()
    assert code == 0
    assert os.environ["OPENROUTER_API_KEY"] == FAKE
    assert f"set (file {f})" in line_for(out, "OPENROUTER_API_KEY")
    assert FAKE not in out


@pytest.mark.parametrize("suffix", ["\r\n", " \n", "\n\n", "\t", "\n", "  \r\n\t"])
def test_trailing_whitespace_is_stripped_and_value_never_printed(
    tmp_path, monkeypatch, caplog, suffix
):
    f = tmp_path / "k"
    f.write_bytes((FAKE + suffix).encode())
    monkeypatch.setenv("ANTHROPIC_API_KEY_FILE", str(f))
    with caplog.at_level(logging.DEBUG):
        code, out, err = status()
    assert code == 0 and os.environ["ANTHROPIC_API_KEY"] == FAKE
    for text in (out, err, caplog.text):
        assert FAKE not in text and repr(FAKE) not in text


@pytest.mark.parametrize("suffix", ["\r\n", " \n", "\n\n", "\t"])
def test_trailing_whitespace_in_the_env_is_stripped_too(monkeypatch, suffix):
    monkeypatch.setenv("OPENAI_COMPAT_API_KEY", FAKE + suffix)
    code, out, err = status()
    assert code == 0 and os.environ["OPENAI_COMPAT_API_KEY"] == FAKE
    assert "set (env)" in line_for(out, "OPENAI_COMPAT_API_KEY")
    assert FAKE not in out + err


@pytest.mark.parametrize("bad", [FAKE + " tail", FAKE + "\nextra", "a\tb" + FAKE, FAKE + "\x07x"])
def test_inner_whitespace_or_control_chars_are_invalid_and_dropped(
    tmp_path, monkeypatch, caplog, bad
):
    monkeypatch.setenv("LLAMA_API_KEY", bad)
    f = tmp_path / "k"
    f.write_text(bad)
    monkeypatch.setenv("USAJOBS_API_KEY_FILE", str(f))
    with caplog.at_level(logging.DEBUG):
        code, out, err = status()
    assert code == 0
    assert "LLAMA_API_KEY" not in os.environ and "USAJOBS_API_KEY" not in os.environ
    assert "invalid (contains" in line_for(out, "LLAMA_API_KEY")
    assert "invalid (contains" in line_for(out, "USAJOBS_API_KEY")
    assert "LLAMA_API_KEY ignored" in err and "USAJOBS_API_KEY ignored" in err
    for text in (out, err, caplog.text):
        assert FAKE not in text and "tail" not in text and "extra" not in text


def test_both_name_and_file_in_the_real_env_is_a_conflict(tmp_path, monkeypatch):
    f = tmp_path / "k"
    f.write_text(FAKE)
    monkeypatch.setenv("OPENROUTER_API_KEY_FILE", str(f))
    monkeypatch.setenv("OPENROUTER_API_KEY", "other-fake-value")
    code, out, err = status()
    assert code == 0
    assert "OPENROUTER_API_KEY" not in os.environ  # neither is silently preferred
    assert "invalid (OPENROUTER_API_KEY and OPENROUTER_API_KEY_FILE are both set)" in out
    assert "other-fake-value" not in out + err and FAKE not in out + err


@pytest.mark.parametrize(
    ("setup", "reason"),
    [
        ("missing", "file is missing"),
        ("empty", "file is empty"),
        ("blank", "file is empty"),
        ("big", "file is larger than 64 KiB"),
        ("binary", "file is not valid UTF-8"),
        ("dir", "file is unreadable"),
    ],
)
def test_bad_files_are_invalid_and_name_the_path(tmp_path, monkeypatch, setup, reason):
    f = tmp_path / "k"
    if setup == "empty":
        f.write_text("")
    elif setup == "blank":
        f.write_text(" \r\n")
    elif setup == "big":
        f.write_text("a" * (64 * 1024 + 1))
    elif setup == "binary":
        f.write_bytes(b"\xff\xfe\x00")
    elif setup == "dir":
        f.mkdir()
    monkeypatch.setenv("ANTHROPIC_API_KEY_FILE", str(f))
    code, out, err = status()
    assert code == 0 and "ANTHROPIC_API_KEY" not in os.environ
    assert f"invalid ({reason})" in line_for(out, "ANTHROPIC_API_KEY")
    assert str(f) in err


def test_exactly_64_kib_is_accepted(tmp_path, monkeypatch):
    f = tmp_path / "k"
    f.write_text("a" * (64 * 1024))
    monkeypatch.setenv("ANTHROPIC_API_KEY_FILE", str(f))
    status()
    assert len(os.environ["ANTHROPIC_API_KEY"]) == 64 * 1024


def test_one_invalid_secret_does_not_stop_the_rest(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "has space")
    monkeypatch.setenv("USAJOBS_EMAIL", "<you>@example.com")
    code, out, _ = status()
    assert code == 0
    assert "invalid" in line_for(out, "ANTHROPIC_API_KEY")
    assert "set (env)" in line_for(out, "USAJOBS_EMAIL")
    assert "unset" in line_for(out, "LLAMA_API_KEY")
    assert os.environ["USAJOBS_EMAIL"] == "<you>@example.com"


def test_other_commands_continue_after_an_invalid_secret(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "has space")
    r = runner.invoke(app, ["paths"])
    assert r.exit_code == 0
    assert "ANTHROPIC_API_KEY ignored" in r.stderr


# ---- .env interplay -----------------------------------------------------------------


def test_synthetic_dotenv_setup_keeps_working_unchanged(tmp_path, monkeypatch):
    # Shaped like a real ~/.env: set keys, empty placeholders, comments, quotes.
    env = tmp_path / ".env"
    env.write_text(
        "# keys\n"
        f"ANTHROPIC_API_KEY={FAKE}\n"
        "OPENROUTER_API_KEY=\n"
        'USAJOBS_EMAIL="<you>@example.com"\n'
        f"USAJOBS_API_KEY = {FAKE}x  \n"
        "UNRELATED=keep me as is\n"
    )
    env.chmod(0o600)
    monkeypatch.setenv(ENV_FILE_VAR, str(env))
    monkeypatch.delenv("UNRELATED", raising=False)
    code, out, err = status()
    assert code == 0 and err == ""  # no warnings at all
    assert os.environ["ANTHROPIC_API_KEY"] == FAKE
    assert os.environ["USAJOBS_EMAIL"] == "<you>@example.com"
    assert os.environ["USAJOBS_API_KEY"] == FAKE + "x"
    assert os.environ["UNRELATED"] == "keep me as is"
    assert "OPENROUTER_API_KEY" not in os.environ  # an empty placeholder is unset
    assert f"set (.env {env})" in line_for(out, "ANTHROPIC_API_KEY")
    assert "unset" in line_for(out, "OPENROUTER_API_KEY")
    assert "chmod" not in out
    monkeypatch.delenv("UNRELATED")


def test_dotenv_that_others_can_read_gets_a_chmod_hint(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text(f"ANTHROPIC_API_KEY={FAKE}\n")
    env.chmod(0o644)
    monkeypatch.setenv(ENV_FILE_VAR, str(env))
    _, out, _ = status()
    assert f"chmod 600 {env}" in out and FAKE not in out


def test_file_wins_over_dotenv_with_a_warning_naming_the_dotenv(tmp_path, monkeypatch):
    env = tmp_path / "e.env"
    env.write_text("OPENROUTER_API_KEY=from-dotenv-fake\n")
    f = tmp_path / "k"
    f.write_text(FAKE)
    monkeypatch.setenv(ENV_FILE_VAR, str(env))
    monkeypatch.setenv("OPENROUTER_API_KEY_FILE", str(f))
    _, out, err = status()
    assert os.environ["OPENROUTER_API_KEY"] == FAKE
    assert str(env) in err and "OPENROUTER_API_KEY_FILE" in err
    assert f"set (file {f})" in line_for(out, "OPENROUTER_API_KEY")
    assert "from-dotenv-fake" not in out + err


def test_invalid_file_does_not_fall_back_to_the_dotenv_value(tmp_path, monkeypatch):
    env = tmp_path / "e.env"
    env.write_text("OPENROUTER_API_KEY=from-dotenv-fake\n")
    monkeypatch.setenv(ENV_FILE_VAR, str(env))
    monkeypatch.setenv("OPENROUTER_API_KEY_FILE", str(tmp_path / "nope"))
    status()
    assert "OPENROUTER_API_KEY" not in os.environ


def test_real_env_beats_dotenv(tmp_path, monkeypatch):
    env = tmp_path / "e.env"
    env.write_text("OPENROUTER_API_KEY=from-dotenv-fake\n")
    monkeypatch.setenv(ENV_FILE_VAR, str(env))
    monkeypatch.setenv("OPENROUTER_API_KEY", FAKE)
    _, out, _ = status()
    assert os.environ["OPENROUTER_API_KEY"] == FAKE
    assert "set (env)" in line_for(out, "OPENROUTER_API_KEY")


# ---- known names --------------------------------------------------------------------


def test_known_names_include_config_and_registry_names(tmp_path, monkeypatch):
    cfg = tmp_path / "config.toml"
    cfg.write_text('[scoring.openrouter]\napi_key_env = "MY_ROUTER_KEY"\n')
    monkeypatch.setenv("JOBHUNTER_CONFIG", str(cfg))
    monkeypatch.setenv("MY_ROUTER_KEY", FAKE + " x")  # invalid, via a renamed variable
    _, out, _ = status()
    assert "invalid" in line_for(out, "MY_ROUTER_KEY")
    assert "USAJOBS_API_KEY" in secrets.registry_auth_names()
    assert "USAJOBS_EMAIL" in secrets.registry_auth_names()


# ---- the fake value appears nowhere -------------------------------------------------


def test_invalid_key_never_reaches_a_header_or_an_error_or_the_db(tmp_path, monkeypatch, caplog):
    """The httpx leak path: a key with a stray CR/space/tab ends up in the exception text, which
    scorers log or store. After validation the key is simply missing, so nothing is sent and
    nothing contains it."""
    bad = FAKE + " \r\nzz"
    monkeypatch.setenv("OPENROUTER_API_KEY", bad)
    sent: list[httpx.Request] = []

    def handler(request):
        sent.append(request)
        return httpx.Response(200, json={"answers": {}})

    with caplog.at_level(logging.DEBUG):
        code, out, err = status()
        client = DecisionsScorer(
            "m", OpenRouter(), client=httpx.Client(transport=httpx.MockTransport(handler))
        )
        with pytest.raises(ScorerError) as exc:
            client.decide({"x": 1})
    assert code == 0 and not sent  # nothing was sent
    stored = sqlite3.connect(":memory:")  # what the rescore path would store: str(exc)
    stored.execute("create table rescore_request(error text)")
    stored.execute("insert into rescore_request values (?)", (str(exc.value),))
    rows = stored.execute("select error from rescore_request").fetchall()
    for text in (out, err, caplog.text, str(exc.value), repr(rows)):
        assert FAKE not in text and "zz" not in text
    assert "OPENROUTER_API_KEY" in str(exc.value)  # names the variable only


def test_scorer_headers_refuse_when_the_key_was_dropped(monkeypatch):
    from jobhunter.config import OpenAICompat
    from jobhunter.scoring.scorers import OpenAICompatScorer

    monkeypatch.setenv("OPENAI_COMPAT_API_KEY", FAKE + "\t x")
    status()
    scorer = OpenAICompatScorer.__new__(OpenAICompatScorer)
    scorer.config = OpenAICompat()
    scorer._env = None
    scorer.config_section = "scoring.openai_compat"
    with pytest.raises(ScorerError) as exc:
        scorer._headers()
    assert FAKE not in str(exc.value)
