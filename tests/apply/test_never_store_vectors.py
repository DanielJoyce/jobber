"""The never-store matcher against its vector file (specs/017 "Never-store list").

Every label in ``tests/fixtures/never_store_vectors.json`` must match its category, and every
negative (``expected: null``) must not: a false positive refuses a question the user wanted
drafted, so the word-boundary negatives are as important as the positives.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jobhunter.apply import labels

VECTORS = json.loads(
    (Path(__file__).parents[1] / "fixtures" / "never_store_vectors.json").read_text("utf-8")
)["vectors"]


@pytest.mark.parametrize("v", VECTORS, ids=[v["label"][:40] for v in VECTORS])
def test_vector(v):
    m = labels.never_store(v["label"], v["options"])
    assert (m.category if m else None) == v["expected"], (v["label"], m)


def test_the_file_covers_every_category_and_the_spec_negatives():
    expected = {v["expected"] for v in VECTORS}
    assert {"pay", "eeo", "identity", "attestation", None} <= expected
    negatives = " ".join(v["label"].lower() for v in VECTORS if v["expected"] is None)
    for word in ("managed", "language", "design", "generate", "collaborate", "trace"):
        assert word in negatives, word


def test_an_option_text_marks_the_whole_choice_group():
    # The label alone is harmless; the self-ID option makes the group yours.
    assert labels.never_store("Please select one", ["Yes", "No"]) is None
    m = labels.never_store("Please select one", ["Yes", "No", "Decline to self-identify"])
    assert m is not None and m.category == "eeo"


def test_field_names_are_matched_too():
    assert labels.never_store("Field 7", names=["address_line_1"]) is not None
    assert labels.never_store("Field 7", names=["applicant_street"]) is None
