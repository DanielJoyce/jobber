"""Generate src/jobhunter/sources/registry.yaml (specs/003 "The registry", specs/010, specs/011).

Inputs: specs/data/robots-survey-2026-10-09.json (one entry per host surveyed) and the
"The table" section of specs/010-source-inventory.md (job bank names).

Re-runnable: `uv run python scripts/gen_registry.py` overwrites the YAML. After the first
commit the YAML is hand-maintained; edit it directly rather than regenerating over edits.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SURVEY = ROOT / "specs" / "data" / "robots-survey-2026-10-09.json"
INVENTORY = ROOT / "specs" / "010-source-inventory.md"
OUT = ROOT / "src" / "jobhunter" / "sources" / "registry.yaml"

VERIFIED = "2026-10-09"

FAMILY = {
    "vos": "vos",
    "joblink": "joblink",
    "nlx": "nlx",
    "next/custom": "next",
    "sfdc": "sfdc",
    "wordpress": "wordpress",
    "unknown": "unknown",
}

# Keys whose host's first label is generic ("jobs", "secure") or equal to the state code.
KEY_SLUG = {
    "MO": "greathires",
    "NJ": "labor",
    "UT": "jobconnection",
    "NM": "dws",
    "SC": "scworks",
    "OR": "emp",
}

# Survey says the entry URL is stale; use the host the survey found serving the real board.
ENTRY_OVERRIDE = {
    "TN": "https://jobs4tnwfs.tn.gov/vosnet/Default.aspx",
    "WA": "https://worksource.my.site.com/worksourcewa/",
}

NAME_FIX = {"California' Caljobs": "California CalJOBS"}

SFDC_TIER = {"sfdc": "browser"}
CRAWL_DELAY_RPS = {"WV"}  # robots Crawl-delay: 10 -> one request per 10 s
API_STATES = {"WY"}  # tier api per the brief (JSON-backed Next.js app)

USAJOBS = {
    "key": "us-usajobs",
    "state": None,
    "class": "C",
    "name": "USAJOBS",
    "family": "usajobs",
    "tier": "api",
    "entry": "https://data.usajobs.gov/api/search",
    "verified": VERIFIED,
    "family_signals": ["documented public API (data.usajobs.gov), keyed"],
    "config": {
        "sanctioned_api": True,
        "auth": {"email_env": "USAJOBS_EMAIL", "key_env": "USAJOBS_API_KEY"},
        "results_per_page": 500,
        "date_posted_days": 7,
    },
    "rate_limit": {"rps": 2, "concurrency": 2, "jitter": False},
    "robots": {"status": "n/a", "checked": None, "note": "documented public API"},
    "policy": "enabled",
    "queries": "from_profile",
    "expect": {"min_jobs_per_week": 0},
}

HEADER = """\
# Source registry: one row per job bank (specs/003 "The registry").
#
# Generated once from specs/data/robots-survey-2026-10-09.json and the specs/010 table by
# scripts/gen_registry.py. The script is re-runnable, but this file is now hand-maintained:
# edit rows here directly.
#
# class: A = workforce job bank (state null = national aggregator), C = federal (USAJOBS).
# policy: enabled = robots allows search AND detail (the specs/010 open set);
#         blocked = robots disallows the host or search; manual = robots unknown or undeterminable.
# robots.status is the survey's robots class, lowercased. rate_limit defaults to 0.2 rps,
# concurrency 1, jitter on; WV honours Crawl-delay 10. expect.min_jobs_per_week is a
# placeholder until real runs are tuned.
"""


def _q(value: object) -> str:
    """Render a scalar as YAML. Strings go through JSON, which is valid YAML double-quoted."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    return json.dumps(value, ensure_ascii=False)


def _inline(mapping: dict[str, object]) -> str:
    return "{" + ", ".join(f"{k}: {_q(v)}" for k, v in mapping.items()) + "}"


def _list(items: list[str]) -> str:
    return "[" + ", ".join(_q(i) for i in items) + "]"


def _short_note(text: str, limit: int = 140) -> str:
    """First clause of a survey note, cut at a word boundary."""
    if not text:
        return ""
    first = re.split(r"(?<=\.)\s|;\s", text, maxsplit=1)[0]
    if len(first) <= limit:
        return first
    return first[:limit].rsplit(" ", 1)[0]


def _names() -> dict[str, str]:
    """State code -> job bank name, from the 010 table (9 columns, two-letter ST)."""
    names: dict[str, str] = {}
    for line in INVENTORY.read_text(encoding="utf-8").splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) != 9 or not re.fullmatch(r"[A-Z]{2}", cells[0]):
            continue
        name = cells[2] or cells[1]
        names[cells[0]] = NAME_FIX.get(name, name)
    return names


def _policy(rec: dict[str, object]) -> str:
    robots = rec["robots_class"]
    search = rec["search_allowed"]
    detail = rec["detail_allowed"]
    if robots == "UNKNOWN":
        return "manual"
    if robots == "DISALLOW_ALL" or search is False:
        return "blocked"
    if search and detail:
        return "enabled"
    return "manual"


def _key(state: str, host: str) -> str:
    if state == "US":
        return "us-nlx"
    if state in KEY_SLUG:
        return f"{state.lower()}-{KEY_SLUG[state]}"
    slug = host.lower().removeprefix("www.").split(".")[0]
    return f"{state.lower()}-{slug}"


def _state_row(rec: dict[str, object], names: dict[str, str]) -> dict[str, object]:
    state = str(rec["state"])
    family = FAMILY[str(rec["family"])]
    survey_family = str(rec["family"])
    name = "National Labor Exchange (NLx)" if state == "US" else names[state]
    tier = "api" if state in API_STATES else SFDC_TIER.get(survey_family, "http")
    rps = 0.1 if state in CRAWL_DELAY_RPS else 0.2
    signals = [s.strip() for s in str(rec["family_signal"]).split(";") if s.strip()]
    return {
        "key": _key(state, str(rec["host"])),
        "state": None if state == "US" else state,
        "class": "A",
        "name": name,
        "family": family,
        "tier": tier,
        "entry": ENTRY_OVERRIDE.get(state, str(rec["url"])),
        "verified": VERIFIED,
        "family_signals": signals,
        "rate_limit": {"rps": rps, "concurrency": 1, "jitter": True},
        "robots": {
            "status": str(rec["robots_class"]).lower(),
            "checked": VERIFIED,
            "note": _short_note(str(rec.get("notes") or "")),
        },
        "policy": _policy(rec),
        "queries": "from_profile",
        "expect": {"min_jobs_per_week": 0},
        "_survey_state": state,
    }


def _render(row: dict[str, object]) -> str:
    row = dict(row)
    row.pop("_survey_state", None)
    lines: list[str] = []
    for field in ("key", "state", "class", "name", "family", "tier", "entry", "verified"):
        lines.append(f"  {field}: {_q(row[field])}")
    lines.append(f"  family_signals: {_list(row['family_signals'])}")
    if "config" in row:
        cfg = row["config"]
        lines.append("  config:")
        for k, v in cfg.items():
            if isinstance(v, dict):
                lines.append(f"    {k}: {_inline(v)}")
            else:
                lines.append(f"    {k}: {_q(v)}")
    lines.append(f"  rate_limit: {_inline(row['rate_limit'])}")
    lines.append(f"  robots: {_inline(row['robots'])}")
    lines.append(f"  policy: {_q(row['policy'])}")
    lines.append(f"  queries: {_q(row['queries'])}")
    lines.append(f"  expect: {_inline(row['expect'])}")
    head, *rest = lines
    return "\n".join([f"- {head[2:]}", *rest]) + "\n"


def build_rows() -> list[dict[str, object]]:
    names = _names()
    survey = json.loads(SURVEY.read_text(encoding="utf-8"))
    rows = [_state_row(rec, names) for rec in survey]
    rows.append(dict(USAJOBS))
    keys = [r["key"] for r in rows]
    dupes = sorted({k for k in keys if keys.count(k) > 1})
    if dupes:
        raise SystemExit(f"duplicate registry keys: {dupes}")
    return rows


def main() -> None:
    rows = build_rows()
    body = "\n".join(_render(r) for r in rows)
    OUT.write_text(HEADER + "\n" + body, encoding="utf-8")
    print(f"wrote {len(rows)} rows to {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
