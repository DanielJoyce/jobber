"""Pay extraction from free description text (specs/003 "Salary from description text").

Pure and deterministic. Structured salary fields always win; this only runs when they gave
nothing. Precision over recall: a figure is returned only when pay context, a period or a
currency tag backs it up, the magnitude is sane for the period, and it is not a bonus, a
funding figure or a benefit amount.

Placeholders lose: some boards fill a fixed "Compensation" field with the federal minimum wage
($7.25/hr) on every posting, so that figure is never taken as pay.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

CONFIDENT = 0.6  # extract_salary_from_text only returns results at or above this
FEDERAL_MIN_WAGE = 7.25  # a placeholder, never the posting's real pay

# Sanity bounds on a stated amount per period (USD).
BOUNDS: dict[str, tuple[float, float]] = {
    "hour": (7, 500),
    "day": (50, 3000),
    "week": (200, 30_000),
    "month": (1_200, 125_000),
    "year": (15_000, 1_500_000),
}

_NUM = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"
_CUR = r"\$|USD\s*\$?"
_SEP = r"\s*(?:--|-|–|—|to)\s*"  # noqa: RUF001
_CAND = re.compile(
    rf"(?<![\w.,])(?:(?P<c1>{_CUR})\s*)?(?P<n1>{_NUM})\s*(?P<k1>[kK](?![A-Za-z]))?"
    rf"(?:{_SEP}(?:(?P<c2>{_CUR})\s*)?(?P<n2>{_NUM})\s*(?P<k2>[kK](?![A-Za-z]))?)?"
    r"(?![\w%])"
)
# "Min USD $130,000.00/Yr. Max USD $160,000.00/Yr."
_MINMAX = re.compile(
    rf"\bmin(?:imum)?\.?\s*:?\s*(?P<ca>{_CUR})?\s*(?P<a>{_NUM})\s*(?P<ka>[kK])?[^\n]{{0,30}}?"
    rf"\bmax(?:imum)?\.?\s*:?\s*(?P<cb>{_CUR})?\s*(?P<b>{_NUM})\s*(?P<kb>[kK])?",
    re.I,
)

_LEAD = r"^\s*(?:\(?USD\)?\s*)?"
_PERIOD_POST: list[tuple[str, re.Pattern[str]]] = [
    ("hour", re.compile(rf"{_LEAD}(?:(?:/|per\b|an?\b)\s*)h(?:ou)?rs?\b|^\s*/?\s*hourly\b", re.I)),
    ("day", re.compile(rf"{_LEAD}(?:/|per\b|a\b)\s*day\b|^\s*daily\b", re.I)),
    ("week", re.compile(rf"{_LEAD}(?:/|per\b|a\b)\s*(?:wk|week)\b|^\s*weekly\b", re.I)),
    ("month", re.compile(rf"{_LEAD}(?:/|per\b|a\b)\s*(?:mo|month)\b|^\s*monthly\b", re.I)),
    (
        "year",
        re.compile(
            rf"{_LEAD}(?:/\s*(?:yr|year|yearly|annum|annually)\b|per\s+(?:year|annum|yr)\b|a\s+year\b"
            r"|yearly\b|annual(?:ly)?\b|p\.?a\b)",
            re.I,
        ),
    ),
]
_PERIOD_PRE: list[tuple[str, re.Pattern[str]]] = [
    ("hour", re.compile(r"\b(?:hourly|per\s+hour)\b[^.\n]{0,30}$", re.I)),
    ("month", re.compile(r"\b(?:monthly|per\s+month)\b[^.\n]{0,30}$", re.I)),
    ("week", re.compile(r"\b(?:weekly|per\s+week)\b[^.\n]{0,30}$", re.I)),
    (
        "year",
        re.compile(
            r"\b(?:annual(?:ly)?|yearly|per\s+(?:year|annum)|base\s+salary)\b[^.\n]{0,40}$",
            re.I,
        ),
    ),
]
_CTX = re.compile(
    r"\b(?:salary|salaries|pay|paid|compensation|wage|wages|base|range|rate|earn|earnings|"
    r"ote|remuneration|starting\s+at|up\s+to)\b",
    re.I,
)
_STRONG = re.compile(r"\b(?:salary|pay|compensation|base|range|wage)\b", re.I)
_BAD_PRE = re.compile(
    r"(?:sign-?on|signing|referral|relocation|retention|bonus|tuition|stipend|reimbursement|"
    r"raised|funding|valuation|revenue|matching|match|incentive|overtime|differential|"
    r"equity|rsus?)[^.\n]{0,25}$",
    re.I,
)
_BAD_WORD = (
    r"sign-?on|signing|bonus|funding|financing|revenue|reimbursement|tuition|stipend|relocation|"
    r"referral|raised|investment|valuation|match|allowance|budget|arr|retention|incentive|"
    r"equity|rsus?|stock|shares|"
    r"in\s+(?:sales|assets|capital|savings|grants?|contracts?)"
)
# The figure itself is a bonus/funding/equity amount: "$5,000 sign-on bonus", "$25,000/yr bonus".
# "plus bonus" or "and equity" adds to the figure, so those joining words stop the match.
_BAD_POST = re.compile(
    r"^\s*(?:usd\s*)?(?:in|of|for|as|toward|towards|to)?\s*(?:an?\s+|the\s+|our\s+)?"
    r"(?:(?!(?:plus|and|with|or|excluding|including|not)\b)[\w-]+\s+){0,2}?"
    rf"(?:{_BAD_WORD})\b",
    re.I,
)
_SCALE_POST = re.compile(r"^\s*(?:million|billion|mm|bn|m\b|b\b)", re.I)
_MIN_WAGE_POST = re.compile(
    r"^\s*(?:an?\s+hour\s+|/\s*h(?:ou)?r\s+|per\s+hour\s+)?minimum\s+wage", re.I
)
_USD_POST = re.compile(r"^\s*\(?\s*USD\b", re.I)
_MIN_ONLY = re.compile(
    r"(?:from|starting(?:\s+at)?|min(?:imum)?(?:\s+of)?|at\s+least)\s*:?\s*$", re.I
)
_MAX_ONLY = re.compile(r"(?:up\s+to|max(?:imum)?(?:\s+of)?|not\s+to\s+exceed)\s*:?\s*$", re.I)


@dataclass
class TextSalary:
    min: float | None
    max: float | None
    period: str
    raw: str
    confidence: float


def _amount(num: str, k: str | None) -> float:
    v = float(num.replace(",", ""))
    return v * 1000 if k else v


def _period_match(text: str, end: int) -> tuple[str, int] | None:
    """(period, index just past the period words) when a period directly follows ``end``."""
    post = text[end : end + 40]
    for name, pat in _PERIOD_POST:
        m = pat.search(post)
        if m:
            return name, end + m.end()
    return None


def _period_after(text: str, end: int) -> str | None:
    hit = _period_match(text, end)
    return hit[0] if hit else None


def _in_bounds(period: str, lo: float | None, hi: float | None) -> bool:
    lo_b, hi_b = BOUNDS[period]
    return all(v is None or lo_b <= v <= hi_b for v in (lo, hi))


def _candidates(text: str) -> list[tuple[int, int, TextSalary]]:
    out: list[tuple[int, int, TextSalary]] = []
    for m in _CAND.finditer(text):
        start, end = m.start(), m.end()
        before = text[max(0, start - 140) : start]
        after = text[end : end + 60]
        c1, c2 = m.group("c1"), m.group("c2")
        if not c1 and re.search(r"[$£€¥]\s*$", before):
            continue  # C$, A$ or another currency: not ours
        if c1 and start and text[start - 1].isalpha():
            continue
        n2 = m.group("n2")
        lo: float | None = _amount(m.group("n1"), m.group("k1"))
        hi: float | None = _amount(n2, m.group("k2")) if n2 else None
        if hi is not None and m.group("k2") and not m.group("k1") and lo < 1000 <= hi:  # type: ignore[operator]
            lo *= 1000  # type: ignore[operator]  # "$85-110K"
        hit = _period_match(text, end)
        period = hit[0] if hit else None
        raw_end = hit[1] if hit else end
        explicit = period is not None
        has_cur = bool(c1 or c2)
        usd_after = bool(_USD_POST.search(after))
        if not (has_cur or usd_after):
            continue
        if _SCALE_POST.search(after) or _MIN_WAGE_POST.search(after):
            continue
        pre_clean = re.sub(r"\([^)]*\)", " ", before)
        ctx_window = pre_clean[-100:]
        ctx = bool(_CTX.search(ctx_window))
        bad_pre = _BAD_PRE.search(pre_clean[-60:])
        if bad_pre and not _STRONG.search(bad_pre.group(0)):
            continue
        # "$94,900 - $135,600 Bonus eligible: No" is a salary range followed by another field,
        # unlike "$5,000 sign-on bonus": a range right after a strong pay word keeps going.
        strong_range = n2 is not None and _STRONG.search(pre_clean[-40:])
        # Look past any period words: "$25,000/yr bonus" is a bonus, not a salary.
        if not strong_range and _BAD_POST.search(text[raw_end : raw_end + 60]):
            continue
        if period is None:
            for name, pat in _PERIOD_PRE:
                if pat.search(ctx_window[-70:]):
                    period = name
                    break
        magnitude = hi if hi is not None else lo
        assert magnitude is not None
        if period is None:
            if magnitude < 15_000:
                continue  # 15..14,999 with no period is ambiguous: skip
            period = "year"
        if hi is None:
            if _MAX_ONLY.search(before):
                lo, hi = None, lo
            elif _MIN_ONLY.search(before) or re.match(r"\s*\+", after):
                hi = None
            else:
                hi = lo
        if lo is not None and hi is not None and lo > hi:
            lo, hi = hi, lo
        if not _in_bounds(period, lo, hi):
            continue
        if period == "hour" and hi == FEDERAL_MIN_WAGE:
            continue  # "Compensation $7.25 / hourly": a board placeholder, not the pay
        if lo and hi and hi / lo > 4:
            continue
        is_range = n2 is not None
        if not ctx and not explicit and not usd_after:
            continue  # nothing but a dollar sign: not enough
        if not is_range and not ctx and not (explicit and has_cur):
            continue
        score = (
            (0.35 if ctx else 0.0)
            + (0.25 if explicit else 0.0)
            + (0.15 if has_cur else 0.0)
            + (0.3 if usd_after else 0.0)
            + (0.15 if is_range else 0.0)
            # An explicit period on a currency amount ("$156,600 - $215,400 per year") is pay
            # even when the sentence before it has no pay word.
            + (0.1 if explicit and has_cur else 0.0)
        )
        out.append((start, raw_end, TextSalary(lo, hi, period, text[start:raw_end].strip(), score)))
    return _join_halves(text, out)


_HALF_SEP = re.compile(rf"^{_SEP}$")


def _join_halves(
    text: str, cands: list[tuple[int, int, TextSalary]]
) -> list[tuple[int, int, TextSalary]]:
    """Join "USD $202,000 per year - USD $224,000 per year": each side carries its own period."""
    out: list[tuple[int, int, TextSalary]] = []
    for s, e, sal in cands:
        if out:
            ps, pe, prev = out[-1]
            if (
                prev.period == sal.period
                and prev.min == prev.max
                and sal.min == sal.max
                and prev.min is not None
                and sal.min is not None
                and prev.min < sal.min
                and _HALF_SEP.match(text[pe:s])
            ):
                joined = TextSalary(
                    prev.min, sal.max, sal.period, text[ps:e].strip(),
                    max(prev.confidence, sal.confidence),
                )  # fmt: skip
                out[-1] = (ps, e, joined)
                continue
        out.append((s, e, sal))
    return out


def _minmax(text: str) -> list[tuple[int, int, TextSalary]]:
    out = []
    for m in _MINMAX.finditer(text):
        lo = _amount(m.group("a"), m.group("ka"))
        hi = _amount(m.group("b"), m.group("kb"))
        period = _period_after(text, m.end("a")) or _period_after(text, m.end("b"))
        if not (m.group("ca") or m.group("cb")):
            continue  # "a minimum 20,000 to a maximum 50,000 users": not money
        if period is None:
            ctx = text[max(0, m.start() - 100) : m.end()]
            if not _CTX.search(ctx):
                continue
            period = "year" if hi >= 15_000 else None
        if period is None or lo > hi or not _in_bounds(period, lo, hi):
            continue
        out.append((m.start(), m.end(), TextSalary(lo, hi, period, m.group(0).strip(), 0.9)))
    return out


_STATE = re.compile(r"[A-Z]{2}")
# A tier label ends at a sentence, a line, a ";" or a closing parenthesis.
_LABEL_CUT = re.compile(r"\.\s|\n|;|\)")


def _location_patterns(location: str | None) -> tuple[list[re.Pattern[str]], list[re.Pattern[str]]]:
    """(place-name patterns, two-letter state patterns) for a job location."""
    if not location:
        return [], []
    places, states = [], []
    for tok in re.split(r"[,;/()|]+", location):
        tok = tok.strip()
        if len(tok) < 2 or tok.lower() in {"us", "usa", "united states", "remote"}:
            continue
        if _STATE.fullmatch(tok):  # "OH" must be capitalized: not "oh"
            states.append(re.compile(rf"(?<![A-Za-z]){tok}(?![A-Za-z])"))
        else:
            places.append(re.compile(rf"(?<![A-Za-z]){re.escape(tok)}(?![A-Za-z])", re.I))
    return places, states


def _by_location(
    text: str, found: list[tuple[int, int, TextSalary]], location: str | None
) -> TextSalary | None:
    """The range whose tier label names the job's location, if any.

    Labels usually come before their range ("Austin: $150,000 - $190,000"), so the text between
    the previous range and this one is checked first, cut at the last sentence, line, ";" or ")"
    so a label belonging to the previous range does not count. A label after the range
    ("$150,000 - $200,000 (For Beavercreek, OH Only)") is tried only when no range has one
    before it. A city or county name beats a state code: "Santa Monica, CA" takes the Santa
    Monica tier, not the first California one.
    """
    places, states = _location_patterns(location)
    leads, tails = [], []
    for i, (s, e, _) in enumerate(found):
        lo_edge = found[i - 1][1] if i else 0
        hi_edge = found[i + 1][0] if i + 1 < len(found) else len(text)
        leads.append(_LABEL_CUT.split(text[max(lo_edge, s - 100) : s])[-1])
        tails.append(_LABEL_CUT.split(text[e : min(hi_edge, e + 40)])[0])
    for pats in (places, states):
        for windows in (leads, tails):
            for (_, _, sal), window in zip(found, windows, strict=True):
                if any(p.search(window) for p in pats):
                    return sal
    return None


def _group_strength(group: list[tuple[int, int, TextSalary]]) -> tuple[bool, float, int, int]:
    """Sort key for a period group: annual first, then confidence, more ranges, earliest.

    Confidence alone is not comparable across periods: "$15 per hour" earns the explicit-period
    bonus while "Salary range: $120,000 - $150,000" has no period word to earn it, yet the
    hourly figure is an on-call or intern side rate. A confident annual candidate already needs
    an annual period word, labelled min/max, a USD tag, or a range after a pay word (a lone
    "Salary: $130,000" scores below CONFIDENT), so it is the stated salary and wins over any
    other period.
    """
    annual = group[0][2].period == "year"
    best = max(f[2].confidence for f in group)
    ranges = sum(1 for f in group if f[2].min != f[2].max)
    return annual, best, ranges, -group[0][0]


def extract_salary_from_text(text: str | None, location: str | None = None) -> TextSalary | None:
    """Best pay range found in a description, or None when nothing is confidently pay.

    Candidates are grouped by period. A confident annual group always wins; otherwise the
    strongest group wins (highest confidence, then more ranges, then earliest). So an hourly
    on-call, overtime or intern rate, or a placeholder, before or after it, cannot displace a
    stated annual range. Within that group, several ranges (location tiers): the one
    labelled with the job's location wins; otherwise the widest min..max across the group.
    """
    if not text:
        return None
    text = re.sub(r"\bUS\$", "$", text.replace("\xa0", " ").replace("\\$", "$"))
    found = _minmax(text)
    spans = [(s, e) for s, e, _ in found]
    for s, e, sal in _candidates(text):
        if not any(a <= s < b for a, b in spans):
            found.append((s, e, sal))
    found = [f for f in found if f[2].confidence >= CONFIDENT]
    if not found:
        return None
    found.sort(key=lambda f: f[0])
    groups: dict[str, list[tuple[int, int, TextSalary]]] = {}
    for f in found:
        groups.setdefault(f[2].period, []).append(f)
    found = max(groups.values(), key=_group_strength)
    if len(found) > 1:
        hit = _by_location(text, found, location)
        if hit is not None:
            return hit
    same = [f[2] for f in found]
    first = same[0]
    if len(same) == 1:
        return first
    los = [s.min for s in same if s.min is not None]
    his = [s.max for s in same if s.max is not None]
    lo, hi = (min(los) if los else None), (max(his) if his else None)
    if lo and hi and hi / lo > 4:
        return first
    raws = list(dict.fromkeys(s.raw for s in same))
    return TextSalary(lo, hi, first.period, " | ".join(raws), max(s.confidence for s in same))
