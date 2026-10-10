"""The no-fabrication checker (specs/017 "No-fabrication rule").

Runs on every version, generated or edited, and writes its ``check_report``. It catches
**mechanical** invention: a missing or non-existent citation, a changed employer, title or
date, a number, quantity, skill, technology, name or credential not in the cited lines (or not
in the resume), a stronger claim class than any cited line uses ("contributed to" -> "leads"),
a sentence about the employer without a meaningful verified posting quote, and a posting quote
that is not in the posting. It cannot judge whether a rephrasing is fair; every line shows its
cited lines so the user can.

``check_report`` (JSON)::

    {"ok": bool,
     "items": [{"key", "ckey", "role", "label", "text", "sources", "status", "reasons",
                "entail"}],
     "confirmed": ["<confirm key>", ...],     # "This is true, keep it", by item text
     "entailment": {"status": "done|skipped|failed|off", "detail": "...", ...}}

``status`` is ``pass``, ``unsupported`` or ``confirmed``. **Mark ready** needs ``ok``: every
item passing or confirmed. The entailment verdict (``entail``) is advisory and never changes a
status.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from jobhunter.apply import claims
from jobhunter.apply.documents import SOURCE_RE, Item, items_of, norm_text
from jobhunter.core.rejections import employer_tokens
from jobhunter.scoring.screen import normalize_for_match, quote_found

ALLOWED_PREFIXES = {
    "resume": ("L",),
    "cover_letter": ("L", "N"),
    "question_draft": ("L", "N", "S"),
}
# An entry's employer, title and dates may sit on the cited line or the next 2 lines, but never
# on a line another entry cites (that is the next job).
STRUCTURE_WINDOW = 2
RESUME_ROLES = {"summary", "bullet", "heading"}
# Section headings that make no claim.
PLAIN_HEADINGS = {
    "experience",
    "work experience",
    "professional experience",
    "relevant experience",
    "other experience",
    "additional experience",
    "employment",
    "employment history",
    "work history",
    "education",
    "skills",
    "technical skills",
    "projects",
    "selected projects",
    "summary",
    "profile",
    "additional",
    "publications",
    "volunteer",
    "volunteering",
    "awards",
    "certifications",
    "training",
}
# A posting quote that backs a sentence about the employer must say something: at least this
# many words, at least two of them not stopwords or the employer's own name.
MIN_QUOTE_WORDS = 4
MIN_QUOTE_CONTENT = 2
STOPWORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "has",
        "have",
        "i",
        "in",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "our",
        "that",
        "the",
        "their",
        "this",
        "to",
        "we",
        "will",
        "with",
        "you",
        "your",
        "yours",
        "us",
    ]
)
ABOUT_EMPLOYER = re.compile(
    r"\b(you|your|yours|the company|this company|the organization|the organisation|"
    r"the agency|the team|this team|their team|their mission|their work)\b",
    re.IGNORECASE,
)
NAME_STOP = frozenset(
    [
        "i",
        "i'm",
        "i've",
        "i'd",
        "i'll",
        "ok",
        "am",
        "pm",
        "january",
        "february",
        "march",
        "april",
        "may",
        "june",
        "july",
        "august",
        "september",
        "october",
        "november",
        "december",
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
        "saturday",
        "sunday",
    ]
)

_NUM = re.compile(
    r"(?<![\w.])(\$)?(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*"
    r"(%|percent\b|\+|k\b|m\b|million\b|billion\b|x\b)?(?:\s*([A-Za-z][A-Za-z\-]*))?",
    re.IGNORECASE,
)
_PHONE = re.compile(r"(?<!\d)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?!\d)")
_SENT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])")
# A name token keeps inner dots ("Node.js", "ASP.NET", "U.S") and a leading one (".NET").
_TOKEN = re.compile(
    r"\.?[A-Za-z0-9](?:[A-Za-z0-9+#&'\-]|\.(?=[A-Za-z0-9]))*[A-Za-z0-9+#]|\.?[A-Za-z0-9]"
)
_ABBREV = re.compile(r"^(e\.g|i\.e|etc|u\.s|u\.k|vs|a\.m|p\.m|inc|ltd|co|no|dr|mr|ms|mrs)$", re.I)
# Vendor words allowed before a name the lines do contain ("Amazon EKS", "Apache Kafka").
_VENDORS = {"amazon", "aws", "microsoft", "azure", "google", "apache", "hashicorp", "red", "oracle"}


def _norm(text: str) -> str:
    return normalize_for_match(text or "")


def _has_phrase(haystack_norm: str, phrase: str) -> bool:
    words = [re.escape(w) for w in phrase.split()]
    if not words:
        return False
    pat = r"(?<![a-z0-9])" + r"[\s\-]+".join(words) + r"(?![a-z0-9+#])"
    return re.search(pat, haystack_norm) is not None


def _words_in(norm: str, words: Iterable[str]) -> list[str]:
    return [w for w in words if _has_phrase(norm, w)]


# ─── numbers ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Num:
    value: float
    kind: str  # "%", "+", "$" or ""
    units: frozenset[str]  # the next three content words, singular, lowercased
    shown: str

    @property
    def is_year(self) -> bool:
        return self.kind == "" and self.value.is_integer() and 1900 <= self.value <= 2100


_UNIT_SKIP = {
    "of",
    "in",
    "to",
    "and",
    "or",
    "the",
    "a",
    "an",
    "per",
    "across",
    "for",
    "on",
    "with",
    "at",
    "from",
    "by",
    "over",
    "than",
    "more",
    "about",
}


def _unit_words(after: str) -> frozenset[str]:
    out = []
    for w in re.findall(r"[A-Za-z][A-Za-z\-]*", after)[:3]:
        w = w.casefold()
        if w in _UNIT_SKIP:
            continue
        out.append(w[:-1] if len(w) > 3 and w.endswith("s") else w)
    return frozenset(out)


def _numbers(text: str) -> list[Num]:
    t = _PHONE.sub(" ", text or "")
    t = re.sub(r"\b24\s*/\s*7\b|\b24x7\b", " around the clock ", t)
    for word, digit in claims.NUMBER_WORDS.items():
        t = re.sub(rf"\b{word}\b", digit, t, flags=re.IGNORECASE)
    out: list[Num] = []
    for m in _NUM.finditer(t):
        dollar, raw, suffix, unit = m.group(1), m.group(2), (m.group(3) or "").lower(), m.group(4)
        try:
            value = float(raw.replace(",", ""))
        except ValueError:
            continue
        scale = {"k": 1e3, "m": 1e6, "million": 1e6, "billion": 1e9}.get(suffix, 1.0)
        kind = "%" if suffix in ("%", "percent") else "+" if suffix == "+" else ""
        kind = kind or ("$" if dollar else "")
        start = m.start(4) if unit else m.end()
        units = _unit_words(t[start : start + 80])
        shown = (m.group(0) if not unit else m.group(0)[: -len(unit)]).strip()
        out.append(Num(value * scale, kind, units, shown))
    return out


def _number_reasons(text: str, cited: str) -> list[str]:
    have = _numbers(cited)
    reasons: list[str] = []
    for n in _numbers(text):
        if n.kind == "+":
            # "200+ Linux servers" restates "200 RHEL servers" (or more): a shared unit word
            # within three words. Never a year, a phone number or a number about something else.
            ok = any(
                not c.is_year
                and c.kind in ("", "+")
                and c.value >= n.value
                and (bool(c.units & n.units) if n.units else c.value == n.value and c.kind == "+")
                for c in have
            )
        elif n.kind == "%":
            ok = any(c.value == n.value and c.kind == "%" for c in have)
        elif n.kind == "$":
            ok = any(c.value == n.value and c.kind == "$" for c in have)
        else:
            ok = any(c.value == n.value and c.kind in ("", "+") for c in have)
        if not ok:
            reasons.append(f"the number {n.shown} is not in the cited lines")
    tn, cn = _norm(text), _norm(cited)
    for word in claims.VAGUE_QUANTITIES:
        if _has_phrase(tn, word) and not _has_phrase(cn, word):
            reasons.append(f"'{word}' is a quantity no cited line states")
    return reasons


# ─── skills, technologies and names ─────────────────────────────────────────


def _canonical(term: str) -> str:
    t = term.casefold().strip()
    return claims.SYNONYMS.get(t, t)


def _aliases(canonical: str) -> list[str]:
    return [canonical, *(a for a, c in claims.SYNONYMS.items() if c == canonical)]


# Ordinary words that are also technology names count only in their written-name case, not
# inside a hyphenated word ("go-live"), and in prose not as a sentence's first word.
_NAME_CASE = {
    "go": "Go",
    "ts": "TS",
    "tf": "TF",
    "js": "JS",
    "node": "Node",
    "git": "Git",
    "sap": "SAP",
    "swift": "Swift",
    "spark": "Spark",
    "chef": "Chef",
    "excel": "Excel",
    "spring": "Spring",
    "vue": "Vue",
}


def _name_hits(name: str, text: str) -> list[int]:
    rx = rf"(?<![A-Za-z0-9\-]){re.escape(name)}(?![A-Za-z0-9\-])"
    return [m.start() for m in re.finditer(rx, text)]


def _in_resume(term: str, resume_norm: str, resume_raw: str) -> bool:
    canon = _canonical(term)
    for alias in _aliases(canon):
        if alias in _NAME_CASE:
            if _name_hits(_NAME_CASE[alias], resume_raw):
                return True
        elif _has_phrase(resume_norm, _norm(alias)):
            return True
    return False


def _skill_reasons(name: str, resume_norm: str, resume_raw: str) -> list[str]:
    n = norm_text(name)
    if not n:
        return ["the skill is empty"]
    if _in_resume(n, resume_norm, resume_raw):
        return []
    # "Terraform and Ansible", "Python / Go": each named part must be in the resume.
    parts = [p for p in re.split(r"\s*(?:,|/|&|\band\b|\(|\))\s*", n) if p.strip()]
    missing = [p for p in parts if not _in_resume(p, resume_norm, resume_raw)]
    if len(parts) > 1 and not missing:
        return []
    return [f"'{p}' is not anywhere in your resume" for p in (missing if len(parts) > 1 else [n])]


def _sentence_start(text: str, pos: int) -> bool:
    before = text[:pos].rstrip(" \t\"'(")
    return not before or before.endswith((".", "!", "?", ":", ";", "\n", "-", "•"))


def _named_in_prose(term: str, text: str) -> bool:
    name = _NAME_CASE.get(term)
    if name is None:
        return True
    return any(not _sentence_start(text, i) for i in _name_hits(name, text))


def _tech_reasons(text: str, resume_norm: str, resume_raw: str) -> tuple[list[str], set[str]]:
    """Reasons, and every alias of every technology the text names (passing or not), so the
    name check never re-flags "Postgres" or "K8s" that this check already judged."""
    norm = _norm(text)
    reasons: list[str] = []
    known: set[str] = set()
    seen: set[str] = set()
    for term in (*claims.TECH, *claims.SYNONYMS):
        if not _has_phrase(norm, _norm(term)) or not _named_in_prose(term, text):
            continue
        canon = _canonical(term)
        known.update(_norm(a) for a in _aliases(canon))
        if canon in seen:
            continue
        seen.add(canon)
        if not _in_resume(canon, resume_norm, resume_raw):
            reasons.append(f"'{term}' is not anywhere in your resume")
    return reasons, known


def _name_like(tok: str, text: str, pos: int) -> bool:
    core = tok.lstrip(".")
    letters = [c for c in core if c.isalpha()]
    if not letters or (len(core) < 2 and not tok.startswith(".")):
        return False
    if any(c.isdigit() for c in core):
        return True  # S3, EC2, Python3
    if tok.startswith(".") or (len(letters) >= 2 and core.isupper()):
        return True  # .NET, AWS
    if any(c.isupper() for c in core[1:]) and any(c.islower() for c in core):
        return True  # PyTorch, iOS
    return core[0].isupper() and not _sentence_start(text, pos)


def _name_reasons(
    text: str,
    *,
    context_norm: str,
    posting_norm: str,
    allowed_norm: str,
    already: set[str],
    title_case: bool = False,
) -> list[str]:
    """Names, products and organisations in prose ("Airflow", "PyTorch", "Google") that are
    in none of the lines this document may cite. A name copied from the posting is the most
    likely tailoring invention; one from nowhere is worse."""
    reasons: list[str] = []
    seen: set[str] = set()
    tokens = list(_TOKEN.finditer(text))
    for idx, m in enumerate(tokens):
        for part in re.split(r"(?<=[A-Za-z0-9])-(?=[A-Za-z0-9])", m.group(0)):
            pos = m.start() + m.group(0).find(part)
            part = part.rstrip("'")
            if part.endswith("'s"):
                part = part[:-2]
            key = _norm(part)
            if not key or key in seen or key in NAME_STOP or _ABBREV.match(part.strip(".")):
                continue
            if not _name_like(part, text, pos):
                continue
            plain = part[:1].isupper() and part[1:].islower() and part.isalpha()
            if title_case and plain:
                continue  # a heading capitalises ordinary words
            seen.add(key)
            if key in already or _has_phrase(context_norm, key) or _has_phrase(allowed_norm, key):
                continue
            if key in _VENDORS and idx + 1 < len(tokens):
                nxt = _norm(tokens[idx + 1].group(0))
                if nxt in already or _has_phrase(context_norm, nxt):
                    continue
            if _has_phrase(posting_norm, key):
                reasons.append(f"'{part}' comes from the posting, not from your lines")
            else:
                reasons.append(f"'{part}' is not in your resume or notes")
    return reasons


# ─── claims ─────────────────────────────────────────────────────────────────


def _claim_reasons(text: str, cited: str, *, seniority: bool) -> list[str]:
    tn, cn = _norm(text), _norm(cited)
    reasons: list[str] = []
    if seniority and (m := claims.SENIORITY_RE.search(tn)) and not claims.SENIORITY_RE.search(cn):
        reasons.append(f"'{m.group(0)}' claims more (seniority) than any cited line says")
    for cls, words in claims.CLAIM_CLASSES.items():
        used = _words_in(tn, words)
        if used and not _words_in(cn, words):
            reasons.append(f"'{used[0]}' claims more ({cls}) than any cited line says")
    if (m := claims.TEAM_SIZE.search(tn)) and not claims.TEAM_SIZE.search(cn):
        reasons.append(f"'{m.group(0)}' adds a team size no cited line states")
    for cred in claims.CREDENTIALS:
        if _has_phrase(tn, cred) and not _has_phrase(cn, cred):
            reasons.append(f"'{cred}' is a credential no cited line states")
            break
    return reasons


def _employer_words(employer: str) -> list[str]:
    """Words that identify the employer in a sentence. Short names count (3M, GE, HP), but a
    stopword never does ("AT&T" is matched as a whole name, not as "at")."""
    toks = [
        t
        for t in employer_tokens(employer or "")
        if t not in claims.EMPLOYER_STOP and t not in STOPWORDS
    ]
    long = [t for t in toks if len(t) > 2]
    words = long or [t for t in toks if len(t) >= 2]
    whole = _norm(employer).strip()
    if re.search(r"[&.]", whole) and len(whole) <= 20:
        words.append(whole)  # "at&t", "j.p. morgan"
    return words


def _meaningful_quote(quote: str, employer: str) -> bool:
    words = re.findall(r"[a-z0-9][a-z0-9+#'\-]*", _norm(quote))
    names = set(_employer_words(employer)) | set(employer_tokens(employer or ""))
    content = [w for w in words if w not in STOPWORDS and w not in names]
    return len(words) >= MIN_QUOTE_WORDS and len(content) >= MIN_QUOTE_CONTENT


def _employer_claim_reasons(
    text: str, quotes: list[str], posting_norm: str, employer: str
) -> list[str]:
    good = [
        _norm(q).strip("\"' ")
        for q in quotes
        if quote_found(q, posting_norm) and _meaningful_quote(q, employer)
    ]
    names = _employer_words(employer)
    reasons: list[str] = []
    for sentence in _SENT.split(norm_text(text)):
        sn = _norm(sentence)
        about = bool(ABOUT_EMPLOYER.search(sentence)) or any(_has_phrase(sn, n) for n in names)
        if not about:
            continue
        if not any(q in sn for q in good):
            short = sentence if len(sentence) <= 80 else sentence[:77] + "..."
            reasons.append(
                f'"{short}" is about the employer but contains no verified posting quote of '
                f"{MIN_QUOTE_WORDS}+ words"
            )
    return reasons


# ─── the check ──────────────────────────────────────────────────────────────


def _cited_text(item: Item, lines: Mapping[str, str]) -> str:
    return "\n".join(lines[s] for s in item.sources if s in lines)


def _citation_reasons(item: Item, kind: str, lines: Mapping[str, str]) -> list[str]:
    allowed = ALLOWED_PREFIXES[kind]
    if not item.sources:
        return ["cites no source line"]
    reasons = []
    for s in item.sources:
        if not SOURCE_RE.match(s) or s not in lines:
            reasons.append(f"cites {s}, which does not exist")
        elif not s.startswith(allowed):
            what = kind.replace("_", " ")
            reasons.append(f"cites {s}; a {what} may cite only {'/'.join(allowed)}")
    return reasons


def _structure_reasons(item: Item, lines: Mapping[str, str], entry_lines: set[str]) -> list[str]:
    src = item.sources[0] if item.sources else ""
    if src not in lines:
        return []  # reported by the citation check
    n = int(src[1:])
    window = [lines.get(src, "")]
    for i in range(n + 1, n + STRUCTURE_WINDOW + 1):
        if f"L{i}" in entry_lines:
            break  # the next job starts here
        window.append(lines.get(f"L{i}", ""))
    wn = _norm(" ".join(window))
    reasons = []
    for name in ("employer", "title", "dates"):
        value = _norm(item.fields.get(name, ""))
        if value and not _has_phrase(wn, value):
            reasons.append(f"{name} '{item.fields[name]}' differs from {src}")
    return reasons


@dataclass
class _Ctx:
    kind: str
    lines: Mapping[str, str]
    posting_norm: str
    employer: str
    resume_norm: str
    resume_raw: str
    context_norm: str
    allowed_norm: str  # employer name and job title: names prose may use freely
    entry_lines: set[str]


def check_item(item: Item, c: _Ctx) -> list[str]:
    if item.role == "heading":
        return _heading_reasons(item.text, c)
    reasons = _citation_reasons(item, c.kind, c.lines)
    cited = _cited_text(item, c.lines)
    if item.role == "header":
        return reasons
    if item.role == "entry":
        return reasons + _structure_reasons(item, c.lines, c.entry_lines)
    if item.role == "skill":
        return reasons + _skill_reasons(item.text, c.resume_norm, c.resume_raw)
    reasons += _prose_reasons(item.text, cited, c, item.role, item.quotes)
    if item.role in ("paragraph", "sentence"):
        for q in item.quotes:
            if not quote_found(q, c.posting_norm):
                short = q if len(q) <= 60 else q[:57] + "..."
                reasons.append(f'the quote "{short}" is not in the posting')
        reasons += _employer_claim_reasons(item.text, item.quotes, c.posting_norm, c.employer)
    return list(dict.fromkeys(reasons))


def _prose_reasons(text: str, cited: str, c: _Ctx, role: str, quotes: list[str]) -> list[str]:
    reasons = _number_reasons(text, cited)
    tech, flagged = _tech_reasons(text, c.resume_norm, c.resume_raw)
    reasons += tech
    verified = " ".join(q for q in quotes if quote_found(q, c.posting_norm))
    reasons += _name_reasons(
        text,
        context_norm=c.context_norm,
        posting_norm=c.posting_norm,
        allowed_norm=f"{c.allowed_norm} {_norm(verified)}",
        already=flagged,
        title_case=role == "heading",
    )
    reasons += _claim_reasons(text, cited, seniority=role in RESUME_ROLES)
    return reasons


def _heading_reasons(heading: str, c: _Ctx) -> list[str]:
    """A section heading cites nothing, so it is checked against the whole resume and may
    make no claim the resume does not."""
    h = norm_text(heading)
    if not h or _norm(h) in PLAIN_HEADINGS:
        return []
    return list(dict.fromkeys(_prose_reasons(h, c.resume_raw, c, "heading", [])))


def check(
    doc: Mapping[str, Any],
    *,
    posting: str,
    employer: str,
    title: str = "",
    confirmed: Iterable[str] = (),
    entail: Mapping[str, str] | None = None,
    entailment: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The ``check_report`` for ``doc``. ``confirmed`` are confirm keys carried or newly set;
    only those matching an item that fails are kept (a passing item needs none)."""
    kind = str(doc["kind"])
    lines: Mapping[str, str] = (doc.get("context") or {}).get("lines") or {}
    if doc.get("base"):
        return {"ok": True, "base": True, "items": [], "confirmed": [], "entailment": None}
    resume_raw = "\n".join(v for k, v in lines.items() if k.startswith("L"))
    items = items_of(doc)
    c = _Ctx(
        kind=kind,
        lines=lines,
        posting_norm=_norm(posting),
        employer=employer,
        resume_norm=_norm(resume_raw),
        resume_raw=resume_raw,
        context_norm=_norm("\n".join(lines.values())),
        allowed_norm=_norm(f"{employer} {title}"),
        entry_lines={i.sources[0] for i in items if i.role == "entry" and i.sources},
    )
    wanted = set(confirmed)
    out_items: list[dict[str, Any]] = []
    kept: list[str] = []
    for item in items:
        reasons = check_item(item, c)
        status = "pass"
        if reasons:
            status = "confirmed" if item.ckey in wanted else "unsupported"
            if status == "confirmed" and item.ckey not in kept:
                kept.append(item.ckey)
        out_items.append(
            {
                "key": item.key,
                "ckey": item.ckey,
                "role": item.role,
                "label": item.label,
                "text": item.text,
                "sources": item.sources,
                "status": status,
                "reasons": reasons,
                "entail": (entail or {}).get(item.key),
            }
        )
    ok = bool(out_items) and all(i["status"] != "unsupported" for i in out_items)
    return {
        "ok": ok,
        "items": out_items,
        "confirmed": kept,
        "entailment": dict(entailment) if entailment else None,
    }


def unsupported(report: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [i for i in report.get("items") or [] if i.get("status") == "unsupported"]
