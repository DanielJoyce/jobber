"""The container help page must not drift from the real command line (specs/018 C11, 019 S11d).

The page lives at src/jobhunter/console/help/containers.md; docs/containers.md is a pointer.

Every `jobhunter ...` (and `jobhunter-ctr ...`, which runs the same CLI in a container) line in
a fenced block must name a real subcommand and only real options. Local links must resolve.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

import pytest
import typer.main

from jobhunter.cli import app

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "src" / "jobhunter" / "console" / "help" / "containers.md"


def _command_lines() -> list[str]:
    lines: list[str] = []
    in_fence = False
    for raw in DOC.read_text(encoding="utf-8").splitlines():
        if raw.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence and re.match(r"jobhunter(-ctr)? ", raw):
            lines.append(raw)
    return lines


def _tokens(line: str) -> list[str]:
    toks = shlex.split(line, comments=True)
    for i, tok in enumerate(toks):
        if tok in {"|", "&&", ";", ">"}:
            return toks[1:i]
    return toks[1:]


def test_doc_has_commands_and_checklist_first():
    text = DOC.read_text(encoding="utf-8")
    assert len(_command_lines()) >= 25
    headings = re.findall(r"^## (.+)$", text, flags=re.M)
    assert headings[0] == "First run checklist"


@pytest.mark.parametrize("line", _command_lines())
def test_documented_command_exists(line: str):
    cmd = typer.main.get_command(app)  # typer vendors click: duck-type groups
    toks = _tokens(line)
    consumed = 0
    while hasattr(cmd, "commands") and consumed < len(toks):
        tok = toks[consumed]
        if tok.startswith("-"):
            break
        sub = cmd.commands.get(tok)
        if sub is None:
            pytest.fail(f"{line!r}: {tok!r} is not a subcommand of {cmd.name}")
        cmd = sub
        consumed += 1
    assert consumed, f"{line!r}: no subcommand"
    known = {opt for p in cmd.params for opt in (*p.opts, *p.secondary_opts)}
    known.add("--help")
    for tok in toks[consumed:]:
        if tok.startswith("--"):
            assert tok.split("=")[0] in known, f"{line!r}: {tok} is not an option of {cmd.name}"
        elif tok.startswith("-") and len(tok) > 1:
            assert tok in known, f"{line!r}: {tok} is not an option of {cmd.name}"


def test_local_links_resolve():
    text = DOC.read_text(encoding="utf-8")
    anchors = {
        re.sub(r"[^a-z0-9 -]", "", h.lower()).replace(" ", "-")
        for h in re.findall(r"^#+ (.+)$", text, flags=re.M)
    }
    for target in re.findall(r"\]\(([^)]+)\)", text):
        if target.startswith("#"):
            assert target[1:] in anchors, f"dead anchor {target}"
        elif not target.startswith("http"):
            assert (DOC.parent / target.split("#")[0]).exists(), f"dead link {target}"


def test_no_personal_data():
    text = DOC.read_text(encoding="utf-8")
    assert not re.search(r"[\w.+-]+@(?!example\.com)[\w-]+\.[a-z]{2,}", text)


def test_deploy_files_and_units_named_in_doc_exist():
    text = DOC.read_text(encoding="utf-8")
    assert "deploy/quadlet/*.container" in text and "deploy/systemd/*" in text
    assert list((ROOT / "deploy" / "quadlet").glob("*.container"))
    assert list((ROOT / "deploy" / "systemd").iterdir())
    quadlet = {p.stem for p in (ROOT / "deploy" / "quadlet").glob("*.container")}
    timers = {p.stem for p in (ROOT / "deploy" / "systemd").glob("*.timer")}
    for name in set(re.findall(r"\b(jobhunter-(?:ctr-)?[a-z]+)(?=[\s.,)`'])", text)):
        if name in {"jobhunter-ctr"}:
            continue
        assert name in quadlet, f"unit {name} named in the doc has no deploy/quadlet file"
    for name in set(re.findall(r"\b(jobhunter-ctr-[a-z]+)\.timer", text)):
        assert name in timers, f"timer {name} has no deploy/systemd file"
