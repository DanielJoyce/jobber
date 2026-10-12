"""In-app help: packaged Markdown topics rendered by the console (specs/019, Addendum A).

Every page is ``<topic>.md`` next to this file, so it ships with the package (the image copies
only ``src/``) and README, CLAUDE.md, GitHub and the console all read the same text.

Nothing here reads settings, secrets, the profile or the database: help renders before anything
is set up and while the database answers 503.
"""

from __future__ import annotations

import posixpath
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from markdown_it import MarkdownIt
from markdown_it.token import Token

HELP_DIR = Path(__file__).parent
# Where this folder sits in the repository: relative links in the pages resolve against it.
REPO_HELP_PATH = "src/jobhunter/console/help"
GITHUB_BLOB = "https://github.com/DanielJoyce/jobber/blob/main/"
REGENERATE_COMMAND = "uv run python -m jobhunter.console.help"


@dataclass(frozen=True)
class Topic:
    slug: str
    title: str
    summary: str


# The fixed list: a request names a topic only by looking it up here, never as a file path.
TOPICS: tuple[Topic, ...] = (
    Topic("getting-started", "Getting started", "The order a fresh install meets things"),
    Topic("profile-and-resume", "Profile and resume", "Preferences, the resume, what re-scores"),
    Topic("scorers", "Choosing a scorer", "Providers, keys and models"),
    Topic("openrouter", "OpenRouter and Jev", "An account, a key and credits"),
    Topic("costs-and-credits", "Costs and credits", "What spends money, and the caps"),
    Topic("email-alerts", "Email alerts", "Gmail, step by step"),
    Topic("browser-extension", "Browser extension", "Send a posting to the console"),
    Topic("drafting", "Drafting", "The Claude Code login and the API runner"),
    Topic(
        "settings-and-secrets",
        "Settings and secrets",
        "config.toml, keys and environment variables",
    ),
    Topic("nightly-runs", "Nightly runs", "Scheduled runs and their logs"),
    Topic("containers", "Containers", "Rootless podman on Fedora atomic"),
    Topic("troubleshooting", "Troubleshooting", "Common errors and fixes"),
)
TOPIC_BY_SLUG = {t.slug: t for t in TOPICS}


def topic_path(slug: str) -> Path:
    """The file for a listed topic. Raises KeyError for anything not in ``TOPICS``."""
    return HELP_DIR / f"{TOPIC_BY_SLUG[slug].slug}.md"


# --- headings ------------------------------------------------------------------------------


def slug(text: str, seen: dict[str, int] | None = None) -> str:
    """A heading id the way GitHub makes it (github-slugger).

    Lowercase; drop punctuation except ``-`` and ``_``; each space to ``-``. Pass the same
    ``seen`` dict for every heading of one page to get ``-1``, ``-2`` on repeats.
    """
    base = re.sub(r"[^\w\- ]", "", text.strip().lower()).replace(" ", "-")
    if seen is None:
        return base
    result = base
    while result in seen:  # github-slugger's loop
        seen[base] += 1
        result = f"{base}-{seen[base]}"
    seen[result] = 0
    return result


@dataclass(frozen=True)
class Heading:
    level: int
    text: str
    id: str


@dataclass(frozen=True)
class Page:
    html: str
    headings: tuple[Heading, ...]


# --- markers and generated blocks ----------------------------------------------------------

MARKER_LINE = re.compile(r"^<!-- jh:(begin|end) ([a-z0-9-]+) -->$")
_FENCE = re.compile(r"^\s*(```|~~~)")


def strip_markers(text: str) -> str:
    """Delete whole marker lines. They must go before parsing: with HTML off markdown-it
    escapes them (visible text, or an extra table row under a table)."""
    return "\n".join(ln for ln in text.split("\n") if not MARKER_LINE.match(ln))


# name -> function returning the block's Markdown. Filled by the tickets that own the content
# (the provider table, CLOUD_STEPS, the environment-variable table, the reload table).
BLOCKS: dict[str, Callable[[], str]] = {}


class BlockError(ValueError):
    pass


def _blocks_in(text: str, name: str = "file") -> list[tuple[int, int, str]]:
    """(begin line, end line, block name) for every block, outside fenced code."""
    lines = text.split("\n")
    found: list[tuple[int, int, str]] = []
    open_at: tuple[int, str] | None = None
    in_fence = False
    for i, ln in enumerate(lines):
        if _FENCE.match(ln):
            in_fence = not in_fence
            continue
        m = MARKER_LINE.match(ln) if not in_fence else None
        if not m:
            continue
        kind, block = m.groups()
        if kind == "begin":
            if open_at is not None:
                raise BlockError(f"{name}:{i + 1}: block {block!r} begins inside {open_at[1]!r}")
            open_at = (i, block)
        else:
            if open_at is None or open_at[1] != block:
                raise BlockError(f"{name}:{i + 1}: end of {block!r} without its begin")
            found.append((open_at[0], i, block))
            open_at = None
    if open_at is not None:
        raise BlockError(f"{name}:{open_at[0] + 1}: block {open_at[1]!r} is never ended")
    return found


def regenerate_text(text: str, blocks: dict[str, Callable[[], str]], name: str = "file") -> str:
    """``text`` with every block rewritten from ``blocks``.

    Always a blank line before each begin marker, after each end marker, and between a marker
    and the block's content, so a table ends before the end marker on GitHub and in the console.
    """
    lines = text.split("\n")
    for begin, end, block in reversed(_blocks_in(text, name)):
        if block not in blocks:
            raise BlockError(f"{name}:{begin + 1}: no generator for block {block!r}")
        body = blocks[block]().strip("\n").split("\n")
        new = [lines[begin], "", *body, "", lines[end]]
        lines[begin : end + 1] = new
        # blank line after the end marker, then before the begin marker
        after = begin + len(new)
        if after < len(lines) and lines[after].strip():
            lines.insert(after, "")
        if begin > 0 and lines[begin - 1].strip():
            lines.insert(begin, "")
    return "\n".join(lines)


def regenerate(
    directory: Path | None = None, blocks: dict[str, Callable[[], str]] | None = None
) -> list[Path]:
    """Rewrite the generated blocks of every ``*.md`` in the help folder. Returns the files
    that changed."""
    directory = directory or HELP_DIR
    blocks = BLOCKS if blocks is None else blocks
    changed: list[Path] = []
    for path in sorted(directory.glob("*.md")):
        old = path.read_text(encoding="utf-8")
        new = regenerate_text(old, blocks, path.name)
        if new != old:
            path.write_text(new, encoding="utf-8")
            changed.append(path)
    return changed


def stale_files(
    directory: Path | None = None, blocks: dict[str, Callable[[], str]] | None = None
) -> list[Path]:
    """Files whose committed blocks differ from what the code generates now."""
    directory = directory or HELP_DIR
    blocks = BLOCKS if blocks is None else blocks
    return [
        p
        for p in sorted(directory.glob("*.md"))
        if regenerate_text(p.read_text(encoding="utf-8"), blocks, p.name)
        != p.read_text(encoding="utf-8")
    ]


# --- links ---------------------------------------------------------------------------------

_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")


def rewrite_href(href: str) -> str:
    """Topic links to ``/help/<topic>``, other repository files to GitHub.

    Left alone: ``#anchors``, console routes (``/setup``) and absolute URLs. ``validateLink``
    in markdown-it is never touched: it is what refuses ``javascript:`` and ``file:``. A
    relative link that leaves the repository is returned unchanged (the drift test fails it).
    """
    if not href or href.startswith(("#", "/")) or _SCHEME.match(href):
        return href
    parts = urlsplit(href)
    resolved = posixpath.normpath(posixpath.join(REPO_HELP_PATH, parts.path))
    if resolved.startswith("..") or parts.path == "":
        return href
    fragment = f"#{parts.fragment}" if parts.fragment else ""
    query = f"?{parts.query}" if parts.query else ""
    directory, name = posixpath.split(resolved)
    if directory == REPO_HELP_PATH and name.endswith(".md") and name[:-3] in TOPIC_BY_SLUG:
        return f"/help/{name[:-3]}{query}{fragment}"
    return f"{GITHUB_BLOB}{resolved}{query}{fragment}"


# --- rendering -----------------------------------------------------------------------------


def _inline_text(token: Token) -> str:
    out: list[str] = []
    for child in token.children or []:
        if child.type in ("text", "code_inline"):
            out.append(child.content)
        elif child.type in ("softbreak", "hardbreak"):
            out.append(" ")
    return "".join(out)


def _post_parse(state) -> None:
    seen: dict[str, int] = {}
    headings: list[Heading] = []
    tokens = state.tokens
    for i, tok in enumerate(tokens):
        if tok.type == "heading_open":
            text = _inline_text(tokens[i + 1])
            hid = slug(text, seen)
            tok.attrSet("id", hid)
            headings.append(Heading(int(tok.tag[1:]), text, hid))
        elif tok.type == "inline":
            for child in tok.children or []:
                if child.type == "link_open":
                    child.attrSet("href", rewrite_href(str(child.attrGet("href") or "")))
    state.env["headings"] = headings


def make_markdown() -> MarkdownIt:
    # ``html`` is explicit: the commonmark preset defaults it to True.
    md = MarkdownIt("commonmark", {"html": False}).enable("table")
    md.core.ruler.push("jh_headings_and_links", _post_parse)
    # A wide table scrolls inside its own box, never the page.
    md.add_render_rule("table_open", lambda *_a: '<div class="help-table"><table>\n')
    md.add_render_rule("table_close", lambda *_a: "</table></div>\n")
    return md


_MD = make_markdown()
_cache: dict[str, tuple[int, Page]] = {}
_cache_lock = threading.Lock()


def render_text(text: str) -> Page:
    env: dict = {}
    html = _MD.render(strip_markers(text), env)
    return Page(html, tuple(env.get("headings", [])))


def render(topic: str) -> Page:
    """The rendered page for a listed topic, cached per file mtime."""
    path = topic_path(topic)
    mtime = path.stat().st_mtime_ns
    with _cache_lock:
        hit = _cache.get(topic)
        if hit and hit[0] == mtime:
            return hit[1]
    page = render_text(path.read_text(encoding="utf-8"))
    with _cache_lock:
        _cache[topic] = (mtime, page)
    return page


def _register_config_watch_block() -> None:
    """The reload table (019 Addendum B, S12a), built from ``config.RELOADABLE``."""
    from jobhunter import config_watch

    BLOCKS["config-reload"] = config_watch.reload_table


_register_config_watch_block()
