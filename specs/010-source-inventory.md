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
| `wyo` (was `next/custom`) | 1 | 1 |
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
| LA | Louisiana | Louisiana Works | `https://www.louisianaworks.net/hire/vosnet/Default.aspx` | `vos` | **verified** — `/vosnet/` in URL; **re-probed 2026-10-09: Incapsula JS challenge on every path, policy manual** | ABSENT | **no** (bot gate) | **no** (bot gate) |
| ME | Maine | Maine Employment Info Guide | `https://joblink.maine.gov/` | `joblink` | **verified** — identical bundle sha `2c0d5d29…` | PARTIAL | **no** | yes |
| MD | Maryland | Maryland Workforce Exchange | `https://mwejobs.maryland.gov/vosnet/Default.aspx` | `vos` | **verified** — `/vosnet/` in URL | DISALLOW_ALL | **no** | **no** |
| MA | Massachusetts | Massachusetts JobQuest | `https://jobquest.mass.gov/` | `?` | unclassified - ASP.NET WebForms (__VIEWSTATE), /JobQuest/Search.aspx | DISALLOW_ALL | **no** | **no** |
| MI | Michigan | Michigan's Talent Bank | `https://www.mitalent.org/` | `htmlconfig` | **classified 2026-10-09** - Orchard CMS front; search is ASP.NET WebForms on `jobs.mitalent.org` (form POST, `__doPostBack` paging, anonymous detail pages) | ABSENT (search host `jobs.mitalent.org`: PARTIAL) | yes | yes |
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
| MP | Northern Mariana Islands |  | `https://marianaslabor.net/jvapub_list.asp` | `htmlconfig` | **classified 2026-10-09** - classic ASP vacancy list + `jvapub_view.asp` details; active list empty on the day | ABSENT | yes | yes |
| OH | Ohio | Ohio's Statewide Job Matching System | `https://ohiomeansjobs.ohio.gov/home` | `?` | unclassified - none: every path on the host (including / and /home) returned a 404 page | ABSENT | ? | ? |
| OK | Oklahoma | Oklahoma Job Link | `https://www.employoklahoma.gov/` | `sfdc` | **verified (2026-10-09)** - Salesforce Experience Cloud community (SfdcApp redirect, "default robots.txt for sfdc communities"); **2026-10-09: no guest access, every page redirects to login, policy manual** | PARTIAL | **no** (login) | **no** (login) |
| OR | Oregon | Find work, post jobs and locate workforce services | `https://secure.emp.state.or.us/jobs/index.cfm?bkmk=w9` | `?` | unclassified - entry is a JS bot-challenge page | DISALLOW_ALL | **no** | **no** |
| PA | Pennsylvania | Services for Job Seekers and Employers | `https://www.pacareerlink.pa.gov/jponline/Common/LandingPage/` | `?` | unclassified - PA CareerLink /jponline/ (Java/JSP-style paths) | DISALLOW_ALL | **no** | **no** |
| RI | Rhode Island | Rhode Island Job Bank | `https://www.employri.org/vosnet/Default.aspx` | `vos` | **verified** — `/vosnet/` in URL | DISALLOW_ALL | **no** | **no** |
| SC | South Carolina | South Carolina Job Bank | `https://jobs.scworks.org/vosnet/Default.aspx` | `vos` | **verified** — `/vosnet/` in URL | DISALLOW_ALL | **no** | **no** |
| SD | South Dakota | South Dakota Department of Labor and Regulation | `https://www.southdakotaworks.org/vosnet/Default.aspx` | `vos` | **verified** — `/vosnet/` in URL | DISALLOW_ALL | **no** | **no** |
| TN | Tennessee | Tennessee Department of Labor and Workforce Development | `https://www.jobs4tn.gov/vosnet/Default.aspx` | `vos` | **verified** - `/vosnet/` in URL; entry URL stale (see robots survey) | DISALLOW_ALL (at real host `jobs4tnwfs.tn.gov`) | **no** | **no** |
| TX | Texas | WorkInTexas | `https://www.workintexas.com/vosnet/Default.aspx` | `vos` | **verified** — `/vosnet/` in URL | DISALLOW_ALL | **no** | **no** |
| UT | Utah | Utah's Job Connection | `https://jobs.utah.gov/index.html` | `?` | unclassified - static CMS (opencms paths); **2026-10-09: the job search is the UtahID-gated UWORKS app, policy manual** | PARTIAL | **no** (login) | **no** (login) |
| VT | Vermont | Vermont JobLink | `https://www.vermontjoblink.com/` | `joblink` | **verified (2026-10-09)** - /packs/js/4743-860615d0fe49e3040bc6.js present; fetched sha256 2c0d5d29e49fd986 matches (newly classified) | PARTIAL | **no** | yes |
| VI | Virgin Islands | Virgin Islands Job Bank | `https://www.vidolviews.org/vosnet/Default.aspx` | `vos` | **verified** — `/vosnet/` in URL | DISALLOW_ALL | **no** | **no** |
| VA | Virginia | Virginia's Automated Labor Exchange | `https://vawc.virginia.gov/vosnet/Default.aspx` | `vos` | **verified** — `/vosnet/` in URL | DISALLOW_ALL | **no** | **no** |
| WA | Washington |  | `https://worksourcewa.com/home.aspx` | `sfdc` | **verified (2026-10-09)** - worksourcewa.com redirects to worksource.my.site.com/worksourcewa/ (Salesforce **LWR** community; same default robots text as OK); **2026-10-09: guest Apex endpoint serves search with descriptions, no login, `htmlconfig` JSON row** | ABSENT (search host `worksource.my.site.com`: PARTIAL) | yes | yes (in the search response) |
| WV | West Virginia | WorkForce West Virginia | `https://workforcewv.org/individuals/` | `wordpress` | **verified (2026-10-09)** - wp-content, WordPress (Yoast block in robots); **the job search lives on `macc.workforcewv.org`, which is `Disallow: /`; policy manual** | OPEN at `workforcewv.org`; **DISALLOW_ALL at `macc.workforcewv.org`** | **no** | **no** |
| WI | Wisconsin | Wisconsin Job Center | `https://jobcenterofwisconsin.com/` | `?` | unclassified - ASP.NET WebForms (__VIEWSTATE), Presentation/JobSeekers/JobSearch.aspx; **2026-10-09: result list public, every detail redirects to Login.aspx; policy manual** | PARTIAL | yes (list only) | **no** (login) |
| WY | Wyoming | Wyoming at Work | `https://hire.wyo.gov/home` | `wyo` (Next.js custom) | **verified** — `__NEXT_DATA__`, `/api/workflow` | ABSENT | yes | yes |

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
| WY | `https://hire.wyo.gov/home` | `wyo` (Next.js custom) | ABSENT | API found 2026-10-09: anonymous JSON, no login. `GET /employer-api/api/jobs/search?skip&limit&sort=desc&searchText&location&postDate=MM/DD/YYYY` (list, full `totalRecords`) and `GET /employer-api/api/jobs/search/<_id>` (detail with description and apply link). The home page's `/api/workflow` is unrelated (internal workflow engine). Board is mostly NLx-fed (`source: NLX`) plus Workforce Services employer postings. Human URL `https://hire.wyo.gov/job/<_id>`. |
| LA | `https://www.louisianaworks.net/hire/vosnet/Default.aspx` | `vos` | ABSENT | **manual**: Incapsula bot challenge on every path (re-probed 2026-10-09) |
| MI | `https://jobs.mitalent.org/job-search` (entry `www.mitalent.org`) | `?` | PARTIAL (`/Feedback/`, `/bot-trap/`) | **built** (`htmlconfig`): robots read from `jobs.mitalent.org`, not the entry host |
| OK | `https://www.employoklahoma.gov/Participants/s/` | `sfdc` | PARTIAL (forgot-password) | **manual**: no guest access; every `/Participants/s/` page redirects to login |
| WA | `https://worksource.my.site.com/worksourcewa/` (entry `worksourcewa.com`) | `sfdc` | ABSENT / PARTIAL (forgot-password) | **built** (`htmlconfig`, JSON): guest Apex call; entry `/home.aspx` is 404 |
| UT | `https://jobs.utah.gov/index.html` | `?` | PARTIAL (template dirs, unrelated) | **manual**: search is the UtahID-gated UWORKS app |
| WV | `https://workforcewv.org/job-seeker/find-a-job/browse-wv-jobs/` | `wordpress` | OPEN | **manual**: real listing host `macc.workforcewv.org` is `Disallow: /` (Crawl-delay 10 applies only to `workforcewv.org`) |
| WI | `https://jobcenterofwisconsin.com/` | `?` | PARTIAL (`/*.axd$`) | **manual**: list is public but job detail needs login |
| MP | `https://marianaslabor.net/jvapub_list.asp` | `?` | ABSENT | **built** (`htmlconfig`): list was empty on the day; layout taken from the inactive list |

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

## htmlconfig discovery (2026-10-09)

Live discovery for milestone M3 ([003](003-sources-and-adapters.md#the-htmlconfig-adapter)):
at most one request per 5 s per host (11 s on the Crawl-delay: 10 host), User-Agent
`jobhunter/0.1 (personal job search; <you>@example.com)`, robots rechecked per host and per path,
no accounts, no gate bypass. Three boards are built; five are `manual` with the reason recorded
here. Fixtures live in `tests/fixtures/htmlconfig/<board>/`.

| ST | Outcome | What was found |
|---|---|---|
| MI | **built** | `jobs.mitalent.org` robots disallows only `/Feedback/` and `/bot-trap/` (named scraper bots aside; a sitemap of `/job-seeker/job-details/JobCode/<id>` URLs is advertised). `/job-search` is a WebForms form: GET it, POST the hidden state plus keywords and a posting-age select (1/7/14/30 days), land on `/job-seeker/jobsearch-results/` (25 per page), page with `__doPostBack('ctl00$MainContent$pageTop<n>','')`. Detail pages are anonymous and carry description, employer, location, posted date, expiry and the employer's apply link. Paging is capped at 4 pages: the pager's later layout was not mapped, and relevance order (not date) is the default. |
| MP | **built** | robots.txt is 404. `jvapub_list.asp` is the public active vacancy list and was empty on 2026-10-09 ("There are no active JVAs at this time"); `jvapub_listinact.asp` shares its table layout and was used for the row fixtures. Details (`jvapub_view.asp?jvaID=`) are label/value tables; the first table (employer address, phone, email form) is deliberately left out of the description. Not verified against a live active row. |
| WA | **built** | `worksource.my.site.com/worksourcewa/` is a Salesforce **LWR** site (not Aura). robots is `Allow: /` apart from the forgot-password path. The public `/job-search` route calls Apex `WswaJobSearchResultsController.initializeJobSearch` as the guest user through `/worksourcewa/webruntime/api/apex/execute`; the same call works without cookies or a CSRF token and returns the first 100 matches newest first with HTML descriptions, closing date and pay. No browser is needed, so the row is tier `api`. Later pages (`loadMoreJobs`) require echoing back a server-built SOQL `filters` string; the row does not do that, so a query yields at most 100 jobs. `payType` is unreliable (a $130,000 salary labelled Hourly), so `salary_raw` is advisory. |
| OK | manual | robots allows all, but the Participants community has no guest access: `/Participants/s/`, `/s/job-search` and others answer with a redirect to `/Participants/s/login`; the community sitemap lists two info pages, no jobs. Needs a login, which we do not create. |
| UT | manual | robots allows it, but the only job search (`/jsp/utjobs/seeker/search/top-jobs`, the target of the Hot Jobs "register to find your next job" link) 302s to a UtahID sign-in. No anonymous search or detail exists. |
| WI | manual | robots allows it. `JobOrderList.aspx?kwords=...&wd=<days>&src=JCW,PARTNERS` returns an anonymous list (title, employer, city, date; 20 per page, `Page$n` postbacks, "more than 500 jobs" cap), but every job detail (`EnhancedJobs-det.aspx?OrderNumber=`) redirects to `/Login.aspx`, with or without a session cookie. A list with no description and no reachable posting cannot be scored. |
| LA | manual | robots.txt is 404, but on re-probe every path (including `/`, `/robots.txt` and the VOS entry) returns an Imperva Incapsula JavaScript challenge page to an honest client. Passing it needs a browser fingerprint; that is a bot gate. |
| WV | manual | `workforcewv.org` is open (Crawl-delay: 10) but is an info site; the job search link `browse-wv-jobs` 301s to `macc.workforcewv.org/jobs`, whose robots.txt is `User-agent: *` `Disallow: /`. The robots survey read the wrong host. |

**Salesforce boards and the browser tier.** A Playwright adapter turned out not to be needed for
WA: the LWR guest Apex endpoint is plain HTTPS+JSON. For OK there is nothing to automate (login
wall). If OK later offers guest access, the likely route is the same Apex execute endpoint of its
`/Participants/webruntime/api/apex/execute` (if it is LWR) or `/Participants/s/sfsites/aura` (if
Aura); a browser adapter would only be needed if those demand a Lightning session token.

**Follow-ups.** (1) WA beyond 100 results needs `loadMoreJobs` with the echoed server filter; decide
whether replaying server-built SOQL is acceptable before doing it. (2) MI: map the pager past page
4 and the posting-date sort (`PageSortTop=Posted` via "Update Results") so a watermark can end
paging. (3) MP: re-run discovery when the active list has rows. (4) Revisit UT, WI, LA and OK with
the weekly `sources verify` for changes in gates.

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
