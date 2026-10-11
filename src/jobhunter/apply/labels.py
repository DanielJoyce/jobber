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
        "comp",
        "total comp",
        "pay expectation",
        "pay expectations",
        "pay requirement",
        "pay requirements",
        "expected pay",
        "desired pay",
        "desired wage",
        "desired wages",
        "wage expectation",
        "base pay",
        "current base",
        "desired base",
        "expected base",
        "annual base",
        "expected ctc",
        "current ctc",
        "ctc",
        "rate",
        "hourly rate",
        "day rate",
        "pay rate",
        "pay range",
        "current pay",
        "wage",
        "wages",
        "remuneration",
        "income",
        "ote",
        "on target earnings",
        "on-target earnings",
        "bonus expectation",
        "bonus expectations",
        "expected bonus",
        "current bonus",
        "current package",
        "expected package",
        "desired package",
        "notice pay",
        "expect to earn",
        "expected earnings",
        "earnings expectation",
        "earnings expectations",
        "annual earnings",
        "minimum pay",
        "hourly expectation",
        "hourly expectations",
        "annual package",
        "what pay",
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
        "latina",
        "latinx",
        "veteran",
        "veteran status",
        "protected veteran",
        "military",
        "armed forces",
        "disability",
        "disabled",
        "sexual orientation",
        "transgender",
        "lgbtq",
        "lgbtq+",
        "religion",
        "religious",
        "marital status",
        "pregnant",
        "pregnancy",
        "minority",
        "indigenous",
        "aboriginal",
        "caste",
    ),
    "identity": (
        "citizenship",
        "citizen",
        "nationality",
        "national origin",
        "street address",
        "address line",
        "address line 1",
        "address line 2",
        "home address",
        "mailing address",
        "current address",
        "address",
        "apartment number",
        "zip",
        "zip code",
        "zipcode",
        "postal code",
        "postcode",
        "post code",
        "pin code",
        "pronouns",
        "pronoun",
        "date of birth",
        "birthdate",
        "birthday",
        "birth",
        "dob",
        "age",
        "how old",
        "ssn",
        "social security",
        "social security number",
        "social insurance",
        "national insurance",
        "national id",
        "passport",
        "tax id",
        "driver's license",
        "drivers license",
        "driver license",
        "driving licence",
    ),
    "attestation": (
        "consent",
        "certify",
        "certification statement",
        "certification of accuracy",
        "applicant certification",
        "certification and signature",
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
    "criminal": (
        "convicted",
        "conviction",
        "convictions",
        "felony",
        "felonies",
        "misdemeanor",
        "misdemeanors",
        "misdemeanour",
        "criminal",
        "pending charges",
        "arrested",
        "charged with",
        "pleaded guilty",
        "pled guilty",
        "plead guilty",
    ),
    "references": (
        "references",
        "reference name",
        "reference phone",
        "reference email",
        "reference contact",
        "referee",
        "referees",
        "name of reference",
        "reference 1",
        "reference 2",
        "reference 3",
        "reference 4",
        "reference 5",
    ),
    "credentials": (
        "password",
        "passcode",
        "security question",
        "security answer",
        "maiden name",
    ),
    "payment": (
        "credit card",
        "debit card",
        "card number",
        "bank account",
        "routing number",
        "account number",
        "iban",
        "sort code",
        "cvv",
        "payment details",
        "bank details",
    ),
}

# Labels that are never-store only as the whole label: a bare "Pay" or "Suite" field is the
# sensitive one, while "pay attention" or "test suite" in a question is not.
NEVER_STORE_WHOLE_LABEL: dict[str, tuple[str, ...]] = {
    "pay": ("pay", "base", "package"),
    "identity": (
        "street",
        "street 1",
        "street 2",
        "apt",
        "apt suite",
        "suite",
        "unit",
        "sin",
        "tin",
    ),
    "attestation": ("certification",),
}

# Option texts that mark a whole choice group as self-identification ("Decline to
# self-identify", CC-305's "I do not want to answer", "Prefer not to disclose", ...).
SELF_ID_OPTIONS = (
    "decline to self identify",
    "decline to self-identify",
    "prefer not to say",
    "prefer not to answer",
    "i don't wish to answer",
    "i do not wish to answer",
    "i don't want to answer",
)
_SELF_ID_OPTION = re.compile(
    r"\b(decline|declined|prefer not|choose not|chose not|do not (want|wish)|don't (want|wish)"
    r"|would rather not|rather not)\b.{0,20}\b(answer|say|state|disclose|identify|self[\s-]?"
    r"identify|respond|specify|share)\b"
)

# A phrase with an exception matches only where the exception does not, so routine
# engineering questions still draft: a race condition, rating your skills or rate limiting,
# a release sign-off, how you would address a problem, an email address.
_EXCEPTIONS: dict[str, re.Pattern[str]] = {
    "race": re.compile(r"\brace[\s-]+(condition|conditions|free)\b|\bdata races?\b"),
    "rate": re.compile(
        r"\brate\s+(your|yourself|my|our|the|each|how|these|this|on|from|of|proficiency)\b"
        r"|\brate[\s-]+limit\w*|\b(error|success|failure|conversion|retention|churn|hit|"
        r"growth|click[\s-]through|bit|frame|sample|refresh|response|interest|exchange)\s+rate"
    ),
    "sign": re.compile(r"\bsign[\s-]+(off|offs|up|in|out|language)\b|\bsign\s+(of|that)\b"),
    "address": re.compile(
        r"\b(e[\s-]?mail|web|website|ip|mac|memory|url|wallet)\s+address\b"
        r"|\baddress(es)?\s+(a|an|the|this|that|these|those|it|them|your|our|their|his|her|any|"
        r"each|such|issues?|conflicts?|concerns?|problems?|challenges?|feedback|gaps?|risks?|"
        r"needs?|questions?|bugs?|requirements?|disagreements?|technical|performance)\b"
        r"|\b(how|to|would|did|do|will|can|you|we|i)\s+address\b"
    ),
    "military": re.compile(r"\bmilitary[\s-]+(grade|time)\b"),
    "criminal": re.compile(r"\bcriminal\s+justice\b"),
    "birth": re.compile(r"\bbirth\s+of\b"),
    "references": re.compile(r"\bcross[\s-]references\b"),
}

# "Are you 18 or older?" and "Are you of legal working age?" are yes/no eligibility questions,
# not an age; a field asking for the age itself ("Age (must be 18 or older)") is never-store.
_AGE_ELIGIBILITY = re.compile(
    r"\b(18|eighteen)\s*(years\s*)?(of\s*age\s*)?(or|and)\s*(older|over|above)\b"
    r"|\b(at least|over|above)\s+(the\s+age\s+of\s+)?(18|eighteen)\b"
    r"|\blegal\s+(working\s+)?age\b|\bage\s+(is\s+)?(18|eighteen)\s+or\s+(above|older|over)\b"
)
_AGE_FIELD = re.compile(r"^(your\s+|current\s+)?age\b|\bwhat is your age\b|\bhow old\b")

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
_SEPARATORS = re.compile(r"[\s_\-/]+")


@dataclass(frozen=True)
class Match:
    category: str
    phrase: str


def _excepted(phrase: str, norm: str) -> bool:
    if phrase == "age":
        return bool(_AGE_ELIGIBILITY.search(norm)) and not _AGE_FIELD.search(norm)
    exc = _EXCEPTIONS.get(phrase)
    if exc is None:
        return False
    # Excepted only if every occurrence of the phrase sits inside an excepted use.
    hits = list(_pattern(phrase).finditer(norm))
    spans = [m.span() for m in exc.finditer(norm)]
    return all(any(s <= h.start() and h.end() <= e for s, e in spans) for h in hits)


def never_store(label: str, options: Sequence[str] = (), names: Iterable[str] = ()) -> Match | None:
    """The first never-store match for a question label (and, in phase 2, field names and a
    choice group's option texts), or None."""
    for text in (label, *names):
        # Underscores, hyphens and slashes as spaces, so a field name like ``address_line_1``
        # reads as its words.
        norm = _SEPARATORS.sub(" ", normalize(text)).strip()
        if not norm:
            continue
        for cat, phrase, pat in _PATTERNS:
            if pat.search(norm) and not _excepted(phrase, norm):
                return Match(cat, phrase)
        if norm in _WHOLE:
            return Match(_WHOLE[norm], norm)
    for option in options:
        norm = normalize(option)
        if any(p.search(norm) for p in _OPTION_PATTERNS) or _SELF_ID_OPTION.search(norm):
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
