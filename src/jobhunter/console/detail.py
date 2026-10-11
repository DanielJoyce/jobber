"""Job detail data layer (specs/007 "/job/{group_id}", 015 apply button, 006 evidence).

Everything user- or model-supplied is escaped here; templates receive ``Markup`` only for
strings built from escaped parts.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from markupsafe import Markup, escape

from jobhunter.apply.packets import live_packet_id
from jobhunter.console.inbox import _json, _strs, salary_text, set_label
from jobhunter.console.tracking import APPLY_CLICK_NOTE, rebuild_status
from jobhunter.core import rejections
from jobhunter.core.manual_sources import EMAIL_MANUAL
from jobhunter.core.models import ApplyLink, ApplyStatus
from jobhunter.pipeline.applylink import get_apply_link
from jobhunter.pipeline.ats_rules import host_of, is_http_url
from jobhunter.pipeline.dedupe import _refresh_group, group_members
from jobhunter.pipeline.locations import load_job_group_locations, location_summary
from jobhunter.pipeline.normalize import normalize_job
from jobhunter.scoring.buckets import compute_row
from jobhunter.scoring.profile import Profile

# ─── evidence highlighting ──────────────────────────────────────────────────


def _normalize(text: str) -> tuple[str, list[int]]:
    """Lowercase and collapse whitespace; ``idx[i]`` is the original offset of char ``i``."""
    out: list[str] = []
    idx: list[int] = []
    prev_space = True  # also strips leading whitespace
    for i, ch in enumerate(text):
        if ch.isspace():
            if not prev_space:
                out.append(" ")
                idx.append(i)
            prev_space = True
        else:
            for c in ch.lower():
                out.append(c)
                idx.append(i)
            prev_space = False
    if out and out[-1] == " ":
        out.pop()
        idx.pop()
    return "".join(out), idx


def find_span(text: str, quote: str) -> tuple[int, int] | None:
    """Original-text ``[start, end)`` of ``quote`` (whitespace/case-insensitive), or None."""
    norm_text, idx = _normalize(text)
    norm_quote, _ = _normalize(quote)
    if not norm_quote:
        return None
    pos = norm_text.find(norm_quote)
    if pos == -1:
        return None
    return idx[pos], idx[pos + len(norm_quote) - 1] + 1


@dataclass
class Highlighted:
    paragraphs: list[Markup] = field(default_factory=list)
    verified: list[dict[str, str]] = field(default_factory=list)
    unverified: list[dict[str, str]] = field(default_factory=list)


def _evidence_items(raw: Any) -> list[dict[str, str]]:
    data = raw if isinstance(raw, list) else _json(raw, [])
    items = []
    for e in data if isinstance(data, list) else []:
        if isinstance(e, dict) and e.get("quote"):
            items.append({"claim": str(e.get("claim") or ""), "quote": str(e["quote"])})
    return items


def highlight(text: str | None, evidence_raw: Any) -> Highlighted:
    """Escaped paragraphs with verified quotes in ``<mark title=claim>``; unverified listed."""
    text = text or ""
    result = Highlighted()
    spans: list[tuple[int, int, str]] = []
    for ev in _evidence_items(evidence_raw):
        span = find_span(text, ev["quote"])
        if span is None:
            result.unverified.append(ev)
        else:
            result.verified.append(ev)
            spans.append((span[0], span[1], ev["claim"]))
    spans.sort()
    kept: list[tuple[int, int, str]] = []
    for s in spans:  # overlapping spans: keep the earlier one
        if not kept or s[0] >= kept[-1][1]:
            kept.append(s)

    pos = 0
    blocks: list[tuple[int, int]] = []
    for m in re.finditer(r"\n\s*\n", text):
        blocks.append((pos, m.start()))
        pos = m.end()
    blocks.append((pos, len(text)))

    for lo, hi in blocks:
        if not text[lo:hi].strip():
            continue
        parts: list[str] = []
        cur = lo
        for s, e, claim in kept:
            if e <= lo or s >= hi:
                continue
            s, e = max(s, lo), min(e, hi)
            parts.append(str(escape(text[cur:s])))
            parts.append(f'<mark title="{escape(claim)}">{escape(text[s:e])}</mark>')
            cur = e
        parts.append(str(escape(text[cur:hi])))
        result.paragraphs.append(Markup("".join(parts)))
    return result


# ─── apply button ───────────────────────────────────────────────────────────


ATS_DISPLAY = {
    "smartrecruiters": "SmartRecruiters",
    "neogov": "NEOGOV",
    "usastaffing": "USA Staffing",
    "icims": "iCIMS",
}


def ats_display(name: str) -> str:
    return ATS_DISPLAY.get(name, name.title())


def ago(then: datetime, now: datetime) -> str:
    secs = max(0, int((now - then).total_seconds()))
    if secs < 3600:
        return "<1h ago"
    hours = secs // 3600
    return f"{hours}h ago" if hours < 48 else f"{hours // 24}d ago"


@dataclass
class ApplyButton:
    group_id: int
    kind: str  # live | expired | open
    label: str
    href: str  # primary link
    open_anyway: str | None = None
    chain: str | None = None
    verified: str | None = None
    host: str | None = None
    pending: bool = False  # no apply_link row yet: resolve on demand


def chain_text(link: ApplyLink) -> str:
    """Compact chain: ``start-host → hop (unwrapped) → Workday · employer_host``."""
    parts = [host_of(link.start_url) or link.start_url]
    hops = [h.host for h in link.chain]
    methods = [h.method for h in link.chain]
    tail = ats_display(link.ats) if link.ats and link.ats != "unknown" else None
    if tail and link.employer_host:
        tail = f"{tail} · {link.employer_host}"
    elif link.employer_host:
        tail = link.employer_host
    if tail and hops and link.chain[-1].host == link.employer_host:
        hops.pop()  # the last hop is the destination; the named ATS replaces it
        methods.pop()
    parts += [
        f"{h} (unwrapped)" if m == "unwrap" else h for h, m in zip(hops, methods, strict=True)
    ]
    if tail:
        parts.append(tail)
    return " → ".join(parts)


def apply_button(
    conn: sqlite3.Connection,
    group_id: int,
    now: datetime,
    link: ApplyLink | None = None,
    *,
    fetch_link: bool = True,
) -> ApplyButton:
    if link is None and fetch_link:
        link = get_apply_link(conn, group_id)
    apply_href = f"/apply/{group_id}"
    if link is None:
        return ApplyButton(group_id, "open", "Open posting ↗", apply_href, pending=True)
    verified = f"verified {ago(link.verified_at, now)}" if link.verified_at else "not verified"
    chain, host = chain_text(link), link.employer_host
    if link.status == ApplyStatus.live:
        name = ats_display(link.ats) if link.ats and link.ats != "unknown" else (host or "site")
        return ApplyButton(
            group_id,
            "live",
            f"Apply on {name} ↗",
            apply_href,
            chain=chain,
            verified=verified,
            host=host,
        )
    if link.status == ApplyStatus.expired:
        return ApplyButton(
            group_id,
            "expired",
            "Posting closed",
            "",
            open_anyway=link.final_url or link.start_url,
            chain=chain,
            verified=verified,
            host=host,
        )
    return ApplyButton(
        group_id,
        "open",
        "Open posting ↗",
        apply_href,
        chain=chain,
        verified=verified,
        host=host,
    )


# ─── page data ──────────────────────────────────────────────────────────────

_FIT_SQL = (
    "SELECT * FROM fit_score WHERE job_group_id = ? "
    "ORDER BY (tier = 'deep') DESC, created_at DESC, id DESC LIMIT 1"
)


@dataclass
class Detail:
    group_id: int
    job: sqlite3.Row
    employer: str
    location: str
    locations: list[str]
    salary: str
    salary_stated: bool
    source_name: str
    salary_from_text: bool = False  # parsed from the description, not a posted pay field
    scored: bool = False
    bucket: str | None = None
    overall: int | None = None
    verdict: str | None = None
    tier: str | None = None
    dims: list[tuple[str, int, str]] = field(default_factory=list)
    recency_skills: int | None = None
    raw_skills: int | None = None
    stale_skills: list[str] = field(default_factory=list)
    comp: int | None = None
    location_fit: int | None = None
    blockers: list[str] = field(default_factory=list)
    missing_info: list[str] = field(default_factory=list)
    shape_flags: list[str] = field(default_factory=list)
    tailoring_hints: list[str] = field(default_factory=list)
    evidence_unverified: bool = False
    # 'none' for decisions-model scores (Jev): probabilities instead of evidence quotes.
    evidence_mode: str = "quotes"
    decisions_verdict: str | None = None
    decisions_answers: list[tuple[str, str]] = field(default_factory=list)
    served_model: str | None = None
    text: Highlighted = field(default_factory=Highlighted)
    siblings: list[dict[str, Any]] = field(default_factory=list)
    partial: bool = False
    pasted: bool = False
    button: ApplyButton | None = None
    prompt: bool = False
    # Rejections from employers (core/rejections): this very posting, or another role here.
    posting_rejection: str | None = None
    employer_rejection: str | None = None
    packet_id: int | None = None  # the live assisted-apply packet (specs/017)

    @property
    def score_on_request(self) -> bool:
        """The group is scored only when the user asks (specs/017 "Scored on request").

        Set for pasted, captured and email-manual groups; whichever member is canonical.
        """
        return bool(self.job["score_on_request"])

    @property
    def pasted_posting(self) -> bool:
        """Pasted or captured by the user (specs/017): never queued for a nightly re-score."""
        return self.score_on_request and self.job["source_key"] != EMAIL_MANUAL

    @property
    def posting_href(self) -> str | None:
        """The posting's web page, or None (a pasted posting with no URL has none)."""
        url = posting_url(self.job)
        return url if is_http_url(url) else None


def group_job(conn: sqlite3.Connection, group_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT j.*, s.name AS source_name, g.score_on_request FROM job_group g "
        "JOIN job j ON j.id = g.canonical_job_id JOIN source s ON s.key = j.source_key "
        "WHERE g.id = ?",
        (group_id,),
    ).fetchone()


def posting_url(job: sqlite3.Row) -> str:
    return job["apply_url"] or job["url"]


def did_you_apply(conn: sqlite3.Connection, group_id: int) -> bool:
    """A click newer than the group's latest application event (and group not settled).

    The click's own interested->preparing event (same timestamp) is not an answer, so it is
    ignored; otherwise shortlisted jobs would never show the prompt.
    """
    click = conn.execute(
        "SELECT MAX(at) FROM apply_click WHERE job_group_id = ?", (group_id,)
    ).fetchone()[0]
    if not click:
        return False
    label = conn.execute("SELECT label FROM label WHERE job_group_id = ?", (group_id,)).fetchone()
    if label and label["label"] in ("not_interesting", "applied"):
        return False
    ev = conn.execute(
        "SELECT MAX(e.at) FROM application_event e JOIN application a ON a.id = e.application_id "
        "WHERE a.job_group_id = ? AND COALESCE(e.note, '') != 'opened apply link'",
        (group_id,),
    ).fetchone()[0]
    return ev is None or click > ev


def _decisions_view(d: Detail, report: Any) -> None:
    """Summary lines for a decisions-model score: the verdict and each yes/no answer."""
    if not isinstance(report, dict):
        return
    verdict = report.get("verdict") or {}
    probs = verdict.get("probabilities") or {}
    choice = verdict.get("choice")
    if isinstance(choice, str):
        p = probs.get(choice)
        conf = verdict.get("confidence")
        parts = [f"{p:.2f}" if isinstance(p, int | float) else None]
        if isinstance(conf, int | float):
            parts.append(f"confidence {conf:.2f}")
        detail = ", ".join(x for x in parts if x)
        d.decisions_verdict = f"fit {choice}" + (f" ({detail})" if detail else "")
    labels = report.get("labels") or {}
    answers = report.get("answers") or {}
    for qid, label in labels.items():
        ans = answers.get(qid)
        if isinstance(ans, dict) and isinstance(ans.get("noul"), int | float):
            d.decisions_answers.append((str(label), f"{ans['noul']:.2f}"))


def load_detail(
    conn: sqlite3.Connection,
    profile: Profile,
    group_id: int,
    now: datetime,
    rejection_days: int = rejections.DEFAULT_WINDOW_DAYS,
) -> Detail | None:
    job = group_job(conn, group_id)
    if job is None:
        return None
    locs = load_job_group_locations(conn, job["id"])
    loc_lines = []
    for loc in locs:
        place = ", ".join(p for p in (loc.city, loc.state) if p)
        loc_lines.append(place or "remote / unspecified")
    d = Detail(
        group_id=group_id,
        job=job,
        employer=job["employer"] or job["agency_raw"] or "employer not stated",
        location=location_summary(locs, job["location_scope"]),
        locations=loc_lines,
        salary=salary_text(job),
        salary_stated=bool(job["salary_stated"]),
        source_name=job["source_name"],
        salary_from_text=job["salary_source"] == "text",
        partial=job["description_completeness"] == "partial",
        pasted=job["description_completeness"] == "pasted",
    )
    fit = conn.execute(_FIT_SQL, (group_id,)).fetchone()
    evidence: Any = []
    if fit is not None:
        c = compute_row(fit, job, locs, profile)
        dims = _json(fit["dimensions"], {})
        if not isinstance(dims, dict):
            dims = {}
        d.scored = True
        d.bucket, d.overall, d.verdict, d.tier = (
            c.bucket.value,
            c.overall,
            c.verdict.value,
            fit["tier"],
        )
        for name in ("skills", "seniority", "domain"):
            dim = dims.get(name)
            if isinstance(dim, dict):
                d.dims.append((name, int(dim.get("score") or 0), str(dim.get("why") or "")))
        d.recency_skills, d.raw_skills = c.recency_weighted_skills, c.raw_skills
        d.stale_skills = _strs(dims.get("stale_skills"))
        d.comp, d.location_fit = c.comp, c.location
        d.blockers = c.blockers
        d.missing_info = _strs(_json(fit["missing_info"], []))
        d.shape_flags = _strs(_json(fit["shape_flags"], []))
        d.tailoring_hints = _strs(_json(fit["tailoring_hints"], []))
        d.evidence_unverified = bool(fit["evidence_unverified"])
        evidence = fit["evidence"]
        keys = fit.keys()
        d.served_model = fit["served_model"] if "served_model" in keys else None
        if "evidence_mode" in keys and fit["evidence_mode"] == "none":
            d.evidence_mode = "none"
            _decisions_view(d, _json(fit["decisions"], {}))
    d.text = highlight(job["description_text"], evidence)
    sources = {r["key"]: r["name"] for r in conn.execute("SELECT key, name FROM source")}
    for m in group_members(conn, group_id):
        if m["id"] == job["id"]:
            continue
        d.siblings.append(
            {
                "title": m["title"],
                "employer": m["employer"] or m["agency_raw"],
                # None for a pasted job's ``paste:<id>`` placeholder: never shown as a link
                "url": u if is_http_url(u := m["apply_url"] or m["url"]) else None,
                "source": sources.get(m["source_key"], m["source_key"]),
            }
        )
    link = get_apply_link(conn, group_id)
    if link is not None or is_http_url(posting_url(job)):
        d.button = apply_button(conn, group_id, now, link, fetch_link=False)
    # else: a posting pasted without a URL (specs/017) has nowhere to apply; no button.
    d.packet_id = live_packet_id(conn, group_id)
    d.prompt = did_you_apply(conn, group_id)
    index = rejections.RejectionIndex.load(conn, now, rejection_days)
    if index:
        emp = job["employer"] or job["agency_raw"]
        same = index.same_posting(group_id, emp, job["title"])
        if same is not None:
            d.posting_rejection = f"The employer rejected you for this posting on {same.day}."
        elif (hit := index.prior(group_id, emp, job["title"])) is not None:
            d.employer_rejection = hit.flag()
    return d


# ─── writes ─────────────────────────────────────────────────────────────────


def _iso(now: datetime) -> str:
    return now.astimezone(UTC).isoformat()


def log_click(
    conn: sqlite3.Connection, group_id: int, final_url: str, outcome: str, now: datetime
) -> None:
    """Insert apply_click; move an 'interested' application to 'preparing' (append-only event)."""
    at = _iso(now)
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "INSERT INTO apply_click (job_group_id, at, final_url, outcome) VALUES (?, ?, ?, ?)",
            (group_id, at, final_url, outcome),
        )
        app = conn.execute(
            "SELECT id, status FROM application WHERE job_group_id = ?", (group_id,)
        ).fetchone()
        if app and app["status"] == "interested":
            conn.execute(
                "INSERT INTO application_event (application_id, at, status, note, source) "
                "VALUES (?, ?, 'preparing', ?, 'manual')",
                (app["id"], at, APPLY_CLICK_NOTE),
            )
            rebuild_status(conn, app["id"])  # the cache follows the log (tracking rules)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


MAX_PASTE_CHARS = 100_000


def pasted_html(text: str) -> str:
    """Plain pasted text as escaped HTML paragraphs, so normalize keeps its structure."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", text.replace("\r\n", "\n")) if p.strip()]
    return "".join(
        "<p>" + "<br>".join(str(escape(line)) for line in p.splitlines()) + "</p>" for p in paras
    )


def paste_description(conn: sqlite3.Connection, group_id: int, text: str, now: datetime) -> int:
    """Store a pasted description on the group's canonical job and queue a re-score.

    Sets ``description_completeness = 'pasted'`` (lifting the partial cap), re-normalizes the
    job, and bumps ``job_group.description_rev`` so the screen stage scores the group again.
    Earlier scores are kept; the newest one is shown (specs/012 "Your click finishes the job").
    Returns the job id.
    """
    text = text.strip()
    if not text:
        raise ValueError("empty description")
    job = group_job(conn, group_id)
    if job is None:
        raise KeyError(f"no such job group: {group_id}")
    html = pasted_html(text[:MAX_PASTE_CHARS])
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "UPDATE job SET description_raw = ?, description_completeness = 'pasted', "
            "needs_resolve = 0, last_seen_at = ? WHERE id = ?",
            (html, _iso(now), job["id"]),
        )
        normalize_job(conn, job["id"])
        conn.execute(
            "UPDATE job_group SET description_rev = description_rev + 1 WHERE id = ?",
            (group_id,),
        )
        _refresh_group(conn, group_id)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return job["id"]


def answer_prompt(conn: sqlite3.Connection, group_id: int, choice: str, now: datetime) -> None:
    """Handle the "Did you apply?" answer: yes | not_yet | not_interested."""
    if choice not in ("yes", "not_yet", "not_interested"):
        raise ValueError(choice)
    if choice == "not_interested":
        set_label(conn, group_id, "not_interesting")
        return
    at = _iso(now)
    conn.execute("BEGIN IMMEDIATE")
    try:
        app = conn.execute(
            "SELECT id, status FROM application WHERE job_group_id = ?", (group_id,)
        ).fetchone()
        yes = choice == "yes"
        if app is None:
            status = "applied" if yes else "preparing"
            cur = conn.execute(
                "INSERT INTO application (job_group_id, status, applied_at, created_at, "
                "updated_at) VALUES (?, ?, ?, ?, ?)",
                (group_id, status, at if yes else None, at, at),
            )
            app_id = cur.lastrowid
        else:
            app_id = app["id"]
            if yes:
                status = "applied"
                conn.execute(
                    "UPDATE application SET status = 'applied', applied_at = ?, updated_at = ? "
                    "WHERE id = ?",
                    (at, at, app_id),
                )
            else:
                status = app["status"]  # the event just records the answer
                conn.execute("UPDATE application SET updated_at = ? WHERE id = ?", (at, app_id))
        conn.execute(
            "INSERT INTO application_event (application_id, at, status, note, source) "
            "VALUES (?, ?, ?, ?, 'manual')",
            (app_id, at, status, "marked applied" if yes else "not applied yet"),
        )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
