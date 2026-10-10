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


# Review fixes (bug ac634ed). Header blocks below are trimmed from real public postings.
NLX_HEADER = (
    "Minimum Education Required\n\nBachelor's Degree\n\nCompensation\n\n$7.25 / hourly\n\n"
    "Hours Per Week\n\n40\n\nNumber Of Positions\n\n1\n\n"
)


def test_federal_minimum_wage_placeholder_is_not_pay():
    # Boards fill the Compensation field with $7.25 (the federal minimum) on every posting.
    assert ex(NLX_HEADER) is None
    assert ex("Compensation $7.25 / hourly Hours Per Week 40") is None


def test_real_range_beats_placeholder_and_earlier_period():
    text = NLX_HEADER + (
        "The estimated base salary range for new hires into this role is $170,000 - $195,000 "
        "annually + annual bonus, depending on factors such as job-related skills."
    )
    for loc in (None, "Lexington, KY"):
        s = ex(text, loc)
        assert s is not None and (s.min, s.max, s.period) == (170000, 195000, "year"), loc


def test_mixed_periods_the_pay_range_wins_over_an_earlier_hourly_mention():
    text = (
        "Overtime is paid at $45/hr. The salary range for this role is $100,000 - $140,000 "
        "per year."
    )
    s = ex(text)
    assert (s.min, s.max, s.period) == (100000, 140000, "year")
    # Without an overtime word, the stronger evidence (a range) still wins over a lone figure.
    s = ex("Pay: $45/hr for on-call. The salary range is $100,000 - $140,000 per year.")
    assert (s.min, s.max, s.period) == (100000, 140000, "year")


@pytest.mark.parametrize(
    ("text", "want"),
    [
        ("Salary $130,000 plus $25,000/yr bonus", None),
        ("Base pay: $150,000. Compensation also includes a $20,000 annual bonus.", None),
        ("Your compensation package includes $40,000 per year in RSUs.", None),
        ("Base salary: $120,000 - $140,000. Equity: $200,000 per year in RSUs.", (120000, 140000)),
        ("Salary: $130,000 per year plus bonus", (130000, 130000)),
        ("The pay range is $150,000 - $180,000 per year plus equity", (150000, 180000)),
    ],
)
def test_bonus_or_equity_with_a_period_is_not_salary(text, want):
    s = ex(text)
    assert (None if s is None else (s.min, s.max)) == want


@pytest.mark.parametrize(
    "text",
    [
        "scale the platform from a minimum 20,000 to a maximum 50,000 users. "
        "The salary range for this role is $120,000 - $160,000.",
        "Minimum 40,000 hours flight time; maximum 60,000 cycles.",
        "The system handles a minimum 20,000 and maximum 50,000 transactions per second.",
    ],
)
def test_minmax_needs_currency(text):
    s = ex(text)
    assert s is None or (s.min, s.max) == (120000, 160000)


def test_minmax_labelled_pay_still_parses():
    s = ex("Pay Range Minimum: $129,100.00 Pay Range Maximum: $214,500.00")
    assert (s.min, s.max, s.period) == (129100, 214500, "year")


INLINE_TIERS = (
    "Pay: San Francisco: $180,000 - $220,000, New York: $170,000 - $210,000, "
    "Austin: $150,000 - $190,000."
)


@pytest.mark.parametrize(
    ("text", "loc", "want"),
    [
        (INLINE_TIERS, "Austin, TX", (150000, 190000)),
        (INLINE_TIERS, "New York, NY", (170000, 210000)),
        (INLINE_TIERS, "San Francisco, CA", (180000, 220000)),
        (
            "- Base Pay/Salary Chicago,IL $152,000.00-$225,000.00; "
            "Jersey City,NJ $175,750.00-$260,000.00\n\nJob Description",
            "Jersey City, NJ",
            (175750, 260000),
        ),
        (
            "USA, CA, San Francisco - 176,600.00 - 239,000.00 USD annually\n\n"
            "USA, CA, Santa Monica - 153,600.00 - 207,800.00 USD annually\n\n"
            "USA, NY, New York - 169,000.00 - 228,600.00 USD annually",
            "Santa Monica, CA",
            (153600, 207800),
        ),
        (
            "Basic Compensation: $150,000 - $200,000 (For Beavercreek, OH Only); "
            "$140,000 - $180,000 (For Dayton, OH Only)",
            "Beavercreek, OH",
            (150000, 200000),
        ),
    ],
)
def test_location_tier_label_belongs_to_its_own_range(text, loc, want):
    s = ex(text, loc)
    assert (s.min, s.max) == want


def test_range_split_by_per_period_on_each_side_stays_whole():
    text = (
        "For San Francisco, CA-based roles: The base salary range for this role is "
        "USD $202,000 per year - USD $224,000 per year.\n\nFor New York, NY-based roles: "
        "The base salary range for this role is USD $190,000 per year - USD $211,000 per year."
    )
    for loc, want in [
        ("San Francisco, CA", (202000, 224000)),
        ("New York, NY", (190000, 211000)),
        (None, (190000, 224000)),
    ]:
        s = ex(text, loc)
        assert (s.min, s.max, s.period) == (*want, "year"), loc


@pytest.mark.parametrize(
    ("text", "lo", "hi", "period"),
    [
        ("and equity. Base Salary Range \\$225,000 - \\$265,000 USD Bring", 225000, 265000, "year"),
        ("Pay: \\$14.00 to \\$19.00 per hour", 14, 19, "hour"),
        ("Salary: $164,757 -- $178,968.00 per year", 164757, 178968, "year"),
    ],
)
def test_escaped_dollar_and_double_dash_ranges(text, lo, hi, period):
    s = ex(text)
    assert (s.min, s.max, s.period) == (lo, hi, period)
