"""The worker's sender policy (specs/022 "Sender policy"): ``MESSAGE_POLICY`` in
``extension/background.js`` names, per message type, the sender kinds that may send it.

The table is parsed from the source here, so the test is about the deciding code's data: no
spend or write type, and no mode or pairing type, may list a content script; every handler is in
the table; and every type in the table has a handler.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

EXT = Path(__file__).resolve().parents[1] / "extension"
SOURCE = (EXT / "background.js").read_text(encoding="utf-8")

CONTENT_KINDS = {"ats", "generic"}
PAGE_ONLY = {  # spend, write, mode, pairing and capture types: extension pages only
    "draft/generate", "draft/estimate", "fill/write", "file/attach", "text/insert", "ats/mode",
    "session/start", "match/pick", "packet/ready", "pair", "unpair", "pairStatus", "open",
    "again", "acceptNotice", "add", "describe", "link", "fetch", "estimate", "status",
    "prepare", "score", "openConsole", "switches/get", "switches/set", "panel/state",
}  # fmt: skip
GENERIC_ALLOWED = {"frame/hello", "forms/get", "frame/bye"}


def policy() -> dict[str, list[str]]:
    block = SOURCE.split("const MESSAGE_POLICY = deepFreeze({", 1)[1].split("\n});", 1)[0]
    return json.loads("{" + block + "}")


def handler_types() -> set[str]:
    """Real registrations only: statements at the start of a line (a comment or a string that
    merely mentions ``HANDLERS["x"] =`` does not count)."""
    names = set(re.findall(r'^HANDLERS\["([^"]+)"\]\s*=', SOURCE, re.M))
    for body, after in re.findall(r"^for \(const name of \[([^\]]*)\]\) \{\n(.*)", SOURCE, re.M):
        if after.lstrip().startswith("HANDLERS[name] ="):
            names |= set(re.findall(r'"([^"]+)"', body))
    return names


def test_policy_parses_and_uses_only_known_kinds():
    table = policy()
    assert table
    for kinds in table.values():
        assert kinds and set(kinds) <= {"ats", "generic", "page"}


@pytest.mark.parametrize("type_", sorted(PAGE_ONLY))
def test_spend_write_mode_and_pairing_types_never_allow_a_content_script(type_):
    assert policy()[type_] == ["page"]


def test_no_page_only_type_is_missing_from_the_table():
    assert set(policy()) >= PAGE_ONLY


def test_generic_frames_get_only_hello_forms_and_bye():
    allowed = {t for t, kinds in policy().items() if "generic" in kinds}
    assert allowed == GENERIC_ALLOWED


def test_ats_content_scripts_get_no_spend_or_write_type():
    allowed = {t for t, kinds in policy().items() if CONTENT_KINDS & set(kinds)}
    assert allowed == {
        "frame/hello", "forms/get", "frame/bye", "panel/open", "submit/attempt",
        "submit/outcome", "ask/answer", "applied/undo",
    }  # fmt: skip
    for t in allowed:
        assert not re.search(r"generate|write|attach|insert|mode|pair|capture|score", t), t


def test_every_handler_is_in_the_table_and_every_type_has_one():
    table = set(policy())
    handlers = handler_types()
    assert handlers - table == set(), "a handler exists for a type not in MESSAGE_POLICY"
    assert table - handlers == set(), "a type in MESSAGE_POLICY has no handler"


def test_the_listener_refuses_a_type_outside_the_table():
    # the first thing the onMessage listener does is the own-property check on the table
    listener = SOURCE.split("chrome.runtime.onMessage.addListener(", 1)[1]
    assert "Object.hasOwn(MESSAGE_POLICY, msg.type)" in listener.split("allowed", 1)[0]


def code_without_comments() -> str:
    text = re.sub(r"/\*.*?\*/", "", SOURCE, flags=re.S)
    return "\n".join(ln for ln in text.split("\n") if not ln.lstrip().startswith("//"))


def test_the_table_is_frozen_and_read_only_by_the_listener():
    assert "const MESSAGE_POLICY = deepFreeze({" in SOURCE
    code = code_without_comments()
    start = code.index("chrome.runtime.onMessage.addListener(")
    end = code.index("\nfunction onMenu")
    definition = code.index("const MESSAGE_POLICY = ") + len("const ")
    uses = [m.start() for m in re.finditer(r"\bMESSAGE_POLICY\b", code)]
    outside = [u for u in uses if u != definition and not start < u < end]
    assert outside == [], "MESSAGE_POLICY is used outside its definition and the listener"


def test_the_handler_registry_counts_only_real_registrations():
    found = handler_types()
    assert {"forms/get", "pair", "draft/generate", "estimate"} <= found
    assert "bogus/type" not in found
