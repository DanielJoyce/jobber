# Costs and credits

Only a few actions spend money. Most are held to the caps below; the exceptions are listed
under them. Nothing starts a paid action without you having set it up or confirmed it. The `/costs` page in the console shows what was spent and
how much of each cap is left.

## What spends money, and what never does

Spends API credits (or OpenRouter credits):

- the nightly score stage of `jobhunter run`
- a re-score you confirm in the console (`/prefs`, or **Save + re-score** on `/models`), or
  `jobhunter score --rescore`
- **Score this group now** on a single job in the console, and scoring a group from the
  browser extension
- `jobhunter score --submit` and `--group`
- `jobhunter backfill`, which scores older jobs under its own budget (see the caps)
- the deep pass: **Deep pass** on Today or a job page in the console, or
  `jobhunter score --deep 10` and `--deep-group`
- a draft made with the `api` runner (see [Drafting](drafting.md))

Never spends: opening `/setup`, saving a model on `/models`, `jobhunter doctor`, the free live checks (`doctor --live`
and the **Test (free)** buttons), reading this help, browsing the inbox,
editing free preferences (see [Profile and resume](profile-and-resume.md)), and drafting with
the `cli` runner on your subscription. A paid `llm bench` is the exception, below.

### Changing a model and re-running

Saving a new screening or deep model on `/models` spends nothing: new jobs from the next run use
it, and jobs already scored keep their scores. To score them again with the new model now, use
**Save + re-score** on the screening row. It saves, then shows the re-score panel for that model
(how many job groups, the estimate, what is left of the cap) and runs nothing until you press
**Re-score now** and confirm. The run is the plan you confirmed: if new jobs arrived, the cap
no longer covers the estimate, or the screening model was changed again in the meantime, nothing
runs and the panel shows the new numbers. The cap applies as for any re-score, and one re-score runs at a time, so
a second click does not start a second run. Cancel in the confirm only cancels the re-score; the
model stays saved.

**Save + deep-score** on the deep row saves and then opens the deep-pass panel with its
estimate and confirm (below). Both buttons work only in a browser on the machine running the
console, and are off under `jobhunter console --allow-remote`; there, run
`jobhunter score --rescore recent` or `jobhunter score --deep N` instead.

## The caps

```toml
[scoring]
daily_cap_usd = 2.0
weekly_cap_usd = 10.0

[apply]
daily_cap_usd = 1.00
```

- `[scoring] daily_cap_usd` and `weekly_cap_usd` cap screening and the deep pass together.
  Scoring runs check the tighter of the two (today's spend, and the last seven days).
- `[apply] daily_cap_usd` is separate and caps drafting on the `api` runner only. Setting it to
  `0` refuses every paid draft.
- At a cap the run stops scoring, says `spend cap reached`, and carries on with everything
  else. A cap reached is a note, not a failure. The values above are the defaults; change
  them in `config.toml` and restart the console. `/prefs` shows the scoring caps.

### Where the caps do not apply

- `jobhunter backfill` has its own ceiling, `--budget-usd` (default: the weekly cap). It
  replaces the daily cap for that command only.
- `jobhunter llm bench` with a paid `--scorer` calls the scorer but records nothing, so it is
  counted against no cap and does not show on `/costs`. Only `--n` limits it.
- Collecting a batch (`score --collect-pending`) spends nothing new: the batch was already paid
  for and reserved when submitted.

## Reservations

A batch is paid for when it is collected, hours after it is submitted. So a submitted but
uncollected batch is counted against the cap straight away, at an estimate per request, and
replaced by the real cost when the batch is collected. This is what stops two runs in a row
from each thinking the whole budget is free.

Every screening model holds its own price against the cap, per job: the highest of a flat
$0.002, the average it has actually cost you, and its list price for a typical job (a batched
Anthropic model at its batch price). So Haiku 5.5 holds $0.002 a job (its real cost is about
a tenth of that), and Sonnet 5.5 about $0.005 a job in a batch once it has been measured.

A model you have not used yet has no average, and the real cost of its first batch depends on
how often the batch reuses the cached part of the prompt (your rubric and profile), which the
batch service does not promise. So until its first batch is collected, an Anthropic model is
held at its price with none of that cached part reused, sized from your own profile: about
$0.012 to $0.014 a job for Sonnet 5.5. After the first collect, the measured average takes
over.

The same figure decides how many jobs a run may send: a batch or a run is cut to what is left
of the cap at that figure, and a single job is sent only when one whole job fits. When what
the cap holds is more than the estimate, the Models page, the re-score panel and Score now
say so, and the re-score panel says how many jobs fit under the cap now. This is why a dearer
screening model, such as Sonnet 5.5, is offered on [Models](/models) without **Save anyway**.

What this does and does not promise: a run never sends more jobs than the cap covers at the
figure it holds. The hold is an estimate, not a bill: a batch of unusually long postings can
still cost more than was held, and the cap then shows it the next time anything is scored.
The cap stops new work; it does not cancel a batch already sent.

Batches already submitted stay held at the figure they were sent with until they are
collected; collecting one replaces its hold with what it really cost.

A model the price table does not list is refused for `anthropic:` specs, because its cost
could not be capped. Other scorers report their own costs; see
[OpenRouter and Jev](openrouter.md) for how.

## Prices

Per million tokens, from the table the caps use. A batch is half price.

<!-- jh:begin model-prices -->

| Anthropic model | Input per 1M tokens | Output per 1M tokens | In a batch (in / out) |
|---|---|---|---|
| `claude-haiku-4-5` | $1.00 | $5.00 | $0.50 / $2.50 |
| `claude-haiku-5-5` | $0.10 | $0.50 | $0.05 / $0.25 |
| `claude-opus-4-8` | $5.00 | $25.00 | $2.50 / $12.50 |
| `claude-opus-5` | $5.00 | $25.00 | $2.50 / $12.50 |
| `claude-opus-5-5` | $4.00 | $20.00 | $2.00 / $10.00 |
| `claude-sonnet-5` | $2.00 | $10.00 | $1.00 / $5.00 |
| `claude-sonnet-5-5` | $2.00 | $10.00 | $1.00 / $5.00 |

<!-- jh:end model-prices -->

`claude-haiku-5-5` is dearer for a very long prompt (over 100,000 tokens); the cap code applies
that rate. Cached prompt reads are cheaper, which is why a run of many jobs costs less per job
than one job alone.

## The deep pass

The deep pass reads the full posting for your top screened jobs and writes a report, on
`deep_scorer` (an `anthropic:` model). It is the dear one: roughly 5 cents per job on the
default model, against a small fraction of a cent for screening. It runs only when you ask.

**In the console.** Press **Deep-score top N** on Today (the Inbox's **Deep pass** link goes
there), **Deep-score…** under the Deep pass row on [Models](/models), or **Deep-score this job**
on a job page (the Inbox's `d` key opens it). The panel says
plainly that this may use more money than screening, and shows, before anything is spent:

- which jobs it would deep-score: the top N by screen score with no deep report yet, or (Re-run
  my top groups) those whose report came from another model;
- the estimate (per job from your measured deep runs on that model, else the price table,
  times the jobs listed), the worst case for one job, and the ceiling;
- what is left of today's and this week's cap, and how many of the N fit under it now.

Opening the panel or changing N spends nothing. Only **Deep-score N jobs, up to $X**, then OK
in the confirm box, starts a run. If the plan changed in the meantime (new scores, a profile
or model change), nothing runs and the panel shows the new plan. A run goes on in the
background while you browse; the activity band shows "Deep pass 3/10", then the result. Cancel
stops it after the call in flight. A second click while one runs only shows its progress.

The button appears only in a browser on the machine running the console. Opened from anywhere
else, the panel gives the command instead. A deep run takes the run lock, so it does not start
while the nightly run, a re-score or another `jobhunter score` holds it.

**From a terminal:**

```bash
jobhunter score --deep 10
jobhunter score --deep-group 1234
```

**The worst case.** One call can produce up to 32,000 tokens, and a model that falls back to
another can bill at the dearer model's rates: about $0.83 on the default model. So a deep call
starts only when what is left of the scoring cap still covers that worst case, in the console
and in the CLI alike; otherwise it stops with `spend cap reached`, and the message names the cap
that binds (daily or weekly) and what to change. When it stops before deep-scoring anything,
`jobhunter score --deep` exits with status 1. The
last ~$0.83 of a day's cap therefore goes unused by deep runs, and **with a daily cap below
about $0.83 the deep pass cannot run at all**: raise `[scoring] daily_cap_usd` if you want it.
A console run also has a ceiling, the estimate plus 25% plus one worst-case call, and starts no
call that could pass it.

**When a call's cost is counted.** A call's cost is written to your spend the moment its answer
arrives, before anything is parsed or saved. A refused or cut-off answer, output that does not
parse, or a save that fails all still count against the cap, once each. The one gap: a call
still in flight when the console or command is stopped (killed, crashed, the machine slept) is
not counted, because its cost arrives only with its answer. Anthropic may still bill it, up to
the worst case above for a deep call; the Anthropic Console's usage page shows what was billed.

## Credits are separate accounts

Anthropic API credits, OpenRouter credits and a claude.ai subscription are three separate
things. Running out of one never draws on another. Set a spending limit on the provider's own
site as well; jobhunter's caps are a second line, not the only one.
