# 008 — Compliance, politeness & what we will not build

These are public-sector job boards publishing public information, and you are one person
looking for work. That is about as benign as automated collection gets. But the system touches
55 government web servers on a schedule, so the posture needs to be deliberate rather than
accidental.

## Defaults

| Setting | Default | Rationale |
|---|---|---|
| `respect_robots` | **true** | A source whose `robots.txt` disallows us is marked `blocked` and not fetched |
| Rate limit | **0.2 req/s per source** (1 per 5s), concurrency 1, jittered | Slower than a human browsing. 55 sources in parallel is still fast overall |
| Global concurrency | 8 sources at once | Caps total outbound load |
| User-Agent | `jobhunter/0.1 (personal job search; <your-email>)` | Identifies the client and gives an admin someone to contact |
| Conditional requests | `If-None-Match` / `If-Modified-Since` always | A `304` costs the server almost nothing |
| Retries | 5 max, exponential backoff with jitter | |
| On `403`/`401` | **stop the source immediately**, mark `broken`, no retries | A block is a decision, not a blip. Do not hammer past it |
| Crawl window | Nightly, off-peak (default 02:00 local) | |
| Query budget | 15–40 targeted queries per source per night | Not a full crawl ([003](003-sources-and-adapters.md#query-driven-ingestion-not-full-crawl)) |
| Resolve budget | 1,500 detail fetches per run, global | Bounds the one stage that is per-job |
| Sessions | **Guest/anonymous only** | No account creation, no login, no credentials |

The query-driven design is itself the main politeness mechanism: a few dozen search requests per
state per night, against the boards' own supported search interface, is a tiny fraction of the
load that exhaustively walking result pages would create.

## Verified robots.txt survey (2026-10-09)

**This section corrects an earlier claim in these specs.** I first wrote that the `robots.txt`
problem was confined to NEOGOV and that "Class A — the 54 job banks — is unaffected." That was
wrong: I had checked NEOGOV and not the job banks. Having now surveyed the banks directly, the
problem is much larger and it lands squarely on the two biggest platform families.

### Class B (state-employer sites)

| Host | Finding |
|---|---|
| `www.governmentjobs.com` | Allowlists Googlebot, bing, Yahoo, IndeedJobBot, Twitterbot, facebookexternalhit, gsa-crawler; then `User-agent: *` → **`Disallow: /`**. Public `sitemap.xml`, no RSS |
| `www.schooljobs.com` | Same policy (also NEOGOV) |
| `calcareers.ca.gov` | `#User-agent: *` / `#Disallow: /` — **commented out**, so not a directive |
| `careers.georgia.gov` | `User-agent: *` with `Allow:` rules (Drupal default) — permissive |
| `careers.wa.gov`, `www.jobapscloud.com` | Empty / absent |

### Class A (the 54 job banks) — all surveyed

Every host in [010](010-source-inventory.md) plus `usnlx.com`, fetched once on 2026-10-09 with
`User-Agent: jobber-research/0.1 (personal job search)`. Raw results:
[`data/robots-survey-2026-10-09.json`](data/robots-survey-2026-10-09.json). "Search" and "Detail"
say whether robots permits the job-search and job-detail paths for a generic crawler. A `200`
HTML page served at `/robots.txt` (a soft 404 or an SPA shell) counts as absent.

| Host | Family | `robots.txt` | Search | Detail |
|---|---|---|---|---|
| `alabamaworks.alabama.gov` (AL) | `vos` | **`Disallow: /`** | **no** | **no** |
| `alaskajobs.alaska.gov` (AK) | `vos` | **`Disallow: /`** | **no** | **no** |
| `www.azjobconnection.gov` (AZ) | `joblink` | only `/search/jobs`, `/search/resumes` | **no** | yes |
| `www.arjoblink.arkansas.gov` (AR) | `joblink` | only `/search/jobs`, `/search/resumes` | **no** | yes |
| `www.caljobs.ca.gov` (CA) | `vos` | **`Disallow: /`** | **no** | **no** |
| `www.connectingcolorado.com` (CO) | `?` | absent (404 or HTML page); search host `jobs.connectingcolorado.gov`: `/` (allowlist includes `/careerhub/explore/jobs`) | yes | ? |
| `www.cthires.com` (CT) | `vos` | **`Disallow: /`** | **no** | **no** |
| `joblink.delaware.gov` (DE) | `joblink` | only `/search/jobs`, `/search/resumes` | **no** | yes |
| `www.dcnetworks.org` (DC) | `vos` | **403** to curl (WAF); not determinable | ? | ? |
| `www.employflorida.com` (FL) | `vos` | **`Disallow: /`** | **no** | **no** |
| `www.worksourcegaportal.com` (GA) | `vos` | **`Disallow: /`** | **no** | **no** |
| `www.hireguam.com` (GU) | `vos` | **`Disallow: /`** | **no** | **no** |
| `www.hirenethawaii.com` (HI) | `vos` | **`Disallow: /`** | **no** | **no** |
| `idahoworks.gov` (ID) | `joblink` | only `/search/jobs`, `/search/resumes` | **no** | yes |
| `illinoisjoblink.illinois.gov` (IL) | `joblink` | only `/search/jobs`, `/search/resumes` | **no** | yes |
| `www.indianacareerconnect.com` (IN) | `vos` | **`Disallow: /`** | **no** | **no** |
| `www.iowaworks.gov` (IA) | `vos` | **`Disallow: /`** | **no** | **no** |
| `www.kansasworks.com` (KS) | `joblink` | only `/search/jobs`, `/search/resumes` | **no** | yes |
| `kyjobs.usnlx.com` (KY) | `nlx` | only `/*feed/`, `/*feeds/` | yes | yes |
| `www.louisianaworks.net` (LA) | `vos` | absent (404 or HTML page) | yes | yes |
| `joblink.maine.gov` (ME) | `joblink` | only `/search/jobs`, `/search/resumes` | **no** | yes |
| `mwejobs.maryland.gov` (MD) | `vos` | **`Disallow: /`** | **no** | **no** |
| `jobquest.mass.gov` (MA) | `?` | **`Disallow: /`** | **no** | **no** |
| `www.mitalent.org` (MI) | `?` | absent (404 or HTML page); search host `jobs.mitalent.org`: `/Feedback/`, `/bot-trap/` | yes | yes |
| `careerforce.mn.gov` (MN) | `?` | only `/core/`, `/profiles/`, `/admin/`, `/search/`, `/job-search`, `/job-search/`, `/occupations/`, `/explore/`, ... | **no** | ? |
| `wings.mdes.ms.gov` (MS) | `?` | **`Disallow: /`** | **no** | **no** |
| `jobs.mo.gov` (MO) | `?` | only `/core/`, `/profiles/`, `/admin/`, `/search/`, `/user/login/`, (plus index.php/ variants) | ? | ? |
| `montanaworks.gov` (MT) | `nlx` | absent (404 or HTML page); search host `montana.usnlx.com`: `/*feed/`, `/*feeds/` | yes | yes |
| `neworks.nebraska.gov` (NE) | `vos` | **`Disallow: /`** | **no** | **no** |
| `www.employnv.gov` (NV) | `vos` | **`Disallow: /`** | **no** | **no** |
| `nhworksjobmatch.nhes.nh.gov` (NH) | `vos` | **403** to curl (WAF); not determinable | ? | ? |
| `nj.gov` (NJ) | `?` | only `/cgi-bin/homelandsecurity/`, `/cgi-bin/dobi/licenseesearch/`, `/cgi-bin/consumeraffairs/search/`, `/cgi-bin/state/`, `/Support/`, `/treasury/treasdocuments/`, `/highereducation/higheddocs/`, `/oag/secure-pdf/`; search host `jobsource.nj.gov`: absent | yes | ? |
| `www.jobs.dws.nm.gov` (NM) | `vos` | **`Disallow: /`** | **no** | **no** |
| `newyork.usnlx.com` (NY) | `nlx` | absent (404 or HTML page); search host `myjobsny.usnlx.com`: `/*feed/`, `/*feeds/` | yes | yes |
| `www.ncworks.gov` (NC) | `vos` | **`Disallow: /`** | **no** | **no** |
| `www.ndworkforceconnection.com` (ND) | `vos` | **`Disallow: /`** | **no** | **no** |
| `marianaslabor.net` (MP) | `?` | absent (404 or HTML page) | yes | yes |
| `ohiomeansjobs.ohio.gov` (OH) | `?` | absent (404 or HTML page) | ? | ? |
| `www.employoklahoma.gov` (OK) | `sfdc` | only `*/secur/forgotpassword.jsp?*` | yes | yes |
| `secure.emp.state.or.us` (OR) | `?` | **`Disallow: /`** | **no** | **no** |
| `www.pacareerlink.pa.gov` (PA) | `?` | **`Disallow: /`** - with the inline comment `# keep them out` | **no** | **no** |
| `www.employri.org` (RI) | `vos` | **`Disallow: /`** | **no** | **no** |
| `jobs.scworks.org` (SC) | `vos` | **`Disallow: /`** | **no** | **no** |
| `www.southdakotaworks.org` (SD) | `vos` | **`Disallow: /`** | **no** | **no** |
| `www.jobs4tn.gov` (TN) | `vos` | **`Disallow: /`** (at `jobs4tnwfs.tn.gov`; entry host serves no robots) | **no** | **no** |
| `www.workintexas.com` (TX) | `vos` | **`Disallow: /`** | **no** | **no** |
| `jobs.utah.gov` (UT) | `?` | only `/calendar`, `/_Admin`, `/_vti`, `/services/foodstamp`, `/infosource`, `/jobseeker/DislocatedWorker/Webhelp`, `/Infosource/eligibilitymanual`, `/edo/geninfo`, ... | yes | yes |
| `www.vermontjoblink.com` (VT) | `joblink` | only `/search/jobs`, `/search/resumes` | **no** | yes |
| `www.vidolviews.org` (VI) | `vos` | **`Disallow: /`** | **no** | **no** |
| `vawc.virginia.gov` (VA) | `vos` | **`Disallow: /`** | **no** | **no** |
| `worksourcewa.com` (WA) | `sfdc` | absent (404 or HTML page); search host `worksource.my.site.com`: `*/secur/forgotpassword.jsp?*` | yes | yes |
| `workforcewv.org` (WV) | `wordpress` | empty `Disallow:` (open), `Crawl-delay: 10` | yes | yes |
| `jobcenterofwisconsin.com` (WI) | `?` | only `/*.axd$` | yes | yes |
| `hire.wyo.gov` (WY) | `next/custom` | absent (404 or HTML page) | yes | yes |
| `usnlx.com` (national) | `nlx` | only `/*feed/`, `/*feeds/` | yes | yes |

Tallies over the 54 banks: **27 `Disallow: /`**, **15 partial**, **1 open**, **9 absent**,
**2 undeterminable** (DC and NH: HTTP 403 to curl).

Four things in the table are worth stating plainly:

1. **The VOS family is closed, with one exception.** 22 of the 25 originally classified VOS
   hosts serve the identical `Disallow: /` file (23 of 26 with NM, newly classified as VOS). DC
   and NH return 403 and are presumed the same. **LA** has no `robots.txt` at all (404), so it
   is the one VOS bank robots does not close.
2. **The JobLink family is 8 hosts, not 7** (VT verified by bundle hash), and all 8 disallow
   `/search/jobs` and `/search/resumes` - a surgical block of programmatic search. Detail
   pages are not disallowed.
3. **Redirects and delegated hosts matter.** TN's entry host `jobs4tn.gov` has no robots (it now
   redirects to a `tn.gov` page); the live VOS host `jobs4tnwfs.tn.gov` is `Disallow: /`, so
   the earlier "no disallow" for TN was wrong. CO, MI, NJ and WA delegate search to another host
   whose robots differs from the entry host. MN, previously read as Drupal-admin-only, also
   disallows `/job-search`, which is the search.
4. **The permissive sources are mostly *not* in the big families.** They are the NLx boards
   and the one-off boards, so in robots-respecting mode the "one adapter, 25 sources" economics
   that justified this architecture **do not apply**; the reachable sources each need their own
   `htmlconfig` row instead.

### What is reachable, respecting robots

The open set is the sources where both search and detail are permitted
([010](010-source-inventory.md#open-set-2026-10-09) has entry URLs and caveats).

| | Count |
|---|---|
| USAJOBS (sanctioned API — see note) | 1 |
| National NLx (`usnlx.com`) | 1 |
| Class A banks in the open set (KY, LA, MI, MP, MT, NY, OK, UT, WA, WV, WI, WY) | 12 |
| Class A banks closed (`Disallow: /`) | 27 |
| Class A banks with search disallowed (8 JobLink, MN) | 9 |
| Class A banks not determinable (DC and NH 403; MO bot-challenge; OH 404 everywhere) | 4 |
| Class A banks with a partial answer (CO: search allowed, detail unclear; NJ: info page, app on another host) | 2 |

Realistically **12 of 54 state banks plus national NLx plus federal** — about a fifth of the
banks, not 55. Several of the 12 carry caveats (OK and WA are JS-rendered Salesforce communities,
WV is an info site with a 10-second crawl delay), so the practically scrapable core is smaller.

### A note on `data.usajobs.gov`

Its `robots.txt` is also `Disallow: /`, and that is **not** a conflict. `robots.txt` governs
crawlers. USAJOBS publishes a documented public API with individually issued keys and published
terms; using it under those terms is not crawling, and the key *is* the permission. Federal
remains fully sanctioned and unaffected.

Two further readings baked into the tables above: a **commented-out** directive is not a
directive, and an **absent** `robots.txt` is not a prohibition. Neither is treated as license to
be impolite — the rate limits above apply regardless.

### Consequence for the design

Email job alerts move from a late nice-to-have to **the primary collection mechanism for the
blocked majority** ([012](012-email-ingest.md)).
Every one of these boards offers them. Same data, delivered through the front door, by a channel
the board built for exactly this purpose. Narrower and a day slower — and not a workaround, but
consent.

## The decision you have to make

This started as a question about NEOGOV and has become the central question of the project.

**What NEOGOV is:** the company behind `governmentjobs.com`, the applicant-tracking system most
state, county, and city HR departments use. If you apply for a job working *for* a government,
you very likely do it there. Its `robots.txt` allowlists seven search-engine crawlers
(Googlebot, bingbot, IndeedJobBot, Twitterbot, and friends) and then tells everyone else
`Disallow: /`.

**Why it stopped being a corner case:** the same posture turns out to hold across the Virtual
OneStop family and, in targeted form, across the entire JobLink family. So this is not a
decision about one secondary source class. It is a decision about whether the 50-state sweep
happens at all.

| Option | Coverage | Posture |
|---|---|---|
| **1. Respect robots everywhere** | USAJOBS + ~10–20 state banks | Cleanest. Loses the VOS and JobLink families, which is most of the volume |
| **2. Email job alerts for the blocked sources** | Most of the blocked majority, narrower and a day late | **Fully sanctioned** — the board is pushing to you by design. The best available answer |
| **3. Ask for access** | Potentially excellent | Geographic Solutions and state workforce agencies both have an interest in people finding jobs. Costs an email |
| **4. Per-source override** | Everything | A `robots.txt` is a site directive, not law, and these are public records — but it is a directive, stated clearly, and in PA's case with the comment `# keep them out`. Your call, not mine |

**Decided 2026-10-09: options 1 + 2.** Respect `robots.txt`; email alerts at a dedicated
`+jobs` address cover the blocked boards ([012](012-email-ingest.md)). Option 3 stays open.

**Recommendation at the time: 1 + 2, then 3 where it matters.** Option 2 is the load-bearing one — it is the
same data through a channel built for it, and it makes the blocked families reachable without
disregarding anything. Option 3 is worth an afternoon. Option 4 exists in the design as an
explicit, logged, per-source `policy` flag because it is a legitimate choice for one person
reading public records at one request per five seconds — but it should be a decision you make
deliberately, and I am not going to set it as a default.

Option 4 is not something I will refuse to implement. It is something I will not quietly enable.

## What we will not build

Not as a lecture, just so the boundary is clear and nobody wonders later:

- **User-agent rotation or spoofing.** The UA identifies us honestly.
- **Proxy or IP rotation pools.** One machine, one IP.
- **CAPTCHA solving or bypass.** A CAPTCHA means stop. That source becomes `manual` tier.
- **Login automation or credential storage** for any board. Guest sessions only.
- **Automated application submission.** [001](001-goals-and-scope.md#non-goals) — against most
  boards' terms, and auto-filed applications are bad applications.
- **Ignoring a `403`.** A block is honored immediately and permanently until you intervene.

If a source cannot be collected politely and openly, it drops to `manual` tier and you export a
CSV. That is a worse experience and a perfectly acceptable outcome.

## Personal data

- `profile/` and `data/` are gitignored. Your resume, preferences, and application history never
  leave the machine except as LLM request bodies.
- Your resume **is** sent to the model API on every Stage 2 call — that is inherent to the task.
  It goes to the configured provider and nowhere else. Worth knowing if you switch
  `screen_scorer` to a third-party endpoint ([006](006-fit-scoring.md#the-scorer-is-pluggable)):
  that choice sends your resume there too.
- No analytics, no telemetry, no outbound reporting. The console binds to `127.0.0.1`.
- `ANTHROPIC_API_KEY` from the environment or an `ant auth login` profile. No keys in config
  files, no keys in the repo.

## Legal note, briefly

I am not a lawyer and this is not legal advice. The relevant context: these are government
public records; US case law on scraping publicly accessible pages (*hiQ v. LinkedIn*,
*Van Buren v. United States*) has generally not treated it as computer fraud; a `robots.txt`
and a site's terms of service are contractual/directive rather than criminal matters. The
practical risks of being impolite are an IP block and a complaint, not a prosecution. The
defaults above are set to avoid both.
