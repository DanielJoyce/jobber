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

import re
import shutil
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from jinja2 import Environment, select_autoescape

from jobhunter.apply import generator, review
from jobhunter.core import db

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

_NAME_TOKEN = re.compile(r"^[^\W\d_]+(?:['.-][^\W\d_]+)*\.?$")


def person_name(first_line: str) -> str:
    """``First-Last`` from a resume's first line, or ``""`` when it does not look like a name."""
    line = (first_line or "").strip()
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
        return next((ln for ln in v.body_md.splitlines() if ln.strip()), "")
    header = (v.doc.get("resume") or {}).get("header") or []
    return str(v.lines.get(header[0], "")) if header else ""


def file_stem(kind: str, name: str) -> str:
    return f"{name}-{SUFFIX[kind]}" if name else SUFFIX[kind]


# ─── text and HTML ──────────────────────────────────────────────────────────


def to_text(md: str) -> str:
    """Plain text for "paste your resume" boxes: headings uppercased, markup dropped."""
    out: list[str] = []
    for line in md.splitlines():
        if line.startswith("## "):
            out.append(line[3:].strip().upper())
        elif line.startswith("### "):
            out.append(line[4:].strip())
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
    if plain:
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
                header.append(lines[i].strip())
            i += 1
    blocks: list[Block] = []
    para: list[str] = []

    def flush() -> None:
        if para:
            blocks.append(Block("p", " ".join(para)))
            para.clear()

    for line in lines[i:]:
        if line.startswith("### "):
            flush()
            blocks.append(Block("h3", line[4:].strip()))
        elif line.startswith("## "):
            flush()
            blocks.append(Block("h2", line[3:].strip()))
        elif line.startswith("- "):
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


def to_html(md: str, kind: str, *, title: str, plain: bool = False) -> str:
    header, blocks = parse_md(md, resume=kind == "resume", plain=plain and kind == "resume")
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
    base = packet_dir(data_dir, packet_id)
    base.mkdir(parents=True, exist_ok=True)
    written = Written()
    for fmt, body in (("md", exp.md), ("txt", exp.text), ("html", exp.html)):
        vp = versioned_path(data_dir, packet_id, exp, fmt)
        vp.write_text(body, encoding="utf-8")
        shutil.copyfile(vp, base / exp.download_name(fmt))
        written.files[fmt] = base / exp.download_name(fmt)
    vpdf = versioned_path(data_dir, packet_id, exp, "pdf")
    pdf_target = base / exp.download_name("pdf")
    err: str | None = None
    if renderer is None:
        err = "PDF export is not available here."
    else:
        try:
            renderer(exp.html, vpdf)
            if not vpdf.is_file() or vpdf.stat().st_size == 0:
                raise RuntimeError("the renderer wrote no file")
        except Exception as exc:  # a renderer fault must not lose the other formats
            err = str(exc) or type(exc).__name__
    if err is None:
        shutil.copyfile(vpdf, pdf_target)
        written.files["pdf"] = pdf_target
        with db.transaction(conn):
            conn.execute(
                "UPDATE packet_document SET rendered_path = ? WHERE id = ?",
                (str(vpdf.relative_to(data_dir)), exp.version.id),
            )
    else:
        # A PDF left from an older version must not be attached by mistake.
        pdf_target.unlink(missing_ok=True)
        vpdf.unlink(missing_ok=True)
        written.pdf_error = err
    return written


def available(data_dir: Path, packet_id: int, exp: Export) -> dict[str, bool]:
    """Which formats of the current version are on disk."""
    return {f: versioned_path(data_dir, packet_id, exp, f).is_file() for f in FORMATS}
