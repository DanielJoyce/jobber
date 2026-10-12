"""Every profile load resolves the resume the same way (specs/019 S0). Synthetic data only."""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import anthropic
import pytest
from typer.testing import CliRunner

from jobhunter.cli import app
from jobhunter.config import Paths, Settings
from jobhunter.pipeline.runner import default_profile_loader
from jobhunter.scoring import profile as profile_mod
from jobhunter.scoring import rescore, screen
from jobhunter.scoring.profile import load_profile_for

SRC = Path(__file__).resolve().parents[2] / "src" / "jobhunter"
EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "preferences.example.yaml"
cli = CliRunner()


def _is_resume_arg(node: ast.AST) -> bool:
    return any(isinstance(n, ast.Attribute) and n.attr == "resume_path" for n in ast.walk(node))


def test_only_scoring_profile_passes_a_resume_path_to_the_loader():
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        if rel == "scoring/profile.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            if name not in {"load_profile", "write_preferences", "_load_file"}:
                continue
            explicit = len(node.args) > 1 or any(
                k.arg == "resume_path" or _is_resume_arg(k.value) for k in node.keywords
            )
            if explicit or any(_is_resume_arg(a) for a in node.args):
                offenders.append(f"{rel}:{node.lineno}")
    assert offenders == [], f"pass Settings to load_profile_for instead: {offenders}"


@pytest.fixture
def split(tmp_path):
    """preferences.yaml names resume A; settings point at a different resume B."""
    pdir = tmp_path / "profile"
    pdir.mkdir()
    text = EXAMPLE.read_text(encoding="utf-8")
    (pdir / "preferences.yaml").write_text(
        text.replace("resume_path: null", "resume_path: yaml-resume.md")
    )
    (pdir / "yaml-resume.md").write_text("Synthetic A\n\n- yaml resume\n", encoding="utf-8")
    other = tmp_path / "config-resume.md"
    other.write_text("Synthetic B\n\n- config resume\n", encoding="utf-8")
    settings = Settings(
        paths=Paths(
            profile_dir=pdir, resume_path=other, data_dir=tmp_path, db_path=tmp_path / "x.db"
        )
    )
    return SimpleNamespace(settings=settings, yaml_resume=pdir / "yaml-resume.md", other=other)


def test_run_loader_uses_the_yaml_resume(split):
    profile = default_profile_loader(split.settings)()
    assert profile is not None
    assert profile.resume_file == split.yaml_resume
    assert profile.scoring_version == load_profile_for(split.settings).scoring_version


@pytest.mark.parametrize("flag", ["--collect-pending", "--rescore-pending"])
def test_score_resolves_the_same_resume_as_run(split, tmp_path, monkeypatch, flag):
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'[paths]\ndb_path = "{tmp_path / "x.db"}"\n'
        f'profile_dir = "{split.settings.paths.profile_dir}"\n'
        f'data_dir = "{tmp_path}"\nresume_path = "{split.other}"\n'
    )
    monkeypatch.setenv("JOBHUNTER_CONFIG", str(cfg))
    seen: list[Path] = []
    real = profile_mod.load_profile_for

    def spy(settings):
        p = real(settings)
        seen.append(p.resume_file)
        return p

    monkeypatch.setattr(profile_mod, "load_profile_for", spy)
    monkeypatch.setattr(anthropic, "Anthropic", lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr(screen, "collect_pending", lambda *a, **k: {})
    monkeypatch.setattr(rescore, "drain_pending", lambda *a, **k: [])
    cli.invoke(app, ["score", flag])
    assert seen == [split.yaml_resume]
    assert default_profile_loader(split.settings)().resume_file == seen[0]


def test_config_resume_is_the_fallback_when_yaml_names_none(split):
    (split.settings.paths.profile_dir / "preferences.yaml").write_text(
        EXAMPLE.read_text(encoding="utf-8")
    )
    assert default_profile_loader(split.settings)().resume_file == split.other
