"""Question labels: the never-store list and question kinds (specs/017 "Never-store list").

``NEVER_STORE`` is data, matched against the normalized label **before anything else** at every
entry point. Matching is by whole word or phrase, never substring, so "managed", "language",
"design", "generate", "collaborate" and "trace" do not match ``age``, ``sign``, ``rate`` or
``race``.

Phase 1b uses it for question drafts (no generation, no spend on a match) and for every
``packet_answer`` write (``apply/answers.py::save_packet_answer``). Phase 1d owns the rest of the
table's enforcement (the vector file, the ``Answers`` model on /prefs, Promote to /prefs) and
grows the table like ``ats_rules.py``; this module is the one matcher they all call.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

YOURS = "This one is yours; jobhunter doesn't store, draft or fill it. Answer it by hand."

NEVER_STORE: dict[str, tuple[str, ...]] = {
    "pay": (
        "salary",
        "salaries",
        "compensation",
        "pay expectation",
        "pay expectations",
        "expected pay",
        "desired pay",
        "desired wage",
        "desired wages",
        "wage expectation",
        "base pay",
        "expected ctc",
        "current ctc",
        "ctc",
        "rate",
        "hourly rate",
        "pay rate",
        "current pay",
    ),
    "eeo": (
        "gender",
        "gender identity",
        "sex",
        "race",
        "racial",
        "ethnicity",
        "ethnic",
        "hispanic",
        "latino",
        "veteran",
        "veteran status",
        "protected veteran",
        "disability",
        "disabled",
        "sexual orientation",
        "transgender",
        "lgbtq",
        "lgbtq+",
    ),
    "identity": (
        "citizenship",
        "citizen",
        "national origin",
        "street address",
        "address line",
        "address line 1",
        "address line 2",
        "home address",
        "mailing address",
        "current address",
        "zip",
        "zip code",
        "postal code",
        "pronouns",
        "pronoun",
        "date of birth",
        "birthdate",
        "birth date",
        "dob",
        "age",
        "ssn",
        "social security",
        "social security number",
    ),
    "attestation": (
        "consent",
        "certify",
        "certification",
        "attest",
        "attestation",
        "signature",
        "sign",
        "acknowledge",
        "acknowledgement",
        "acknowledgment",
        "information is true",
        "agree to the terms",
    ),
}

# Option texts that mark a whole choice group as self-identification.
SELF_ID_OPTIONS = (
    "decline to self identify",
    "decline to self-identify",
    "prefer not to say",
    "prefer not to answer",
    "i don't wish to answer",
    "i do not wish to answer",
    "i don't want to answer",
)

# "Are you 18 or older?" is a yes/no eligibility question, not a date of birth.
_AGE_EXCEPTION = re.compile(r"\b(18|eighteen)\s*(years\s*(of\s*age\s*)?)?(or|and)\s*(older|over)\b")

_WS = re.compile(r"\s+")
_TRAIL = re.compile(r"[\s*:?.!,;]+$")
_LEAD = re.compile(r"^[\s*]+")
_APOS = str.maketrans({"\u2019": "'", "\u2018": "'", "\u00a0": " "})


def normalize(label: str) -> str:
    """Case fold, NBSP and runs of whitespace to one space, asterisks and trailing punctuation
    dropped."""
    text = unicodedata.normalize("NFKC", label or "").translate(_APOS).casefold()
    text = text.replace("*", " ")
    text = _WS.sub(" ", text).strip()
    text = _TRAIL.sub("", _LEAD.sub("", text))
    return text


def _pattern(phrase: str) -> re.Pattern[str]:
    words = [re.escape(w) for w in phrase.split()]
    # Word boundaries on letters/digits only, so "lgbtq+" and "self-identify" still match.
    return re.compile(r"(?<![a-z0-9])" + r"[\s\-]+".join(words) + r"(?![a-z0-9])")


_PATTERNS: list[tuple[str, str, re.Pattern[str]]] = [
    (cat, phrase, _pattern(phrase)) for cat, phrases in NEVER_STORE.items() for phrase in phrases
]
_OPTION_PATTERNS = [_pattern(normalize(o)) for o in SELF_ID_OPTIONS]


@dataclass(frozen=True)
class Match:
    category: str
    phrase: str


def never_store(label: str, options: Sequence[str] = (), names: Iterable[str] = ()) -> Match | None:
    """The first never-store match for a question label (and, in phase 2, field names and a
    choice group's option texts), or None."""
    for text in (label, *names):
        norm = normalize(text)
        if not norm:
            continue
        for cat, phrase, pat in _PATTERNS:
            if pat.search(norm):
                if phrase == "age" and _AGE_EXCEPTION.search(norm):
                    continue
                return Match(cat, phrase)
    for option in options:
        norm = normalize(option)
        if any(p.search(norm) for p in _OPTION_PATTERNS):
            return Match("eeo", "self-identify option")
    return None


# ─── question kinds (specs/017 "Cover letter and question drafts") ──────────

BEHAVIORAL = re.compile(
    r"\b(describe a (time|situation)|tell (us|me) about a (time|situation)|give (us |me )?an "
    r"example|share an example|walk (us|me) through a time|a time (when|that) you)\b"
)
WHY_US = re.compile(
    r"\b(why (do you want to|would you like to|are you interested in|are you applying|this "
    r"(company|role|position|job)|us|our|join|work (at|for|with))|what (draws|attracts|excites) "
    r"you|interest(ed)? in (this|our|the) (role|company|position|team))\b"
)
NUMERIC = re.compile(
    r"\b(how many years|years of( professional)? experience|years( of)?( hands-on)? "
    r"experience (with|in|using)|number of years)\b|^years of\b"
)


def question_kind(question: str) -> str:
    """``behavioral``, ``why_us``, ``numeric`` or ``other``."""
    q = normalize(question)
    if NUMERIC.search(q):
        return "numeric"
    if BEHAVIORAL.search(q):
        return "behavioral"
    if WHY_US.search(q):
        return "why_us"
    return "other"


def question_key(question: str) -> str:
    """The stored key of a question (``packet_document.question_key``): its normalized label."""
    return normalize(question)[:300]


_FILLER = frozenset(
    [
        "how",
        "many",
        "years",
        "year",
        "of",
        "experience",
        "do",
        "you",
        "have",
        "with",
        "in",
        "using",
        "professional",
        "professionally",
        "hands-on",
        "hands",
        "on",
        "the",
        "a",
        "an",
        "what",
        "is",
        "your",
        "number",
        "total",
        "working",
        "as",
        "at",
        "production",
        "commercial",
        "industry",
        "environment",
        "and",
        "or",
        "any",
        "did",
        "had",
    ]
)


def numeric_subject(question: str) -> str:
    """ "How many years of experience do you have with Terraform?" -> "terraform"."""
    words = re.findall(r"[a-z0-9][a-z0-9+#./-]*", normalize(question))
    return " ".join(w for w in words if w not in _FILLER)
