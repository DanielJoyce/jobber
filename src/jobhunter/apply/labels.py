"""Question labels: the never-store list and question kinds (specs/017 "Never-store list").

``NEVER_STORE`` is data, matched against the normalized label **before anything else** at every
entry point. Matching is by whole word or phrase, never substring, so "managed", "language",
"design", "generate", "collaborate" and "trace" do not match ``age``, ``sign``, ``rate`` or
``race``.

Every entry point calls this one matcher: question drafts (no generation, no spend on a match),
every ``packet_answer`` write (``apply/answers.py::save_packet_answer``, which Save as answer
goes through), the ``Answers`` model behind /prefs Application answers (on every load, form save
and Promote to /prefs), and the raw YAML save on /prefs. ``tests/fixtures/never_store_vectors.json``
holds the label vectors, negatives included; the table grows like ``ats_rules.py``.
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
        "pay range",
        "current pay",
        "wage",
        "wages",
        "remuneration",
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

# Labels that are never-store only as the whole label: a bare "Address" field is a postal one,
# while "How did you address the conflict?" is a behavioral question and must still draft.
NEVER_STORE_WHOLE_LABEL: dict[str, tuple[str, ...]] = {
    "identity": ("address", "address 1", "address 2", "street", "street 1", "street 2"),
}

# A phrase with an exception matches only where the exception does not: "Are you 18 or older?"
# and "at least 18 years of age" are yes/no eligibility questions, not a date of birth.
_EXCEPTIONS: dict[str, re.Pattern[str]] = {
    "age": re.compile(
        r"\b(18|eighteen)\s*(years\s*(of\s*age\s*)?)?(or|and)\s*(older|over)\b"
        r"|\b(at least|over)\s+(18|eighteen)\b"
    ),
}

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
_WHOLE = {label: cat for cat, labels in NEVER_STORE_WHOLE_LABEL.items() for label in labels}
_SEPARATORS = re.compile(r"[\s_\-]+")


@dataclass(frozen=True)
class Match:
    category: str
    phrase: str


def never_store(label: str, options: Sequence[str] = (), names: Iterable[str] = ()) -> Match | None:
    """The first never-store match for a question label (and, in phase 2, field names and a
    choice group's option texts), or None."""
    for text in (label, *names):
        # Underscores and hyphens as spaces, so a field name like ``address_line_1`` reads as
        # its words.
        norm = _SEPARATORS.sub(" ", normalize(text)).strip()
        if not norm:
            continue
        for cat, phrase, pat in _PATTERNS:
            if pat.search(norm):
                if (exc := _EXCEPTIONS.get(phrase)) is not None and exc.search(norm):
                    continue
                return Match(cat, phrase)
        if norm in _WHOLE:
            return Match(_WHOLE[norm], norm)
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
