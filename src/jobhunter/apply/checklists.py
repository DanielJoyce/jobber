"""Per-board checklists for the packet page (specs/017 "Checklists"), in resume-first order.

Data, plus which board a packet's posting is on. Nothing here fetches anything: the board comes
from the stored apply link, the job's URLs (``pipeline/ats_rules.py``) and its source family.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

from jobhunter.pipeline.ats_rules import match_ats

# The four public ATSs phase 2 targets (and ``jobhunter apply stats`` counts for its gate).
PUBLIC_ATS = ("greenhouse", "lever", "ashby", "workable")


@dataclass(frozen=True)
class Checklist:
    board: str
    title: str
    steps: tuple[str, ...]
    warning: str | None = None


CHECKLISTS: dict[str, Checklist] = {
    "any": Checklist(
        "any",
        "Any ATS",
        (
            'Attach the resume first (or use the site\'s "Autofill from resume" box), then let '
            "the site and your fill helper fill the form.",
            "Check the parsed work history against your resume.",
            "Answer the custom questions: drafts and saved answers are on this page.",
            "Salary, EEO and self-ID, consent and signatures are yours to answer by hand.",
            "Review the whole form.",
            "Submit it yourself.",
        ),
        "Do not re-upload the file after correcting fields: most ATSs re-parse it and overwrite "
        "your corrections.",
    ),
    "usajobs": Checklist(
        "usajobs",
        "USAJOBS",
        (
            "Sign in via login.gov.",
            "Build or pick the resume in USAJOBS, and check the announcement's rules: page "
            "limit, month/year dates, hours per week.",
            "Attach the documents the announcement lists.",
            "Answer the questionnaire. For a self-assessment question, paste it below to see "
            "the resume lines that may be relevant; the level is yours to choose.",
            "Review, then submit it yourself.",
        ),
    ),
    "workday": Checklist(
        "workday",
        "Workday",
        (
            "Sign in to the employer's Workday account (your password manager).",
            "Upload the resume.",
            "Check the parsed history: Workday often splits or merges jobs.",
            "Then My Information and Application Questions.",
            "Disclosures are yours to answer.",
            "Review, then submit it yourself.",
        ),
    ),
    "neogov": Checklist(
        "neogov",
        "NEOGOV (governmentjobs.com)",
        (
            "Fill in the profile fields.",
            "Supplemental questions: draft each one above; drafts are checked like letters.",
            "Review, then submit it yourself.",
        ),
    ),
}

_BOARD_OF_ATS = {"workday": "workday", "neogov": "neogov", "usastaffing": "usajobs"}


def packet_urls(conn: sqlite3.Connection, packet_id: int) -> Mapping[str, str | None]:
    row = conn.execute(
        "SELECT l.ats, l.final_url, l.start_url, j.apply_url, j.url, s.family "
        "FROM application_packet p JOIN application a ON a.id = p.application_id "
        "JOIN job_group g ON g.id = a.job_group_id JOIN job j ON j.id = g.canonical_job_id "
        "LEFT JOIN apply_link l ON l.job_group_id = g.id "
        "LEFT JOIN source s ON s.key = j.source_key WHERE p.id = ?",
        (packet_id,),
    ).fetchone()
    return dict(row) if row else {}


def ats_of(urls: Mapping[str, str | None]) -> str | None:
    """The ATS a posting is on: the resolved apply link's, else the first URL a rule matches."""
    if urls.get("ats"):
        return urls["ats"]
    for key in ("final_url", "start_url", "apply_url", "url"):
        if (u := urls.get(key)) and re.match(r"https?://", u) and (rule := match_ats(u)):
            return rule.name
    return None


def board_of(urls: Mapping[str, str | None]) -> str:
    if urls.get("family") == "usajobs":
        return "usajobs"
    for key in ("final_url", "start_url", "apply_url", "url"):
        host = (urlsplit(urls.get(key) or "").hostname or "").lower()
        if host == "usajobs.gov" or host.endswith(".usajobs.gov"):
            return "usajobs"  # a pasted USAJOBS announcement
    return _BOARD_OF_ATS.get(ats_of(urls) or "", "any")


def for_packet(conn: sqlite3.Connection, packet_id: int) -> Checklist:
    return CHECKLISTS[board_of(packet_urls(conn, packet_id))]


_STOP_WORDS = (
    "about above after again also and any are been being both but can could did does doing "
    "during each few for from had has have having her here how into its just more most must "
    "not now off once only other our out over own same should some such than that the their "
    "them then there these they this those through too under until very was were what when "
    "where which while who whom why will with would you your yours "
    "experience experienced level following describe select statement best"
)
_STOP = frozenset(_STOP_WORDS.split())


def relevant_lines(
    lines: Mapping[str, str], statement: str, limit: int = 8
) -> list[tuple[str, str]]:
    """Resume lines that may be relevant to a federal self-assessment statement: those sharing
    the most content words with it. Shown, never turned into a level (specs/017)."""
    words = {
        w[:6] for w in re.findall(r"[a-z][a-z0-9+#.-]{3,}", statement.casefold()) if w not in _STOP
    }
    if not words:
        return []
    scored: list[tuple[int, str, str]] = []
    for lid, text in lines.items():
        if not lid.startswith("L"):
            continue
        have = {w[:6] for w in re.findall(r"[a-z][a-z0-9+#.-]{3,}", text.casefold())}
        hits = len(words & have)
        if hits:
            scored.append((hits, lid, text))
    scored.sort(key=lambda x: (-x[0], int(x[1][1:]) if x[1][1:].isdigit() else 0))
    return [(lid, text) for _hits, lid, text in scored[:limit]]
