# 020 — Choosing models

Status: **draft for review, revision 1** (bug `051d76b`). Written 2026-10-11. Assumes PR 117
(bug `15f7012`, Claude 5.5 defaults and the verified price table) has merged, and builds on
[019](019-setup-and-readiness.md) revision 4b (readiness checks, help pages, config watch).

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

## Decisions

| Question | Choice | Why |
|---|---|---|
| Where choices are saved | **`<data dir>/models.toml`**, written only by `/models` and `jobhunter models set`, layered over `config.toml`, whose model keys still apply where the file is silent | Your decision (2026-10-11): a separate file, not `config.toml`. Data dir because containers mount only `config.toml` itself, read only ([Storage](#storage)) |
| Does Save spend? | **Never.** Save + re-run only opens the confirm step that already exists for that use | A model change can make the nightly run cost more; a re-run costs money now. Both stay your call |
| Which subscription checks are free and automatic | `claude` on `PATH`, a `stat` of the CLI's login file, `CLAUDE_CODE_OAUTH_TOKEN` set, the runner state, and recent subscription drafts. `claude auth status` runs only when you press **Check (free)** | 019 rule 1: no subprocess while rendering a page |
| Models that cannot do a use | **Not offered** for it. Opus 5.5 is not offered for screening or the fact check: its thinking cannot be turned off, and both calls are bounded | PR 117 `THINKING_OFF` (`scorers.py`): the screen and the check send thinking off. Opus 5.5 gets nothing there, so its thinking would eat the 2,048-token screen budget (`screen.MAX_TOKENS`) |
| An Anthropic model with no price | **Refused** on save | `compute_cost` raises `no pricing for model` (`scorers.py:97` after PR 117), and spend caps would not see the cost |
| A broken `models.toml` | **Ignored whole**: `config.toml` values apply. Reported on `/models`, `/setup`, `doctor` and the run summary | Scoring already fails soft (019). A file the console writes must not stop ingest |

Recorded on bug `051d76b`.

## Every model use (checked in code, 2026-10-11, with PR 117)

| Use | Setting, default | Providers allowed | Used by | Re-running means |
|---|---|---|---|---|
| **Screening** (Stage 2) | `[scoring] screen_scorer`, `anthropic:claude-haiku-5-5` (`config.py:233`) | `anthropic`, `openrouter`, `openai-compat`, `local`, `jev`, `decisions` (`config.SCORER_PROVIDERS`, `config.py:191`) | The nightly run's score stage (`pipeline/runner.py:433`); `score --submit` and `--collect` (`cli.py:546`); the re-score panel's default (`console/rescore_routes.py:51`); Score now on captured and packet pages and the extension (`apply/score.py:128`, `ext_routes.py:472`); `backfill`; `llm bench`'s default (`cli.py:1731`) | A **confirmed re-score** (`scoring/rescore.py`, [014](014-preferences-console.md#re-score-now)) with the new scorer, scope "recent" (open A-E, last 14 days) or "all". Eligibility is per model (`screen.py` `_ELIGIBLE` keys on `f.model`), so every group in scope is eligible again. Without a re-run, only new jobs use the new model: the nightly run takes groups with no screen score under any model (`new_only`) |
| **Deep pass** (Stage 3) | `[scoring] deep_scorer`, `anthropic:claude-opus-5-5` (`config.py:234`) | `anthropic` only (`check_scorer_spec(..., anthropic_only=True)`, `config.py:217`) | `jobhunter score --deep N` only (`cli.py:571`). No console route runs it | `score --deep N` again: the shortlist skips groups that have a deep score **for this model** (`deep.py:211`), so the top N are deep-scored again under the new one |
| **Packet drafting** | `[apply] runner` `cli`; `cli_model` `opus` (a CLI alias); `api_model` `claude-opus-5-5`; `effort` `medium` (`config.py:307`-`310`) | `cli`: the `claude` CLI on your subscription. `api`: the Anthropic SDK | Generate on the packet page (`console/packet_routes.py:397`) and `jobhunter apply draft` (`generator.py:1068`, `1077`) | Generate again on one packet. Each click makes a new version; old versions stay. Never in bulk |
| **Fact check** (entailment) | `[apply] entailment_check` on; `cli_entailment_model` `haiku`; `api_entailment_model` `claude-haiku-5-5` (`config.py:315`-`317`) | Same runner as the draft it checks | After every generation (`generator.py:905`, `917`) | Runs again with the next Generate. There is no check-only re-run today |

Not separate uses:

- **Score now** and **backfill** use the screening choice.
- **`llm bench --scorer`**, **`score --scorer`** and the re-score panel's scorer picker override
  the choice for one run and save nothing. They keep doing so.
- **`eval`** calls no model (`scoring/evaluate.py`: "Read-only: nothing here calls a model"). Its
  `--model` filters stored scores.
- Mail parsers, the prefilter and buckets call no model.

Two things the inventory turned up that this spec fixes:

- **Three places check only `ANTHROPIC_API_KEY`**: the re-score panel's scorer list
  (`rescore_routes.py:57`), the packet page's runner status (`console/packet_docs.py:285`) and
  the drafting API pre-check (`generator.py:682`). The SDK also accepts `ANTHROPIC_AUTH_TOKEN`,
  a profile and the other sources 019 lists, so someone with only those sees "no key" in three
  places while scoring works. M3 sends all three through 019's `credential_for`.
- **Queued re-scores follow the current model.** A re-score queued by a paid `/prefs` save has
  no plan until it runs. The nightly drain (`score --rescore-pending`, from
  `jobhunter-collect.service` and `jobhunter-ctr-rescore`) plans it with
  `row["scorer"] or scoring.screen_scorer` (`rescore.py:578`). Changing the screening model
  therefore changes what those requests cost tonight. `/models` says so before you save
  ([Save](#save)).

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

| Provider | Local, free, on every render | Only on **Check (free)** (019 S6) |
|---|---|---|
| Anthropic API | `credential_for`: `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, a profile or config dir, workload identity. A source only the SDK can judge is `likely` ("via SDK profile, not checked") | `models.list`: key accepted |
| OpenRouter, Jev, decisions | The variable named by `scoring.openrouter.api_key_env` | `GET /key` on openrouter.ai only: accepted, credit left |
| openai-compat | The variable named by `scoring.openai_compat.api_key_env` (or none, by design) | none (no free check for an unknown endpoint) |
| Local server | Nothing to detect: `unknown` until checked. Never `missing` | `local_server_status` (`scorers.py`): reachable, and **the loaded model list**, which fills the dropdown |
| Claude subscription (the `cli` runner) | see below | `claude auth status --json` through S6's uncached `auth_status()`: logged in with claude.ai |

**The subscription, without running anything.** In host mode, in this order:

1. `shutil.which("claude")` (`cli_runner.find_binary`) is None: **missing**, "the claude command
   is not installed".
2. The runner is off (`runner_state.load`, `<data dir>/apply-runner.json`): **missing**, with
   the stored reason ("not a claude.ai subscription login" and the like).
3. This process already passed `check_auth` (`cli_runner._AUTH_OK`, filled only by a real draft),
   or a `packet-cli` row in `llm_spend` from the last 30 days: **ok**, "a subscription draft
   worked at <time>".
4. `CLAUDE_CODE_OAUTH_TOKEN` is set (presence only), or the CLI's login file exists
   (`$CLAUDE_CONFIG_DIR/.credentials.json`, else `~/.claude/.credentials.json`; a `stat`,
   never opened): **likely**, "claude is installed and a login exists; not checked".
5. Otherwise: **missing**, "claude is installed but no login was found".

The login file path is what Claude Code uses on Linux. M3's worker confirms it against the
installed CLI and records the result in the PR. If the CLI keeps its login somewhere a `stat`
cannot see, step 5 becomes `unknown`, never `missing`. Step 4 cannot tell a claude.ai login
from a Console API-key login; only `auth status` can, which is why it stays `likely`. A real
draft still runs `check_auth` first (`cli_runner.py:298`) and turns the runner off on a wrong
login, as today.

**Modes** (019 [Modes](019-setup-and-readiness.md#modes)):

- **container**: the console has its own secrets (the console column of 018's matrix includes
  `jobhunter_anthropic`, `jobhunter_openrouter`, `jobhunter_openai_compat` and `jobhunter_llama`),
  so API rows work as on the host. Drafting runs on the host (`jobhunter apply draft`, 018 D16)
  and the console has no `claude`: the subscription row is **unknown**, "drafting runs on the
  host; check there with `claude auth status`". Unknown never raises a banner.
- **host managing containers**: secret rows use 019's drop-in rule (a secret counts when
  preflight's drop-in for the unit exists). The subscription row is the host's own, as above,
  because `apply draft` runs on the host.
- **The nightly run sees other things.** For the screening row, the effective status is 019's:
  the worse of the console's view and a fresh `run_inputs` snapshot (S4), with the disagreement
  shown. Before S4 lands, the row uses the console's view only.

## Model catalog

`src/jobhunter/scoring/catalog.py` builds each dropdown. Every entry has the model, a short
label, the uses it can serve, and a price. No dropdown is filled from the network while a page
renders.

| Provider | Where the options come from | How fresh |
|---|---|---|
| Anthropic | A static table in `catalog.py`, keyed by `PRICING_PER_MTOK` (`scorers.py`, PR 117: Haiku 5.5, Sonnet 5.5, Opus 5.5, and older models kept for stored spend). Each entry says which uses it serves. Older models (`claude-haiku-4-5`, `claude-opus-5`, `claude-opus-4-8`, `claude-sonnet-5`) sit under "Older" and are offered only where they still work | Changes with the code. The page shows "prices checked 2026-10-11" from a constant next to the table. A test fails when a `PRICING_PER_MTOK` key has no catalog entry, or the other way round |
| CLI aliases | `opus`, `sonnet`, `haiku`, which the CLI resolves to whatever your plan serves, plus "a specific model" (any Anthropic id from the table) | Static |
| OpenRouter | The catalog cache `<cache dir>/openrouter_models.json` (`scoring.openrouter.catalog_cache`), which `OpenRouterScorer.catalog()` already keeps (`scorers.py:486`, refreshed at most daily, `CATALOG_MAX_AGE_S`). Only models that support structured output are offered, because `provider.require_parameters = true` refuses the others | The cache's `fetched_at` is shown. **Refresh list (free; contacts openrouter.ai)** fetches the public `/models` list on click. It needs no key |
| Jev, decisions | `decisions.JEV_DEFAULT_MODEL` (`typesafe/jev-1.13`), plus a free-text slug | Static |
| Local | The configured model, plus the models the server reported on the last **Check server (free)** in this page view | Only on click |
| openai-compat | Free text: the endpoint is yours and has no catalog | n/a |

The OpenRouter cache today keeps only prices (`{"fetched_at", "prices": {slug: [in, out]}}`).
M1 adds a `models` map beside it (`{slug: {"name", "context_length", "structured": bool}}`,
read from `supported_parameters`). Readers that know only `prices` keep working, and a cache
without `models` offers its priced slugs with "structured output not known".

**Which Anthropic models serve which use** (M1 checks each against the platform docs and
records the source in the PR; the table below is the starting point):

| Use | Offered | Not offered, and why |
|---|---|---|
| Screening | Haiku 5.5 (default), Sonnet 5.5; Haiku 4.5 under Older | Opus 5.5: thinking cannot be turned off (above) |
| Deep pass | Opus 5.5 (default), Sonnet 5.5 | Haiku 4.5: it rejects `output_config.effort`, and the deep pass sends effort `high` (`deep.py` `EFFORT`) |
| Drafting, `api` | Opus 5.5 (default), Sonnet 5.5 | Haiku 4.5: same effort reason (`generator.api_params`) |
| Fact check, `api` | Haiku 5.5 (default), Sonnet 5.5; Haiku 4.5 under Older | Opus 5.5: same as screening |

**Prices shown per use.** Each option shows a cost in the unit that use spends in. Estimates
use measured history first, as the existing estimates do:

| Use | Per unit | "At your volume" |
|---|---|---|
| Screening | Per 1,000 jobs: the measured average `fit_score.cost_usd` for that model (`rescore.cost_per_job`); else the price table at `backfill.TYPICAL_USAGE`, batch-priced for Anthropic (`backfill.per_job_cost`); else the decisions fallback; else the OpenRouter catalog price at typical tokens; `local:` is $0 | Jobs screened in the last 7 days × per job, as "about $X a week" |
| Deep pass | Per job: the measured average for `tier = 'deep'` and that model; else the price table at 006's typical deep tokens | × `deep_shortlist` per `score --deep` run |
| Drafting | Per resume, letter and answer: `generator.estimate`, taken per model (today it takes the median over every model; M1 filters on `packet_document.model`) | none. `cli` shows "$0 on your subscription; about $X at API prices", from the median `api_equiv_usd` |
| Fact check | Per draft: the price table at the check's typical tokens, else `ENTAILMENT_ESTIMATE_USD` | none |

The page states which source each number came from ("measured over 412 jobs", "price table",
"no price: the cap check uses $0.04 a request"). For screening it also says when the cap
check reserves more than the price table: a model with no measured history is reserved at
`screen.ESTIMATED_COST_PER_REQUEST_USD` ($0.002 a job), which is several times Haiku 5.5's
table price. That reservation stays as it is, on purpose: a cap must not be overrun on an
estimate.

**Measure before switching** ([016](016-alternative-scorers.md#ground-rules) rule 1). Each
screening option shows how many of your labeled jobs it has scored (a count from `fit_score`
and the triage labels), and the `jobhunter eval --compare` line to measure it. The page does
not run `eval`.

## Storage

**Your decision (2026-10-11): a separate, console-managed `models.toml`, not `config.toml`.**
TOML rather than YAML, so it shares `config.toml`'s format and loader (`tomllib`, the
`_deep_merge` in `load_settings`). This section says where it lives and how it layers.

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
| Config watch (019 S12a) | The console's own write looks like your edit | One more watched file, with its own source label | Not watched; the profile is reloaded per use |
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
"saved on /models at 14:05", "config.toml" or "default", and when both files set a key, "config.toml
says X; /models overrides it" with Reset. `jobhunter models show` and `doctor -v` print the same
source, and the config watch labels an overridden `config.toml` edit "no effect: set on /models".

- **What the file may hold.** Only `[scoring] screen_scorer`, `deep_scorer`, and `[apply]
  runner`, `cli_model`, `api_model`, `effort`, `entailment_check`, `cli_entailment_model`,
  `api_entailment_model`. Every one is in 019 S12a's `RELOADABLE` set. Any other key makes the
  file invalid. Caps, `api_key_env` and endpoints stay in `config.toml`.
- **Loading.** `load_settings` builds settings from `config.toml` as now, which settles
  `paths.data_dir`, then reads `<data dir>/models.toml`, merges it with the existing
  `_deep_merge` (`config.py:367`) and validates again. Every process does this at start: the
  nightly run, every CLI command, `apply draft` on the host, every container.
- **Broken file.** Bad TOML, an unknown key, or a value that fails validation: the whole file is
  ignored and `config.toml` values apply. `Settings` carries the problem. `/models` shows it
  with "Reset file", 019 adds an inventory row "Model choices" (broken), `doctor` lists it,
  and the run summary says `models.toml ignored: <fixed reason>`. Error text follows 019 B's
  rules: location and message, never `str(exc)`.
- **Writing.** Atomic (temporary file, `fsync`, rename), mode 0600, under a `flock` on
  `models.toml.lock`. The form carries the SHA-256 of the file as the page read it. A mismatch
  answers 409, "changed elsewhere; reload", and writes nothing. The file starts with a comment
  saying it is written by `/models` and `jobhunter models`, and overrides `config.toml`.
- **Validation on save.** Scorer specs through `check_scorer_spec` (deep with
  `anthropic_only=True`), which already rejects bare names and `openrouter:jev:...`. An
  Anthropic model must be in `PRICING_PER_MTOK`. The catalog's use rules apply (Opus 5.5 is
  refused for screening). An OpenRouter slug not in the catalog, a local model not in the
  server's list, or a model whose credential is missing saves only after an explicit
  "save anyway", with the reason shown. On the host the console and the nightly run can see
  different keys (019 [What the nightly run sees](019-setup-and-readiness.md#what-the-nightly-run-sees)),
  so a key the console cannot see is not proof the run cannot.
- **History and revert.** A migration adds `model_change` (`id`, `at`, `use`, `field`,
  `old_value`, `new_value`, `source` in `ui`, `cli`, `reset`, `revert`). `/models` lists the
  last 20 with **Revert**, which is a new save, not a rewind. Model ids hold no personal data.
- **Seeing where a value came from.** Every row on `/models` shows its source: "saved on
  /models at 14:05", "config.toml", or "default". When `config.toml` also sets an overridden
  field, the row says "config.toml says X; /models overrides it" with **Reset**, which removes
  the key from `models.toml`. `jobhunter paths` lists `models.toml`, and `doctor -v` prints the
  source of each model setting.

### Config watch (019 S12a)

- S12a's watcher adds `models.toml` to the files it fingerprints. A change made outside the
  console (`jobhunter models set`, or a hand edit) is diffed against the running settings like
  `config.toml` and offered as Apply. Every field in the file reloads in place.
- The console's own save updates the watcher's fingerprint after the rename, so it never
  notifies itself.
- A `config.toml` edit to a field that `models.toml` overrides is listed as "no effect: set on
  /models", the way S12a lists a `console.port` set on the command line, with a link to the
  row's Reset.
- Before S12a lands, `GET /models` reads `models.toml` (one small read) and, when it differs
  from the running settings, shows "changed outside the console: [Load]".

### `jobhunter models`

For the terminal and for containers (`jobhunter-ctr models ...`):

| Command | Does |
|---|---|
| `models show` | Each use: effective value, source, credential status (local checks only) |
| `models set USE VALUE` | Validates and writes, as the page does. `--force` for "save anyway" |
| `models reset USE` | Removes the key: `config.toml` or the default applies |
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
  Also used by: Score now, backfill, llm bench
  [ Save ]  [ Save + re-score… ]

Deep pass                                            default
  [ Claude Opus 5.5 ▾ ]   about $0.05 a job; 40 a run, about $2.00
  [ Save ]  then run: jobhunter score --deep 40

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
alone). The Check buttons are 019 S6's live checks (`POST /setup/live/{check_id}`): shown only
to a loopback peer, refused under `--allow-remote`, booleans only. Their result replaces the
row and, through an out-of-band swap, the banner, for that page view. Nothing is cached or
stored (019: "never stored or logged"). Before S6 lands, each row shows the command instead
(`jobhunter doctor --live`).

**Use rows.** Each dropdown lists usable options first, then options whose credential is
missing ("needs an Anthropic key"), then Older. An option the use cannot run on is not listed.
A non-Anthropic choice shows `privacy_notice` (`scorers.py`), which says where your resume and
the postings go. Changing the dropdown updates the price and access lines through htmx before
anything is saved.

### Save

`POST /models/{use}`. Saving spends nothing:

1. Validate and write `models.toml` ([Storage](#storage)); add a `model_change` row.
2. Swap the running settings: build a copy of `app.state.settings` with **only the saved
   fields** changed (`model_copy` plus validation) and assign `app.state.settings` once, as
   019 S12a's Apply does. Only the saved fields change, so an unrelated `config.toml` edit
   still waits for its own Apply.
3. Answer with the row, now showing "saved at HH:MM", plus what takes effect when:
   - **Screening**: "New jobs from the next run use Haiku 5.5. 1,840 open jobs keep their
     scores from Haiku 4.5." When unplanned re-scores are queued: "2 queued re-scores will run
     tonight with the new model, about $0.48" (planned with `make_plan` for the new spec).
     A run already waiting for the run lock read its settings before it waited (019 B) and
     uses the old model.
   - **Deep pass**: the command and its estimate.
   - **Drafting and the fact check**: "the next Generate uses it".

Writes go through the existing middleware (loopback `Host`, same-origin `Origin`) and also
require a **loopback peer**, like 019 S12a's Apply. A model change does not spend now, but it
decides what the nightly run spends, so a LAN client under `--allow-remote` gets 403.

### Save + re-run

Never runs anything by itself. It saves, then opens the existing confirm step for that use:

| Use | Save + re-run opens |
|---|---|
| Screening | The **re-score panel**, inline on `/models` (`_rescore_panel.html`, `GET /prefs/rescore/panel?scorer=<new spec>`), with the new model selected and the scope choice. You see the job count, the estimate and the spend ceiling, then press Run and confirm. The plan token (`rescore_routes.py:152`) makes the run match what you confirmed. With an Anthropic model it only submits a batch; the nightly collect, or `score --collect-pending`, finishes it, as today |
| Deep pass | No button. The row shows `jobhunter score --deep N` with its estimate ([open question 1](#open-questions)) |
| Drafting, fact check | No bulk re-run. The row lists up to five packets whose current documents came from another model (`packet_document.model`), each linking to its packet page, where Generate shows its estimate (API) or paid-usage confirm (subscription on extra usage) as today. When `/models` was opened from a packet page (`?next=/packet/N`, checked with `safe_next`), the button is **Save and go back** |

The re-score panel's own scorer list (`available_scorers`, `rescore_routes.py:45`) shows the
effective screening choice as "your default", plus the usable catalog choices from M1.

## Banners elsewhere

One partial, `_model_access.html`, renders `model_access.problems()` for the uses a page
needs. It shows only `missing` (never `likely` or `unknown`), names the use and its
consequence, and links the help topic and the `/models` row:

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
and a new topic `models.md` ("Choosing models": the page, precedence, `models.toml`,
`jobhunter models`). 019's drift tests check every link and anchor.

## Console rules

- No secret is typed, shown or stored. Access rows use `SecretState.describe` wording.
- No page render, banner or `doctor` run makes a network call, a subprocess or a model call. The
  OpenRouter refresh, Check server and the S6 checks run only on click, and none calls a model.
- `Cache-Control: no-store`, like every page.

## Testing

- **No spend**: `GET /models`, every Save, Reset and Revert with the `anthropic` client, the
  `httpx` transport and `subprocess.Popen` patched to raise. Save + re-score returns the panel
  and creates **no** `rescore_request`; only the existing confirm route does.
- **Precedence**: a default, a `config.toml` value and a `models.toml` value per field; the
  `--scorer` flag beats all three; `overrides` beats the file. The nightly run's
  `load_settings` sees `models.toml` (a test through `pipeline/runner`), and so does
  `apply draft`.
- **Broken file**: bad TOML, an unknown key, a bare scorer name, `openrouter:jev:...`, an
  unpriced Anthropic model, Opus 5.5 for screening. Each ignores the file, keeps
  `config.toml`'s values, and shows a fixed message on `/models` and in `doctor`, with a sentinel
  value never echoed.
- **Writes**: two concurrent saves serialize under the lock; a stale digest gets 409 and writes
  nothing; the file is 0600; the swap changes only the saved fields (a pending `config.toml`
  edit is not applied); a non-loopback peer gets 403.
- **Access**, table-driven per mode: each row of the subscription order with a fake `claude` on
  `PATH` and a temp `HOME` (no login file, a login file, `CLAUDE_CODE_OAUTH_TOKEN`, runner off,
  a recent `packet-cli` spend row); `ANTHROPIC_AUTH_TOKEN` alone is not "missing" in the
  re-score panel, the packet page or the drafting pre-check; container mode gives `unknown` for
  the subscription and no banner. A test asserts the login file is never opened.
- **Catalog**: every `PRICING_PER_MTOK` key has an entry and the reverse; Opus 5.5 is not
  offered for screening or the fact check; an old-format OpenRouter cache still loads; a cache
  with `structured: false` hides that slug.
- **Estimates**: measured history wins over the table; the queued re-score warning appears
  only for unplanned pending requests and prices them with the new model.
- **Banners**: each page in the table shows the line for `missing` and nothing for `likely`
  or `unknown`; every link resolves (019's link test, extended to `/models`).
- **Browser check** before merge, against a copy of your database: light, dark, 375 px; save,
  revert, and Save + re-score up to the confirm dialog, then Cancel.

## Proposed tickets

Not filed yet; the PM files them after you approve. Labels `area:console`, `area:scoring`;
worker and reviewer per the CLAUDE.md table.

| # | Ticket | Difficulty | Worker / reviewer | Depends on |
|---|---|---|---|---|
| M1 | `scoring/catalog.py`: Anthropic table keyed on `PRICING_PER_MTOK` with per-use rules (checked against the platform docs, sources in the PR), CLI aliases, Jev default, OpenRouter cache `models` map (backward compatible) and refresh, local list from `local_server_status`; per-use price estimates with their sources; `generator.estimate` per model | normal | sonnet / opus | PR 117 |
| M2 | `models.toml`: two-pass `load_settings`, key allowlist, fail-soft with a reported problem, atomic locked write with digest check, validation (`check_scorer_spec`, price rule, use rules), `model_change` migration, history and revert functions, `jobhunter models show/set/reset/catalog`, `jobhunter paths` row, run summary line, 019 inventory row "Model choices" | hard (every process's config loading, a migration, it decides what the nightly run spends) | opus / opus at higher effort | PR 117, M1 (use rules; may stub them and land first) |
| M3 | `model_access.py` on `credential_for`: providers, the subscription order (with the login-file path confirmed on the real CLI), modes, `problems()`; the re-score panel's scorer list, `packet_docs.runner_status` and `generator._preflight` moved off bare `ANTHROPIC_API_KEY` | hard (credentials, the drafting money path) | opus / opus at higher effort | S1 `a1af329` |
| M4 | `/models` page: nav, Access rows (S6 buttons when present, else commands), use rows with htmx dropdowns, price and access lines, privacy notice, Save (field-only swap, loopback peer), Reset, History and Revert, source labels, the `/models` banner, dark mode and 375 px | normal | sonnet / opus | M1, M2, M3; S3a `9512dd7` (shared status partials); S6 `531b700` optional |
| M5 | Save + re-run: inline re-score panel with the new scorer (no request until confirm), queued re-score warning and its estimate, deep command with estimate, packet list and Save and go back; `available_scorers` from the catalog | hard (re-score money path) | opus / opus at higher effort | M4 |
| M6 | Help: `help/models.md`; the headings the banners link in `scorers.md`, `openrouter.md` and `drafting.md` if missing; troubleshooting entries for a broken `models.toml` | mechanical | haiku / sonnet | S11a, S11b, S11c |
| M7 | Banners on the re-score panel, packet page, captured page and Score now panels; the "no model access" line; link tests | normal | sonnet / opus | M3, M4, M6 |
| M8 | Config watch: `models.toml` in S12a's fingerprints and diff, self-write baseline, "no effect: set on /models" for overridden `config.toml` fields; until then, the Load prompt on `/models` (M4 ships that) | normal | sonnet / opus | M2, S12a |

Order: M1 and M3 can start once their dependencies land (PR 117; S1). M2 after M1 (or with a
stub). M6 whenever S11a-c land. M4 after M1, M2, M3. M5 after M4. M7 after M4 and M6. M8 after
M2 and S12a.

**Estimate**, by the calibrated rule (ticket wall-clock: mechanical 0.75 h, normal 1.4 h, hard
2.1 h; schedule = sum / 3, plus 30%, range 0.7x to 1.6x; floor = the longest dependency chain):

- Sum: 4 normal (5.6 h) + 3 hard (6.3 h) + 1 mechanical (0.75 h) = 12.65 h.
- Schedule: 12.65 / 3 = 4.2 h, plus 30% = **about 5.5 h**, range about 3.8 to 8.8 h.
- Floor: the longest chain inside 020 is M1, M2, M4, M5 = 1.4 + 2.1 + 1.4 + 2.1 = **7.0 h**,
  above the point estimate, so it binds: **about 7 h** of continuous run once PR 117, S1 and
  S3a have landed. Landing M2 against a stub of M1's use rules shortens the chain to 5.6 h.
- Tokens, per ticket (normal 0.6M, mechanical 0.5M, hard 2.0M): 4 x 0.6 + 0.5 + 3 x 2.0 =
  **about 8.9M**.
- Not included: waiting on 019's tickets (S1, S3a, S6, S11a-c, S12a) and PR 117, PR merge
  waits, and your review.

## Open questions

Answered 2026-10-11: choices go in a separate, console-managed `models.toml` (TOML, to match
`config.toml`), never in `config.toml`. Placed in the data dir for containers
([Storage](#storage)).

1. **A console button for the deep pass?** Today only `jobhunter score --deep N` runs it, and a
   button would add a new paid background run to the console. **Recommended: not now.** The
   row shows the command and its estimate; a button can be a later ticket if you use the deep
   pass often.

## History

### Revision 1

First draft, from the code on `main` at `e575ff5` plus PR 117 (`15f7012`) and 019 revision 4b
(PR 118).
