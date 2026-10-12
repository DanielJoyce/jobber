# Choosing models

jobhunter uses a model for four jobs. You can pick each one separately, on the
[Models](/models) page in the console or with `jobhunter models`. Nothing you pick here spends
money by itself. A change only decides what the next run, or the next click, spends.

| Use | What it does | Default |
|---|---|---|
| Screening | Scores new jobs in the nightly run, Score now and backfill | `anthropic:claude-haiku-5-5` |
| Deep pass | The second, fuller report on your top groups (`score --deep`) | `anthropic:claude-opus-5-5` |
| Drafting | Writes the resume, cover letter and answers on a packet | the `claude` command, model `opus` |
| Fact check | Checks each draft against your facts after it is written | on, model `haiku` |

Prices are in [Costs and credits](costs-and-credits.md). The scorer formats (`anthropic:`,
`openrouter:`, `local:` and the rest) are explained in [Choosing a scorer](scorers.md).

## Choosing a model per use

1. Open [Models](/models). Each use is a row with a dropdown. The value in effect is always
   selected, and the row says where it came from: `models.toml`, `config.toml` or the default.
   Options the catalog does not offer for that use are not in the list.
2. Read the line under the dropdown. It gives the price and whether the credential for that
   model is there (see [Access](#access-keys-subscription-and-local-server)).
3. Press **Save**. Saving spends nothing and does not start a run. If re-scores are queued, the
   row asks first what they should use (see [Queued re-scores](#queued-re-scores)).

**Save + re-score…** (screening) and **Save + deep-score…** (deep pass) save exactly like
**Save**, then open that use's confirm step under the row. Neither runs anything by itself:

- **Save + re-score…** shows the re-score panel for the model you just saved: how many job
  groups, the estimate and what is left of the spend cap, plus the most the run may spend for a
  model scored right away. An Anthropic model is sent as a batch and billed when the batch is
  collected, so its panel shows the estimate only. Pick the scope, then press **Re-score now** and confirm. Nothing runs if, by then, new jobs changed the
  plan, the cap no longer covers the estimate, or the screening model was changed again (in
  another tab, say); the panel shows the new numbers instead. One re-score runs at a time, so a
  second click does not start a second run. Cancel in the confirm only cancels the re-score; the
  model stays saved. Queued re-scores from Prefs stay there to run or dismiss; a re-score from
  here that finishes closes the queued ones it covers, as on Prefs.
- **Save + deep-score…** opens the deep-pass panel (the same as **Deep-score…** under the Deep
  pass row): pick N and either the next unscored groups or **Re-run my top groups** (those with
  a report from another model), then confirm the estimate. See
  [The deep pass](costs-and-credits.md#the-deep-pass). One group: its job page, or
  `jobhunter score --deep-group GID`.

Both buttons work only in a browser on the machine running the console, and are off under
`jobhunter console --allow-remote`. There, use `jobhunter score --rescore recent` (it shows the
estimate and asks first) or `jobhunter score --deep N`.

Drafting and the fact check have no bulk re-run. Each draft is a click with its own estimate, so
the next **Generate** on a packet uses the new choice.

## Where choices are saved

Choices made on the page, or with `jobhunter models set`, are written to `models.toml` in your
data folder, next to the database:

- Linux, default: `~/.local/share/jobhunter/models.toml`
- `jobhunter paths` prints the exact path (the row `models_file`). The Models page shows it too.

The file starts with a comment that says it is written by the console and by `jobhunter
models`. Edit it with the commands or the page rather than by hand. Only the keys you changed
are in it. Reset removes a row's keys again.

Only these keys can be set there: the screening and deep scorers, and the drafting runner,
models, effort and fact-check settings. Caps, key names and endpoints stay in `config.toml`.

## Precedence

Each setting takes the highest layer that sets it:

1. The built-in default.
2. `config.toml`.
3. `models.toml`.
4. A flag on one command, for example `--scorer` on `jobhunter llm bench`, or the scorer picker
   in the re-score panel. A flag changes that one run and saves nothing.

Your existing `config.toml` model keys keep working. Nothing copies them into `models.toml`, so
a hand edit to `config.toml` still takes effect for every use that `models.toml` does not set.
When both files set a key, the page shows "config.toml says X; /models overrides it" with a
Reset.

## What fail-closed means

A broken `models.toml` never makes a spending use fall back to another provider.

- **Screening or deep pass.** If `models.toml` sets `screen_scorer` or `deep_scorer` to a value
  jobhunter cannot use, that use is **blocked**. If the file is not valid TOML at all, it is
  impossible to know what it set, so **both** screening and the deep pass are blocked. Every
  path that would spend on a blocked use stops with its own message, and jobs stay queued.
  Nothing is sent to `config.toml`'s model or the default instead.
- **Drafting and the fact check.** These fall back. An invalid drafting key is ignored, the
  `config.toml` value or the default applies, and the Models page shows a notice saying so.
  Every draft shows its runner and its estimate before anything is spent.
- **Ingest** keeps running. A blocked scorer only stops scoring.

The exact messages, and what to do, are in
[Troubleshooting](troubleshooting.md#a-model-use-is-blocked-by-the-saved-choice) and
[the broken file entry](troubleshooting.md#the-models-file-is-broken).

## Model names that look like keys

To keep a pasted API key out of messages and logs, jobhunter never prints a model name that
looks like a key: a known key prefix (`AIza`, `glpat-`, `gsk_`, `hf_`, `xai-` and the like), 32
or more hex digits, a UUID, a JWT, or a segment of 32 or more letters and digits. Such a name is
shown as "(value not shown)".

Only the first group is also **refused**: `/models` and `jobhunter models set` will not save a
name with a key prefix, a long hex run, a UUID or a JWT. A name that is merely hidden (a long
unbroken segment) is still saved and still works; it just is not printed. Ordinary quantised
names such as `DeepSeek-R1-Distill-Qwen-7B-Q4_K_M` or `...-W8A8-FP8` are never hidden. A name
you cannot save can still be set in `config.toml`; it works there but is never printed.

## The Models page

- **Save** writes the changed fields and records the change in the history.
- **Reset** on a row removes that row's keys, so `config.toml` or the default applies again.
- **History** lists the last 20 changes, each with its old and new value. **Revert** writes the
  old value back as a new change. It does not rewind anything else.
- **Queued re-score buttons** appear when re-scores are queued. Save offers
  **Save; queued re-scores use the new model** and **Save; keep queued re-scores on** the old
  model. Reset and Revert show the same choice. The screening row also offers
  **Save + re-score; queued re-scores use the new model…** and **Save + re-score; keep queued
  re-scores on** the old model and "…". They save, then open the re-score panel.
- **Reset file** appears when `models.toml` is broken. It replaces the file with an empty one and
  keeps the old file beside it, named `models.toml.broken-<time>`.
- **Changed outside the console.** When `models.toml` was changed outside the console (for
  example by `jobhunter models set` in a terminal), the page says so and links to **Config
  changes** in the console. Review the change there and press **Apply now**, which takes it in
  together with any `config.toml` edit. If the file has an error, Config changes lists it and
  offers no Apply until you fix the file or press Reset file here. See [Config changes while
  the console runs](settings-and-secrets.md#config-changes-while-the-console-runs). A console
  started without the config watch shows a **Load** button here instead.
- **Refresh list** fetches the public OpenRouter model list. It needs no key and contacts
  openrouter.ai, so it runs only when you press it.
- **Check server** asks your local model server whether it answers and which models it loads.
  It runs only when you press it.

The page makes no network call and no model call when it opens or when you change a dropdown.
The only things that contact a service are the two buttons named above, and a re-score or deep
pass you confirm, which calls the model.

A browser that is not on the machine running the console sees the page read-only. Each row then
shows the `jobhunter models` command to run there instead of the buttons.

### Queued re-scores

A re-score you queued from Prefs has no plan yet. The nightly run plans it with the current
screening model, so changing that model changes what those re-scores cost tonight. Before you
save, the page says how many are queued and what they would cost with the new model. The command
line asks the same question with `--queued new` or `--queued keep`, and requires it when
re-scores are queued and the command is not running in a terminal.

## The command line

The same choices are available in a terminal, and in containers as `jobhunter-ctr models`.

```bash
jobhunter models show
jobhunter models show --json
jobhunter models set screen anthropic:claude-haiku-5-5
jobhunter models set screen local:<model> --queued keep
jobhunter models set deep anthropic:claude-opus-5-5 --force
jobhunter models reset screen
jobhunter models reset drafting
jobhunter models reset-file
jobhunter models catalog
jobhunter models catalog --refresh
```

- `show` prints each use, its value, where it came from, any problem, and, for the screening and
  deep rows only, whether the credential is set. It checks only what is on this machine and
  contacts nothing.
- `set USE VALUE` validates the value and writes it, the same as the page. It spends nothing.
  `--force` saves a value the catalog would not offer. It never saves a value that cannot work.
- `reset USE` removes the use's key from `models.toml`. `USE` can also be `drafting`, `check`
  or `all`.
- `reset-file` replaces a broken `models.toml`. Add `--force` to empty a file that is valid TOML.
- `catalog` lists the options for each use with their prices. `--refresh` first fetches the
  OpenRouter model list, which contacts openrouter.ai.

The use names are `screen`, `deep`, `draft_cli`, `draft_api`, `check_cli`, `check_api`,
`runner`, `effort` and `entailment_check`.

## Access: keys, subscription and local server

For each provider, jobhunter checks whether it can be used, and shows one of these:

- **missing**: nothing was found, so the use cannot run. The page names the use and what it
  blocks.
- **likely**: something was found but not checked, for example a login that exists but has not
  yet worked.
- **ok**: a check passed.
- **unknown**: jobhunter cannot tell from here. This is never shown as a problem.

The checks read only the environment and local files. No key value is ever shown or stored.

- **Anthropic API** (screening, deep pass, the API drafting runner): `ANTHROPIC_API_KEY`,
  `ANTHROPIC_AUTH_TOKEN`, or a profile configured through the Anthropic environment variables.
  See [Get an Anthropic API key](scorers.md#get-an-anthropic-api-key).
- **OpenRouter and Jev**: the variable named by `[scoring.openrouter] api_key_env`, which is
  `OPENROUTER_API_KEY` by default. See [OpenRouter and Jev](openrouter.md).
- **Claude subscription** (the `cli` drafting runner and fact check). It is **missing** when the
  `claude` command is not installed, or when the CLI runner is off (the page shows the stored
  reason). It is **likely** when a login is found: either `CLAUDE_CODE_OAUTH_TOKEN` is set, or the
  Claude Code login file exists (`.credentials.json` in `CLAUDE_CONFIG_DIR`, or in `~/.claude`).
  It becomes **ok** once a subscription draft has worked. See
  [Drafting](drafting.md#the-cli-runner-log-in-to-claude-code).
- **Local model server** (`local:`): jobhunter cannot detect it without asking, so it is
  **unknown** until you press **Check server**.

A key the console cannot see is not proof the nightly run cannot see it, because the two can run
with different environments. A row marked missing in the console is worth checking in the run's
log before you change a choice.

## Costs

The price shown next to each model is list price. What a run actually spends, and the caps that
stop it, are in [Costs and credits](costs-and-credits.md). Changing a model never raises a cap.
