"""The credential rule's cheap gates (bug 501fee8) never change a result.

The gates only skip the full patterns when no pattern can match. Every test here compares the
gated answer with the patterns run alone (``reference``), so a gate that dropped a real match,
or a fast path that folded case differently from ``re.I``, fails.
"""

from __future__ import annotations

import itertools
import re
import string
import sys

import pytest

from jobhunter.scoring import prefilter as pf
from jobhunter.scoring.profile import Profile

KEYS = list(pf._CREDENTIAL_PATTERNS)
RSQUO = "\N{RIGHT SINGLE QUOTATION MARK}"


def reference(text: str, key: str) -> bool:
    return any(p.search(text) for p in pf._COMPILED[key])


# Each phrase must be rejected; together they exercise every alternative of every pattern.
POSITIVE = {
    "active_security_clearance": [
        "An active Secret clearance required.",
        "Active top secret clearance is required for this role.",
        "Active TS/SCI clearance required",
        "Active DoD clearance are required",
        "Candidates must possess an active TS/SCI clearance.",
        "You shall maintain an active security clearance.",
        "Must hold active Top Secret clearance",
        "This role requires an active Secret clearance.",
        "Requires active DoD clearance",
        "Secret clearance required.",
        "Security clearance is required.",
        "TS / SCI clearance required",
    ],
    "cdl": [
        "Valid CDL required.",
        "CDL Class A is required",
        "CDL-Class B required",
        "Current, valid CDL required",
        "Must possess a valid CDL",
        "Must have a CDL class C",
        "Requires a CDL.",
        "Commercial driver's license required.",
        f"Commercial driver{RSQUO}s license required.",
        "Commercial drivers licence is required",
        "Commercial driver license required",
        "Must hold a valid commercial driver's license",
        f"Requires a commercial driver{RSQUO}s licence",
    ],
    "rn_license": [
        "Current RN license required",
        "Active, unrestricted RN licence is required",
        "Registered nurse license required.",
        "License as a registered nurse required",
        "Must hold a current RN license",
        "Must possess an active registered nurse license",
        "Must have a license as a registered nurse",
        "Requires a valid state RN license",
        "Requires licence as a Registered Nurse",
    ],
    "pe_license": [
        "Must hold a valid PE license.",
        "PE license required",
        "Professional Engineer license is required",
        "Professional engineering licence required",
        "License as a professional engineer required",
        "Requires a current professional engineer license",
        "Must possess a license as a Professional Engineer",
        "Licensed Professional Engineer required",
        "Licensed professional engineer is required.",
    ],
}

NEGATIVE = {
    "active_security_clearance": [
        "Secret clearance preferred.",
        "Ability to obtain a clearance.",
        "Must be able to obtain an active clearance.",
        "Clearance desired; active clearance a plus.",
    ],
    "cdl": ["CDL preferred.", "Commercial real estate experience required.", "CDL desired"],
    "rn_license": ["RN license preferred.", "Nurse experience required.", "Modern licensing."],
    "pe_license": [
        "PE license preferred.",
        "Professional engineering experience required.",
        "EIT or PE license a plus.",
    ],
}


@pytest.mark.parametrize(
    ("key", "text"), [(k, t) for k, ts in POSITIVE.items() for t in ts], ids=str
)
def test_every_phrasing_still_matches(key, text):
    assert reference(text, key), "fixture phrase must match the patterns themselves"
    assert pf.credential_in_text(text, key)
    # Embedded in a long posting, in upper case, and through the rule itself.
    long = "Lorem ipsum dolor sit amet. " * 200 + text + " Benefits follow. " * 200
    assert pf.credential_in_text(long, key)
    assert pf.credential_in_text(text.upper(), key) == reference(text.upper(), key) is True
    hard = Profile.model_validate({"hard": {"requires_i_lack": [key]}}).hard
    assert pf.rule_credential_required({"description_text": long}, hard) == pf.CREDENTIAL_REQUIRED


@pytest.mark.parametrize(
    ("key", "text"), [(k, t) for k, ts in NEGATIVE.items() for t in ts], ids=str
)
def test_preferred_and_obtain_do_not_match(key, text):
    assert not reference(text, key)
    assert not pf.credential_in_text(text, key)


# Characters that ``re.I`` treats as an ASCII letter although ``lower()`` does not give it.
UNICODE_SWAPS = {
    "i": ["\N{LATIN SMALL LETTER DOTLESS I}", "\N{LATIN CAPITAL LETTER I WITH DOT ABOVE}"],
    "s": ["\N{LATIN SMALL LETTER LONG S}"],
    "k": ["\N{KELVIN SIGN}"],
    " ": ["\N{NO-BREAK SPACE}", "\t", "\n", "\N{EM SPACE}", "\x1c"],
}


def variants(text: str):
    yield text
    yield text.upper()
    yield text.lower()
    yield text.title()
    yield text.swapcase()
    for ch, subs in UNICODE_SWAPS.items():
        for sub in subs:
            yield text.replace(ch, sub)
            yield text.lower().replace(ch, sub)


def test_gate_agrees_with_patterns_on_generated_variants():
    phrases = [t for ts in (*POSITIVE.values(), *NEGATIVE.values()) for t in ts]
    checked = matched = 0
    for phrase in phrases:
        for text in variants(phrase):
            for key in KEYS:
                want = reference(text, key)
                assert pf.credential_in_text(text, key) == want, (key, text)
                checked += 1
                matched += want
    assert checked > 1000 and matched > 100  # the corpus really exercises both answers


def test_gate_agrees_with_patterns_on_combinatorial_phrasings():
    lead = ["", "must have a ", "shall hold an ", "requires ", "require a ", "must possess "]
    adj = ["", "active ", "current, valid ", "state "]
    noun = [
        "secret clearance",
        "ts/sci clearance",
        "security clearance",
        "cdl class a",
        f"commercial driver{RSQUO}s license",
        "rn license",
        "registered nurse licence",
        "licence as a registered nurse",
        "pe license",
        "professional engineering license",
        "license as a professional engineer",
        "licensed professional engineer",
    ]
    tail = ["", " required", " is required", " are required.", " preferred"]
    for parts in itertools.product(lead, adj, noun, tail):
        base = "".join(parts)
        for text in (base, base.upper(), base.title()):
            for key in KEYS:
                assert pf.credential_in_text(text, key) == reference(text, key), (key, text)


def test_fast_path_folding_assumptions_hold_for_every_code_point():
    """The proof the fast path rests on, checked rather than trusted (see prefilter.py)."""
    every = "".join(chr(cp) for cp in range(128, sys.maxunicode + 1))
    # Non-ASCII characters ``re.I`` equates with some ASCII letter (one scan), then exactly.
    candidates = set(re.findall(r"[a-z]", every, re.I))
    odd = {
        ch
        for ch in candidates
        for letter in string.ascii_lowercase
        if re.fullmatch(letter, ch, re.I) and ch.lower() != letter
    }
    assert odd == set(pf._FOLDS_TO_ASCII)
    for ch in re.findall(r"\s", every):
        assert ch.lower() == ch
    assert [ch for ch in every if len(ch.lower()) != 1] == [
        "\N{LATIN CAPITAL LETTER I WITH DOT ABOVE}"
    ]


def test_memo_is_per_text_and_profile_independent():
    hit = "Active Secret clearance required."
    miss = "Secret clearance preferred."
    lack = Profile.model_validate({"hard": {"requires_i_lack": ["active_security_clearance"]}})
    none = Profile.model_validate({"hard": {"requires_i_lack": ["cdl"]}})
    for _ in range(2):  # the second round runs on the memo
        assert pf.rule_credential_required({"description_text": hit}, lack.hard)
        assert not pf.rule_credential_required({"description_text": hit}, none.hard)
        assert not pf.rule_credential_required({"description_text": miss}, lack.hard)
    # An equal but distinct string object is not mistaken for the remembered one either way.
    copy = "".join(["Secret clearance ", "preferred."])
    assert copy == miss and copy is not miss
    assert not pf.rule_credential_required({"description_text": copy}, lack.hard)
