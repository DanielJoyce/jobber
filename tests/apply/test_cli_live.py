"""Live smoke test of the real Claude Code CLI runner (specs/017 "CLI runner").

OPT-IN ONLY. It makes two tiny model calls on the user's Claude subscription, so it is marked
``live``: deselected by default (pyproject ``-m 'not e2e and not live'``) and skipped unless
``JOBHUNTER_LIVE_TESTS=1``. Run it deliberately with::

    JOBHUNTER_LIVE_TESTS=1 uv run pytest -m live tests/apply/test_cli_live.py

It checks what the offline tests can only assume: with ``--safe-mode``, ``--tools ''`` and
``--json-schema`` the subscription login works, the init line passes the runner's checks
(``apiKeySource`` none, ``tools`` exactly ``["StructuredOutput"]``, no MCP servers), the result
carries a validated ``structured_output`` with usage, ``~/.claude.json`` gains at most one
project entry (the fixed working directory), not one per call, and neither a CLAUDE.md above
the working directory nor a file the prompt @-mentions reaches the model (canary codewords).
It cannot see the account email and date the CLI adds to every request (recorded on d41f324).
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
    "properties": {"word": {"type": "string"}, "codewords": {"type": "string"}},
    "required": ["word", "codewords"],
    "additionalProperties": False,
}
MD_CANARY = "PINEAPPLE-CLAUDEMD-7731"
AT_CANARY = "MANGO-ATMENTION-4419"


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
    # A project CLAUDE.md above the working directory, and a file the prompt @-mentions.
    (tmp_path / "cache" / "CLAUDE.md").write_text(f"The codeword is {MD_CANARY}.\n")
    canary = tmp_path / "canary.txt"
    canary.write_text(f"The codeword is {AT_CANARY}.\n")
    cli_runner.check_auth(binary, cwd)
    before = _projects(_REAL_HOME)
    results = []
    for _ in range(2):
        results.append(
            cli_runner.run(
                binary=binary,
                cwd=cwd,
                model="haiku",
                system_prompt=(
                    "Answer with one lowercase word. In codewords, list every codeword that "
                    "appears anywhere in your context, or 'none'."
                ),
                schema=SCHEMA,
                user_message=f"Say the word: ready. See @{canary} for the codeword.",
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
        seen = json.dumps(res.structured)
        assert MD_CANARY not in seen and AT_CANARY not in seen
    assert list(cwd.iterdir()) == []
    assert _projects(_REAL_HOME) - before <= 1
