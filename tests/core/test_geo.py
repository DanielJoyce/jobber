import pytest

from jobhunter.core import geo


def test_has_56_unique_entries():
    assert len(geo.STATES) == 56
    assert len({s.usps for s in geo.STATES}) == 56
    assert len({s.fips for s in geo.STATES}) == 56
    assert len({s.name for s in geo.STATES}) == 56


def test_kinds():
    kinds = {s.usps: s.kind for s in geo.STATES}
    assert kinds["DC"] == "district"
    assert sum(k == "state" for k in kinds.values()) == 50
    assert {c for c, k in kinds.items() if k == "territory"} == {"AS", "GU", "MP", "PR", "VI"}


@pytest.mark.parametrize(
    ("usps", "fips"),
    [
        ("CA", "06"),
        ("TX", "48"),
        ("WA", "53"),
        ("DC", "11"),
        ("PR", "72"),
        ("AK", "02"),
        ("HI", "15"),
        ("WY", "56"),
        ("AL", "01"),
        ("AZ", "04"),
        ("AR", "05"),
        ("CO", "08"),
        ("FL", "12"),
        ("ID", "16"),
        ("NY", "36"),
        ("OH", "39"),
        ("PA", "42"),
        ("RI", "44"),
        ("VT", "50"),
        ("VA", "51"),
        ("WV", "54"),
        ("WI", "55"),
        ("AS", "60"),
        ("GU", "66"),
        ("MP", "69"),
        ("VI", "78"),
    ],
)
def test_fips_spot_checks(usps, fips):
    assert geo.by_usps(usps).fips == fips
    assert geo.by_fips(fips).usps == usps


def test_by_fips_accepts_int_and_unpadded():
    assert geo.by_fips(6).usps == "CA"
    assert geo.by_fips("6").usps == "CA"
    assert geo.by_fips("06").usps == "CA"
    assert geo.by_fips(99) is None
    assert geo.by_fips("zz") is None


def test_by_usps_case_insensitive_and_missing():
    assert geo.by_usps("ca").name == "California"
    assert geo.by_usps(" Dc ").name == "District of Columbia"
    assert geo.by_usps("XX") is None


def test_by_name_case_and_whitespace_insensitive():
    assert geo.by_name("  new   YORK ").usps == "NY"
    assert geo.by_name("district of columbia").usps == "DC"
    assert geo.by_name("U.S. Virgin Islands").usps == "VI"
    assert geo.by_name("Atlantis") is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("CA", "CA"),
        ("ca", "CA"),
        ("California", "CA"),
        ("california ", "CA"),
        ("Calif.", "CA"),
        ("Mass.", "MA"),
        ("Penn.", "PA"),
        ("Wash.", "WA"),
        ("Commonwealth of Pennsylvania", "PA"),
        ("State of Texas", "TX"),
        ("Washington DC", "DC"),
        ("Washington, D.C.", "DC"),
        ("Washington D.C.", "DC"),
        ("District of Columbia", "DC"),
        ("D.C.", "DC"),
        ("DC", "DC"),
        ("Puerto Rico", "PR"),
        ("Guam", "GU"),
        ("U.S. Virgin Islands", "VI"),
        ("Northern Mariana Islands", "MP"),
        ("Washington", "WA"),
        ("washington", "WA"),
        ("  Texas  ", "TX"),
    ],
)
def test_normalize_state_accepts(value, expected):
    assert geo.normalize_state(value) == expected


@pytest.mark.parametrize(
    "value",
    [None, "", "   ", "Atlantis", "Denver, CO", "CA1", "Ontario", "Fort Carson, Colorado", "XX"],
)
def test_normalize_state_returns_none_when_unknown(value):
    assert geo.normalize_state(value) is None


def test_groupings():
    assert len(geo.CONTIGUOUS) == 49
    assert "DC" in geo.CONTIGUOUS
    assert not {"AK", "HI", "PR"} & set(geo.CONTIGUOUS)
    assert len(geo.ALL_STATES_AND_DC) == 51
    assert len(set(geo.ALL_STATES_AND_DC)) == 51
    assert len(geo.US_SUBDIVISIONS) == 56
    assert set(geo.ALL_STATES_AND_DC) <= set(geo.US_SUBDIVISIONS)
    assert set(geo.CONTIGUOUS) <= set(geo.ALL_STATES_AND_DC)


def test_tile_grid_covers_50_states_and_dc():
    assert len(geo.TILE_GRID) == 51
    assert set(geo.TILE_GRID) == set(geo.ALL_STATES_AND_DC)


def test_tile_grid_cells_unique_and_in_bounds():
    cells = list(geo.TILE_GRID.values())
    assert len(set(cells)) == len(cells)
    for row, col in cells:
        assert 0 <= row < 8
        assert 0 <= col < 12


def test_tile_grid_corners():
    assert geo.TILE_GRID["AK"] == (0, 0)
    assert geo.TILE_GRID["ME"][0] == 0 and geo.TILE_GRID["ME"][1] == 11
    assert geo.TILE_GRID["HI"][0] == 7 and geo.TILE_GRID["HI"][1] == 0
    assert geo.TILE_GRID["FL"][0] == 7


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Denver, CO", ("Denver", "CO")),
        ("Denver, Colorado", ("Denver", "CO")),
        ("Fort Carson, Colorado", ("Fort Carson", "CO")),
        ("Washington, DC", ("Washington", "DC")),
        ("Washington, D.C.", ("Washington", "DC")),
        ("  San   Jose ,  ca ", ("San Jose", "CA")),
        ("Remote, Atlantis", ("Remote, Atlantis", None)),
        ("Denver", ("Denver", None)),
        ("Washington", ("Washington", None)),
        ("Denver, XX", ("Denver, XX", None)),
        (", CO", (", CO", None)),
    ],
)
def test_parse_city_state(raw, expected):
    assert geo.parse_city_state(raw) == expected
