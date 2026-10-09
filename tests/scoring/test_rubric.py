"""Rubric versioning and the structured-output schema (specs/006 "Calibration")."""

from __future__ import annotations

import json

from jobhunter.core.models import Screen
from jobhunter.scoring import rubric


def test_prompt_change_requires_version_bump():
    assert rubric.prompt_fingerprint() == rubric.PROMPT_FINGERPRINT, (
        "RUBRIC_TEXT or SCREEN_SCHEMA changed: bump PROMPT_VERSION and update "
        f"PROMPT_FINGERPRINT to {rubric.prompt_fingerprint()}"
    )


def test_prompt_version_shape():
    assert rubric.PROMPT_VERSION.startswith("screen-v")


def test_rubric_covers_required_sections():
    text = rubric.RUBRIC_TEXT
    for needle in (
        "raw_skills",
        "recency_weighted_skills",
        "last 3 years",
        "3 to 7",
        "quarter",
        "current_focus_overlap",
        "done_with_hits",
        "stale_skills",
        "shape_flags",
        "blockers",
        "missing_info",
        "tailoring_hints",
        "verbatim",
        "90-100",
        "70-89",
        "40-69",
        "10-39",
        "0-9",
    ):
        assert needle in text, needle


def test_rubric_has_nothing_volatile():
    text = rubric.RUBRIC_TEXT
    assert "2026" not in text
    assert "{" not in text  # no template placeholders left unrendered


def test_schema_is_strict_and_closed():
    schema = rubric.SCREEN_SCHEMA
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(Screen.model_fields)
    dims = schema["properties"]["dimensions"]
    assert dims["additionalProperties"] is False
    assert set(dims["required"]) == {"skills", "seniority", "domain"}
    assert "overall" not in schema["properties"]
    dumped = json.dumps(schema)
    # Keywords strict structured outputs reject are folded into descriptions.
    for keyword in ('"minimum"', '"maximum"', '"maxLength"', '"maxItems"', '"propertyNames"'):
        assert keyword not in dumped, keyword


def test_schema_refs_resolve():
    refs = {"#/$defs/" + name for name in rubric.SCREEN_SCHEMA["$defs"]}
    dumped = json.dumps(rubric.SCREEN_SCHEMA)
    for ref in ("#/$defs/Dimension", "#/$defs/Evidence", "#/$defs/Verdict"):
        assert ref in refs
        assert ref in dumped
