"""Measure how much of a robots-blocked state's job market national NLx carries (specs/009 M3).

For each state, runs ONE broad query against the national usnlx.com search, filtered to that
state with the site's own location slug, and records the result count the API reports. Goes
through FetchContext, so robots.txt, the 0.2 rps per-host limit and the fetch log all apply;
one request per state, at least 5 s apart.

    uv run python scripts/nlx_coverage.py                      # every blocked state
    uv run python scripts/nlx_coverage.py --states TX,CA,FL    # a subset
    uv run python scripts/nlx_coverage.py --query "software engineer" --out /tmp/nlx

Writes ``<out>.csv`` and ``<out>.md``. The count is what NLx holds for the query, not a
measure of the state board's own inventory: the blocked boards cannot be searched to compare.
"""

from __future__ import annotations

import argparse
import csv
import sys
import tempfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from jobhunter.config import load_settings
from jobhunter.core import db
from jobhunter.core.fetch import FetchContext
from jobhunter.core.geo import by_usps
from jobhunter.core.models import Policy, Query, RateLimit
from jobhunter.sources.adapters.nlx import SEARCH_API, build_params, site_origin
from jobhunter.sources.registry import load_registry

MIN_INTERVAL_S = 5.0


def blocked_states() -> list[str]:
    return sorted(
        {r.state for r in load_registry() if r.policy is Policy.blocked and r.state is not None}
    )


def measure(states: list[str], query: str) -> list[dict[str, object]]:
    national = next(r for r in load_registry() if r.key == "us-nlx")
    # Never faster than one request per 5 s, whatever the registry row says.
    rps = min(national.rate_limit.rps if national.rate_limit else 0.2, 1 / MIN_INTERVAL_S)
    national = national.model_copy(update={"rate_limit": RateLimit(rps=rps, jitter=True)})
    rows: list[dict[str, object]] = []
    with tempfile.TemporaryDirectory() as tmp:
        settings = load_settings(overrides={"paths": {"cache_dir": str(Path(tmp) / "cache")}})
        conn = db.connect(":memory:")
        db.migrate(conn)
        with FetchContext(national, settings, conn) as ctx:
            for st in states:
                state = by_usps(st)
                if state is None:
                    print(f"skipping unknown state {st!r}", file=sys.stderr)
                    continue
                src = national.model_copy(update={"state": st})
                params = build_params(src, Query(title=query), 0, 15)
                body = ctx.get(
                    SEARCH_API,
                    params=params,
                    headers={"Accept": "application/json", "X-Origin": site_origin(national)},
                ).json()
                jobs = body.get("jobs") or []
                in_state = Counter(j.get("state_short") for j in jobs)[st]
                newest = max((j.get("date_new") or "" for j in jobs), default="")
                total = int((body.get("pagination") or {}).get("total") or 0)
                rows.append(
                    {
                        "state": st,
                        "location": params.get("location", ""),
                        "query": params["q"],
                        "results": total,
                        "first_page_in_state": f"{in_state}/{len(jobs)}",
                        "newest_posted": newest[:10],
                    }
                )
                print(f"{st}: {total} results ({in_state}/{len(jobs)} on page 1 in state)")
        conn.close()
    return rows


def write(rows: list[dict[str, object]], out: Path, query: str) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    fields = ["state", "location", "query", "results", "first_page_in_state", "newest_posted"]
    with out.with_suffix(".csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    lines = [
        f"NLx coverage, query {query!r}, measured {datetime.now(UTC):%Y-%m-%d %H:%M} UTC",
        "",
        "| ST | Results | Page 1 in state | Newest posted |",
        "|---|---:|---|---|",
    ]
    lines += [
        f"| {r['state']} | {r['results']:,} | {r['first_page_in_state']} | {r['newest_posted']} |"
        for r in rows
    ]
    out.with_suffix(".md").write_text("\n".join(lines) + "\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--states", help="comma-separated USPS codes (default: every blocked state)")
    ap.add_argument("--query", default="software engineer", help="one broad title query")
    ap.add_argument("--out", type=Path, default=Path("nlx-coverage"), help="output path stem")
    args = ap.parse_args(argv)
    states = (
        [s.strip().upper() for s in args.states.split(",") if s.strip()]
        if args.states
        else blocked_states()
    )
    rows = measure(states, args.query)
    write(rows, args.out, args.query)
    print(f"wrote {args.out.with_suffix('.csv')} and {args.out.with_suffix('.md')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
