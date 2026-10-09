# 004 — Ingest pipeline

Eight stages. Each reads rows in one state, writes rows in the next, and is idempotent. Any
stage can be re-run alone.

```
discover → list → resolve → normalize → dedupe → prefilter → score → triage
   │         │        │          │          │         │          │
 registry   stubs  full text  parsed    grouped   survivors   verdicts
 health     +      (the CSV   fields    dupes     +reasons
            water   problem)
            mark
```

```bash
jobhunter run                      # all stages, all enabled sources
jobhunter run --stage resolve      # one stage
jobhunter run --state WA,OR,CA     # subset
jobhunter run --since 2026-09-01   # override the window
jobhunter run --full               # ignore watermarks, backfill everything
jobhunter run --dry-run            # no network, no LLM; report what would happen
```

## Time windows and watermarks

"Latest jobs based on some time limit" is handled per source, not globally.

Each source row in `source_state` keeps:

- `last_run_at` — when we last completed successfully
- `max_posted_at_seen` — the newest `posted_at` ingested
- `watermark` — `max(max_posted_at_seen - overlap, now - lookback_days)`

`lookback_days` defaults to **7**; `overlap` defaults to **2 days**. The overlap is deliberate:
boards backdate postings, revise them, and occasionally publish out of order, so a
zero-overlap watermark silently drops jobs. Re-ingesting two days of already-seen jobs is free
(they dedupe to existing rows by natural key and never re-enter scoring).

`list_jobs` is a generator that stops as soon as it yields a stub older than the watermark.
On a board sorted newest-first, a nightly run reads one or two pages per state, not forty.

For boards with no date on the listing page, the adapter declares
`posted_at_available: false`; the pipeline then uses *first-seen* time as a proxy, paginates a
bounded `max_pages`, and relies on dedupe. Marked clearly in the data as `posted_at_estimated`
so scoring and the console never present a guess as a fact.

## 1. discover

Load and validate `registry.yaml`. Skip rows whose `policy` is not `enabled`, recording why.
Resolve each source's watermark. Emits a run plan; `--dry-run` stops here and prints it.

## 2. list

Per source, call `adapter.list_jobs(...)`. Produces `JobStub`:

```python
class JobStub(BaseModel):
    source_key: str
    external_id: str          # the board's own ID. The natural key.
    title: str
    url: HttpUrl
    posted_at: datetime | None
    closes_at: datetime | None = None
    location_raw: str | None = None
    salary_raw: str | None = None
    agency_raw: str | None = None
    description_raw: str | None = None   # rarely populated at list time
    needs_resolve: bool = True
```

Written to `jobs` with `stage='listed'`. `UNIQUE(source_key, external_id)` makes re-listing a
no-op via `INSERT … ON CONFLICT DO UPDATE` on the volatile fields only (`closes_at`, `title`),
preserving `first_seen_at`.

## 3. resolve — the CSV problem

**This is the stage that exists specifically because exports omit descriptions.**

Any stub with `needs_resolve=true` and an empty `description_raw` gets its detail page fetched
and parsed by `adapter.resolve(...)`. This is the only stage that makes one request per job, so
it is the expensive one and it is explicitly governed:

- `--max-resolve N` caps requests per run (default 1,500 across all sources).
- **Resolve order is prioritized, not FIFO.** Jobs are resolved cheapest-signal-first: stubs
  that already pass the deterministic prefilter on title/location/salary alone go first, then
  everything else. A state that dumps 4,000 postings in one day does not starve the other 49.
- Unresolved stubs stay queued at `stage='listed'` and are picked up next run. The console's
  Sources page shows the resolve backlog depth, so a permanently growing queue is visible.
- Closed postings (`closes_at` in the past) are never resolved. The description is moot.
- Resolution is cached permanently, so a job is resolved exactly once in its life.

Where the board offers a bulk export containing descriptions, the adapter sets
`needs_resolve=false` at list time and this stage skips it entirely. Where the export omits
them — the common case — the CSV supplies the stub set and `resolve` fills the gap. Either way
the invariant holds: **nothing reaches scoring without a description.**

## 4. normalize

Pure functions over cached bytes; no network. Safe to re-run over all history after a fix.

| Field | From | Notes |
|---|---|---|
| `description_text` | `description_raw` HTML | tags stripped, entities decoded, whitespace collapsed, lists preserved as `- ` lines |
| `salary_min`, `salary_max`, `salary_period`, `salary_currency` | `salary_raw` | handles `$5,432.00 - $7,310.00 Monthly`, `$85K–$110K`, `39.42/hr`, `DOE`, `Commensurate` → nulls with `salary_stated=false` |
| `state`, `city`, `county` | `location_raw` | state from the source row as fallback |
| `remote` | title + location + description | enum `onsite \| hybrid \| remote \| unknown` — cheap regex, the LLM corrects it later |
| `employment_type` | various | `full_time \| part_time \| temporary \| seasonal \| contract \| unknown` |
| `posted_at`, `closes_at` | raw dates | `dateparser` with the source's timezone; `posted_at_estimated` flag |
| `content_hash` | sha256 of normalized title+agency+location+description | drives exact dedupe |

Two rules keep the data honest: **never invent a value** (unparseable → `NULL` plus a
`parse_warnings` entry), and **never discard the raw** (every normalized field keeps its
`*_raw` source so a parser fix is always possible).

## 5. dedupe

States repost the same job across agencies, re-advertise after a failed search, and list one
opening in five counties. Without dedupe the inbox is unusable and the LLM bill multiplies.

Three passes, cheapest first:

1. **Natural key** — `(source_key, external_id)`. Already handled by the unique index.
2. **Exact content** — identical `content_hash` → same `job_group_id`.
3. **Near duplicate** — candidate pairs blocked on `(state, normalized_agency)` and compared
   with `rapidfuzz.token_set_ratio` on title plus a Jaccard check on description shingles.
   Threshold: title ≥ 92 **and** description ≥ 0.85, within a 90-day window.

Blocking on agency keeps this O(n·k) rather than O(n²) — critical at hundreds of thousands of
rows.

**Scoring operates on `job_group`, not `job`.** One LLM call covers all members; the console
shows "also posted in 4 other counties." Groups are advisory, never destructive: every original
row survives, and a group can be split from the console when the heuristic gets it wrong.

## 6. prefilter

Deterministic, cheap, and tuned for **recall over precision** — its job is to cut obvious
non-matches before paying for tokens, not to make judgment calls.

The rule that makes this safe: **reject only on facts explicitly present.** A job with no stated
salary is never rejected by a salary rule; it goes to the LLM. Silence is not evidence.

Rules come from `profile/preferences.yaml` hard constraints:

- state not in your allowed set (and not remote)
- stated salary max below your floor
- employment type in your exclusion list
- title matches an explicit exclusion pattern
- posting already closed
- a hard credential you do not hold *and the posting states it as required* (CDL, RN license,
  P.E., active clearance)

Every rejection writes a `prefilter_reason`, so the console can show you *why* a job was
dropped and you can audit the filter. A prefilter that cannot be audited drifts into silently
hiding good jobs.

Expected pass rate: 10–25% of resolved jobs.

## 7. score

[006](006-fit-scoring.md) in full. From the pipeline's view: survivors are submitted to the
Batch API, the run records the batch ID and exits, and a later invocation collects results.
Nightly timing: submit at 02:00, collect at 03:30 (most batches finish within an hour).

## 8. triage

No automation. Scored groups land in the console inbox sorted by score; you accept or reject.
Your decisions are written to `labels` and become the calibration set that
[006](006-fit-scoring.md#calibration) measures against. This is the loop that makes the scorer
get better rather than just confident.

## Failure handling

| Failure | Behavior |
|---|---|
| HTTP 429 / 503 | Exponential backoff with jitter, max 5 tries, then mark source `suspect` and continue to the next source |
| HTTP 403 / 401 | Stop the source immediately, mark `broken`, no retries — this is a block, not a blip |
| Parse error on one job | Log, store the raw, continue. One malformed posting never fails a source |
| Parse error on > 20% of a source's rows | Mark source `broken`, keep the cached HTML for diagnosis |
| Playwright timeout | Two retries, then mark that job `resolve_failed` and move on |
| Anthropic API error | Batch results are durable for 29 days; collection is retried on the next run |
| Spend cap hit | Scoring stops, survivors stay queued, console shows a banner |

A run never aborts because one source is broken. The exit code is non-zero if any source ended
non-`ok`, which is what the `systemd` `OnFailure=` hook reports on.
