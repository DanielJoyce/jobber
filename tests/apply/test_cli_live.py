"""Live smoke test of the real Claude Code CLI runner (specs/017 "CLI runner").

OPT-IN ONLY. It makes two tiny model calls on the user's Claude subscription, so it is marked
``live``: deselected by default (pyproject ``-m 'not e2e and not live'``) and skipped unless
``JOBHUNTER_LIVE_TESTS=1``. Run it deliberately with::

    JOBHUNTER_LIVE_TESTS=1 uv run pytest -m live tests/apply/test_cli_live.py

It checks what the offline tests can only assume: with ``--safe-mode``, ``--tools ''`` and
``--json-schema`` the subscription login works, the init line passes the runner's checks
(``apiKeySource`` none, ``tools`` exactly ``["StructuredOutput"]``, no MCP servers), the result
carries a validated ``structured_output`` with usage, and ``~/.claude.json`` gains at most one
project entry (the fixed working directory), not one per call.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from jobhunter.apply import cli_runner

pytestmark = pytest.mark.live

SCHEMA = {
    "type": "object",
    "properties": {"word": {"type": "string"}},
    "required": ["word"],
    "additionalProperties": False,
}


def _projects(home: Path) -> int:
    path = home / ".claude.json"
    try:
        return len(json.loads(path.read_text(encoding="utf-8")).get("projects") or {})
    except (OSError, ValueError):
        return 0


def test_real_cli_envelope_on_the_subscription(monkeypatch, tmp_path):
    import pwd

    _REAL_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)

    # The suite points HOME at a temp dir; the real login lives in the real home.
    monkeypatch.setenv("HOME", str(_REAL_HOME))
    binary = cli_runner.find_binary()
    assert binary, "claude is not on PATH"
    cwd = cli_runner.work_dir(tmp_path / "cache")
    cli_runner.check_auth(binary, cwd)
    before = _projects(_REAL_HOME)
    results = []
    for _ in range(2):
        results.append(
            cli_runner.run(
                binary=binary,
                cwd=cwd,
                model="haiku",
                system_prompt="Answer with one lowercase word.",
                schema=SCHEMA,
                user_message="Say the word: ready",
                effort=None,
                on_overage=lambda: False,  # never spend paid extra usage in a smoke test
                environ=dict(os.environ),
            )
        )
    for res in results:
        assert isinstance(res.structured, dict) and isinstance(res.structured.get("word"), str)
        assert res.model
        assert res.output_tokens > 0
        assert res.overage is False
    assert list(cwd.iterdir()) == []
    assert _projects(_REAL_HOME) - before <= 1
