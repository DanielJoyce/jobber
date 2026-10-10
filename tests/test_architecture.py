"""Only jobhunter.core.fetch may touch the network (specs/003#adapter-contract)."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "jobhunter"
GUARDED_PACKAGES = ["sources", "pipeline", "scoring", "mail", "apply"]
FORBIDDEN = {"httpx", "playwright"}
# API clients (not scrapers) that may use httpx: the OpenAI-compatible scorer talks to an LLM
# endpoint the user configured. Keep this list explicit and tiny.
ALLOWED = {"scoring/scorers.py", "scoring/decisions.py"}
# jobhunter.mail talks to the user's own Gmail through Google's official client libraries.
# It is an API client, not a scraper, so it is exempt from the FetchContext rule.
ALLOWED_PREFIXES = ("mail/",)


def network_imports(path: Path, root: Path = SRC) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names = [node.module]
        else:
            continue
        where = f"{path.relative_to(root)}:{node.lineno}"
        found += [f"{where} {n}" for n in names if n.split(".")[0] in FORBIDDEN]
    return found


def guarded_modules() -> list[Path]:
    return sorted(p for pkg in GUARDED_PACKAGES for p in (SRC / pkg).rglob("*.py"))


def test_guarded_packages_exist():
    for pkg in GUARDED_PACKAGES:
        assert (SRC / pkg / "__init__.py").is_file()
    assert guarded_modules()


@pytest.mark.parametrize("path", guarded_modules(), ids=lambda p: str(p.relative_to(SRC)))
def test_no_direct_network_imports(path):
    rel = str(path.relative_to(SRC))
    if rel in ALLOWED or rel.startswith(ALLOWED_PREFIXES):
        pytest.skip("explicitly allowed API client")
    assert network_imports(path) == [], "use jobhunter.core.fetch.FetchContext instead"


def test_detector_catches_violations(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text(
        "import httpx\nfrom playwright.sync_api import Page\nimport os\nfrom . import x\n"
    )
    assert network_imports(bad, root=tmp_path) == [
        "bad.py:1 httpx",
        "bad.py:2 playwright.sync_api",
    ]


def test_allowlist_is_only_scorers_and_actually_needed():
    assert {"scoring/scorers.py", "scoring/decisions.py"} == ALLOWED
    assert network_imports(SRC / "scoring" / "scorers.py")
    assert network_imports(SRC / "scoring" / "decisions.py")
