"""020 M10: the "Model choices" readiness row, the scorer rows' source label, and the model
access rows and sources in ``doctor``. Synthetic data; no network, no model call."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from jobhunter.cli import app
from jobhunter.ops import doctor
from jobhunter.readiness import BROKEN, MISSING, OK
from test_cli_doctor import no_key, write_config  # noqa: F401
from test_readiness import SENTINEL, evaluate, good_settings

runner = CliRunner()


def write_models(tmp_path: Path, text: str) -> None:
    (tmp_path / "data").mkdir(exist_ok=True)
    (tmp_path / "data" / "models.toml").write_text(text, encoding="utf-8")


def test_model_choices_ok_without_a_file(tmp_path):
    row = evaluate(good_settings(tmp_path))["models.choices"]
    assert row.status == OK and "no models.toml" in row.detail


def test_model_choices_ok_with_a_valid_file(tmp_path):
    write_models(tmp_path, '[scoring]\nscreen_scorer = "anthropic:claude-haiku-4-5"\n')
    row = evaluate(good_settings(tmp_path))["models.choices"]
    assert row.status == OK and row.source == "models.toml" and "1 setting" in row.detail


def test_model_choices_broken_for_a_file_that_is_not_toml(tmp_path):
    write_models(tmp_path, f"[scoring\nscreen_scorer = {SENTINEL}\n")
    row = evaluate(good_settings(tmp_path))["models.choices"]
    assert row.status == BROKEN and row.fix is not None
    assert SENTINEL not in json.dumps(row.as_dict())


def test_model_choices_broken_for_a_blocked_use_and_a_drafting_fallback(tmp_path):
    write_models(
        tmp_path,
        '[scoring]\nscreen_scorer = "anthropic:claude-opus-5-5"\n[apply]\n'
        f'api_model = "{SENTINEL} key"\n',
    )
    row = evaluate(good_settings(tmp_path))["models.choices"]
    assert row.status == BROKEN
    assert "screen_scorer" in row.detail and "apply.api_model" in row.detail
    assert SENTINEL not in json.dumps(row.as_dict())


def test_model_choices_unknown_key_is_ok_but_noted(tmp_path):
    write_models(tmp_path, "[scoring]\nmystery = 1\n")
    row = evaluate(good_settings(tmp_path))["models.choices"]
    assert row.status == OK and "ignored" in row.detail


def test_scorer_row_names_models_toml_as_the_source_of_a_valid_value(tmp_path):
    write_models(tmp_path, '[scoring]\nscreen_scorer = "openrouter:x/y"\n')
    row = evaluate(good_settings(tmp_path))["scorer.screen"]
    assert row.status == MISSING and row.source == "models.toml"
    assert "(models.toml)" in row.detail and "config.toml" not in row.detail
    ok = evaluate(good_settings(tmp_path), {"OPENROUTER_API_KEY": "sk-or-fake"})
    assert ok["scorer.screen"].status == OK


def test_doctor_model_access_rows_and_verbose_sources(tmp_path, monkeypatch, no_key):  # noqa: F811
    write_config(tmp_path, monkeypatch)
    write_models(
        tmp_path,
        '[scoring]\nscreen_scorer = "anthropic:claude-haiku-4-5"\n'
        'deep_scorer = "anthropic:claude-bogus-9"\n[apply]\nrunner = "api"\n'
        f'api_model = "{SENTINEL} key"\n',
    )
    result = runner.invoke(app, ["doctor", "--json"])
    rows = {r["id"]: r for r in json.loads(result.output)}
    assert {f"models.access.{u}" for u in ("screen", "deep", "draft", "factcheck")} <= set(rows)
    assert rows["models.access.screen"]["status"] == "missing"
    assert "ANTHROPIC_API_KEY" in rows["models.access.screen"]["detail"]
    assert rows["models.access.deep"]["detail"].startswith("blocked")
    assert rows["models.choices"]["status"] == "broken"
    assert "models.access.screen" not in doctor.readiness.CHECK_IDS  # doctor-only
    out = runner.invoke(app, ["doctor", "-v"]).output
    assert "scoring.screen_scorer" in out and "[models.toml]" in out
    assert "apply.cli_model" in out and "[default]" in out
    assert SENTINEL not in out and SENTINEL not in result.output
    assert "Model settings" not in runner.invoke(app, ["doctor"]).output
