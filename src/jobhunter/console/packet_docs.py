"""View models for the packet page's documents (specs/017 phase 1b).

Turns a stored version into rows the template renders as edit boxes, each beside the lines it
cites and its check status, and works out which runner buttons the page may show. Nothing here
runs a model, reads the network or launches a process: the CLI's presence is a ``PATH`` lookup
(through ``cli_runner.find_binary``) and its health is the runner state file.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from jobhunter import container, model_access
from jobhunter.apply import cli_runner, generator, runner_state
from jobhunter.apply.review import Version
from jobhunter.config import Settings
from jobhunter.scoring import catalog


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
    # Container mode (specs/018 D16, C9): the console never drafts; the page shows the host command.
    container: bool = False

    @property
    def cap_left_usd(self) -> float:
        return max(self.cap_usd - self.spent_today_usd, 0.0)


# Request sizes for the estimate before a request is built (resume + posting + prompt).
_TYPICAL_CHARS = {"resume": 32_000, "cover_letter": 36_000, "question_draft": 20_000}


CONTAINER_DRAFT_MESSAGE = (
    "Drafting runs on your host with your Claude subscription, not in the container. "
    "Run the command shown on the packet page there; the console picks up the result on reload."
)


def host_draft_command(packet_id: int, kind: str) -> str:
    base = f"jobhunter apply draft {packet_id}"
    if kind == "cover_letter":
        return base + " --letter"
    if kind == "question_draft":
        return base + " --question -"
    return base


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
    if container.is_container():
        # No availability probe here: no PATH lookup for claude, no auth status, no key check.
        sizes = dict(_TYPICAL_CHARS, **(request_chars or {}))
        return RunnerStatus(
            default=settings.apply.runner,
            cli_ok=False,
            cli_reason=CONTAINER_DRAFT_MESSAGE,
            off=False,
            off_reason=None,
            off_at=None,
            overage=False,
            api_ok=False,
            api_reason=CONTAINER_DRAFT_MESSAGE,
            cap_usd=settings.apply.daily_cap_usd,
            spent_today_usd=generator.packet_spent_today(conn, now),
            estimates={k: generator.estimate(conn, settings, k, sizes[k]) for k in _TYPICAL_CHARS},
            container=True,
        )
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
    if not generator.api_credential_present(settings, env):
        api_reason = generator.NO_API_CREDENTIAL
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


# ─── the runner line before Generate (specs/022 "Runner and cost") ──────────


@dataclass
class DraftLine:
    """What a Generate click runs, on what, and what it costs; with 020's fallback notices."""

    runner: str
    model: str
    line: str
    notices: list[str]
    estimated_usd: float
    estimate_source: str
    charged: bool
    cap_usd: float
    cap_left_usd: float
    confirm_label: str | None
    refusal: str | None = None
    help: str | None = None

    def token(self, packet_id: int, kind: str, qkey: str) -> str:
        """Identifies this exact line; Generate posts it back and runs only on a match."""
        blob = json.dumps(
            [packet_id, kind, qkey, self.runner, self.model, f"{self.estimated_usd:.6f}",
             self.charged, self.refusal]
        )  # fmt: skip
        return hashlib.sha256(blob.encode()).hexdigest()[:16]


def _no_access(detail: str, provider: str) -> tuple[str, str]:
    access = model_access.Access(
        provider, model_access.MISSING, detail, model_access.HELP[provider],
        uses=(model_access.DRAFT,),
    )  # fmt: skip
    return model_access.summary(access), f"/help/{access.help}"


def draft_line(
    conn: sqlite3.Connection,
    settings: Settings,
    kind: str,
    est: generator.Estimate,
    *,
    packet_id: int,
    runner: str | None,
    now: datetime,
    environ: Mapping[str, str] | None = None,
    cli: Any = None,
) -> DraftLine:
    """The runner line for one draft on the effective runner (``models.toml`` over
    ``config.toml``, already merged into ``settings``) or the one the click names.

    ``est`` is ``generator.estimate`` for this request on that runner (API prices). Local and
    free: the API runner's credential through ``credential_for``, the subscription's through
    the runner's binary lookup and the runner state file, as ``generator._preflight``."""
    apply = settings.apply
    runner = runner or apply.runner
    model = apply.cli_model if runner == "cli" else apply.api_model
    cap = apply.daily_cap_usd
    left = max(cap - generator.packet_spent_today(conn, now), 0.0)
    base = dict(
        runner=runner,
        model=model,
        notices=list(getattr(settings, "models_notices", [])),
        estimated_usd=round(est.usd, 6),
        estimate_source=est.source,
        cap_usd=cap,
        cap_left_usd=round(left, 6),
    )
    if container.is_container():
        msg = f"Drafting runs on the host: `{host_draft_command(packet_id, kind)}`"
        return DraftLine(**base, line=msg, charged=False, confirm_label=None, refusal=msg)
    spend = f"≈ ${est.usd:.2f}"
    if runner == "api":
        access = model_access.anthropic_api_access(model, settings, environ=environ)
        line = f"Drafted on the Anthropic API ({model}), {spend}; apply cap ${left:.2f} left today"
        if access.status == model_access.MISSING:
            msg, help_link = _no_access(access.detail, model_access.ANTHROPIC)
            return DraftLine(**base, line=line, charged=True, confirm_label=None, refusal=msg,
                             help=help_link)  # fmt: skip
        charged = True
    elif runner == "cli":
        state = runner_state.load(settings.paths.data_dir)
        detail = None
        if (cli or cli_runner).find_binary() is None:
            detail = "the claude command is not installed"
        elif state.off:
            detail = f"the CLI runner is off: {state.off_reason}"
        if detail is not None:
            msg, help_link = _no_access(detail, model_access.SUBSCRIPTION)
            return DraftLine(**base, line=msg, charged=False, confirm_label=None, refusal=msg,
                             help=help_link)  # fmt: skip
        charged = state.overage
        if charged:
            line = (
                f"Your subscription is on paid extra usage: this draft is charged, {spend}; "
                f"apply cap ${left:.2f} left today"
            )
        else:
            api = catalog.cli_api_equivalent(conn, kind, model)
            at_api = api.usd if api.usd is not None else est.usd
            line = (
                f"Drafted on your Claude subscription with `claude -p` ({model}): $0 on your "
                f"subscription; about ${at_api:.2f} at API prices"
            )
    else:
        msg = f"unknown runner {runner!r}"
        return DraftLine(**base, line=msg, charged=False, confirm_label=None, refusal=msg)
    if charged and left < est.usd:
        msg = f"Over today's apply cap (${cap:.2f})"
        return DraftLine(**base, line=line, charged=True, confirm_label=None, refusal=msg)
    label = f"Generate, spend {spend}" if charged else "Generate"
    return DraftLine(**base, line=line, charged=charged, confirm_label=label)
