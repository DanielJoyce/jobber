"""US states, DC and territories, plus location helpers (specs/011, specs/013).

Pure data and functions. No I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Kind = Literal["state", "district", "territory"]


@dataclass(frozen=True)
class State:
    usps: str
    fips: str
    name: str
    kind: Kind


# 50 states, DC, and 5 territories. FIPS ids match the us-atlas TopoJSON.
STATES: tuple[State, ...] = (
    State("AL", "01", "Alabama", "state"),
    State("AK", "02", "Alaska", "state"),
    State("AZ", "04", "Arizona", "state"),
    State("AR", "05", "Arkansas", "state"),
    State("CA", "06", "California", "state"),
    State("CO", "08", "Colorado", "state"),
    State("CT", "09", "Connecticut", "state"),
    State("DE", "10", "Delaware", "state"),
    State("DC", "11", "District of Columbia", "district"),
    State("FL", "12", "Florida", "state"),
    State("GA", "13", "Georgia", "state"),
    State("HI", "15", "Hawaii", "state"),
    State("ID", "16", "Idaho", "state"),
    State("IL", "17", "Illinois", "state"),
    State("IN", "18", "Indiana", "state"),
    State("IA", "19", "Iowa", "state"),
    State("KS", "20", "Kansas", "state"),
    State("KY", "21", "Kentucky", "state"),
    State("LA", "22", "Louisiana", "state"),
    State("ME", "23", "Maine", "state"),
    State("MD", "24", "Maryland", "state"),
    State("MA", "25", "Massachusetts", "state"),
    State("MI", "26", "Michigan", "state"),
    State("MN", "27", "Minnesota", "state"),
    State("MS", "28", "Mississippi", "state"),
    State("MO", "29", "Missouri", "state"),
    State("MT", "30", "Montana", "state"),
    State("NE", "31", "Nebraska", "state"),
    State("NV", "32", "Nevada", "state"),
    State("NH", "33", "New Hampshire", "state"),
    State("NJ", "34", "New Jersey", "state"),
    State("NM", "35", "New Mexico", "state"),
    State("NY", "36", "New York", "state"),
    State("NC", "37", "North Carolina", "state"),
    State("ND", "38", "North Dakota", "state"),
    State("OH", "39", "Ohio", "state"),
    State("OK", "40", "Oklahoma", "state"),
    State("OR", "41", "Oregon", "state"),
    State("PA", "42", "Pennsylvania", "state"),
    State("RI", "44", "Rhode Island", "state"),
    State("SC", "45", "South Carolina", "state"),
    State("SD", "46", "South Dakota", "state"),
    State("TN", "47", "Tennessee", "state"),
    State("TX", "48", "Texas", "state"),
    State("UT", "49", "Utah", "state"),
    State("VT", "50", "Vermont", "state"),
    State("VA", "51", "Virginia", "state"),
    State("WA", "53", "Washington", "state"),
    State("WV", "54", "West Virginia", "state"),
    State("WI", "55", "Wisconsin", "state"),
    State("WY", "56", "Wyoming", "state"),
    State("AS", "60", "American Samoa", "territory"),
    State("GU", "66", "Guam", "territory"),
    State("MP", "69", "Northern Mariana Islands", "territory"),
    State("PR", "72", "Puerto Rico", "territory"),
    State("VI", "78", "U.S. Virgin Islands", "territory"),
)


def _key(text: str) -> str:
    """Canonical comparison key: casefolded, letters and digits only."""
    return "".join(ch for ch in text.casefold() if ch.isalnum())


_BY_USPS: dict[str, State] = {s.usps: s for s in STATES}
_BY_FIPS: dict[str, State] = {s.fips: s for s in STATES}
_BY_NAME: dict[str, State] = {_key(s.name): s for s in STATES}

# Keys are _key() forms. Only unambiguous abbreviations; never guessed.
_ALIASES: dict[str, str] = {
    "calif": "CA",
    "mass": "MA",
    "penn": "PA",
    "penna": "PA",
    "wash": "WA",
    "conn": "CT",
    "tenn": "TN",
    "mich": "MI",
    "minn": "MN",
    "miss": "MS",
    "ill": "IL",
    "ariz": "AZ",
    "ark": "AR",
    "colo": "CO",
    "del": "DE",
    "fla": "FL",
    "kans": "KS",
    "mont": "MT",
    "nebr": "NE",
    "neb": "NE",
    "nev": "NV",
    "okla": "OK",
    "ore": "OR",
    "oreg": "OR",
    "tex": "TX",
    "wis": "WI",
    "wisc": "WI",
    "wyo": "WY",
    "ala": "AL",
    "alas": "AK",
    "washingtondc": "DC",
    "districtofcolumbia": "DC",
    "virginislands": "VI",
    "usvirginislands": "VI",
    "usvi": "VI",
    "northernmarianaislands": "MP",
    "cnmi": "MP",
    "americansamoa": "AS",
}

_PREFIXES: tuple[str, ...] = ("the ", "state of ", "commonwealth of ", "territory of ")


def by_usps(code: str) -> State | None:
    """Look up by 2-letter USPS code, case-insensitive."""
    return _BY_USPS.get(code.strip().upper())


def by_fips(code: str | int) -> State | None:
    """Look up by FIPS id: "06", "6", or 6 all find California."""
    digits = str(code) if isinstance(code, int) else code.strip()
    if not digits.isdigit():
        return None
    return _BY_FIPS.get(f"{int(digits):02d}")


def by_name(name: str) -> State | None:
    """Look up by official name, case- and whitespace-insensitive."""
    return _BY_NAME.get(_key(name))


def normalize_state(value: str | None) -> str | None:
    """Map a state name, abbreviation or code to its USPS code, or None if unknown."""
    if not value:
        return None
    text = " ".join(value.split()).casefold()
    stripped = True
    while stripped:
        stripped = False
        for prefix in _PREFIXES:
            if text.startswith(prefix):
                text = text[len(prefix) :]
                stripped = True
    key = _key(text)
    if not key:
        return None
    if key in _ALIASES:
        return _ALIASES[key]
    if key in _BY_NAME:
        return _BY_NAME[key].usps
    if len(key) == 2 and key.upper() in _BY_USPS:
        return key.upper()
    return None


# Groupings, as USPS codes ordered by FIPS.
CONTIGUOUS: tuple[str, ...] = tuple(
    s.usps for s in STATES if s.usps not in {"AK", "HI", "AS", "GU", "MP", "PR", "VI"}
)
ALL_STATES_AND_DC: tuple[str, ...] = tuple(s.usps for s in STATES if s.kind != "territory")
US_SUBDIVISIONS: tuple[str, ...] = tuple(s.usps for s in STATES)

# Equal-square tile grid: usps -> (row, col), 8 rows x 12 cols.
# AK top-left, ME top-right, HI bottom-left, FL at the bottom.
TILE_GRID: dict[str, tuple[int, int]] = {
    "AK": (0, 0),
    "ME": (0, 11),
    "VT": (1, 10),
    "NH": (1, 11),
    "WA": (2, 1),
    "ID": (2, 2),
    "MT": (2, 3),
    "ND": (2, 4),
    "MN": (2, 5),
    "WI": (2, 6),
    "MI": (2, 7),
    "NY": (2, 9),
    "MA": (2, 10),
    "RI": (2, 11),
    "OR": (3, 1),
    "NV": (3, 2),
    "WY": (3, 3),
    "SD": (3, 4),
    "IA": (3, 5),
    "IL": (3, 6),
    "IN": (3, 7),
    "OH": (3, 8),
    "PA": (3, 9),
    "NJ": (3, 10),
    "CT": (3, 11),
    "CA": (4, 1),
    "UT": (4, 2),
    "CO": (4, 3),
    "NE": (4, 4),
    "MO": (4, 5),
    "KY": (4, 6),
    "WV": (4, 7),
    "VA": (4, 8),
    "MD": (4, 9),
    "DE": (4, 10),
    "AZ": (5, 2),
    "NM": (5, 3),
    "KS": (5, 4),
    "AR": (5, 5),
    "TN": (5, 6),
    "NC": (5, 7),
    "SC": (5, 8),
    "DC": (5, 9),
    "TX": (6, 3),
    "OK": (6, 4),
    "LA": (6, 5),
    "MS": (6, 6),
    "AL": (6, 7),
    "GA": (6, 8),
    "HI": (7, 0),
    "FL": (7, 8),
}


def parse_city_state(raw: str) -> tuple[str, str | None]:
    """Split "City, ST" into (city, usps). Returns (raw, None) when it cannot be resolved.

    Splits on the last comma. The city is returned as written, so "Fort Carson, Colorado"
    gives ("Fort Carson", "CO"). A bare "Washington" has no comma, so it is not resolved.
    """
    text = " ".join(raw.split())
    if "," not in text:
        return raw, None
    city, _, state_part = text.rpartition(",")
    city = city.strip()
    code = normalize_state(state_part)
    if not city or code is None:
        return raw, None
    return city, code


# Territories sit below the state grid on /prefs (row 8). Kept out of TILE_GRID because the
# dashboard grid map lays territories out on its own.
TERRITORY_TILES: dict[str, tuple[int, int]] = {
    "PR": (8, 0),
    "GU": (8, 1),
    "VI": (8, 2),
    "MP": (8, 3),
    "AS": (8, 4),
}
