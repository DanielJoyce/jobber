"""The single bucket-name table and its helpers."""

from __future__ import annotations

from jobhunter.core import bucketnames as bn


def test_bucket_name_title_cases_the_inbox_names():
    assert [bn.bucket_name(b) for b in "ABCDEFG"] == [
        "Bullseye",
        "Strong",
        "Stretch Up",
        "Lateral",
        "Downlevel",
        "Stale Match",
        "Mismatch",
    ]
    assert bn.bucket_name("b") == "Strong"
    assert bn.bucket_name(None) == "unscored"
    assert bn.bucket_name("Z") == "Z"


def test_bucket_label_keeps_the_letter_for_terminals():
    assert bn.bucket_label("B") == "Strong (B)"
    assert bn.bucket_label("F") == "Stale Match (F)"
    assert bn.bucket_label(None) == "unscored"


def test_parse_letters_filters_dedupes_and_orders():
    assert bn.parse_letters("b, a,x,B") == ["A", "B"]
    assert bn.parse_letters("") == [] and bn.parse_letters(None) == []
    assert bn.parse_letters(["G", "A"]) == ["A", "G"]


def test_group_name():
    assert bn.group_name("AB") == "Bullseye + Strong" == bn.GROUP_AB
    assert bn.group_name(["C"]) == "Stretch Up"
    assert bn.group_name(bn.FIT_GROUP) == "All fits"
    assert bn.group_name(bn.LETTERS) == "All buckets"
    assert bn.group_name("ABCD") == "4 buckets"
    assert bn.group_name([]) == "No buckets"
