"""Which of a job group's resolved apply links the button uses (bug 334d9eb, specs/015).

A group gathers copies of one opening from several sources; each copy's start URL is resolved
on its own (``applylink.resolve_group``) and the results are ranked by where they **end**:

1. a known applicant tracking system (``ats_rules.ATS_RULES`` and ``OTHER_ATS``);
2. the employer's own site: a host that names the employer;
3. anything else;
4. a click tracker the chain never got past (aplitrak, appcast, ...);
5. job boards, aggregators and state job banks (``ats_rules.AGGREGATOR_HOSTS``, LinkedIn and
   Indeed, and the hosts of class A sources).

Login and single-sign-on walls never count as the posting. A closed page beats a live one only
on an ATS or the employer's site (a dead tracker or board copy says nothing about the job). A
link set by hand, then a capture of the employer's page (a captured page on an ATS or a host
naming the employer; never a board), beat any ranking. Ties: the final URL more members
reached, then the most recently seen member.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from enum import IntEnum

from jobhunter.pipeline.ats_rules import (
    AGGREGATOR_HOSTS,
    TRACKER_HOSTS,
    ats_name,
    host_in,
    host_of,
    is_http_url,
)
from jobhunter.pipeline.board_ids import is_board_host


class HostTier(IntEnum):
    ats = 0
    employer = 1
    other = 2
    tracker = 3
    aggregator = 4


TIER_LABEL = {
    HostTier.ats: "employer's applicant tracking system",
    HostTier.employer: "employer's site",
    HostTier.other: "best link found",
    HostTier.tracker: "redirect not followed to the end",
    HostTier.aggregator: "job board copy; no employer link found",
}

_LEGAL = frozenset(
    {"inc", "llc", "ltd", "corp", "corporation", "company", "co", "plc", "lp", "llp", "gmbh",
     "sa", "ag", "bv", "nv", "pbc", "the"}
)  # fmt: skip
# First words too common to stand for an employer on their own.
_GENERIC = frozenset(
    {"state", "city", "county", "university", "department", "dept", "national", "american",
     "united", "global", "international", "first", "general", "north", "south", "east",
     "west", "bank", "health", "medical", "community", "office", "division", "bureau",
     "commonwealth", "town", "village", "school", "college", "center", "group", "services",
     "public", "federal", "us", "usa", "new", "great"}
)  # fmt: skip
_WORD = re.compile(r"[a-z0-9]+")


def employer_keys(name: str | None) -> frozenset[str]:
    """Host labels that would name this employer: ``Cribl, Inc.`` -> {cribl};
    ``Acme Widget Co`` -> {acmewidget, acme}."""
    words = [w for w in _WORD.findall((name or "").lower()) if w not in _LEGAL]
    if not words:
        return frozenset()
    keys = {"".join(words)}
    if len(words[0]) >= 4 and words[0] not in _GENERIC:
        keys.add(words[0])
    return frozenset(k for k in keys if len(k) >= 3)


def _bare(host: str) -> str:
    return host[4:] if host.startswith("www.") else host


def _source_patterns(host: str) -> tuple[str, ...]:
    """A class A source's entry host as given (``www.ncworks.gov``), and without ``www.`` with
    its subdomains; a bare two-label state domain (``nj.gov``) covers only itself, since the
    state's agencies live under it too."""
    bare = _bare(host)
    if bare.count(".") == 1 and bare.endswith(".gov"):
        return tuple(dict.fromkeys((host, bare)))
    return tuple(dict.fromkeys((host, bare, f"*.{bare}")))


@dataclass(frozen=True)
class HostClasses:
    """Everything ranking needs to place a host, loaded once per group."""

    employer_keys: frozenset[str] = frozenset()
    aggregator_hosts: tuple[str, ...] = AGGREGATOR_HOSTS

    @classmethod
    def load(cls, conn: sqlite3.Connection, group_id: int) -> HostClasses:
        patterns: list[str] = list(AGGREGATOR_HOSTS)
        for r in conn.execute(
            "SELECT entry FROM source WHERE class = 'A' AND family NOT IN ('mailalerts', 'manual')"
        ):
            if is_http_url(r[0] or ""):
                patterns += _source_patterns(host_of(r[0]))
        keys: set[str] = set()
        for r in conn.execute(
            "SELECT j.url, j.employer, j.agency_raw, s.class FROM job j "
            "LEFT JOIN source s ON s.key = j.source_key WHERE j.job_group_id = ?",
            (group_id,),
        ):
            keys |= employer_keys(r["employer"] or r["agency_raw"])
            if r["class"] == "A" and is_http_url(r["url"] or ""):
                patterns.append(host_of(r["url"]))  # the listing's own board
        return cls(frozenset(keys), tuple(dict.fromkeys(patterns)))

    def is_aggregator(self, host: str) -> bool:
        return is_board_host(host) or host_in(host, self.aggregator_hosts)

    def is_employer(self, host: str) -> bool:
        """A host that names the employer: ``cribl.io`` for Cribl, ``careers.toasttab.com``
        for Toast (a label starting with a key of five or more letters)."""
        labels = [lbl.replace("-", "") for lbl in host.split(".")[:-1]]
        return any(
            lbl == k or (len(k) >= 5 and lbl.startswith(k))
            for lbl in labels
            for k in self.employer_keys
        )

    def employer_or_ats(self, url: str | None) -> bool:
        """Whether a page can be the employer's own posting: what a capture must be to win."""
        return self.tier(url) in (HostTier.ats, HostTier.employer)

    def tier(self, url: str | None) -> HostTier:
        if not url:
            return HostTier.other
        host = host_of(url)
        if ats_name(url) is not None:
            return HostTier.ats
        if self.is_aggregator(host):
            return HostTier.aggregator
        if self.is_employer(host):
            return HostTier.employer
        if host_in(host, TRACKER_HOSTS):
            return HostTier.tracker
        return HostTier.other
