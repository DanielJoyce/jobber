"""Capture fixtures are hand-written and synthetic (specs/017 1e): no personal data in them."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "capture"
FILES = sorted(p for p in FIXTURES.rglob("*") if p.is_file())
EMAIL = re.compile(r"[\w.+-]+@([\w-]+\.)+[a-z]{2,}", re.I)
PHONE = re.compile(r"\(?\b\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}\b")
TOKEN = re.compile(r"""(?:value|content|data-[\w-]+)\s*=\s*["'][A-Za-z0-9_\-]{32,}["']""")


@pytest.mark.parametrize("path", FILES, ids=lambda p: str(p.relative_to(FIXTURES)))
def test_capture_fixture_has_no_personal_data(path):
    text = path.read_text(encoding="utf-8")
    for m in EMAIL.finditer(text):
        assert m.group(0).lower().endswith("@example.com"), m.group(0)
    assert not PHONE.search(text)
    assert not TOKEN.search(text)
    assert 'type="hidden"' not in text.lower()


def test_fixtures_exist():
    assert any(p.suffix == ".html" for p in FILES)
