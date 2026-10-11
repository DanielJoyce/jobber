"""/prefs Application answers section (specs/017 "Application answers"): form <-> ``Answers``.

The section sits in the main /prefs form and saves with it, but its data never touches
``Profile``: it is parsed here, validated by ``apply/answers.py::validate_for_save`` (which
refuses a never-store question with nothing written) and written under ``answers:`` by the same
014 round-trip writer, in the same atomic write as the profile's own changes. A form that does
not carry the section (``ans.present``) leaves ``answers:`` alone.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from jobhunter.apply import answers as ans

PRESENT = "ans.present"
WORK_AUTH_CHOICES = (("", "not stored"), ("yes", "yes"), ("no", "no"))

Form = Mapping[str, Sequence[str]]


@dataclass
class AnswersForm:
    present: bool
    view: dict[str, Any]
    errors: dict[str, str] = field(default_factory=dict)  # keyed by form field
    answers: ans.Answers | None = None


def _last(form: Form, key: str) -> str:
    values = form.get(key)
    return values[-1] if values else ""


def view_of(a: ans.Answers) -> dict[str, Any]:
    """Form field -> value for rendering, plus ``custom`` as (question, answer) rows."""
    view: dict[str, Any] = {f"ans.links.{k}": getattr(a.links, k) or "" for k in ans.LINK_KEYS}
    for k, _label in ans.TEXT_FIELDS:
        view[f"ans.{k}"] = getattr(a, k) or ""
    wa = a.work_authorization
    view["ans.work_authorization"] = "" if wa is None else ("yes" if wa else "no")
    view["custom"] = [(c.question, c.answer) for c in a.custom]
    return view


def parse(form: Form) -> AnswersForm:
    """Read the section from a submitted form and validate it (never-store first)."""
    present = PRESENT in form
    # Rows with both boxes empty (the blank one for adding, or one you cleared) are dropped
    # first, so a row's index is the same in the errors and in the re-rendered form.
    rows = [
        (" ".join(q.split()), a.replace("\r\n", "\n").strip())
        for q, a in zip(
            form.get("ans.custom.question", []), form.get("ans.custom.answer", []), strict=False
        )
    ]
    rows = [(q, a) for q, a in rows if q or a]
    view: dict[str, Any] = {f"ans.links.{k}": _last(form, f"ans.links.{k}") for k in ans.LINK_KEYS}
    for k, _label in ans.TEXT_FIELDS:
        view[f"ans.{k}"] = _last(form, f"ans.{k}")
    view["ans.work_authorization"] = _last(form, "ans.work_authorization")
    view["custom"] = rows
    out = AnswersForm(present=present, view=view)
    if not present:
        return out
    errors: dict[str, str] = {}
    wa = view["ans.work_authorization"]
    if wa not in ("", "yes", "no"):
        errors["ans.work_authorization"] = "pick yes, no or not stored"
    custom: list[dict[str, str]] = []
    where: list[int] = []  # row index of each kept custom entry
    for i, (q, a) in enumerate(rows):
        if not q:
            errors[f"ans.custom.{i}.question"] = "a saved answer needs its question"
            continue
        custom.append({"question": q, "answer": a})
        where.append(i)
    data: dict[str, Any] = {
        "links": {k: view[f"ans.links.{k}"] for k in ans.LINK_KEYS},
        **{k: view[f"ans.{k}"] for k, _label in ans.TEXT_FIELDS},
        "work_authorization": {"yes": True, "no": False}.get(wa),
        "custom": custom,
    }
    if not errors:
        try:
            out.answers = ans.validate_for_save(data)
        except ans.AnswersRefused as exc:
            for path, message in exc.errors.items():
                errors[_form_field(path, where)] = message
    out.errors = errors
    return out


def _form_field(path: str, where: list[int]) -> str:
    """``custom.1.answer`` (model path) -> ``ans.custom.<row>.answer`` (form row index)."""
    parts = path.split(".")
    if parts[0] == "custom" and len(parts) >= 2 and parts[1].isdigit():
        row = where[int(parts[1])] if int(parts[1]) < len(where) else int(parts[1])
        return f"ans.custom.{row}.{parts[2] if len(parts) > 2 else 'question'}"
    return f"ans.{path}"
