# 003 — Sources & adapters

Your CareerOneStop link settled the inventory problem. This spec is written against **54
verified source URLs** (50 states + DC, Guam, Northern Mariana Islands, US Virgin Islands)
scraped from [CareerOneStop's state job banks page](https://www.careeronestop.org/JobSearch/FindJobs/state-job-banks.aspx)
on 2026-10-09. The full table is in [010](010-source-inventory.md).

More importantly, probing those 54 URLs proved the "families, not states" bet: **three platform
families cover at least 35 of the 54 sources.**

## What kind of board these are — read this first

The CareerOneStop list is **state workforce-agency job banks** (the public labor exchange):
WorkInTexas, CalJOBS, Employ Florida, KansasWorks. They carry **private and public sector**
postings for that state, in enormous volume — these are the state-run equivalents of Indeed,
not the state's own HR system.

That is almost certainly what you want, and it is what this spec builds. But it is a different
thing from **state-government-employee career sites** (CalCareers, careers.wa.gov), which carry
only jobs working *for* the state. Two source classes:

| Class | What it is | Count | Volume | Status |
|---|---|---|---|---|
| **A — workforce job banks** | Public labor exchange, all employers | 54, URLs verified | Very high | **Primary.** Built first |
| **B — state employer career sites** | Jobs working for the state government itself | ~50, not yet inventoried | Low | **Secondary.** Same adapter layer, later milestone |
| **C — USAJOBS (federal)** | Federal positions, **blended into states by location** | 1 source, real API | High | **Primary.** Cheapest source in the system — see [011](011-federal-and-geography.md) |

Class C is one API, documented and keyed, covering every federal agency nationwide. It is the
highest-quality data in the whole system and the least work to integrate — see
[011](011-federal-and-geography.md) for the API and for how its multi-location postings get
blended into per-state views.

Class B is still worth having — state employment is a distinct, slower, pension-bearing market
that Class A boards cover inconsistently — but it is a later milestone.

> **Read [008](008-compliance.md#verified-robotstxt-survey-2026-10-09) before relying on the
> family economics below.** Most VOS hosts serve `Disallow: /` and every JobLink host disallows
> `/search/jobs`. The families are real and the adapter analysis stands, but in
> robots-respecting mode most of their members are reachable only through email alerts.

**The volume consequence is the single biggest design driver.** A Class A sweep could surface
hundreds of thousands of new postings a week nationally. Resolving and scoring all of that is
neither affordable nor useful, which forces the decision in the next section.

## Query-driven ingestion, not full-crawl

**We do not crawl these boards exhaustively. We run your searches against them.**

Every one of these sites has a search interface with filters for occupation, keyword, location,
radius, salary, and posting date. The registry therefore carries a `queries` block derived from
your profile:

```yaml
queries:
  - {keywords: "site reliability engineer", radius_miles: null, remote: true}
  - {keywords: "platform engineer", posted_within_days: 7}
  - {onet_soc: "15-1244.00"}      # Network/Systems Admin — where the board supports O*NET
```

This is the right call on four independent grounds:

1. **Cost** — turns ~500,000 stubs/week into ~5,000. That is the difference between a $400/week
   LLM bill and a $9/week one ([006](006-fit-scoring.md#cost)).
2. **Politeness** — a few dozen targeted queries per state per night instead of walking
   hundreds of thousands of result pages. See [008](008-compliance.md).
3. **Relevance** — these boards index the whole labor market, including tens of thousands of
   jobs no amount of LLM scoring would make interesting to you.
4. **Robustness** — a search result page is a stable, supported interface. A full-site crawl is
   not, and breaks constantly.

The cost is **recall**: a job whose title uses vocabulary none of your queries contain is
invisible. Three mitigations, all specified:

- Queries are deliberately **broad and numerous** (15–40 per profile), covering synonyms and
  adjacent titles, not just your exact job title. Cheap to add.
- O*NET/SOC occupation codes where supported, which catch title variants by classification
  rather than by string.
- A **monthly wide sweep** on 2–3 states with a near-empty query, scored at the prefilter level
  only, to measure what the narrow queries miss. If a wide sweep surfaces good jobs the queries
  missed, the queries were wrong — and now you know, measurably.

Queries live in `profile/preferences.yaml` so one edit changes all 54 sources.

## Platform families — verified

Probed 2026-10-09. "Verified" means I observed the signal live.

### 1. Geographic Solutions "Virtual OneStop" (VOS) — 25 sources

The dominant platform. ASP.NET WebForms; `/vosnet/` in the path, `__VIEWSTATE` in the body,
literal `Geographic Solutions` and `Virtual OneStop` strings.

**Verified:** 24 sources carry `/vosnet/` in the CareerOneStop URL outright; Connecticut
(`cthires.com`) 302-redirects to `/vosnet/default.aspx` and returned all five signals.

> AL, AK, CA, CT, DC, FL, GA, GU, HI, IN, IA, LA, MD, NE, NV, NH, NC, ND, RI, SC, SD, TN, TX, VI, VA

Adapter notes: WebForms means stateful postbacks — `__VIEWSTATE` and `__EVENTVALIDATION` must
be round-tripped, and search is a POST against a cookie session. This is the fiddliest adapter
to write and by far the highest-value one. Get a guest session, hold the cookie jar, post the
search form, paginate via the postback event target. **One adapter, 25 sources.**

### 2. Shared-codebase "JobLink" family (probably AJLA-TS) — 8 sources

Ruby on Rails with Webpacker (`/assets/application-<digest>.js`, `/packs/js/<chunk>-<hash>.js`).

**Verified conclusively:** the file `/packs/js/4743-860615d0fe49e3040bc6.js` fetched from all
seven hosts returned the **byte-identical sha256 (`2c0d5d29e49fd986…`)**, as did
`translate_config-d06f51c2…js`. Same application, seven state deployments — not merely a
similar vendor.

> AZ (azjobconnection.gov), AR (arjoblink.arkansas.gov), DE (joblink.delaware.gov),
> ID (idahoworks.gov), IL (illinoisjoblink.illinois.gov), KS (kansasworks.com),
> ME (joblink.maine.gov), and VT (vermontjoblink.com), confirmed by the full survey

Adapter notes: **a JSON API exists.** `GET /ada/r/search/jobs` returned **HTTP 401 with a
26-byte body** — authentication required, not "no such route". `/search/jobs` returned 202.
**But all 8 hosts disallow `/search/jobs` in `robots.txt`**
([008](008-compliance.md#verified-robotstxt-survey-2026-10-09)), so under the robots decision
this family is collected through email alerts, not search. Detail pages are *not* disallowed,
so jobs arriving by email can still be resolved to full descriptions
([012](012-email-ingest.md#the-partial-description-problem)). Vendor attribution to America's Job Link
Alliance is probable from the "JobLink" branding, not verified; the shared-codebase fact is what
the adapter depends on, and that is certain.

### 3. NLx / National Labor Exchange — 3 sources

`usnlx.com` domains and `usnlx` strings. The NLx is the NASWA/DirectEmployers national
aggregator that many state banks syndicate through.

**Verified:** KY (`kyjobs.usnlx.com`), NY (`newyork.usnlx.com`), MT (`montanaworks.gov`, which
serves `usnlx` strings from its own domain).

Adapter notes: the **national** site `usnlx.com` is open: `robots.txt` is `Allow: /` with only
feed paths disallowed, and it publishes a sitemap (checked 2026-10-09). Because NLx aggregates
across states, it may cover jobs from boards that block crawling. That would make it the single
most valuable open source, but treat it skeptically until measured: aggregator coverage is
usually partial and lagging. Measuring it is part of M3 ([009](009-roadmap.md)).

### 4. Next.js custom — 1+ sources

**Verified:** WY (`hire.wyo.gov`) ships `__NEXT_DATA__` and references `/api/workflow`. Next.js
means a JSON API under `/api/` and server-side props embedded in the page — an `api`-tier source
once the route is found.

### 5. Unclassified — 18 sources

MA, MI, MN, MS, MO, NJ, NM, OH, OK, OR, PA, UT, VT, WA, WV, WI, MP, plus any reclassification.
Entry URLs are known and verified reachable; the family is not yet determined. Classifying these
is Milestone 1 work, and the expectation is that most fall into families 1–3 once probed with a
session rather than a bare GET.

### Coverage summary

| | Sources | Share |
|---|---|---|
| Families 1–4 (adapter identified) | 36 | **67%** |
| USAJOBS federal API (Class C) | 1 | — |
| Unclassified (URL verified, family TBD) | 18 | 33% |
| **Code adapters needed for the 36** | **4** | |

That ratio — 4 adapters for 36 sources — is the whole argument for this architecture.

## Access tiers

Always prefer the lowest tier that works.

| Tier | Mechanism | Relative cost | Use when |
|---|---|---|---|
| `api` | JSON/XML endpoint | 1x | A real endpoint exists (JobLink `/ada/r/`, Next.js `/api/`) |
| `feed` | RSS/Atom, sitemap, job-alert email | 1x | A sanctioned feed exists |
| `http` | `httpx` + HTML parse, session + cookies | 2x | Server-rendered results (VOS) |
| `browser` | Playwright Chromium | **~50–200x** | Content only exists after JS runs |
| `manual` | Human drops a CSV into `data/inbox/` | — | Board defeats automation; still fully resolved and scored |

The `manual` tier is the answer to *"some offer csv export but doesn't contain the job
description."* A CSV supplies the stub set; the `resolve` stage
([004](004-ingest-pipeline.md#3-resolve--the-csv-problem)) fetches the descriptions separately.
A hostile board degrades to "export a CSV weekly" rather than going dark, and the records it
produces are just as complete as scraped ones.

## The registry

`src/jobhunter/sources/registry.yaml`, generated from [010](010-source-inventory.md). One row
per source.

```yaml
- key: tx-workintexas
  state: TX
  class: A                                  # A = job bank, B = state employer
  name: WorkInTexas
  family: vos                               # -> adapter
  tier: http
  entry: https://www.workintexas.com/vosnet/Default.aspx
  verified: 2026-10-09                      # URL reachability
  family_signals: ["/vosnet/", "__VIEWSTATE", "Geographic Solutions"]
  config:
    session_entry: /vosnet/Default.aspx
    search_path: /vosnet/joblist/joblist.aspx
  queries: from_profile                     # or an override list
  pagination: {kind: postback, max_pages: 25}
  rate_limit: {rps: 0.2, concurrency: 1, jitter: true}
  robots: {status: unknown, checked: null}
  policy: enabled                           # enabled | blocked | manual | disabled
  expect: {min_jobs_per_week: 100}
```

`policy: blocked` makes the fetcher refuse the source outright. Nothing is fetched against a
`robots.txt` prohibition unless you change that field yourself ([008](008-compliance.md)).

## Adapter contract

Two methods. Everything hard — caching, rate limiting, robots, retries, cookie sessions,
browser lifecycle — lives in `FetchContext`, so an adapter is parsing logic and nothing else.

```python
# sources/adapters/base.py
from typing import Protocol, Iterator
from datetime import datetime
from jobhunter.core.models import JobStub, JobDetail, SourceRow, Query
from jobhunter.core.fetch import FetchContext

class SourceAdapter(Protocol):
    family: str

    def search(
        self, src: SourceRow, query: Query, since: datetime, ctx: FetchContext
    ) -> Iterator[JobStub]:
        """Run one query against one source. Yield stubs newest-first.

        Must be a generator: the pipeline stops consuming at the watermark, so
        a well-written adapter never fetches page 25 of a result set whose
        page 2 already predates `since`.
        """

    def resolve(self, stub: JobStub, ctx: FetchContext) -> JobDetail:
        """Fetch and parse the full posting. Called only when needed."""
```

`FetchContext` is the only thing that touches the network:

```python
class FetchContext:
    def get(self, url, *, params=None, ttl=None) -> CachedResponse: ...
    def post(self, url, *, data=None, json=None) -> CachedResponse: ...
    def session(self) -> httpx.Client: ...          # per-source cookie jar, warmed
    def page(self) -> "playwright.sync_api.Page": ...  # lazy; raises if tier != browser
    def policy(self) -> SourcePolicy: ...
```

A test asserts no adapter module imports `httpx` or `playwright` directly. An adapter that
bypasses `ctx` has escaped the rate limiter and the cache, and that is a bug.

### Session handling

Both dominant families need a session, so this belongs in the shared layer, not in adapters:
`FetchContext.session()` returns a per-source `httpx.Client` with a cookie jar, warmed by
GETting the source's `session_entry` once per run and extracting any CSRF/`__VIEWSTATE` tokens
into `ctx.tokens`. Sessions are reused across all queries for that source and re-warmed on a
401/403/session-expiry signal.

These are **guest/anonymous sessions only.** No account creation, no login, no credentials.
See [008](008-compliance.md).

## The `htmlconfig` adapter

To keep bespoke per-state code near zero, one adapter is driven entirely by registry selectors:

```yaml
family: htmlconfig
config:
  search:
    method: GET
    path: /jobs/search
    params: {q: "{keywords}", posted: "{posted_within_days}"}
  list:
    rows: "div.job-result"
    title: "h3 a::text"
    url: "h3 a::attr(href)"
    posted_at: "span.posted::text"
    employer: "span.employer::text"
    location: "span.location::text"
    salary_raw: "span.salary::text"
  detail:
    description: "div#job-description"
    closes_at: "span.closing::text"
```

Dates go through `dateparser`, salary through the shared parser. Most of the 18 unclassified
sources should land here or in a known family without a new Python file.

**Salary from description text** (`core/salary_text.py`, bug fec27be). Most boards (us-nlx,
wyoming, new york, kentucky, montana) have no structured pay field, yet the pay is in the
description ("$225,000 - $260,000 / yearly", "$70-$90/hr"), so postings showed "salary not
stated" and comp fit was dropped. Normalize now falls back to a precision-first extractor when
the structured field yields no numbers: it needs pay context, an explicit period or a USD tag,
sane magnitude per period, and rejects bonuses, funding, benefits and minimum-wage blurbs.
Several ranges (location tiers): the one near the job's location, else the widest min..max over
ranges sharing the first period. `job.salary_source` is `structured` or `text`; for `text`,
`salary_raw` holds the matched snippet and is re-derived on every normalize. Free backfill:
`jobhunter backfill-salary`.

**As built** (`sources/adapters/htmlconfig.py`, milestone M3). Beyond the sketch above the adapter
also handles: a JSON variant (`list.format: json`, `rows` as a dotted path, fields as dotted paths
or `template`/`re` specs, JSON POST bodies); WebForms boards (`search.form` GETs the page and POSTs
its hidden state, `pagination.kind: postback` re-posts the result form with a `__doPostBack`
target); `page`, `offset` and `next_link` paging; flat result lists (`row_siblings`); label/value
detail tables (`{label: "Closing Date"}`); `date_formats` and `timezone` for date text; and
`days_buckets` to round `posted_within_days` up to a select box's options. The module docstring is
the reference for every key. Findings per board are in
[010](010-source-inventory.md#htmlconfig-discovery-2026-10-09).

## Breakage detection

A scraper that fails loudly is maintainable; one that fails quietly is worthless. Surfaced on
the console's **Sources** page ([007](007-console-and-tracking.md)):

1. **Volume floor** — `expect.min_jobs_per_week`. Dropping below it sets `status: suspect` even
   though the run "succeeded."
2. **Shape assertions** — each adapter declares required fields. Rows that parse with 95% null
   `posted_at` indicate a layout change, not an empty board.
3. **Family-signal drift** — `jobhunter sources verify` re-probes every entry URL weekly and
   diffs HTTP status, family signals, and `robots.txt` hash against the recorded values. This is
   what catches a state migrating VOS → something else *before* the adapter silently returns
   zero. The byte-identical-bundle check is a first-class signal here: if the JobLink bundle
   hash changes on one host but not the other six, that state has been upgraded early.

Status values: `ok`, `suspect`, `broken`, `blocked`, `manual`, `disabled`. A run prints the
non-`ok` set and exits non-zero. Silence means healthy; it never means "zero jobs found."

## Email job-alert ingest

Email alerts are the channel for boards that disallow crawling. They are Pass 2 of the roadmap,
and every subscription uses the dedicated `<you>+jobs@gmail.com` address so alerts
never reach your inbox. Full design, including how descriptions are handled when the board's
detail pages are off-limits: [012](012-email-ingest.md).
