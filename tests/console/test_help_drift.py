"""Help pages must not drift from the code or from each other (specs/019, Addendum A).

Generalised from the old docs/containers.md checks: every ``*.md`` under
``src/jobhunter/console/help/`` is checked, so a new topic is covered the day it is added.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

import pytest
import typer.main
from fastapi.testclient import TestClient
from pydantic import BaseModel

from jobhunter import console
from jobhunter.cli import app as cli_app
from jobhunter.config import Paths, Settings
from jobhunter.console import help as helpdocs
from jobhunter.console.app import create_app
from jobhunter.core import db

ROOT = Path(__file__).resolve().parents[2]
FILES = sorted(helpdocs.HELP_DIR.glob("*.md"))
IDS = [p.stem for p in FILES]

# Real senders that may appear in prose. Anything else with an address fails.
ALLOWED_ADDRESSES = {
    # LinkedIn's job-alert sender: public, and the user must confirm it on a real alert.
    "jobalerts-noreply@linkedin.com",
}


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("help")
    path = tmp / "t.db"
    settings = Settings(paths=Paths(profile_dir=tmp / "profile", db_path=path))
    return TestClient(create_app(settings, lambda: db.connect(path)))


def text_of(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# --- commands ------------------------------------------------------------------------------


def fenced_code(text: str) -> list[str]:
    """Contents of every fenced or indented code block, parsed the way the renderer parses."""
    toks = helpdocs._MD.parse(helpdocs.strip_markers(text))
    return [t.content for t in toks if t.type in ("fence", "code_block")]


def command_lines(text: str) -> list[str]:
    lines: list[str] = []
    for block in fenced_code(text):
        for raw in block.splitlines():
            line = raw.strip().removeprefix("$ ")
            if re.match(r"jobhunter(-ctr)? ", line):
                lines.append(line)
    return lines


def tokens(line: str) -> list[str]:
    toks = shlex.split(line, comments=True)
    for i, tok in enumerate(toks):
        if tok in {"|", "&&", ";", ">"}:
            return toks[1:i]
    return toks[1:]


def check_command(line: str) -> str | None:
    """Why ``line`` is not a real invocation, or None."""
    cmd = typer.main.get_command(cli_app)  # typer vendors click: duck-type groups
    toks = tokens(line)
    consumed = 0
    while hasattr(cmd, "commands") and consumed < len(toks):
        tok = toks[consumed]
        if tok.startswith("-"):
            break
        sub = cmd.commands.get(tok)
        if sub is None:
            return f"{tok!r} is not a subcommand of {cmd.name}"
        cmd = sub
        consumed += 1
    if not consumed:
        return "no subcommand"
    known = {opt for p in cmd.params for opt in (*p.opts, *p.secondary_opts)} | {"--help"}
    for tok in toks[consumed:]:
        if tok.startswith("-") and len(tok) > 1 and tok.split("=")[0] not in known:
            return f"{tok} is not an option of {cmd.name}"
    return None


@pytest.mark.parametrize(
    "text",
    [
        "```bash\njobhunter frobnicate\n```\n",
        "1. Step\n\n   ```bash\n   jobhunter frobnicate\n   ```\n",
        "~~~bash\njobhunter frobnicate\n~~~\n",
        "```console\n$ jobhunter frobnicate\n```\n",
    ],
)
def test_command_lines_finds_every_fence_shape(text):
    assert command_lines(text) == ["jobhunter frobnicate"]


def test_command_checker_catches_bad_commands():
    assert check_command("jobhunter schedule install --dry-run") is None
    assert "not a subcommand" in check_command("jobhunter frobnicate")
    assert "not an option" in check_command("jobhunter schedule install --nope")


@pytest.mark.parametrize("path", FILES, ids=IDS)
def test_documented_commands_exist(path):
    for line in command_lines(text_of(path)):
        assert (why := check_command(line)) is None, f"{path.name}: {line!r}: {why}"


# --- `[section] key` -----------------------------------------------------------------------

SECTION_KEY = re.compile(r"`\[([a-z_][a-z_.]*)\]\s+([a-z_][a-z0-9_]*)`")


def settings_field_exists(section: str, key: str) -> bool:
    model: type[BaseModel] = Settings
    for part in section.split("."):
        field = model.model_fields.get(part)
        sub = getattr(field, "annotation", None)
        if not (isinstance(sub, type) and issubclass(sub, BaseModel)):
            return False
        model = sub
    return key in model.model_fields


def test_settings_field_checker():
    assert settings_field_exists("fetch", "user_agent_contact")
    assert settings_field_exists("scoring.openrouter", "api_key_env")
    assert settings_field_exists("mail", "alerts_address")
    assert not settings_field_exists("fetch", "nope")
    assert not settings_field_exists("nope", "x")


@pytest.mark.parametrize("path", FILES, ids=IDS)
def test_bracket_section_key_names_are_real_settings(path):
    for section, key in SECTION_KEY.findall(text_of(path)):
        assert settings_field_exists(section, key), f"{path.name}: `[{section}] {key}`"


# --- links ---------------------------------------------------------------------------------

HREF = re.compile(r'<a href="([^"]*)"')


def ids_in(html: str) -> set[str]:
    return set(re.findall(r'\bid="([^"]+)"', html))


def check_links(html: str, own_ids: set[str], client: TestClient) -> list[str]:
    """Every problem with the rendered links of one page."""
    problems: list[str] = []
    for href in HREF.findall(html):
        href = href.replace("&amp;", "&")
        if href.startswith("#"):
            if href[1:] not in own_ids:
                problems.append(f"dead anchor {href}")
        elif href.startswith(helpdocs.GITHUB_BLOB):
            rel, _, _frag = href[len(helpdocs.GITHUB_BLOB) :].partition("#")
            if not (ROOT / rel).exists():
                problems.append(f"repository file {rel} does not exist")
        elif href.startswith("/help/"):
            topic, _, frag = href[len("/help/") :].partition("#")
            if topic not in helpdocs.TOPIC_BY_SLUG:
                problems.append(f"{href}: no such topic")
            elif frag and frag not in {h.id for h in helpdocs.render(topic).headings}:
                problems.append(f"{href}: no such heading")
        elif href.startswith("/"):
            route, _, frag = href.partition("#")
            r = client.get(route)
            if r.status_code != 200:
                problems.append(f"{href}: {r.status_code}")
            elif frag and frag not in ids_in(r.text):
                problems.append(f"{href}: no element with id {frag!r}")
        elif re.match(r"^(https?:|mailto:)", href):
            continue
        else:
            problems.append(f"relative link {href!r} was not rewritten or leaves the repository")
    return problems


def test_link_checker_catches_bad_links(client):
    html = helpdocs.render_text(
        "# T\n\n[a](#nope) [b](email-alerts.md#nope) [c](../../../../nope.md) [d](/nope)\n"
        "[e](../../../../../x.md) [f](/help/getting-started) [g](../../../../README.md)\n"
    ).html
    problems = check_links(html, ids_in(html), client)
    assert len(problems) == 5, problems


@pytest.mark.parametrize("path", FILES, ids=IDS)
def test_links_and_anchors_resolve(path, client):
    page = helpdocs.render(path.stem)
    assert check_links(page.html, {h.id for h in page.headings}, client) == []


# --- units ---------------------------------------------------------------------------------

UNIT = re.compile(r"\b(jobhunter(?:-ctr)?-[a-z]+@?)\.(service|timer|container)\b")


def test_unit_names_exist():
    quadlet = {p.stem for p in (ROOT / "deploy" / "quadlet").glob("*.container")}
    systemd = {p.name for p in (ROOT / "deploy" / "systemd").iterdir()}
    host = {p.name for p in (ROOT / "src" / "jobhunter" / "ops" / "units").iterdir()}
    assert quadlet and systemd and host
    for path in FILES:
        for stem, kind in UNIT.findall(text_of(path)):
            where = f"{path.name}: {stem}.{kind}"
            if kind == "container":
                assert stem in quadlet, f"{where} has no deploy/quadlet file"
            elif stem.startswith("jobhunter-ctr-"):
                if kind == "timer":
                    assert f"{stem}.timer" in systemd, f"{where} has no deploy/systemd file"
                else:
                    assert stem in quadlet or f"{stem}.service" in systemd, where
            else:
                assert f"{stem}.{kind}" in host, f"{where} is not in ops/units"


# --- personal data -------------------------------------------------------------------------

ADDRESS = re.compile(r"([^\s@`'\"(),;]*)@([\w-]+(?:\.[\w-]+)+)")


def disallowed_addresses(text: str) -> list[str]:
    bad = []
    for local, domain in ADDRESS.findall(text):
        full = f"{local}@{domain}"
        if domain == "example.com" or local.startswith("<you>") or full in ALLOWED_ADDRESSES:
            continue
        bad.append(full)
    return bad


def test_address_checker():
    assert disallowed_addresses("<you>@example.com <you>+jobs@gmail.com a@example.com") == []
    assert disallowed_addresses("jobalerts-noreply@linkedin.com") == []
    assert disallowed_addresses("someone@example.org") == ["someone@example.org"]
    assert disallowed_addresses("me+jobs@example.org") == ["me+jobs@example.org"]


@pytest.mark.parametrize("path", FILES, ids=IDS)
def test_no_personal_addresses(path):
    assert disallowed_addresses(text_of(path)) == []


# --- generated blocks and rendered output --------------------------------------------------


def test_generated_blocks_are_current():
    stale = helpdocs.stale_files()
    assert not stale, (
        f"generated help blocks are out of date in {[p.name for p in stale]}; "
        f"run: {helpdocs.REGENERATE_COMMAND}"
    )


@pytest.mark.parametrize("path", FILES, ids=IDS)
def test_every_block_has_a_generator(path):
    for _begin, _end, name in helpdocs._blocks_in(text_of(path), path.name):
        assert name in helpdocs.BLOCKS, f"{path.name}: block {name!r} has no generator"


@pytest.mark.parametrize("path", FILES, ids=IDS)
def test_rendered_html_has_no_markers_or_escaped_html(path):
    html = helpdocs.render(path.stem).html
    assert "jh:" not in html
    assert "&lt;!--" not in html
    assert "&lt;" not in outside_code(html), f"{path.name} has stray HTML outside code"


def outside_code(html: str) -> str:
    html = re.sub(r"<pre>.*?</pre>", "", html, flags=re.S)
    return re.sub(r"<code>.*?</code>", "", html, flags=re.S)


def test_outside_code_flags_bare_placeholders_and_spares_code():
    bare = helpdocs.render_text("Write <you>+jobs here\n").html
    assert "&lt;" in outside_code(bare)
    ok = helpdocs.render_text("`<you>`\n\n1. s\n\n   ```bash\n   x <name>\n   ```\n").html
    assert "&lt;" not in outside_code(ok)


@pytest.mark.parametrize("path", FILES, ids=IDS)
def test_headings_are_unique_and_start_with_one_h1(path):
    headings = helpdocs.render(path.stem).headings
    assert headings and headings[0].level == 1
    assert sum(h.level == 1 for h in headings) == 1


# --- sentinel: no setting or secret is ever rendered ---------------------------------------


def test_no_setting_or_secret_is_rendered(tmp_path, monkeypatch):
    sentinel = "SENTINEL-4f1c9a-do-not-render"
    for name in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "OPENROUTER_API_KEY",
        "USAJOBS_API_KEY",
        "USAJOBS_EMAIL",
        "CLAUDE_CODE_OAUTH_TOKEN",
    ):
        monkeypatch.setenv(name, sentinel)
    path = tmp_path / "t.db"
    settings = Settings.model_validate(
        {
            "paths": {"profile_dir": str(tmp_path / "profile"), "db_path": str(path)},
            "fetch": {"user_agent_contact": f"{sentinel}@example.org"},
            "mail": {"alerts_address": f"{sentinel}+jobs@example.org"},
        }
    )
    client = TestClient(create_app(settings, lambda: db.connect(path)))
    pages = ["/help", *(f"/help/{t.slug}" for t in helpdocs.TOPICS)]
    for page in pages:
        r = client.get(page)
        assert r.status_code == 200
        assert sentinel not in r.text, page


def test_help_package_does_not_touch_settings_or_the_database():
    src = "".join(p.read_text() for p in [*helpdocs.HELP_DIR.glob("*.py")])
    src += (Path(console.__file__).parent / "help_routes.py").read_text()
    forbidden_names = ("jobhunter.config", "jobhunter.core", "sqlite3", "app.state", "get_conn")
    for forbidden in (*forbidden_names, "Depends"):
        assert forbidden not in src, forbidden


def test_readme_help_table_lists_exactly_the_topics():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    linked = re.findall(r"\]\(src/jobhunter/console/help/([a-z-]+)\.md\)", readme)
    assert sorted(linked) == sorted(t.slug for t in helpdocs.TOPICS)
    assert all((helpdocs.HELP_DIR / f"{s}.md").is_file() for s in linked)


# --- environment variables: every one the code reads is in the generated table --------------

ENV_SHAPE = re.compile(
    r"(JOBHUNTER|ANTHROPIC|USAJOBS|OPENROUTER|OPENAI_COMPAT|LLAMA|CLAUDE|XDG)_[A-Z0-9_]+"
)


def env_like_literals() -> dict[str, str]:
    """Every string literal in src/ shaped like an environment variable name -> first file."""
    import ast

    found: dict[str, str] = {}
    for path in sorted((ROOT / "src").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and ENV_SHAPE.fullmatch(node.value)
            ):
                found.setdefault(node.value, str(path.relative_to(ROOT)))
    return found


def test_every_environment_variable_in_the_code_is_documented():
    from jobhunter import helpblocks

    table = helpblocks.env_var_table()
    missing = {}
    for name, where in env_like_literals().items():
        base = name.removesuffix("_FILE")
        if f"`{name}`" in table or f"`{base}`" in table or name in helpblocks.NOT_ENV:
            continue
        missing[name] = where
    assert not missing, (
        "read in the code but not in the environment-variable table of settings-and-secrets.md "
        f"(add to jobhunter/helpblocks.py): {missing}"
    )


def test_environment_table_names_are_still_read_by_the_code():
    """The reverse: a documented variable that no code reads any more is stale."""
    from jobhunter import helpblocks

    used = set(env_like_literals())
    used |= {f"{n}_FILE" for n in used}
    for name in re.findall(r"^\| `([A-Z0-9_]+)`", helpblocks.env_var_table(), flags=re.M):
        assert name in used or name.removesuffix("_FILE") in used, f"{name} is never read"
