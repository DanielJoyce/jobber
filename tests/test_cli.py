from typer.testing import CliRunner

from jobhunter.cli import app

runner = CliRunner()


def test_help():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for cmd in ("run", "score", "console", "eval", "sources", "mail", "applylinks"):
        assert cmd in result.output


def test_run_dry_run_uses_packaged_registry_without_network():
    result = runner.invoke(app, ["run", "--dry-run", "--stage", "list,resolve"])
    assert result.exit_code == 0, result.output
    assert "Run plan" in result.output
    assert "us-usajobs" in result.output


def test_run_dry_run_state_filter():
    result = runner.invoke(app, ["run", "--dry-run", "--state", "WA"])
    assert result.exit_code == 0
    assert "us-usajobs" not in result.output


def test_run_bad_stage():
    result = runner.invoke(app, ["run", "--dry-run", "--stage", "bogus"])
    assert result.exit_code == 2


def test_subcommand_stubs():
    for args in (["mail", "sync"], ["applylinks", "unknown"]):
        result = runner.invoke(app, args)
        assert result.exit_code == 0, args
        assert "not implemented yet" in result.output


def test_score_requires_one_mode():
    result = runner.invoke(app, ["score"])
    assert result.exit_code == 2
    assert "--submit" in result.output
