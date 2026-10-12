"""Propose contact values from a resume's header (specs/022 "The answer bank").

A deliberately simple parser: it reads only the first lines, finds an email and a phone by
pattern, takes the name from the first line that looks like one and a ``City, ST`` style
location. It proposes; the /prefs page shows the values for you to check and nothing is saved
until you save. It never raises and returns only what it is fairly sure of. A street address or
postal code is never proposed (the never-store list).
"""

from __future__ import annotations

import re

HEADER_LINES = 8

_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+")
_NANP = re.compile(
    r"(?<![\w.\-])(?:\+?1[\s.\-]?)?(?:\([2-9]\d{2}\)\s?|[2-9]\d{2}[\s.\-]?)\d{3}[\s.\-]?\d{4}(?![\w\-])"
)
_INTL = re.compile(r"(?<![\w.\-])\+\d{1,3}(?:[\s.\-]?(?:\(\d{1,5}\)|\d{1,5})){2,5}(?![\w\-])")
# A postal code, with or without its state, is never part of a phone number.
_POSTAL = re.compile(r"\b(?:[A-Z]{2}\s+\d{5}(?:-\d{4})?|\d{5}-\d{4})\b")
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_SEPARATORS = re.compile(r"\s*(?:\||\u00b7|\u2022|\u2014|\u2013|\s/\s|;|\t|\s-\s)\s*|\s{3,}")
_CREDENTIAL = re.compile(r"^[A-Z][A-Za-z.]{1,8}$")
_SUFFIX = {"jr", "jr.", "sr", "sr.", "ii", "iii", "iv"}
_STATE = re.compile(r"^[A-Z]{2}$")
_ZIP = re.compile(r"\s+\d{5}(?:-\d{4})?$")
_PLACE = re.compile(r"^[A-Z][A-Za-z.'\u2019\- ]{1,60}$")
_SECTION = re.compile(
    r"^(summary|profile|objective|experience|education|skills|work experience|"
    r"professional experience|about)\b",
    re.IGNORECASE,
)


def _clean(line: str) -> str:
    line = _LINK.sub(lambda m: m.group(1), line)
    line = line.replace("mailto:", " ")
    return line.strip().strip("#*_ \t").strip()


def _phone(line: str) -> str | None:
    text = _POSTAL.sub(" ", _EMAIL.sub(" ", line))
    if m := _NANP.search(text):
        return m.group(0).strip()
    if (m := _INTL.search(text)) and 8 <= sum(c.isdigit() for c in m.group(0)) <= 15:
        return m.group(0).strip()
    return None


def _split_name(name: str) -> dict[str, str]:
    parts = name.split(",")
    if len(parts) > 1 and all(_CREDENTIAL.match(p.strip()) for p in parts[1:]):
        name = parts[0]  # "Jane Public, PhD" -> "Jane Public"
    tokens = name.replace(",", " ").split()
    while len(tokens) > 1 and tokens[-1].casefold() in _SUFFIX:
        tokens.pop()
    if not tokens:
        return {}
    out = {"first_name": tokens[0]}
    if len(tokens) > 1:
        out["last_name"] = tokens[-1]
    return out


def _looks_like_name(text: str) -> bool:
    if not text or len(text) > 60 or any(c.isdigit() for c in text) or "@" in text:
        return False
    lowered = text.casefold()
    if "http" in lowered or "www." in lowered or _SECTION.match(text):
        return False
    return 1 < len(text.split(",")[0].split()) <= 5


def _location(segment: str) -> dict[str, str]:
    segment = _ZIP.sub("", segment.strip().rstrip(",").strip())
    if not segment or any(c.isdigit() for c in segment):
        return {}
    parts = [p.strip() for p in segment.split(",")]
    if not 2 <= len(parts) <= 3 or not all(_PLACE.match(p) for p in parts):
        return {}
    if len(parts) == 2:
        return {"city": parts[0], "region": parts[1]} if _STATE.match(parts[1]) else {}
    return {"city": parts[0], "region": parts[1], "country": parts[2]}


def _header_lines(text: str) -> list[str]:
    lines: list[str] = []
    for raw in (text or "").splitlines():
        if not raw.strip():
            continue
        if raw.lstrip().startswith("## ") or _SECTION.match(_clean(raw)):
            break  # the first section heading ends the header
        lines.append(_clean(raw))
        if len(lines) >= HEADER_LINES:
            break
    return lines


def propose_contact(resume_text: str) -> dict[str, str]:
    """Contact fields (``Contact`` field names) found in the header of ``resume_text``."""
    found: dict[str, str] = {}
    named = False
    for line in _header_lines(resume_text):
        if not named and _looks_like_name(line):
            found.update(_split_name(line))
            named = True
            continue
        if "email" not in found and (m := _EMAIL.search(line)):
            found["email"] = m.group(0)
        if "phone" not in found and (phone := _phone(line)):
            found["phone"] = phone
        if "city" not in found:
            for seg in _SEPARATORS.split(line):
                seg = seg.strip().removeprefix("Location:").strip()
                if seg and not _EMAIL.search(seg) and not _phone(seg) and (loc := _location(seg)):
                    found.update(loc)
                    break
    return found
