"""Structured output of the drafting calls (specs/017 "Targeted resume").

The model returns structure, not prose, so every line can be checked. The resume, the cover
letter and a question draft are **separate calls with separate schemas**; the advisory
entailment pass has its own. Both runners send the same schema (``--json-schema`` on the CLI,
``output_config.format`` on the API) and validate with the same pydantic model.

Sources: resume lines ``L*``, employer-note lines ``N*``, story-fact lines ``S*``.
"""

from __future__ import annotations

import copy
from typing import Any, Literal

from anthropic import transform_schema
from pydantic import BaseModel, ConfigDict


class _Out(BaseModel):
    model_config = ConfigDict(extra="ignore")


class Cited(_Out):
    text: str
    sources: list[str]


class Entry(_Out):
    source_line: str
    employer: str
    title: str
    dates: str
    bullets: list[Cited]


class Section(_Out):
    heading: str
    entries: list[Entry]


class Skill(_Out):
    name: str
    sources: list[str]


class Resume(_Out):
    # The contact header, as resume line ids rendered verbatim (never reworded).
    header: list[str]
    summary: Cited
    sections: list[Section]
    skills: list[Skill]
    omitted: list[str]
    change_notes: list[str]


class ResumeOut(_Out):
    resume: Resume


class Paragraph(_Out):
    text: str
    resume_sources: list[str]  # L* and N*
    posting_quotes: list[str]


class CoverLetter(_Out):
    paragraphs: list[Paragraph]


class LetterOut(_Out):
    cover_letter: CoverLetter


class Sentence(_Out):
    text: str
    sources: list[str]  # L*, N*, S*
    posting_quotes: list[str]


class Draft(_Out):
    sentences: list[Sentence]


class DraftOut(_Out):
    draft: Draft


class Verdict(_Out):
    id: str
    verdict: Literal["yes", "partly", "no"]


class EntailmentOut(_Out):
    lines: list[Verdict]


OUTPUT_MODELS: dict[str, type[_Out]] = {
    "resume": ResumeOut,
    "cover_letter": LetterOut,
    "question_draft": DraftOut,
    "entailment": EntailmentOut,
}


def json_schema(kind: str) -> dict[str, Any]:
    """The structured-output schema for ``kind`` (strict: no extra keys, all required)."""
    return transform_schema(copy.deepcopy(OUTPUT_MODELS[kind].model_json_schema()))
