# 001 — Goals & scope

## Problem

State government job boards are 50 separate, inconsistent systems. Finding relevant openings
means visiting dozens of sites, each with its own search UI, each with its own idea of what a
"category" is. Some offer CSV export, but the export omits the job description — the one field
that actually determines whether a job is worth applying to. Keyword search is a poor filter:
it misses jobs whose titles use state-specific classification language ("Information Technology
Specialist 4", "Program Analyst III") and floods you with jobs that happen to contain your
keywords.

## Goals

1. **Coverage** — one sweep touches all 50 states' primary job boards.
2. **Freshness** — pull postings within a configurable window (default: last 7 days), with
   per-source watermarks so repeat runs are cheap and incremental.
3. **Complete records** — every job in the database has a full description, even when the
   source's own export does not provide one.
4. **Real fit assessment** — an LLM reads the full description against your resume and stated
   preferences and produces a graded, *evidence-quoting* verdict. Not keyword overlap.
5. **Cheap at volume** — screening thousands of jobs per week must cost dollars, not hundreds
   of dollars.
6. **A console** — triage an inbox of scored jobs, then track applications from interest through
   offer or rejection, with follow-up reminders.
7. **Honest operations** — when a source silently breaks (and they will), the system says so
   rather than quietly reporting zero new jobs.

## Non-goals

| Not doing | Why |
|---|---|
| **Automatic application submission** | Violates the terms of nearly every board, and auto-filed applications are bad applications. The console helps you apply fast; you press the button. |
| Private-sector job boards (Indeed, LinkedIn, Greenhouse) | Different problem, aggressively anti-automation, and not what was asked. The adapter layer would accommodate them later. |
| Federal jobs (USAJOBS) | Out of scope as asked — though USAJOBS has a real, keyed, documented API and would be the single easiest source to add if you want it. (Its API returned 403 unauthenticated in my probe, as expected; it needs a free API key.) |
| County / city / school district jobs | Enormous long tail. The adapter layer supports it; the registry just would not list them. |
| Multi-user, hosted, or authenticated deployment | Single user, localhost, one SQLite file. |
| Resume generation | Scoring emits *tailoring hints* and gap lists, which feed your own editing. It does not write your resume. |
| Evasion of bot defenses | UA rotation, proxy pools, CAPTCHA solving: explicitly out. See [008](008-compliance.md). |

## Success criteria

The system is working when:

- A nightly run completes in under 30 minutes and reports per-source health.
- ≥ 45 of 50 states are in `ok` status, with the remainder *explicitly* `blocked` or `broken`
  rather than silently empty.
- 100% of jobs that reach the scoring stage have a non-empty description.
- On a hand-labeled calibration set of ≥ 150 jobs, the `strong` + `possible` verdicts capture
  ≥ 90% of jobs you actually labeled interesting (recall), while discarding ≥ 70% of the rest
  (precision proxy). These are the numbers [006](006-fit-scoring.md#calibration) measures.
- Daily LLM spend is visible in the console and under a configured cap.
