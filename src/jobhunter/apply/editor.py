"""The structured, cited-line editor (specs/017 "Editing").

Each summary, bullet, skill, paragraph and sentence is its own edit box that keeps its
``sources``; the form posts them back and :func:`doc_from_form` rebuilds the document, which
:func:`jobhunter.apply.generator.write_version` saves as a new version (``origin = 'edited'``).
Nothing is overwritten. Order comes from each item's ``pos`` number; ``remove`` drops an item;
``true`` ("This is true, keep it") confirms an item that would otherwise be unsupported. A new
item needs a source line or **This is true**; without either it is saved and badged
unsupported, which blocks Mark ready.

Form field names (``i`` entry, ``k`` item index)::

    header, summary.text, summary.sources
    e.<i>.heading|source_line|employer|title|dates|pos|remove
    b.<i>.<k>.text|sources|pos|remove|true      nb.<i>.text|sources|true
    k.<k>.name|sources|pos|remove|true          nk.name|sources|true
    p.<k>.text|sources|quotes|pos|remove|true   np.text|sources|quotes|true   (cover letter)
    q.<k>.text|sources|quotes|pos|remove|true   nq.text|sources|quotes|true   (question draft)
"""

from __future__ import annotations

import copy
import re
from collections.abc import Mapping
from typing import Any

from jobhunter.apply.documents import confirm_key, norm_text, parse_sources

MAX_ITEMS = 200


class EditError(ValueError):
    pass


def _pos(form: Mapping[str, str], key: str, default: float) -> float:
    try:
        return float(form.get(key, "") or default)
    except ValueError:
        return default


def _on(form: Mapping[str, str], key: str) -> bool:
    return form.get(key) in ("1", "on", "true", "yes")


def _indices(form: Mapping[str, str], pattern: str) -> list[int]:
    rx = re.compile(pattern)
    found = {int(m.group(1)) for k in form if (m := rx.match(k))}
    if len(found) > MAX_ITEMS:
        raise EditError("too many items")
    return sorted(found)


def _quotes(raw: str) -> list[str]:
    return [q.strip() for q in (raw or "").splitlines() if q.strip()]


def _cited_items(
    form: Mapping[str, str], prefix: str, *, text_field: str = "text", quotes: bool = False
) -> tuple[list[dict[str, Any]], list[str]]:
    """Existing ``prefix.<k>.*`` items in ``pos`` order, then the new ``n<prefix>`` item."""
    confirmed: list[str] = []
    rows: list[tuple[float, int, dict[str, Any]]] = []
    for k in _indices(form, rf"^{re.escape(prefix)}\.(\d+)\.{text_field}$"):
        base = f"{prefix}.{k}"
        if _on(form, f"{base}.remove"):
            continue
        text = norm_text(form.get(f"{base}.{text_field}", ""))
        if not text:
            continue
        item: dict[str, Any] = {
            text_field: text,
            "sources": parse_sources(form.get(f"{base}.sources", "")),
        }
        if quotes:
            item["posting_quotes"] = _quotes(form.get(f"{base}.quotes", ""))
        if _on(form, f"{base}.true"):
            confirmed.append(confirm_key(text))
        rows.append((_pos(form, f"{base}.pos", k), k, item))
    rows.sort(key=lambda r: (r[0], r[1]))
    items = [r[2] for r in rows]
    new = f"n{prefix}"
    text = norm_text(form.get(f"{new}.{text_field}", ""))
    if text:
        item = {text_field: text, "sources": parse_sources(form.get(f"{new}.sources", ""))}
        if quotes:
            item["posting_quotes"] = _quotes(form.get(f"{new}.quotes", ""))
        if _on(form, f"{new}.true"):
            confirmed.append(confirm_key(text))
        items.append(item)
    return items, confirmed


def _resume_from_form(
    current: Mapping[str, Any], form: Mapping[str, str]
) -> tuple[dict[str, Any], list[str]]:
    confirmed: list[str] = []
    old = current["resume"]
    summary_text = norm_text(form.get("summary.text", ""))
    summary = {"text": summary_text, "sources": parse_sources(form.get("summary.sources", ""))}
    if summary_text and _on(form, "summary.true"):
        confirmed.append(confirm_key(summary_text))
    entries: list[tuple[float, int, str, dict[str, Any]]] = []
    for i in _indices(form, r"^e\.(\d+)\.source_line$"):
        base = f"e.{i}"
        if _on(form, f"{base}.remove"):
            continue
        bullets, conf = _cited_items(form, f"b.{i}")  # its new-bullet slot is "nb.<i>"
        confirmed += conf
        entry = {
            "source_line": (parse_sources(form.get(f"{base}.source_line", "")) or [""])[0],
            "employer": norm_text(form.get(f"{base}.employer", "")),
            "title": norm_text(form.get(f"{base}.title", "")),
            "dates": norm_text(form.get(f"{base}.dates", "")),
            "bullets": bullets,
        }
        if _on(form, f"{base}.true"):
            head = " | ".join(x for x in (entry["title"], entry["employer"], entry["dates"]) if x)
            confirmed.append(confirm_key(head))
        heading = norm_text(form.get(f"{base}.heading", "")) or "Experience"
        entries.append((_pos(form, f"{base}.pos", i), i, heading, entry))
    entries.sort(key=lambda r: (r[0], r[1]))
    sections: list[dict[str, Any]] = []
    for _p, _i, heading, entry in entries:
        if sections and sections[-1]["heading"] == heading:
            sections[-1]["entries"].append(entry)
        else:
            sections.append({"heading": heading, "entries": [entry]})
    skills_raw, conf = _cited_items(form, "k", text_field="name")
    confirmed += conf
    header = parse_sources(form.get("header", ""))
    resume = {
        "header": header,
        "summary": summary,
        "sections": sections,
        "skills": [{"name": s["name"], "sources": s["sources"]} for s in skills_raw],
        "omitted": list(old.get("omitted") or []),
        "change_notes": list(old.get("change_notes") or []),
    }
    return resume, confirmed


def doc_from_form(
    current: Mapping[str, Any], form: Mapping[str, str]
) -> tuple[dict[str, Any], list[str]]:
    """A new document from the editor form; returns (doc, confirm keys ticked)."""
    kind = current["kind"]
    if current.get("base"):
        raise EditError("the base resume is not edited here; generate a targeted version")
    doc = {
        k: copy.deepcopy(v)
        for k, v in current.items()
        if k not in ("resume", "cover_letter", "draft")
    }
    doc.pop("instruction", None)
    if kind == "resume":
        doc["resume"], confirmed = _resume_from_form(current, form)
    elif kind == "cover_letter":
        paras, confirmed = _cited_items(form, "p", quotes=True)
        doc["cover_letter"] = {
            "paragraphs": [
                {
                    "text": p["text"],
                    "resume_sources": p["sources"],
                    "posting_quotes": p["posting_quotes"],
                }
                for p in paras
            ]
        }
    elif kind == "question_draft":
        sents, confirmed = _cited_items(form, "q", quotes=True)
        doc["draft"] = {"sentences": sents}
    else:
        raise EditError(f"unknown document kind {kind!r}")
    return doc, confirmed


def restore_line(current: Mapping[str, Any], line_id: str) -> dict[str, Any]:
    """One-click restore of an omitted resume line: a bullet citing it, with its own text, in
    the nearest entry above it (or a new "Additional" section)."""
    if current.get("kind") != "resume" or current.get("base"):
        raise EditError("only a targeted resume has omitted lines")
    lines = (current.get("context") or {}).get("lines") or {}
    if line_id not in lines:
        raise EditError(f"no line {line_id}")
    doc = copy.deepcopy(dict(current))
    doc.pop("instruction", None)
    r = doc["resume"]
    text = re.sub(r"^\s*[-*•]\s*", "", lines[line_id]).strip()
    n = int(line_id[1:])
    best: dict[str, Any] | None = None
    best_n = -1
    for sec in r["sections"]:
        for e in sec["entries"]:
            src = e.get("source_line") or ""
            if src[1:].isdigit() and best_n < int(src[1:]) <= n:
                best, best_n = e, int(src[1:])
    bullet = {"text": text, "sources": [line_id]}
    if best is not None:
        best["bullets"].append(bullet)
    else:
        r["sections"].append(
            {
                "heading": "Additional",
                "entries": [
                    {
                        "source_line": line_id,
                        "employer": "",
                        "title": "",
                        "dates": "",
                        "bullets": [bullet],
                    }
                ],
            }
        )
    r["omitted"] = [x for x in r.get("omitted") or [] if x != line_id]
    return doc
