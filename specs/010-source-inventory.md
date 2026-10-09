# 010 — Source inventory

**54 sources** (50 states + DC, Guam, Northern Mariana Islands, US Virgin Islands), extracted
from [CareerOneStop's state job banks page](https://www.careeronestop.org/JobSearch/FindJobs/state-job-banks.aspx)
and probed on **2026-10-09**. Plus USAJOBS as source class C ([011](011-federal-and-geography.md)).

Every URL below returned HTTP 200 on a live request on the original probe; on the 2026-10-09 re-probe OH (`/home`) and WA (`/home.aspx`) returned 404 and DC / NH returned 403 to curl. `registry.yaml` is generated
from this table — this document is the human-editable record of what was observed and when.

## Family distribution

Updated 2026-10-09 after the robots survey and the Milestone 1 classification pass
(54 job banks; USAJOBS is class C and `usnlx.com` is a 55th host surveyed but not a state row).

| Family | Sources | Was |
|---|---|---|
| `vos` | 26 | 25 (+NM) |
| `joblink` | 8 | 7 (+VT) |
| `nlx` | 3 | 3 |
| `sfdc` (Salesforce Experience Cloud community) | 2 (OK, WA) | new |
| `wordpress` (info site; job backend not identified) | 1 (WV) | new |
| `next/custom` | 1 | 1 |
| `?` (custom / unidentified) | 13 | 18 |
| `usajobs` (class C) | 1 | 1 |

The 18 unclassified rows resolved as: NM to `vos` (redirects to `/vosnet/`, all of vosnet /
Geographic Solutions / `__VIEWSTATE`), VT to `joblink` (the shared bundle is present and its
sha256 is `2c0d5d29e49fd986…`), OK and WA to `sfdc`, WV to `wordpress`. The other 13 are one-off
custom apps (ASP.NET WebForms for MA, WI, CO; Spring/JSP for MS; Drupal for MN and MO; JSF for NJ;
classic ASP for MP; Orchard CMS for MI) or could not be observed (OH 404s on every path; MO and OR
serve a bot-challenge page to curl). The earlier expectation that most would fall into `vos` or
`joblink` was wrong: only two did.

## The table

| ST | State / Territory | Job bank | Entry URL | Family | Evidence | Robots | Search | Detail |
|---|---|---|---|---|---|---|---|---|
| AL | Alabama | Alabama JobLink | `https://alabamaworks.alabama.gov/vosnet/Default.aspx` | `vos` | **verified** — `/vosnet/` in URL | DISALLOW_ALL | **no** | **no** |
| AK | Alaska | Alaska Job Center Network | `https://alaskajobs.alaska.gov/vosnet/Default.aspx` | `vos` | **verified** — `/vosnet/` in URL | DISALLOW_ALL | **no** | **no** |
| AZ | Arizona | Arizona Workforce Connection | `https://www.azjobconnection.gov/` | `joblink` | **verified** — identical bundle sha `2c0d5d29…` | PARTIAL | **no** | yes |
| AR | Arkansas | Arkansas JobLink | `https://www.arjoblink.arkansas.gov/` | `joblink` | **verified** — identical bundle sha `2c0d5d29…` | PARTIAL | **no** | yes |
| CA | California | California' Caljobs | `https://www.caljobs.ca.gov/vosnet/Default.aspx` | `vos` | **verified** — `/vosnet/` in URL | DISALLOW_ALL | **no** | **no** |
| CO | Colorado | Connecting Colorado | `https://www.connectingcolorado.com/` | `?` | unclassified - entry is ASP.NET (empyra); search delegated to `jobs.connectingcolorado.gov/careerhub` | ABSENT (search host `jobs.connectingcolorado.gov`: PARTIAL) | yes | ? |
| CT | Connecticut | Job and Career ConneCTion. | `https://www.cthires.com/` | `vos` | **verified** — 302 → `/vosnet/default.aspx`, 5 signals | DISALLOW_ALL | **no** | **no** |
| DE | Delaware | Delaware Job Link | `https://joblink.delaware.gov/` | `joblink` | **verified** — identical bundle sha `2c0d5d29…` | PARTIAL | **no** | yes |
| DC | District of Columbia | DCNetworks | `https://www.dcnetworks.org/vosnet/Default.aspx` | `vos` | **verified** — `/vosnet/` in URL | UNKNOWN (403) | ? | ? |
| FL | Florida | Employ Florida Marketplace | `https://www.employflorida.com/vosnet/Default.aspx` | `vos` | **verified** — `/vosnet/` in URL | DISALLOW_ALL | **no** | **no** |
| GA | Georgia | WorkSource Georgia | `https://www.worksourcegaportal.com/vosnet/Default.aspx` | `vos` | **verified** — `/vosnet/` in URL | DISALLOW_ALL | **no** | **no** |
| GU | Guam | Guam Department of Labor - Job Bank | `https://www.hireguam.com/vosnet/default.aspx` | `vos` | **verified** — `/vosnet/` in URL | DISALLOW_ALL | **no** | **no** |
| HI | Hawaii | HireNet Hawaii | `https://www.hirenethawaii.com/vosnet/Default.aspx` | `vos` | **verified** — `/vosnet/` in URL | DISALLOW_ALL | **no** | **no** |
| ID | Idaho | Idaho Commerce and Labor Job Search | `https://idahoworks.gov/` | `joblink` | **verified** — identical bundle sha `2c0d5d29…` | PARTIAL | **no** | yes |
| IL | Illinois | Illinois Department of Employment Security | `https://illinoisjoblink.illinois.gov/` | `joblink` | **verified** — identical bundle sha `2c0d5d29…` | PARTIAL | **no** | yes |
| IN | Indiana | Indiana Workforce Development | `https://www.indianacareerconnect.com/vosnet/Default.aspx` | `vos` | **verified** — `/vosnet/` in URL | DISALLOW_ALL | **no** | **no** |
| IA | Iowa | IowaJobs | `https://www.iowaworks.gov/vosnet/Default.aspx` | `vos` | **verified** — `/vosnet/` in URL | DISALLOW_ALL | **no** | **no** |
| KS | Kansas | KansasWorks | `https://www.kansasworks.com/` | `joblink` | **verified** — identical bundle sha `2c0d5d29…` | PARTIAL | **no** | yes |
| KY | Kentucky | EmployKy | `https://kyjobs.usnlx.com/jobs/` | `nlx` | **verified** — `usnlx` strings | PARTIAL | yes | yes |
| LA | Louisiana | Louisiana Works | `https://www.louisianaworks.net/hire/vosnet/Default.aspx` | `vos` | **verified** — `/vosnet/` in URL | ABSENT | yes | yes |
| ME | Maine | Maine Employment Info Guide | `https://joblink.maine.gov/` | `joblink` | **verified** — identical bundle sha `2c0d5d29…` | PARTIAL | **no** | yes |
| MD | Maryland | Maryland Workforce Exchange | `https://mwejobs.maryland.gov/vosnet/Default.aspx` | `vos` | **verified** — `/vosnet/` in URL | DISALLOW_ALL | **no** | **no** |
| MA | Massachusetts | Massachusetts JobQuest | `https://jobquest.mass.gov/` | `?` | unclassified - ASP.NET WebForms (__VIEWSTATE), /JobQuest/Search.aspx | DISALLOW_ALL | **no** | **no** |
| MI | Michigan | Michigan's Talent Bank | `https://www.mitalent.org/` | `?` | unclassified - Orchard CMS front (meta generator) | ABSENT (search host `jobs.mitalent.org`: PARTIAL) | yes | yes |
| MN | Minnesota | Minnesota's Job Bank - Where Job Seekers and Employers Click | `https://careerforce.mn.gov/` | `?` | unclassified - Drupal 11 (meta generator) | PARTIAL | **no** | ? |
| MS | Mississippi | Increasing Employment in Mississippi | `https://wings.mdes.ms.gov/wings/welcome.jsp` | `?` | unclassified - Spring/JSP app (/wings/spring/self-service/job-order/job-order-lookup) | DISALLOW_ALL | **no** | **no** |
| MO | Missouri | GreatHires - Missouri's Workforce Resource | `https://jobs.mo.gov/` | `?` | unclassified - entry returned a 205-byte Incapsula bot-challenge page to curl | PARTIAL | ? | ? |
| MT | Montana | Jobs - Montana | `https://montanaworks.gov/` | `nlx` | **verified** — `usnlx` strings | ABSENT (search host `montana.usnlx.com`: PARTIAL) | yes | yes |
| NE | Nebraska | NEworks | `https://neworks.nebraska.gov/vosnet/Default.aspx` | `vos` | **verified** — `/vosnet/` in URL | DISALLOW_ALL | **no** | **no** |
| NV | Nevada |  | `https://www.employnv.gov/vosnet/Default.aspx` | `vos` | **verified** — `/vosnet/` in URL | DISALLOW_ALL | **no** | **no** |
| NH | New Hampshire |  | `https://nhworksjobmatch.nhes.nh.gov/vosnet/Default.aspx` | `vos` | **verified** — `/vosnet/` in URL | UNKNOWN (403) | ? | ? |
| NJ | New Jersey | New Jersey Department of Labor and Workforce Development | `https://nj.gov/labor/career-services/` | `?` | unclassified - nj.gov/labor/career-services is a static info page | PARTIAL (search host `jobsource.nj.gov`: ABSENT) | yes | ? |
| NM | New Mexico | New Mexico Workforce Connection | `https://www.jobs.dws.nm.gov/` | `vos` | **verified (2026-10-09)** - entry redirects to /vosnet/default.aspx; page has vosnet, Geographic Solutions, __VIEWSTATE (newly classified) | DISALLOW_ALL | **no** | **no** |
| NY | New York | Workforce New York | `https://newyork.usnlx.com/index.asp` | `nlx` | **verified** — `usnlx` strings | ABSENT (search host `myjobsny.usnlx.com`: PARTIAL) | yes | yes |
| NC | North Carolina | The Employment Security Commission of North Carolina | `https://www.ncworks.gov/vosnet/Default.aspx` | `vos` | **verified** — `/vosnet/` in URL | DISALLOW_ALL | **no** | **no** |
| ND | North Dakota | JobService North Dakota - Your Workforce Connection | `https://www.ndworkforceconnection.com/vosnet/Default.aspx` | `vos` | **verified** — `/vosnet/` in URL | DISALLOW_ALL | **no** | **no** |
| MP | Northern Mariana Islands |  | `https://marianaslabor.net/jvapub_list.asp` | `?` | unclassified - classic ASP (jvapub_list.asp) | ABSENT | yes | yes |
| OH | Ohio | Ohio's Statewide Job Matching System | `https://ohiomeansjobs.ohio.gov/home` | `?` | unclassified - none: every path on the host (including / and /home) returned a 404 page | ABSENT | ? | ? |
| OK | Oklahoma | Oklahoma Job Link | `https://www.employoklahoma.gov/` | `sfdc` | **verified (2026-10-09)** - Salesforce Experience Cloud community (SfdcApp redirect, "default robots.txt for sfdc communities") | PARTIAL | yes | yes |
| OR | Oregon | Find work, post jobs and locate workforce services | `https://secure.emp.state.or.us/jobs/index.cfm?bkmk=w9` | `?` | unclassified - entry is a JS bot-challenge page | DISALLOW_ALL | **no** | **no** |
| PA | Pennsylvania | Services for Job Seekers and Employers | `https://www.pacareerlink.pa.gov/jponline/Common/LandingPage/` | `?` | unclassified - PA CareerLink /jponline/ (Java/JSP-style paths) | DISALLOW_ALL | **no** | **no** |
| RI | Rhode Island | Rhode Island Job Bank | `https://www.employri.org/vosnet/Default.aspx` | `vos` | **verified** — `/vosnet/` in URL | DISALLOW_ALL | **no** | **no** |
| SC | South Carolina | South Carolina Job Bank | `https://jobs.scworks.org/vosnet/Default.aspx` | `vos` | **verified** — `/vosnet/` in URL | DISALLOW_ALL | **no** | **no** |
| SD | South Dakota | South Dakota Department of Labor and Regulation | `https://www.southdakotaworks.org/vosnet/Default.aspx` | `vos` | **verified** — `/vosnet/` in URL | DISALLOW_ALL | **no** | **no** |
| TN | Tennessee | Tennessee Department of Labor and Workforce Development | `https://www.jobs4tn.gov/vosnet/Default.aspx` | `vos` | **verified** - `/vosnet/` in URL; entry URL stale (see robots survey) | DISALLOW_ALL (at real host `jobs4tnwfs.tn.gov`) | **no** | **no** |
| TX | Texas | WorkInTexas | `https://www.workintexas.com/vosnet/Default.aspx` | `vos` | **verified** — `/vosnet/` in URL | DISALLOW_ALL | **no** | **no** |
| UT | Utah | Utah's Job Connection | `https://jobs.utah.gov/index.html` | `?` | unclassified - static CMS (opencms paths) | PARTIAL | yes | yes |
| VT | Vermont | Vermont JobLink | `https://www.vermontjoblink.com/` | `joblink` | **verified (2026-10-09)** - /packs/js/4743-860615d0fe49e3040bc6.js present; fetched sha256 2c0d5d29e49fd986 matches (newly classified) | PARTIAL | **no** | yes |
| VI | Virgin Islands | Virgin Islands Job Bank | `https://www.vidolviews.org/vosnet/Default.aspx` | `vos` | **verified** — `/vosnet/` in URL | DISALLOW_ALL | **no** | **no** |
| VA | Virginia | Virginia's Automated Labor Exchange | `https://vawc.virginia.gov/vosnet/Default.aspx` | `vos` | **verified** — `/vosnet/` in URL | DISALLOW_ALL | **no** | **no** |
| WA | Washington |  | `https://worksourcewa.com/home.aspx` | `sfdc` | **verified (2026-10-09)** - worksourcewa.com redirects to worksource.my.site.com/worksourcewa/ (Salesforce community; same default robots text as OK) | ABSENT (search host `worksource.my.site.com`: PARTIAL) | yes | yes |
| WV | West Virginia | WorkForce West Virginia | `https://workforcewv.org/individuals/` | `wordpress` | **verified (2026-10-09)** - wp-content, WordPress (Yoast block in robots) | OPEN | yes | yes |
| WI | Wisconsin | Wisconsin Job Center | `https://jobcenterofwisconsin.com/` | `?` | unclassified - ASP.NET WebForms (__VIEWSTATE), Presentation/JobSeekers/JobSearch.aspx | PARTIAL | yes | yes |
| WY | Wyoming | Wyoming at Work | `https://hire.wyo.gov/home` | `next/custom` | **verified** — `__NEXT_DATA__`, `/api/workflow` | ABSENT | yes | yes |

## Open set (2026-10-09)

Sources where robots permits both job search and job detail (see
[008](008-compliance.md#verified-robotstxt-survey-2026-10-09), raw data in
[`data/robots-survey-2026-10-09.json`](data/robots-survey-2026-10-09.json)). "Open" is judged
from robots.txt only; every row still needs an adapter and a fixture, and the caveats are real.

| Source | Entry URL | Family | Robots | Caveat |
|---|---|---|---|---|
| US (national NLx) | `https://usnlx.com/` | `nlx` | PARTIAL (feeds only) | none |
| KY | `https://kyjobs.usnlx.com/jobs/` | `nlx` | PARTIAL (feeds only) | none |
| MT | `https://montana.usnlx.com/jobs/` (landing `montanaworks.gov`) | `nlx` | ABSENT; `montana.usnlx.com` feeds only | search host confirmed 2026-10-09 (same NLx app, X-Origin `montana.usnlx.com`) |
| NY | `https://myjobsny.usnlx.com/jobs/` (legacy `newyork.usnlx.com`) | `nlx` | ABSENT; `myjobsny.usnlx.com` feeds only | search host confirmed 2026-10-09 (same NLx app, X-Origin `myjobsny.usnlx.com`) |
| WY | `https://hire.wyo.gov/home` | `next/custom` | ABSENT | none |
| LA | `https://www.louisianaworks.net/hire/vosnet/Default.aspx` | `vos` | ABSENT | the only VOS host without `Disallow: /`; needs the VOS adapter |
| MI | `https://jobs.mitalent.org/job-search` (entry `www.mitalent.org`) | `?` | PARTIAL (`/Feedback/`, `/bot-trap/`) | robots read from `jobs.mitalent.org`, not the entry host |
| OK | `https://www.employoklahoma.gov/Participants/s/` | `sfdc` | PARTIAL (forgot-password) | entry redirects to a login page; guest access unverified; JS-rendered |
| WA | `https://worksource.my.site.com/worksourcewa/` (entry `worksourcewa.com`) | `sfdc` | ABSENT / PARTIAL (forgot-password) | entry `/home.aspx` is 404; JS-rendered |
| UT | `https://jobs.utah.gov/index.html` | `?` | PARTIAL (template dirs, unrelated) | search backend path not identified |
| WV | `https://workforcewv.org/job-seeker/find-a-job/browse-wv-jobs/` | `wordpress` | OPEN | **Crawl-delay: 10**; listing backend not identified |
| WI | `https://jobcenterofwisconsin.com/` | `?` | PARTIAL (`/*.axd$`) | WebForms postback app |
| MP | `https://marianaslabor.net/jvapub_list.asp` | `?` | ABSENT | small vacancy list |

That is 12 state/territory sources plus national NLx. Near misses, not in the open set:

- **CO** - search host `jobs.connectingcolorado.gov` is `Disallow: /` with an allowlist that
  includes `/careerhub/explore/jobs`; search allowed, detail URL shape not determined.
- **JobLink (8)** - search disallowed, detail allowed; reachable only through email alerts
  ([012](012-email-ingest.md)).
- **MN, MO, NJ, OH** - search disallowed (MN) or not observable (MO bot-challenge, NJ info page
  with a JSF app on `jobsource.nj.gov`, OH 404 everywhere).
- **DC, NH** - robots.txt returns 403 to curl; presumed the standard VOS `Disallow: /`.

Correction: TN was previously read as "no disallow". `jobs4tn.gov` now redirects to a `tn.gov`
page and the live VOS host `jobs4tnwfs.tn.gov` serves `Disallow: /`. The entry URL above is stale.

## NLx coverage (2026-10-09)

How much of a robots-blocked state's market does national NLx (`usnlx.com`) carry? Measured
with `scripts/nlx_coverage.py`: one query per state, `q="software engineer"` (quoted, which is
what the adapter sends for a title), against the national site's search API filtered to the
state's location slug, at most one request per 5 s. Ten states, chosen to bound load; nine are
`blocked` in the registry and CO is a near miss (`manual`).

**How NLx search works** (found by reading the site's Nuxt bundles; no browser was available):
the pages are client-rendered shells. Search is
`GET https://prod-search-api.jobsyn.org/api/v1/solr/search?q=&location=&sort=date&num_items=&offset=`
with an `X-Origin` header naming the site; the site fixes the page size (15 national, 10 on
Montana) and paging is by `offset`. Detail is `GET https://microsites.dejobs.org/ALL_JOBS/<GUID>.json`.
Neither JSON host serves a robots.txt (404); every `*.usnlx.com` site disallows only
`/*feed/` and `/*feeds/`. The KY, MT and NY sites are the same app and index: their
`X-Origin` widens or narrows a regional scope (the KY site's default results include TN, OH
and WV), so state rows also filter by `location=<state slug>`.

| ST | Registry policy | Results for "software engineer" | Page 1 in state |
|---|---|---:|---|
| TX | blocked | 3,601 | 15/15 |
| CA | blocked | 6,540 | 15/15 |
| FL | blocked | 1,761 | 15/15 |
| NC | blocked | 1,543 | 15/15 |
| VA | blocked | 3,342 | 15/15 |
| GA | blocked | 1,433 | 15/15 |
| IL | blocked | 1,709 | 15/15 |
| MA | blocked | 1,691 | 15/15 |
| PA | blocked | 1,562 | 15/15 |
| CO | manual | 2,326 | 15/15 |

For scale, the same query nationally returned 68,154; the in-state sites returned 762 (KY),
626 (MT) and 3,493 (NY). Every result on each first page was in the filtered state, and the
newest was posted the same day.

**Reading.** National NLx carries thousands of current software postings in every blocked
state measured, so it is worth running as the main route into those states. It does not
replace the state boards, and these numbers do not show what share of a board's inventory it
holds:

- **Not measured: overlap with the blocked boards.** Their robots.txt forbids searching them,
  so there is no denominator. The counts are what NLx holds, not a coverage percentage.
- NLx is mostly employer-ATS syndication (DirectEmployers members) plus state-bank feeds. Jobs
  posted only on a state board by small employers are the likeliest to be missing.
- Counts include multi-state duplicates: some employers post one remote role in every state
  (Oracle lists the same role in each capital, e.g. Helena MT and Frankfort KY). Dedupe
  collapses these; raw counts overstate distinct jobs.
- Results are matched loosely. Even quoted, the phrase matches inside longer titles and
  descriptions, so the counts include adjacent roles.
- One query, one day, ten states. Not measured: other titles, other blocked states, lag
  between a board posting and its NLx appearance, or expiry behaviour.

Re-run with `uv run python scripts/nlx_coverage.py` (all blocked states) or `--states`.

## Not in this list

- **Puerto Rico** — absent from the CareerOneStop page. Worth adding manually if wanted.
- **Class B (state-employer career sites)** — a separate inventory, not yet built. The six rows
  probed while drafting [003](003-sources-and-adapters.md) are recorded there, including the
  finding that `governmentjobs.com` disallows generic crawlers ([008](008-compliance.md)).
