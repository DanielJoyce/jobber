# 011 — Federal jobs & geographic blending

Federal postings are added as **source class C** and surfaced inside the same state views as
everything else. A GS-2210 job in Denver appears in your Colorado list next to Connecting
Colorado postings, labeled federal.

## USAJOBS is the best source in the system

Unlike all 54 state job banks, USAJOBS has a real, documented, officially sanctioned API. No
scraping, no sessions, no `robots.txt` question, no HTML parsing, no `__VIEWSTATE`.

**Verified 2026-10-09:**

- `GET https://data.usajobs.gov/api/search` returns **HTTP 401** without credentials — the
  endpoint is live and requires a key.
- A free key is self-issued at `developer.usajobs.gov/APIRequest`. Auth is three headers:
  `Host: data.usajobs.gov`, `User-Agent: <your registered email>`,
  `Authorization-Key: <key>`.
- `ResultsPerPage` accepts **up to 500**, and `DatePosted` filters to **0–60 days** — both
  ideal for the incremental watermark model in [004](004-ingest-pipeline.md#time-windows-and-watermarks).

### Fields we use

Confirmed against the official Search API reference:

| Our field | USAJOBS path |
|---|---|
| `external_id` | `MatchedObjectId` |
| `title` | `MatchedObjectDescriptor.PositionTitle` |
| `url` | `…PositionURI` |
| `employer` | `…OrganizationName` / `DepartmentName` |
| `posted_at` | `…PublicationStartDate` |
| `closes_at` | `…ApplicationCloseDate` |
| `salary_min/max`, `salary_period` | `…PositionRemuneration[].MinimumRange` / `.MaximumRange` / `.RateIntervalCode` |
| `grade_low/high`, `pay_plan` | `…JobGrade[].Code`, `…UserArea.Details.LowGrade` / `.HighGrade` |
| `occupation_code` | `…JobCategory[].Code` (occupational series, e.g. `2210`) |
| **`locations[]`** | **`…PositionLocation[]`** → `CityName`, `CountrySubDivisionCode` (state), `CountryCode`, `Latitude`, `Longitude` |
| `description_text` | concatenation of `…UserArea.Details.JobSummary`, `.MajorDuties`, `.QualificationSummary`, `.Requirements`, `.Education` |

**`needs_resolve = false`.** The search response already carries the full description in
`UserArea.Details`, so federal jobs skip the `resolve` stage entirely — no second request per
job. This is the only source in the system with that property.

Two structured signals that no state board provides, and both are strong fit inputs:
**occupational series** (`2210` = IT management, `0343` = management analyst) and **GS grade
range**, which maps to seniority far more reliably than a job title does.

### Registry row

```yaml
- key: us-usajobs
  state: null                     # federal: location comes from the posting, not the source
  class: C
  name: USAJOBS
  family: usajobs
  tier: api
  entry: https://data.usajobs.gov/api/search
  verified: 2026-10-09
  config:
    auth: {email_env: USAJOBS_EMAIL, key_env: USAJOBS_API_KEY}
    results_per_page: 500
    date_posted_days: 7
  queries: from_profile           # Keyword / PositionTitle / JobCategoryCode / PayGradeLow
  rate_limit: {rps: 2, concurrency: 2}   # a real API; far more generous than a scrape
  robots: {status: n/a, note: "documented public API"}
  policy: enabled
  expect: {min_jobs_per_week: 500}
```

Queries map cleanly onto your profile: keywords → `Keyword`, target titles → `PositionTitle`,
occupational series → `JobCategoryCode`, salary floor → `RemunerationMinimumAmount`, seniority →
`PayGradeLow`/`PayGradeHigh`.

## The blending problem

One federal posting is frequently open in **many locations at once** — a single announcement
listing 14 cities across 9 states. Some are nationwide. Some say "Location Negotiable". Some are
remote. `PositionLocation` is an **array**, and that breaks the one-job-one-state assumption
that state boards let you get away with.

Getting this wrong produces one of two bad outcomes: a 14-location job appears 14 times in your
inbox, or it appears only under the first state and is invisible in the other 13.

### The fix: jobs and locations are many-to-many

A `job_locations` table, not a `state` column on `job` ([005](005-data-model.md)):

```sql
CREATE TABLE job_locations (
  job_id        INTEGER NOT NULL REFERENCES job(id),
  state         TEXT,            -- 2-letter; NULL for overseas/unspecified
  city          TEXT,
  county        TEXT,
  lat           REAL,
  lon           REAL,
  is_primary    INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (job_id, state, city)
);
CREATE INDEX job_locations_state ON job_locations(state);
```

Every job gets at least one row. State-scoped sources get exactly one (from the posting, with
the source's own state as fallback). USAJOBS gets one per `PositionLocation` entry, deduplicated
by `(state, city)`.

And a scope classification on `job`, because "this job is in 9 states" and "this job is anywhere
in the US" are different facts and must not be conflated:

```
location_scope := single | multi_state | nationwide | remote_us | negotiable | overseas | unknown
```

Derived from the location array plus text signals in the posting
(`Location Negotiable`, `Many vacancies in multiple locations`, telework/remote fields).
`nationwide`, `remote_us`, and `negotiable` get a synthetic `job_locations` row with
`state = NULL` **in addition to** their enumerated rows, so a "remote, anywhere" job is not
accidentally confined to the one city the agency happens to list.

### Display rules

| Context | Rule |
|---|---|
| **Per-state view** (`/state/CO`) | Join through `job_locations`. A 9-state job appears in all 9 state views — correctly, because it genuinely is available in all 9 |
| **Global inbox** | One row per job. Shows `Denver, CO +13 more`, expandable. Never duplicated |
| **Dedupe / scoring** | Operate on the job, never the location. A 14-location posting is **one** LLM call, not 14 — see the cost note below |
| **Remote jobs** | Surface in every state in your allowed set, badged `remote`, and sorted above equally-scored onsite jobs if your profile prefers remote |
| **Source badge** | Every row shows `federal` / `state` / `state-employer`, so you always know which market you are looking at |

**Cost note:** scoring per-job rather than per-location is not a minor optimization. Federal
postings average several locations and nationwide announcements can list dozens; scoring by
location would multiply the federal LLM bill severalfold for zero added information, since the
description is identical across locations.

### Location fit scoring

Because location is a set, not a value, the prefilter and the LLM both take the set:

- **Prefilter** keeps a job if **any** of its locations is in your allowed states, or if
  `location_scope` is `remote_us` / `nationwide` / `negotiable`. Rejecting a multi-state job
  because its *first* listed location is wrong would be a serious recall bug.
- `location_fit` is **computed in Python**, not by the model, from the location set,
  `location_scope`, your `state_ranking`, `states_excluded` and `remote_bonus`. A job open in both
  your first-choice and fifth-choice states scores on the first choice. Because it's computed,
  re-ranking states on the preferences page re-sorts everything instantly at no cost
  ([014](014-preferences-console.md)).
- `negotiable` is scored as a **mild positive**, since it means the constraint is soft.

### Normalization details

- `CountrySubDivisionCode` is usually a 2-letter state code but occasionally a full name —
  normalize through a state table, and record a `parse_warning` rather than guessing on a miss.
- Non-US locations (`CountryCode != "United States"`) get `state = NULL`,
  `location_scope = overseas`, and are excluded by the prefilter unless your profile opts in.
- Military installations (`LocationName` like `Fort Carson, Colorado`) carry a city that is not
  a municipality. Keep `LocationName` verbatim in `city` and do not try to resolve it; the
  state code is the part that matters for filtering.
- DC-area postings legitimately span DC, VA, and MD. They are genuinely multi-state; let the
  data say so rather than collapsing to one.

## Should state-employer (Class B) jobs blend too?

Yes, by the same mechanism — a state-employee job in Sacramento is a California job. Class B
needs no special handling once `job_locations` exists, which is the main reason to build the
many-to-many table now rather than retrofit it later.
