from __future__ import annotations

import os

from jobhunter.config import ENV_FILE_VAR, load_env_files


def test_env_file_loads_without_overriding_real_env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".env").write_text("JH_TEST_A=from-home\nJH_TEST_B=from-home\n")
    explicit = tmp_path / "explicit.env"
    explicit.write_text("JH_TEST_A=from-explicit\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(ENV_FILE_VAR, str(explicit))
    monkeypatch.setenv("JH_TEST_B", "from-real-env")
    monkeypatch.delenv("JH_TEST_A", raising=False)

    loaded = load_env_files()

    assert explicit in loaded and home / ".env" in loaded
    assert os.environ["JH_TEST_A"] == "from-explicit"  # earlier file wins
    assert os.environ["JH_TEST_B"] == "from-real-env"  # real env always wins
    monkeypatch.delenv("JH_TEST_A")


def test_missing_env_files_are_fine(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(ENV_FILE_VAR, raising=False)
    assert load_env_files() == []
