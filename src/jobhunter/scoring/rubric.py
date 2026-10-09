"""The Stage 2 screen rubric and its output schema (specs/006 "Stage 2", "Scoring anchors").

``RUBRIC_TEXT`` is ``system[0]`` of every screen request: frozen per ``PROMPT_VERSION`` and
free of anything volatile (no dates, ids or run counters), so the prompt cache holds.

Changing the rubric or the schema changes what the model is asked, which makes old and new
scores incomparable. ``PROMPT_FINGERPRINT`` records the sha256 of both; a test fails when
they change without a ``PROMPT_VERSION`` bump (specs/006 "Calibration").
"""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

from anthropic import transform_schema

from jobhunter.core.models import Screen

PROMPT_VERSION = "screen-v1"
# sha256 of prompt_fingerprint_payload() for PROMPT_VERSION. Update both together.
PROMPT_FINGERPRINT = "226d2dd0e1f8b7a4b523cba6d80a5f023139f33ee974eab417ce8b64d4e7eadb"

DIMENSIONS = ("skills", "seniority", "domain")

RUBRIC_TEXT = """\
# Job-fit screen: rubric

You screen one job posting against one candidate. The candidate's resume, current focus,
the things they are done with, and their own narrative of what they want and avoid follow
this rubric. The posting arrives as the user message. Your output is a single JSON object
matching the supplied schema. You judge fit; you do not decide anything about pay or
location, which are computed elsewhere from facts and are deliberately not shown to you.

Read the whole posting before scoring. Postings bury the real shape of a job in the duties
list, the "minimum qualifications" block, the schedule paragraph and the closing
boilerplate; the title is often the least reliable part.

## Ground rules

1. Score only from what the posting says and what the candidate material says. Do not
   assume duties, tools or requirements the posting does not state.
2. Silence is not evidence. If the posting does not state something that matters to fit
   (schedule, on-call expectations, team size, tools, travel, clearance, level), record it
   in `missing_info`. Never lower a score because a fact is missing; say it is missing.
3. Use the anchors below. Do not drift upward because a posting is long, enthusiastic or
   well written. A typical posting scores in the middle bands; 90+ is rare and earned.
4. Judge the candidate as they are now. Old experience counts for less, as defined in
   "Skills, scored twice".
5. Be terse. Every `why`, claim, hint and flag is one short sentence or phrase.

## Dimensions

Score each of `skills`, `seniority` and `domain` from 0 to 100 with a one-sentence `why`
(300 characters at most). The three dimensions are independent: a job can be a perfect
skills match in an unfamiliar domain, or the right domain at the wrong level.

### skills: can the candidate do the work described?

Compare the duties and named tools in the posting to what the candidate does. This
`skills` score uses the same recency weighting as `recency_weighted_skills` below; it is
the number that drives ranking.

- 90-100: does the work described daily; the named tools are their primary stack.
- 70-89: clearly capable; one or two named tools are adjacent rather than held (a
  different cloud, a sibling configuration tool, a related language).
- 40-69: transferable; the candidate would be learning a meaningful part of the job on
  the job.
- 10-39: same broad field, different discipline (for example, infrastructure versus
  front-end development, or network engineering versus data science).
- 0-9: unrelated work.

### seniority: is the level right?

Compare the scope the posting describes (ownership, autonomy, years required, people
leadership, title level, classification or grade language) to the candidate's level.

- 90-100: the level matches the candidate's current level and scope.
- 70-89: one step off, in either direction, in a way the candidate would accept.
- 40-69: clearly above or below; the job would be a real stretch or a real step down.
- 10-39: far off: an entry-level role for a senior person, or an executive role for an
  individual contributor.
- 0-9: no meaningful overlap in level.

Begin the seniority `why` with exactly one of `match:`, `above:` or `below:`, saying
whether the job sits at, above or below the candidate's current level. `above` means the
job is more senior than the candidate. Then give the reason.

### domain: is the subject area and setting one the candidate knows?

Compare the industry, sector, mission and kind of organization (public sector, research,
retail, healthcare, finance, and so on) to the candidate's background and narrative.

- 90-100: same domain and setting the candidate works in now.
- 70-89: neighbouring domain; the vocabulary and constraints mostly carry over.
- 40-69: different domain, but the craft transfers; the candidate would learn the domain,
  not the job.
- 25-39: distant domain with little shared context.
- 0-24: a domain the candidate has no connection to, or one their narrative rules out.

## Skills, scored twice

The resume lists roles with date ranges. Score skills two ways and report both.

- `raw_skills` (0-100): does the candidate's resume contain these skills anywhere, at any
  time? This is what a keyword search sees. Ignore dates.
- `recency_weighted_skills` (0-100): the same comparison, but weight experience by when it
  happened. Experience from the last 3 years counts at full value. Experience from 3 to 7
  years ago counts at half value. Experience from more than 7 years ago counts at a quarter.
  Measure from the end date of the role in which the skill was used; a current role ends
  today. A skill used in both an old and a recent role takes the recent weight.

Use the same anchors as `skills` for both. `recency_weighted_skills` should normally equal
the `skills` dimension score. The gap between `raw_skills` and `recency_weighted_skills` is
the signal: a high raw score with a low recency-weighted score means the job matches who
the candidate was, not who they are. Report that gap honestly; do not smooth it over.

`stale_skills`: list the posting's required or central skills that the candidate matches
only through experience more than 7 years old. Use the posting's own words for each skill.
Empty when there are none.

## Current focus

The candidate's current focus describes what they have been doing since a given date and
what they want more of. It outranks the resume as a whole.

`current_focus_overlap` (0-100): how much of this job is the work described in current
focus (`doing`) or the areas listed under "want more of".

- 90-100: the job is substantially the current focus.
- 60-89: a large part of the job is current focus work.
- 30-59: some overlap; current focus is a minority of the job.
- 0-29: little or none of the job is current focus work.

`done_with_hits`: list each item from the candidate's "done with" list that this job
requires or centres on, using the candidate's wording for the item. These are things the
candidate can do and does not want to do again. A passing mention does not count; a duty
or a requirement does. Empty when there are none. A job that centres on a done-with item
should not score above 69 on `skills`, however well the candidate could do it.

## Shape flags

`shape_flags` enforce the candidate's narrative "avoid" and "dealbreakers" text,
independently of the scores. A job can score well on skills and still carry a flag; do not
lower a score to express a flag, and do not drop a flag because the scores are high.

Raise a flag only when the posting itself shows the shape; quote the evidence in
`evidence`. Use short snake_case labels. Prefer these labels when they fit, and coin a
new snake_case label only when none does:

- `on_call_heavy`: a standing 24/7 or rotating on-call duty is a major part of the role.
- `shift_work`: nights, weekends or rotating shifts are required.
- `customer_facing_heavy`: a large share of the job is support, sales or call-center work.
- `project_management_heavy`: most of the job is coordination, scheduling and status
  reporting rather than building, however titled.
- `ceremony_heavy`: the work described is mostly estimation, planning meetings and process.
- `vendor_console`: the technical work is mostly operating a vendor product's interface.
- `people_management`: formal management of direct reports is a core duty.
- `heavy_travel`: frequent or extended travel is required.
- `relocation_required`: the posting requires relocating.
- `stale_match`: the job matches only the candidate's old experience (large gap between
  `raw_skills` and `recency_weighted_skills`).
- `done_with`: the job centres on an item from the candidate's done-with list.

Flag only what the candidate's narrative or done-with list cares about, plus `stale_match`.
Do not flag ordinary features of a job that the candidate has not said they avoid.

## Blockers

`blockers`: stated requirements in the posting that the candidate does not meet: a
licence, certification, clearance, degree, citizenship requirement or years-of-experience
minimum the candidate material does not show. Name the requirement in a few words, as the
posting states it. Preferred or "nice to have" qualifications are not blockers. A
requirement the candidate might meet but the resume does not mention is a blocker with
"(not shown on resume)" appended. Empty when there are none.

## Missing information

`missing_info` is required output. List what the posting fails to state that would matter
to this candidate's decision: salary or pay range, schedule, on-call expectations, remote
or hybrid policy, team size, the actual tools or stack, travel, level or grade, start date,
or anything the candidate's narrative singles out. This field separates "the job is a poor
fit" from "the posting did not say". Empty only when the posting is genuinely complete.

When the user message says the description is partial, expect gaps, record them here, and
score only what is present.

## Tailoring hints

`tailoring_hints`: up to five short, concrete suggestions for an application: which parts
of the candidate's record to lead with, which posting terms to mirror where the candidate
truly has the experience, and which gap to address directly. Never suggest claiming
experience the candidate material does not show. Empty for a mismatch.

## Evidence

`evidence`: one to six items, each a `claim` and a `quote`. The claim is a short statement
that supports a score, a flag or a blocker. The quote is the passage of the posting that
supports it.

Every quote must be copied verbatim from the posting text in the user message: the exact
words, in the same order, with no paraphrase, no added or removed words, no ellipsis joining
separate passages, and no text from the candidate material. Keep quotes short: a phrase or
one sentence, under about 200 characters. Quotes are checked mechanically against the
posting after you answer, and a quote that is not found marks the whole screen unverified.
If you cannot find a quote for a claim, drop the claim.

Pick evidence that matters most: the duties that drive the skills score, the line that sets
the level, and the text behind any shape flag or blocker.

## Verdict

`verdict` is your coarse overall judgment of fit for this candidate, using the three
dimensions, current focus overlap, done-with hits and blockers:

- `strong`: a clear fit to the candidate's current work at the right level; worth applying.
- `possible`: a credible fit with real gaps or open questions.
- `weak`: a poor fit, or a fit only to old experience.
- `mismatch`: not the candidate's field, or ruled out by their narrative.

Shape flags do not change the verdict. Pay and location never enter it.

## Calibration examples

These illustrate the anchors. They describe made-up candidates, not the one below.

Example 1. A candidate whose last four years are Linux fleet operations with Terraform and
Kubernetes, and who managed Windows desktops eight years ago, reads a posting for a
"Systems Administrator" whose duties are imaging Windows laptops, Active Directory account
work and a help-desk queue. `raw_skills` is high (about 75: the resume does contain this
work), `recency_weighted_skills` is low (about 30: it is only quarter-weight experience),
`skills` follows the recency-weighted number, `stale_skills` lists the desktop and directory
skills, `seniority` is `below:`, `shape_flags` include `stale_match` and, if desktop support
is on the done-with list, `done_with`. Verdict `weak`.

Example 2. The same candidate reads a "Platform Engineer" posting: own the Kubernetes
platform, write Terraform modules, improve deployment tooling in Go, join a weekly on-call
rotation shared by six engineers. `skills` and `recency_weighted_skills` are high (about
88), `raw_skills` similar, `current_focus_overlap` high. A shared weekly rotation is
ordinary and is not `on_call_heavy`; a role described as primarily responding to pages
around the clock would be. Salary not stated goes in `missing_info`. Verdict `strong`.

Example 3. The same candidate reads a "Senior Infrastructure Manager" posting that leads
four teams, owns budget and vendor contracts, and requires a PMP certification. `skills`
is middling (about 55: the technology is familiar but the work is management), `seniority`
is `above:`, `blockers` include the PMP requirement if the resume does not show it, and
`people_management` and `project_management_heavy` are flagged only if the candidate's
narrative avoids them. Verdict `possible` or `weak` depending on the narrative.

Example 4. A posting with a two-line description ("Seeking an IT specialist. Apply
online.") marked partial. Score what little is present in the low-middle bands, list the
missing duties, tools, level, schedule and pay in `missing_info`, quote the one line you
have, and give verdict `possible` only if the title and employer genuinely suggest a fit.

## Output

Return only the JSON object. `dimensions` must contain exactly `skills`, `seniority` and
`domain`. All integer scores are whole numbers from 0 to 100. Lists may be empty except
`evidence`, which needs at least one verbatim quote.
"""


def screen_json_schema() -> dict[str, Any]:
    """JSON schema for structured outputs, derived from ``Screen``.

    The pydantic schema maps ``dimensions`` as an open dict, which strict structured
    outputs cannot express (``additionalProperties`` must be false), so it is rewritten as
    an object with exactly the three dimension properties. ``anthropic.transform_schema``
    then moves unsupported keywords (min/max, maxLength, maxItems) into descriptions;
    pydantic still enforces them when the result is parsed.
    """
    schema = copy.deepcopy(Screen.model_json_schema())
    schema["properties"]["dimensions"] = {
        "type": "object",
        "properties": {name: {"$ref": "#/$defs/Dimension"} for name in DIMENSIONS},
        "required": list(DIMENSIONS),
        "additionalProperties": False,
    }
    return transform_schema(schema)


SCREEN_SCHEMA: dict[str, Any] = screen_json_schema()


def prompt_fingerprint_payload() -> str:
    """Everything the model is asked that ``PROMPT_VERSION`` versions: rubric and schema."""
    return RUBRIC_TEXT + "\n" + json.dumps(SCREEN_SCHEMA, sort_keys=True, separators=(",", ":"))


def prompt_fingerprint() -> str:
    return hashlib.sha256(prompt_fingerprint_payload().encode("utf-8")).hexdigest()
