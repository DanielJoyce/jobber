"""Packet documents: numbered source lines, checkable items, Markdown rendering (specs/017).

A ``packet_document.doc_json`` holds the structure the model returned (or the user edited)
plus a ``context`` snapshot of every line it may cite, so a later check of an edited version
reads the same lines the generation saw even if the resume file changed since::

    {"kind": "resume", "resume": {...ResumeOut.resume...},
     "context": {"lines": {"L1": "...", "N1": "...", "S1": "..."}, "resume_sha": "..."},
     "instruction": "shorter"}

A base version (**Use base resume**) is ``{"kind": "resume", "base": true, "context": ...}``.

Every checkable piece is an :class:`Item` with a stable key; ``check_report`` statuses and
confirmations attach to items. A confirmation is keyed by the item's normalized text, so it
carries forward to the next version only while the text is unchanged.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from jobhunter.apply.schemas import Draft, LetterOut, Resume, ResumeOut

KINDS = ("resume", "cover_letter", "question_draft")
KIND_LABEL = {"resume": "Resume", "cover_letter": "Cover letter", "question_draft": "Draft"}
_WS = re.compile(r"\s+")
SOURCE_RE = re.compile(r"^[LNS][1-9]\d{0,3}$")


def numbered_resume(text: str) -> dict[str, str]:
    """``{"L1": "Synthetic Person", ...}``: every non-blank line, header included."""
    out: dict[str, str] = {}
    n = 0
    for line in (text or "").splitlines():
        if line.strip():
            n += 1
            out[f"L{n}"] = line.rstrip()
    return out


def numbered(prefix: str, lines: Sequence[str]) -> dict[str, str]:
    return {f"{prefix}{i}": s.strip() for i, s in enumerate((x for x in lines if x.strip()), 1)}


def render_lines(lines: Mapping[str, str], prefix: str) -> str:
    return "\n".join(f"{k}: {v}" for k, v in lines.items() if k.startswith(prefix))


def resume_sha(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


def norm_text(text: str) -> str:
    return _WS.sub(" ", text or "").strip()


def confirm_key(text: str) -> str:
    return hashlib.sha256(norm_text(text).casefold().encode("utf-8")).hexdigest()[:16]


def parse_sources(raw: str | Iterable[str]) -> list[str]:
    """``"L4, l12 N1"`` -> ``["L4", "L12", "N1"]`` (dedup, order kept)."""
    parts = re.split(r"[\s,;]+", raw) if isinstance(raw, str) else list(raw)
    out: list[str] = []
    for p in parts:
        p = (p or "").strip().upper()
        if p and p not in out:
            out.append(p)
    return out


@dataclass
class Item:
    """One checkable line of a document."""

    key: str
    role: str  # header, summary, entry, bullet, skill, paragraph, sentence
    label: str
    text: str
    sources: list[str]
    quotes: list[str] = field(default_factory=list)
    fields: dict[str, str] = field(default_factory=dict)  # entry: employer, title, dates

    @property
    def ckey(self) -> str:
        return confirm_key(self.text)


def items_of(doc: Mapping[str, Any]) -> list[Item]:
    kind = doc.get("kind")
    if doc.get("base"):
        return []
    if kind == "resume":
        r = Resume.model_validate(doc["resume"])
        items: list[Item] = []
        if r.header:
            items.append(Item("header", "header", "Header", " / ".join(r.header), list(r.header)))
        if r.summary.text.strip():
            items.append(Item("summary", "summary", "Summary", r.summary.text, r.summary.sources))
        for i, sec in enumerate(r.sections):
            if not sec.entries:
                continue  # an empty section is neither rendered nor checked
            # A heading cites nothing; the checker reads it against the whole resume.
            items.append(Item(f"s{i}.h", "heading", "Section heading", sec.heading, []))
            for j, e in enumerate(sec.entries):
                head = " | ".join(x for x in (e.title, e.employer, e.dates) if x)
                items.append(
                    Item(
                        f"s{i}.e{j}",
                        "entry",
                        f"{sec.heading}: entry",
                        head,
                        [e.source_line],
                        fields={"employer": e.employer, "title": e.title, "dates": e.dates},
                    )
                )
                for k, b in enumerate(e.bullets):
                    items.append(Item(f"s{i}.e{j}.b{k}", "bullet", "Bullet", b.text, b.sources))
        for k, s in enumerate(r.skills):
            items.append(Item(f"skill{k}", "skill", "Skill", s.name, s.sources))
        return items
    if kind == "cover_letter":
        letter = LetterOut.model_validate({"cover_letter": doc["cover_letter"]}).cover_letter
        return [
            Item(
                f"p{k}",
                "paragraph",
                f"Paragraph {k + 1}",
                p.text,
                p.resume_sources,
                p.posting_quotes,
            )
            for k, p in enumerate(letter.paragraphs)
        ]
    if kind == "question_draft":
        d = Draft.model_validate(doc["draft"])
        return [
            Item(f"q{k}", "sentence", f"Sentence {k + 1}", s.text, s.sources, s.posting_quotes)
            for k, s in enumerate(d.sentences)
        ]
    raise ValueError(f"unknown document kind {kind!r}")


def render_md(doc: Mapping[str, Any], base_text: str = "") -> str:
    """``body_md`` for a document (export in 1c renders from this)."""
    if doc.get("base"):
        return (base_text or "").strip() + "\n"
    kind = doc.get("kind")
    lines_ctx: Mapping[str, str] = (doc.get("context") or {}).get("lines") or {}
    if kind == "resume":
        r = ResumeOut.model_validate({"resume": doc["resume"]}).resume
        out: list[str] = [lines_ctx.get(h, "").strip() for h in r.header if lines_ctx.get(h)]
        if out:
            out.append("")
        if r.summary.text.strip():
            out += ["## Summary", "", norm_text(r.summary.text), ""]
        for sec in r.sections:
            if not sec.entries:
                continue
            out += [f"## {norm_text(sec.heading)}", ""]
            for e in sec.entries:
                head = ", ".join(x for x in (norm_text(e.title), norm_text(e.employer)) if x)
                if e.dates.strip():
                    head += f" ({norm_text(e.dates)})"
                out += [f"### {head}" if head else "###", ""]
                out += [f"- {norm_text(b.text)}" for b in e.bullets]
                out.append("")
        if r.skills:
            out += ["## Skills", "", ", ".join(norm_text(s.name) for s in r.skills), ""]
        return "\n".join(out).strip() + "\n"
    if kind == "cover_letter":
        paras = doc["cover_letter"]["paragraphs"]
        return "\n\n".join(norm_text(p["text"]) for p in paras).strip() + "\n"
    if kind == "question_draft":
        return " ".join(norm_text(s["text"]) for s in doc["draft"]["sentences"]).strip() + "\n"
    raise ValueError(f"unknown document kind {kind!r}")


def dumps(doc: Mapping[str, Any]) -> str:
    return json.dumps(doc, ensure_ascii=False, separators=(",", ":"))
