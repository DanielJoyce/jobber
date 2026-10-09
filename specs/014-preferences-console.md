# 014 — Preferences console

`/prefs` lets you change salary floors, states, weights, queries, and everything else in your
profile from the console, without hand-editing YAML. The page shows what a change will cost
before you save it.

## Source of truth stays a file

`profile/preferences.yaml` remains the source of truth. You can still edit it by hand and diff
it, and it's what the pipeline reads. The page reads and writes that file:

- **Round-trip editing with `ruamel.yaml`**, so your comments and ordering survive a save from
  the UI.
- **Atomic writes:** write a temp file, validate, then rename. A crash can't leave half a file.
- **Validation** through the same pydantic model the pipeline uses. Errors show inline next to
  the field, and nothing is saved until the whole profile validates.
- **External edits are detected.** If the file changed on disk since the page loaded, saving is
  refused and the page offers to reload, so a hand edit is never silently overwritten.

## The design change: free settings vs. paid settings

Changing a salary floor should be instant and cost nothing. In the original scoring design
([006](006-fit-scoring.md)) it wouldn't have been: the model scored `comp` and `location` itself,
so any edit to salary or states meant paying to re-score.

So **dimensions that are facts are now computed in Python, and the model only judges.**

| Dimension | Computed by | Inputs |
|---|---|---|
| `skills` (raw + recency-weighted) | **model** | resume, `current_focus`, posting |
| `seniority` | **model** | resume, posting |
| `domain` | **model** | resume, `narrative`, posting |
| `comp` | **Python** | parsed salary vs `salary_floor` and target; `null` when salary isn't stated |
| `location` | **Python** | `job_locations`, `location_scope`, `state_ranking`, `remote_bonus`, `states_excluded` |

That splits the profile hash in two:

- **`filter_version`:** hard constraints, salary floor, states, ranking, weights, bucket
  thresholds, title exclusions. Changing any of these **re-applies instantly, for free**:
  prefilter, `comp`, `location`, `overall` and buckets are all recomputed in Python from scores
  already stored.
- **`scoring_version`:** resume, `current_focus`, `done_with`, `narrative`. These are what the
  model reads. Changing one **costs money** to apply to existing jobs, so it never happens
  automatically.

`fit_score` is keyed on `scoring_version`. Buckets are recomputed on read from the stored
model dimensions plus the current filter settings.

## Page layout

Sections in the order you're likely to touch them:

| Section | Fields | Kind |
|---|---|---|
| **Pay** | Salary floor, target salary, period; "hide jobs with no stated salary" toggle (off by default) | free |
| **Where** | Map picker: click states to rank, exclude, or leave neutral. Remote OK, remote bonus, relocation OK | free |
| **What** | Target titles, title exclusions, employment types excluded, credentials you lack | free |
| **Weights** | Sliders for skills / seniority / domain / comp / location, normalized to 1.0 | free |
| **Buckets** | Thresholds for A–G | free |
| **Search queries** | Stage 0 keywords and occupation codes, one per line | free; affects the next run |
| **Spend** | Daily and weekly LLM caps; Stage 3 shortlist size | free |
| **Recency** | `current_focus`: since date, what you're doing, want more of, `done_with` | **paid** |
| **Narrative** | `want`, `avoid`, `dealbreakers_soft`, `context` (free-text boxes) | **paid** |
| **Resume** | Path, last modified, hash, and a "changed since last scoring" warning | **paid** |

The **Where** picker reuses the dashboard map ([013](013-dashboard.md)): click cycles a state
through neutral → ranked → excluded, and ranked states show their rank number.

Paid sections are visually separated and marked as costing money to re-apply.

## Live preview

Every free change shows its effect **before** saving, recomputed on the stored scores (usually
well under a second):

```
Salary floor  $140,000 → $125,000

  Preview against the last 30 days (1,284 scored jobs):
    Bucket A   6 → 9     (+3)
    Bucket B  23 → 31    (+8)
    Filtered out by salary   212 → 141   (−71)
    [show the 11 jobs that would move into A/B]

                                           [ Cancel ]  [ Save — free ]
```

Paid changes show an estimate instead:

```
done_with: added "Java enterprise"

  Applies to new jobs automatically from the next run.
  Re-score existing open jobs too?
    ( ) No, new jobs only                                    $0.00
    (•) Open A–E jobs from the last 14 days   (312 jobs)    ≈ $0.62
    ( ) All open jobs                         (2,410 jobs)  ≈ $4.80

                                           [ Cancel ]  [ Save ]
```

The estimate uses the measured cost per job from `llm_spend`, not the spec's assumed figure.
Re-scoring goes through the Batch API like everything else and counts against the spend cap.

## History and undo

Every save appends to a `profile_change` table: timestamp, field path, old value, new value, and
both version hashes. The page shows a change log, and any entry can be reverted (a revert is
itself a new entry).

The log also matters for calibration ([006](006-fit-scoring.md#calibration)). When the bucket
mix shifts, `eval` can tell whether the scorer drifted or you lowered your salary floor on
Tuesday.

```sql
CREATE TABLE profile_change (
  id               INTEGER PRIMARY KEY,
  at               TEXT NOT NULL,
  field_path       TEXT NOT NULL,       -- e.g. "hard.salary_floor.amount"
  old_value        TEXT,                -- JSON
  new_value        TEXT,                -- JSON
  filter_version   TEXT NOT NULL,
  scoring_version  TEXT NOT NULL,
  source           TEXT NOT NULL        -- ui | file | revert
);
```

Edits made by hand in the file are detected on the next run or page load, diffed against the
last known version, and logged with `source = 'file'`.

## Milestone placement

**M5** with the rest of the console. The free/paid split in scoring lands in **M4**, because
retrofitting it after the first scores exist would mean re-scoring everything.
