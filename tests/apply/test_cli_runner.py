"""The Claude Code CLI runner (specs/017 "CLI runner") against a fake ``claude`` on PATH.

The fake records its argv, cwd, environment and stdin, then replays the recorded-shape
stream-json transcript (tests/fixtures/claude_cli). No test runs the real binary: the autouse
guard in tests/conftest.py refuses to launch anything but the fake.
"""

from __future__ import annotations

import os
import shutil
import time

import pytest

from jobhunter.apply import cli_runner

SCHEMA = {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]}
OUT = {"x": "hello"}


def _run(fake, tmp_path, **kw):
    cwd = cli_runner.work_dir(tmp_path / "cache")
    args = dict(
        binary=str(fake.path),
        cwd=cwd,
        model="opus",
        system_prompt="SYSTEM PROMPT",
        schema=SCHEMA,
        user_message="L1 Synthetic Person\nposting text",
        effort="medium",
        on_overage=lambda: pytest.fail("no overage expected"),
    )
    args.update(kw)
    return cli_runner.run(**args)


def test_exact_argv_cwd_stdin_and_parsed_result(fake_claude, claude_stream, tmp_path):
    fake_claude.set([claude_stream(OUT)])
    res = _run(fake_claude, tmp_path)
    (call,) = fake_claude.calls()
    assert call["argv"] == [
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--model",
        "opus",
        "--effort",
        "medium",
        "--system-prompt",
        "SYSTEM PROMPT",
        "--json-schema",
        '{"type":"object","properties":{"x":{"type":"string"}},"required":["x"]}',
        "--tools",
        "",
        "--strict-mcp-config",
        "--setting-sources",
        "",
        "--safe-mode",
        "--disable-slash-commands",
        "--no-session-persistence",
        "--max-budget-usd",
        "1.00",
    ]
    assert call["cwd"] == str(tmp_path / "cache" / "apply-cli")
    assert call["stdin"] == "L1 Synthetic Person\nposting text"
    assert res.structured == OUT
    assert res.model == "claude-opus-5"
    assert (res.input_tokens, res.output_tokens) == (8123, 3412)
    assert res.total_cost_usd == pytest.approx(0.1259)
    assert res.overage is False


def test_haiku_call_has_no_effort_flag(fake_claude, claude_stream, tmp_path):
    fake_claude.set([claude_stream(OUT)])
    _run(fake_claude, tmp_path, model="haiku", effort=None)
    argv = fake_claude.calls()[0]["argv"]
    assert "--effort" not in argv
    assert argv[argv.index("--model") + 1] == "haiku"


def test_environment_is_built_not_inherited(fake_claude, claude_stream, tmp_path, monkeypatch):
    for name in cli_runner.NEVER_PASSED:
        monkeypatch.setenv(name, "must-not-pass")
    monkeypatch.setenv("SOME_OTHER_SECRET", "must-not-pass")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/tmp/claude-config")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat-synthetic")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:3128")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("LANG", "C.UTF-8")
    monkeypatch.setenv("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "0")
    fake_claude.set([claude_stream(OUT)])
    _run(fake_claude, tmp_path)
    env = fake_claude.calls()[0]["env"]
    for name in (*cli_runner.NEVER_PASSED, "SOME_OTHER_SECRET"):
        assert name not in env, name
    assert env["CLAUDE_CONFIG_DIR"] == "/tmp/claude-config"
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat-synthetic"
    assert env["HTTPS_PROXY"] == "http://127.0.0.1:3128"
    assert env["XDG_CONFIG_HOME"] == str(tmp_path / "xdg")
    assert env["HOME"] == os.environ["HOME"]
    assert env["PATH"] == os.environ["PATH"]
    assert env["LANG"] == "C.UTF-8"
    assert env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
    # A python child adds nothing of its own except these; anything else leaked through.
    extra = set(env) - set(cli_runner.child_env()) - {"LC_CTYPE", "PWD", "SHLVL", "_"}
    assert extra == set()


@pytest.mark.parametrize(
    ("init", "words"),
    [
        ({"apiKeySource": "ANTHROPIC_API_KEY"}, "API key"),
        ({"tools": ["StructuredOutput", "Bash"]}, "tools"),
        ({"tools": []}, "tools"),
        ({"mcp_servers": [{"name": "x", "status": "connected"}]}, "MCP"),
    ],
)
def test_bad_init_line_kills_the_child_before_anything_else(
    fake_claude, claude_stream, tmp_path, init, words
):
    run = claude_stream(OUT, init=init)
    # After the init line the fake waits, then marks that it got further: it never should.
    run["steps"][1:1] = [{"sleep": 3}, {"touch": "after_init"}]
    fake_claude.set([run])
    start = time.monotonic()
    with pytest.raises(cli_runner.CliFailure, match=words) as exc:
        _run(fake_claude, tmp_path)
    assert time.monotonic() - start < 2.5
    assert exc.value.turn_off and exc.value.killed
    time.sleep(0.2)
    assert not fake_claude.reached("after_init")


def test_lines_before_init_are_refused(fake_claude, claude_stream, tmp_path):
    run = claude_stream(OUT)
    run["steps"] = [s for s in run["steps"] if s["line"]["type"] != "system"]
    fake_claude.set([run])
    with pytest.raises(cli_runner.CliFailure, match="init") as exc:
        _run(fake_claude, tmp_path)
    assert exc.value.turn_off


def test_overage_asks_the_caller_and_continues_when_allowed(fake_claude, claude_stream, tmp_path):
    asked = []
    fake_claude.set([claude_stream(OUT, overage=True)])
    res = _run(fake_claude, tmp_path, on_overage=lambda: asked.append(1) or True)
    assert asked == [1]
    assert res.overage is True


def test_overage_over_the_cap_kills_the_child(fake_claude, claude_stream, tmp_path):
    run = claude_stream(OUT, overage=True)
    run["steps"][2:2] = [{"sleep": 3}, {"touch": "after_overage"}]
    fake_claude.set([run])
    with pytest.raises(cli_runner.CliFailure, match="paid extra usage") as exc:
        _run(fake_claude, tmp_path, on_overage=lambda: False)
    assert exc.value.killed and not exc.value.turn_off
    time.sleep(0.2)
    assert not fake_claude.reached("after_overage")


def test_non_zero_exit_without_result_fails(fake_claude, tmp_path):
    fake_claude.set([{"steps": [], "exit": 2, "stderr": "Error: not logged in"}])
    with pytest.raises(cli_runner.CliFailure, match=r"code 2.*not logged in"):
        _run(fake_claude, tmp_path)


def test_error_result_fails_but_carries_usage(fake_claude, claude_stream, tmp_path):
    fake_claude.set([claude_stream(None, result={"is_error": True, "subtype": "error_max_turns"})])
    with pytest.raises(cli_runner.CliFailure, match="error_max_turns") as exc:
        _run(fake_claude, tmp_path)
    assert exc.value.result is not None and exc.value.result.output_tokens == 3412


def test_missing_structured_output_fails(fake_claude, claude_stream, tmp_path):
    fake_claude.set([claude_stream(None)])
    with pytest.raises(cli_runner.CliFailure, match="no structured output"):
        _run(fake_claude, tmp_path)


def test_timeout_kills_the_child(fake_claude, claude_stream, tmp_path):
    run = claude_stream(OUT)
    run["steps"].insert(1, {"sleep": 5})
    fake_claude.set([run])
    start = time.monotonic()
    with pytest.raises(cli_runner.CliFailure, match="timed out") as exc:
        _run(fake_claude, tmp_path, timeout=0.8)
    assert time.monotonic() - start < 4
    assert exc.value.killed


def test_missing_binary(fake_claude, tmp_path):
    with pytest.raises(cli_runner.CliFailure, match="not installed"):
        cli_runner.run(
            binary=str(fake_claude.dir / "gone" / "claude"),
            cwd=cli_runner.work_dir(tmp_path),
            model="opus",
            system_prompt="s",
            schema=SCHEMA,
            user_message="u",
            effort="medium",
            on_overage=lambda: True,
        )


def test_working_directory_must_be_empty(fake_claude, claude_stream, tmp_path):
    fake_claude.set([claude_stream(OUT)])
    (cli_runner.work_dir(tmp_path / "cache") / "stray.txt").write_text("x")
    with pytest.raises(cli_runner.CliFailure, match="not empty"):
        _run(fake_claude, tmp_path)
    assert fake_claude.calls() == []


def test_auth_status_must_be_a_claude_ai_login(fake_claude, tmp_path):
    fake_claude.set(auth={"loggedIn": True, "authMethod": "api_key", "apiProvider": "firstParty"})
    with pytest.raises(cli_runner.CliFailure, match=r"claude\.ai") as exc:
        cli_runner.check_auth(str(fake_claude.path), cli_runner.work_dir(tmp_path))
    assert exc.value.turn_off


def test_auth_status_third_party_provider_is_refused(fake_claude, tmp_path):
    fake_claude.set(auth={"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "bedrock"})
    with pytest.raises(cli_runner.CliFailure) as exc:
        cli_runner.check_auth(str(fake_claude.path), cli_runner.work_dir(tmp_path))
    assert exc.value.turn_off


def test_auth_status_is_checked_once_per_process(fake_claude, tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-pass")
    cwd = cli_runner.work_dir(tmp_path)
    cli_runner.check_auth(str(fake_claude.path), cwd)
    cli_runner.check_auth(str(fake_claude.path), cwd)
    calls = fake_claude.calls(auth=True)
    assert len(calls) == 1
    assert calls[0]["argv"] == ["auth", "status", "--json"]
    assert "ANTHROPIC_API_KEY" not in calls[0]["env"]


# ─── the guard itself ───────────────────────────────────────────────────────


def test_guard_hides_any_real_claude_on_path(tmp_path, monkeypatch):
    real_dir = tmp_path / "bin"
    real_dir.mkdir()
    exe = real_dir / "claude"
    exe.write_text("#!/bin/sh\ntouch ran\n")
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", f"{real_dir}{os.pathsep}{os.environ['PATH']}")
    assert shutil.which("claude") == str(exe)
    assert cli_runner.find_binary() is None


def test_guard_refuses_to_launch_anything_but_the_fake(tmp_path):
    from conftest import RealClaudeBlocked

    exe = tmp_path / "claude"
    exe.write_text("#!/bin/sh\ntouch ran\n")
    exe.chmod(0o755)
    with pytest.raises(RealClaudeBlocked):
        cli_runner._spawn([str(exe), "-p"], cwd=str(tmp_path))
    with pytest.raises(RealClaudeBlocked):
        cli_runner.check_auth(str(exe), tmp_path)
    assert not (tmp_path / "ran").exists()


def test_guard_lets_the_fake_run(fake_claude):
    assert cli_runner.find_binary() == str(fake_claude.path)
