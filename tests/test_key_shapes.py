"""shown_value hides key-shaped values and keeps real model ids (bug 72a8a5e, df04faa N4), and
doctor's model access rows (informational, key-shaped values never printed)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jobhunter.config import shown_value
from test_cli_doctor import complete_setup, run_doctor

KEY_SHAPES = [
    "AIza" + "SyA1b2C3d4E5f6G7h8I9j0KlMnOpQrStUvW",
    "glpat" + "-aB3dE6gH9jK2mN5pQ8sT",
    "gsk_aB3dE6gH9jK2mN5pQ8sT1vW4xY7z",
    "hf_aB3dE6gH9jK2mN5pQ8sT1vW4xY7z",
    "xai-aB3dE6gH9jK2mN5pQ8sT1vW4xY7z",
    "sk-or-v1-0123456789abcdef",
    "ghp_aB3dE6gH9jK2mN5pQ8sT1vW4xY7z",
    "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",  # Together
    "aB3dE6gH9jK2mN5pQ8sT1vW4xY7zLm0o",  # Mistral
    "openrouter:aB3dE6gH9jK2mN5pQ8sT1vW4xY7zLm0o",
    "local:AIza" + "SyA1b2C3d4E5f6G7h8I9j0KlMnOpQrStUvW",
]


def model_id_corpus() -> list[str]:
    from jobhunter.core import spend
    from jobhunter.scoring import catalog

    ids = {
        "claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5", "openai/gpt-5.5-pro",
        "jev:typesafe/jev-1.13", "openrouter:anthropic/claude-opus-4.5", "local:qwen2.5-7b",
        "meta-llama/llama-3.3-70b-instruct:free", "openai-compat:mistral-large-latest",
        "google/gemini-2.5-flash-lite-preview-09-2025", "deepseek/deepseek-chat-v3.1",
        "claude-3-5-haiku-20241022", "claude-sonnet-4-5-20250929",
        "Qwen/Qwen2.5-72B-Instruct-Turbo", "mistralai/Mixtral-8x22B-Instruct-v0.1",
        "gpt-4o-mini-2024-07-18", "decisions:acme/model-v2",
    }  # fmt: skip
    ids |= set(catalog.ANTHROPIC_MODELS) | set(catalog.CLI_ALIASES)
    for name in dir(spend):
        table = getattr(spend, name)
        if name.isupper() and "PRICE" in name and isinstance(table, dict):
            ids |= {k for k in table if isinstance(k, str)}
    return sorted(ids)


@pytest.mark.parametrize("value", KEY_SHAPES)
def test_shown_value_hides_every_key_shape(value):
    assert shown_value(value) is None


def test_shown_value_keeps_every_real_model_id():
    corpus = model_id_corpus()
    assert len(corpus) > 20
    for model_id in corpus:
        assert shown_value(model_id) == model_id, model_id


def test_access_rows_are_informational_and_never_change_the_exit_code(tmp_path, monkeypatch):
    complete_setup(tmp_path, monkeypatch)
    result = run_doctor("--json")
    assert result.exit_code == 0, result.output
    rows = {r["id"]: r for r in json.loads(result.output)}
    access = [r for r in rows.values() if r["id"].startswith("models.access.")]
    assert len(access) == 4 and all(r["level"] == "info" for r in access)
    # A malformed scorer in config.toml makes its access row report a problem
    # (scorer_problems); the exit code still follows the essential rows alone.
    cfg = tmp_path / "config.toml"
    cfg.write_text(cfg.read_text() + '[scoring]\ndeep_scorer = "nonsense:x"\n')
    bad = run_doctor("--json")
    rows = {r["id"]: r for r in json.loads(bad.output)}
    assert rows["models.access.deep"]["status"] == "missing"
    assert "blocked" in rows["models.access.deep"]["detail"]
    failing = [r for r in rows.values() if r["level"] == "essential" and r["status"] != "ok"]
    assert bad.exit_code == (1 if failing else 0)


@pytest.mark.parametrize("key", KEY_SHAPES[:5] + KEY_SHAPES[7:9])
def test_verbose_doctor_never_prints_a_key_shaped_model_value(tmp_path, monkeypatch, key):
    settings = complete_setup(tmp_path, monkeypatch)
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        cfg.read_text() + f'[scoring]\nscreen_scorer = "{key}"\n[apply]\napi_model = "{key}"\n'
    )
    (Path(settings.paths.data_dir) / "models.toml").write_text(
        f'[apply]\ncli_model = "{key}"\n[scoring]\ndeep_scorer = "{key}"\n'
    )
    for args in ([], ["-v"], ["--json"], ["-v", "--json"]):
        assert key not in run_doctor(*args).output, args
