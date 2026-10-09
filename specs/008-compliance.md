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

### Class A (the 54 job banks) — sampled

| Host | Family | `robots.txt` |
|---|---|---|
| `www.workintexas.com` | vos | **`Disallow: /`** (Twitterbot allowed on the landing page only) |
| `www.caljobs.ca.gov` | vos | **`Disallow: /`** |
| `www.employflorida.com` | vos | **`Disallow: /`** |
| `www.ncworks.gov` | vos | **`Disallow: /`** |
| `mwejobs.maryland.gov` | vos | **`Disallow: /`** |
| `vawc.virginia.gov` | vos | **`Disallow: /`** |
| `www.employnv.gov` | vos | **`Disallow: /`** |
| `neworks.nebraska.gov` | vos | **`Disallow: /`** |
| `www.cthires.com` | vos | **`Disallow: /`** |
| `jobs.scworks.org` | vos | **`Disallow: /`** |
| `www.hirenethawaii.com` | vos | **`Disallow: /`** |
| `www.jobs4tn.gov` | vos | no disallow ✅ |
| **all 7 JobLink hosts** | joblink | **`Disallow: /search/jobs`** + `/search/resumes` |
| `www.pacareerlink.pa.gov` | ? | **`Disallow: /`** — with the inline comment `# keep them out` |
| `jobquest.mass.gov` | ? | **`Disallow: /`** |
| `montanaworks.gov` | nlx | no disallow ✅ |
| `newyork.usnlx.com` | nlx | no disallow ✅ |
| `kyjobs.usnlx.com` | nlx | only `/*feed/`, `/*feeds/` ✅ |
| `hire.wyo.gov` | next | no disallow ✅ |
| `ohiomeansjobs.ohio.gov` | ? | no disallow ✅ |
| `careerforce.mn.gov` | ? | only `/core/`, `/profiles/` (Drupal admin) ✅ |
| `jobcenterofwisconsin.com` | ? | only `/*.axd$` ✅ |
| `worksourcewa.com` | ? | empty / absent ✅ |

Three conclusions, none of them convenient:

1. **The VOS family is effectively closed.** 11 of 12 sampled VOS hosts serve `Disallow: /` —
   that is ~23 of the 25 sources the highest-value adapter was meant to unlock.
2. **The JobLink family disallows the exact endpoint we need.** All 7 hosts disallow
   `/search/jobs` specifically. Not a blanket block — a surgical one aimed precisely at
   programmatic job search. The intent is unambiguous.
3. **The permissive sources are mostly *not* in the big families.** They are the one-off and
   unclassified boards, so in robots-respecting mode the "one adapter, 25 sources" economics
   that justified this architecture **does not apply**; the reachable sources each need their own
   `htmlconfig` row instead.

### What is reachable, respecting robots

| | Count |
|---|---|
| USAJOBS (sanctioned API — see note) | 1 |
| Class A banks confirmed permissive | 8 |
| Class A banks confirmed blocked | 14+ (≈23 once the VOS family is assumed) |
| Class A banks not yet checked | ~22 |

Realistically **~10–20 of 54 state banks plus federal** — not 55. The ~22 unchecked hosts will
split, but since most are VOS or JobLink by family, the split is unlikely to favor us.

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
