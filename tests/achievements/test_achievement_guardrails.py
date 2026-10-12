"""Guardrails (specs/021 "Privacy and money", G3): the achievements package never imports a
scorer, the packet generator, an API client or the fetch layer; its SQL never names scoring,
spending or re-score tables or cost columns; evaluate never reads them at run time; and no
card copy talks about scoring, generating or sending more applications."""

from __future__ import annotations

import ast
import re
import sqlite3
from pathlib import Path

import pytest
from achv_world import NOW, TZ

from jobhunter.achievements import catalog, engine

PKG = Path(engine.__file__).parent
FORBIDDEN_IMPORTS = (
    "jobhunter.scoring",
    "jobhunter.apply.generator",
    "jobhunter.apply.score",
    "jobhunter.core.fetch",
    "jobhunter.console",
    "anthropic",
    "httpx",
    "playwright",
)
FORBIDDEN_SQL = (
    "fit_score",
    "llm_spend",
    "rescore_request",
    "rescore_run",
    "cost_usd",
    "api_equiv_usd",
    "job_fts",
)
FORBIDDEN_TABLES = {"fit_score", "llm_spend", "rescore_request", "rescore_run", "job", "job_fts"}
DENY_COPY = ("score", "generat", "more applications")


def sources() -> list[Path]:
    files = sorted(PKG.rglob("*.py"))
    assert files
    return files


def imports_of(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names += [node.module] + [f"{node.module}.{a.name}" for a in node.names]
    return names


def bad_imports(path: Path) -> list[str]:
    return [
        n
        for n in imports_of(path)
        if any(n == f or n.startswith(f + ".") for f in FORBIDDEN_IMPORTS)
    ]


def strings_of(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)
    ]


def bad_sql(path: Path) -> list[str]:
    found = []
    for s in strings_of(path):
        low = s.lower()
        found += [f for f in FORBIDDEN_SQL if re.search(rf"\b{f}\b", low)]
        if re.search(r"\b(from|join)\s+job\b(?!_)", low):
            found.append("job")
    return found


@pytest.mark.parametrize("path", sources(), ids=lambda p: p.name)
def test_no_forbidden_imports(path):
    assert bad_imports(path) == []


@pytest.mark.parametrize("path", sources(), ids=lambda p: p.name)
def test_rule_sql_never_names_scoring_or_spending(path):
    assert bad_sql(path) == []


def test_detectors_catch_violations(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text(
        "import anthropic\nfrom jobhunter.scoring import rescore\n"
        "from jobhunter.apply import generator\n"
        "Q = 'SELECT verdict FROM fit_score'\nR = 'SELECT SUM(cost_usd) FROM llm_spend'\n"
        "S = 'SELECT title FROM job WHERE id = ?'\n"
    )
    assert {"anthropic", "jobhunter.scoring", "jobhunter.apply.generator"} <= set(bad_imports(bad))
    assert set(bad_sql(bad)) == {"fit_score", "cost_usd", "llm_spend", "job"}


def test_evaluate_reads_no_scoring_spending_or_job_table(world):
    """At run time, with every rule exercised: SQLite's authorizer sees every table read."""
    from test_achievement_engine import history

    history(world)
    read: set[str] = set()

    def authorizer(action, arg1, arg2, dbname, source):
        if action == sqlite3.SQLITE_READ and arg1:
            read.add(arg1)
        return sqlite3.SQLITE_OK

    world.conn.set_authorizer(authorizer)
    try:
        engine.evaluate(world.conn, NOW, TZ)
        engine.fingerprint(world.conn, NOW, TZ)
    finally:
        world.conn.set_authorizer(None)
    assert "application_event" in read and "label" in read  # the hook saw the reads
    assert read & FORBIDDEN_TABLES == set()


@pytest.mark.parametrize("a", catalog.CATALOG, ids=lambda a: a.id)
def test_copy_never_mentions_scoring_generating_or_volume(a):
    for text in (a.name, a.description, a.hint):
        low = text.lower()
        assert not [w for w in DENY_COPY if w in low], text


@pytest.mark.parametrize("a", catalog.CATALOG, ids=lambda a: a.id)
def test_copy_is_present_and_hidden_ones_have_no_hint(a):
    assert a.name and a.description
    assert a.category in catalog.CATEGORY_KEYS
    assert (a.hint == "") == a.hidden
    assert list(a.thresholds) == sorted(set(a.thresholds))


def test_copy_never_leads_with_rejected_or_says_lost():
    for a in catalog.CATALOG:
        for text in (a.name, a.description, a.hint):
            assert not text.lower().startswith("rejected")
            assert not re.search(r"\b(lost|broken)\b", text.lower()), text
