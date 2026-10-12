"""models.toml: layers, per-key validation, fail closed, and the writer (specs/020 M2).

No network, no model calls: everything here reads and writes files under tmp dirs.
"""

from __future__ import annotations

import logging
import os
import stat
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from jobhunter import model_choices as mc
from jobhunter.cli import app
from jobhunter.config import (
    BLOCKED_SPEC,
    load_layers,
    load_settings,
    models_file,
)
from jobhunter.core import db

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
SENTINEL = "sk-ant-SENTINEL-must-never-be-echoed"
cli = CliRunner()


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    """A config.toml with an explicit data dir; returns (config path, models.toml path)."""
    data = tmp_path / "data"
    data.mkdir()
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'[paths]\ndata_dir = "{data}"\n')
    monkeypatch.setenv("JOBHUNTER_CONFIG", str(cfg))
    return cfg, data / "models.toml"


def write_config(cfg: Path, extra: str) -> None:
    cfg.write_text(cfg.read_text() + extra)


# ─── Precedence ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("section", "key", "config_value", "models_value"),
    [
        ("scoring", "screen_scorer", '"jev:typesafe/jev-1.13"', '"local:qwen3"'),
        ("scoring", "deep_scorer", '"anthropic:claude-opus-5"', '"anthropic:claude-sonnet-5-5"'),
        ("apply", "runner", '"api"', '"cli"'),
        ("apply", "cli_model", '"sonnet"', '"claude-opus-5-5"'),
        ("apply", "api_model", '"claude-sonnet-5-5"', '"claude-opus-5"'),
        ("apply", "effort", '"low"', '"high"'),
        ("apply", "entailment_check", "false", "true"),
        ("apply", "cli_entailment_model", '"sonnet"', '"claude-haiku-5-5"'),
        ("apply", "api_entailment_model", '"claude-sonnet-5-5"', '"claude-haiku-4-5"'),
    ],
)
def test_precedence_default_then_config_then_models_then_overrides(
    dirs, section, key, config_value, models_value
):
    import tomllib

    cfg, mfile = dirs
    default = getattr(getattr(load_settings(), section), key)
    write_config(cfg, f"[{section}]\n{key} = {config_value}\n")
    from_config = tomllib.loads(f"v = {config_value}")["v"]
    from_models = tomllib.loads(f"v = {models_value}")["v"]
    assert from_config != default and from_models != from_config

    layers = load_layers()
    assert getattr(getattr(layers.merged, section), key) == from_config
    assert layers.source(section, key) == "config.toml"

    mfile.write_text(f"[{section}]\n{key} = {models_value}\n")
    layers = load_layers()
    assert getattr(getattr(layers.merged, section), key) == from_models
    assert getattr(getattr(layers.base, section), key) == from_config
    assert layers.source(section, key) == "models.toml"
    assert layers.problems == []

    # The overrides argument (tests) stays highest.
    over = load_layers(overrides={section: {key: from_config}})
    assert getattr(getattr(over.merged, section), key) == from_config
    assert over.source(section, key) == "override"


def test_no_models_file_behaves_as_before(dirs):
    layers = load_layers()
    assert not layers.exists and layers.problems == [] and layers.layer == {}
    assert layers.merged.model_dump() == layers.base.model_dump()
    assert layers.merged.scoring.scorer_problems() == {}


def test_models_file_lives_in_the_data_dir_from_the_environment(tmp_path, monkeypatch):
    data = tmp_path / "elsewhere"
    data.mkdir()
    (data / "models.toml").write_text('[apply]\neffort = "high"\n')
    monkeypatch.setenv("JOBHUNTER_DATA_DIR", str(data))
    settings = load_settings()
    assert settings.apply.effort == "high"
    assert settings.models_layers.path == data / "models.toml"


def test_container_mode_reads_models_toml_from_the_data_mount(tmp_path, monkeypatch):
    """Container units run with JOBHUNTER_CONTAINER=1 and the data dir mounted at /data."""
    mount = tmp_path / "data-mount"
    mount.mkdir()
    (mount / "models.toml").write_text('[scoring]\nscreen_scorer = "local:qwen3"\n')
    monkeypatch.setenv("JOBHUNTER_CONTAINER", "1")
    monkeypatch.setattr("jobhunter.container.DATA_DIR", str(mount))
    settings = load_settings()
    assert settings.scoring.screen_scorer == "local:qwen3"
    assert models_file(settings.paths.data_dir) == mount / "models.toml"


def test_model_copy_keeps_the_block(dirs):
    _, mfile = dirs
    mfile.write_text('[scoring]\nscreen_scorer = "claude-haiku-5-5"\n')
    settings = load_settings()
    copied = settings.model_copy(update={"apply": settings.apply.model_copy()})
    assert "screen_scorer" in copied.scoring.scorer_problems()
    # Even a rebuild from plain data, which drops private state, cannot run the scorer.
    rebuilt = type(settings).model_validate(settings.model_dump())
    assert rebuilt.scoring.screen_scorer == BLOCKED_SPEC
    assert "screen_scorer" in rebuilt.scoring.scorer_problems()


# ─── Fail closed ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("screen_scorer", '"claude-haiku-5-5"'),  # bare name
        ("screen_scorer", '"openrouter:jev:typesafe/jev-1.13"'),
        ("screen_scorer", '"anthropic:claude-unpriced-9"'),  # no price
        ("screen_scorer", '"anthropic:claude-opus-5-5"'),  # thinking cannot be turned off
        ("screen_scorer", "42"),
        ("deep_scorer", '"openrouter:openai/gpt-oss-120b"'),  # deep is Anthropic only
        ("deep_scorer", '"anthropic:claude-haiku-4-5"'),  # rejects effort
    ],
)
def test_invalid_spending_key_blocks_and_never_falls_back(dirs, key, value):
    cfg, mfile = dirs
    write_config(cfg, '[scoring]\nscreen_scorer = "jev:typesafe/jev-1.13"\n')
    mfile.write_text(f"[scoring]\n{key} = {value}\n")
    layers = load_layers()
    settings = layers.merged
    assert getattr(settings.scoring, key) == BLOCKED_SPEC  # not config.toml's, not the default
    problem = settings.scoring.scorer_problems()[key]
    assert problem.startswith(f"models.toml {key} is invalid")
    assert layers.source("scoring", key) == "models.toml (invalid)"
    # The other spending key is untouched.
    other = "deep_scorer" if key == "screen_scorer" else "screen_scorer"
    assert other not in settings.scoring.scorer_problems()


def test_broken_file_blocks_both_spending_uses_and_drafting_falls_back(dirs):
    cfg, mfile = dirs
    write_config(cfg, '[apply]\ncli_model = "sonnet"\n')
    mfile.write_text('[scoring\nscreen_scorer = "local:x"\n')
    layers = load_layers()
    assert layers.broken
    problems = layers.merged.scoring.scorer_problems()
    assert set(problems) == {"screen_scorer", "deep_scorer"}
    assert "not valid TOML" in problems["screen_scorer"]
    assert layers.merged.apply.cli_model == "sonnet"
    assert any("drafting uses the config.toml values" in n for n in layers.notices)


def test_unreadable_file_blocks(dirs):
    _, mfile = dirs
    mfile.mkdir()  # a directory where the file should be: reading it fails
    problems = load_settings().scoring.scorer_problems()
    assert set(problems) == {"screen_scorer", "deep_scorer"}


def test_scoring_not_a_table_blocks(dirs):
    _, mfile = dirs
    mfile.write_text('scoring = "local:x"\n')
    assert set(load_settings().scoring.scorer_problems()) == {"screen_scorer", "deep_scorer"}


def test_config_toml_values_are_not_judged_by_the_catalog(dirs):
    """Only models.toml keys fail closed; a config.toml Opus screen scorer runs as before."""
    cfg, _ = dirs
    write_config(cfg, '[scoring]\nscreen_scorer = "anthropic:claude-opus-5-5"\n')
    settings = load_settings()
    assert settings.scoring.screen_scorer == "anthropic:claude-opus-5-5"
    assert settings.scoring.scorer_problems() == {}


def test_an_override_unblocks(dirs):
    _, mfile = dirs
    mfile.write_text('[scoring]\nscreen_scorer = "nope"\n')
    settings = load_settings(overrides={"scoring": {"screen_scorer": "local:x"}})
    assert settings.scoring.screen_scorer == "local:x"
    assert settings.scoring.scorer_problems() == {}


@pytest.mark.parametrize(
    ("key", "value", "fallback"),
    [
        ("runner", '"subscription"', "cli"),
        ("effort", '"max"', "medium"),
        ("entailment_check", '"yes"', "true"),
        ("cli_model", '"haiku"', "opus"),  # the CLI runner always passes --effort
        ("api_model", '"anthropic:claude-opus-5-5"', "claude-opus-5-5"),  # a spec, not an id
        ("api_entailment_model", '"claude-opus-5-5"', "claude-haiku-5-5"),
    ],
)
def test_invalid_drafting_key_falls_back_with_a_notice(dirs, key, value, fallback):
    _, mfile = dirs
    mfile.write_text(
        f'[apply]\n{key} = {value}\neffort = "high"\n'
        if key != "effort"
        else (f"[apply]\n{key} = {value}\n")
    )
    layers = load_layers()
    got = getattr(layers.merged.apply, key)
    assert (str(got).lower() if isinstance(got, bool) else got) == fallback
    assert layers.merged.scoring.scorer_problems() == {}  # drafting never blocks scoring
    notice = layers.merged.models_notices
    assert notice == [f"models.toml's apply.{key} is invalid; using {fallback} from the default"]
    if key != "effort":
        assert layers.merged.apply.effort == "high"  # the valid key beside it still applies


def test_unknown_keys_warn_and_are_ignored(dirs, caplog):
    _, mfile = dirs
    mfile.write_text(
        '[scoring]\nscreen_scorer = "local:x"\nfuture_key = 1\ndaily_cap_usd = 99.0\n'
        "[newer_section]\nwhatever = true\n"
    )
    with caplog.at_level(logging.WARNING, logger="jobhunter.config"):
        layers = load_layers()
    assert layers.merged.scoring.screen_scorer == "local:x"
    assert layers.merged.scoring.daily_cap_usd == 2.0  # caps stay in config.toml
    kinds = {p.key: p.kind for p in layers.problems}
    assert kinds == {
        "scoring.future_key": "unknown",
        "scoring.daily_cap_usd": "unknown",
        "newer_section": "unknown",
    }
    assert layers.merged.scoring.scorer_problems() == {}
    assert "scoring.future_key" in caplog.text


def test_a_sentinel_value_is_never_echoed(dirs, caplog):
    _, mfile = dirs
    mfile.write_text(
        f'[scoring]\nscreen_scorer = "{SENTINEL}"\ndeep_scorer = "x {SENTINEL}"\n'
        f'[apply]\neffort = "{SENTINEL}"\ncli_model = "{SENTINEL}"\n'
        f'runner = "{SENTINEL}"\n"{SENTINEL}" = 1\n'
    )
    with caplog.at_level(logging.WARNING):
        layers = load_layers()
    text = "\n".join(
        [p.message for p in layers.problems]
        + list(layers.notices)
        + list(layers.merged.scoring.scorer_problems().values())
        + [caplog.text]
    )
    assert "SENTINEL" not in text
    out = cli.invoke(app, ["models", "show"])
    assert "SENTINEL" not in out.output
    out = cli.invoke(app, ["models", "show", "--json"])
    assert "SENTINEL" not in out.output


# ─── The writer ─────────────────────────────────────────────────────────────


def test_write_is_0600_has_the_header_and_returns_the_digest(dirs):
    _, mfile = dirs
    base = load_layers().digest
    res = mc.write_changes(mfile, {"effort": "high"}, base, source="cli", now=NOW)
    assert stat.S_IMODE(os.stat(mfile).st_mode) == 0o600
    assert mfile.read_text().startswith("# Model choices, written by /models")
    assert res.digest == load_layers().digest != base
    assert load_settings().apply.effort == "high"


def test_two_saves_in_a_row_from_one_view_succeed(dirs):
    _, mfile = dirs
    d0 = load_layers().digest
    d1 = mc.write_changes(mfile, {"effort": "high"}, d0, source="ui", now=NOW).digest
    d2 = mc.write_changes(mfile, {"screen": "local:qwen3"}, d1, source="ui", now=NOW).digest
    settings = load_settings()
    assert (settings.apply.effort, settings.scoring.screen_scorer) == ("high", "local:qwen3")
    assert d2 == load_layers().digest


def test_a_stale_digest_writes_nothing(dirs):
    _, mfile = dirs
    d0 = load_layers().digest
    mc.write_changes(mfile, {"effort": "high"}, d0, source="ui", now=NOW)
    before = mfile.read_bytes()
    with pytest.raises(mc.StaleDigest):
        mc.write_changes(mfile, {"effort": "low"}, d0, source="ui", now=NOW)
    assert mfile.read_bytes() == before


def test_concurrent_saves_serialize_under_the_lock(dirs):
    _, mfile = dirs
    d0 = load_layers().digest
    results: list[str] = []
    gate = threading.Barrier(4)

    def save(value: str) -> None:
        gate.wait()
        try:
            mc.write_changes(mfile, {"effort": value}, d0, source="ui", now=NOW)
            results.append("ok")
        except mc.StaleDigest:
            results.append("stale")

    threads = [threading.Thread(target=save, args=(v,)) for v in ("low", "high", "low", "high")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == ["ok", "stale", "stale", "stale"]
    assert load_layers().problems == []


def test_only_the_changed_field_is_written_and_other_keys_stay(dirs):
    cfg, mfile = dirs
    write_config(cfg, '[apply]\ncli_model = "sonnet"\n')
    mfile.write_text('[scoring]\nfuture_key = [1, 2]\n[newer]\nx = "y"\n')
    d0 = load_layers().digest
    mc.write_changes(mfile, {"effort": "low"}, d0, source="ui", now=NOW)
    layers = load_layers()
    assert layers.layer == {"apply": {"effort": "low"}}  # cli_model was not copied in
    assert layers.source("apply", "cli_model") == "config.toml"
    import tomllib

    data = tomllib.loads(mfile.read_text())
    assert data["scoring"]["future_key"] == [1, 2] and data["newer"] == {"x": "y"}


def test_setting_the_same_value_is_no_change(dirs):
    _, mfile = dirs
    d1 = mc.write_changes(mfile, {"effort": "low"}, load_layers().digest, source="ui", now=NOW)
    res = mc.write_changes(mfile, {"effort": "low"}, d1.digest, source="ui", now=NOW)
    assert res.changed == {} and res.digest == d1.digest


def test_a_broken_file_is_not_written_until_reset(dirs):
    _, mfile = dirs
    mfile.write_text("[apply\n")
    d0 = load_layers().digest
    with pytest.raises(mc.BrokenFile):
        mc.write_changes(mfile, {"effort": "low"}, d0, source="ui", now=NOW)
    assert mfile.read_text() == "[apply\n"
    digest, kept = mc.reset_file(mfile, now=NOW)
    assert kept is not None and kept.read_text() == "[apply\n"
    assert kept.name == "models.toml.broken-20261011T120000"
    layers = load_layers()
    assert layers.digest == digest and layers.problems == []
    assert layers.merged.scoring.scorer_problems() == {}


def test_reset_removes_the_key_so_config_applies(dirs):
    cfg, mfile = dirs
    write_config(cfg, '[apply]\neffort = "low"\n')
    d1 = mc.write_changes(mfile, {"effort": "high"}, load_layers().digest, source="ui", now=NOW)
    assert load_settings().apply.effort == "high"
    mc.write_changes(mfile, {"effort": None}, d1.digest, source="reset", now=NOW)
    layers = load_layers()
    assert layers.merged.apply.effort == "low" and layers.source("apply", "effort") == "config.toml"


# ─── Save checks ────────────────────────────────────────────────────────────


def test_check_save_refuses_what_could_never_work():
    check = mc.check_save("screen", "anthropic:claude-opus-5-5")
    assert check.error and "thinking" in check.error
    assert mc.check_save("deep", "openrouter:x/y").error
    assert mc.check_save("effort", "max").error
    assert mc.check_save("screen", "anthropic:claude-haiku-5-5").ok


def test_check_save_needs_force_for_a_dearer_screening_model():
    check = mc.check_save("screen", "anthropic:claude-sonnet-5-5")
    assert check.error is None and check.needs_force
    assert "reserves" in check.needs_force[0]


def test_check_save_needs_force_for_an_uncached_openrouter_slug():
    check = mc.check_save("screen", "openrouter:someone/unknown-model")
    assert check.error is None and check.needs_force


def test_a_forced_value_applies_when_read_back(dirs):
    """--force saves a value the reservation rule would not offer; the reader must not then
    block it (the reservation is a save-time rule)."""
    _, mfile = dirs
    mc.write_changes(
        mfile,
        {"screen": "anthropic:claude-sonnet-5-5"},
        load_layers().digest,
        source="cli",
        now=NOW,
    )
    settings = load_settings()
    assert settings.scoring.screen_scorer == "anthropic:claude-sonnet-5-5"
    assert settings.scoring.scorer_problems() == {}


# ─── History ────────────────────────────────────────────────────────────────


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.migrate(c)
    yield c
    c.close()


def test_migration_adds_model_change(conn):
    cols = [r[1] for r in conn.execute("PRAGMA table_info(model_change)")]
    assert cols == ["id", "at", "use", "field", "old_value", "new_value", "source"]
    with pytest.raises(Exception, match="CHECK"):
        conn.execute(
            "INSERT INTO model_change (at, use, field, source) VALUES ('x', 'screen', 'f', 'bad')"
        )


def test_history_records_each_changed_field_and_revert_writes_the_old_value(dirs, conn):
    _, mfile = dirs
    d = load_layers().digest
    d = mc.write_changes(mfile, {"runner": "api"}, d, conn=conn, source="ui", now=NOW).digest
    d = mc.write_changes(
        mfile, {"runner": "cli", "entailment_check": False}, d, conn=conn, source="cli", now=NOW
    ).digest
    rows = mc.history(conn)
    assert [(r["field"], r["old_value"], r["new_value"], r["source"]) for r in rows] == [
        ("apply.entailment_check", None, "false", "cli"),
        ("apply.runner", "api", "cli", "cli"),
        ("apply.runner", None, "api", "ui"),
    ]
    assert rows[0]["use"] == "check" and rows[1]["use"] == "drafting"
    # Revert the runner change: a new change with the old value, not a rewind.
    name, value = mc.revert_change(rows[1])
    assert (name, value) == ("runner", "api")
    mc.write_changes(mfile, {name: value}, d, conn=conn, source="revert", now=NOW)
    assert load_settings().apply.runner == "api"
    assert mc.history(conn)[0]["source"] == "revert"
    # Reverting a first save removes the key again (None: models.toml did not set it).
    name, value = mc.revert_change(rows[0])
    assert (name, value) == ("entailment_check", None)


def test_history_never_copies_a_non_model_value(dirs, conn):
    _, mfile = dirs
    mfile.write_text(f'[apply]\neffort = "{SENTINEL}"\n')
    d = load_layers().digest
    mc.write_changes(mfile, {"effort": "low"}, d, conn=conn, source="ui", now=NOW)
    row = mc.history(conn)[0]
    assert row["old_value"] == mc.INVALID_TEXT and "SENTINEL" not in str(tuple(row))


# ─── jobhunter models ───────────────────────────────────────────────────────


def test_models_set_and_show(dirs):
    out = cli.invoke(app, ["models", "set", "effort", "high"])
    assert out.exit_code == 0, out.output
    assert "saved apply.effort = high" in out.output
    out = cli.invoke(app, ["models", "show"])
    assert out.exit_code == 0, out.output
    line = next(ln for ln in out.output.splitlines() if ln.startswith("effort"))
    assert "high" in line and "[models.toml]" in line


def test_models_set_refuses_a_value_that_cannot_work(dirs):
    _, mfile = dirs
    out = cli.invoke(app, ["models", "set", "screen", "anthropic:claude-opus-5-5"])
    assert out.exit_code == 2 and "refused" in out.output
    out = cli.invoke(app, ["models", "set", "screen", "anthropic:claude-opus-5-5", "--force"])
    assert out.exit_code == 2  # --force never overrides a value that cannot work
    assert not mfile.exists()


def test_models_set_dearer_screening_needs_force(dirs):
    _, mfile = dirs
    out = cli.invoke(app, ["models", "set", "screen", "anthropic:claude-sonnet-5-5"])
    assert out.exit_code == 2 and "--force" in out.output and not mfile.exists()
    out = cli.invoke(app, ["models", "set", "screen", "anthropic:claude-sonnet-5-5", "--force"])
    assert out.exit_code == 0, out.output
    assert load_settings().scoring.screen_scorer == "anthropic:claude-sonnet-5-5"


def test_models_reset_group_and_reset_file(dirs):
    _, mfile = dirs
    assert cli.invoke(app, ["models", "set", "effort", "low"]).exit_code == 0
    assert cli.invoke(app, ["models", "set", "runner", "api"]).exit_code == 0
    out = cli.invoke(app, ["models", "reset", "drafting"])
    assert out.exit_code == 0, out.output
    assert load_layers().layer == {}
    mfile.write_text("[[[")
    out = cli.invoke(app, ["models", "set", "effort", "low"])
    assert out.exit_code == 1 and "reset-file" in out.output
    out = cli.invoke(app, ["models", "reset-file"])
    assert out.exit_code == 0 and "the old file is" in out.output
    assert not load_layers().broken


def test_models_catalog_makes_no_network_call(dirs):
    out = cli.invoke(app, ["models", "catalog"])
    assert out.exit_code == 0, out.output
    assert "anthropic:claude-haiku-5-5" in out.output and "per 1,000 jobs" in out.output


def test_paths_lists_models_toml(dirs):
    _, mfile = dirs
    out = cli.invoke(app, ["paths"])
    assert out.exit_code == 0, out.output
    assert f"models_file         missing  {mfile}  [under data_dir]" in out.output
