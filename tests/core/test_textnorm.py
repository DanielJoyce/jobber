from datetime import UTC, datetime

import pytest

from jobhunter.core.textnorm import (
    annualize,
    content_hash,
    detect_employment_type,
    detect_remote,
    html_to_text,
    parse_date,
    parse_salary,
)

SALARY_CASES = [
    # raw, min, max, period, stated
    ("$5,432.00 - $7,310.00 Monthly", 5432.0, 7310.0, "month", True),
    ("$85K–$110K", 85000.0, 110000.0, "year", True),  # noqa: RUF001
    ("$85k-110k/yr", 85000.0, 110000.0, "year", True),
    ("$85-110K per year", 85000.0, 110000.0, "year", True),
    ("39.42/hr", 39.42, 39.42, "hour", True),
    ("$39.42 - $52.10 Hourly", 39.42, 52.10, "hour", True),
    ("$25 an hour", 25.0, 25.0, "hour", True),
    ("Up to $120,000", None, 120000.0, "year", True),
    ("up to $60/hour", None, 60.0, "hour", True),
    ("$120,000+", 120000.0, None, "year", True),
    ("Starting at $45,000 annually", 45000.0, None, "year", True),
    ("$50,000 per annum", 50000.0, 50000.0, "year", True),
    ("$1,200 - $1,500 weekly", 1200.0, 1500.0, "week", True),
    ("$300 - $350 daily", 300.0, 350.0, "day", True),
    ("$2,000 Biweekly", 1000.0, 1000.0, "week", True),
    ("Grade 12: $85,000 - $110,000 per year", 85000.0, 110000.0, "year", True),
    ("$110,000 - $85,000 annually", 85000.0, 110000.0, "year", True),
    ("$4,000", 4000.0, 4000.0, None, True),  # period unknown: not invented
    ("DOE", None, None, None, False),
    ("Commensurate with experience", None, None, None, False),
    ("Negotiable", None, None, None, False),
    ("40 hours per week", None, None, None, False),
    ("see posting", None, None, None, False),
    ("", None, None, None, False),
    (None, None, None, None, False),
]


@pytest.mark.parametrize(("raw", "lo", "hi", "period", "stated"), SALARY_CASES)
def test_parse_salary(raw, lo, hi, period, stated):
    r = parse_salary(raw)
    assert (r.min, r.max, r.period, r.stated) == (lo, hi, period, stated)
    if raw and not stated:
        assert r.warnings  # unparseable is recorded, not silent


def test_salary_currency_and_warnings():
    assert parse_salary("$10/hr").currency == "USD"
    assert parse_salary("10/hr").currency is None
    assert any("biweekly" in w for w in parse_salary("$2,000 biweekly").warnings)
    assert any("inferred" in w for w in parse_salary("$120,000+").warnings)
    assert any("not stated" in w for w in parse_salary("$4,000").warnings)


def test_annualize():
    assert annualize(10, 20, "hour") == (20800, 41600)
    assert annualize(100, None, "day") == (26000, None)
    assert annualize(1, 2, "week") == (52, 104)
    assert annualize(5000, 6000, "month") == (60000, 72000)
    assert annualize(5, 6, "year") == (5, 6)
    assert annualize(5, 6, None) == (None, None)


def test_html_to_text_paragraphs_and_entities():
    out = html_to_text("<p>Hello &amp; welcome</p><p>Second   para</p>")
    assert out == "Hello & welcome\n\nSecond para"


def test_html_to_text_lists():
    out = html_to_text("<p>Duties:</p><ul><li>One</li><li>Two <b>bold</b></li></ul><p>End</p>")
    assert out == "Duties:\n\n- One\n- Two bold\n\nEnd"


def test_html_to_text_drops_script_style_and_br():
    out = html_to_text("<style>p{}</style><script>alert(1)</script>a<br>b&nbsp;c")
    assert out == "a\nb c"


def test_html_to_text_empty_and_plain():
    assert html_to_text(None) == ""
    assert html_to_text("") == ""
    assert html_to_text("just   text\n here") == "just text here"


def test_parse_date_tz_and_garbage():
    d = parse_date("2026-10-03 08:00", "America/Los_Angeles")
    assert d is not None
    assert d.tzinfo is not None
    assert d.astimezone(UTC) == datetime(2026, 10, 3, 15, 0, tzinfo=UTC)
    assert parse_date("not a date at all") is None
    assert parse_date(None) is None
    assert parse_date("") is None


@pytest.mark.parametrize(
    ("title", "loc", "desc", "expected"),
    [
        ("Analyst (Remote)", "Olympia, WA", "", "remote"),
        ("Analyst", "Remote - US", "", "remote"),
        ("Analyst", "Olympia", "This is a hybrid position", "hybrid"),
        ("Analyst", "Olympia", "Telework eligible after probation", "hybrid"),
        ("Analyst", "Olympia", "This is not a remote position", "onsite"),
        ("Analyst", "Olympia", "Position is on-site", "onsite"),
        ("Analyst", "Olympia", "Work from home when needed", "remote"),
        ("Analyst", "Olympia", "Do the work.", "unknown"),
        (None, None, None, "unknown"),
    ],
)
def test_detect_remote(title, loc, desc, expected):
    assert detect_remote(title, loc, desc) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Full-time permanent position", "full_time"),
        ("Part time, 20 hrs", "part_time"),
        ("Temporary assignment", "temporary"),
        ("Seasonal Park Ranger", "seasonal"),
        ("Contract position, 6 months", "contract"),
        ("Independent contractor", "contract"),
        ("Seasonal, full-time during summer", "seasonal"),
        ("Contract administration duties", "unknown"),
        ("", "unknown"),
        (None, "unknown"),
    ],
)
def test_detect_employment_type(text, expected):
    assert detect_employment_type(text) == expected


def test_content_hash_stable_and_normalized():
    a = content_hash("Data Analyst", "Dept of X", "Olympia, WA", "Do  the\nwork")
    b = content_hash("  data analyst ", "DEPT OF X", "olympia,  wa", "do the work")
    assert a == b
    assert len(a) == 64
    assert a != content_hash("Data Analyst", "Dept of X", "Seattle, WA", "Do the work")
    # field boundaries matter
    assert content_hash("ab", "c", None, None) != content_hash("a", "bc", None, None)
