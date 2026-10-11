"""Answers (specs/017 "Never-store list", "Application answers").

Two stores, no overlap:

- **Per packet**, ``packet_answer``, written only by :func:`save_packet_answer`: employer notes
  (``notes:employer``, ``N1``-``N3``), a behavioral question's story facts
  (``story:<question key>``, ``S1``-``S3``) and, from phase 1d, **Save as answer**
  (``q:<question key>``, source ``user`` or ``draft``). Notes and story facts are drafting
  inputs; ``q:`` answers are never sent to a model.
- **Application answers on /prefs**, the ``answers:`` key of ``preferences.yaml``, validated by
  the :class:`Answers` model. It is loaded **apart from** ``Profile`` (which never sees the key),
  leniently, entry by entry, so a bad hand edit drops that entry with a warning and can never
  stop scoring or the nightly run. Its two writers, the /prefs form save and **Promote to
  /prefs**, both validate through the model first, which refuses a never-store label with
  nothing written. No ``answers:`` content is ever part of a model request.

Every write runs the never-store match on the label first. Logs and warnings name labels and
keys, never answer values.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from ruamel.yaml import YAML
from ruamel.yaml.constructor import DuplicateKeyError
from ruamel.yaml.error import YAMLError

from jobhunter.apply import labels

logger = logging.getLogger(__name__)

NOTES_KEY = "notes:employer"
QUESTION_PREFIX = "q:"
MAX_LINES = 3
MAX_LINE_CHARS = 500
MAX_ANSWER_CHARS = 5000
MAX_QUESTION_CHARS = 300


class NeverStore(ValueError):
    """A write refused by the never-store list. ``str()`` is the message for the page."""


def never_store_message(label: str, match: labels.Match) -> str:
    return (
        f"This one is yours: '{label}' is on the never-store list ({match.category}); answer "
        "it by hand. jobhunter doesn't store, draft or fill it."
    )


def refuse_never_store(label: str) -> None:
    """Raise :class:`NeverStore` when ``label`` is on the never-store list."""
    if (m := labels.never_store(label)) is not None:
        raise NeverStore(never_store_message(label, m))


def story_key(question: str) -> str:
    return f"story:{labels.question_key(question)}"


def clean_lines(raw: str | list[str]) -> list[str]:
    """One to three non-empty lines, each trimmed and capped."""
    items = raw.splitlines() if isinstance(raw, str) else list(raw)
    out = [" ".join(x.split())[:MAX_LINE_CHARS] for x in items if x and x.strip()]
    if len(out) > MAX_LINES:
        raise ValueError(f"at most {MAX_LINES} lines, please")
    return out


def save_packet_answer(
    conn: sqlite3.Connection,
    packet_id: int,
    field_key: str,
    label: str,
    value: str,
    source: str = "user",
) -> None:
    """Insert or replace one ``packet_answer``. Raises :class:`NeverStore` on a match of the
    label (or the question inside a ``story:``/``q:`` key) against the never-store list."""
    for text in (label, field_key.partition(":")[2]):
        if (m := labels.never_store(text)) is not None:
            raise NeverStore(never_store_message(label, m))
    if source not in ("draft", "user"):
        raise ValueError(f"bad source {source!r}")
    conn.execute(
        "INSERT INTO packet_answer (packet_id, field_key, label, value, source) "
        "VALUES (?, ?, ?, ?, ?) ON CONFLICT (packet_id, field_key) DO UPDATE SET "
        "label = excluded.label, value = excluded.value, source = excluded.source",
        (packet_id, field_key, label, value, source),
    )


def get_lines(conn: sqlite3.Connection, packet_id: int, field_key: str) -> list[str]:
    row = conn.execute(
        "SELECT value FROM packet_answer WHERE packet_id = ? AND field_key = ?",
        (packet_id, field_key),
    ).fetchone()
    return [x for x in (row[0] or "").splitlines() if x.strip()] if row else []


def employer_notes(conn: sqlite3.Connection, packet_id: int) -> list[str]:
    return get_lines(conn, packet_id, NOTES_KEY)


def story_facts(conn: sqlite3.Connection, packet_id: int, question: str) -> list[str]:
    return get_lines(conn, packet_id, story_key(question))


# ─── Save as answer and reuse (packet_answer ``q:`` rows) ───────────────────


@dataclass(frozen=True)
class SavedRow:
    """One saved ``q:`` answer on a packet. ``question`` is the label as the user gave it."""

    id: int
    packet_id: int
    field_key: str
    question: str
    value: str
    source: str


def question_field_key(question: str) -> str:
    return QUESTION_PREFIX + labels.question_key(question)


def _row(r: sqlite3.Row) -> SavedRow:
    return SavedRow(
        id=int(r["id"]),
        packet_id=int(r["packet_id"]),
        field_key=r["field_key"],
        question=r["label"] or r["field_key"].removeprefix(QUESTION_PREFIX),
        value=r["value"] or "",
        source=r["source"],
    )


def save_question_answer(
    conn: sqlite3.Connection, packet_id: int, question: str, value: str, source: str = "user"
) -> SavedRow:
    """**Save as answer**: keep a draft or your own text on this packet (``q:<question>``).

    The never-store check runs first, on the question, before anything else is looked at.
    Writes nothing to /prefs (that is Promote to /prefs)."""
    question = " ".join((question or "").split())[:MAX_QUESTION_CHARS]
    if not question:
        raise ValueError("Paste the question first.")
    refuse_never_store(question)
    value = clean_text(value or "").strip()
    if not value:
        raise ValueError("Write the answer (or draft it) first; an empty answer is not saved.")
    if len(value) > MAX_ANSWER_CHARS:
        raise ValueError(f"An answer is at most {MAX_ANSWER_CHARS} characters.")
    key = question_field_key(question)
    save_packet_answer(conn, packet_id, key, question, value, source)
    row = conn.execute(
        "SELECT * FROM packet_answer WHERE packet_id = ? AND field_key = ?", (packet_id, key)
    ).fetchone()
    return _row(row)


def is_never_store(row: SavedRow) -> bool:
    """Re-checked on every read: the table grows, so an answer saved before its label joined
    the never-store list is never shown, offered or promoted again."""
    key = row.field_key.removeprefix(QUESTION_PREFIX)
    return labels.never_store(row.question) is not None or labels.never_store(key) is not None


def packet_answers(conn: sqlite3.Connection, packet_id: int) -> list[SavedRow]:
    """This packet's saved ``q:`` answers, oldest first, never-store ones left out."""
    return [r for r in _all_packet_answers(conn, packet_id) if not is_never_store(r)]


def hidden_answers(conn: sqlite3.Connection, packet_id: int) -> int:
    """Saved answers on this packet whose label is now on the never-store list (hidden)."""
    return sum(1 for r in _all_packet_answers(conn, packet_id) if is_never_store(r))


def _all_packet_answers(conn: sqlite3.Connection, packet_id: int) -> list[SavedRow]:
    rows = conn.execute(
        "SELECT * FROM packet_answer WHERE packet_id = ? AND field_key LIKE 'q:%' ORDER BY id",
        (packet_id,),
    ).fetchall()
    return [_row(r) for r in rows]


def get_answer(conn: sqlite3.Connection, answer_id: int) -> SavedRow | None:
    row = conn.execute(
        "SELECT * FROM packet_answer WHERE id = ? AND field_key LIKE 'q:%'", (answer_id,)
    ).fetchone()
    return _row(row) if row else None


def earlier_answers(
    conn: sqlite3.Connection, packet_id: int, field_key: str, limit: int = 5
) -> list[SavedRow]:
    """Answers saved on **other** packets to the same normalized question, newest first, one
    per distinct text: what a later packet offers to copy."""
    rows = conn.execute(
        "SELECT * FROM packet_answer WHERE field_key = ? AND packet_id != ? ORDER BY id DESC",
        (field_key, packet_id),
    ).fetchall()
    out: list[SavedRow] = []
    seen: set[str] = set()
    for r in rows:
        row = _row(r)
        if is_never_store(row):
            continue
        if row.value.strip() and row.value not in seen:
            seen.add(row.value)
            out.append(row)
        if len(out) >= limit:
            break
    return out


# ─── Application answers on /prefs (``answers:`` in preferences.yaml) ───────

LINK_FIELDS: tuple[tuple[str, str], ...] = (
    ("portfolio", "Portfolio link"),
    ("github", "GitHub"),
    ("linkedin", "LinkedIn"),
    ("website", "Personal website"),
)
TEXT_FIELDS: tuple[tuple[str, str], ...] = (
    ("notice_period", "Notice period"),
    ("relocation", "Relocation"),
    ("remote", "Remote preference"),
)
WORK_AUTH_LABEL = "Work authorization (yes/no)"
LINK_KEYS = tuple(k for k, _ in LINK_FIELDS)
SCALAR_KEYS = (*(k for k, _ in TEXT_FIELDS), "work_authorization")
FIELD_LABELS: dict[str, str] = {
    **{f"links.{k}": label for k, label in LINK_FIELDS},
    **dict(TEXT_FIELDS),
    "work_authorization": WORK_AUTH_LABEL,
}


_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def clean_text(value: Any) -> Any:
    """Drop control characters (other than tab and newline) from pasted text: a YAML block
    cannot hold them, and they are never meant (Word and PDF copies carry \\x0b, \\x0c)."""
    if isinstance(value, str):
        return _CONTROL.sub("", value.replace("\r\n", "\n").replace("\r", "\n"))
    return value


def _blank_to_none(value: Any) -> Any:
    value = clean_text(value)
    return None if isinstance(value, str) and not value.strip() else value


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Links(_Strict):
    portfolio: str | None = Field(None, max_length=500)
    github: str | None = Field(None, max_length=500)
    linkedin: str | None = Field(None, max_length=500)
    website: str | None = Field(None, max_length=500)

    _blank = field_validator("*", mode="before")(_blank_to_none)


class SavedAnswer(_Strict):
    """A reusable custom answer, e.g. a "Why this company?" template."""

    question: str = Field(min_length=1, max_length=MAX_QUESTION_CHARS)
    answer: str = Field(min_length=1, max_length=MAX_ANSWER_CHARS)

    _clean = field_validator("question", "answer", mode="before")(clean_text)

    @field_validator("question")
    @classmethod
    def _not_never_store(cls, value: str) -> str:
        # The never-store check lives in the model, so every load and every writer of
        # answers: runs it (specs/017 "Application answers").
        if (m := labels.never_store(value)) is not None:
            raise ValueError(never_store_message(value, m))
        return " ".join(value.split())


class Answers(_Strict):
    """``answers:`` in preferences.yaml. Never part of ``Profile``; never sent to a model."""

    links: Links = Field(default_factory=Links)
    notice_period: str | None = Field(None, max_length=500)
    relocation: str | None = Field(None, max_length=500)
    remote: str | None = Field(None, max_length=500)
    work_authorization: bool | None = None
    custom: list[SavedAnswer] = Field(default_factory=list)

    _blank = field_validator("notice_period", "relocation", "remote", mode="before")(_blank_to_none)


class AnswersRefused(ValueError):
    """A save of ``answers:`` refused; nothing was written. ``errors`` is keyed by the path
    inside ``answers:`` (``custom.0.question``); ``str()`` is one line for the page."""

    def __init__(self, errors: Mapping[str, str]) -> None:
        self.errors = dict(errors)
        super().__init__("; ".join(self.errors.values()))


@dataclass
class LoadedAnswers:
    """What a lenient load found: the valid answers and a warning per dropped entry."""

    answers: Answers = field(default_factory=Answers)
    warnings: list[str] = field(default_factory=list)
    # The subset of ``warnings`` that are never-store drops (a save must refuse these).
    refused: list[str] = field(default_factory=list)
    # ``answers.custom`` entries as written that did not validate (not never-store ones): the
    # form never shows them, so a form save keeps them in the file rather than deleting them.
    ignored_custom: list[Any] = field(default_factory=list)


def _warn(out: LoadedAnswers, message: str, *, never_store: bool = False) -> None:
    logger.warning("preferences answers: %s", message)
    out.warnings.append(message)
    if never_store:
        out.refused.append(message)


def _unknown_key(out: LoadedAnswers, path: str, key: str) -> None:
    label = key.replace("_", " ")
    if (m := labels.never_store(label)) is not None:
        _warn(out, f"{never_store_message(label, m)} answers.{path} was dropped.", never_store=True)
    else:
        _warn(out, f"answers.{path} is not an answer jobhunter knows, so it was ignored.")


def answers_from_data(raw: Any) -> LoadedAnswers:
    """Validate ``answers:`` data entry by entry. Never raises: an entry that does not
    validate (a never-store label above all) is dropped with a warning; the rest load."""
    out = LoadedAnswers()
    if raw is None:
        return out
    if not isinstance(raw, Mapping):
        _warn(out, "answers: is not a mapping of answers, so it was ignored.")
        return out
    kept: dict[str, Any] = {}
    for key_raw, value in raw.items():
        key = str(key_raw)
        if key == "links":
            kept["links"] = _lenient_links(value, out)
        elif key == "custom":
            kept["custom"] = _lenient_custom(value, out)
        elif key in SCALAR_KEYS:
            try:
                Answers.model_validate({key: value})
            except ValidationError:
                _warn(out, f"answers.{key} does not validate, so it was ignored.")
                continue
            kept[key] = value
        else:
            _unknown_key(out, key, key)
    out.answers = Answers.model_validate(kept)
    return out


def _lenient_links(value: Any, out: LoadedAnswers) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        _warn(out, "answers.links is not a mapping of links, so it was ignored.")
        return {}
    links: dict[str, Any] = {}
    for lk_raw, lv in value.items():
        lk = str(lk_raw)
        if lk not in LINK_KEYS:
            _unknown_key(out, f"links.{lk}", lk)
            continue
        try:
            Links.model_validate({lk: lv})
        except ValidationError:
            _warn(out, f"answers.links.{lk} is not a short text, so it was ignored.")
            continue
        links[lk] = lv
    return links


def _lenient_custom(value: Any, out: LoadedAnswers) -> list[SavedAnswer]:
    if not isinstance(value, list):
        _warn(out, "answers.custom is not a list of answers, so it was ignored.")
        return []
    items: list[SavedAnswer] = []
    for i, item in enumerate(value, start=1):
        question = item.get("question") if isinstance(item, Mapping) else None
        if isinstance(question, str) and (m := labels.never_store(question)) is not None:
            message = f"{never_store_message(question, m)} Custom answer {i} was dropped."
            _warn(out, message, never_store=True)
            continue
        try:
            items.append(SavedAnswer.model_validate(item))
        except ValidationError as exc:
            where = ".".join(str(p) for p in exc.errors()[0]["loc"]) or "entry"
            _warn(
                out,
                f"answers.custom item {i} ({where}) does not validate; ignored here and kept in "
                "the file as written.",
            )
            out.ignored_custom.append(item)
    return items


def _yaml(typ: str) -> YAML:
    yaml = YAML(typ=typ, pure=True)
    # A hand-added second answer that forgot its "- " repeats a key; read it (ruamel keeps
    # the first value) rather than losing every answer. ``_duplicate_key_line`` warns.
    yaml.allow_duplicate_keys = True
    return yaml


def _duplicate_key_line(text: str) -> int | None:
    """The 1-based line of the first repeated key inside the ``answers:`` block, if any."""
    from jobhunter.scoring.profile import split_separate_blocks

    # Everything but the answers block becomes blank lines, so line numbers are the file's.
    only = "".join(
        run if key == "answers" else "\n" * run.count("\n")
        for key, run in split_separate_blocks(text)
    )
    try:
        YAML(typ="safe", pure=True).load(only)
    except DuplicateKeyError as exc:
        mark = exc.problem_mark
        return mark.line + 1 if mark is not None else 0
    except YAMLError:
        return None
    return None


def answers_from_text(text: str) -> LoadedAnswers:
    try:
        data = _yaml("safe").load(text)
    except YAMLError:
        out = LoadedAnswers()
        _warn(out, "preferences.yaml is not valid YAML, so no answers were loaded.")
        return out
    out = answers_from_data(data.get("answers") if isinstance(data, Mapping) else None)
    if (line := _duplicate_key_line(text)) is not None:
        where = f"line {line}" if line else "the answers block"
        # Names the line, never the values (logs and warnings carry no answer values).
        _warn(
            out,
            f"preferences.yaml repeats a key inside answers: ({where}); only the first value is "
            "used, and saving answers on this page is refused until you fix it in the raw "
            "editor (other preferences still save).",
        )
    return out


def refused_in_text(text: str) -> list[str]:
    """Every never-store label anywhere in the ``answers:`` block of ``text``, whatever its
    shape: what the raw YAML editor refuses to write. Labels are mapping keys, ``question``
    values (any case) and plain strings in lists; answer values are not labels."""
    try:
        data = _yaml("rt").load(text)
    except YAMLError:
        return []
    raw = data.get("answers") if isinstance(data, Mapping) else None
    found: list[str] = []

    def check(label: str) -> None:
        if (m := labels.never_store(label.replace("_", " "))) is not None:
            message = never_store_message(label, m)
            if message not in found:
                found.append(message)

    def walk(node: Any) -> None:
        if isinstance(node, Mapping):
            for key, value in node.items():
                check(str(key))
                if str(key).casefold() == "question" and isinstance(value, str):
                    check(value)
                walk(value)
        elif isinstance(node, list):
            for item in node:
                if isinstance(item, str):
                    check(item)
                else:
                    walk(item)

    walk(raw)
    return found


def load_answers(profile_dir: Path) -> LoadedAnswers:
    """Read ``answers:`` from ``<profile_dir>/preferences.yaml``. Never raises: a missing file
    is no answers, an unreadable one is no answers and a warning. ``Profile`` is not
    involved, so nothing here can stop scoring or the nightly run."""
    from jobhunter.scoring.profile import PREFERENCES_FILE

    path = Path(profile_dir).expanduser() / PREFERENCES_FILE
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return LoadedAnswers()
    except (OSError, UnicodeDecodeError):
        out = LoadedAnswers()
        _warn(out, "preferences.yaml could not be read, so no answers were loaded.")
        return out
    return answers_from_text(text)


def validate_for_save(data: Mapping[str, Any]) -> Answers:
    """The strict check every writer of ``answers:`` runs first. Raises
    :class:`AnswersRefused`; a never-store match is reported before any other error."""
    for i, item in enumerate(data.get("custom") or []):
        question = item.get("question") if isinstance(item, Mapping) else None
        if isinstance(question, str) and (m := labels.never_store(question)) is not None:
            raise AnswersRefused({f"custom.{i}.question": never_store_message(question, m)})
    try:
        return Answers.model_validate(data)
    except ValidationError as exc:
        raise AnswersRefused(
            {
                ".".join(str(p) for p in e["loc"]): str(e["msg"]).removeprefix("Value error, ")
                for e in exc.errors()
            }
        ) from exc


def answer_writes(
    new: Answers, old: Answers | None = None, keep: list[Any] | None = None
) -> dict[str, Any]:
    """``{dotted path: value}`` for the 014 round-trip writer, only where ``new`` differs from
    ``old``; ``None`` deletes a key (and prunes maps it leaves empty). Keys under ``answers:``
    that jobhunter does not manage are left in the file as they are, and so are the ``keep``
    custom entries (``LoadedAnswers.ignored_custom``) when the custom list is rewritten."""
    old = old or Answers()
    out: dict[str, Any] = {}
    for k in LINK_KEYS:
        if getattr(new.links, k) != getattr(old.links, k):
            out[f"answers.links.{k}"] = getattr(new.links, k)
    for k in SCALAR_KEYS:
        if getattr(new, k) != getattr(old, k):
            out[f"answers.{k}"] = getattr(new, k)
    if new.custom != old.custom:
        out["answers.custom"] = [c.model_dump() for c in new.custom] + list(keep or []) or None
    return out


def write_answers(profile_dir: Path, new: Answers, old: Answers, *, expected_mtime_ns: int) -> int:
    """Write the changed answers into preferences.yaml with the 014 round-trip writer
    (comments and order kept, atomic, refused when the file changed since
    ``expected_mtime_ns``). Returns the number of paths written."""
    from jobhunter.scoring.profile import save_profile_changes

    writes = answer_writes(new, old)
    if writes:
        save_profile_changes(
            profile_dir, writes, expected_mtime_ns=expected_mtime_ns, require_resume=False
        )
    return len(writes)


def promote(conn: sqlite3.Connection, profile_dir: Path, answer_id: int) -> SavedRow:
    """**Promote to /prefs**: copy a saved packet answer into ``answers.custom`` (replacing the
    entry for the same normalized question). Validated by the same model as the /prefs save;
    a never-store question is refused with nothing written."""
    from jobhunter.scoring.profile import PREFERENCES_FILE

    row = get_answer(conn, answer_id)
    if row is None:
        raise KeyError(answer_id)
    refuse_never_store(row.question)
    refuse_never_store(row.field_key.removeprefix(QUESTION_PREFIX))
    prefs = Path(profile_dir).expanduser() / PREFERENCES_FILE
    try:
        mtime = prefs.stat().st_mtime_ns
    except FileNotFoundError as exc:
        raise ValueError("There is no preferences file yet: create it on /prefs first.") from exc
    new_item = (
        validate_for_save({"custom": [{"question": row.question, "answer": row.value}]})
        .custom[0]
        .model_dump()
    )
    # Work on the list as it is in the file, not as the lenient load kept it, so a hand-edited
    # entry the load ignored (say ``answer: 30``) is left where it is rather than deleted.
    raw = _raw_answers(prefs).get("custom")
    if raw is not None and not isinstance(raw, list):
        raise ValueError(
            "answers.custom in preferences.yaml is not a list, so nothing was written; "
            "fix it on /prefs first."
        )
    items: list[Any] = list(raw or [])
    key = labels.question_key(row.question)
    for i, item in enumerate(items):
        q = item.get("question") if isinstance(item, Mapping) else None
        if isinstance(q, str) and labels.question_key(q) == key:
            items[i] = new_item
            break
    else:
        items.append(new_item)
    from jobhunter.scoring.profile import save_profile_changes

    save_profile_changes(
        profile_dir, {"answers.custom": items}, expected_mtime_ns=mtime, require_resume=False
    )
    return row


def _raw_answers(prefs: Path) -> dict[str, Any]:
    try:
        data = _yaml("safe").load(prefs.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, YAMLError):
        return {}
    raw = data.get("answers") if isinstance(data, Mapping) else None
    if raw is not None and not isinstance(raw, Mapping):
        raise ValueError(
            "answers: in preferences.yaml is not a mapping, so nothing was written; "
            "fix it on /prefs first."
        )
    return dict(raw or {})


def prefs_offers(a: Answers, question_keys: set[str]) -> list[tuple[SavedAnswer, bool]]:
    """/prefs custom answers to offer on a packet; those for a question on it come first."""
    marked = [(c, labels.question_key(c.question) in question_keys) for c in a.custom]
    return sorted(marked, key=lambda x: not x[1])
