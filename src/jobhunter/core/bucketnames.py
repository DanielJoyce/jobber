"""Bucket names, the one table every surface reads (specs/006, specs/007, specs/013).

Buckets are stored and passed around as letters (``A``..``G``: URL params, data keys, DB
rows). Anything a person reads shows the name instead, with the letter only as a small
secondary badge (console) or a ``Strong (B)`` suffix (terminal).
"""

from __future__ import annotations

from collections.abc import Iterable

# letter -> (UPPER name, one-line action hint). Order is best fit first.
BUCKET_TITLES: dict[str, tuple[str, str]] = {
    "A": ("BULLSEYE", "apply, minimal tailoring"),
    "B": ("STRONG", "apply, tailor to the gaps"),
    "C": ("STRETCH UP", "apply if you want the jump"),
    "D": ("LATERAL", "apply selectively"),
    "E": ("DOWNLEVEL", "only if the trade is worth it"),
    "F": ("STALE MATCH", "matched skills you last used years ago"),
    "G": ("MISMATCH", "not your field"),
}
LETTERS: tuple[str, ...] = tuple(BUCKET_TITLES)
# The default "good fits" group: what the dashboard's "New ..." numbers count.
DEFAULT_GROUP: tuple[str, ...] = ("A", "B")
# Everything but Mismatch, which the inbox hides by design.
FIT_GROUP: tuple[str, ...] = ("A", "B", "C", "D", "E", "F")


def bucket_name(letter: str | None) -> str:
    """``"B"`` -> ``"Strong"``; a missing letter is ``"unscored"``, an unknown one is as given."""
    if not letter:
        return "unscored"
    entry = BUCKET_TITLES.get(letter.upper())
    return entry[0].title() if entry else letter


def bucket_label(letter: str | None) -> str:
    """``"B"`` -> ``"Strong (B)"``: for terminals, where the letter still helps."""
    if not letter or letter.upper() not in BUCKET_TITLES:
        return bucket_name(letter)
    return f"{bucket_name(letter)} ({letter.upper()})"


def parse_letters(raw: str | Iterable[str] | None) -> list[str]:
    """``"b, a,x"`` -> ``["A", "B"]``: valid letters only, de-duplicated, best fit first."""
    if raw is None:
        return []
    parts = raw.split(",") if isinstance(raw, str) else list(raw)
    wanted = {p.strip().upper() for p in parts}
    return [b for b in LETTERS if b in wanted]


def group_name(letters: Iterable[str]) -> str:
    """Readable name for a set of buckets: ``Bullseye + Strong``, ``All fits``."""
    ls = parse_letters(list(letters))
    if not ls:
        return "No buckets"
    if ls == list(FIT_GROUP):
        return "All fits"
    if ls == list(LETTERS):
        return "All buckets"
    if len(ls) > 3:
        return f"{len(ls)} buckets"
    return " + ".join(bucket_name(b) for b in ls)


GROUP_AB = group_name(DEFAULT_GROUP)  # "Bullseye + Strong"
