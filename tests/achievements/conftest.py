"""Fixtures for achievements tests; builders live in ``achv_world``."""

from __future__ import annotations

from pathlib import Path

import pytest
from achv_world import World

from jobhunter.core import db


@pytest.fixture(autouse=True)
def _no_anthropic(monkeypatch: pytest.MonkeyPatch) -> None:
    """specs/021 Testing: constructing an Anthropic client fails the test."""
    import anthropic

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("achievements tests must never construct an Anthropic client")

    monkeypatch.setattr(anthropic, "Anthropic", refuse)
    monkeypatch.setattr(anthropic, "AsyncAnthropic", refuse)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "a.db"
    c = db.connect(path)
    db.migrate(c)
    c.close()
    return path


@pytest.fixture
def world(db_path: Path):
    conn = db.connect(db_path)
    yield World(conn, db_path)
    conn.close()
