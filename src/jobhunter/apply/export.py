"""Export a packet's current resume or cover letter (specs/017 "Export", phase 1c).

Formats: Markdown (the stored ``body_md``), plain text, an HTML page from one jinja template,
and a PDF printed from that page when a renderer is available. Files go to
``<data dir>/packets/<packet_id>/``: a versioned copy (``resume-v3.pdf``, recorded in
``packet_document.rendered_path`` for the PDF, relative to the data dir) and a copy named for
employers (``<First>-<Last>-Resume.pdf``, ``Cover-Letter.pdf`` style for a letter), which is the
one you attach.

Only the current version's own document is exported. Question drafts, saved answers, employer
notes and the check report never reach a file. The resume header is exported exactly as the
resume has it (specs/017 decision: the header already goes to every scorer; nothing is hidden).
An unconfirmed line blocks export of that document, the same gate as Mark ready.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from jinja2 import Environment, select_autoescape

from jobhunter.apply import generator, review
from jobhunter.core import db

log = logging.getLogger(__name__)
MANIFEST = ".named.json"

KINDS = ("resume", "cover_letter")
# URL slug -> document kind.
SLUGS = {"resume": "resume", "cover-letter": "cover_letter"}
FORMATS = ("pdf", "txt", "md", "html")
MEDIA = {
    "pdf": "application/pdf",
    "txt": "text/plain; charset=utf-8",
    "md": "text/markdown; charset=utf-8",
    "html": "text/html; charset=utf-8",
}
VERSIONED = {"resume": "resume", "cover_letter": "cover-letter"}
SUFFIX = {"resume": "Resume", "cover_letter": "Cover-Letter"}

PdfRenderer = Callable[[str, Path], None]


class ExportError(ValueError):
    """Refused; ``str()`` is the message for the page."""


def packet_dir(data_dir: Path, packet_id: int) -> Path:
    return Path(data_dir) / "packets" / str(int(packet_id))


# ─── names ──────────────────────────────────────────────────────────────────

_HEADING = re.compile(r"^#{1,6}\s+")
_NAME_TOKEN = re.compile(r"^[^\W\d_]+(?:['.-][^\W\d_]+)*\.?$")


def person_name(first_line: str) -> str:
    """``First-Last`` from a resume's first line, or ``""`` when it does not look like a name."""
    line = _HEADING.sub("", (first_line or "").strip())  # "# Jane Doe" is a name too
    if not line or any(c in line for c in "@|/\\:,;0123456789"):
        return ""
    tokens = line.split()
    if not 1 <= len(tokens) <= 4 or not all(_NAME_TOKEN.match(t) for t in tokens):
        return ""
    parts = [tokens[0], tokens[-1]] if len(tokens) > 1 else tokens
    parts = [re.sub(r"[^\w-]", "", p, flags=re.UNICODE).strip("-") for p in parts]
    return "-".join(p for p in parts if p)


def _first_line_of(v: review.Version) -> str:
    if v.doc.get("base"):
        first = next((ln for ln in v.body_md.splitlines() if ln.strip()), "")
        return _HEADING.sub("", first.strip())  # a base resume may open with "# Name"
    header = (v.doc.get("resume") or {}).get("header") or []
    return str(v.lines.get(header[0], "")) if header else ""


def file_stem(kind: str, name: str) -> str:
    return f"{name}-{SUFFIX[kind]}" if name else SUFFIX[kind]


# ─── text and HTML ──────────────────────────────────────────────────────────


def to_text(md: str) -> str:
    """Plain text for "paste your resume" boxes: markup dropped, ``##`` sections uppercased."""
    out: list[str] = []
    for line in md.splitlines():
        m = _HEADING.match(line)
        if m:
            level = len(m.group(0).strip())
            body = line[m.end() :].strip()
            out.append(body.upper() if level == 2 else body)
        else:
            out.append(line.rstrip())
    return "\n".join(out).strip() + "\n"


@dataclass
class Block:
    tag: str  # h2, h3, ul, p, pre
    text: str = ""
    items: list[str] = field(default_factory=list)


def parse_md(md: str, *, resume: bool, plain: bool) -> tuple[list[str], list[Block]]:
    """Split our Markdown subset into header lines and blocks.

    A base resume is raw text (``plain``): its first line is the name and the rest one
    line-preserving paragraph. A generated resume's header is whatever precedes the first
    ``## `` heading.
    """
    lines = md.splitlines()
    if plain:  # raw text with no Markdown in it: keep its lines as they are
        nonblank = [ln for ln in lines if ln.strip()]
        if not nonblank:
            return [], []
        rest = "\n".join(lines[lines.index(nonblank[0]) + 1 :]).strip()
        return [nonblank[0].strip()], [Block("pre", rest)] if rest else []
    header: list[str] = []
    i = 0
    if resume:
        while i < len(lines) and not lines[i].startswith("## "):
            if lines[i].strip():
                header.append(_HEADING.sub("", lines[i].strip()))
            i += 1
    blocks: list[Block] = []
    para: list[str] = []

    def flush() -> None:
        if para:
            blocks.append(Block("p", " ".join(para)))
            para.clear()

    for line in lines[i:]:
        if line.startswith(("### ", "#### ")):
            flush()
            blocks.append(Block("h3", _HEADING.sub("", line).strip()))
        elif line.startswith(("## ", "# ")):
            flush()
            blocks.append(Block("h2", _HEADING.sub("", line).strip()))
        elif line.startswith(("- ", "* ")):
            flush()
            if blocks and blocks[-1].tag == "ul":
                blocks[-1].items.append(line[2:].strip())
            else:
                blocks.append(Block("ul", items=[line[2:].strip()]))
        elif not line.strip():
            flush()
        else:
            para.append(line.strip())
    flush()
    return header, blocks


_env = Environment(autoescape=select_autoescape(default=True, default_for_string=True))
_TEMPLATE = _env.from_string(
    """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>{{ title }}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root { color-scheme: light; }
body { font: 10.5pt/1.4 "Helvetica Neue", Arial, sans-serif; color: #111; background: #fff;
       max-width: 46rem; margin: 0 auto; padding: 1rem; }
h1 { font-size: 20pt; margin: 0 0 0.15rem; }
.contact { margin: 0 0 0.8rem; color: #333; overflow-wrap: anywhere; }
h2 { font-size: 11.5pt; text-transform: uppercase; letter-spacing: 0.04em;
     border-bottom: 1px solid #999; margin: 1rem 0 0.35rem; padding-bottom: 0.1rem; }
h3 { font-size: 10.5pt; margin: 0.6rem 0 0.15rem; }
ul { margin: 0.1rem 0 0.3rem; padding-left: 1.2rem; }
li { margin: 0.1rem 0; }
p { margin: 0 0 0.7rem; overflow-wrap: anywhere; }
pre { font: inherit; white-space: pre-wrap; margin: 0; }
@media print { body { padding: 0; max-width: none; } h2, h3 { break-after: avoid; }
               li, p { break-inside: avoid; } }
</style></head><body>
{%- if header %}
<h1>{{ header[0] }}</h1>
{%- if header[1:] %}
<p class="contact">{{ header[1:] | join("<br>" | safe) }}</p>
{%- endif %}
{%- endif %}
{%- for b in blocks %}
{%- if b.tag == "ul" %}
<ul>{% for it in b.items %}<li>{{ it }}</li>{% endfor %}</ul>
{%- elif b.tag == "pre" %}
<pre>{{ b.text }}</pre>
{%- elif b.tag == "h2" %}
<h2>{{ b.text }}</h2>
{%- elif b.tag == "h3" %}
<h3>{{ b.text }}</h3>
{%- else %}
<p>{{ b.text }}</p>
{%- endif %}
{%- endfor %}
</body></html>
"""
)


def _is_markdown(md: str) -> bool:
    """Only a ``#`` heading marks Markdown: plain text may use ``- `` bullets of its own."""
    return any(_HEADING.match(ln) for ln in md.splitlines())


def to_html(md: str, kind: str, *, title: str, plain: bool = False) -> str:
    plain = plain and kind == "resume" and not _is_markdown(md)
    header, blocks = parse_md(md, resume=kind == "resume", plain=plain)
    return _TEMPLATE.render(title=title, header=header, blocks=blocks)


# ─── a document's export ────────────────────────────────────────────────────


@dataclass
class Export:
    kind: str
    version: review.Version
    name: str  # First-Last, or "" when the header's first line is not a name
    md: str
    text: str
    html: str

    @property
    def stem(self) -> str:
        return file_stem(self.kind, self.name)

    def download_name(self, fmt: str) -> str:
        return f"{self.stem}.{fmt}"


def current_export(conn: sqlite3.Connection, packet_id: int, kind: str) -> Export | None:
    """The current ``kind`` document ready to write, or None when the packet has none."""
    if kind not in KINDS:
        raise ExportError(f"unknown document {kind!r}")
    doc_id = generator.current_doc_id(conn, packet_id, kind)
    v = review.get_version(conn, doc_id) if doc_id else None
    if v is None:
        return None
    # The employer-facing name comes from the current resume's header, for letters too.
    if kind == "resume":
        rv: review.Version | None = v
    else:
        rid = generator.current_doc_id(conn, packet_id, "resume")
        rv = review.get_version(conn, rid) if rid else None
    name = person_name(_first_line_of(rv)) if rv is not None else ""
    return Export(
        kind=kind,
        version=v,
        name=name,
        md=v.body_md,
        text=to_text(v.body_md),
        html=to_html(v.body_md, kind, title=file_stem(kind, name), plain=bool(v.doc.get("base"))),
    )


def refusal(exp: Export) -> str | None:
    v = exp.version
    if v.ok:
        return None
    what = "Cover letter" if exp.kind == "cover_letter" else "Resume"
    n = v.unsupported
    if n:
        s = "" if n == 1 else "s"
        return f"{what} v{v.version} has {n} unsupported line{s}: fix or confirm each first."
    return f"{what} v{v.version} has nothing checked yet."


def versioned_path(data_dir: Path, packet_id: int, exp: Export, fmt: str) -> Path:
    return packet_dir(data_dir, packet_id) / f"{VERSIONED[exp.kind]}-v{exp.version.version}.{fmt}"


def exported(data_dir: Path, packet_id: int, exp: Export) -> bool:
    """The versioned files on disk are this version's export.

    Their names (``resume-v3.md``) are trusted only when their content matches: after the
    database is restored from a backup, a new v3 can reuse the number of an old v3 whose
    files are still on disk, and those must never be attached or downloaded as the new one.
    The Markdown and the HTML page are compared (the text and the PDF are made from them).
    """
    try:
        return all(
            versioned_path(data_dir, packet_id, exp, fmt).read_bytes() == body.encode("utf-8")
            for fmt, body in (("md", exp.md), ("html", exp.html))
        )
    except FileNotFoundError:
        return False


def packet_refusal(status: str) -> str | None:
    return (
        "This packet was abandoned; nothing is exported for it." if status == "abandoned" else None
    )


def _chmod(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError as exc:  # e.g. a filesystem without modes: the export still works
        log.warning("could not set mode %o on %s: %s", mode, path, exc)


def _private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    _chmod(path, 0o700)


def _private_file(path: Path) -> None:
    _chmod(path, 0o600)


def _owned(d: Path) -> set[str]:
    """Names of the employer-named copies jobhunter wrote here (never anything else)."""
    try:
        data = json.loads((d / MANIFEST).read_text(encoding="utf-8"))
        return {n for n in data.get("files", []) if isinstance(n, str) and "/" not in n}
    except (OSError, ValueError, AttributeError):
        return set()


def _save_owned(d: Path, names: set[str]) -> None:
    f = d / MANIFEST
    f.write_text(json.dumps({"files": sorted(names)}), encoding="utf-8")
    _private_file(f)


def named_files(data_dir: Path, packet_id: int, kind: str) -> list[Path]:
    """The employer-named copies of ``kind`` that jobhunter wrote (per its manifest)."""
    d = packet_dir(data_dir, packet_id)
    out = []
    for n in sorted(_owned(d)):
        if any(n.endswith(f"{SUFFIX[kind]}.{ext}") for ext in FORMATS) and (d / n).is_file():
            out.append(d / n)
    return out


def sync_named(conn: sqlite3.Connection, data_dir: Path, packet_id: int) -> None:
    """Make the employer-named copies match the current version, or remove them.

    Those are the files you attach, so a copy of a version that is no longer current must not
    stay. Only files jobhunter wrote (the ``.named.json`` manifest) are ever removed. Every one
    that is not the current version's is dropped, and the current version's are restored from
    its versioned files when that version was exported and still passes the gate. Run after
    anything that can move the current version (generate, edit, restore, base, abandon) and
    before the packet page is drawn. Idempotent; a packet never exported is untouched.
    """
    data_dir = Path(data_dir)
    d = packet_dir(data_dir, packet_id)
    if not d.is_dir():
        return
    row = conn.execute(
        "SELECT status FROM application_packet WHERE id = ?", (packet_id,)
    ).fetchone()
    live = row is not None and packet_refusal(row["status"]) is None
    owned = _owned(d)
    keep: dict[str, Path] = {}
    for kind in KINDS:
        exp = current_export(conn, packet_id, kind) if live else None
        if exp is not None and refusal(exp) is None and exported(data_dir, packet_id, exp):
            for fmt in FORMATS:
                src = versioned_path(data_dir, packet_id, exp, fmt)
                if src.is_file():
                    keep[exp.download_name(fmt)] = src
    now_owned: set[str] = set()
    for name in sorted(owned):
        f = d / name
        try:
            same = name in keep and f.read_bytes() == keep[name].read_bytes()
        except FileNotFoundError:
            continue  # another request got there first
        if same:
            now_owned.add(name)
        else:
            f.unlink(missing_ok=True)
    for name, src in keep.items():
        dst = d / name
        if name in now_owned or dst.exists():
            continue  # never overwrite a file we did not write
        try:
            shutil.copyfile(src, dst)
        except FileNotFoundError:
            continue
        _private_file(dst)
        now_owned.add(name)
    if now_owned != owned:
        _save_owned(d, now_owned)


def safe_sync(conn: sqlite3.Connection, data_dir: Path, packet_id: int) -> str | None:
    """``sync_named`` that never raises: a one-line warning for the page, or None."""
    try:
        sync_named(conn, data_dir, packet_id)
    except OSError as exc:
        log.warning("could not refresh the exported files of packet %s: %s", packet_id, exc)
        return f"Could not refresh the exported files in the data folder ({exc.strerror or exc})."
    return None


def sync_abandoned(conn: sqlite3.Connection, data_dir: Path) -> int:
    """``safe_sync`` every packet that still has employer-named copies but is abandoned.

    A merge (the nightly ``dedupe_url`` pass, a cross-state merge, **Link them**) can abandon
    a packet without its page being opened; its named copies, the files you attach, must go
    then and not wait for that page. Run after those. Returns how many packets were synced.
    """
    root = Path(data_dir) / "packets"
    try:
        dirs = [d for d in root.iterdir() if d.is_dir() and d.name.isdigit()]
    except FileNotFoundError:
        return 0
    n = 0
    for d in sorted(dirs):
        if not _owned(d):
            continue
        row = conn.execute(
            "SELECT status FROM application_packet WHERE id = ?", (int(d.name),)
        ).fetchone()
        if row is not None and packet_refusal(row["status"]) is None:
            continue
        safe_sync(conn, data_dir, int(d.name))
        n += 1
    return n


def named_version(data_dir: Path, packet_id: int, exp: Export) -> int | None:
    """The version the employer-named copies hold (always the current one, by ``sync_named``)."""
    return exp.version.version if named_files(data_dir, packet_id, exp.kind) else None


@dataclass
class Written:
    files: dict[str, Path] = field(default_factory=dict)  # fmt -> the employer-named file
    pdf_error: str | None = None


def write_export(
    conn: sqlite3.Connection,
    data_dir: Path,
    packet_id: int,
    exp: Export,
    renderer: PdfRenderer | None,
) -> Written:
    """Write every format; the PDF only if ``renderer`` works. A PDF failure never loses the rest.

    ``renderer`` is injected (``jobhunter.core.pdf.render_pdf`` in the console), so this
    package never imports playwright. Any exception from it becomes ``pdf_error``.
    """
    msg = refusal(exp)
    if msg:
        raise ExportError(msg)
    data_dir = Path(data_dir)
    _private_dir(data_dir / "packets")
    _private_dir(packet_dir(data_dir, packet_id))
    written = Written()
    for fmt, body in (("md", exp.md), ("txt", exp.text), ("html", exp.html)):
        vp = versioned_path(data_dir, packet_id, exp, fmt)
        vp.write_text(body, encoding="utf-8")
        _private_file(vp)
    vpdf = versioned_path(data_dir, packet_id, exp, "pdf")
    err: str | None = None
    if renderer is None:
        err = "PDF export is not available here."
    else:
        try:
            renderer(exp.html, vpdf)
            if not vpdf.is_file() or vpdf.stat().st_size == 0:
                raise RuntimeError("the renderer wrote no file")
            _private_file(vpdf)
        except Exception as exc:  # a renderer fault must not lose the other formats
            err = str(exc) or type(exc).__name__
    if err is not None:
        vpdf.unlink(missing_ok=True)
        written.pdf_error = err
    from jobhunter.apply.packets import attach_late_export  # late: packets imports tracking

    with db.transaction(conn):
        conn.execute(
            "UPDATE packet_document SET rendered_path = ? WHERE id = ?",
            (str(vpdf.relative_to(data_dir)) if err is None else None, exp.version.id),
        )
        if err is None:
            # Marked applied before this export: attach the file now (950d5eb (6)).
            attach_late_export(conn, packet_id, data_dir)
    # An explicit export replaces the copies of this document, whatever their version or name
    # (the manifest says they are ours) and a file of the exact same name.
    for f in named_files(data_dir, packet_id, exp.kind):
        f.unlink(missing_ok=True)
    for fmt in FORMATS:
        (packet_dir(data_dir, packet_id) / exp.download_name(fmt)).unlink(missing_ok=True)
    sync_named(conn, data_dir, packet_id)
    for fmt in FORMATS:
        f = packet_dir(data_dir, packet_id) / exp.download_name(fmt)
        if f.is_file():
            written.files[fmt] = f
    return written


def available(data_dir: Path, packet_id: int, exp: Export) -> dict[str, bool]:
    """Which formats of the current version are on disk (none when they are another
    version's files under the same name, ``exported``)."""
    ok = exported(data_dir, packet_id, exp)
    return {f: ok and versioned_path(data_dir, packet_id, exp, f).is_file() for f in FORMATS}
