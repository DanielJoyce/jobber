"""The no-fabrication checker (specs/017 "No-fabrication rule").

Runs on every version, generated or edited, and writes its ``check_report``. It catches
**mechanical** invention: a missing or non-existent citation, a changed employer, title or
date, a number, skill, technology or credential not in the cited lines (or not in the
resume), a stronger claim class than any cited line uses ("contributed to" -> "led"), a
sentence about the employer without a verified posting quote, and a posting quote that is not
in the posting. It cannot judge whether a rephrasing is fair; every line shows its cited lines
so the user can.

``check_report`` (JSON)::

    {"ok": bool,
     "items": [{"key", "role", "label", "text", "sources", "status", "reasons", "entail"}],
     "confirmed": ["<confirm key>", ...],     # "This is true, keep it", by item text
     "entailment": {"status": "done|skipped|failed|off", "detail": "...", ...}}

``status`` is ``pass``, ``unsupported`` or ``confirmed``. **Mark ready** needs ``ok``: every
item passing or confirmed. The entailment verdict (``entail``) is advisory and never changes a
status.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
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
STRUCTURE_WINDOW = 2  # an entry's employer, title and dates may sit on the cited line or the next 2

_WORD = re.compile(r"[a-z0-9][a-z0-9+#.\-/']*")
_NUM = re.compile(
    r"(?<![\w.])(\$)?(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*(%|percent\b|\+|k\b|m\b|million\b|"
    r"billion\b|x\b)?",
    re.IGNORECASE,
)
_SENT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])")
_YOU = re.compile(r"\b(you|your|yours)\b", re.IGNORECASE)


def _norm(text: str) -> str:
    return normalize_for_match(text or "")


def _has_phrase(haystack_norm: str, phrase: str) -> bool:
    words = [re.escape(w) for w in phrase.split()]
    pat = r"(?<![a-z0-9])" + r"[\s\-]+".join(words) + r"(?![a-z0-9])"
    return re.search(pat, haystack_norm) is not None


def _words_in(norm: str, words: Iterable[str]) -> list[str]:
    return [w for w in words if _has_phrase(norm, w)]


# ─── numbers ────────────────────────────────────────────────────────────────


def _numbers(text: str) -> list[tuple[float, str, str]]:
    """(value, suffix) for each number in ``text``; spelled-out small numbers included.
    Suffix is ``%``, ``+``, ``$`` or ``""``; k/m/million scale the value."""
    t = text or ""
    for word, digit in claims.NUMBER_WORDS.items():
        t = re.sub(rf"\b{word}\b", digit, t, flags=re.IGNORECASE)
    out: list[tuple[float, str, str]] = []
    for m in _NUM.finditer(t):
        dollar, raw, suffix = m.group(1), m.group(2), (m.group(3) or "").lower()
        try:
            value = float(raw.replace(",", ""))
        except ValueError:
            continue
        scale = {"k": 1e3, "m": 1e6, "million": 1e6, "billion": 1e9}.get(suffix, 1.0)
        kind = "%" if suffix in ("%", "percent") else "+" if suffix == "+" else ""
        kind = kind or ("$" if dollar else "")
        out.append((value * scale, kind, m.group(0).strip()))
    return out


def _number_reasons(text: str, cited: str) -> list[str]:
    have = _numbers(cited)
    values = {v for v, _, _ in have}
    reasons: list[str] = []
    for value, kind, shown in _numbers(text):
        if kind == "+":
            # "5+ years" restates any cited number of at least 5.
            ok = any(v >= value for v in values)
        elif kind == "%":
            ok = any(v == value and k == "%" for v, k, _ in have)
        else:
            ok = value in values
        if not ok:
            reasons.append(f"the number {shown} is not in the cited lines")
    return reasons


# ─── skills and technologies ────────────────────────────────────────────────


def _canonical(term: str) -> str:
    t = term.casefold().strip()
    return claims.SYNONYMS.get(t, t)


def _aliases(canonical: str) -> list[str]:
    return [canonical, *(a for a, c in claims.SYNONYMS.items() if c == canonical)]


def _in_resume(term: str, resume_norm: str) -> bool:
    return any(_has_phrase(resume_norm, _norm(a)) for a in _aliases(_canonical(term)))


def _skill_reasons(name: str, resume_norm: str) -> list[str]:
    n = norm_text(name)
    if not n:
        return ["the skill is empty"]
    if _in_resume(n, resume_norm):
        return []
    # "Terraform and Ansible", "Python / Go": each named part must be in the resume.
    parts = [p for p in re.split(r"\s*(?:,|/|&|\band\b|\(|\))\s*", n) if p.strip()]
    missing = [p for p in parts if not _in_resume(p, resume_norm)]
    if len(parts) > 1 and not missing:
        return []
    return [f"'{p}' is not anywhere in your resume" for p in (missing if len(parts) > 1 else [n])]


# Ordinary words that are also technology names count in prose only in their written-name case
# and not as a sentence's first word ("Go", "TS"; never "go" or "Go further").
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
}


def _named_in_prose(term: str, text: str) -> bool:
    name = _NAME_CASE.get(term)
    if name is None:
        return True
    for m in re.finditer(rf"(?<![A-Za-z0-9]){re.escape(name)}(?![A-Za-z0-9])", text):
        before = text[: m.start()].rstrip()
        if before and not before.endswith((".", "!", "?", ":")):
            return True
    return False


def _tech_reasons(text: str, resume_norm: str) -> list[str]:
    norm = _norm(text)
    reasons: list[str] = []
    seen: set[str] = set()
    for term in (*claims.TECH, *claims.SYNONYMS):
        if not _has_phrase(norm, _norm(term)) or not _named_in_prose(term, text):
            continue
        canon = _canonical(term)
        if canon in seen:
            continue
        seen.add(canon)
        if not _in_resume(canon, resume_norm):
            reasons.append(f"'{term}' is not anywhere in your resume")
    return reasons


# ─── claims ─────────────────────────────────────────────────────────────────


def _claim_reasons(text: str, cited: str) -> list[str]:
    tn, cn = _norm(text), _norm(cited)
    reasons: list[str] = []
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
    return [
        t for t in employer_tokens(employer or "") if t not in claims.EMPLOYER_STOP and len(t) > 2
    ]


def _employer_claim_reasons(
    text: str, quotes: list[str], posting_norm: str, employer: str
) -> list[str]:
    good = [q for q in quotes if quote_found(q, posting_norm)]
    names = _employer_words(employer)
    reasons: list[str] = []
    for sentence in _SENT.split(norm_text(text)):
        sn = _norm(sentence)
        about = bool(_YOU.search(sentence)) or any(_has_phrase(sn, n) for n in names)
        if not about:
            continue
        if not any(_norm(q).strip("\"' ") in sn for q in good):
            short = sentence if len(sentence) <= 80 else sentence[:77] + "..."
            reasons.append(
                f'"{short}" is about the employer but quotes nothing verified from the posting'
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
            reasons.append(
                f"cites {s}; a {kind.replace('_', ' ')} may cite only {'/'.join(allowed)}"
            )
    return reasons


def _structure_reasons(item: Item, lines: Mapping[str, str]) -> list[str]:
    src = item.sources[0] if item.sources else ""
    if src not in lines:
        return []  # reported by the citation check
    n = int(src[1:])
    window = " ".join(lines.get(f"L{i}", "") for i in range(n, n + STRUCTURE_WINDOW + 1))
    wn = _norm(window)
    reasons = []
    for name in ("employer", "title", "dates"):
        value = _norm(item.fields.get(name, ""))
        if value and value not in wn:
            reasons.append(f"{name} '{item.fields[name]}' differs from {src}")
    return reasons


def check_item(
    item: Item,
    kind: str,
    *,
    lines: Mapping[str, str],
    posting: str,
    employer: str,
    resume_norm: str,
) -> list[str]:
    reasons = _citation_reasons(item, kind, lines)
    cited = _cited_text(item, lines)
    if item.role == "header":
        return reasons
    if item.role == "entry":
        return reasons + _structure_reasons(item, lines)
    if item.role == "skill":
        return reasons + _skill_reasons(item.text, resume_norm)
    reasons += _number_reasons(item.text, cited)
    reasons += _tech_reasons(item.text, resume_norm)
    reasons += _claim_reasons(item.text, cited)
    if item.role in ("paragraph", "sentence"):
        posting_norm = _norm(posting)
        for q in item.quotes:
            if not quote_found(q, posting_norm):
                short = q if len(q) <= 60 else q[:57] + "..."
                reasons.append(f'the quote "{short}" is not in the posting')
        reasons += _employer_claim_reasons(item.text, item.quotes, posting_norm, employer)
    return list(dict.fromkeys(reasons))


def check(
    doc: Mapping[str, Any],
    *,
    posting: str,
    employer: str,
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
    resume_norm = _norm("\n".join(v for k, v in lines.items() if k.startswith("L")))
    wanted = set(confirmed)
    out_items: list[dict[str, Any]] = []
    kept: list[str] = []
    for item in items_of(doc):
        reasons = check_item(
            item, kind, lines=lines, posting=posting, employer=employer, resume_norm=resume_norm
        )
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
