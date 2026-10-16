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
   Links the stored description gives (`pipeline/apply_hints.py`, bug 3bab68d), read from
   `description_raw` at resolve time (no refetch, no column backfill), each graded by where it
   points once unwrapped:
   - *matched* (it shares a number of four digits or more with the job's own links or id, or
     carries every word of the title, and no own link on the same host names another number):
     an ordinary start on an ATS posting or the employer's site. Ranked after the job's own
     results on the same tier, live or closed.
   - *fallback* (cued by "apply" text, an ATS posting, not matched): tried only when no own or
     matched start reached an ATS or the employer's site (live, closed, failing or behind a
     sign-in, directly or through a board's button), no own start is a live board copy, and
     nothing failed transiently; kept only when it ends live on an ATS posting
     (`ats_posting`). The row has `from_hint = 1`, and its URLs are never used to merge groups
     (`dedupe_url`, `dedupe_xstate`) or to match mail alerts, pastes, captures or pages.
   - dropped: uncued links that are not matched, ATS home and list pages (`ats_posting`: hosts
     known only by name count only with a posting shape, e.g. Infor short links), employer
     pages that are not matched, trackers, other hosts, USA Staffing links (trusted only from
     ApplyURI), and boilerplate (privacy, arbitration, accommodation, accessibility, EEO,
     referral, talent pool, "how to apply"), read before and after the link. Two matched
     links in one description are both demoted to fallbacks.
2. **Unwrap known redirectors without fetching.** Many tracking links carry the destination in a
   query parameter (`url=`, `dest=`, `target=`, `u=`). A table of known redirector hosts and their
   parameter names unwraps them locally, which needs no request.
3. **Follow the rest.** HTTP 3xx via `Location`, `<meta http-equiv="refresh">`, and simple
   `window.location = "…"` assignments in an inline script. At most 8 hops, with loop detection.
   A page on a job board or aggregator (a board copy, e.g. a dejobs.org or usnlx.com page) is
   not the posting: its off-site Apply button (to an ATS, the employer or a tracker; never a
   login page or a USA Staffing link) is walked, and that walk replaces the board copy only
   when it ends live off the boards, or closed on an ATS or the employer's site (the job is
   then closed); a wall, an error or another board keeps the board copy,
   and a passing failure marks the result transient (the nightly pass defers). The hop is
   stored with `method: "meta"` and `via: "button"`, so older code still parses the chain.
   USAJOBS postings are never walked past. One button walk and one detail-JSON request per
   posting per group.
   NLx and dejobs.org pages are client-rendered shells, so their button is the detail JSON's
   `other.application_link` when it qualifies (often a state job bank, which does not), else
   its `link` (`de.jobsyn.org/<GUID>10`; one request to `microsites.dejobs.org`).
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

**Retries (bug c369bcc).** A link is *settled* when the user set it or a capture supplied it
(`chosen_by`), or it is `expired` or `blocked`, or `live` anywhere but a job board copy or a
tracker (`HostTier.aggregator`, `HostTier.tracker`). Exception: a live link ending on a USAJOBS
posting is settled although `usajobs.gov` is a board host. USAJOBS is the federal apply entry
point, and its Apply button hands off to the agency's hiring system only after sign-in, so a
retry can never find a better link (`apply_rank.RESOLVED_BOARD_HOSTS`). Anything else (no row,
a deferred try, `unresolved`, a live board copy or tracker) is tried again, nightly and on
demand, up to `apply_links.max_attempts` (default 5) tries in all, counted per group in
`apply_link_attempt` (migration 0043) and spaced 1 h, 4 h, 12 h, then 20 h apart from when each
try started. The nightly pass goes in score order (the user: "we should process links in order of
score"): shortlisted groups by latest overall score (unscored ones after, newest first), then A
and B groups in one list by score, whatever the letter; never-tried before a retry only on an
equal score. A retry
replaces the stored row only with a link no worse (`resolve_group(only_if_better=True)`,
compared with the row as it is at write time, under the write lock); a capture is never
replaced by a ranked link; a live board copy stays the Apply target meanwhile. A retry that
finds a live link on the employer's site keeps it even when another start failed transiently;
when the job's own starts failed, a link from the posting's text counts only when it is matched
by an id number, never by title words alone (bug 5c1d738: those also match a same-title
opening).
Each try stores `applylink.WALK_VERSION`; when a change to the walk bumps it, tries by the
older walk stop counting. Reset on the job page clears the count.

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
  error         TEXT,
  chosen_by     TEXT,              -- NULL (ranked) | capture | user   (migration 0036)
  why           TEXT               -- one line under the button       (migration 0036)
);

CREATE TABLE apply_link_attempt (   -- tries per group (migration 0043, bug c369bcc)
  job_group_id  INTEGER PRIMARY KEY REFERENCES job_group(id),
  attempts      INTEGER NOT NULL DEFAULT 0,
  last_at       TEXT NOT NULL,     -- when the last try started; never moves back
  last_outcome  TEXT,              -- live | expired | unresolved | blocked | board | kept |
                                   -- deferred | user_link | no_url | error
  walk_version  INTEGER NOT NULL DEFAULT 1  -- applylink.WALK_VERSION of the last try
);

CREATE TABLE apply_click (
  id            INTEGER PRIMARY KEY,
  job_group_id  INTEGER NOT NULL REFERENCES job_group(id),
  at            TEXT NOT NULL,
  final_url     TEXT NOT NULL,
  outcome       TEXT              -- redirected | expired
);
```

Keyed on `job_group`. ~~When the same opening is posted by several sources, the first chain to
reach `live` wins.~~ Superseded (bug 334d9eb): a state job bank whose link redirected to a
single-sign-on page won over copies whose links led to the employer's ATS.

### Choosing among a group's links (bug 334d9eb)

- **Login walls are never the posting.** A hop to a login or single-sign-on page (hosts such as
  `id.<state>.gov`, `login.*`, `sso.*`, `auth.*`, `accounts.*`; paths such as `/login`,
  `/signin`, `/sso/`, `oauth2/authorize`, SAML), or a fetched page that is a password form with
  little else, stops the walk *before* the wall: status `blocked`, error `login wall at <host>`,
  final URL the page before it. The wall is never requested, and its URL never merges groups.
  An ATS's own steps are not walls: a login-looking path on the ATS posting path (iCIMS
  `/jobs/<id>/<slug>/login`), or an ATS page redirecting to the same ATS's own sign-in page
  (`AtsRule.sign_in`: Workday `/<site>/login`, Taleo `iam/accessmanagement/login.jsf`; e.g.
  Taleo `jobapply.ftl` → `login.jsf`), keeps the ATS page as a live link (bug 5c1d738). The
  sign-in hop stays in the chain, but the row is not a login link (`ends_at_own_sign_in`): Apply
  opens the ATS page, it is re-verified like any live link, and a re-verify that meets the same
  redirect keeps it live. Only those paths count: a login-looking board slug on the same ATS
  (Greenhouse `/cas`, `/sso`, a NEOGOV or Workday site named `CAS`) is a board home, so a
  posting redirecting there is closed.
- **Every member's start URL is a candidate** (deduplicated), resolved in this order: links an
  extension capture supplied, then by where the start points (ATS, employer, other, trackers,
  boards last), one per host before a second from the same host. At most 6 starts that cost
  requests per group (`MAX_FETCH_STARTS`); LinkedIn and Indeed starts cost none and are never
  requested. Each request goes through `FetchContext` (robots.txt, per-host rate limits). A
  live ATS posting, or a capture of the employer's page, settles it early.
- **Ranking by final host** (`pipeline/apply_rank.py`; the host lists live in
  `pipeline/ats_rules.py`): known ATS > the employer's own site (a host naming the employer) >
  other > a tracker the chain never got past (aplitrak, appcast) > job boards, aggregators and
  state job banks (`AGGREGATOR_HOSTS`, the one list, which capture also uses for "this page is
  a board", plus class A sources' entry hosts with and without `www.`). Within a tier: live >
  expired > blocked > unresolved; then the final URL more members reached; then the most
  recently seen member. A closed page outranks live links only on an ATS or the employer's
  site; a dead tracker or board copy ranks after every live link, and when it is all there is,
  the closed page says the link no longer works rather than that the employer's posting
  closed. Login walls rank last.
- **A capture wins**: a linked capture of the employer's page (or a board capture's off-site
  Apply destination) beats any ranking unless it is closed or a wall. A captured page counts
  as the employer's only when it is on an ATS or a host naming the employer; a capture made on
  builtin.com, wellfound.com or any other board is just another copy.
- **The user's link wins over everything**: **Use a different apply link** on the job page
  (`POST /job/{id}/apply-link`, same-origin, http(s) only) stores it with `chosen_by = 'user'`;
  it is never re-resolved, re-verified or replaced by a capture. **Reset** deletes it and the
  group resolves again.
- The job page shows one line of why, e.g. `via www.aplitrak.com → jobs.ashbyhq.com ·
  employer's applicant tracking system · 2 of 9 links checked`, or `set by you`.
- Rows stored before this change that took a login wall for the posting (`live` with a login
  final URL or hop) are resolved again by the nightly run's resolve stage, on demand when their
  job page is opened, and on a click on Apply (`/apply/{id}`, which a packet's Open application
  uses). The old row is replaced only by an answer (live, expired, or a real login stop); after
  a fetch failure it stays and is tried again, and meanwhile Apply opens its start URL, never
  the login page.

## Milestone placement

The resolver and ATS table go in **M3**, alongside the open-board adapters, since that's where
the first real chains appear. USAJOBS `ApplyURI` support goes in **M2**. The button, chain
display, click endpoint and "Did you apply?" prompt go in **M5**.
