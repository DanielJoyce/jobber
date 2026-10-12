"""A model name that is hidden on screen but valid (a vLLM snapshot path with a 40-hex commit)
keeps working on /models: Save + re-score opens the panel, the confirm runs, history keeps the
value so Revert works while showing only a digest label, and no value is ever echoed
(bug 72a8a5e). Synthetic data; the same guards as test_models_rerun."""

from __future__ import annotations

import re
from html import unescape

import pytest
from test_models_rerun import (  # noqa: F401  (the autouse guards must be collected here too)
    _guards,
    confirm,
    make_client,
    requests_of,
    save,
    setup_data,
    token_of,
)

from jobhunter import model_choices as mc
from jobhunter.config import check_model_key, hidden_label, shown_value

COMMIT = "a1b2c3d4e5" * 4
HIDDEN = "local:/models/hub/models--Qwen--Qwen2.5-7B-Instruct/snapshots/" + COMMIT


def test_the_hidden_name_is_hidden_but_valid():
    assert shown_value(HIDDEN) is None
    assert check_model_key("scoring", "screen_scorer", HIDDEN) is None


def test_save_rerun_on_a_hidden_valid_model_opens_the_panel_without_echoing_it(tmp_path):
    cfg = setup_data(tmp_path, f'screen_scorer = "{HIDDEN}"\n')
    client = make_client(cfg)
    page = client.get("/models").text
    assert COMMIT not in page
    r = save(client, "screen", page, rerun="1")
    text = unescape(r.text)
    assert COMMIT not in text
    assert "cannot be shown here" not in text and 'id="rescore-form"' in text
    label = hidden_label(HIDDEN)
    assert f'name="saved" value="{label}"' in text
    token = token_of(text)
    ok = confirm(client, token, saved=label, scope="recent")
    assert COMMIT not in ok.text and "cannot be shown here" not in ok.text
    client.app.state.rescore.join()
    assert len(requests_of(client)) == 1
    # A stale label does not run.
    other = confirm(client, token, saved=hidden_label(HIDDEN + "x"), scope="recent")
    assert "changed since" in other.text and COMMIT not in other.text


def test_history_keeps_a_hidden_value_for_revert_and_shows_a_label(tmp_path):
    cfg = setup_data(tmp_path)
    client = make_client(cfg)
    conn = client.app.state.conn_factory()
    try:
        with conn:
            conn.execute(
                "INSERT INTO model_change (at, use, field, old_value, new_value, source) "
                "VALUES ('2026-10-10T01:00:00Z', 'screen', 'scoring.screen_scorer', ?, ?, 'ui')",
                (mc.to_text(HIDDEN), mc.to_text("local:other-model-7b")),
            )
        row = conn.execute("SELECT * FROM model_change").fetchone()
        assert row["old_value"] == HIDDEN  # stored so Revert can write it back
        name, value = mc.revert_change(dict(row))
        assert (name, value) == ("screen", HIDDEN)
        shown = mc.masked_history([row])[0]
        assert shown["old_value"] == hidden_label(HIDDEN)
        assert shown["new_value"] == "local:other-model-7b"
    finally:
        conn.close()
    assert COMMIT not in client.get("/models").text


def test_a_refused_value_is_still_stored_as_invalid():
    assert mc.to_text("openrouter:sk-or-v1-SENTINEL0123456789abcdef") == mc.INVALID_TEXT


@pytest.mark.parametrize("key", ["api_model", "api_entailment_model"])
def test_the_reason_never_quotes_a_hidden_value(key):
    hidden = "AKIA" + "EXAMPLEMODEL1234" + "xyz" * 12  # hidden (long segment), not refused
    assert shown_value(hidden) is None
    msg = check_model_key("apply", key, hidden)
    assert msg is not None and hidden not in msg


def test_the_page_never_prints_the_commit_in_any_form(tmp_path):
    cfg = setup_data(tmp_path, f'screen_scorer = "{HIDDEN}"\n')
    client = make_client(cfg)
    for url in ("/models", "/models/screen/rescore"):
        assert re.search(COMMIT, client.get(url).text) is None
