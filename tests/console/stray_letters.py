"""Scanner for bucket letters shown where a bucket name belongs (shared by console tests)."""

from __future__ import annotations

import re

# Buckets are shown by name; a bare letter is allowed only as a small badge
# (class bucket-badge / bucket-letter), which is removed before scanning.
_BADGE = re.compile(r'<span class="(?:bucket-badge|bucket-letter)[^"]*"[^>]*>.*?</span>', re.S)
_TITLE_ATTR = re.compile(r'\s(?:title|aria-label)="[^"]*"')  # hover text may say "Bucket B"
_KBD = re.compile(r"<kbd>.*?</kbd>", re.S)  # keyboard shortcut keys, not buckets
_SCRIPT = re.compile(r"<(script|style)\b.*?</\1>", re.S | re.I)
_STRAY = [
    ("letters joined with +", re.compile(r"\b[A-G]\s?\+\s?[A-G]\b")),
    ("'bucket X'", re.compile(r"\bbucket\s+[A-G]\b", re.I)),
    ("letter then count", re.compile(r">\s*[A-G]\s+\d+\s*<")),
    ("bare letter element", re.compile(r">\s*[A-G]\s*<")),
    ("letter arrow letter", re.compile(r"\b[A-G]\s*(?:→|&rarr;|->)\s*[A-G]\b")),
    ("New A", re.compile(r"\bNew [A-G]\b")),
]


def stray_letters(html: str) -> list[str]:
    visible = _KBD.sub("", _BADGE.sub("", _SCRIPT.sub("", _TITLE_ATTR.sub("", html))))
    return [name for name, rx in _STRAY if rx.search(visible)]
