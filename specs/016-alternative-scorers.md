# 016 — Alternative scorers: OpenRouter, Jev, and local models

Researched 2026-10-09 from OpenRouter's live catalog (`/api/v1/models`, 458 models) and this
machine's hardware. Prices move; the method below matters more than the exact numbers.

## Ground rules

1. **Measure before switching.** Any scorer is adopted only after `jobhunter eval --compare`
   on your own labeled jobs shows it holds **recall ≥ 0.90** on buckets A+B
   ([006](006-fit-scoring.md#calibration)). Cheap and wrong is not cheap.
2. **One scorer, one model per comparison.** Calibration compares fixed models. Anything that
   picks a different model per request has to record which model actually answered, or
   its scores can't be compared.
3. **Your resume goes wherever the scorer runs** ([008](008-compliance.md#personal-data)). That
   makes data-retention policy a selection criterion, not a footnote.

## Cost per 1,000 jobs screened

Assumes ~5,000 input tokens (rubric + profile + posting) and ~400 output, with **no caching
and no batch discount**. Anthropic direct with Batch API + caching is shown for reference.

| Option | ~$/1,000 jobs | Notes |
|---|---:|---|
| Claude Haiku 4.5, direct API, batch + cache | **≈ 2.00** | Current default ([006](006-fit-scoring.md#cost)) |
| Claude Haiku 4.5 via OpenRouter | 7.00 | No batch discount, so direct is cheaper |
| `openai/gpt-oss-120b` (batch) | 0.20 | Open-weight 120B MoE |
| `openai/gpt-oss-20b` | 0.13 | Open-weight 21B MoE, 3.6B active. **Also runnable locally** |
| `qwen/qwen3.7-flash` | 0.20 | 1M context |
| `google/gemma-4-26b-a4b-it` | free tier | 25B MoE, 3.8B active. **Also runnable locally** |
| `nvidia/nemotron-3-super-120b-a12b` | free tier | Too large to run locally |
| `ibm-granite/granite-4.0-h-micro` | 0.13 | 3B. **Fits entirely on your GPU** |
| `liquid/lfm-2.5-2.6b` | free tier | 2.6B "compact reasoning". **Fits on your GPU** |
| `typesafe/jev-router` | variable | See below |

Free tiers on OpenRouter are rate-limited and some providers log prompts. With a resume in
every request, set provider preferences to deny data collection (below) or skip free tiers.

## Jev Router

`typesafe/jev-router` is a **router**: it picks a model and reasoning effort for each request,
"balancing quality, speed, and cost," running on TypeSafe's Jev system. It is listed with
variable pricing (`-1`) and supports structured outputs.

How it fits here:

- **As the primary scorer: not recommended.** It's deliberately non-stationary. The same
  posting can be judged by different models on different days, which breaks calibration
  (rule 2) and makes the daily spend cap hard to predict.
- **As one arm of `eval --compare`: worth one run.** Score the labeled set once through Jev,
  recording the served model returned in each OpenRouter response, and compare it to Haiku. If
  it routes most jobs to a cheap model and still holds recall, that's real information. In
  particular it would tell us which cheap model to pin directly.

### Packed requests

> **Why this exists.** Packing was built on the assumption that `typesafe/jev-router`
> was a chat model priced at roughly $0.04 per request, so several judgments per request
> would make the backlog affordable. Testing showed otherwise: Jev is a *decisions* model
> (`/api/alpha/decisions`, priced per input token at about $0.042 per million), handled by
> the dedicated Jev decisions scorer, which batches through its own state and question
> format. Packing is kept because it still helps chat-completion scorers whose price is per
> request or whose prompt prefix dominates the cost: the local llama.cpp model (one shared
> rubric and profile per pack instead of per job) and any future OpenRouter chat model.
> Measure with `eval --compare` before relying on it, since packing can affect judgment
> quality.

Jev bills about $0.04 per request whatever its size, so judge several jobs per request:
`jobs_per_request` under `[scoring.openrouter]`, `[scoring.openai_compat]` or `[scoring.local]`
(default 1, hard cap 16), or `--jobs-per-request N` on `jobhunter score --submit` and
`jobhunter llm bench`. With N = 8 the roughly 2,200 eligible jobs take about 275 requests
(about $11 at $0.04, so lower N or the cap if the budget is $10; N = 10 is about $9).

- **Request.** `system` is the unchanged rubric + profile text, so the cached prefix is
  identical to single mode. One user message holds N postings, each between
  `BEGIN POSTING <custom_id>` and `END POSTING <custom_id>` lines, with an instruction to judge
  each independently from its own text only. The reply schema is
  `{"judgments": [Screen + custom_id]}` with `minItems = maxItems = N` (strict JSON schema when
  the server accepts it, else the usual instructed-JSON fallback). A pack is also split when its
  estimated input (characters / 4) would pass `max_input_tokens_per_request` (default 40,000).
- **Parsing.** Items are mapped by `custom_id` and each is validated against `Screen` on its
  own. A missing, duplicated, unknown-id or invalid item leaves only that job eligible; the rest
  are written. Evidence quotes are verified against that job's own posting text only, so a
  quote copied from a neighbor fails and marks the score `evidence_unverified`.
- **Cost.** The request's total cost (`usage.cost` when OpenRouter reports it) is split evenly
  over the jobs in the pack for `fit_score.cost_usd`; `llm_spend` records the true total with
  one call per request. The served model is stored on every row. The daily cap is checked
  before each wave of requests against the mean cost per request measured so far (today, else
  history), else `est_cost_per_request_usd` (default 0.04).
- **Order.** Eligible jobs are packed in group-id order.
- **Quality.** Packing can change scores: compare with `eval --compare` against one job per
  request before trusting it. `llm bench --jobs-per-request N` reports seconds and cost per
  job next to the per-request figures.

## Running locally on this machine

Hardware: **Intel i7-10850H** (6 cores / 12 threads, AVX2, no AVX-512), **31 GB RAM** (about
9.5 GB free while the dev servers run), **GTX 1650 Ti Mobile, 4 GB VRAM** (Turing; CUDA and
Vulkan both work).

The GPU is old, but it isn't useless: 4 GB holds a **3B model at 4-bit quantization
entirely**, and prompt processing on any GPU is far faster than on this CPU.

| Model | Quantized size | Where it runs | Expected speed (rough) | Fit for this job |
|---|---|---|---|---|
| Granite 4.0 H Micro (3B) | ~2 GB Q4 | **GPU** | Fast: roughly 10–20 s per job | Quick triage only. Unproven on nuanced fit |
| LFM 2.5 (2.6B) | ~1.7 GB Q4 | **GPU** | Fast, similar | Same |
| gpt-oss-20b (21B MoE, 3.6B active) | ~12–13 GB | CPU (+ partial GPU offload) | Roughly 30–90 s per job | Plausible full screen, **overnight** |
| Gemma 4 26B-A4B (MoE, 3.8B active) | ~15 GB Q4 | CPU (+ partial offload) | Similar to gpt-oss-20b | Plausible full screen, overnight |
| Anything dense ≥ 24B | 14 GB+ | CPU only | Minutes per job | Too slow |

The speeds are order-of-magnitude estimates, not measurements, and the honest bottleneck is
**prompt processing**: ~5,000 input tokens per job on a 6-core laptop CPU. Two things help a
lot:

- **Prefix caching.** The rubric + profile (~3,000 tokens) is identical for every job. llama.cpp's
  server reuses that cached prefix, so each job only processes its ~2,000-token posting.
- **Mixture-of-experts.** gpt-oss-20b and Gemma 4 26B-A4B only compute ~4B parameters per token,
  which is why they're viable on CPU at all.

At ~30 s per job, 300 jobs is about 2.5 hours, which is fine as a nightly batch. Free RAM is the
constraint for the MoE models: they need ~13–16 GB, so the console and other memory-heavy
processes should be closed while scoring runs.

### Runtime

Both of these expose an **OpenAI-compatible `/v1/chat/completions` endpoint with JSON-schema
constrained output**, so the `openai-compat` scorer being built
([006](006-fit-scoring.md#the-scorer-is-pluggable)) works against them unchanged:

- **llama.cpp server** (`llama-server`): smallest footprint, best prefix caching, CUDA or Vulkan.
- **Ollama**: easiest model management, bundles CUDA.

Either is a new install on your machine, so it's your call. Nothing has been installed.

### Local server security

The resume and postings sit in the server's prompt cache and request log, and any web page open
in the user's browser can reach `127.0.0.1:8080`. Three controls, in order of weight:

- **API key.** `scripts/setup-local-llm.sh` generates one random 32-byte key once, stores it as
  `LLAMA_API_KEY` in `~/.env` (mode 600) after a y/N, and never prints it. The server starts with
  `--api-key "$LLAMA_API_KEY"`. The local scorer and `jobhunter llm status`/`bench` send
  `Authorization: Bearer <key>` when `[scoring.local] api_key_env` (default `LLAMA_API_KEY`) is set
  in the environment; with it unset they send no header, which Ollama and key-less servers need. A
  401 gives a message that names the variable to set. Known gap: `--api-key` puts the key in the
  process argument list, visible to other users on this machine; `--api-key-file` avoids that and
  is the follow-up if this machine is ever shared.
- **`--no-slots`.** `/slots` exposes cached prompts, which hold the resume. It is disabled.
- **CORS, left at llama-server's default (`--cors-origins *`), on purpose.** A browser page on
  another origin can send requests to the server, but it cannot read the answers without the key,
  and no web page has the key. Restricting origins would not stop a non-browser client, and the key
  is the control that matters. Verify the routes on your build with `llama-server --help`.

### Running a local model

Nothing is installed until you say so. Every step below prints its exact command and waits for
a `y`; `--dry-run` prints the whole plan and runs nothing.

1. `scripts/setup-local-llm.sh --dry-run` detects `llama-server`, `ollama`, the GPU and its
   VRAM, free RAM and AVX2, recommends a runtime and model for this machine (a 3B model fully on
   the GPU, or a 20-26B MoE on the CPU when at least 16 GB of RAM is free), and prints download
   sizes, disk and RAM needs and the `llama-server` command line (loopback only, `--ctx-size
   8192`, `--threads 6`, `--cache-reuse 256`, `--jinja`, GPU layers sized for 4 GB). Drop
   `--dry-run` to be asked before the install and before the model download.
2. Set `screen_scorer = "local:<model>"` and `[scoring.local] runtime = "llama.cpp"` (or
   `"ollama"`). The preset is `openai-compat` against `http://127.0.0.1:8080` (llama.cpp) or
   `http://127.0.0.1:11434/v1` (Ollama): cost 0, one request at a time, a 15-minute timeout, and
   a notice that the resume and postings stay on this machine.
3. `jobhunter llm status` checks the server is up and lists loaded models.
4. `jobhunter llm bench --scorer local:<model> --n 10` scores ingested jobs **without writing
   `fit_score` rows** and reports seconds per job (first vs later, showing the prefix cache),
   tokens/s when the server reports them, schema-valid rate, evidence-quote verification rate,
   and jobs per night. Use `--scorer openrouter:<slug>` or `anthropic:<model>` for the same
   measurement on a cloud scorer.
5. Adopt it only if `jobhunter eval --compare` holds recall >= 0.90 (ground rule 1).

## Proposed rollout

1. **OpenRouter support in the `openai-compat` scorer** (small): base URL
   `https://openrouter.ai/api/v1`, `OPENROUTER_API_KEY` from `~/.env`, provider preferences
   `{"require_parameters": true, "data_collection": "deny"}` so structured output is enforced and
   providers that retain prompts are excluded, and the **served model recorded** in `fit_score`.
2. **Label ~150 jobs** with Haiku as the reference scorer ([006](006-fit-scoring.md#calibration)).
3. **One `eval --compare` run** over those labels: Haiku vs `gpt-oss-120b` vs `gpt-oss-20b` vs
   `gemma-4-26b-a4b` vs Jev. Total cost well under a dollar.
4. **Then decide, on the numbers:**
   - If a cheap cloud model holds recall, make it the screen scorer and keep Opus for the deep pass.
   - If gpt-oss-20b or Gemma 4 holds recall, run it locally overnight for $0. Install
     llama.cpp or Ollama at that point, with your OK.
   - A **cascade** (a 3B model on the GPU discarding obvious mismatches, then a stronger model on
     the rest) only if measurement shows the small model can drop jobs without losing good
     ones. Measure the simple single-model option first; cascades add moving parts.

## Jev decisions scorer

`typesafe/jev-1.13` is not a chat model. It is a **decisions model**
([docs.typesafe.ai/primitives](https://docs.typesafe.ai/primitives)) reached through
`POST https://openrouter.ai/api/alpha/decisions` with `{model, state, questions}`. Each
question is typed: `choice` (one option, with probabilities and confidence), `score` (a
position along ordered levels) or `noul` (the probability of yes). There is no rationale and no
quoted evidence. Observed 2026-10-09: about **$0.042 per million input tokens**, output free,
so a packed request of 8 postings costs a small fraction of a cent.

```bash
jobhunter score --submit --scorer jev:typesafe/jev-1.13 --limit 200 --jobs-per-request 8
jobhunter llm bench --scorer jev:typesafe/jev-1.13 --n 16
```

`jev:<slug>` and the generic `decisions:<slug>` build the scorer in
`scoring/decisions.py`. It reuses `[scoring.openrouter]` (`OPENROUTER_API_KEY`, `X-Title`,
`provider.data_collection`). Rows are written under prompt version `decisions-v1`.

### State

```json
{"candidate": {"resume", "current_focus": {"since", "doing", "want_more_of"}, "done_with",
               "want", "avoid", "dealbreakers", "context"},
 "jobs": [{"id": "g123", "title", "employer", "location", "employment_type", "remote",
           "description"}, ...]}
```

The candidate block holds the same fields as the Haiku screen's profile block
(`scoring_inputs`): no salary floor, states, weights or thresholds, and no stated salary in
the jobs. Pay and location fit stay in Python ([014](014-preferences-console.md)).

### Question set (composite scoring: one focused question per factor)

Ids are namespaced `<custom_id>.<question>`. Instructions point at the job as `` `jobs[i]` ``
and at the candidate by path (`` `candidate.resume` ``, `` `candidate.current_focus` ``).

| Id | Type | Asks |
|---|---|---|
| `candidate.level` | score, 5 levels | the candidate's level (once per request) |
| `skills_raw` | score, 5 levels (006 skills anchors) | can the candidate do this, judged on the whole resume |
| `skills_recent` | score, same levels | the same, judged only on `candidate.current_focus` (last ~3 years) |
| `job_level` | score, 5 levels (junior, mid, senior, lead/staff, principal/exec) | the level the job requires |
| `seniority_direction` | choice: below, match, above, unclear | job vs candidate level |
| `domain` | score, 5 levels (006 domain anchors) | domain and kind of organisation |
| `focus_overlap` | score, 4 levels | how much of the job is current-focus work |
| `verdict` | choice: strong, possible, weak, mismatch, other | cross-check, kept in the report |
| `done_with.<slug>` | noul per `current_focus.done_with` item | is it a substantial part of the job |
| `avoid.<slug>` | noul per line of `narrative.avoid` and `dealbreakers_soft` | does the posting show this shape |
| `requires.<slug>` | noul per `hard.requires_i_lack` credential | stated as a hard requirement |
| `states.salary`, `states.location`, `states.remote_policy` | noul | does the posting state it |

### Mapping to the dimensions JSON

A score answer becomes a 0-1 position (sum of level times probability, over the top level),
then 0-100. The five-level skills and domain scales put each level at 0, 25, 50, 75 and 100,
which falls inside the matching 006 anchor band.

- `skills.score` = `recency_weighted_skills` = `skills_recent`; `raw_skills` = `skills_raw`.
  The gap drives bucket F as before, and also adds a `stale_match` flag.
- `seniority.score` = `100 - 25 x |job_level - candidate.level|` in levels (one level off is
  75, two is 50). `seniority_direction` is the choice; `unclear` falls back to the sign of the
  level gap (more than half a level).
- `domain.score` = `domain`; `current_focus_overlap` = `focus_overlap`.
- `done_with_hits`: items with p >= 0.5 (plus a `done_with` flag). `shape_flags`: avoid slugs
  with p >= 0.6. `blockers`: credentials with p >= 0.6. `missing_info`: a `states.*` answer
  with p < 0.4.
- `stale_skills` is always empty: one question per skill would be needed to name them.
- `low_confidence` flag when the verdict confidence is below 0.4 or more than half of the
  five dimension scores are.
- `fit_score.verdict` is the verdict choice; when it is `other`, the most likely of the four
  stored verdicts.

Every row stores `evidence = []`, `evidence_unverified = 0`, `evidence_mode = 'none'` and the
raw answers (probabilities and confidence per question) in `fit_score.decisions`. The detail
page shows "Jev: fit strong (0.97, confidence 0.96)" and the yes/no probabilities in place of
quotes; `eval` leaves these rows out of the `evidence_unverified` rate and reports the count.

### Requests, cost and failure

- Jobs per request: `--jobs-per-request` (default 8, at most 25), and a request also stops at
  about 60,000 estimated input tokens (characters / 4). One oversized posting still gets a
  request of its own.
- The spend cap is checked before each request against its estimated cost.
- `usage.cost` is split evenly over the request's jobs; the whole request goes to
  `llm_spend` as one call. The served model (`typesafe/jev-1.13-20260917`) is stored per row.
- A 400 lists zod issues; they are printed as `path: message` and the request's jobs stay
  eligible. 401, 402 and 403 (for example the key's model guardrail) stop the run. A job with
  any unanswered question is not written and stays eligible.

## Scoring through a Claude Max subscription

Possible via a scorer that calls `claude -p` headlessly. It runs on the subscription instead
of API credits, but it is sequential (no batch discount), counts against plan usage limits,
and whether it fits the plan's terms for this kind of automated use is for you to check. Listed
for completeness; the API or a local model is the cleaner fit.
