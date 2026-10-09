from typer.testing import CliRunner

from jobhunter.cli import app

runner = CliRunner()


def test_help():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for cmd in ("run", "score", "console", "eval", "sources", "mail", "applylinks"):
        assert cmd in result.output


def test_run_dry_run():
    result = runner.invoke(app, ["run", "--dry-run"])
    assert result.exit_code == 0
    assert "Run plan: no sources enabled" in result.output


def test_run_not_implemented():
    result = runner.invoke(app, ["run"])
    assert result.exit_code == 0
    assert "not implemented yet" in result.output


def test_subcommand_stubs():
    for args in (["score"], ["sources", "list"], ["mail", "sync"], ["applylinks", "unknown"]):
        result = runner.invoke(app, args)
        assert result.exit_code == 0, args
        assert "not implemented yet" in result.output
