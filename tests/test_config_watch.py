"""Config watch core (specs/019 Addendum B, S12a): classification, detection, value-free errors."""

from __future__ import annotations

import os
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError, field_validator

from jobhunter import config
from jobhunter import config_watch as cw
from jobhunter.config import Settings, load_settings

SENTINEL = "zqxSENTINELqxz"


# --- classification ----------------------------------------------------------------------------


def test_every_settings_leaf_is_classified_exactly_once():
    leaves = config.leaf_fields()
    assert len(leaves) == len(set(leaves))
    for leaf in leaves:
        hits = (leaf in config.RELOADABLE) + (leaf in config.RESTART_FIELDS)
        assert hits == 1, f"{leaf} is classified {hits} times; add it to RELOADABLE or RESTART"
    assert set(leaves) == config.RELOADABLE | config.RESTART_FIELDS


def test_allowlists_name_real_fields_and_paths_are_never_shown():
    leaves = set(config.leaf_fields())
    assert leaves >= config.SHOWN_VALUES
    assert leaves >= config.SHOWN_AS_COUNT
    assert config.NAME_VALUED <= config.RESTART_FIELDS
    personal = {
        "fetch.user_agent_contact",
        "mail.alerts_address",
        "mail.fallback_sender_domains",
        "mail.fallback_senders",
        "capture.disabled_hosts",
        "scoring.openrouter.provider",
        "scoring.openrouter.title",
        "scoring.openrouter.referer",
    }
    assert not personal & config.SHOWN_VALUES
    assert not {f for f in config.SHOWN_VALUES if f.startswith("paths.") or "url" in f}


def test_spec_table_classes():
    for f in ("paths.db_path", "console.port", "fetch.global_concurrency"):
        assert config.reload_class(f) == "restart"
    for f in ("scoring.openrouter.api_key_env", "scoring.openrouter.catalog_cache"):
        assert config.reload_class(f) == "restart"
    for f in ("scoring.weekly_cap_usd", "apply.runner", "capture.disabled_hosts", "mail.label"):
        assert config.reload_class(f) == "reload"
    assert config.reload_class("no.such.field") == "restart"  # new fields default to restart


# --- custom validators and error text ----------------------------------------------------------


def _models(model: type[BaseModel] = Settings) -> list[type[BaseModel]]:
    out = [model]
    for info in model.model_fields.values():
        ann = info.annotation
        if isinstance(ann, type) and issubclass(ann, BaseModel):
            out += _models(ann)
    return out


def _validators() -> list[tuple[str, str]]:
    found = []
    for model in _models():
        decos = model.__pydantic_decorators__
        for kind in ("validators", "field_validators", "root_validators", "model_validators"):
            for name in getattr(decos, kind):
                found.append((model.__name__, name))
    return sorted(set(found))


# Reviewed: neither raises with the input in its message. A new validator fails this test until
# someone checks its messages against the value-free rule (validation_lines hides any
# value_error message for a field not on SHOWN_VALUES anyway).
REVIEWED_VALIDATORS = [("Paths", "_resolve_defaults"), ("Settings", "_derive_catalog_cache")]


def test_custom_validators_are_reviewed():
    assert _validators() == sorted(REVIEWED_VALIDATORS)


@pytest.mark.parametrize("model, name", _validators())
def test_a_custom_validator_message_quoting_input_is_hidden(model, name):
    # Stand in for each validator: one that quotes its input, on a field not on SHOWN_VALUES
    # and on one that is.
    class Fake(BaseModel):
        user_agent_contact: str = ""

        @field_validator("user_agent_contact")
        @classmethod
        def _quote(cls, v: str) -> str:
            raise ValueError(f"{model}.{name} rejects {v!r}")

    with pytest.raises(ValidationError) as info:
        Fake(user_agent_contact=SENTINEL)
    hidden = cw.validation_lines(info.value)
    assert hidden == ["user_agent_contact: invalid value"]
    assert SENTINEL not in str(hidden)


def test_value_error_on_shown_field_keeps_its_message(monkeypatch):
    class Fake(BaseModel):
        label: str = ""

        @field_validator("label")
        @classmethod
        def _quote(cls, v: str) -> str:
            raise ValueError("label must not be empty")

    monkeypatch.setattr(config, "SHOWN_VALUES", frozenset({"label"}))
    with pytest.raises(ValidationError) as info:
        Fake(label="")
    assert cw.validation_lines(info.value) == ["label: Value error, label must not be empty"]


def _bad_values(leaf: str) -> list[Any]:
    return [f"{SENTINEL}", {"x": SENTINEL}, [SENTINEL]]


def _toml(leaf: str, value: Any) -> str:
    *section, key = leaf.split(".")

    def lit(v: Any) -> str:
        if isinstance(v, dict):
            return "{ " + ", ".join(f'{k} = "{x}"' for k, x in v.items()) + " }"
        if isinstance(v, list):
            return "[" + ", ".join(f'"{x}"' for x in v) + "]"
        return f'"{v}"'

    return f"[{'.'.join(section)}]\n{key} = {lit(value)}\n"


@pytest.mark.parametrize("leaf", config.leaf_fields())
def test_a_bad_value_in_any_field_never_reaches_the_error_text(tmp_path, leaf):
    """For every leaf, some wrong-typed value holding the sentinel fails validation, and no
    error line carries it."""
    path = tmp_path / "config.toml"
    failed = False
    for value in _bad_values(leaf):
        path.write_text(_toml(leaf, value))
        _, errors = cw.load_errors(lambda: load_settings(path))
        assert SENTINEL not in " ".join(errors)
        failed = failed or bool(errors)
    assert failed, f"no bad value made {leaf} fail; extend _bad_values"


def test_unknown_key_that_looks_like_a_secret_is_not_echoed(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(f'[fetch]\n"sk-ant-{SENTINEL}" = 1\nbogus = 2\n')
    _, errors = cw.load_errors(lambda: load_settings(path))
    text = " ".join(errors)
    assert SENTINEL not in text
    assert "fetch.bogus" in text and "(a key not shown)" in text


def test_toml_error_gives_line_and_column_only(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(f"[fetch]\nuser_agent_contact = {SENTINEL}\n")
    _, errors = cw.load_errors(lambda: load_settings(path))
    assert errors == ("config.toml line 2, column 22: not valid TOML",)


# --- diff, describe, partial -------------------------------------------------------------------


def _settings(**sections: dict[str, Any]) -> Settings:
    return Settings.model_validate({"paths": {"data_dir": "/tmp/jh-a"}, **sections})


def test_diff_shows_only_allowlisted_values():
    a = _settings()
    b = _settings(
        fetch={"user_agent_contact": SENTINEL, "default_rps": 0.5},
        capture={"disabled_hosts": [SENTINEL, "b.example"]},
        scoring={"openrouter": {"referer": SENTINEL, "api_key_env": f"sk-{SENTINEL}"}},
        mail={"alerts_address": SENTINEL},
    )
    changes = {c.field: c for c in cw.diff(a, b)}
    assert changes["fetch.default_rps"].old == "0.2" and changes["fetch.default_rps"].new == "0.5"
    assert changes["fetch.user_agent_contact"].note == "changed"
    assert changes["capture.disabled_hosts"].note == "changed (2 entries, was 0)"
    assert changes["scoring.openrouter.api_key_env"].note == "changed (not shown)"
    assert changes["scoring.openrouter.api_key_env"].cls == "restart"
    assert SENTINEL not in repr(list(changes.values()))


def test_api_key_env_names_are_shown_when_both_look_like_names():
    a = _settings()
    b = _settings(scoring={"openrouter": {"api_key_env": "MY_ROUTER_KEY"}})
    (change,) = cw.diff(a, b)
    assert (change.old, change.new) == ("OPENROUTER_API_KEY", "MY_ROUTER_KEY")


def test_command_line_override_makes_a_port_change_no_effect():
    a = _settings()
    b = _settings(console={"port": 9999, "public_port": 9000})
    changes = {c.field: c.cls for c in cw.diff(a, b, overrides={"console.port"})}
    assert changes == {"console.port": "no-effect", "console.public_port": "restart"}


def test_partial_keeps_every_restart_field():
    a = _settings()
    b = _settings(
        paths={"data_dir": "/tmp/jh-b"},
        console={"port": 9999},
        fetch={"global_concurrency": 2, "default_rps": 1.0},
        scoring={
            "weekly_cap_usd": 99.0,
            "openrouter": {"api_key_env": "OTHER", "catalog_cache": "/x.json"},
        },
    )
    new = cw.partial(a, b)
    for leaf in config.leaf_fields():
        expected = b if config.reload_class(leaf) == "reload" else a
        assert config.field_value(new, leaf) == config.field_value(expected, leaf), leaf
    assert new.paths is a.paths
    assert config.field_value(b, "fetch.global_concurrency") == 2  # b itself is untouched


# --- .env --------------------------------------------------------------------------------------


def test_env_diff_name_rules():
    before = {"ANTHROPIC_API_KEY": "d1", "LOWER_unknown": "d2", "JOBHUNTER_X": "d3"}
    after = {
        "ANTHROPIC_API_KEY": "d9",  # changed, known
        "JOBHUNTER_X": "d3",
        "OPENROUTER_API_KEY": "d4",  # added, known
        "SOME_UNKNOWN": "d5",  # added, not known: counted
        f"sk-ant-{SENTINEL}": None,  # a pasted key on a bare line: counted
        "JOBHUNTER_NEW": "d6",
    }
    change = cw.env_diff(
        Path("/home/u/.env"), before, after, ["ANTHROPIC_API_KEY", "OPENROUTER_API_KEY"],
        home=Path("/home/u"),
    )  # fmt: skip
    assert change is not None
    assert change.file == "~/.env"
    assert change.changed == ("ANTHROPIC_API_KEY",)
    assert change.added == ("JOBHUNTER_NEW", "OPENROUTER_API_KEY")
    assert change.removed == ()
    assert change.hidden == 2  # SOME_UNKNOWN added, LOWER_unknown removed
    assert change.bare == 1
    assert SENTINEL not in repr(change)


# --- detection ---------------------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class Stat:
    """os.stat with optional per-path attribute overrides, counting calls."""

    def __init__(self) -> None:
        self.calls = 0
        self.patch: dict[Path, dict[str, Any]] = {}

    def __call__(self, path: Path) -> Any:
        self.calls += 1
        st = os.stat(path)
        fields = {
            k: getattr(st, k)
            for k in ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns", "st_nlink")
        }
        fields["st_mode"] = st.st_mode
        fields.update(self.patch.get(Path(path), {}))
        return SimpleNamespace(**fields)


@pytest.fixture
def env(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text("[scoring]\nweekly_cap_usd = 10.0\n# a comment\n")
    home_env = tmp_path / "home.env"
    clock, stat = Clock(), Stat()
    loads: list[Path] = []

    def loader(path: Path) -> Settings:
        loads.append(path)
        return load_settings(path)

    def make(container_mode: bool = False, **kw: Any) -> cw.ConfigWatcher:
        # Explicit: a CLI test that ran `console` leaves the module-wide snapshot behind.
        kw.setdefault("start_env", {home_env: {}})
        return cw.ConfigWatcher(
            config_path=lambda: cfg,
            env_candidates=lambda: [home_env],
            loader=loader,
            stat=stat,
            clock=clock,
            container_mode=container_mode,
            known_names=lambda s: ["ANTHROPIC_API_KEY"],
            **kw,
        )

    running = load_settings(cfg)
    return SimpleNamespace(
        cfg=cfg, home_env=home_env, clock=clock, stat=stat, loads=loads, make=make, running=running
    )


def tick(env, s: float = 3.0) -> None:
    env.clock.t += s


def test_first_check_diffs_without_a_baseline_and_sees_an_early_edit(env):
    w = env.make()
    env.cfg.write_text("[scoring]\nweekly_cap_usd = 25.0\n")  # before the first poll
    state = w.check(env.running)
    assert [f.field for f in state.fields] == ["scoring.weekly_cap_usd"]
    assert state.can_apply and not state.needs_restart


def test_a_touch_or_a_comment_edit_shows_nothing(env):
    w = env.make()
    assert not w.check(env.running).pending
    n = len(env.loads)
    os.utime(env.cfg, ns=(1, 1))
    tick(env)
    assert not w.check(env.running).pending
    assert len(env.loads) == n  # same bytes: not even loaded again
    env.cfg.write_text("[scoring]\nweekly_cap_usd = 10.0\n# another comment\n")
    tick(env)
    assert not w.check(env.running).pending


def test_a_rename_save_on_the_host_is_seen(env):
    w = env.make()
    w.check(env.running)
    new = env.cfg.with_name("config.toml.tmp")
    new.write_text("[scoring]\nweekly_cap_usd = 30.0\n")
    os.replace(new, env.cfg)
    tick(env)
    state = w.check(env.running)
    assert [f.field for f in state.fields] == ["scoring.weekly_cap_usd"]
    assert not state.replaced


def test_container_unlinked_inode_gives_the_cannot_see_notice(env):
    w = env.make(container_mode=True)
    w.check(env.running)
    env.stat.patch[env.cfg] = {"st_nlink": 0}
    tick(env)
    state = w.check(env.running)
    assert state.replaced and state.needs_restart and not state.can_apply


def test_container_backup_save_ctime_only_gives_the_notice(env):
    w = env.make(container_mode=True)
    w.check(env.running)
    st = os.stat(env.cfg)
    env.stat.patch[env.cfg] = {"st_ctime_ns": st.st_ctime_ns + 10_000}
    tick(env)
    assert w.check(env.running).replaced


def test_container_chmod_and_touch_do_not_fire(env):
    w = env.make(container_mode=True)
    w.check(env.running)
    st = os.stat(env.cfg)
    env.stat.patch[env.cfg] = {"st_ctime_ns": st.st_ctime_ns + 10_000, "st_mode": st.st_mode ^ 0o4}
    tick(env)
    assert not w.check(env.running).pending  # chmod
    env.stat.patch.clear()
    os.utime(env.cfg, ns=(5, 5))
    tick(env)
    assert not w.check(env.running).pending  # touch


def test_host_mode_ignores_a_ctime_only_change(env):
    w = env.make(container_mode=False)
    w.check(env.running)
    env.stat.patch[env.cfg] = {"st_nlink": 0}
    tick(env)
    assert not w.check(env.running).replaced


def test_a_new_home_env_is_seen(env):
    w = env.make(start_env={env.home_env: {}})
    assert not w.check(env.running).pending
    env.home_env.write_text("ANTHROPIC_API_KEY=placeholder\nOTHER_THING=1\n")
    tick(env)
    state = w.check(env.running)
    (change,) = state.env
    assert change.added == ("ANTHROPIC_API_KEY",) and change.hidden == 1
    assert state.needs_restart and not state.can_apply


def test_env_reverted_to_the_start_state_shows_nothing(env):
    env.home_env.write_text("ANTHROPIC_API_KEY=a\n")
    w = env.make(start_env={env.home_env: cw.read_env_digests(env.home_env)})
    env.home_env.write_text("ANTHROPIC_API_KEY=b\n")
    assert w.check(env.running).env[0].changed == ("ANTHROPIC_API_KEY",)
    env.home_env.write_text("ANTHROPIC_API_KEY=a\n")
    os.utime(env.home_env, ns=(7, 7))
    tick(env)
    assert not w.check(env.running).pending


def test_throttle_holds_with_three_pollers(env):
    w = env.make()
    w.check(env.running)
    tick(env)
    before = env.stat.calls
    threads = [threading.Thread(target=w.check, args=(env.running,)) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert env.stat.calls - before == 3  # one round: config.toml, models.toml, one .env
    env.clock.t += 1.0  # still inside 2 s of that round
    w.check(env.running)
    assert env.stat.calls - before == 3
    w.check(env.running, force=True)
    assert env.stat.calls - before == 6


def test_invalid_file_keeps_old_settings_and_dismiss_remembers_by_hash(env):
    w = env.make()
    env.cfg.write_text(f'[scoring]\nweekly_cap_usd = "{SENTINEL}"\n')
    state = w.check(env.running)
    assert state.errors and not state.can_apply
    assert SENTINEL not in " ".join(state.errors)
    w.dismiss(state.key)
    tick(env)
    assert w.is_dismissed(w.check(env.running))
    env.cfg.write_text("[scoring]\nweekly_cap_usd = 11.0\n")
    tick(env)
    assert not w.is_dismissed(w.check(env.running))


def test_reload_table_marks_each_table():
    table = cw.reload_table()
    assert "| `[paths]` | Restart the console |" in table
    assert "`[fetch]` | Apply now (reloads in place); except `global_concurrency`" in table


# --- review fixes: coupled scorer sections, missing file, key-shaped values, models.toml -------


@pytest.mark.parametrize("section", ["openai_compat", "openrouter", "local"])
def test_an_endpoint_change_alone_never_reloads(section):
    """A key resolved at start (a podman secret's *_FILE, the process environment) can rotate
    unseen, so a new host must never get the running key: an endpoint is a restart field."""
    a = _settings(scoring={section: {"base_url": "https://provider-a.example/v1"}})
    b = _settings(scoring={section: {"base_url": "https://provider-b.example/v1"}})
    (url,) = cw.diff(a, b)
    assert url.cls == "restart"
    assert cw.partial(a, b) == a
    state = cw.State(fields=(url,), new_settings=b)
    assert not state.can_apply and state.needs_restart


def test_local_runtime_picks_an_endpoint_and_needs_a_restart():
    a = _settings()
    b = _settings(scoring={"local": {"runtime": "ollama"}})
    assert [c.cls for c in cw.diff(a, b)] == ["restart"]
    assert cw.partial(a, b).scoring.local.runtime == "llama.cpp"


@pytest.mark.parametrize("section", ["openai_compat", "openrouter", "local"])
def test_a_section_whose_key_name_changes_is_held_whole(section):
    a = _settings()
    b = _settings(scoring={section: {"max_concurrency": 4, "api_key_env": "PROVIDER_B_KEY"}})
    changes = {c.field: c for c in cw.diff(a, b)}
    held = changes[f"scoring.{section}.max_concurrency"]
    assert held.cls == "restart" and held.held == cw.HELD_KEY
    assert held.class_text == "needs restart (held with its API key)"
    assert getattr(cw.partial(a, b).scoring, section) == getattr(a.scoring, section)


def test_a_pending_env_change_holds_every_scorer_section():
    a = _settings()
    b = _settings(scoring={"openrouter": {"max_concurrency": 4}}, fetch={"default_rps": 0.5})
    (held,) = [c for c in cw.diff(a, b, env_pending=True) if c.field.startswith("scoring.")]
    assert held.cls == "restart"
    new = cw.partial(a, b, env_pending=True)
    assert new.scoring.openrouter.max_concurrency == 2
    assert new.fetch.default_rps == 0.5  # unrelated reloads still apply
    assert cw.partial(a, b).scoring.openrouter.max_concurrency == 4  # nothing pending


def test_watcher_holds_the_endpoint_while_env_changes(env):
    w = env.make()
    env.cfg.write_text('[scoring.openai_compat]\nbase_url = "https://provider-b.example/v1"\n')
    env.home_env.write_text("OPENAI_COMPAT_API_KEY=placeholder\n")
    state = w.check(env.running)
    assert state.env and not state.can_apply
    assert [f.cls for f in state.fields] == ["restart"]


def test_deleted_or_emptied_config_is_an_error_not_a_change_to_defaults(env):
    env.cfg.write_text("[scoring]\ndaily_cap_usd = 0.25\n")
    running = load_settings(env.cfg)
    w = env.make()
    env.cfg.unlink()
    state = w.check(running)
    assert state.errors == ("config.toml is missing; save it again",)
    assert not state.can_apply and not state.fields
    env.cfg.write_text("")
    tick(env)
    state = w.check(running)
    assert state.errors == ("config.toml is empty; save it again",) and not state.can_apply
    env.cfg.write_text("[scoring]\ndaily_cap_usd = 0.25\n")
    tick(env)
    assert not w.check(running).pending


def test_key_shaped_scorer_spec_is_not_shown_and_its_warning_is_named_only():
    a = _settings()
    b = _settings(
        scoring={"screen_scorer": f"anthropic:sk-ant-api03-{SENTINEL}"},
        apply={"api_model": f"sk-ant-api03-{SENTINEL}"},
    )
    changes = cw.diff(a, b)
    assert {c.note for c in changes} == {"changed (not shown)"}
    warnings = cw.scorer_warnings(b)
    assert warnings == ("screen_scorer: not a usable scorer spec (the value is not shown)",)
    assert SENTINEL not in repr(changes) + repr(warnings)


def test_apply_forgets_old_dismissals(env):
    from datetime import datetime

    w = env.make()
    env.cfg.write_text("[scoring]\nweekly_cap_usd = 20.0\n")
    first = w.check(env.running)
    w.dismiss(first.key)
    env.cfg.write_text("[scoring]\nweekly_cap_usd = 30.0\n")
    tick(env)
    applied = load_settings(env.cfg)
    w.mark_applied(1, datetime.now())
    env.cfg.write_text("[scoring]\nweekly_cap_usd = 20.0\n")  # the dismissed bytes again
    tick(env)
    state = w.check(applied)
    assert state.key == first.key and state.fields
    assert not w.is_dismissed(state)


def test_models_toml_is_watched_and_listed_under_its_name(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'[paths]\ndata_dir = "{data}"\n[scoring]\nweekly_cap_usd = 10.0\n')
    running = load_settings(cfg)
    clock = Clock()
    w = cw.ConfigWatcher(
        config_path=lambda: cfg, env_candidates=lambda: [], clock=clock,
        container_mode=False, start_env={},
    )  # fmt: skip
    assert not w.check(running).pending
    (data / "models.toml").write_text('[scoring]\nscreen_scorer = "local:qwen3"\n')
    clock.t += 3
    state = w.check(running)
    (change,) = state.fields
    assert change.field == "scoring.screen_scorer" and change.source == "models.toml"
    # An unrelated config.toml edit applied later keeps models.toml's value.
    cfg.write_text(f'[paths]\ndata_dir = "{data}"\n[scoring]\nweekly_cap_usd = 12.0\n')
    clock.t += 3
    state = w.check(running)
    new = cw.partial(running, state.new_settings)
    assert new.scoring.screen_scorer == "local:qwen3"
    assert new.scoring.weekly_cap_usd == 12.0


def _data_config(tmp_path, name: str, models: str = "") -> Path:
    data = tmp_path / name
    data.mkdir(exist_ok=True)
    if models:
        (data / "models.toml").write_text(models)
    cfg = tmp_path / f"{name}.toml"
    cfg.write_text(f'[paths]\ndata_dir = "{data}"\n')
    return cfg


def test_a_data_dir_change_holds_the_model_keys_and_the_running_layers(tmp_path):
    a = load_settings(_data_config(tmp_path, "a", '[scoring]\nscreen_scorer = "local:qwen3"\n'))
    b = load_settings(_data_config(tmp_path, "b", '[scoring]\nscreen_scorer = "local:other"\n'))
    changes = {c.field: c for c in cw.diff(a, b)}
    held = changes["scoring.screen_scorer"]
    assert held.cls == "restart" and held.held == cw.HELD_DATA_DIR
    new = cw.partial(a, b)
    assert new.scoring.screen_scorer == "local:qwen3"
    assert new._models is a._models  # the running models.toml layers, not the new folder's


def test_a_comment_only_config_counts_as_empty(env):
    env.cfg.write_text("[scoring]\ndaily_cap_usd = 0.25\n")
    running = load_settings(env.cfg)
    w = env.make()
    env.cfg.write_text("# everything commented out\n   \n")
    state = w.check(running)
    assert state.errors == ("config.toml is empty; save it again",) and not state.can_apply


def test_a_comment_only_config_at_start_is_not_an_error(env):
    env.cfg.write_text("# nothing yet\n")
    running = load_settings(env.cfg)
    w = env.make()
    env.cfg.write_text("# still nothing\n")
    assert not w.check(running).pending


def test_an_invalid_models_key_does_not_claim_a_config_edit(tmp_path):
    cfg = _data_config(tmp_path, "d", '[apply]\nrunner = "bogus"\n')
    running = load_settings(cfg)
    clock = Clock()
    w = cw.ConfigWatcher(
        config_path=lambda: cfg, env_candidates=lambda: [], clock=clock,
        container_mode=False, start_env={},
    )  # fmt: skip
    cfg.write_text(cfg.read_text() + '[apply]\nrunner = "api"\n')
    (change,) = w.check(running).fields
    assert change.field == "apply.runner" and change.source == "config.toml"
