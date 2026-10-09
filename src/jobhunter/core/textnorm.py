"""Whitespace, html-to-text, salary, date and enum detection (specs/004 "normalize").

Pure functions, no I/O. Rule: never invent a value; unparseable input yields None plus a warning.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime

import dateparser
from selectolax.lexbor import LexborHTMLParser as HTMLParser
from selectolax.lexbor import LexborNode as Node

# ─── html -> text ───────────────────────────────────────────────────────────

_DROP = {"script", "style", "noscript", "head", "template"}
_PARA = {
    "p", "div", "section", "article", "header", "footer", "blockquote", "pre", "table",
    "h1", "h2", "h3", "h4", "h5", "h6", "hr", "aside", "main", "form", "fieldset",
}  # fmt: skip
_LINE = {"ul", "ol", "tr", "dl", "dt", "dd", "br"}
_WS = re.compile(r"\s+")


def _walk(node: Node, out: list[str]) -> None:
    child = node.child
    while child is not None:
        tag = child.tag
        if tag == "-text":
            out.append(_WS.sub(" ", child.text(deep=False)))
        elif tag in _DROP or tag.startswith("-"):
            pass
        elif tag == "li":
            out.append("\n- ")
            _walk(child, out)
            out.append("\n")
        elif tag in _PARA:
            out.append("\x01")
            _walk(child, out)
            out.append("\x01")
        elif tag in _LINE:
            out.append("\n")
            _walk(child, out)
            out.append("\n")
        else:
            _walk(child, out)
        child = child.next


def html_to_text(html: str | None) -> str:
    """Strip tags, decode entities, collapse whitespace; keep paragraphs and ``- `` list items."""
    if not html:
        return ""
    tree = HTMLParser(html)
    out: list[str] = []
    if tree.root is not None:
        _walk(tree.root, out)
    text = "".join(out).replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"[\s\x01]*\x01[\s\x01]*", "\x02", text)  # runs holding a paragraph break
    text = re.sub(r"\s*\n\s*", "\n", text)
    text = re.sub(r"(?m)^- ?$", "", text)  # empty bullets
    text = text.replace("\x02", "\n\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ─── salary ─────────────────────────────────────────────────────────────────


@dataclass
class SalaryParse:
    min: float | None = None
    max: float | None = None
    period: str | None = None  # hour | day | week | month | year
    currency: str | None = None
    stated: bool = False
    warnings: list[str] = field(default_factory=list)


_NUM = r"(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s*([kK])?(?![\w])"
_SEP = r"\s*(?:-|–|—|to)\s*"  # noqa: RUF001
_RANGE_DOLLAR = re.compile(rf"\$\s*{_NUM}(?:{_SEP}\$?\s*{_NUM})?")
_PERIOD_AHEAD = (
    r"(?=\s*(?:/|per\b|an?\b)?\s*(?:hr|hrs|hour|hourly|day|daily|week|weekly|bi-?weekly"
    r"|month|monthly|mo|yr|year|annum|annual|annually)\b)"
)
_RANGE_BARE = re.compile(rf"(?<![\w.$]){_NUM}(?:{_SEP}{_NUM})?{_PERIOD_AHEAD}", re.I)

_PERIODS: list[tuple[str, re.Pattern[str]]] = [
    ("biweekly", re.compile(r"bi-?\s?weekly|every\s+(?:two|2)\s+weeks", re.I)),
    (
        "hour",
        re.compile(r"/\s*h(?:ou)?rs?\b|\bhourly\b|\bper\s+hour\b|\ban\s+hour\b|\bhrs?\b", re.I),
    ),
    ("day", re.compile(r"/\s*day\b|\bdaily\b|\bper\s+day\b|\ba\s+day\b", re.I)),
    ("week", re.compile(r"/\s*(?:wk|week)\b|\bweekly\b|\bper\s+week\b|\ba\s+week\b", re.I)),
    ("month", re.compile(r"/\s*(?:mo|month)\b|\bmonthly\b|\bper\s+month\b|\ba\s+month\b", re.I)),
    (
        "year",
        re.compile(
            r"/\s*(?:yr|year)\b|\bannual(?:ly)?\b|\bper\s+(?:year|annum|yr)\b|\ba\s+year\b"
            r"|\byearly\b|\bp\.?a\.?(?:\W|$)|\byr\b",
            re.I,
        ),
    ),
]
_MAX_ONLY = re.compile(r"(?:up\s+to|max(?:imum)?(?:\s+of)?|not\s+to\s+exceed)\s*$", re.I)
_MIN_ONLY = re.compile(r"(?:from|starting(?:\s+at)?|min(?:imum)?(?:\s+of)?|at\s+least)\s*$", re.I)

ANNUAL_FACTORS = {"hour": 2080, "day": 260, "week": 52, "month": 12, "year": 1}


def _amount(num: str, k: str | None) -> float:
    v = float(num.replace(",", ""))
    return v * 1000 if k else v


def parse_salary(raw: str | None) -> SalaryParse:
    """Parse free-text pay into numbers. Anything not clearly numeric yields all-None."""
    res = SalaryParse()
    if raw is None or not raw.strip():
        return res
    text = raw.strip()
    m = _RANGE_DOLLAR.search(text) or _RANGE_BARE.search(text)
    if m is None:
        res.warnings.append(f"salary unparseable: {text[:80]!r}")
        return res

    n1, k1, n2, k2 = m.group(1), m.group(2), m.group(3), m.group(4)
    lo: float | None = _amount(n1, k1)
    hi: float | None = _amount(n2, k2) if n2 else None
    if lo is not None and hi is not None and k2 and not k1 and lo < 1000 <= hi:
        lo *= 1000  # "$85-110K"
    prefix = text[: m.start()]
    suffix = text[m.end() :]
    if hi is None:
        if _MAX_ONLY.search(prefix):
            lo, hi = None, lo
        elif suffix.lstrip().startswith("+") or _MIN_ONLY.search(prefix):
            hi = None
        else:
            hi = lo  # single figure: min == max
    if lo is not None and hi is not None and lo > hi:
        lo, hi = hi, lo
        res.warnings.append("salary range reversed; swapped")

    period: str | None = None
    for name, pat in _PERIODS:
        if pat.search(text):
            period = name
            break
    if period == "biweekly":
        lo = lo / 2 if lo is not None else None
        hi = hi / 2 if hi is not None else None
        period = "week"
        res.warnings.append("biweekly pay converted to weekly (/2)")
    elif period is None:
        ref = hi if hi is not None else lo
        if ref is not None and ref >= 10000:
            period = "year"
            res.warnings.append("salary period inferred as year from magnitude")
        else:
            res.warnings.append("salary period not stated")

    res.min, res.max, res.period, res.stated = lo, hi, period, True
    if "$" in text or re.search(r"\bUSD\b", text, re.I):
        res.currency = "USD"
    return res


def annualize(
    min_: float | None, max_: float | None, period: str | None
) -> tuple[float | None, float | None]:
    """Convert a pay range to annual (hour*2080, day*260, week*52, month*12)."""
    factor = ANNUAL_FACTORS.get(period or "")
    if factor is None:
        return None, None
    return (
        None if min_ is None else min_ * factor,
        None if max_ is None else max_ * factor,
    )


# ─── dates ──────────────────────────────────────────────────────────────────


def parse_date(raw: str | None, tz: str = "UTC") -> datetime | None:
    """Parse a free-text date in the source's timezone; returns an aware datetime or None."""
    if raw is None or not raw.strip():
        return None
    try:
        return dateparser.parse(
            raw.strip(),
            settings={
                "PREFER_DATES_FROM": "past",
                "RETURN_AS_TIMEZONE_AWARE": True,
                "TIMEZONE": tz,
            },
        )
    except Exception:  # dateparser raises on odd tz names / overflow
        return None


# ─── remote / employment type ───────────────────────────────────────────────

_NOT_REMOTE = re.compile(
    r"\b(?:not|no|non)[\s-]+(?:a\s+)?(?:remote|telework|telecommut\w*)\b"
    r"|\b(?:remote|telework)\s+(?:work\s+)?(?:is\s+)?not\s+(?:available|eligible|allowed|offered)",
    re.I,
)
_HYBRID = re.compile(
    r"\bhybrid\b|\btelework\s+(?:eligible|available|option\w*)\b|\bpartially\s+remote\b", re.I
)
_REMOTE = re.compile(r"\bremote\b|\bwork(?:ing)?\s+from\s+home\b|\bwfh\b|\btelecommut\w+", re.I)
_ONSITE = re.compile(r"\bon[\s-]?site\b|\bin[\s-]person\b|\bin[\s-]office\b", re.I)


def detect_remote(title: str | None, location_raw: str | None, description: str | None) -> str:
    """Cheap regex: onsite | hybrid | remote | unknown. Title/location outrank description."""
    head = f"{title or ''}\n{location_raw or ''}"
    body = description or ""
    if _NOT_REMOTE.search(head) or _NOT_REMOTE.search(body):
        return "onsite"
    for text in (head, body):
        if _HYBRID.search(text):
            return "hybrid"
        if _REMOTE.search(text):
            return "remote"
    if _ONSITE.search(head) or _ONSITE.search(body):
        return "onsite"
    return "unknown"


_EMP: list[tuple[str, re.Pattern[str]]] = [
    ("seasonal", re.compile(r"\bseasonal\b", re.I)),
    (
        "temporary",
        re.compile(
            r"\btemporary\b|\btemp\b|\blimited[\s-]term\b|\bterm[\s-]limited\b"
            r"|\bnot[\s-]to[\s-]exceed\b",
            re.I,
        ),
    ),
    (
        "contract",
        re.compile(
            r"\bcontractor\b|\bcontract\s+(?:position|role|basis|to\s+hire)\b"
            r"|\bcontract\b(?=\s*[-,/(]|\s*$)|\b1099\b|\bfixed[\s-]term\b",
            re.I,
        ),
    ),
    ("part_time", re.compile(r"\bpart[\s-]?time\b", re.I)),
    ("full_time", re.compile(r"\bfull[\s-]?time\b|\bpermanent\b", re.I)),
]


def detect_employment_type(text: str | None) -> str:
    """Return an EmploymentType value; first match in priority seasonal > ... > full_time."""
    if not text:
        return "unknown"
    for name, pat in _EMP:
        if pat.search(text):
            return name
    return "unknown"


# ─── hashing ────────────────────────────────────────────────────────────────


def _norm(s: str | None) -> str:
    return " ".join((s or "").lower().split())


def content_hash(
    title: str | None,
    employer: str | None,
    location_raw: str | None,
    description_text: str | None,
) -> str:
    """sha256 over lowercase, whitespace-collapsed fields."""
    payload = "\x1f".join(_norm(x) for x in (title, employer, location_raw, description_text))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
