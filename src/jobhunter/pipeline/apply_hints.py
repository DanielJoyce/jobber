"""Apply links a posting gives in its own text (bug 3bab68d, specs/015).

Readers, no I/O:

- ``description_hints``: links in a stored description that say where to apply. HTML anchors
  (kept in ``description_raw`` by most HTML sources), Markdown links (NLx descriptions arrive as
  Markdown and keep ``[text](url)`` as text) and typed URLs. A link is *cued* when its text, or
  the words just before it, say to apply (``Apply online: https://...``, ``[Apply here](...)``);
  an ATS posting named anywhere is kept uncued. Links about privacy, arbitration,
  accommodations, accessibility, EEO, benefits, referrals, talent pools, "how to apply" guides
  and the like never count, whether those words come before the link or in the rest of its
  sentence.
- ``page_apply_hrefs``: a job page's Apply buttons (``<a>`` whose text, label, id or class
  says apply). The resolver uses them to continue a walk that landed on a job board's copy.
- ``matches_job``: whether a link is this job's own posting (a shared id, or every word of the
  title in its path), not a sibling requisition or a talent-pool link in the footer.

The resolver (``applylink._candidates``) decides what each hint may do: a hint that matches the
job is an ordinary start; a cued ATS posting that does not is only a fallback.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html import unescape
from typing import Literal
from urllib.parse import urljoin, urlsplit

from selectolax.lexbor import LexborHTMLParser, LexborNode

from jobhunter.core.textnorm import html_to_text
from jobhunter.pipeline.ats_rules import ats_name, host_of, is_http_url, match_ats

# Hints used per job (after the resolver's filter): each one may cost a request.
MAX_HINTS = 3
# How far back (characters) the words before a typed URL are read for an apply cue.
CUE_WINDOW = 90
# How far forward the rest of a link's sentence is read for boilerplate words.
AFTER_WINDOW = 120

# Link text that is an Apply button or says where to apply.
_APPLY_TEXT = re.compile(
    r"\bappl(?:y|ying)\b|\bonline application\b|\bapplication (?:portal|form|site|page)\b"
    r"|\bjob portal\b|\bcareers? (?:site|portal)\b",
    re.I,
)
# Words before a typed URL that say "apply there" (the clause right before the URL).
_APPLY_CUE = re.compile(
    r"\bappl(?:y|ying)\b|\bapplications? (?:are |is )?(?:accepted|available|submitted|can|may|must"
    r"|should)\b|\bonline application\b|\bapplication (?:portal|form|site|page)\b"
    r"|\bjob portal\b|\bcareers? (?:site|portal)\b"
    r"|\bsubmit (?:an |your |a )?(?:application|resume)",
    re.I,
)
# Text that marks a link as boilerplate even when "apply" or "application" is near it.
_NOT_APPLY = re.compile(
    r"privacy|arbitration|accommodat|accessib|\beeo\b|equal (?:employment )?opportunit"
    r"|e-?verify|benefit|know your rights|disabilit|\bpolicy\b|\bterms\b|cookie|disclaimer"
    r"|transparen|unsubscribe|\bfaq|learn more|agree|\bnotice\b|reasonable|referral"
    r"|talent (?:pool|community|network)",
    re.I,
)
# Link labels for guides, not applications ("How to apply" opens a video or a help page). Only
# labels: "How to apply: apply online at https://..." is a real cue.
_HOW_TO = re.compile(r"\bhow to apply\b|\bapplicant tips\b|\bapplying (?:tips|guide)", re.I)
_NOT_APPLY_PATH = re.compile(
    r"privacy|legal|arbitration|accommodat|accessib|\beeo|e-?verify|benefit|polic|terms"
    r"|cookie|disclos|transparen|\.pdf$|\.docx?$|\.png$|\.jpe?g$|\.gif$|knowyourrights"
    r"|talent-?pool|referral",
    re.I,
)
_MD_LINK = re.compile(r"\[([^\[\]]{0,300})\]\(\s*((?:https?://|www\.)[^\s)]+)\s*\)", re.I)
_TYPED = re.compile(r"(?<![\w/@.\-\]])(?:https?://|www\.)[^\s<>\"'()\[\]]+", re.I)
_MD_ESCAPE = re.compile(r"\\([\\_*\[\]()#~`.\-+!])")
_CLAUSE_END = re.compile(r"(?:[.!?](?=\s)|\n|\)|;)")
_SENTENCE_END = re.compile(r"[.!?](?:\s|$)|\n")
_TRAILING = ".,;:!?*'\""


def _clean_url(raw: str, base: str | None = None) -> str | None:
    """An absolute http(s) URL, or None for anything malformed (``http://[object Object]``)."""
    try:
        url = unescape(unescape(_MD_ESCAPE.sub(r"\1", raw.strip()))).rstrip(_TRAILING)
        if url.lower().startswith("www."):
            url = "https://" + url
        if base and not urlsplit(url).scheme:
            url = urljoin(base, url)
        return url if is_http_url(url) and "." in host_of(url) else None
    except ValueError:  # urlsplit and urljoin reject e.g. "http://[" and "//[x"
        return None


# Posting shapes on ATS hosts known only by host (``ats_rules.OTHER_ATS``). A host with no
# entry here never counts as a posting: its list and careers pages look like anything else.
OTHER_ATS_POSTINGS: dict[str, re.Pattern[str]] = {
    # Infor HCM: a per-posting short link, or the JobPosting page it redirects to.
    "infor": re.compile(r"/hcm/xmlhttp/shorturl\.do\?(?:.*&)?key=\w+|/JobPosting\["),
}


def ats_posting(url: str) -> bool:
    """A posting on a known ATS, not its home, list or search page."""
    rule = match_ats(url)
    if rule is not None:
        return rule.is_posting(url)
    pattern = OTHER_ATS_POSTINGS.get(ats_name(url) or "")
    if pattern is None:
        return False
    p = urlsplit(url)
    return bool(pattern.search(p.path + ("?" + p.query if p.query else "")))


_ID = re.compile(r"(?<![a-z0-9])\d{4,}(?![a-z0-9])")
_YEAR = re.compile(r"(?:19|20)\d\d")
# A zip code after a state code in a posting URL ("...-FL-32501/"): a place, not an id.
_ZIP = re.compile(r"(?<![a-z0-9])[a-z]{2}-(\d{5})(?![0-9])")
_WORDS = re.compile(r"[a-z0-9]+")
_STOP = frozenset({"and", "the", "for", "with", "from", "of", "a", "an", "to", "in", "at", "or"})


def _ids(text: str | None) -> set[str]:
    """Numbers that can name a posting: four digits or more, not a year (2026), not a short
    number padded with zeros (0001), not a zip code after a state code."""
    low = (text or "").lower()
    zips = set(_ZIP.findall(low))
    return {
        n
        for n in _ID.findall(low)
        if not (len(n) == 4 and _YEAR.fullmatch(n)) and len(n.lstrip("0")) >= 4 and n not in zips
    }


def match_kind(
    url: str, *, title: str | None, own: list[str | None]
) -> Literal["id", "title"] | None:
    """How ``url`` names this job, if it does: ``"id"`` when it shares an id number (``_ids``)
    with one of the job's own links or ids (``own``); ``"title"`` when every word of the title
    is in its path (levels and letters too: "Manager I" never matches "Manager II") and no own
    link on the same host names a different number (a sibling requisition). A title match is
    weaker: a same-title opening elsewhere matches too (bug 5c1d738)."""
    try:
        p = urlsplit(url)
        hay = f"{p.path}?{p.query}".lower()
        hint_ids = _ids(hay)
        own_ids: set[str] = set()
        for o in own:
            own_ids |= _ids(o)
        if hint_ids & own_ids:
            return "id"
        host = host_of(url)
        for o in own:
            if o and _ids(o) and host_of(o) == host:
                return None  # the job's own posting on this host carries another number
    except ValueError:  # a malformed URL
        return None
    words = [w for w in _WORDS.findall((title or "").lower()) if w not in _STOP]
    tokens = set(_WORDS.findall(hay))
    return "title" if words and all(w in tokens for w in words) else None


def matches_job(url: str, *, title: str | None, own: list[str | None]) -> bool:
    """Whether ``url`` is this job's own posting (``match_kind``): a shared id number, or
    every word of the title, and not a sibling requisition."""
    return match_kind(url, title=title, own=own) is not None


def _boilerplate(url: str, *texts: str) -> bool:
    return bool(_NOT_APPLY_PATH.search(urlsplit(url).path)) or any(
        _NOT_APPLY.search(t) for t in texts if t
    )


def _clause_before(text: str, start: int) -> str:
    """The words just before ``start``, back to the end of the previous sentence or line.
    A URL alone on its line takes the line before only when that line ends with a colon
    (``Apply here:`` then the URL); after a full sentence it has no cue."""
    window = text[max(0, start - CUE_WINDOW) : start]
    ends = list(_CLAUSE_END.finditer(window))
    if not ends:
        return window
    tail = window[ends[-1].end() :]
    if tail.strip():
        return tail
    head = window[: ends[-1].start()].rstrip()
    if not head.endswith(":"):
        return ""
    return _clause_before(head, len(head))


def _clause_after(text: str, end: int) -> str:
    """The rest of the sentence after a link: ``careers site (URL) accessible to all``."""
    window = text[end : end + AFTER_WINDOW]
    m = _SENTENCE_END.search(window)
    return window[: m.start()] if m else window


def _markdownize(html: str, base: str | None) -> str:
    """The description as text with each anchor written as ``[text](href)``."""
    tree = LexborHTMLParser(html)
    for a in tree.css("a[href]"):
        href = (a.attributes.get("href") or "").strip()
        label = " ".join((a.text(deep=True) or "").split()).replace("[", "(").replace("]", ")")
        url = _clean_url(href, base) if href else None
        if url is None:
            a.replace_with(label)
        else:
            a.replace_with(f"[{label}]({url})")
    return html_to_text(tree.html)


@dataclass(frozen=True)
class Hint:
    url: str
    cued: bool  # its own text, or the words before it, say to apply


def description_hints(raw: str | None, base: str | None = None) -> list[Hint]:
    """Apply links named in a description (HTML, Markdown, or plain text), best first:
    links whose own text says apply, then typed URLs after an apply cue (both cued), then ATS
    postings named anywhere (uncued). ``base`` absolutizes relative hrefs (the posting's URL).
    Every hint is returned; the resolver filters and caps them."""
    if not raw or not raw.strip():
        return []
    text = _markdownize(raw, base) if "<a" in raw.lower() else html_to_text(raw)
    text = unescape(text)
    found: list[tuple[int, int, str]] = []  # (rank, position, url)
    spans: list[tuple[int, int]] = []
    for m in _MD_LINK.finditer(text):
        spans.append(m.span())
        url = _clean_url(m.group(2))
        if url is None:
            continue
        label = m.group(1)
        before = _clause_before(text, m.start())
        typed_label = bool(_TYPED.fullmatch(label.strip())) or not label.strip()
        if _HOW_TO.search(label) or _boilerplate(url, label, before, _clause_after(text, m.end())):
            continue
        if not typed_label and _APPLY_TEXT.search(label):
            found.append((0, m.start(), url))
        elif _APPLY_CUE.search(before):
            found.append((1, m.start(), url))
        elif ats_posting(url):
            found.append((2, m.start(), url))
    for m in _TYPED.finditer(text):
        if any(s <= m.start() < e for s, e in spans):
            continue
        url = _clean_url(m.group(0))
        if url is None:
            continue
        before = _clause_before(text, m.start())
        if _boilerplate(url, before, _clause_after(text, m.end())):
            continue
        if _APPLY_CUE.search(before):
            found.append((1, m.start(), url))
        elif ats_posting(url):
            found.append((2, m.start(), url))
    out: dict[str, Hint] = {}
    for rank, _, url in sorted(found):
        out.setdefault(url, Hint(url, cued=rank < 2))
    return list(out.values())


def _button_label(node: LexborNode) -> str:
    attrs = node.attributes
    parts = [
        node.text(deep=True) or "",
        attrs.get("aria-label") or "",
        attrs.get("title") or "",
        attrs.get("id") or "",
        (attrs.get("class") or "").replace("-", " ").replace("_", " "),
    ]
    return " ".join(" ".join(parts).split())


def page_apply_hrefs(html: str | None, base: str) -> list[str]:
    """A job page's Apply button targets (absolute http(s) URLs), off-site ones first, all of
    them: the resolver takes the first that qualifies. Navigation and boilerplate links
    (``How to apply``, privacy, accommodations) never count; malformed hrefs are skipped."""
    if not html:
        return []
    tree = LexborHTMLParser(html)
    here = host_of(base)
    out: list[str] = []
    for a in tree.css("a[href]"):
        href = (a.attributes.get("href") or "").strip()
        if not href or href.startswith("#"):
            continue
        url = _clean_url(href, base)
        if url is None or url.split("#")[0] == base.split("#")[0]:
            continue
        label = _button_label(a)
        if not _APPLY_TEXT.search(label) or _HOW_TO.search(label) or _boilerplate(url, label):
            continue
        if url not in out:
            out.append(url)
    out.sort(key=lambda u: host_of(u) == here)  # stable: off-site first, page order kept
    return out


__all__ = [
    "MAX_HINTS",
    "OTHER_ATS_POSTINGS",
    "Hint",
    "ats_posting",
    "description_hints",
    "match_kind",
    "matches_job",
    "page_apply_hrefs",
]
