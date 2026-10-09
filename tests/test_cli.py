from typer.testing import CliRunner

from jobhunter.cli import app

runner = CliRunner()


def test_help():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for cmd in ("run", "score", "console", "eval", "sources", "mail", "applylinks"):
        assert cmd in result.output


def test_run_dry_run_uses_packaged_registry_without_network(tmp_path, monkeypatch):
    _config(tmp_path, monkeypatch)
    result = runner.invoke(app, ["run", "--dry-run", "--stage", "list,resolve"])
    assert result.exit_code == 0, result.output
    assert "Run plan" in result.output
    assert "us-usajobs" in result.output


def test_run_dry_run_state_filter(tmp_path, monkeypatch):
    _config(tmp_path, monkeypatch)
    result = runner.invoke(app, ["run", "--dry-run", "--state", "WA"])
    assert result.exit_code == 0
    assert "us-usajobs" not in result.output


def test_run_bad_stage():
    result = runner.invoke(app, ["run", "--dry-run", "--stage", "bogus"])
    assert result.exit_code == 2


def test_mail_sync_without_token_skips_cleanly(monkeypatch):
    from jobhunter.mail import auth

    monkeypatch.setattr(auth, "load_token", lambda: None)  # never the real keyring
    result = runner.invoke(app, ["mail", "sync"])
    assert result.exit_code == 0
    assert "mail sync skipped: no Gmail token" in result.output


def test_score_requires_one_mode():
    result = runner.invoke(app, ["score"])
    assert result.exit_code == 2
    assert "--submit" in result.output


def _config(tmp_path, monkeypatch):
    db_path = tmp_path / "jh.db"
    cfg = tmp_path / "config.toml"
    # profile_dir too: the default is the cwd's profile/, which is the developer's real one.
    cfg.write_text(f'[paths]\ndb_path = "{db_path}"\nprofile_dir = "{tmp_path / "profile"}"\n')
    monkeypatch.setenv("JOBHUNTER_CONFIG", str(cfg))
    return db_path


def test_applylinks_unknown_without_db(tmp_path, monkeypatch):
    db_path = _config(tmp_path, monkeypatch)
    result = runner.invoke(app, ["applylinks", "unknown"])
    assert result.exit_code == 0
    assert "no apply links resolved yet" in result.output
    assert not db_path.exists()


def test_applylinks_unknown_lists_hosts(tmp_path, monkeypatch):
    from jobhunter.core import db

    db_path = _config(tmp_path, monkeypatch)
    conn = db.connect(db_path)
    db.migrate(conn)
    for i, host in enumerate(["a.example", "a.example", "b.example"], start=1):
        conn.execute(
            "INSERT INTO job_group (id, member_count, method, created_at) "
            "VALUES (?, 1, 'manual', '2026-10-09T00:00:00+00:00')",
            (i,),
        )
        conn.execute(
            "INSERT INTO apply_link (job_group_id, start_url, chain, ats, employer_host, status, "
            "resolved_at) VALUES (?, 'https://x.example/', '[]', 'unknown', ?, 'live', "
            "'2026-10-09T00:00:00+00:00')",
            (i, host),
        )
    conn.close()
    result = runner.invoke(app, ["applylinks", "unknown", "--top", "5"])
    assert result.exit_code == 0
    lines = result.output.strip().splitlines()
    assert lines[1].split() == ["2", "a.example"]
    assert lines[2].split() == ["1", "b.example"]
