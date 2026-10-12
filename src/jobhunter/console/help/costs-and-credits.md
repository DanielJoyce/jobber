# Costs and credits

Only a few actions spend money. Most are held to the caps below; the exceptions are listed
under them. Nothing starts a paid action without you having set it up or confirmed it. The `/costs` page in the console shows what was spent and
how much of each cap is left.

## What spends money, and what never does

Spends API credits (or OpenRouter credits):

- the nightly score stage of `jobhunter run`
- a re-score you confirm in the console (`/prefs`), or `jobhunter score --rescore`
- **Score this group now** on a single job in the console, and scoring a group from the
  browser extension
- `jobhunter score --submit` and `--group`
- `jobhunter backfill`, which scores older jobs under its own budget (see the caps)
- the deep pass: `jobhunter score --deep 10` or `--deep-group`
- a draft made with the `api` runner (see [Drafting](drafting.md))

Never spends: opening `/setup`, `jobhunter doctor`, reading this help, browsing the inbox,
editing free preferences (see [Profile and resume](profile-and-resume.md)), and drafting with
the `cli` runner on your subscription. A paid `llm bench` is the exception, below.

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

For a batched Anthropic model the estimate is the model's batch price for a typical request,
never below a flat $0.002 a job. A model the price table does
not list is refused for `anthropic:` specs, because its cost could not be capped. Other
scorers report their own costs; see [OpenRouter and Jev](openrouter.md) for how.

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
default model, against a small fraction of a cent for screening. It runs only when you ask:

```bash
jobhunter score --deep 10
```

Before each job it checks that at least $0.08 of the scoring cap is left, and stops with
`daily spend cap reached` otherwise. Admission is deliberately stricter than the average: one
call can produce up to 32,000 tokens, and a model that falls back to another can bill at the
dearer model's rates. The design in specs/020 (model selection) will admit a deep call only when
the cap still covers its worst case, which is about 85 cents on the default model, and the
last part of a day's cap then goes unused by deep runs.

## Credits are separate accounts

Anthropic API credits, OpenRouter credits and a claude.ai subscription are three separate
things. Running out of one never draws on another. Set a spending limit on the provider's own
site as well; jobhunter's caps are a second line, not the only one.
