"""Lint of the jobhunter Chrome extension (specs/017 phase 1e "Tests (1e)": Extension lint).

No JavaScript toolchain: a small lexer here strips comments, strings, template literals and
regular expressions so the rules look at code only.

- manifest: exactly the 1e permissions and the console host, pinned key, no content scripts,
  no web-accessible resources or external connections, ``_execute_action`` on Alt+Shift+J;
- ``capture/extract.js``: one self-invoking expression with no top-level declaration, and none
  of the DOM-writing, event, timer or network APIs (it reads the page and nothing else);
- everywhere: no HTML sinks, no ``eval``, no external messaging, no ``chrome.debugger``;
- the worker restricts storage to trusted contexts and never adds a ``link`` menu context.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from pathlib import Path

import pytest

from jobhunter.apply.ext_pairing import EXTENSION_ID
from jobhunter.pipeline.board_ids import _INDEED_HOST, _LINKEDIN_HOST

EXT = Path(__file__).resolve().parents[1] / "extension"
MANIFEST = json.loads((EXT / "manifest.json").read_text(encoding="utf-8"))
JS_FILES = sorted(EXT.rglob("*.js"))
HTML_FILES = sorted(EXT.rglob("*.html"))

PERMISSIONS = {"activeTab", "scripting", "storage", "contextMenus"}
FORBIDDEN_PERMISSIONS = {
    "<all_urls>", "tabs", "webNavigation", "cookies", "webRequest", "declarativeNetRequest",
    "debugger", "tabCapture", "desktopCapture", "downloads", "history", "clipboardRead",
    "notifications", "sidePanel",
}  # fmt: skip
FORBIDDEN_KEYS = {
    "content_scripts", "web_accessible_resources", "externally_connectable",
    "optional_host_permissions", "optional_permissions",
}  # fmt: skip

# ─── a minimal JavaScript lexer ─────────────────────────────────────────────

_REGEX_AFTER = set("(,=:[!&|?{};+-*%<>~^")
_REGEX_KEYWORDS = {"return", "typeof", "case", "in", "of", "new", "delete", "void", "throw"}


def strip_js(src: str) -> str:
    """Code with comments, strings, templates and regex literals blanked (kept: structure)."""
    out: list[str] = []
    i, n = 0, len(src)
    prev = ""  # last significant character, or the last identifier word
    stack: list[int] = []  # brace depths at which a template's ${ } closes

    def scan_template(j: int) -> int:
        """From after the opening backtick to after the closing one, recursing into ${}."""
        nonlocal i
        while j < n:
            ch = src[j]
            if ch == "\\":
                j += 2
                continue
            if ch == "`":
                return j + 1
            if src.startswith("${", j):
                out.append('"" + (')
                i = j + 2
                code_until_close()
                out.append(') + ""')
                j = i
                continue
            j += 1
        raise ValueError("unterminated template literal")

    def code_until_close() -> None:
        nonlocal i, prev
        depth = 0
        while i < n:
            if src[i] == "}" and depth == 0:
                i += 1
                return
            if src[i] == "{":
                depth += 1
            elif src[i] == "}":
                depth -= 1
            step()
        raise ValueError("unterminated ${")

    def step() -> None:
        nonlocal i, prev
        c = src[i]
        if src.startswith("//", i):
            j = src.find("\n", i)
            i = n if j < 0 else j
            out.append(" ")
        elif src.startswith("/*", i):
            j = src.find("*/", i + 2)
            if j < 0:
                raise ValueError("unterminated comment")
            i = j + 2
            out.append(" ")
        elif c in "'\"":
            j = i + 1
            while j < n and src[j] != c:
                if src[j] == "\\":
                    j += 1
                elif src[j] == "\n":
                    raise ValueError("newline in a string")
                j += 1
            i = j + 1
            out.append('""')
            prev = '"'
        elif c == "`":
            i = scan_template(i + 1)
            out.append('""')
            prev = '"'
        elif c == "/" and (prev == "" or prev in _REGEX_AFTER or prev in _REGEX_KEYWORDS):
            j, in_class = i + 1, False
            while j < n:
                ch = src[j]
                if ch == "\\":
                    j += 2
                    continue
                if ch == "[":
                    in_class = True
                elif ch == "]":
                    in_class = False
                elif ch == "/" and not in_class:
                    break
                elif ch == "\n":
                    raise ValueError("newline in a regex literal")
                j += 1
            j += 1
            while j < n and src[j].isalpha():
                j += 1
            i = j
            out.append("/r/")
            prev = "r"
        elif c.isalpha() or c in "_$":
            m = re.match(r"[A-Za-z_$][\w$]*", src[i:])
            assert m is not None
            out.append(m.group(0))
            prev = m.group(0)
            i += len(m.group(0))
        else:
            out.append(c)
            if not c.isspace():
                prev = c
            i += 1

    while i < n:
        step()
    del stack
    return "".join(out)


def top_level_statements(code: str) -> list[str]:
    """Statements at depth 0 of stripped code (split on ';' and on closing a block)."""
    parts: list[str] = []
    depth, start = 0, 0
    for k, ch in enumerate(code):
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == ";" and depth == 0:
            parts.append(code[start:k])
            start = k + 1
    parts.append(code[start:])
    return [p.strip() for p in parts if p.strip()]


def single_iife(code: str) -> bool:
    """One expression statement: ``(() => { ... })()``, nothing else at the top level."""
    stmts = top_level_statements(code)
    if len(stmts) != 1:
        return False
    s = re.sub(r"\s+", " ", stmts[0])
    if not re.match(r"^\( ?\( ?\) ?=> ?\{", s):
        return False
    depth = 0
    for k, ch in enumerate(s):
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth == 0:
                return re.fullmatch(r" ?\( ?\) ?", s[k + 1 :]) is not None
    return False


DECLARATION = re.compile(r"^(?:const|let|var|function|class|async\s+function|import|export)\b")

CAPTURE_DENY = [
    r"\bappendChild\b", r"\.append\s*\(", r"\.prepend\s*\(", r"\.before\s*\(",
    r"\.after\s*\(", r"\binsertBefore\b", r"\binsertAdjacent\w*", r"\.remove\s*\(",
    r"\bremoveChild\b", r"\breplaceChild\b", r"\breplaceChildren\b", r"\breplaceWith\b",
    r"\bsetAttribute\b", r"\bremoveAttribute\b", r"\btoggleAttribute\b",
    r"\.(?:textContent|innerText|nodeValue|data|value|checked|hidden)\s*(?:[-+*/]?=)(?!=)",
    r"\bclassList\b", r"\bstyle\s*\.", r"\baddEventListener\b", r"\bsetTimeout\b",
    r"\bsetInterval\b", r"\bMutationObserver\b", r"\.click\s*\(", r"\bfocus\s*\(",
    r"\bscrollIntoView\b", r"\bremoveAllRanges\b", r"\bdispatchEvent\b", r"\bfetch\b",
    r"\bXMLHttpRequest\b", r"\bWebSocket\b", r"\bEventSource\b", r"\bsendBeacon\b",
    r"\bwindow\s*\.\s*open\b", r"\blocation\s*(?:\.\s*href\s*)?=(?!=)",
    r"\blocation\s*\.\s*(?:assign|replace|reload)\b", r"\.(?:value|checked)\b",
    r"\bchrome\s*\.", r"\bimport\s*\(",
]  # fmt: skip
EVERYWHERE_DENY = [
    r"\binnerHTML\b", r"\bouterHTML\b", r"\binsertAdjacentHTML\b", r"\bdocument\s*\.\s*write",
    r"\bsrcdoc\b", r"\bpostMessage\b", r"\bonMessageExternal\b", r"\bonConnectExternal\b",
    r"\bchrome\s*\.\s*debugger\b", r"\beval\s*\(", r"\bnew\s+Function\b",
    r"\bexternally_connectable\b",
]  # fmt: skip


def violations(code: str, rules: list[str]) -> list[str]:
    return [rule for rule in rules if re.search(rule, code)]


# ─── the lexer and rules catch what they should ─────────────────────────────


def test_lexer_ignores_comments_strings_and_regexes():
    src = (
        "// appendChild in a comment\n"
        "/* fetch( */ const a = 'innerHTML'; const r = /a\\/b[/]c/g; const t = `x ${1 + 2} y`;\n"
        "return a / 2 / 3;"
    )
    code = strip_js(src)
    assert "appendChild" not in code and "innerHTML" not in code and "fetch" not in code
    assert "a / 2 / 3" in code and "1 + 2" in code


def test_rules_catch_violations():
    bad = strip_js(
        "(() => { const d = document.body; d.appendChild(x); el.value; el.textContent = 'x'; "
        "el.style.color = 'red'; setTimeout(f, 1); fetch('/x'); location = 'y'; })();"
    )
    found = violations(bad, CAPTURE_DENY)
    for needle in ("appendChild", "value|checked)\\b", "textContent", "style", "setTimeout",
                   "fetch", "location"):  # fmt: skip
        assert any(needle in rule for rule in found), needle
    assert violations(strip_js("x.innerHTML = y; window.postMessage(1)"), EVERYWHERE_DENY)


def test_single_iife_detector():
    assert single_iife(strip_js("// c\n(() => {\n  const a = 1;\n  return a;\n})();\n"))
    assert not single_iife(strip_js("const a = 1;\n(() => a)();"))
    assert not single_iife(strip_js("(() => { return 1; })(); var leak = 2;"))
    assert not single_iife(strip_js("function f() {}\n(() => {})();"))
    assert not single_iife(strip_js("(() => {})"))


# ─── the extension ─────────────────────────────────────────────────────────


def test_manifest_permissions_are_exactly_the_1e_list():
    assert set(MANIFEST["permissions"]) == PERMISSIONS
    assert not set(MANIFEST["permissions"]) & FORBIDDEN_PERMISSIONS
    assert MANIFEST["host_permissions"] == ["http://127.0.0.1/*"]
    assert not FORBIDDEN_KEYS & set(MANIFEST)
    assert MANIFEST["manifest_version"] == 3


def test_manifest_pins_the_extension_id():
    digest = hashlib.sha256(base64.b64decode(MANIFEST["key"])).hexdigest()[:32]
    assert "".join(chr(ord("a") + int(c, 16)) for c in digest) == EXTENSION_ID


def test_manifest_declares_the_shortcut_and_entry_points():
    assert MANIFEST["commands"]["_execute_action"]["suggested_key"]["default"] == "Alt+Shift+J"
    assert MANIFEST["background"] == {"service_worker": "background.js", "type": "module"}
    assert MANIFEST["action"]["default_popup"] == "popup/index.html"
    for rel in ("background.js", "popup/index.html", "options/index.html", "capture/extract.js"):
        assert (EXT / rel).is_file(), rel


def test_extract_is_one_self_invoking_expression():
    code = strip_js((EXT / "capture" / "extract.js").read_text(encoding="utf-8"))
    assert single_iife(code)
    for stmt in top_level_statements(code):
        assert not DECLARATION.match(stmt), stmt[:60]


def test_capture_code_reads_and_never_writes():
    for path in sorted((EXT / "capture").rglob("*.js")):
        code = strip_js(path.read_text(encoding="utf-8"))
        assert violations(code, CAPTURE_DENY) == [], path.name


@pytest.mark.parametrize("path", JS_FILES + HTML_FILES, ids=lambda p: str(p.relative_to(EXT)))
def test_no_html_sinks_eval_or_external_messaging(path):
    text = path.read_text(encoding="utf-8")
    code = strip_js(text) if path.suffix == ".js" else text
    assert violations(code, EVERYWHERE_DENY) == []


@pytest.mark.parametrize("path", HTML_FILES, ids=lambda p: str(p.relative_to(EXT)))
def test_pages_load_only_local_scripts(path):
    text = path.read_text(encoding="utf-8")
    assert "<script>" not in text  # MV3 refuses inline scripts anyway
    for src in re.findall(r'<script[^>]*src="([^"]+)"', text):
        assert not re.match(r"^[a-z]+:", src), src
    assert not re.search(r"\bon[a-z]+\s*=", text)  # no inline handlers


def test_worker_restricts_storage_to_trusted_contexts():
    code = strip_js((EXT / "background.js").read_text(encoding="utf-8"))
    assert re.search(r"chrome\s*\.\s*storage\s*\.\s*local\s*\.\s*setAccessLevel\s*\(", code)
    text = (EXT / "background.js").read_text(encoding="utf-8")
    assert 'accessLevel: "TRUSTED_CONTEXTS"' in text


def test_context_menu_has_no_link_context():
    text = (EXT / "background.js").read_text(encoding="utf-8")
    contexts = re.findall(r"contexts:\s*\[([^\]]*)\]", text)
    assert contexts, "the context menu must declare its contexts"
    for c in contexts:
        assert set(re.findall(r'"(\w+)"', c)) <= {"page", "selection"}


def test_worker_notice_hosts_match_the_console():
    text = (EXT / "background.js").read_text(encoding="utf-8")
    block = text.split("const NOTICE_HOSTS = [", 1)[1].split("];", 1)[0]
    patterns = [json.loads(f'"{p}"') for p in re.findall(r'"((?:[^"\\]|\\.)*)"', block)]
    assert patterns == [_LINKEDIN_HOST.pattern, _INDEED_HOST.pattern]
    extract = (EXT / "capture" / "extract.js").read_text(encoding="utf-8")
    assert "/(^|\\.)linkedin\\.com$/" in extract
    assert "/(^|\\.)indeed\\.(com|co\\.[a-z]{2}|com\\.[a-z]{2}|[a-z]{2})$/" in extract


def test_worker_talks_only_to_the_console():
    text = (EXT / "background.js").read_text(encoding="utf-8")
    hosts = set(re.findall(r"https?://([^/\"' +]+)", text))
    assert hosts <= {"127.0.0.1:"}, hosts


def test_site_table_is_empty_at_launch_and_has_no_board_rows():
    text = (EXT / "capture" / "extract.js").read_text(encoding="utf-8")
    assert re.search(r"const SITES = \[\];", text)
