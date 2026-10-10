"""Description-text salary extraction (specs/003). Real public posting snippets."""

import pytest

from jobhunter.core.salary_text import extract_salary_from_text as ex


@pytest.mark.parametrize(
    ("text", "lo", "hi", "period"),
    [
        (
            "Compensation $225,000.00 - $260,000.00 / yearly Hours Per Week 40",
            225000,
            260000,
            "year",
        ),
        ("Min USD $130,000.00/Yr. Max USD $160,000.00/Yr.", 130000, 160000, "year"),
        (
            "US: Hiring Range in USD from: $104,500 to $234,600 per annum. May be eligible",
            104500,
            234600,
            "year",
        ),
        ("US: $262000 - $364000 (USD) + 25% bonus target + equity", 262000, 364000, "year"),
        (
            "The expected base pay range for this position is: New York $202,500 - $274,000 EOE",
            202500,
            274000,
            "year",
        ),
        ("The pay range for this position is $70.00 - $90.00/hr.", 70, 90, "hour"),
        (
            "Basic Compensation: $150,000 - $200,000 (For Beavercreek, OH Only)",
            150000,
            200000,
            "year",
        ),
        (
            "Annual base salary range (excluding equity and bonus): $207,955—$218,900 USD",
            207955,
            218900,
            "year",
        ),
        ("The salary range is $95K-$120K depending on experience.", 95000, 120000, "year"),
        ("Pay: $45/hour", 45, 45, "hour"),
        ("Salary: $85-110K", 85000, 110000, "year"),
        ("Pay is $5,000 - $7,000 per month", 5000, 7000, "month"),
        ("Starting at $85,000 per year", 85000, None, "year"),
        ("Salary up to $140,000 annually", None, 140000, "year"),
        ("Compensation: $90,000 USD", 90000, 90000, "year"),
    ],
)
def test_positives(text, lo, hi, period):
    s = ex(text)
    assert s is not None, text
    assert (s.min, s.max, s.period) == (lo, hi, period)
    assert s.raw


def test_tiers_without_dollar_sign_widest():
    text = (
        "Tier 1 - United States of America 118,000 - 178,000 USD per year "
        "Tier 2 - United States of America 131,000 - 197,000 USD per year"
    )
    s = ex(text)
    assert (s.min, s.max, s.period) == (118000, 197000, "year")


def test_location_tier_matches_job_location():
    text = (
        "Pay range in New York: $202,500 - $274,000. Pay range in Austin, TX: "
        "$180,000 - $240,000. Salary range elsewhere in the US: $160,000 - $210,000."
    )
    s = ex(text, "Austin, TX")
    assert (s.min, s.max) == (180000, 240000)
    assert ex(text, "Boise, ID").min == 160000  # widest overall
    assert ex(text, "Boise, ID").max == 274000


@pytest.mark.parametrize(
    "text",
    [
        "We offer a $5,000 sign-on bonus for qualified hires.",
        "A sign-on bonus of $5,000 is available.",
        "The company has raised $1,000,000 in funding.",
        "Series B: $1,000,000 in funding from top investors.",
        "Enjoy a 401(k) with company match.",
        "Benefits include a $15 minimum wage and paid leave.",
        "Up to $5,000 in tuition reimbursement per year.",
        "We have $2 million in annual revenue.",
        "Open to C$120,000 - C$150,000 candidates.",
        "Founded in 2015 - 2020 saw 300% growth.",
        "Gym membership $50/month and snacks.",
        "",
        None,
    ],
)
def test_negatives(text):
    assert ex(text) is None


def test_bonus_not_picked_over_salary():
    s = ex("Base salary: $150,000 - $180,000. Sign-on bonus: $5,000. $1,000,000 in funding.")
    assert (s.min, s.max) == (150000, 180000)


def test_sanity_bounds_reject():
    assert ex("Pay $2/hour") is None
    assert ex("Salary: $9,000,000 per year") is None


def test_ambiguous_no_period_small_number_rejected():
    assert ex("Pay range $40 - $60") is None


# Shapes still missed after the first backfill on real postings.
@pytest.mark.parametrize(
    ("text", "lo", "hi", "period"),
    [
        (
            "Pay Range Minimum: $129,100.00 Pay Range Maximum: $214,500.00 Base pay is",
            129100,
            214500,
            "year",
        ),
        (
            "Anticipated salary range: $94,900 - $135,600 Bonus eligible: No Benefits",
            94900,
            135600,
            "year",
        ),
        (
            "certifications, etc. $156,600 - $215,400 per year This job is eligible for a bonus",
            156600,
            215400,
            "year",
        ),
        ("Compensation $193,461.00 / Yearly Hours Per Week 40", 193461, 193461, "year"),
        ("Compensation $7.25 / hourly Hours Per Week 40", 7.25, 7.25, "hour"),
    ],
)
def test_second_pass_positives(text, lo, hi, period):
    got = ex(text)
    assert got is not None and (got.min, got.max, got.period) == (lo, hi, period)


@pytest.mark.parametrize(
    "text", ["Earn a $5,000 sign-on bonus", "a salary range and a $5,000 bonus"]
)
def test_bonus_still_rejected(text):
    assert ex(text) is None
