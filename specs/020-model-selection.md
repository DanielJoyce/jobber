# 020 — Choosing models

Status: **draft for review, revision 2** (bug `051d76b`). Written 2026-10-11 and revised the
same day after an adversarial review ([History](#history)). Checked against `main` with PR 117
(bug `15f7012`, Claude 5.5 defaults and the verified price table) and PR 118 (019 revision 4b:
readiness checks, help pages, config watch) merged.

You asked for an easy way to choose models. Today every model is a line in `config.toml`. This
spec adds:

- **`/models`** in the console: one row per thing jobhunter uses a model for, each with a
  dropdown of models you can use, whether the key or subscription it needs is there, what it
  costs, and **Save** or **Save + re-run**.
- **Credential detection** for the Anthropic API, OpenRouter (and Jev), a Claude subscription
  through the `claude` CLI, and a local model server, without spending anything.
- **A message at the top** of `/models`, and of the pages where a use matters (the re-score
  panel, the packet page, Score now), when no key or subscription is found for that use, with a
  link to the help page that fixes it.
- **`<data dir>/models.toml`**, the file `/models` saves to. It is layered over `config.toml`,
  which the console still never writes.
- **One fix to existing spend checks** that has to come first: the batch spend reservation is a
  flat $0.002 a job whatever the model ([M0](#proposed-tickets)).

## Decisions

| Question | Choice | Why |
|---|---|---|
| Where choices are saved | **`<data dir>/models.toml`**, written only by `/models` and `jobhunter models set`, layered over `config.toml`, whose model keys still apply where the file is silent | Your decision (2026-10-11): a separate file, not `config.toml`. Data dir because containers mount only `config.toml` itself, read only ([Storage](#storage)) |
| Does Save spend? | **Never.** Save + re-run only opens the confirm step that already exists for that use | A model change can make the nightly run cost more; a re-run costs money now. Both stay your call |
| Which subscription checks are free and automatic | `claude` on `PATH`, a `stat` of the CLI's login file, `CLAUDE_CODE_OAUTH_TOKEN` set, the runner state, and recent subscription drafts. `claude auth status` runs only when you press **Check (free)** | 019 rule 1: no subprocess while rendering a page |
| Models that cannot do a use | **Not offered** for it, but if one is already in effect (from `config.toml`) it is still shown, selected, with the reason | Opus 5.5's thinking cannot be turned off (PR 117 `THINKING_OFF`, `scorers.py:143`), and the screen and fact check are bounded calls (`screen.MAX_TOKENS` 2,048) |
| Screening models dearer than the flat reservation | **Not offered until M0 lands** (Sonnet 5.5 today) | The batch path reserves $0.002 a job for every model (`screen.py:527`); Sonnet 5.5 costs about $0.005 a job, so caps would be overrun about 2.5x |
| An Anthropic model with no price | **Refused** on save | `compute_cost` raises `no pricing for model` (`scorers.py:97`), and spend caps would not see the cost |
| A broken or invalid `models.toml` | **Fail closed for spending, fail soft for ingest.** An invalid screening or deep key blocks that use (skipped with a message, jobs stay queued), never falls back to another provider. Drafting falls back to `config.toml` with a notice, because every draft is a click that shows its estimate | Falling back could spend on a provider you switched away from ([Storage](#storage)) |

Recorded on bug `051d76b`.

## Every model use (checked in code, 2026-10-11)

| Use | Setting, default | Providers allowed | Used by | Re-running means |
|---|---|---|---|---|
| **Screening** (Stage 2) | `[scoring] screen_scorer`, `anthropic:claude-haiku-5-5` (`config.py:233`) | `anthropic`, `openrouter`, `openai-compat`, `local`, `jev`, `decisions` (`config.SCORER_PROVIDERS`, `config.py:191`) | The nightly run's score stage (`pipeline/runner.py:433`); `score --submit` and `--collect` (`cli.py:546`); the re-score panel's default (`console/rescore_routes.py:51`); Score now on captured and packet pages and the extension (`apply/score.py:128`, `ext_routes.py:472`); `backfill`; `llm bench`'s default (`cli.py:1731`) | A **confirmed re-score** (`scoring/rescore.py`, [014](014-preferences-console.md#re-score-now)) with the new scorer, scope "recent" (open A-E, last 14 days) or "all". Eligibility is per model (`screen.py` `_ELIGIBLE` keys on `f.model`), so every group in scope is eligible again. The nightly run takes only groups with no screen score under any model (`new_only`), so without a re-run it scores only new jobs with the new model. **But** a hand-run `score --submit` does not use `new_only` (`cli.py:600`, `614`-`622`): it re-screens up to `--limit 500` already-scored groups under the new model, with no confirm, bounded only by the caps. The page and help say so; M9 proposes changing it |
| **Deep pass** (Stage 3) | `[scoring] deep_scorer`, `anthropic:claude-opus-5-5` (`config.py:234`) | `anthropic` only (`check_scorer_spec(..., anthropic_only=True)`, `config.py:217`) | `jobhunter score --deep N` and `--deep-group GID` only (`cli.py:571`, `397`). No console route runs it | A new deep model applies **only to groups not yet deep-scored**. `score --deep N` skips groups deep-scored under **any** model and moves on to the next N (`deep.py:217`, by design since PR 117: a model change must not pay again for the same groups). To re-run a group under the new model: `score --deep-group GID`, one group at a time (`deep.py:209` `_stored` is per model) |
| **Packet drafting** | `[apply] runner` `cli`; `cli_model` `opus` (a CLI alias); `api_model` `claude-opus-5-5`; `effort` `medium` (`config.py:307`-`310`) | `cli`: the `claude` CLI on your subscription. `api`: the Anthropic SDK | Generate on the packet page (`console/packet_routes.py:397`) and `jobhunter apply draft` (`generator.py:1071`, `1080`). In containers only `apply draft` on the host (`packet_routes.py:406`, 018 D16) | Generate again on one packet. Each click makes a new version; old versions stay. Never in bulk |
| **Fact check** (entailment) | `[apply] entailment_check` on; `cli_entailment_model` `haiku`; `api_entailment_model` `claude-haiku-5-5` (`config.py:315`-`317`) | Same runner as the draft it checks | After every generation (`generator.py:908`, `920`) | Runs again with the next Generate. There is no check-only re-run today |

Not separate uses:

- **Score now** and **backfill** use the screening choice.
- **`llm bench --scorer`**, **`score --scorer`** and the re-score panel's scorer picker override
  the choice for one run and save nothing. They keep doing so.
- **`eval`** calls no model (`scoring/evaluate.py`: "Read-only: nothing here calls a model"). Its
  `--model` filters stored scores.
- Mail parsers, the prefilter and buckets call no model.

What the inventory turned up:

- **Three places check only `ANTHROPIC_API_KEY`**: the re-score panel's scorer list
  (`rescore_routes.py:57`), the packet page's runner status (`console/packet_docs.py:285`) and
  the drafting API pre-check (`generator.py:686`). The SDK also accepts `ANTHROPIC_AUTH_TOKEN`,
  a profile and the other sources 019 lists, so someone with only those sees "no key" in three
  places while scoring works. M3 sends all three through 019's `credential_for`.
- **Queued re-scores follow the current model.** A re-score queued by a paid `/prefs` save is
  stored with no scorer and no plan (`prefs.py:786`). The nightly drain (`score
  --rescore-pending`, from `jobhunter-collect.service` and `jobhunter-ctr-rescore`) plans it
  with `scorer or row["scorer"] or scoring.screen_scorer` (`rescore.py:578`). Changing the
  screening model therefore changes what those requests cost tonight. `/models` warns before
  you save and lets you keep them on the old model ([Queued re-scores](#queued-re-scores)).
- **The batch reservation is flat.** `submit_batch` and the sync path cap a submission at
  `remaining / $0.002` (`screen.py:527`, `911`); uncollected batches are counted at $0.002 a
  request (`screen.py:438`); a model with no history is planned at $0.002 a job
  (`rescore.py:150`-`151`); and the Anthropic re-score submits its whole plan without checking
  the plan's `max_usd` (`rescore.py:634`-`653`). That is safe for Haiku 5.5 (about $0.0003 a
  job) and wrong for anything above $0.002. The PM is filing this existing-code bug
  separately; M0 is the fix 020 depends on.
- **Two estimates ignore the model.** `/prefs`' paid-save estimate averages `fit_score.cost_usd`
  over every model ever used (`prefs.py:737`-`748`), and the fact check is always estimated and
  cap-checked at a flat $0.003 (`ENTAILMENT_ESTIMATE_USD`, `generator.py:62`, `434`, `902`,
  `923`, `932`), which a Sonnet 5.5 check can exceed about ten times. M0 and M1 fix them.

## Credential detection

One module, `src/jobhunter/model_access.py`, answers "can this use run, and on what?" It is
built on 019's `credential_for` (S1, `a1af329`) and follows 019's rules for every check: local
and free, never a value shown, same answer everywhere. The `/models` page, the banners and
`doctor` only render it.

```python
@dataclass(frozen=True)
class Access:
    provider: str      # "anthropic", "openrouter", "openai-compat", "local", "subscription"
    status: str        # "ok" | "likely" | "missing" | "unknown"
    detail: str        # fixed wording, never a value
    help: str          # help topic and anchor, e.g. "scorers#get-an-anthropic-api-key"
    live_check: str | None  # 019 S6 check id, when a free live check exists

def access_for(use: str, choice: Choice, settings, *, mode, now) -> Access: ...
def problems(uses, settings, *, mode, now) -> list[Access]: ...   # what the banners show
```

| Provider | Local, free, on every render | Only on a click |
|---|---|---|
| Anthropic API | `credential_for`: `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, a profile or config dir, workload identity. A source only the SDK can judge is `likely` ("via SDK profile, not checked") | S6 `models.list`: key accepted |
| OpenRouter, Jev, decisions | The variable named by `scoring.openrouter.api_key_env` | S6 `GET /key` on openrouter.ai only: accepted, credit left |
| openai-compat | The variable named by `scoring.openai_compat.api_key_env` (or none, by design) | none (no free check for an unknown endpoint) |
| Local server | Nothing to detect: `unknown` until checked. Never `missing` | **Check server (free)**, a 020 route ([Local server check](#local-server-check)): reachable, and the loaded model ids |
| Claude subscription (the `cli` runner) | see below | S6 `claude auth status --json` through the uncached `auth_status()`: logged in with claude.ai |

**The subscription, without running anything.** In host mode, in this order:

1. `shutil.which("claude")` (`cli_runner.find_binary`) is None: **missing**, "the claude command
   is not installed".
2. The runner is off (`runner_state.load`, `<data dir>/apply-runner.json`): **missing**, with
   the stored reason ("not a claude.ai subscription login" and the like).
3. Look for a login: `CLAUDE_CODE_OAUTH_TOKEN` set (presence only), or the CLI's login file
   (`$CLAUDE_CONFIG_DIR/.credentials.json`, else `~/.claude/.credentials.json`; a `stat`, never
   opened). None found: **missing**, "claude is installed but no login was found".
4. A login was found **and** this process already passed `check_auth` (`cli_runner._AUTH_OK`,
   filled only by a real draft) or a `packet-cli` row in `llm_spend` is from the last 30 days:
   **ok**, "a subscription draft worked at <time>". Spend history alone never makes the row ok,
   so a `claude logout` that removed the login shows at once.
5. A login was found, no recent draft: **likely**, "claude is installed and a login exists; not
   checked".

The login file path is what Claude Code uses on Linux. M3's worker confirms it against the
installed CLI and records the result in the PR. If the CLI keeps its login somewhere a `stat`
cannot see, step 3 gives `unknown`, never `missing`. Step 5 cannot tell a claude.ai login from
a Console API-key login; only `auth status` can, which is why it stays `likely`. A real draft
still runs `check_auth` first (`cli_runner.py:298`) and turns the runner off on a wrong login,
as today.

**Modes** (019 [Modes](019-setup-and-readiness.md#modes)). Each row is judged in the
environment that will actually run it:

| Mode | Screening and deep rows | Drafting and fact-check rows (both runners) |
|---|---|---|
| host | the console's environment, then 019's effective status | the console's environment |
| container | the console container's secrets (018's matrix gives it `jobhunter_anthropic`, `jobhunter_openrouter`, `jobhunter_openai_compat`, `jobhunter_llama`), then 019's effective status | **unknown**, "drafting runs on the host with `jobhunter apply draft`; check there". The container never drafts (`packet_routes.py:406`), so its secrets say nothing about drafting |
| host managing containers | 019's drop-in rule (a secret counts when preflight's drop-in for the unit exists) | `credential_for` over the **host's** environment and the subscription steps above, because `apply draft` runs on the host |

Unknown never raises a banner. **The nightly run sees other things**: for the screening row the
effective status is 019's, the worse of the console's view and a fresh `run_inputs` snapshot
(S4), with the disagreement shown. Before S4 lands, the row uses the console's view only.

### Local server check

`POST /models/local/check` calls `local_server_status` (`scorers.py:684`) and returns the
reachable flag and the model ids. This is a deliberate, narrow exception to 019 S6's
"booleans only" rule: model ids are not secret (019 rule 3 lists keys, tokens, addresses,
accounts and organizations), and the dropdown needs them. Everything else follows 019:

- `ServerStatus.detail` is **never** rendered or logged. It can hold `str(exc)` (`scorers.py:700`,
  `706`), which 019 rule 4 forbids. Outcomes map to fixed text: "reachable", "not reachable",
  "answered but asks for a key (set `LLAMA_API_KEY`, or the name in `[scoring.local]
  api_key_env`)", "answered, listed no models".
- Model ids are shown escaped, at most 50, each cut to 200 characters.
- Gated like 019 S6: loopback peer only, refused under `--allow-remote` (it sends the server
  key). The ids live only in the response; nothing is stored. When Save later checks a local
  choice, the page posts back the ids it received, and without them the server list counts as
  unknown (a "save anyway" prompt, no network call).

## Model catalog

`src/jobhunter/scoring/catalog.py` builds each dropdown. Every entry has the model, a short
label, the uses it can serve, and a price. **No page render, preview or Save makes a network
call.** OpenRouter data is read through a load-only helper, `catalog.load_openrouter_cache()`,
which reads the cache file and never fetches. Render, preview and Save must not call
`OpenRouterScorer.catalog()`, `.cost()` or `backfill.per_job_cost` on an OpenRouter scorer,
because `catalog()` fetches `/models` when the cache is over 24 hours old (`scorers.py:555`) and
swallows the error.

| Provider | Where the options come from | How fresh |
|---|---|---|
| Anthropic | A static table in `catalog.py`, keyed by `PRICING_PER_MTOK` (`scorers.py`: Haiku 5.5, Sonnet 5.5, Opus 5.5, and older models kept for stored spend). Each entry says which uses it serves. Older models (`claude-haiku-4-5`, `claude-opus-5`, `claude-opus-4-8`, `claude-sonnet-5`) sit under "Older" and are offered only where they still work | Changes with the code. The page shows "prices checked 2026-10-11" from a constant next to the table. A test fails when a `PRICING_PER_MTOK` key has no catalog entry, or the other way round |
| CLI aliases | `opus`, `sonnet`, `haiku`, which the CLI resolves to whatever your plan serves, plus a specific Anthropic id where the use rules allow it | Static |
| OpenRouter | The catalog cache `<cache dir>/openrouter_models.json` (`scoring.openrouter.catalog_cache`), which the scorer already keeps (refreshed at most daily during scoring, `CATALOG_MAX_AGE_S`, `scorers.py:488`). Only models that support structured output are offered, because `provider.require_parameters = true` refuses the others | The cache's `fetched_at` is shown. **Refresh list (free; contacts openrouter.ai)** fetches the public `/models` list on click ([routes](#routes)). It needs no key |
| Jev, decisions | `decisions.JEV_DEFAULT_MODEL` (`typesafe/jev-1.13`), plus a free-text slug | Static |
| Local | The configured model, plus the ids the last **Check server (free)** returned in this page view | Only on click |
| openai-compat | Free text: the endpoint is yours and has no catalog | n/a |

The OpenRouter cache today keeps only prices (`{"fetched_at", "prices": {slug: [in, out]}}`).
M1 adds a `models` map beside it (`{slug: {"name", "context_length", "structured": bool}}`,
read from `supported_parameters`). Readers that know only `prices` keep working, and a cache
without `models` offers its priced slugs with "structured output not known".

**The model in effect is always shown.** Whatever the effective value is (from `models.toml`,
`config.toml` or the default), it is rendered as the selected option, even when the catalog
does not offer it for this use or has never heard of it (an OpenRouter slug with no cache, a
`config.toml` Opus 5.5 screen scorer). It carries its source and, when the catalog would not
offer it, why: "from config.toml; not offered for screening: its thinking cannot be turned
off". The price and access lines are computed for that value, never for the first option. A
`<select>` with its value missing from its options would otherwise show, and save, the first
option.

**Which Anthropic models serve which use** (M1 checks each against the platform docs and
records the source in the PR; the table below is the starting point):

| Use | Offered | Not offered, and why |
|---|---|---|
| Screening | Haiku 5.5 (default); Haiku 4.5 under Older; **Sonnet 5.5 once M0 lands** | Opus 5.5: thinking cannot be turned off. Sonnet 5.5 before M0: the flat $0.002 reservation is below its price |
| Deep pass | Opus 5.5 (default), Sonnet 5.5 | Haiku 4.5: it rejects `output_config.effort`, and the deep pass sends effort `high` (`deep.py` `EFFORT`) |
| Drafting, `api` | Opus 5.5 (default), Sonnet 5.5 | Haiku 4.5: same effort reason (`generator.api_params`) |
| Drafting, `cli` | Aliases `opus` (default), `sonnet`; ids `claude-opus-5-5`, `claude-sonnet-5-5` | The `haiku` alias and `claude-haiku-4-5`: the CLI runner always passes `--effort` (`generator.py:1080`-`1081`, `cli_runner.py:189`-`191`), and Haiku 4.5 rejects it. The fallback-only ids (`claude-opus-4-8`, `claude-sonnet-5`) |
| Fact check, `api` | Haiku 5.5 (default), Sonnet 5.5; Haiku 4.5 under Older | Opus 5.5: same as screening |
| Fact check, `cli` | Aliases `haiku` (default), `sonnet`; ids `claude-haiku-5-5`, `claude-sonnet-5-5` | The `opus` alias and Opus ids: same as the `api` row. The check passes no effort (`generator.py:920`-`921`), so `haiku` is fine here |

The same rules apply when `cli_model`, `cli_entailment_model` and the other keys are validated
on save.

**Prices shown per use.** Each option shows a cost in the unit that use spends in. Estimates
use measured history first, as the existing estimates do:

| Use | Per unit | "At your volume" |
|---|---|---|
| Screening | Per 1,000 jobs: the measured average `fit_score.cost_usd` for that model (`rescore.cost_per_job`); else the price table at `backfill.TYPICAL_USAGE`, batch-priced for Anthropic; else the decisions fallback; else the cached OpenRouter price at typical tokens (load-only); `local:` is $0 | Jobs screened in the last 7 days × per job, as "about $X a week" |
| Deep pass | Per job: the measured average for `tier = 'deep'` and that model; else the price table at 006's typical deep tokens | × `deep_shortlist` per `score --deep` run |
| Drafting | Per resume, letter and answer: `generator.estimate`, taken per model (today it takes the median over every model; M1 filters on `packet_document.model`) | none. `cli` shows "$0 on your subscription; about $X at API prices", from the median `api_equiv_usd` |
| Fact check | Per draft: the price table at the check's typical tokens for `api_entailment_model`, else `ENTAILMENT_ESTIMATE_USD`. M1 uses the same figure in `generator.estimate` and the cap checks, not the flat $0.003 | none |

The page states which source each number came from ("measured over 412 jobs", "price table",
"no price: the cap check uses $0.04 a request"). After M0 the cap reservation for screening is
the larger of $0.002, the measured average and the table price, so the number the page shows
and the number the cap reserves can differ only upward, and the page says so when they do.

**Measure before switching** ([016](016-alternative-scorers.md#ground-rules) rule 1). Each
screening option shows how many of your labeled jobs it has scored (a count from `fit_score`
and the triage labels), and the `jobhunter eval --compare` line to measure it. The page does
not run `eval`.

## Storage

**Your decision (2026-10-11): a separate, console-managed `models.toml`, not `config.toml`.**
TOML rather than YAML, so it shares `config.toml`'s format and loader (`tomllib`, the
`_deep_merge` in `load_settings`, `config.py:368`). This section says where it lives and how it
layers.

**Data dir, not config dir.** The config directory (`~/.config/jobhunter/`) would sit next to
`config.toml`, but containers never see it: only the single file `config.toml` is mounted, read
only, because that directory also holds `gmail_token.json` and `google_client_secret.json`
(018 D3). The console container could neither read nor write a `models.toml` there, and a
second single-file mount would have to be read-write and created before the first start. The
data dir is already a writable bind mount at `/data` in every container, and the same host
directory (`~/.local/share/jobhunter/`) for the host CLI, the timers and `apply draft`. So:
**`<data dir>/models.toml`**, next to `apply-runner.json`, `extension.json` and
`readiness.json`, the other files the console writes.

The three places considered:

| | `config.toml` | **`<data dir>/models.toml`** (chosen) | `preferences.yaml` |
|---|---|---|---|
| 019's rule that the console never writes `config.toml` | Breaks it | Keeps it | Keeps it |
| Containers | Impossible: a single-file read-only mount (018 D3) | `/data` is a writable bind mount, shared by the console, every run container and the host's `apply draft` | Writable |
| Comments and your hand edits | Needs a comment-keeping TOML writer (`tomlkit` is not a dependency; `tomllib` only reads) and conflict handling with your editor | The file is the console's. Your `config.toml` is untouched | `/prefs` already round-trips it |
| Config watch (019 S12a) | The console's own write looks like your edit | One more watched file, with its own layer | Not watched; the profile is reloaded per use |
| Meaning | Right place, wrong writer | Settings layer, clear owner | Your job preferences, not system settings. Its contents feed `filter_version` and `scoring_version`, and it is read by `load_profile`, not `Settings` |

**Precedence**, lowest to highest: code defaults, `config.toml`, `models.toml`, then a
per-command flag (`--scorer`, the re-score panel's picker). The `overrides` argument of
`load_settings` (tests) stays highest.

**The existing `config.toml` keys stay, and are not migrated.** `screen_scorer`,
`deep_scorer`, `api_model` and the other model keys in `config.toml` keep working exactly as
today. They are the value whenever `models.toml` does not set that key, so an install that
never opens `/models` behaves as before, and hand-editing `config.toml` still works. Nothing
copies them into `models.toml`, and nothing removes or rewrites them: copying would freeze
today's values and silently stop your later `config.toml` edits from taking effect. A key
enters `models.toml` only when you save that use on `/models` (or run `jobhunter models set`),
and it leaves on **Reset**. **Which source wins is always shown**: each row on `/models` says
"saved on /models at 14:05", "config.toml" or "default", and when both files set a key,
"config.toml says X; /models overrides it" with Reset. `jobhunter models show` and `doctor -v`
print the same source, and the config watch labels an overridden `config.toml` edit "no effect:
set on /models".

- **What the file may hold.** Only `[scoring] screen_scorer`, `deep_scorer`, and `[apply]
  runner`, `cli_model`, `api_model`, `effort`, `entailment_check`, `cli_entailment_model`,
  `api_entailment_model`. Every one is in 019 S12a's `RELOADABLE` set. Caps, `api_key_env` and
  endpoints stay in `config.toml`.
- **Layers.** M2 adds `config.load_layers()`, which returns the settings built from defaults and
  `config.toml` alone, the `models.toml` layer (the keys it set), the problems found in it, and
  the merged `Settings`. `load_settings()` returns the merged one, so every caller is unchanged.
  It first settles `paths.data_dir` from `config.toml` and the environment, then reads
  `<data dir>/models.toml`, merges the valid keys with `_deep_merge` and validates again. Every
  process does this at start: the nightly run, every CLI command, `apply draft` on the host,
  every container. The config watch and `/models` use the layers.
- **Validated per key.** Each key is checked on its own: `check_scorer_spec` for the scorer
  specs (deep with `anthropic_only=True`, which already rejects bare names and
  `openrouter:jev:...`), the price rule, the use rules, and the field's own type. A valid key
  applies. An **unknown** key (a newer version's file read by an older image, or the other way
  round) is ignored with a warning, not an error.
- **Fail closed when a spending key is invalid.** When `models.toml` sets `screen_scorer` or
  `deep_scorer` and that value is invalid, or the file is not valid TOML at all (so what it set
  cannot be known), the use is **blocked**: `Scoring.scorer_problems()` (`config.py:245`)
  reports it, and every path that spends on that use skips with its existing message
  ("skipped; survivors stay queued") instead of falling back to `config.toml` or the default.
  `scorer_problems` is already what the run's score stage checks (`runner.py:434`) and what
  `score`'s mode check uses (`cli.py:127` `_check_scorer_override`). M2 adds the check where
  it is missing today: the re-score drain (`cli.py:712` checks only an explicit `--scorer`),
  `backfill`, Score now (`apply/score.py:130` checks only the spec's syntax), the extension's
  Score now, and the re-score panel. Why: you may have chosen `local:` or a cheap OpenRouter
  model on `/models` to stop spending, and a hand edit or a version difference must not quietly
  send tonight's jobs to the Anthropic default.
- **Drafting keys fall back.** An invalid `[apply]` key in `models.toml` is ignored and the
  `config.toml` value or default applies, with a notice on the packet page and `/models`
  ("models.toml's drafting model is invalid; using opus from config.toml"). Every draft is a
  click that shows its runner, and an API draft its estimate, so nothing is spent unseen.
- **Reporting.** A problem shows on `/models` (with **Reset** for the key, or **Reset file**),
  in M10's `/setup` row "Model choices" (broken) and `doctor`, and in the run summary
  (`score: skipped: models.toml screen_scorer is invalid (see /models)`). Error text follows
  019 B's rules: location and message, never `str(exc)` or the value, except values on
  `SHOWN_VALUES` (scorer specs and model ids).
- **Writing.** Atomic (temporary file, `fsync`, rename), mode 0600, under a `flock` on
  `models.toml.lock`. Each write carries the SHA-256 of the file it was based on. A mismatch
  answers 409, "changed elsewhere; reload", and writes nothing. Every successful write returns
  the new digest ([routes](#routes)). The file starts with a comment saying it is written by
  `/models` and `jobhunter models`, and overrides `config.toml`.
- **Only what you changed is written.** A Save writes the fields you changed in that row and
  nothing else, compared with the effective values the page showed. Changing only the drafting
  effort never writes `cli_model` into the file.
- **Save anyway.** An OpenRouter slug not in the cache, a local model not in the posted server
  list, or a model whose credential is missing saves only after an explicit "save anyway", with
  the reason shown. On the host the console and the nightly run can see different keys (019
  [What the nightly run sees](019-setup-and-readiness.md#what-the-nightly-run-sees)), so a key
  the console cannot see is not proof the run cannot.
- **History and revert.** A migration adds `model_change` (`id`, `at`, `use`, `field`,
  `old_value`, `new_value`, `source` in `ui`, `cli`, `reset`, `revert`). `/models` lists the
  last 20 with **Revert**, which is a new save, not a rewind. Model ids hold no personal data.
- **Where a value came from.** `jobhunter paths` lists `models.toml`, and `doctor -v` prints the
  source of each model setting (M10).

### Queued re-scores

Any change to the screening model (Save, Reset, Revert, `models set`, `models reset`, or a
config-watch Apply of a `models.toml` or `config.toml` edit) can reprice re-scores that are
queued without a plan. So every one of those paths shows the same line **before** it writes:

- In the page, the htmx preview that runs when the dropdown changes (and on Reset and Revert)
  shows "2 queued re-scores would run tonight with Sonnet 5.5: about $3.10, instead of about
  $0.31 with Haiku 5.5".
- It is priced the way the drain will run: `drain_pending` takes pending requests in id order
  (`rescore.py:711`), and a `done` run cancels the later requests it covers
  (`supersede_pending`, `rescore.py:408`). So the line plans the first pending request, and the
  widest scope among them, with `make_plan` for the candidate model, and lists covered requests
  as "would be superseded", rather than summing every plan. Planning runs `ensure_prefilter`
  (`rescore.py:229`), a free local write the re-score panel's GET already does. It is the only
  write a preview makes.
- When unplanned pending requests exist, Save offers two buttons: **Save; queued re-scores use
  the new model** and **Save; keep queued re-scores on Haiku 5.5**. The second writes the old
  spec into `rescore_request.scorer` for those rows, which `run_request` already honours
  (`rescore.py:578`).
- `jobhunter models set` and `models reset` print the line and ask the same question
  (`--queued new|keep`, required when there are unplanned requests and stdin is not a TTY). The
  config-watch notice shows the line next to Apply and applies with "keep" unless you pick
  otherwise.

### Config watch (019 S12a)

S12a diffs `load_settings()` against the running settings. With `models.toml` merged in, that
diff alone cannot see an overridden `config.toml` edit and would read a broken `models.toml` as
a change back to `config.toml`'s models. So M8 builds on the layers:

- `models.toml` joins the files S12a fingerprints. A change made outside the console
  (`jobhunter models set`, or a hand edit) is diffed layer by layer and offered as Apply; every
  field in it reloads in place. A `models.toml` problem is shown as a validation error, and the
  running settings stay.
- A `config.toml` edit to a key the `models.toml` layer sets is listed as "no effect: set on
  /models", the way S12a lists a `console.port` set on the command line, with a link to the
  row's Reset.
- **Not the replaced-file rule.** S12a's container heuristic (`st_nlink == 0`, or a ctime
  change with the same content) exists for the single-file `config.toml` mount. `models.toml`
  sits inside the `/data` directory mount and is replaced by rename on every save, which the
  directory mount sees normally. The heuristic does not apply to it.
- The console's own writes update the watcher's fingerprint after the rename, so it never
  notifies itself.
- **One lock for the running settings.** Save, Reset, Revert, Load and S12a's Apply all read,
  modify and assign `app.state.settings`. They take one lock, `app.state.settings_lock`, around
  that read-modify-assign, so neither overwrites the other's change. Whichever of M4 and S12a
  lands second adopts the lock the first created.
- Before S12a lands, `GET /models` reads `models.toml` (one small read) and, when its layer
  differs from the running settings, shows "changed outside the console: [Load]".

### `jobhunter models`

For the terminal and for containers (`jobhunter-ctr models ...`):

| Command | Does |
|---|---|
| `models show` | Each use: effective value, source, any problem, credential status (local checks only) |
| `models set USE VALUE` | Validates and writes, as the page does. `--force` for "save anyway"; `--queued new\|keep` ([above](#queued-re-scores)) |
| `models reset USE` | Removes the key: `config.toml` or the default applies. Same queued-re-score question |
| `models catalog [--refresh]` | The options per use with prices. `--refresh` fetches the OpenRouter list, saying so first |

## `/models`

Nav entry **Models**, after Prefs. Uses `app.css` tokens for light and dark. At 375 px each row
stacks (title, then the dropdown, then price and access, then the buttons) with no horizontal
scroll.

```
Models
┌──────────────────────────────────────────────────────────────────────────────┐
│ ! No Anthropic API credential found. Screening and the deep pass cannot run. │
│   How to add one: Help › Choosing a scorer › Get an Anthropic API key        │
└──────────────────────────────────────────────────────────────────────────────┘

Access
  ✕ missing   Anthropic API         unset                         [How to add one]
  ~ likely    Claude subscription   claude installed, login found  [Check (free)]
  – optional  OpenRouter and Jev    OPENROUTER_API_KEY unset
  ? unknown   Local model server    not checked                    [Check server (free)]

Screening new jobs                                   from config.toml
  [ Claude Haiku 5.5 (anthropic:claude-haiku-5-5)                 ▾ ]
  about $0.26 per 1,000 jobs (price table); about $0.53 a week at your volume
  Needs: Anthropic API ✕
  Also used by: Score now, backfill, llm bench, a hand-run score --submit
  [ Save ]  [ Save + re-score… ]

Deep pass                                            default
  [ Claude Opus 5.5 ▾ ]   about $0.05 a job
  New deep runs use it; 38 groups keep their Opus 5 reports.
  [ Save ]   Re-run your top groups: jobhunter score --deep-group 1412  (about $0.05) …

Packet drafting                                      saved on /models at 14:05
  Runner (•) Subscription (claude CLI)  ( ) API key
  Model [ opus ▾ ]   Effort [ medium ▾ ]
  $0 on your subscription; about $0.17 a resume at API prices
  [ Save ]   Regenerate on: Acme packet, Globex packet

Fact check after each draft                          default
  [x] on    Model [ haiku ▾ ]
  [ Save ]

History   14:05 drafting runner: api → cli   [Revert]
```

**Access rows** show each provider once, with 019's status words and icons (never colour
alone). The Check buttons are 019 S6's live checks (`POST /setup/live/{check_id}`) and, for the
local server, [this spec's route](#local-server-check). Their result replaces the row and,
through an out-of-band swap, the banner, for that page view. Nothing is cached or stored (019:
"never stored or logged"). Before S6 lands, each S6 row shows the command instead (`jobhunter
doctor --live`).

**Use rows.** The effective value is always the selected option ([above](#model-catalog)).
Then usable options, then options whose credential is missing ("needs an Anthropic key"), then
Older. A non-Anthropic choice shows `privacy_notice` (`scorers.py:755`), which says where your
resume and the postings go. Changing the dropdown runs the preview (price, access, the queued
re-score line) before anything is saved.

### Routes

All go through the existing middleware (loopback `Host` on every request, same-origin `Origin`
on writes, [017](017-assisted-apply.md#local-api-security)). The writes also require a
**loopback peer** (`request.client.host`), like 019 S12a's Apply: a model change spends nothing
now, but it decides what the nightly run spends.

| Route | Method | Gate | Does |
|---|---|---|---|
| `/models` | GET | middleware | The page. No network, no subprocess, no write |
| `/models/{use}/preview` | GET | middleware | Price, access and queued re-score lines for a candidate value. No network. Its only write is the prefilter catch-up ([Queued re-scores](#queued-re-scores)) |
| `/models/{use}` | POST | loopback peer | Save (and Save + re-run). Body: the changed fields, the base digest, `queued=new\|keep`, `force` |
| `/models/{use}/reset` | POST | loopback peer | Remove the use's keys from `models.toml` |
| `/models/history/{id}/revert` | POST | loopback peer | Write the entry's old value as a new change |
| `/models/reset-file` | POST | loopback peer | Replace a broken `models.toml` with an empty one (the old file kept as `models.toml.broken-<time>`) |
| `/models/load` | POST | loopback peer | Before S12a: take the on-disk `models.toml` layer into the running settings |
| `/models/openrouter/refresh` | POST | loopback peer; **403 under `--allow-remote`** | Fetch the public OpenRouter `/models` list and rewrite the cache. Network, so it is gated like 019 S6 |
| `/models/local/check` | POST | loopback peer; **403 under `--allow-remote`** | [Local server check](#local-server-check) |

- **When the browser's own peer is not loopback** (the 018 D11 pasta fallback in containers,
  or a LAN browser under `--allow-remote`), the page is shown read-only, following 019: the
  write and check buttons are hidden, and each row shows its command instead,
  `jobhunter-ctr models set USE VALUE` (container modes) or `jobhunter models set USE VALUE`
  (host), and `models reset USE`.
- **The digest moves with every write.** The page holds the file's digest in one element,
  `#models-digest`, which every form includes (`hx-include`). Each successful write returns it
  updated through an out-of-band swap, so saving one row and then another does not get a 409
  for your own first save. A 409 means another writer.

### Save

Saving spends nothing:

1. Validate the changed fields and write `models.toml` ([Storage](#storage)), with the queued
   re-score choice; add a `model_change` row.
2. Under `app.state.settings_lock`, build a copy of `app.state.settings` with **only the saved
   fields** changed (`model_copy` plus validation) and assign it once. An unrelated
   `config.toml` edit still waits for its own Apply.
3. Answer with the row ("saved at HH:MM"), the new digest, and what takes effect when:
   - **Screening**: "New jobs from the next run use Haiku 5.5. 1,840 open jobs keep their
     scores from Haiku 4.5. A hand-run `jobhunter score --submit` would re-screen up to 500 of
     them with Haiku 5.5 (about $0.13)." A run already waiting for the run lock read its
     settings before it waited (019 B) and uses the old model.
   - **Deep pass**: "New deep runs use Sonnet 5.5. 38 groups keep their Opus 5.5 reports;
     `score --deep N` moves on to the next N." Plus the re-run commands below.
   - **Drafting and the fact check**: "the next Generate uses it".

### Save + re-run

Never runs anything by itself. It saves, then opens the existing confirm step for that use:

| Use | Save + re-run opens |
|---|---|
| Screening | The **re-score panel**, inline on `/models` (`_rescore_panel.html`, `GET /prefs/rescore/panel?scorer=<new spec>`), with the new model selected and the scope choice. You see the job count, the estimate and the spend ceiling, then press Run and confirm. The plan token (`rescore_routes.py:152`) makes the run match what you confirmed. With an Anthropic model it only submits a batch, capped at the plan's `max_usd` after M0; the nightly collect, or `score --collect-pending`, finishes it. The panel says "the model is saved; Cancel only cancels the re-score" |
| Deep pass | No button ([open question 1](#open-questions)). The row lists the five highest-ranked groups whose deep report came from another model, each as `jobhunter score --deep-group GID` with its estimate, and says that `score --deep N` scores the next N unscored groups, not these. A flag that re-runs the top N in one go (`--deep N --redo`, with its own confirm) is a possible later ticket, not proposed now |
| Drafting, fact check | No bulk re-run. The row lists up to five packets whose current documents came from another model (`packet_document.model`), each linking to its packet page, where Generate shows its estimate (API) or paid-usage confirm (subscription on extra usage) as today. When `/models` was opened from a packet page (`?next=/packet/N`, checked with `safe_next`), the button is **Save and go back** |

The re-score panel's own scorer list (`available_scorers`, `rescore_routes.py:45`) shows the
effective screening choice as "your default", plus the usable catalog choices from M1.

## Banners elsewhere

One partial, `_model_access.html`, renders `model_access.problems()` for the uses a page
needs. It shows only `missing` (never `likely` or `unknown`) and blocked uses, names the use and
its consequence, and links the help topic and the `/models` row:

| Page | Uses checked | Example |
|---|---|---|
| `/models` | all chosen | "No Anthropic API credential found. Screening and the deep pass cannot run." |
| Re-score panel (`/prefs#sec-rescore`) | the selected scorer | "No OpenRouter key: Jev cannot run. Help · Models" |
| Packet page | drafting, fact check (the chosen runner) | "Claude subscription not found: claude is not installed. Drafting with the subscription cannot run; you can switch the runner on Models." |
| Captured page and Score now panels | screening | as the re-score panel |
| `/` and `/inbox` | none | 019's banner already covers the screening scorer as an essential. Not shown twice |

When nothing at all is usable (no Anthropic credential, no OpenRouter key, no subscription,
and no local server configured), `/models` shows one line instead of several: "No model access
found: no API key and no Claude subscription. Start with Help › Getting started."

**Help links.** The banner and the Access rows link to 019's help topics: `scorers.md`
(Anthropic key, local models), `openrouter.md`, `drafting.md` (Claude Code login),
`costs-and-credits.md`. M6 adds the headings these links need, if S11b has not written them,
and a new topic `models.md` ("Choosing models": the page, precedence, `models.toml`, what a
model change does to queued re-scores and to `score --submit` and `--deep`, `jobhunter
models`). 019's drift tests check every link and anchor.

## Console rules

- No secret is typed, shown or stored. Access rows use `SecretState.describe` wording.
- No page render, preview, banner, Save or `doctor` run makes a network call, a subprocess or a
  model call. The OpenRouter refresh, Check server and the S6 checks run only on click, and
  none calls a model.
- `Cache-Control: no-store`, like every page.

## Testing

- **No spend, no network**: `GET /models`, every preview, Save, Reset and Revert with the
  `anthropic` client and `subprocess.Popen` patched to raise, and a **recording**
  `httpx.MockTransport` that asserts zero requests (an error-raising transport would be
  swallowed by `catalog()`). The OpenRouter cache in these tests is more than 24 hours old, so a
  stray `catalog()` call would try to fetch. Save + re-score returns the panel and creates
  **no** `rescore_request`; only the existing confirm route does.
- **Precedence**: a default, a `config.toml` value and a `models.toml` value per field; the
  `--scorer` flag beats all three; `overrides` beats the file. The nightly run's
  `load_settings` sees `models.toml` (a test through `pipeline/runner`), and so does
  `apply draft`.
- **Fail closed**: an invalid `screen_scorer` in `models.toml` (a bare name,
  `openrouter:jev:...`, an unpriced Anthropic model, Opus 5.5), and a file that is not TOML.
  The run's score stage, the re-score drain, `score --submit`, `score --deep`, `backfill`,
  Score now and the panel each skip with the message, and **no Anthropic client is
  constructed** (the factory is patched to fail the test). An invalid `[apply]` key falls back
  with the notice. An unknown key is a warning only. A sentinel value is never echoed.
- **Writes**: two concurrent saves serialize under the lock; a stale digest gets 409 and writes
  nothing; **saving two rows in a row from one page view succeeds**; the file is 0600; the swap
  changes only the saved fields (a pending `config.toml` edit is not applied); changing only
  the effort writes only `effort`; a non-loopback peer gets 403 on every write and sees the
  read-only page with commands (including the container pasta fallback); `refresh` and
  `local/check` get 403 under `--allow-remote`.
- **Effective value shown**: a `config.toml` `screen_scorer = "anthropic:claude-opus-5-5"`
  and an OpenRouter slug with no cache each render selected, with their reason, and their own
  price line; saving another row does not change them.
- **Queued re-scores**: the line appears in the preview before Save, and from Reset, Revert,
  `models set`, `models reset` and the config-watch Apply; it prices the first request and the
  widest scope, not a sum; "keep" writes the old spec into unplanned rows only.
- **Access**, table-driven per mode: each step of the subscription order with a fake `claude`
  on `PATH` and a temp `HOME` (no login file, a login file, `CLAUDE_CODE_OAUTH_TOKEN`, runner
  off, a recent `packet-cli` row with and without a login); `ANTHROPIC_AUTH_TOKEN` alone is not
  "missing" in the re-score panel, the packet page or the drafting pre-check; container mode
  gives `unknown` for drafting and fact check on both runners and no banner; host-managing mode
  judges drafting from the host environment. A test asserts the login file is never opened.
- **Local check**: a transport error and a 401 render fixed text only; `detail` never appears
  in the response or logs; ids are escaped and capped.
- **Catalog**: every `PRICING_PER_MTOK` key has an entry and the reverse; Opus 5.5 is not
  offered for screening or either fact check; `haiku` and Haiku 4.5 are not offered for CLI
  drafting; Sonnet 5.5 is offered for screening only when M0's reservation is present; an
  old-format OpenRouter cache still loads; a cache with `structured: false` hides that slug.
- **Banners**: each page in the table shows the line for `missing` and blocked uses and nothing
  for `likely` or `unknown`; every link resolves (019's link test, extended to `/models`).
- **Browser check** before merge, against a copy of your database: light, dark, 375 px; save
  two rows, revert, and Save + re-score up to the confirm dialog, then Cancel.

## Proposed tickets

Not filed yet; the PM files them after you approve. Labels `area:console`, `area:scoring`;
worker and reviewer per the CLAUDE.md table.

| # | Ticket | Difficulty | Worker / reviewer | Depends on |
|---|---|---|---|---|
| M0 | Per-model spend reservation: `submit_batch` and the sync path (`screen.py:527`, `911`), `_pending_batch_usd` (per batch, from `score_batch.model`) and `rescore.cost_per_job`'s no-history fallback all use max($0.002, measured, table price at `TYPICAL_USAGE`); the Anthropic re-score batch capped at `plan.max_usd / per_job` (`rescore.py:634`); `/prefs`' estimate (`prefs.py:737`) priced for the current screening model; test: Sonnet 5.5 screening never submits more than the cap covers at its table price. Shares or replaces the PM's separately filed cap bug | hard (money path) | opus / opus at higher effort | |
| M1 | `scoring/catalog.py`: Anthropic table keyed on `PRICING_PER_MTOK` with per-use rules for both runners (checked against the platform docs, sources in the PR), CLI aliases, Jev default, OpenRouter cache `models` map (backward compatible), the load-only cache reader, the refresh function; per-use price estimates with their sources; `generator.estimate` per model; the fact-check estimate and cap checks priced from `api_entailment_model` | hard (estimates feed the drafting cap preflight and `apply draft`'s confirm) | opus / opus at higher effort | |
| M2 | `models.toml`: `load_layers()`, key allowlist, per-key validation, unknown keys as warnings, fail closed for screening and deep through `scorer_problems` on every spending path (drain, backfill, Score now, extension, panel), drafting fallback with notice; atomic locked write with digest; changed-fields-only writes; `model_change` migration, history and revert; the queued re-score line and `keep` choice as functions; `jobhunter models show/set/reset/catalog`; `jobhunter paths` row; run summary line | hard (every process's config loading, a migration, it decides what the nightly run spends) | opus / opus at higher effort | M1 (use rules; may stub them and land first) |
| M3 | `model_access.py` on `credential_for`: providers, the subscription order (login-file path confirmed on the real CLI), the per-mode table, `problems()`; the re-score panel's scorer list, `packet_docs.runner_status` and `generator._preflight` moved off bare `ANTHROPIC_API_KEY` | hard (credentials, the drafting money path) | opus / opus at higher effort | S1 `a1af329` |
| M4 | `/models` page and its routes: nav, Access rows (S6 buttons when present, else commands), the local server check route, use rows with the effective value selected, preview, Save under `settings_lock` (field-only swap), Reset, Reset file, Load, History and Revert, digest swap, loopback-peer gating with the read-only command view, OpenRouter refresh, source labels, the `/models` banner, dark mode and 375 px | hard (a live settings swap under concurrent requests, like S12a) | opus / opus at higher effort | M1, M2, M3; S3a `9512dd7` (shared status partials); S6 `531b700` optional |
| M5 | Save + re-run: inline re-score panel with the new scorer (no request until confirm), the queued re-score line in previews and the two Save buttons, deep `--deep-group` list with estimates, packet list and Save and go back; `available_scorers` from the catalog; Sonnet 5.5 offered for screening | hard (re-score money path) | opus / opus at higher effort | M4, M0 |
| M6 | Help: `help/models.md`; the headings the banners link in `scorers.md`, `openrouter.md` and `drafting.md` if missing; troubleshooting entries for a blocked use and a broken `models.toml` | mechanical | haiku / sonnet | S11a, S11b, S11c |
| M7 | Banners on the re-score panel, packet page, captured page and Score now panels; blocked-use lines; the "no model access" line; link tests | normal | sonnet / opus | M3, M4, M6 |
| M8 | Config watch on the layers: `models.toml` fingerprinted, layer diffs, "no effect: set on /models", problems as validation errors, exempt from the replaced-file rule, self-write baseline, the shared `settings_lock` in Apply, the queued re-score line next to Apply | normal | sonnet / opus | M2, M4, S12a |
| M9 | Plain `score --submit` uses the nightly `new_only` rule unless `--scorer` is given, so a model change never re-screens scored jobs without a confirmed re-score; help and changelog note | normal | sonnet / opus | |
| M10 | Readiness and `doctor`: the 019 inventory row "Model choices" (broken file, blocked use), model access lines in `doctor` and `--json`, `doctor -v` prints each model setting's source | normal | sonnet / opus | S1 `a1af329`, S2 `3cdd9d1`, M2, M3 |

Order: M0, M1, M9 can start now; M3 after S1. M2 after M1 (or against a stub). M6 whenever
S11a-c land. M4 after M1, M2, M3. M5 after M4 and M0. M7 after M4 and M6. M8 after M2, M4 and
S12a. M10 after S1, S2, M2 and M3.

**Estimate**, by the calibrated rule (ticket wall-clock: mechanical 0.75 h, normal 1.4 h, hard
2.1 h; schedule = sum / 3, plus 30%, range 0.7x to 1.6x; floor = the longest dependency chain,
which is also the low end of the range):

- Sum: 7 hard (14.7 h) + 4 normal (5.6 h) + 1 mechanical (0.75 h) = 21.05 h.
- Schedule: 21.05 / 3 = 7.0 h, plus 30% = 9.1 h.
- Floor: the longest chain inside 020 is M1, M2, M4, M5 = 4 x 2.1 = **8.4 h** (6.3 h if M2 lands
  against a stub of M1's use rules). It is below 9.1 h, so it does not bind.
- **About 9 h** of continuous run once S1 has landed, range about 8.4 to 14.6 h.
- Tokens, per ticket (normal 0.6M, mechanical 0.5M, hard 2.0M): 7 x 2.0 + 4 x 0.6 + 0.5 =
  **about 16.9M**.
- Not included: waiting on 019's tickets (S1, S2, S3a, S6, S11a-c, S12a), PR merge waits, and
  your review.

## Open questions

Answered 2026-10-11: choices go in a separate, console-managed `models.toml` (TOML, to match
`config.toml`), never in `config.toml`. Placed in the data dir for containers
([Storage](#storage)).

1. **A console button for the deep pass?** Today only `jobhunter score --deep N` and
   `--deep-group GID` run it, and a button would add a new paid background run to the console.
   **Recommended: not now.** The row lists the `--deep-group` commands for your top groups with
   their estimates; a button, or a `--redo` flag, can be a later ticket if you use the deep pass
   often.

## History

### Revision 2: adversarial review

16 verified findings (5 blocking), all addressed:

- **Deep re-run was wrong.** Since PR 117 `score --deep N` skips groups deep-scored under any
  model and scores the next N. The spec now says a new deep model applies to unscored groups
  only, and re-running a group is `--deep-group GID`.
- **Flat batch reservation.** Sonnet 5.5 for screening would overrun caps about 2.5x. New
  prerequisite M0 makes the reservation per model and caps the Anthropic re-score batch at its
  plan; Sonnet 5.5 is not offered for screening until it lands.
- **A broken `models.toml` no longer falls back** to another provider for screening or deep: the
  use is blocked on every spending path. Keys are validated one by one; unknown keys warn.
- **The model in effect is always shown and selected**, with its reason when the catalog would
  not offer it, and Save writes only the fields you changed.
- **Routes fully specified**: gates per route, the read-only command view for a non-loopback
  peer (pasta fallback), the digest returned and swapped after every write.
- Smaller: the queued re-score line before every model change, priced the way the drain runs,
  with a keep-old-model choice; config watch built on the loader's layers with one settings
  lock and no replaced-file rule for `models.toml`; CLI-runner use rules; the local check as its
  own route returning ids, never `detail`; drafting rows judged where drafting runs (unknown in
  containers, the host in host-managing mode); spend history no longer outranks a missing
  login; model-aware `/prefs` and fact-check estimates; no `catalog()` calls on render or Save,
  tested with a recording transport and a stale cache; `score --submit`'s re-screening noted
  and M9 proposed; M1 and M4 rated hard, M10 added for readiness and `doctor`, the estimate
  restated.

### Revision 1

First draft, from the code on `main` at `e575ff5` plus PR 117 (`15f7012`) and 019 revision 4b
(PR 118).
