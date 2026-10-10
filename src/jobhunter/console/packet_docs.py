"""View models for the packet page's documents (specs/017 phase 1b).

Turns a stored version into rows the template renders as edit boxes, each beside the lines it
cites and its check status, and works out which runner buttons the page may show. Nothing here
runs a model, reads the network or launches a process: the CLI's presence is a ``PATH`` lookup
(through ``cli_runner.find_binary``) and its health is the runner state file.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from jobhunter.apply import cli_runner, generator, runner_state
from jobhunter.apply.review import Version
from jobhunter.config import Settings


@dataclass
class Row:
    name: str  # form prefix, e.g. "b.0.2"
    role: str
    text: str
    sources: list[str]
    pos: int
    quotes: list[str] = field(default_factory=list)
    status: str = "pass"
    reasons: list[str] = field(default_factory=list)
    entail: str | None = None
    ckey: str = ""
    cited: list[tuple[str, str]] = field(default_factory=list)
    fields: dict[str, str] = field(default_factory=dict)

    @property
    def sources_str(self) -> str:
        return ", ".join(self.sources)

    @property
    def quotes_str(self) -> str:
        return "\n".join(self.quotes)


def _row(
    name: str,
    role: str,
    text: str,
    sources: list[str],
    pos: int,
    report: Mapping[str, Mapping[str, Any]],
    key: str,
    lines: Mapping[str, str],
    **extra: Any,
) -> Row:
    item = report.get(key) or {}
    return Row(
        name=name,
        role=role,
        text=text,
        sources=list(sources),
        pos=pos,
        status=str(item.get("status") or "pass"),
        reasons=list(item.get("reasons") or []),
        entail=item.get("entail"),
        ckey=str(item.get("ckey") or ""),
        cited=[(s, lines.get(s, "(no such line)")) for s in sources],
        **extra,
    )


def resume_view(v: Version) -> dict[str, Any]:
    lines = v.lines
    if v.doc.get("base"):
        return {"base": True}
    report = {i["key"]: i for i in v.report.get("items") or []}
    r = v.doc["resume"]
    header_ids = list(r.get("header") or [])
    entries: list[dict[str, Any]] = []
    flat = 0
    for si, sec in enumerate(r.get("sections") or []):
        for ej, e in enumerate(sec.get("entries") or []):
            key = f"s{si}.e{ej}"
            head = " | ".join(x for x in (e.get("title"), e.get("employer"), e.get("dates")) if x)
            entry_row = _row(
                f"e.{flat}",
                "entry",
                head,
                [e.get("source_line") or ""],
                flat,
                report,
                key,
                lines,
                fields={k: e.get(k) or "" for k in ("employer", "title", "dates")},
            )
            bullets = [
                _row(
                    f"b.{flat}.{k}",
                    "bullet",
                    b["text"],
                    b.get("sources") or [],
                    k,
                    report,
                    f"{key}.b{k}",
                    lines,
                )
                for k, b in enumerate(e.get("bullets") or [])
            ]
            heading_row = None
            if ej == 0:
                heading_row = _row(
                    f"h.{si}",
                    "heading",
                    sec.get("heading") or "",
                    [],
                    si,
                    report,
                    f"s{si}.h",
                    lines,
                )
            entries.append(
                {
                    "i": flat,
                    "heading_row": heading_row,
                    "heading": sec.get("heading") or "",
                    "row": entry_row,
                    "bullets": bullets,
                    "source_line": e.get("source_line") or "",
                }
            )
            flat += 1
    skills = [
        _row(f"k.{k}", "skill", s["name"], s.get("sources") or [], k, report, f"skill{k}", lines)
        for k, s in enumerate(r.get("skills") or [])
    ]
    summary = r.get("summary") or {"text": "", "sources": []}
    return {
        "base": False,
        "header": ", ".join(header_ids),
        "header_row": _row("header", "header", "", header_ids, 0, report, "header", lines),
        "summary": _row(
            "summary",
            "summary",
            summary["text"],
            summary.get("sources") or [],
            0,
            report,
            "summary",
            lines,
        ),
        "entries": entries,
        "skills": skills,
        "omitted": [(x, lines.get(x, "")) for x in r.get("omitted") or [] if x in lines],
        "change_notes": list(r.get("change_notes") or []),
    }


def letter_view(v: Version) -> dict[str, Any]:
    report = {i["key"]: i for i in v.report.get("items") or []}
    paras = v.doc["cover_letter"]["paragraphs"]
    return {
        "rows": [
            _row(
                f"p.{k}",
                "paragraph",
                p["text"],
                p.get("resume_sources") or [],
                k,
                report,
                f"p{k}",
                v.lines,
                quotes=list(p.get("posting_quotes") or []),
            )
            for k, p in enumerate(paras)
        ]
    }


def draft_view(v: Version) -> dict[str, Any]:
    report = {i["key"]: i for i in v.report.get("items") or []}
    sents = v.doc["draft"]["sentences"]
    return {
        "question": v.doc.get("question") or v.question_key,
        "qkind": v.doc.get("qkind") or "",
        "rows": [
            _row(
                f"q.{k}",
                "sentence",
                s["text"],
                s.get("sources") or [],
                k,
                report,
                f"q{k}",
                v.lines,
                quotes=list(s.get("posting_quotes") or []),
            )
            for k, s in enumerate(sents)
        ],
    }


@dataclass
class RunnerStatus:
    default: str  # [apply] runner
    cli_ok: bool
    cli_reason: str | None
    off: bool
    off_reason: str | None
    off_at: str | None
    overage: bool
    api_ok: bool
    api_reason: str | None
    cap_usd: float
    spent_today_usd: float
    estimates: dict[str, generator.Estimate]

    @property
    def cap_left_usd(self) -> float:
        return max(self.cap_usd - self.spent_today_usd, 0.0)


# Request sizes for the estimate before a request is built (resume + posting + prompt).
_TYPICAL_CHARS = {"resume": 32_000, "cover_letter": 36_000, "question_draft": 20_000}


def runner_status(
    conn: sqlite3.Connection,
    settings: Settings,
    now: datetime,
    *,
    request_chars: Mapping[str, int] | None = None,
    environ: Mapping[str, str] | None = None,
) -> RunnerStatus:
    env = os.environ if environ is None else environ
    state = runner_state.load(settings.paths.data_dir)
    installed = cli_runner.find_binary() is not None
    cli_reason = None
    if not installed:
        cli_reason = "the claude command is not installed (or not on PATH)"
    elif state.off:
        cli_reason = f"the CLI runner is off: {state.off_reason}"
    sizes = dict(_TYPICAL_CHARS, **(request_chars or {}))
    estimates = {k: generator.estimate(conn, settings, k, sizes[k]) for k in _TYPICAL_CHARS}
    spent = generator.packet_spent_today(conn, now)
    cap = settings.apply.daily_cap_usd
    api_reason = None
    if not env.get("ANTHROPIC_API_KEY"):
        api_reason = "no ANTHROPIC_API_KEY is set"
    elif cap - spent < min(e.usd for e in estimates.values()):
        api_reason = f"the [apply] daily cap of ${cap:.2f} is reached"
    return RunnerStatus(
        default=settings.apply.runner,
        cli_ok=cli_reason is None,
        cli_reason=cli_reason,
        off=state.off,
        off_reason=state.off_reason,
        off_at=state.off_at,
        overage=state.overage,
        api_ok=api_reason is None,
        api_reason=api_reason,
        cap_usd=cap,
        spent_today_usd=spent,
        estimates=estimates,
    )
