# 006 — Fit scoring

Four stages, each cheaper than the next by roughly two orders of magnitude, each narrowing the
funnel. **Keyword search runs first and only at the source; the model does the real judging.**

```
  Stage 0  source-side keyword/occupation search   ~500,000 →  ~6,000 / week   free
  Stage 1  deterministic prefilter (hard facts)      ~6,000 →  ~2,000 / week   free
  Stage 2  Haiku 4.5 screen, batched                 ~2,000 →    ~120 / week   ~$4/wk
  Stage 3  Opus 5 deep pass on the shortlist           ~120 →     ~40 / week   ~$3/wk
  Stage 4  you, in the console, by bucket               ~40 →  applications
```

The design principle throughout: **keywords narrow, the model judges, and you decide.** No stage
before Stage 2 is allowed to make a judgment call, and no stage is allowed to reject on the
absence of information.

## Stage 0 — keyword search at the source

Covered in [003](003-sources-and-adapters.md#query-driven-ingestion-not-full-crawl). Boards'
own search filters do the gross reduction, because crawling 54 labor-exchange boards
exhaustively is neither affordable nor polite.

Keywords here are a **net, not a filter** — deliberately broad, 15–40 per profile, covering
synonyms, adjacent titles, and O*NET occupation codes where supported. Over-narrow queries are
the one failure in this pipeline that nothing downstream can fix, which is why
[003](003-sources-and-adapters.md) specifies a monthly wide sweep to measure what they miss.

## The profile spec

This is the input that makes the model better than keyword matching, so it carries far more than
keywords. Two files, both gitignored, both yours.

`profile/resume.md` — your actual resume as markdown. Full text, not a summary.

`profile/preferences.yaml` — split deliberately into facts and judgment:

```yaml
version_note: "hashed into scoring_version (model inputs) and filter_version (everything else)"

# ─── HARD CONSTRAINTS ─── machine-checkable. Stage 1 may reject on these.
hard:
  states_allowed: [CO, NM, AZ, UT, WA, OR]
  remote_ok: true
  remote_only: false
  salary_floor: {amount: 125000, period: year}
  employment_types_excluded: [seasonal, temporary]
  requires_i_lack: [active_security_clearance, cdl, rn_license, pe_license]
  max_travel_pct: 25
  title_exclusions: ["intern", "volunteer", "student trainee"]

# ─── SOFT PREFERENCES ─── weighted. Only the model reads these.
soft:
  weights: {skills: 0.30, seniority: 0.20, domain: 0.20, comp: 0.15, location: 0.15}
  state_ranking: [CO, NM, WA, OR, AZ, UT]

# ─── THE PART KEYWORDS CANNOT EXPRESS ─── free prose. This is the real payload.
narrative:
  want: |
    Deep Linux/infrastructure work: systems I can reason about end to end.
    Small teams where I own a system rather than a ticket queue.
    Public-sector stability and pension vesting matter more to me than equity upside.
    Mentoring is welcome; formal people-management is not what I'm after.
  avoid: |
    Roles that are really 24/7 on-call rotations with an engineering title.
    Jobs where "DevOps" means clicking through a vendor console.
    Heavy customer-facing or call-center components.
    Pure Windows/Active Directory shops.
    Agile-ceremony-heavy orgs where the work is estimation, not building.
  dealbreakers_soft: |
    A posting that is 60%+ project management, however titled.
    Anything requiring relocation to a metro over ~1M people.
  context: |
    15 years experience. Comfortable being the most senior IC on a team.
    Taking a pay cut for stability and interesting systems work is acceptable.
    Prior state-government experience, so I read classification language fine.
```

Plus a `current_focus` block — the highest-weighted part of the whole profile, specified in
[its own section below](#bucket-f-exists-because-of-recency-and-recency-is-the-actual-problem)
because it is what fixes the stale-match problem.

The `narrative` block is the whole point of using a model. "Not a 24/7 on-call rotation with an
engineering title" is not a keyword — it is a judgment about a posting's *shape*, available only
to something that read the whole description. Same for "60% project management however titled."

The profile is hashed into **two versions** ([014](014-preferences-console.md#the-design-change-free-settings-vs-paid-settings)):

- **`scoring_version`** covers what the model reads: the resume, `current_focus`, `done_with` and
  `narrative`. Every model score records it. Changing these costs money to re-apply, so it never
  happens automatically.
- **`filter_version`** covers everything computed in Python: hard constraints, salary, states,
  ranking, weights and bucket thresholds. Changing these re-applies instantly over stored
  scores, for free.

So you can move a salary floor from the preferences page without paying to re-score anything.

## Stage 1 — deterministic prefilter

Free, instant, auditable. Checks only `hard`.

**The rule that makes this safe: reject only on facts explicitly present in the posting.** A job
with no stated salary is never rejected by `salary_floor`; it goes to the model. A job with no
parseable location is never rejected by `states_allowed`. **Silence is not evidence.**

Multi-location jobs pass if **any** location qualifies, or if `location_scope` is `remote_us` /
`nationwide` / `negotiable` ([011](011-federal-and-geography.md#location-fit-scoring)).

Every rejection writes its rule keys to `prefilter_result.reasons`, and the console has a
**Rejected** view filterable by rule. A prefilter you cannot audit silently hides good jobs;
this one is inspectable, and reviewing it occasionally is a real maintenance task, not a nicety.

## Stage 2 — the screen (Haiku 4.5, batched)

Model `claude-haiku-4-5`, through the **Batch API** for the 50% discount. Latency is irrelevant
for a nightly run; results are durable for 29 days.

One call per **`job_group`**, never per job and never per location
([011](011-federal-and-geography.md)) — identical descriptions must not be scored repeatedly.

### Output schema

Structured outputs via `output_config.format`, read back with `client.messages.parse()`:

```python
class Dimension(BaseModel):
    score: int = Field(ge=0, le=100)
    why: str = Field(max_length=300)

class Evidence(BaseModel):
    claim: str
    quote: str          # MUST be verbatim from the posting — validated, see below

class Screen(BaseModel):
    verdict: Literal["strong", "possible", "weak", "mismatch"]
    overall: int = Field(ge=0, le=100)
    dimensions: dict[
        Literal["skills", "seniority", "domain"], Dimension
    ]
    # comp and location are NOT model output. They are computed in Python from
    # parsed salary and job_locations against the current filter settings, so
    # editing a salary floor or state ranking never requires re-scoring.
    evidence: list[Evidence] = Field(min_length=1, max_length=6)
    blockers: list[str]        # stated requirements you do not meet
    missing_info: list[str]    # what the posting failed to say
    shape_flags: list[str]     # matches against narrative.avoid, e.g. "on_call_heavy"
    tailoring_hints: list[str]
```

Three parts of that schema are doing real work:

**`evidence[].quote` must be verbatim.** After parsing, every quote is checked against
`description_text` by normalized substring match. A quote that is not present marks the score
`evidence_unverified`, and the console flags it. This turns "the model sounded confident" into
a mechanically checkable claim and is the single most effective guard against a plausible,
fabricated rationale.

**`missing_info` is required output.** It forces the model to distinguish "this job pays too
little" from "this job did not say what it pays" — a distinction that a single 0–100 score
erases, and the most common way a scorer quietly misleads you.

**`shape_flags`** is where `narrative.avoid` gets enforced, independent of the numeric score.
A job can score 85 on skills and still carry `on_call_heavy`; the console shows both rather
than averaging the warning away.

### Prompt construction & caching

Render order is `tools` → `system` → `messages`, and cache matching is **prefix-based**, so:

| Block | Content | Volatility |
|---|---|---|
| `system[0]` | rubric, dimension definitions, scoring anchors, output rules | frozen per `prompt_version` |
| `system[1]` | resume, `current_focus`, `done_with`, `narrative` (the judgment inputs only; not salary or states) | frozen per `scoring_version` |
| ← `cache_control: {"type": "ephemeral"}` here | | |
| `messages[0]` | **this one job posting** | per request |

Everything stable sits before the breakpoint; the only varying bytes are the posting. Nothing
volatile — no timestamp, no job ID, no run counter — may appear in the system blocks, which is
the classic silent cache-invalidator.

Two verification steps, because a cache that is not working costs 10x and says nothing:

- Assert `usage.cache_read_input_tokens > 0` on the second and later requests of a run. If it
  is zero, something is invalidating the prefix and that is a bug, not a mystery.
- Haiku's **minimum cacheable prefix is larger than Opus's**. If the rubric + profile come in
  under it, caching silently does not happen. Pad the rubric with genuinely useful scoring
  anchors rather than filler, and confirm with the assertion above.

Note two Haiku 4.5 specifics: `output_config.effort` is **not** supported and errors, and
thinking on Haiku 4.5 uses `thinking: {type: "enabled", budget_tokens: N}` rather than adaptive.
For a bounded classification task, run it with thinking off — the structured schema plus
required evidence quotes does the work that thinking would, at a fraction of the output cost.

### Scoring anchors

Free-floating 0–100 scores drift and inflate. The rubric defines anchors explicitly:

```
skills   90-100  does the work described daily, named tools are their primary stack
          70-89  clearly capable; 1-2 named tools are adjacent, not held
          40-69  transferable but would be learning on the job
          10-39  same broad field, different discipline
           0-9   unrelated
```

Anchors for all five dimensions. `overall` is **computed in Python** from the dimension scores
and `soft.weights`, not asked for from the model — a model asked for both a breakdown and a
total will produce a total that does not match its own breakdown, and you will not notice.

## Compatibility buckets

A single 0–100 score is not actionable. **Jobs sort into named buckets that say what kind of
fit this is and what to do about it.** Buckets are computed in Python from the dimension scores,
recency signals, and flags — not asked for from the model — so they are consistent, tunable
without re-scoring, and cannot drift.

| Bucket | Means | Your move |
|---|---|---|
| **A · Bullseye** | Core match to what you do *now*. You could start Monday. | Apply, minimal tailoring |
| **B · Strong** | Clear fit; 1–2 gaps your record covers adjacently | Apply, tailor to the gaps |
| **C · Stretch up** | Broader or more senior than your current title, but reachable | Apply if you want the jump |
| **D · Lateral** | Craft transfers, domain does not. You'd learn the domain, not the job | Apply selectively |
| **E · Downlevel** | You're overqualified — seniority or pay below your floor, otherwise a match | Only if the trade is worth it |
| **F · Stale match** | Matches skills you last used years ago, **not your recent work** | Usually skip — see below |
| **G · Mismatch** | Not your field | Hidden by default |

Bucket rules, in `config` and evaluated in order (first match wins):

```
G  skills < 35  or  domain < 25
F  recency_weighted_skills < 55  and  raw_skills >= 70      # the keyword-search trap
E  seniority_direction == "below"  and  comp_fit < 45
C  seniority_direction == "above"  and  skills >= 65
D  skills >= 60  and  domain < 55
A  overall >= 80  and  recency_weighted_skills >= 80  and  no blockers
B  overall >= 62
```

A is checked before B. An earlier draft listed B first, which made A unreachable under
"first match wins".

Any `blockers` entry demotes one bucket (A→B, B→C) — a stated requirement you do not meet is a
real obstacle, not a rounding error. `shape_flags` never change the bucket; they ride along as
visible badges, because "great fit, but it's a 24/7 on-call job" is two separate facts and
averaging them loses both.

## Bucket F exists because of recency, and recency is the actual problem

> *"keyword searching is usually crappy and jobs don't fit based on my most recent experience"*

This is the single most common way keyword matching fails, and it is not a search-quality
problem — it is a **model-of-you** problem. A keyword index treats a tool you touched in 2012
exactly like the stack you have run for the last three years. So it confidently surfaces jobs
matching the oldest, least relevant third of your resume, and you do the filtering by hand.

Two mechanisms fix it:

**1. Experience is dated, and the rubric weights it.** `profile/resume.md` keeps explicit date
ranges per role, and the rubric instructs the model to score skills **twice**:

- `raw_skills` — does your resume contain this anywhere, at any time? (what a keyword search sees)
- `recency_weighted_skills` — weighting the last 3 years at full value, 3–7 years at half,
  beyond 7 years at a quarter

The **gap between those two numbers is the signal.** `raw_skills: 88` with
`recency_weighted_skills: 41` means: this job matches who you were, not who you are. That is
bucket F, caught mechanically, and it is exactly the noise you are currently filtering by hand.

**2. A `current_focus` block in the profile**, weighted above the resume as a whole:

```yaml
current_focus:
  since: 2023-01
  doing: |
    Linux fleet operations at scale, Kubernetes, Terraform, incident response,
    internal platform tooling in Go and Python.
  want_more_of: [distributed systems, platform engineering, infrastructure as code]
  done_with: [Windows/AD administration, VMware, desk-side support, Oracle DBA]
```

`done_with` is the mirror image of keywords: things that *are* on your resume, that you *can*
do, and that you do **not** want surfaced. A keyword search cannot express that. A model reading
the full description can, and it feeds both bucket F and a `stale_match` shape flag.

Added to the Stage 2 schema:

```python
raw_skills: int                      # keyword-equivalent match, any era
recency_weighted_skills: int         # recency-weighted — the one that drives buckets
stale_skills: list[str]              # matched only on experience > 7 years old
current_focus_overlap: int           # 0-100 against current_focus.doing
done_with_hits: list[str]            # matched against done_with
```

`overall` uses `recency_weighted_skills`, never `raw_skills`. `raw_skills` is kept and displayed
only to expose the gap.

## Verdict thresholds

The `verdict` field is retained as a coarse rollup beneath the buckets: `strong ≥ 78`,
`possible ≥ 60`, `weak ≥ 40`, else `mismatch`. Buckets are what the console sorts by; thresholds
live in config, not the prompt, so tuning either costs nothing and re-scores nothing.

## Employer rejections

Rows in the `rejection` table ([007](007-console-and-tracking.md#optional-gmail-matching),
`core/rejections.py`) affect scoring in two ways, both decided in Python:

- **The same posting is never scored.** A group the rejection matched, or a group at the same
  normalized employer with the same title (the same words in any order, ignoring case,
  punctuation, plurals, filler words like "and"/"of", and spacing, so "Full Stack" is
  "Fullstack"; every other word must match, so "Engineer II" is not "Engineer III" and
  "Engineer, Cloud" is not "Engineer, Core"), is excluded from every
  eligibility query (`screen._ELIGIBLE` for Haiku, chat and Jev scorers, the bench, the
  backfill count, the deep shortlist), so no credits are spent. It is also left out of the
  inbox, and its detail page says so. No time window: rejected stays rejected.
- **Same employer, different role, within `scoring.employer_rejection_days` (default 90)**: the
  job is still scored, and the prompt carries one neutral sentence ("candidate was rejected by
  this employer for <title> on <date>", or "(role not stated)" when the email named no title,
  since then it is unknown whether this job is the rejected posting): a
  `Candidate history with this employer` line in the
  Haiku/chat posting text, an `employer_history` field in the Jev job state. No question asks
  about it, and pay and location remain Python's job. The bucket is **not** capped: a
  rejection for another role says little about fit for this one, so the inbox row and detail
  page show a flag instead and you decide.

Neither changes `prompt_version`, `scoring_version` or `filter_version`, on purpose: those key
the paid re-score, and a new rejection must not re-send every job. Skipping is filter-level and
free; the context sentence reaches jobs scored after the rejection arrives, and older scores
stand.

## Stage 3 — deep pass (Opus 5)

Top N by screen score (default 40/week, configurable), plus anything you manually promote.
Model `claude-opus-5` with `thinking: {type: "adaptive"}` and `output_config: {effort: "high"}`
— this is the stage where correctness is worth paying for. Not batched; it runs on demand when
you open a job, so you get it interactively.

Produces a fuller report: a reasoned recommendation, requirement-by-requirement gap analysis
against your resume, the two or three things to emphasize in an application, questions worth
asking, salary-band read against the posting's stated range, and a flag if the screen looks
wrong. A Stage 3 pass that **disagrees** with Stage 2 is a valuable signal and is surfaced as
such rather than silently overwriting it — both scores are kept.

## The scorer is pluggable

> *"we can use something like haiku or we can rope in a third party decision model"*

Yes — the decision model is behind an interface, and swapping it changes one config line:

```python
class FitScorer(Protocol):
    name: str                      # recorded in fit_score.model
    def score_batch(self, items: list[ScoreRequest]) -> list[Screen]: ...
    def supports_batching(self) -> bool: ...
```

```toml
[scoring]
screen_scorer = "anthropic:claude-haiku-4-5"    # default
deep_scorer   = "anthropic:claude-opus-5"
```

Implementations ship for Anthropic (batched) and a generic OpenAI-compatible HTTP endpoint,
which covers most third-party and local models. All of them produce the **same `Screen`
schema**, so buckets, evidence verification, the console, and `eval` are unchanged by the
choice.

The reason this matters is not portability — it is that **`eval` can compare scorers on your own
labeled jobs**:

```bash
jobhunter eval --compare anthropic:claude-haiku-4-5 anthropic:claude-opus-5 openai-compat:local
```

Recall, precision, bucket agreement, hallucinated-quote rate, and cost per 1,000 jobs, side by
side on your labels. That turns "which model should I use" from a guess into a measurement — and
it is worth re-running occasionally, since a cheaper model that holds recall is pure savings.

Default recommendation: **Haiku 4.5 for Stage 2, Opus 5 for Stage 3.** Haiku is fast and cheap
enough to screen everything, and the structured schema plus verified evidence quotes does most
of the work that a bigger model would. Measure before paying more.

## Calibration

Without this, prompt tuning is just vibes, and a scorer that has quietly drifted is worse than
no scorer.

Your console triage writes `label` rows (`interesting` / `not_interesting` / `applied`). That
is the calibration set — generated by using the tool, with no separate labeling chore.

```bash
jobhunter eval                        # current prompt_version vs all labels
jobhunter eval --compare v3 v4        # two rubric versions on the same labeled set
```

Reports, per `prompt_version` × `scoring_version`, and annotated with `profile_change` entries
so a shift caused by your own edit isn't mistaken for scorer drift:

- **Recall of `strong`+`possible`** against `interesting` — the metric that matters. A missed
  good job is invisible to you; a bad job that got through costs ten seconds.
- **Precision** of `strong`, and the discard rate on `not_interesting`.
- **Score distribution** — catches grade inflation, where everything creeps toward 80.
- **Dimension correlations** — a dimension that never varies is not measuring anything.
- **`evidence_unverified` rate** — the hallucinated-quote rate. Should be near zero.
- **Confusion pairs** — the specific jobs where model and human disagreed most, listed with
  links, because reading five of those teaches you more about the rubric than any summary.

Target before trusting the funnel: **≥ 150 labels**, recall ≥ 0.90, `evidence_unverified`
≤ 0.02. A prompt change that improves precision while dropping recall below 0.90 is a
regression, and `eval` says so rather than leaving it to judgment.

`rubric.py` carries `PROMPT_VERSION`; changing the prompt without bumping it is caught by a test.

## Cost

Prices: Haiku 4.5 $1.00/$5.00 per MTok, halved by the Batch API to **$0.50/$2.50**; cache reads
at 0.1x, so **$0.05/MTok** batched. Opus 5 $5.00/$25.00 per MTok.

**Stage 2, per job group:**

| | Tokens | Rate | Cost |
|---|---|---|---|
| Posting + metadata (uncached) | 2,000 | $0.50/MTok | $0.00100 |
| Rubric + profile (cache read) | 3,000 | $0.05/MTok | $0.00015 |
| Output | 350 | $2.50/MTok | $0.00088 |
| | | | **≈ $0.0020** |

**≈ $2.00 per 1,000 jobs screened.** At 2,000/week → **$4.00/week**.

**Stage 3, per job:** 5,000 in + 3,000 cached + 1,500 out ≈ **$0.064**. At 40/week → **$2.56/week**.

**Total ≈ $6.50/week, ≈ $28/month**, and that is the number the console's cost page tracks
against a configured cap.

For contrast, this is what the Stage 0 decision bought: exhaustively crawling 54 labor-exchange
boards would put on the order of 50,000 jobs/week into Stage 2 even after prefiltering —
**~$100/week, ~$430/month**, for a pile of postings that are mostly not in your field. Roughly
15x the cost for worse results.

A one-time backfill is affordable: 50,000 historical jobs ≈ $100, run once.

## Failure modes this design defends against

| Failure | Defense |
|---|---|
| Plausible but fabricated rationale | `evidence[].quote` verified verbatim against the posting |
| Rejecting jobs for not stating something | Stage 1 rejects only on present facts; `missing_info` is required output |
| Grade inflation over time | Fixed anchors; `eval` tracks score distribution |
| Total disagreeing with its own breakdown | `overall` computed in Python from dimensions |
| Prompt tuning that feels better and is worse | `eval` on a held labeled set, recall as the primary metric |
| Soft preferences averaged away by a high skills score | `shape_flags` surfaced separately from the score |
| Scoring the same posting 14 times | Scoring keyed on `job_group`, not `job` or location |
| Re-scoring everything after a trivial edit | `comp`/`location` computed in Python; `UNIQUE(job_group, tier, prompt_version, scoring_version, model)` |
| Silent cache failure costing 10x | Assertion on `cache_read_input_tokens` |
| Runaway spend | Daily cap in config; scoring stops and the console shows a banner |
| **Jobs matching stale experience** | `recency_weighted_skills` vs `raw_skills` gap → **bucket F** |
| **Jobs you can do but are done with** | `current_focus.done_with` → `done_with_hits` + flag |
| Opaque single score | Deterministic **buckets** with a stated action per bucket |
| Locked into one model | `FitScorer` interface; `eval --compare` measures alternatives |
