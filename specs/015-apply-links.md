# 015 — Direct apply links

Every job detail page has an **Apply** button that goes straight to the employer's actual
application, after following redirects, tracking wrappers and aggregator hops. The page also
shows where the button leads, so you can see it before clicking.

The button opens the application. **It never fills it in or submits it**
([001](001-goals-and-scope.md#non-goals)).

## The problem

The URL a job board gives you is rarely the application. Typical chains:

```
NLx listing ─→ click.appcast.io/track/… ─→ acme.wd5.myworkdayjobs.com/…/job/R-1234 ─→ …/apply
USAJOBS PositionURI ─→ (ApplyURI) ─→ apply.usastaffing.gov/…
State bank detail ─→ "Apply on employer site" ─→ boards.greenhouse.io/acme/jobs/567#app
```

Each hop is a chance to land on an expired posting, a login wall, or a spammy reposting site.
Resolving the chain ahead of time catches those before you spend time on a dead job.

## Resolution

New pipeline step after `resolve` ([004](004-ingest-pipeline.md)), implemented in
`pipeline/applylink.py`:

1. **Start from the best source-provided link.** USAJOBS supplies one directly: its search
   response includes an `ApplyURI` field, so federal jobs usually resolve with no extra
   requests. For other sources, the adapter's `resolve()` extracts the "Apply" link from the
   detail page it has already fetched.
2. **Unwrap known redirectors without fetching.** Many tracking links carry the destination in a
   query parameter (`url=`, `dest=`, `target=`, `u=`). A table of known redirector hosts and their
   parameter names unwraps them locally, which needs no request.
3. **Follow the rest.** HTTP 3xx via `Location`, `<meta http-equiv="refresh">`, and simple
   `window.location = "…"` assignments in an inline script. At most 8 hops, with loop detection.
4. **Stop at a known applicant tracking system** and canonicalize to its apply URL. Rules per
   ATS, each tested against fixtures:

| ATS | Recognized by | Canonical apply URL |
|---|---|---|
| Workday | `*.myworkdayjobs.com` | posting URL + `/apply` |
| Greenhouse | `boards.greenhouse.io`, `job-boards.greenhouse.io` | posting URL + `#app` |
| Lever | `jobs.lever.co` | posting URL + `/apply` |
| Ashby | `jobs.ashbyhq.com` | posting URL + `/application` |
| iCIMS | `*.icims.com` | posting URL (apply is on-page) |
| SmartRecruiters | `jobs.smartrecruiters.com` | posting URL (apply is on-page) |
| Oracle Taleo | `*.taleo.net` | posting URL |
| NEOGOV | `governmentjobs.com` | posting URL + `/apply` |
| USA Staffing | `apply.usastaffing.gov` | as given by `ApplyURI` |

   Unknown destinations are kept as the final resolved URL with `ats = 'unknown'`. The table
   grows as new systems show up, and `jobhunter applylinks unknown` lists the most common
   unknown domains to add next.
5. **JavaScript-only redirects** fall back to a Playwright page load, bounded to a few seconds,
   and only for jobs in buckets A–C. Most chains never need it.

### Respecting robots on every hop

Every request goes through `FetchContext` ([003](003-sources-and-adapters.md#adapter-contract)),
so each hop's host gets its own `robots.txt` check and rate limit. If a hop is disallowed, the
resolver **stops there**. It records the last allowed URL and sets status `blocked`. The button
then opens that URL, and you click through the rest yourself in your browser, which is ordinary
browsing.

This covers email-only jobs from blocked boards ([012](012-email-ingest.md)): their links aren't
followed by script, so they show **Open posting** rather than a resolved **Apply**.

## Status shown on the button

| Status | Button | Meaning |
|---|---|---|
| `live` | **Apply on Workday ↗** (names the ATS) | Final page reached and it looks open |
| `expired` | **Posting closed**, disabled, with an "open anyway" link | Final page returned 404/410, or says the posting is closed |
| `unresolved` | **Open posting ↗** | Chain couldn't be completed: unknown JS, timeout, too many hops |
| `blocked` | **Open posting ↗** | A hop's host disallows automated access; finish in your browser |

Closed postings are detected by status code and by a small set of phrases on the final page
("no longer accepting applications", "this job has expired", "position has been filled"). A job
marked `expired` drops out of the inbox, and its application, if one exists, gets a note.

Below the button, the detail page shows the **chain**, so you can see the destination before
clicking:

```
[ Apply on Workday ↗ ]   acme.wd5.myworkdayjobs.com · verified 2h ago
  NLx → appcast (unwrapped) → Workday · Acme Corporation
```

An unexpected destination stands out. A posting from a state agency whose chain ends on an
unrelated reposting site is worth noticing before you hand over your information.

## Fresh at click time

Links go stale. The button points at a local endpoint, `/apply/{job_group_id}`, which:

1. Re-verifies the final URL if it was resolved more than 24 hours ago: one GET, following at
   most the last hop, through `FetchContext`.
2. Redirects your browser there, or shows a "posting has closed" page with the original link if
   it has.
3. Logs the click and moves the application to `preparing` if it's still at `interested`.

When you come back to the console, the job shows an **"Did you apply?"** prompt (yes / not yet /
not interested) that feeds the existing `a` flow ([007](007-console-and-tracking.md)), so the
tracker stays accurate without extra work.

## When resolution runs

Not for every job. Resolution costs requests, and most jobs are never looked at.

- **Eagerly** for jobs that land in buckets A–C, right after scoring.
- **On demand** when you open any other job's detail page. The page renders immediately with
  **Open posting** and swaps in the resolved button by HTMX when the resolver finishes.
- **Re-checked** nightly for every application still at `interested` or `preparing`, so a
  posting that closed while you were drafting a cover letter is flagged the next morning.

## Data model

```sql
CREATE TABLE apply_link (
  job_group_id  INTEGER PRIMARY KEY REFERENCES job_group(id),
  start_url     TEXT NOT NULL,
  final_url     TEXT,
  chain         TEXT NOT NULL,     -- JSON [{url, host, method: unwrap|3xx|meta|js|browser, status}]
  ats           TEXT,              -- workday | greenhouse | ... | unknown
  employer_host TEXT,
  status        TEXT NOT NULL,     -- live | expired | unresolved | blocked
  resolved_at   TEXT NOT NULL,
  verified_at   TEXT,
  error         TEXT
);

CREATE TABLE apply_click (
  id            INTEGER PRIMARY KEY,
  job_group_id  INTEGER NOT NULL REFERENCES job_group(id),
  at            TEXT NOT NULL,
  final_url     TEXT NOT NULL,
  outcome       TEXT              -- redirected | expired
);
```

Keyed on `job_group`: when the same opening is posted by several sources, the first chain to
reach `live` wins, and the button uses it.

## Milestone placement

The resolver and ATS table go in **M3**, alongside the open-board adapters, since that's where
the first real chains appear. USAJOBS `ApplyURI` support goes in **M2**. The button, chain
display, click endpoint and "Did you apply?" prompt go in **M5**.
