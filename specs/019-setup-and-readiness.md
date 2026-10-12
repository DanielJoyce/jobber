# 019 — First-run setup and readiness

Status: **draft for review, revision 4b** (bug `ba4d25f`; addenda for bugs `e2f8c10` and
`ccb3b21`). Written 2026-10-11; revised three times the same day after adversarial reviews and
your answers, then extended with [help by topic](#addendum-a-help-by-topic) and
[config watch](#addendum-b-config-watch), and revised once more after their review
([History](#history)).

You asked that on first launch jobhunter guide you through supplying your resume,
preferences, alert-email setup and the rest, and say what is missing and which features will not
work until it is supplied. This spec adds three things that share one set of checks:

- **`/setup`** in the console: every input jobhunter reads, each with a status, what it unlocks,
  and how to fix it. The fix is a form on the page where the console can do the job (resume
  upload, a starter `preferences.yaml`, extension pairing), a numbered guide for email alerts,
  and the exact command otherwise.
- **A banner** on Today (`/`) and the inbox, linking to `/setup`, shown while an essential is
  missing or broken. Essentials cannot be dismissed. Recommended items can.
- **`jobhunter doctor`**: the same checks in the terminal. It exits 1 when an essential is
  missing or broken.

Nothing here adds a new input. It reports on the ones that exist and points at their fixes.

## Why

Missing inputs fail in three different ways today, and two of them are hard to notice:

- **Silent.** `scoring.screen_scorer` defaults to `anthropic:claude-haiku-5-5`. With no
  Anthropic credential, the SDK client is still constructed and the error surfaces at the batch
  submit: the score stage catches it, adds `score: submit failed (Could not resolve
  authentication method ...)` to the run summary, logs a warning, and does not change the exit
  code ("scoring trouble must not fail the ingest exit code", `pipeline/runner.py`). New jobs
  wait unscored and nothing in the console says why. A missing or invalid profile and resume
  behave the same way, as do the placeholder User-Agent contact and an empty search net.
- **Noisy but unexplained.** The run exits 1 whenever any source fails (`RunReport.exit_code`),
  and the run units have `OnFailure=`. A missing USAJOBS key raises inside the adapter, so on a
  fresh install the nightly run fails and notifies **every night**, with the cause only in the
  journal. An expired Gmail token does the same.
- **Refusals.** A broken `config.toml`, the old data layout, or a schema that needs `upgrade`
  stop commands with a clear error. Those are fine, but the console cannot start in those states,
  so only `doctor` can report them.

## Decisions

### Already made (2026-10-11)

| Question | Answer | Why |
|---|---|---|
| Where preferences live | **`profile/preferences.yaml` stays the source of truth** ([014](014-preferences-console.md#source-of-truth-stays-a-file)) | You can hand-edit and diff it, and the pipeline reads it. Moving prefs into the database is out of scope; it can be a later ticket if you stop hand-editing YAML |
| Where settings live | **`config.toml` stays a file** | It is read before the database opens: it says where the database is. In a container it is mounted read-only ([018](018-containers.md) D3) |
| Is the User-Agent contact essential? | **Yes, essential, not dismissible** | Every request to a job board otherwise carries the placeholder `<you>@example.com`; 008 says we identify ourselves honestly |
| Should a run with no usable scorer fail? | **No: it still exits 0** and reports through the banner, `doctor` and the run summary | Ingest did useful work. This is only about scoring. Source failures keep their existing exit 1 ([reconciled below](#exit-codes-of-the-run)) |
| First-run redirect | **`/` redirects to `/setup` on first run only**: an essential missing or broken **and** zero jobs | Guides a fresh install; never hides data you already have |
| USAJOBS with no credentials (S10) | **Approved: skipped** when no credentials are configured; a configured but rejected key still fails the run | A fresh install is not paged every night for a source you have not set up |

### Exit codes of the run

The two rules sit side by side and stay as they are: a failed **source** makes `jobhunter run`
exit 1 (existing behaviour, [004](004-ingest-pipeline.md)), a skipped or failed **score stage**
never does (your decision above). What changes is that both are explained: `/setup`, the banner
and `doctor` name the cause ("nightly run failing: USAJOBS key rejected"; "new jobs are
not being scored: no Anthropic credential"). Ticket S10 (approved) makes an **unconfigured**
source (no USAJOBS credentials at all) a skip, the way the email source already skips without a Gmail
token, so a fresh install stops paging you every night for something you have not set up yet.
A configured source that breaks (an expired token, a rejected key) still fails the run.

## What exists today (checked in code, 2026-10-11)

Every setting, file, secret and outside dependency a feature needs, found by grepping
`src/jobhunter`. **Level**: **E** essential (banner, not dismissible, `doctor` exit 1),
**R** recommended (dismissible), **O** optional (shown when your config chooses it, otherwise
under "More"), **info** (shown, never a fault).

| # | Input | Read by | Level | Without it, today | How it fails today |
|---|---|---|---|---|---|
| 1 | `config.toml` parses, no unknown keys | `config.load_settings` | E | Nothing runs | **Refusal**: every command and the console stop with a TOML or pydantic error naming the key. A missing file is fine (defaults) |
| 2 | Old repo-relative data layout | `legacy_data.check` | E | Commands refuse | **Refusal** with a `migrate-paths` hint |
| 3 | Database present, schema current | `ops/version.database_state`, `core/db` | E | Console and commands | Host without the deployment marker: migrates on the next command, after a backup. With the marker: **refusal** (`MigrationsPending`, `SchemaTooNew`); the console refuses at boot or answers 503 |
| 4 | `fetch.user_agent_contact` | `Settings.user_agent`, sent by `FetchContext` on every request | E | Compliance ([008](008-compliance.md)) | **Silent**: the placeholder goes out in every User-Agent |
| 5 | `preferences.yaml` present and valid | `scoring/profile.py`, runner, console | E | USAJOBS queries (`from_profile`), prefilter, scoring, buckets, packets | **Silent** in the run: log warning `profile unusable`, summary line `skipped (no profile ...)`. Console: inbox banner; `/prefs` shows the skeleton as a form (missing) or field errors (broken) |
| 6 | Resume: one readable, non-empty `.md` | `profile._resolve_resume` | E | Scoring, `scoring_version`, packet drafting | Same as 5. See [resume resolution](#resume-resolution-differs-between-commands) |
| 7 | Screen scorer usable: `scoring.screen_scorer` and its credential | `scoring/scorers.py`, runner `_score_stage` | E | All fit scores, buckets A-G, the inbox's ordering | **Silent** (above). Credential rules in [Scorer credentials](#scorer-credentials) |
| 8 | `scoring.daily_cap_usd`, `weekly_cap_usd` above 0 | `screen.remaining_budget` | E if 0 | Scoring | **Silent**: summary line `spend cap reached`. A cap reached today is a note, not a fault |
| 9 | Profile content: `target_titles` or `queries`; `salary_floor`; `current_focus`, `done_with` | USAJOBS `queries_from_profile`, prefilter, rubric | R | Empty titles and queries: USAJOBS gets **no queries** and fetches nothing. No floor: `comp` is null. No focus: little for recency weighting (bug `591fb3c`) | **Silent** |
| 10 | `USAJOBS_API_KEY`, `USAJOBS_EMAIL` | registry `auth.key_env`/`email_env`, `adapters/usajobs.py` | R | Federal jobs, the best source ([011](011-federal-and-geography.md)) | `UsajobsAuthMissing`: the source is marked broken, **the run exits 1 and `OnFailure=` fires, every night** (S10 turns "not configured" into a skip) |
| 11 | `mail.alerts_address` | `mail/setup.py`, `console/alerts.py`, `mail sample` | R | Gmail filter; the address `/alerts` tells you to sign up with | `mail setup` **refuses** the placeholder; `/alerts` shows a warning |
| 12 | Google OAuth client JSON | `mail.client_secrets_path`, `$JOBHUNTER_GOOGLE_CLIENT_SECRETS`; container `/run/secrets/google_client_secret.json` | R | Email ingest, `mail auth` | Mail source **skipped** (`Google client secrets file missing`), exit unaffected. `mail auth` refuses |
| 13 | Gmail refresh token | keyring, else `~/.config/jobhunter/gmail_token.json`; container `$JOBHUNTER_GMAIL_TOKEN_FILE` (podman secret) | R | Email ingest | Missing: source **skipped**. Expired (the app is in Testing, so a token lasts 7 days; [018](018-containers.md#gmail)): `RefreshError`, source broken, **run exits 1, `OnFailure=` fires**. No issue date is recorded, so nothing warns before it lapses |
| 14 | Gmail label and filter | `mail setup` | R | Alerts stay in your inbox; the label is empty | Only `mail status` (a Gmail API call) can tell. Local proxy: alerts arriving under the label (`mail_message`) |
| 15 | Alert subscriptions on blocked boards | `/alerts` (`alert_signup`, `mail_message`) | R | Coverage of the roughly 40 banks that block crawlers ([008](008-compliance.md)) | The checklist exists; nothing points you to it |
| 16 | Alert parsers | `mail/parsers`: `vos`, `joblink`, `neogov`, `nlx`, `generic` | info | LinkedIn alerts (bug `430a0e7`) and Craigslist saved searches (bug `b0ac328`) have no parser yet | **Silent**: their messages are labelled but yield no jobs |
| 17 | Deep pass: `scoring.deep_scorer` (`anthropic:claude-opus-5-5`) | `score --deep`, `score --collect-pending` | R with an Anthropic screen scorer, else O | Stage 3 shortlist | `score --deep` errors when run |
| 18 | Drafting runner ([017](017-assisted-apply.md)) | `apply.runner`: `cli` needs `claude` on `PATH` and a claude.ai login; `api` needs an Anthropic credential. Runner-off state in `<data dir>/apply-runner.json` | R | Generate on a packet, the entailment check | Generate shows the error and offers the other runner; never switches by itself. Containers: drafting runs on the host (`apply draft`, C9) |
| 19 | `apply.daily_cap_usd` | `apply/generator.py` | info | Paid packet drafting | 0 refuses paid runs, by design |
| 20 | Extension pairing | `<data dir>/extension.json` (`ext_pairing.status`) | O | Send to jobhunter from Chrome | The extension gets 401 |
| 21 | PDF export | `core/pdf.py` (the `browser` extra and Chromium) | O | Packet PDFs | `PdfUnavailable` with an install hint, on click |
| 22 | Scheduled runs | host: `systemd --user` timers (`schedule install`); containers: Quadlet timers | R | Nightly ingest and scoring | **Silent**: nothing runs. The `run` table's newest `started_at` shows it |
| 23 | Backups | `<data dir>/backups/`, `backups/auto/` (`core/backup.py`) | R | Recovery | Containers back up nightly (C6). Host mode has only pre-migrate backups |
| 24 | Secrets valid, `.env` files private | `secrets.startup` report, `secrets.env_file_warnings` | R | Whichever feature uses the key | A bad value is **dropped with a warning** at CLI start, then the feature fails as if unset |
| 25 | Data directory 0700, database 0600 | modes set by `init` (`ops/migrate_paths.secure_data`) | R | Privacy | **Silent** if loosened later |
| 26 | Container deployment: podman, mounts, SELinux labels, drop-ins, marker, image at the host's schema, relative paths in the DB | `ops/preflight.py` (C13) | E when the deployment marker exists | The Quadlet units | `container preflight` prints FAIL lines; the units refuse to start |
| 27 | Console exposure | `console --allow-remote`, `console.host` | info | | With `--allow-remote` there is no auth |

### Resume resolution differs between commands

`console` and `/prefs` load the profile through `load_profile_for`: the `resume_path` in
`preferences.yaml` wins, and `paths.resume_path` is the fallback. Four other call sites pass
`paths.resume_path` as an explicit argument, so it **always** wins and the YAML value is
ignored: the run's `default_profile_loader` (`pipeline/runner.py`), `_score_mode` in `cli.py`
(every `score` mode: `--submit`, `--collect-pending`, `--rescore-pending`, `--deep`, `--group`),
`eval`, and `llm bench`. With the YAML pointing elsewhere, the console shows one resume while the
nightly run and the paid re-score you confirmed on `/prefs` score with another (under a different
`scoring_version`), or find none and skip scoring with a log line.

Ticket **S0** sends every profile load through `load_profile_for`, with a test that no module
outside `scoring/profile.py` passes `paths.resume_path` explicitly, and one that the run and
`score` resolve the same file. Until S0 lands, the resume row compares the two resolutions and
shows **broken, "the console and the nightly run read different resumes"** with both paths when
they differ.

After S0 the YAML value wins inside containers too, where `JOBHUNTER_RESUME_PATH=/data/resume`
is only the fallback. A `resume_path` that is absolute or outside the data directory does not
resolve there (the `/prefs` upload can write an absolute path). In container and
container-managing modes the resume row is **broken** in that case, with the fix "upload the
resume again on `/setup`, or set `resume_path` relative to the profile folder".

## Modes

Fix commands and some checks depend on how you run jobhunter. There are three modes:

| Mode | Detected by | Secrets | Schedule | Fix commands |
|---|---|---|---|---|
| **host** | not in a container, no deployment marker | env, `.env` files | host timers (`schedule install`) | `jobhunter ...`, `~/.env` |
| **container** | `JOBHUNTER_CONTAINER=1` | `*_FILE` from podman secrets | Quadlet timers (not visible) | podman secret and `jobhunter-ctr ...` commands, run on the host |
| **host managing containers** | on the host, `<data dir>/deployment.toml` present | podman secrets, seen through preflight's drop-in files | Quadlet timers; host timers enabled are **broken** (preflight's rule) | as container |

In **host managing containers**, the host process's own environment is not what the containers
use, so secret rows do not read it. A secret counts as present when preflight's drop-in
`<unit dir>/<unit>.container.d/50-<secret>.conf` exists for the unit that needs it (a stat; the
drop-in exists only when the podman secret did at the last preflight). The scheduled-runs row
checks the `jobhunter-ctr-*` timer symlinks and marks host timers still enabled as broken, with
`jobhunter schedule uninstall`.

In **container** mode the console shows paths as the host sees them: `/data/...` as
`~/.local/share/jobhunter/...`, `/config/config.toml` as `~/.config/jobhunter/config.toml`, and
`/cache` as `~/.cache/jobhunter`, from the mount sources the base units name (`preflight`'s
`DATA_SOURCE`, `CONFIG_SOURCE`, `CACHE_SOURCE`), with the container path in brackets.

## Rules for every check

1. **Local and free.** A local check reads files (`stat` and small reads), environment
   variables, the loaded settings, and the database through a read-only connection. Nothing
   else: no network, no model call, no subprocess other than `shutil.which`, no keyring or D-Bus,
   no `chmod`, no walk of a directory tree. Rendering `/setup`, the banner, and plain `doctor`
   never spend a credit or contact anyone. Subprocesses and network calls happen only on an
   explicit action: a live-check click, `doctor --live`, or `doctor --containers`. One exception:
   plain `doctor` runs `legacy_data.check`, which may spawn a local, read-only
   `git rev-parse --git-common-dir` from a source checkout. The page and the banner never do.
2. **Format, not validity.** A key's presence, source and shape can be checked locally: the C2
   rules, and the expected prefix for Anthropic API keys (`sk-ant-`) and OpenRouter keys
   (`sk-or-`). A wrong prefix is a note ("doesn't look like an Anthropic API key"), not broken.
3. **Never show a value.** No key, token, prefix, masked key, length, email address, account or
   organization name, or contact string: not on the page, in `doctor` output, `--json`, the
   snapshot, a live-check result, or a log. Secret rows say "set (env)", "set (file
   /run/secrets/...)", "set (.env ~/.env)", "invalid (contains whitespace)" or "unset", reusing
   `SecretState.describe`. `user_agent_contact` and `alerts_address` say "set in config.toml" or
   "still the placeholder". Paths are shown, with `$HOME` as `~`.
4. **No stored free-form error text.** `source.status_note` holds `str(exc)`, which has carried
   secrets before ([018](018-containers.md) "Secret leak path"). Checks map known failures to
   fixed messages (Google `RefreshError` or `invalid_grant`: "Gmail token expired or revoked";
   `UsajobsAuthMissing`: "USAJOBS credentials missing") and otherwise show only the exception
   type and a link to `/sources`.
5. **Same answer everywhere.** One module computes the checks; the page, the banner, `doctor`
   and the snapshot only render them. Where the console and the nightly run can see different
   things, the [snapshot](#what-the-nightly-run-sees) decides.
6. **Restart notes.** The console reads `config.toml` and resolves secrets once, at start. Every
   row whose value comes from either says so: "read when the console started at HH:MM; restart
   it after changing this" with the command for the mode (`Ctrl-C` and `jobhunter console` on
   the host; `systemctl --user restart jobhunter-console` for containers). In containers, an
   editor that saves `config.toml` by rename leaves the running console on the old file
   ([018](018-containers.md) D3), which the restart also fixes. After S12 the console notices
   the change itself and offers Apply or Restart ([Addendum B](#addendum-b-config-watch)); the
   note then reads "read at HH:MM" with the start or apply time.

## The checks module

`src/jobhunter/readiness.py`:

```python
@dataclass(frozen=True)
class Check:
    id: str            # stable, from a fixed list, e.g. "scorer.screen", "gmail.token"
    group: str         # Basics, Profile, Scoring, Sources, Email alerts, Apply, Running
    level: str         # "essential" | "recommended" | "optional" | "info"
    status: str        # "ok" | "missing" | "broken" | "optional" | "unknown"
    due: bool          # ok now, but needs action soon (a Gmail token on day 6)
    title: str
    detail: str        # fixed wording, never a value
    unlocks: str
    fix: Fix | None    # a route on /setup, a command, a link, or a numbered step
    source: str = ""   # where the value came from: "config.toml", "env", "podman secret"

def run_checks(settings, *, secrets_report, now, mode, cheap_only=False) -> list[Check]: ...
def essentials_ok(checks) -> bool: ...
```

| Status | Meaning |
|---|---|
| ok | Present and well-formed |
| missing | Not supplied |
| broken | Supplied but unusable: invalid YAML, a bad secret, the placeholder, an expired token, a schema that needs `upgrade`, or the nightly run disagreeing |
| optional | An optional input you have not set, which nothing in your config needs |
| unknown | This process cannot see it and there is no fresh snapshot |

`essentials_ok` is true when every essential's **effective status** (below) is ok. `unknown`
never counts as missing: it does not raise the banner and does not change `doctor`'s exit code.
`doctor` lists unknown rows in its summary.

Reused, all within rule 1: `secrets.Report` states, `secrets.env_file_warnings`,
`ops/version.database_state`, `ext_pairing.status`, `runner_state`, `prefs_graceful.lenient_state`
(the same missing/broken/resume states `/prefs` shows), the `/alerts` checklist counts,
`preflight.secret_matrix` and `host_timers_enabled`, and `cli._absolute_path_count` (moved to
`core/dbpaths.py` so both can use it).

Not reused, because they break rule 1:

- `legacy_data.check` runs `git rev-parse` in a subprocess. **Doctor only**; on `/setup` rows 1-3
  are ok by construction, since the console refuses to start or answers 503 in those states.
  The console's startup refusal messages gain "run `jobhunter doctor` for details".
- `MailAlertsAdapter.unavailable`, `auth.load_token` and `auth.token_location` read the OS
  keyring over D-Bus, which can block or pop an unlock prompt. See [Gmail token](#gmail-token).
- `preflight.unlabelled_files` walks the data directory and the cache with a `getxattr` per
  file. It stays in `container preflight` and `doctor --containers`.
- `secure_data` changes modes. Row 25 is a `stat` of the data directory and the database only.

An autouse fixture in `tests/conftest.py` (S1) makes `keyring.get_password` raise, so no test
can read a developer's real keyring.

Each check costs a `stat`, an environment lookup or one small indexed query, once the slow
inputs are cached per process: the registry's secret names and `secret_matrix` (parsing
`registry.yaml` takes about 0.5 s; take the USAJOBS names from the startup `secrets.Report`, or
pass `registry_rows` loaded once at console start; `functools.cache` helps only the
no-argument path, `usajobs_auth()` with `rows=None`, since `secret_matrix` takes an unhashable
settings object), and `lenient_state` (about 50-60 ms; cache it keyed on the mtimes of
`preferences.yaml`, the resume file, and the resume folder, so a file added to or removed from
the folder invalidates it). With those warm, `run_checks` must finish in under 50 ms on
your real data; a timing test uses a realistic-size `preferences.yaml`. `cheap_only=True` (the banner) runs the essentials plus
the recommended rows marked cheap: everything in the table except the alert-subscription counts
and the backup listing.

### Scorer credentials

One new function in `scoring/scorers.py`, `credential_for(spec, scoring) -> Credential`, used by
the checks and by the run's score stage (S7), so the two cannot disagree. Its answer is
`present`, `missing` or `unknown` plus the variable names involved:

| Provider | Present when | Notes |
|---|---|---|
| `anthropic:` (and bare names) | `ANTHROPIC_API_KEY` or `ANTHROPIC_AUTH_TOKEN` set; or `ANTHROPIC_PROFILE` or `ANTHROPIC_CONFIG_DIR` set; or the workload-identity variables (`ANTHROPIC_IDENTITY_TOKEN[_FILE]` with the federation ids) set; or `<config_dir>/active_config` or `<config_dir>/configs/default.json` exists, with `config_dir` honouring `ANTHROPIC_CONFIG_DIR` (stats only, never read). A test for each alone | What `anthropic` 1.13 resolves, in its order. Anything only the SDK can judge is `present` with the note "via SDK profile, not checked locally". `missing` only when none exists. `ANTHROPIC_AUTH_TOKEN` is not a `*_FILE` name today (S1 adds it to `secrets.DEFAULT_NAMES`) |
| `openrouter:`, `jev:`, `decisions:` | the variable named by `scoring.openrouter.api_key_env` | `missing` otherwise |
| `openai-compat:` | the variable named by `scoring.openai_compat.api_key_env`, or that name set empty | Server reachability is a live check |
| `local:` | always `present` for the key (optional); the server is a live check | `unknown` until checked live |

S7 skips the score stage only on `missing`, with `score: skipped: no Anthropic credential for
anthropic:claude-haiku-5-5 (see jobhunter doctor)`; `present` and `unknown` try as today.

### Gmail token

Local checks never read the token. The row is built from:

1. `<data dir>/gmail-auth.json` (S5, 0600): `issued_at` (UTC) and the storage kind (`keyring`,
   `file`, or `podman secret <name>`). Never the token.
2. A `stat` of the token file: `~/.config/jobhunter/gmail_token.json` on the host, or
   `$JOBHUNTER_GMAIL_TOKEN_FILE` in a container.
3. The mail source's latest status in the database, mapped per rule 4.

Storage `keyring` is reported from `gmail-auth.json` without opening the keyring. Without
`gmail-auth.json` (a token from before S5, or keyring storage before S5) the row is `unknown`
with "use Test (free), or run `jobhunter mail status`", unless the source status settles it.

Age, while the app is in Testing:

- ok, "re-auth due about <date>", days 0 to 5;
- ok with `due`, "re-auth due tomorrow", from day 6; `due` rows appear in the recommended
  banner line;
- broken after day 7, or when the latest mail source status is a `RefreshError`;
- a successful mail source run **newer than issued_at + 7 days** turns the age rule off until
  the next `mail auth` (the app has left Testing), and the row stays ok.

`mail auth --podman-secret NAME --from-local` moves a token without new consent: S5 keeps the
original `issued_at` from the local metadata, or records it as unknown, never "now". Fix:
`jobhunter mail auth` (host) or `jobhunter mail auth --podman-secret jobhunter_gmail_token`
(containers).

## What the nightly run sees

The console and the nightly run do not share an environment. On the host, timers run with
systemd's environment and the repository's `./.env`, not your shell. In containers, the console
and `jobhunter-ctr-run` get different drop-ins, and the console reads secrets only at start. The
console container also never sees the Gmail token, the Google client JSON or the USAJOBS
credentials: those go to the run unit and the `jobhunter-ctr` wrapper (verify also gets
USAJOBS), per [018](018-containers.md#which-container-gets-which-secret). So the run reports
what it sees:

- **One file, four sections.** `<data dir>/readiness.json` (0600) holds `run_inputs`,
  `run_outcome`, `manual_inputs` and `host`. Each has its own `checked_at`, writer and trigger.
  Every writer takes a small lock (`readiness.json.lock`, `flock`), reads the file, replaces only
  its own section, and renames a temp file into place, so no writer erases another's section.
- **Run inputs.** `jobhunter run` writes `run_inputs` **after it takes the run lock**, so a run
  that times out on the lock writes nothing: each check's id, effective status and fixed message,
  the mode, and the trigger. No values, no free-form text.
- **Run outcome.** At the end the run writes `run_outcome`: the score stage's result as a fixed
  code (`scored`, `skipped_no_credential` from S7, `cap_reached`, `failed:<exception type>`,
  `no_profile`, or `not_run` when the run's stages did not include scoring) and each failed
  source's mapped reason. Only `run` writes it. A `not_run` outcome never replaces an earlier
  score outcome: a partial manual run (`--stage resolve`) leaves the last real score result in
  place. The `run` table stores only counts, so this is how the console learns that the last run
  did not score.
- **Trigger.** Timer runs are marked explicitly with `JOBHUNTER_TRIGGER=timer` (systemd's
  `INVOCATION_ID` does not reach a container). The value goes into the snapshot and into a new
  `run.trigger` column (a migration in S4). Existing rows migrate to `NULL` (unknown), not
  `manual`, so the upgrade itself raises no alarm. Units installed before S4 lack the marker, and
  nothing copies units on upgrade, so S4 delivers it to them:
  - **Containers:** `jobhunter container preflight` writes the marker as its own drop-in,
    `<unit>.container.d/40-jobhunter-trigger.conf` with the preflight header, for the run,
    collect and rescore units, alongside the secret drop-ins. The base units in
    `deploy/quadlet/` gain the line too.
  - **Host:** `ops/units/jobhunter-run.service` (and collect) gain
    `Environment=JOBHUNTER_TRIGGER=timer`; `jobhunter schedule install` writes it, and `schedule
    status` flags an installed unit without it with "rerun `jobhunter schedule install`".
- **Manual inputs.** `jobhunter doctor --snapshot`, or `jobhunter-ctr doctor --snapshot` inside a
  one-off container (which sees every secret the wrapper gets), writes `manual_inputs`. It fills
  rows the console cannot see and is labelled "as seen from your shell at HH:MM" (or "from
  `jobhunter-ctr`"). It never stands in for the nightly run in the rule below, since on the host
  your shell's environment is not the timer's.
- **Host section.** On the host, `jobhunter doctor --containers --snapshot` writes `host`, with
  preflight's [mapped results](#jobhunter-doctor), which the console container shows on its
  Containers row with the time. Without it the row says "run `jobhunter doctor --containers` on
  the host".

**Effective status.** A section is fresh when written within 36 hours. For a row the console
cannot check, the effective status is the newer of a fresh `run_inputs` or `manual_inputs`, else
`unknown`. For a row both can check (scorer, caps, preferences, resume and the rest), it is **the
worse of the console's view and a fresh `run_inputs`**, and a disagreement is shown: "the console
sees ANTHROPIC_API_KEY; the run at 02:00 (timer) did not". A fresh `run_outcome` of
`skipped_no_credential` or `no_profile` makes the scorer or profile row broken whatever the
console sees, **until a newer `run_inputs` shows that row ok**, so fixing the cause and running
again clears it. The scorer and profile fixes therefore end with "or confirm now with `jobhunter
run --stage prefilter,score` (`jobhunter-ctr run ...` for containers). **This scores waiting jobs
and spends credits, up to the daily cap**" (the label is required; the page offers no button for
it). `essentials_ok`, the banner and dismissals all use the effective status.

**Timers not running** is judged from the newest `run` row with `trigger = 'timer'` (none in the
last 36 hours), so a manual run does not hide dead timers, **provided the installed run unit
carries the marker**. The check reads the installed unit (a small file read: the host
`~/.config/systemd/user/jobhunter-run.service`, or the run unit's trigger drop-in in
host-managing mode). When the marker is missing there, or the console cannot see the unit
(container mode) and no timer-triggered row exists yet, it falls back to the newest run of any
trigger and says "timer runs are not marked yet: rerun `jobhunter schedule install` (host) or
`jobhunter container preflight` (containers)", rather than reporting dead timers. One exception: a unit that carries the marker but has no
timer-triggered row at all (for example linger off from day one) is reported as "timers not
running" once the newer of the unit file's mtime and the run timer's enable link (`*.wants`
`lstat` mtime) is over 36 hours old. Wording that
says "nightly" is used only for timer-triggered data; manual runs are labelled "manual run at
HH:MM".

## `/setup`

Nav entry **Setup**, after Prefs. Groups in the order a fresh install meets them:

| Group | Rows (inventory #) |
|---|---|
| Basics | Contact for the User-Agent (4), data permissions (25). Rows 1-3 are doctor-only |
| Profile | Preferences (5), resume (6), profile completeness (9) |
| Scoring | Screen scorer (7), spend caps (8), deep pass (17) |
| Sources | USAJOBS (10), scheduled runs (22), last run and its outcome |
| Email alerts | The [numbered guide](#email-alerts-step-by-step) (11-16) |
| Apply | Drafting runner (18), packet cap (19), extension (20), PDF export (21) |
| Running | Secrets and `.env` (24), backups (23), console exposure (27), containers (26) |

Each row: status word with an icon (never colour alone), the title, one line on what it unlocks,
where the value came from, and the fix. Rows that are ok collapse to one line; a group whose rows
are all ok collapses to its header. "Show everything" expands all.

```
Scoring
  ✕ missing   Screen scorer                       Fit scores and buckets for new jobs
              screen_scorer is not set in config.toml, so the default
              anthropic:claude-haiku-5-5 is used, and no Anthropic credential is set.
              New jobs are not being scored (last run 02:00: skipped, no credential).
              Fix, either:
                · add ANTHROPIC_API_KEY to ~/.env, then restart the console
                · or pick another scorer in config.toml, e.g.  [scoring] screen_scorer = "jev:typesafe/jev-1.13"
              Read when the console started at 09:12.

  ✓ ok        Spend caps                          $2.00/day, $10.00/week; $0.41 spent today
```

### Writes from the page

`/setup` adds four POST routes and one GET page, and reuses one route. All go through the existing middleware (loopback
`Host` and same-origin `Origin` on writes, [017](017-assisted-apply.md#local-api-security), C1).
None takes a secret.

| Route | Does | Notes |
|---|---|---|
| `POST /setup/starter-prefs` | Writes `SKELETON` (`prefs_graceful.py`) through `write_preferences_data` (atomic, 0600), then 303 to `/prefs?created=1` | Refuses with 409 if the file exists. Not `/prefs/save`: that route builds the file from form fields, and a bare POST gets weights of 0 (422) and loses the skeleton's exclusions. Test: the written YAML loads to the same data as `SKELETON` |
| `POST /setup/resume` | The `/prefs` upload's checks (`check_upload`: `.md`/`.txt`, 1 MB, UTF-8) and write helper, writing a path relative to the profile folder; answers with the refreshed resume row (htmx) or 303 to `/setup` | Not `/prefs/resume`, which renders the whole prefs page |
| `POST /setup/dismiss` | Body: one or more `check_id`, each validated against the fixed id list, recommended or due rows only; writes `<data dir>/setup-dismissed.json` atomically at 0600 (id plus the effective status dismissed at) | 422 for an essential or unknown id |
| `POST /setup/live/{check_id}` | One [live check](#live-checks); `check_id` from the fixed list | Requires the **peer address** (`request.client.host`) to be loopback, since `Host` is client-supplied. Under `--allow-remote` it always answers 403. The page shows the buttons only when the request's own peer is loopback; otherwise each row says "run `jobhunter doctor --live`" (or `jobhunter-ctr doctor --live`). This covers the 018 D11 fallback, where pasta makes the host browser's peer non-loopback. Route tests pass `client=("127.0.0.1", ...)` to `TestClient` |
| `GET /setup/gmail` (S11e) | 303 to `/help/email-alerts#step-1-google-client-json`, the one Gmail how-to ([Addendum A](#addendum-a-help-by-topic)), which carries `mail/auth.py` `CLOUD_STEPS` as a generated block | Added by S11e, once the help page exists, together with pointing `/alerts`' `SETUP_DOC` (today a dead relative link to `specs/012`) at it. Before that, the `/setup` row shows the steps, the mode's target path and the command inline (S3a) |
| `POST /prefs/ext/pair` (reused) | The one-time pairing code panel; it already answers with the `_ext_pair.html` fragment | |

Everything else is a link (`/prefs#sec-pay` and the other sections, `/alerts`, `/sources`) or
the exact command with a copy button and a selectable `<code>` fallback. S3a links no `/help`
page; S11e adds the help links once the pages exist.

**No secret is ever typed into the console.** Why: the console has no authentication, a form
puts the key in a request body and browser history, and in containers secrets belong to podman.
The page shows where each key goes, per mode:

- **host**: create the file private first, then edit it:
  `[ -e ~/.env ] || install -m 600 /dev/null ~/.env`, then add `ANTHROPIC_API_KEY=...` with an
  editor, then restart the console.
- **container / host managing containers**: the form below, shown inline (S11e adds a link to
  `/help/containers#secrets`), which keeps the
  key off the command line and out of history, then the full [018](018-containers.md#mechanism)
  sequence. When the row is broken or the secret already exists, the line uses
  `podman secret create --replace` (018), since plain `create` refuses a name in use:

  ```
  read -rs KEY && printf '%s' "$KEY" | podman secret create jobhunter_anthropic - && unset KEY
  jobhunter container preflight
  systemctl --user daemon-reload
  systemctl --user restart jobhunter-console
  ```

**`config.toml` is not edited from the page.** For the contact and the alerts address the page
shows the file path (the host path in container mode) and the exact lines to add, plus the
restart note. Why: it is read once at process start, is a read-only mount in containers, and
stays a file by decision.

### Email alerts, step by step

The group is a numbered guide. A step whose predecessor is missing says "waiting on step N"
instead of its own fix, because `mail setup` refuses the placeholder address and `mail auth`
needs the client JSON.

| Step | Row | Done when (local) | Fix |
|---|---|---|---|
| 1 | Google client JSON (12) | the file exists (container: the snapshot says so) | The ten-minute Cloud console procedure, shown inline from `CLOUD_STEPS` (S11e adds the link to `/help/email-alerts`), with the file's target path for the mode (`podman secret create jobhunter_google_client <file>` for containers). `help/email-alerts.md` (S11c) is the repository copy |
| 2 | Alerts address (11) | not the placeholder | The `[mail] alerts_address` line, your `+jobs` address, and the restart note |
| 3 | Gmail token (13) | [Gmail token](#gmail-token) rules | `jobhunter mail auth`, or `--podman-secret jobhunter_gmail_token` |
| 4 | Label and filter (14) | an alert arrived under the label in the last 14 days (`mail_message`); otherwise `unknown` until the live check | `jobhunter mail setup` (or `jobhunter-ctr mail setup`); it prints filter XML for manual import if you skip the settings scope |
| 5 | Board subscriptions (15) | `/alerts` counts: subscribed, receiving, stale | Link to `/alerts`, the existing checklist, which gives each board's sign-up link, criteria and the address to use |
| 6 | LinkedIn job alerts (16) | alerts from LinkedIn's job-alert sender arrived under the label | Create alerts in LinkedIn (Jobs, search, "Set alert"). Do **not** make the `+jobs` address your LinkedIn primary email: every LinkedIn notification would follow it. Route only the alerts: S9 adds `mail.fallback_senders`, a list of **exact sender addresses** (LinkedIn's job-alert sender, read from the From line of a real alert in your inbox before you route it, and before S9 ships it as a suggested value; `mail sample` cannot see an alert that is not yet under the label), which `mail setup` turns into `from:<address>` filters. Never put `linkedin.com` in `fallback_sender_domains`: that becomes `from:@linkedin.com`, which takes InMail, recruiter and connection messages out of your inbox and stores their subjects as alerts. `/setup` and `/alerts` warn when a domain entry matches a site that also sends personal mail |
| 7 | Craigslist saved searches (16), optional | as 6 | A Craigslist account and a saved search with email alerts, routed by the exact alert sender in `mail.fallback_senders`, not the domain. Low priority |

Steps 6 and 7 also become rows on `/alerts` (S9), since they are not registry sources and the
checklist does not list them today.

**Alerts that arrive before their parser.** LinkedIn (bug `430a0e7`) and Craigslist (bug `b0ac328`)
have no parser yet. Their messages stay in Gmail under the label; jobhunter stores only metadata
(`mail_message`), not bodies, and a message with no parser yields no jobs. Each parser ticket
therefore includes a one-time **re-read**: when the parser lands, it fetches that sender's messages
still under the label (by the Gmail ids recorded in `mail_message`) and ingests them once, so
alerts received while it was pending are not lost. Until then the row says "N LinkedIn alerts
received; they will be read when the parser lands (`430a0e7`)". The PM adds this re-read to both
bugs when S9 is filed. jobhunter never fetches linkedin.com or craigslist.org
([008](008-compliance.md)); the alert email is the only channel.

### Live checks

Each is a button on its row, labelled "Test (free)", and part of `doctor --live`. All are free:
none calls a model. Each runs only on its own click. Results are **booleans mapped to fixed
text**, shown on the row with the time and never stored or logged: no email address, no masked
key, no account or organization, no raw response.

| Row | Live check | Result fields | Network |
|---|---|---|---|
| Anthropic credential | `models.list` through the `anthropic` SDK | accepted | api.anthropic.com |
| OpenRouter or Jev key | `GET <scoring.openrouter.base_url>/key` | accepted; credit remaining | openrouter.ai only: when `base_url`'s host is not `openrouter.ai`, "no free live check for this endpoint" and no call. The response's `label` (a masked key) and the raw body are never rendered or logged |
| Local model | `/health` on the configured loopback URL (the existing `llm status` probe) | reachable; model loaded | loopback |
| Gmail | refresh the access token; get the label; list filters | token accepted; label present; filter for your alerts address present; N of M fallback sender filters present | Google |
| Drafting runner (`cli`) | `claude auth status --json`, uncached (a new `auth_status()` helper, not `check_auth`) | logged in; claude.ai login (not an API key) | may contact claude.ai |

The Gmail check does not reuse `mail status` or `mailbox_status` output, whose filter keys are
`to:<your address>`; it compares criteria internally and returns only the booleans. The runner
check never adds to `cli_runner._AUTH_OK` and never calls `runner_state.turn_off`: a failed check
is a status, not a turn-off. In container mode the console cannot see the Gmail token, so the
Gmail button is replaced by `jobhunter-ctr mail status`. In host-managing mode, `doctor --live`
does not test the host shell's keys (the containers use podman secrets): for every secret-backed
check it prints `jobhunter-ctr doctor --live` instead, which runs the same checks with the
wrapper's secrets.

**No live check may spend credits.** A future one that would must be labelled "Test (may spend
credits, about $X)", must confirm, and is never part of `doctor --live`.

## Banner

On `/` and `/inbox`, above the page content. Each request runs `run_checks(cheap_only=True)`
and reads the snapshot and the dismissals file.

```
  Setup: 2 essentials missing, so new jobs are not being scored.   [ Open setup ]
  Screen scorer · Resume
```

- **Essentials line**: shown while any essential's effective status is missing or broken. Not
  dismissible. Names the items and the main consequence in one line.
- **Recommended line**: shown on its own when no essential is failing: "3 recommended items:
  USAJOBS key, Gmail token due tomorrow, scheduled runs. [Open setup] [Hide]". It lists
  recommended rows that are missing, broken or `due`. **Hide** posts the listed ids to
  `/setup/dismiss`. A dismissed row comes back when its effective status changes (ok to broken, or
  unknown to broken once a snapshot arrives). Optional, info and unknown rows never appear.
- The inbox's existing "No profile yet" banner becomes this banner (one banner, not two).
- **First run** (your decision): while an essential is missing or broken **and** the database
  holds no jobs, `GET /` answers 303 to `/setup`. Once any job exists, only the banner remains.
  Existing `GET /` tests on an empty database seed a job or a complete setup.
- `jobhunter init` ends with "next: open http://127.0.0.1:8808/setup, or run `jobhunter
  doctor`". `jobhunter console` prints one stderr line at start when essentials are failing.

## `jobhunter doctor`

```
$ jobhunter doctor
Basics
  ok       config file          ~/.config/jobhunter/config.toml
  BROKEN   User-Agent contact   still the placeholder; add [fetch] user_agent_contact to config.toml
Profile
  ok       preferences          ~/.local/share/jobhunter/profile/preferences.yaml
  MISSING  resume               no .md file in ~/.local/share/jobhunter/resume
Sources
  BROKEN   last run             02:00 exited 1: USAJOBS credentials missing
...
2 essentials failing, 4 recommended, 1 unknown. Details: jobhunter doctor -v
```

| Option | Effect |
|---|---|
| (none) | Every local check, plus rows 1-3 (including `legacy_data.check`); essentials and anything not ok are listed |
| `-v` | Every row, with what it unlocks and where the value came from |
| `--json` | The `Check` list as JSON, same rules |
| `--group NAME` | One group |
| `--live` | Also the free [live checks](#live-checks); prints what it will contact first |
| `--containers` | Also `container preflight` in dry-run mode, below |
| `--snapshot` | Write `readiness.json` (S4) |

Exit codes: **0** every essential's effective status ok; **1** an essential missing or broken
(with `--live`, also an essential's live check failing; with `--containers` and the marker
present, any preflight FAIL); **2** usage error. Recommended and unknown rows never change it.

`doctor` joins `UNGUARDED_COMMANDS` and `MIGRATION_EXEMPT_COMMANDS`: it must run when the layout
is old, the schema pending or newer, or the config broken, because it reports those. It opens the
database read-only and never migrates, writes, or backs up, except `readiness.json` with
`--snapshot`.

**`--containers`** is explicit because preflight is not cheap or side-effect-free even in dry
run: it probes podman (possibly through `flatpak-spawn`), starts one container
(`podman run --rm --network=none localhost/jobhunter:current version --json`, up to 120 s), and
runs `quadlet -dryrun` on copies of the units in a temporary directory. It never writes drop-ins
or the marker in dry run; `container preflight` itself still does. S2 extracts a
`build_preflight_context()` helper from `container_preflight_cmd`. Preflight's lines contain
podman and quadlet stderr, so they print in the terminal only; `--json` and the snapshot carry
fixed ids with ok/warn/fail and fixed messages. S2 adds an `id` argument to preflight's
`Result.add` and sets it at every call: `podman`, `bind_sources`, `owner`, `labels`,
`host_timers`, `db_paths`, `version_skew`, `env_files`, `mounted_config`, `secrets`,
`dropin_conflict`, `base_units`, `quadlet`, `marker`. A line without an id maps to a catch-all
`preflight_other` that keeps its severity, never to ok. With the marker
present, a FAIL is **essential** (the units will not start) and a WARN recommended; without it,
both are recommended. Without `--containers`, host-managing mode shows the Containers row as
`unknown` unless a fresh host section exists.

## Console rules

- `/setup` and its routes sit behind the existing middleware: loopback `Host` on every request
  and same-origin `Origin` on writes unless `--allow-remote`. Under `--allow-remote` a LAN
  client can read `/setup` (no values, but which keys are set) and post the three non-live
  writes, exactly as it can already edit `/prefs`. The live-check route refuses every request
  under `--allow-remote` (403) and otherwise checks the peer address, since it uses your
  credentials. `Cache-Control: no-store` like every page.
- Light and dark through the existing `app.css` tokens; checked at 375 px wide, where each row
  stacks (status and title, then detail, then the fix) with no horizontal scroll.
- A test requests every internal link `/setup` renders (S11e extends it to the `/help/...` links) and expects
  200; external links (Google Cloud console, LinkedIn) are not fetched.

## Testing

- No network, no subprocess, no keyring: `run_checks`, `GET /setup` and the banner path run with
  sockets guarded (conftest), `subprocess.Popen` patched to raise, and `keyring.get_password`
  raising (autouse). Live checks take injected clients (`httpx.MockTransport`, a mocked
  `anthropic` client and Gmail service, a fake `claude` on `PATH` as `cli_runner`'s tests do).
- A table-driven test per inventory row: build the failing state in a temp home (no credential,
  the placeholder, a 0644 `.env`, invalid YAML, two `.md` resumes, an absolute YAML
  `resume_path` in container mode, a pending schema with the marker, a `RefreshError` source
  status, an 8-day-old `gmail-auth.json`, host timers with the marker) and assert status, level,
  mode-specific fix text and the exit code.
- Anthropic credential: `ANTHROPIC_AUTH_TOKEN` alone and `ANTHROPIC_PROFILE` alone are
  `present`; S7 does not skip on them.
- **No value leaks**: set every secret, contact field and `mail.alerts_address` to a sentinel;
  give the mocked Gmail service filters on that address, the mocked OpenRouter `/key` a `label`
  holding the sentinel, and the fake `claude auth status` an email and org. Assert the sentinel
  never appears in the page, live-check fragments, `doctor` (plain, `--json`, `--live`),
  `readiness.json` or captured logs.
- Snapshot: a console that sees a key while the snapshot's outcome is `skipped_no_credential`
  shows the scorer broken and the banner; a stale snapshot leaves console-only rows as they are
  and container-only rows unknown; a lock timeout writes no snapshot.
- The motivating case end to end: default config, no Anthropic credential, valid profile,
  no jobs: `doctor` exits 1 naming the screen scorer; `GET /` redirects to `/setup`; `/inbox`
  shows the banner; the run summary carries the S7 line and the run exits 0. With one job seeded,
  `/` shows the banner instead of redirecting.
- Snapshot writers: the run's write keeps `host` and `manual_inputs`; a manual
  `doctor --snapshot` never changes `run_inputs`; after the fix and a newer `run_inputs`, the
  scorer row clears. A manual run does not reset "timers not running". An installed unit
  without the trigger marker, or migrated `NULL` rows only, gives the fallback hint, never
  "timers not running". A `--stage resolve` run writes `not_run` and keeps the earlier score
  outcome.
- `/setup/live/*` with `Host: 127.0.0.1` from a non-loopback peer is 403; under
  `--allow-remote` every request is 403.
- Banner: Hide dismisses a recommended row; a status change brings it back; essentials have no
  Hide and `/setup/dismiss` refuses them.
- Browser check before merge, against a copy of your database (CLAUDE.md): light, dark, 375 px.

## Addendum A: help by topic

Bug `e2f8c10`. You asked for in-app help, organised by topic, that tells you what to do to get
jobhunter running. It has to work in containers and must not drift from the README.

### Where the help lives (single source)

**Every help page is a plain Markdown file in the package: `src/jobhunter/console/help/<topic>.md`**
(your decision, 2026-10-11). The console renders those files. `README.md` and `CLAUDE.md` both
link to them, so you, GitHub and the agents all read the same text. Nothing is written twice:

- **Why the package.** The image copies only `src/` (`Containerfile`: `COPY src/ src/`; 018 keeps
  `docs/` and `specs/` out of it), so a page under `docs/` cannot be served in a container. The
  `static/` and `templates/` folders next to `console/app.py` already ship this way.
- **README.** The user-facing sections move into help files (your decision): "Running nightly"
  goes to `nightly-runs.md`, "Browser extension" to `browser-extension.md`, "Running in
  containers" to `containers.md`, and "Choosing a scorer" (`README.md:65`, merged with
  `75dacda`) to `scorers.md`. The README keeps Layout, Setup (developer install), the
  old-layout migration and Work tracking for now; moving Layout and the migration into help is
  a later choice. It gains a **Help** table that links each topic file
  and says the same pages are at `http://127.0.0.1:8808/help` in the console.
- **CLAUDE.md** gains a short "User help" line that points at `src/jobhunter/console/help/`.
  It says to read the help topic for the area being changed, and to update it in the same
  change when behaviour that it describes changes (S11a).
- **`docs/containers.md`** moves to `help/containers.md` with `git mv` (your decision).
  `docs/containers.md` stays as a three-line pointer, so links in 002, 018, 019 and
  `specs/README.md` keep working. `tests/test_docs_containers.py` moves its checks to every
  help file (below).
- **One Gmail how-to.** `help/email-alerts.md` is the only Gmail guide. `docs/gmail.md` (S8)
  is not written. S11e makes `GET /setup/gmail` answer 303 to
  `/help/email-alerts#step-1-google-client-json` and points `/alerts`' `SETUP_DOC` at the help
  page. The `/setup` row itself (S3a) shows the steps, the mode-specific target path and the
  command inline.
- **Text built from code is a generated block, checked by a test.** Some content must match
  the code exactly: the Google Cloud steps (`mail/auth.py` `CLOUD_STEPS`), the scorer provider
  table (`config.SCORER_PROVIDERS`), the environment-variable table, and which settings reload
  without a restart ([Addendum B](#addendum-b-config-watch)). Each sits between a
  `<!-- jh:begin NAME -->` line and a `<!-- jh:end NAME -->` line, and is committed, so GitHub
  shows it too. `jobhunter.console.help.regenerate()` rewrites the blocks and always writes a
  blank line before and after each marker, so a table ends before the end marker on GitHub and
  in the console. A test fails, naming the command to run, when a committed block differs from
  what the code generates now. Nothing is filled in at request time, so the page and the file
  are the same text.

### Rendering

`markdown-it-py`, built as `MarkdownIt("commonmark", {"html": False}).enable("table")`. Pass
`html` explicitly, because the `commonmark` preset defaults to `html: True`. It is already
installed (`rich`, through `typer`, depends on it; `uv.lock` has 4.2.0), and S11a adds it to
`pyproject.toml` as a direct dependency with no new download.

- **Markers.** With HTML off, markdown-it **escapes** HTML, it does not drop it: a marker line
  renders as visible `&lt;!-- jh:begin ... --&gt;`, and one directly under a table becomes an
  extra row (checked against 4.2.0). So the renderer deletes whole lines matching
  `^<!-- jh:(begin|end) [a-z0-9-]+ -->$` **before** parsing. Any other stray HTML is shown escaped,
  and the drift test forbids it.
- **Links.** `validateLink` is never overridden: it is what refuses `javascript:`, `vbscript:`
  and `file:` links. Link rewriting:
  - a relative link to another topic (`email-alerts.md#step-3-gmail-token`) becomes
    `/help/email-alerts#step-3-gmail-token`;
  - a relative link to any other repository file (`../../../../specs/018-containers.md`) becomes
    `https://github.com/DanielJoyce/jobber/blob/main/<path>`, because `specs/`, `deploy/` and
    `extension/` are not in the image;
  - console routes (`/setup`, `/prefs#sec-pay`) and absolute URLs are left alone. On GitHub a
    console route is a dead link, so each one sits in a sentence that says "in the console".
- **Heading ids.** One function, `help.slug()`, used by the renderer, the drift tests and S11e,
  does what GitHub does (github-slugger): lowercase; drop punctuation except `-` and `_`; each
  space to `-`; `-1`, `-2` on repeats within a page. `topic.md#anchor` then works on GitHub and as
  `/help/topic#anchor`.
- Rendered HTML is cached per file mtime, as `static_url` does for CSS (`app.py:52`).

### Routes and reachability

| Route | Serves |
|---|---|
| `GET /help` | The index: each topic's title, one line, and its section headings as links. "Start here" points at Getting started |
| `GET /help/{topic}` | One topic. `topic` must be in the fixed list (404 otherwise, never a file path built from input). A left column lists all topics; on a phone it is a `<details>` above the page |

- **Nav.** A **Help** entry, last in `PAGES` (`app.py:74`).
- **From `/setup`.** Every row's fix ends with a "Help" link to its topic and section
  (`Fix.help`, e.g. `email-alerts#step-3-gmail-token`). A test walks the fixed check-id list and
  requires a help link on each, pointing at a heading that exists.
- **From the first-run redirect.** `/` redirects to `/setup` on first run, so the page heading
  carries "New here? Read [Getting started](/help/getting-started)". `jobhunter init`'s closing
  line and `console`'s start-up stderr line name `/help/getting-started` too. `doctor -v` prints
  each row's help topic.
- **Works before anything is set up.** The help routes take no `Conn` and load no profile,
  settings values or secrets. They render with no `preferences.yaml`, no resume, and while the
  database answers 503 (`app.py:251`): a test covers each case.

Rules:

- **No personal data.** The pages are static files from the repository. The only runtime
  context is the mode (host, container, host managing containers), which puts the matching
  section first and labels it "you are running: container". Your settings are never
  interpolated. The alerts address is always written `<you>+jobs@gmail.com`, and the contact
  `<you>@example.com`.
  - One test renders every page with `user_agent_contact`, `alerts_address` and every secret
    set to a sentinel, and asserts the sentinel never appears.
  - The email-address check from `test_docs_containers.py` is widened to every help file. It
    allows `example.com` addresses, `<you>`-prefixed placeholders (the same rule as
    `.gitleaks.toml`), and a commented allowlist of public no-reply senders (LinkedIn's
    job-alert sender). Anything else fails.
- **Drift tests over every help file** (generalised from `test_docs_containers.py`):
  - every `jobhunter ...` or `jobhunter-ctr ...` line in a fenced block names a real subcommand
    and real options;
  - every `[section] key` written in backticks is a real `Settings` field;
  - every `/help/...` link and its `#anchor` match a topic and a `slug()` heading;
  - every console-route link answers 200 from `TestClient`, and its `#anchor` is an `id` in that
    response;
  - every rewritten repository link names a file that exists, and any other relative link
    fails;
  - host unit names are checked against `src/jobhunter/ops/units/` and container unit names
    against `deploy/quadlet/`;
  - generated blocks are current;
  - the rendered HTML of every topic contains neither `jh:` nor `&lt;!--`.
- Light and dark through the `app.css` tokens. At 375 px wide there is no page-level
  horizontal scroll: wide tables and code blocks scroll inside their own box.
- **No search.** Why: there are about a dozen pages, the index lists every section heading, and
  the browser's find-in-page covers a single page. Search would need an index and a route for
  little gain. It can come later if the pages grow.

### Topics

Each row is one file. The "must cover" column is what the page must explain, grounded in the code named.
Commands and setting names are shown, never values.

| Topic (file) | Must cover |
|---|---|
| Getting started (`getting-started.md`) | The order a fresh install meets things, mirroring `/setup`'s groups: contact for the User-Agent, preferences and resume, a scorer and its key, the first run, scheduled runs, then the optional parts (email alerts, USAJOBS, extension, drafting). What `/setup` and `jobhunter doctor` are. Host and container start commands |
| Profile and resume (`profile-and-resume.md`) | `preferences.yaml` as the source of truth ([014](014-preferences-console.md)), the `/prefs` form and the starter file from `/setup`; one `.md` resume, upload on `/setup` or `/prefs`; `resume_path` relative to the profile folder (the container rule above); which edits are free and which re-score (014) |
| Choosing a scorer (`scorers.md`) | README's "Choosing a scorer", moved; the generated provider table (`<provider>:<model>`, endpoint, key variable); no bare names; `deep_scorer` must be `anthropic:`; **`jev:<slug>`, never `openrouter:jev:...`**; a malformed scorer skips scoring and does not stop fetch or mail; local models (016); `llm bench`. **The Anthropic key:** create one in the Anthropic Console; API credits are prepaid and separate from a claude.ai subscription; keys start `sk-ant-` (019's format note); `ANTHROPIC_AUTH_TOKEN` as the alternative; host `~/.env` versus podman secret `jobhunter_anthropic` (`ops/preflight.py:89`) |
| OpenRouter and Jev (`openrouter.md`) | Creating an account and an API key, buying credits; `OPENROUTER_API_KEY` (or the name in `[scoring.openrouter] api_key_env`); the default `provider` settings `data_collection = "deny"` and `require_parameters = true`, and why (your resume goes in every request); `jev:<slug>` and `decisions:<slug>` use the decisions endpoint with the same key, `openrouter:<slug>` the chat endpoint; how cost is shown (from the response when OpenRouter reports it, else the cached model catalog; `est_cost_per_request_usd` for the cap estimate); the free key check on `/setup` (S6) |
| Costs and credits (`costs-and-credits.md`) | `[scoring] daily_cap_usd` / `weekly_cap_usd` and what happens at the cap; `[apply] daily_cap_usd` (separate, 0 refuses paid drafting); `/costs`; which actions spend (the nightly score, a confirmed re-score, Score now, Generate with the `api` runner, `score --deep`) and which never do (`/setup`, `doctor`, live checks, help); the `cli` runner uses your claude.ai subscription, not API credits |
| Email alerts (`email-alerts.md`) | Gmail end to end, as the seven [steps](#email-alerts-step-by-step), each step a heading so `/setup` can link it: a Google Cloud project and a Desktop OAuth client (the generated `CLOUD_STEPS` block), where the client JSON goes per mode (`mail.client_secrets_path`, `$JOBHUNTER_GOOGLE_CLIENT_SECRETS`, podman secret `jobhunter_google_client`); `[mail] alerts_address`; `jobhunter mail auth`, and for containers `--podman-secret jobhunter_gmail_token` and `--from-local`; re-auth every 7 days while the app is in Testing; `jobhunter mail setup` (label and filter, or filter XML to import); `mail.fallback_senders` for LinkedIn (its job-alert sender, normally the `jobalerts-noreply` address at `linkedin.com`: confirm it on a real alert's From line) and never a `linkedin.com` domain entry; how to tell it works (`jobhunter mail status`, alerts arriving under the label, the `/setup` rows, `/alerts`) |
| Browser extension (`browser-extension.md`) | The README section as it stands: load unpacked, pairing, options page, `public_port` in containers, `ext status` / `ext unpair` |
| Drafting (`drafting.md`) | The `cli` runner: install Claude Code, run `claude` and log in with your claude.ai subscription. Any other login (an API key, Bedrock, Vertex) fails `claude auth status`'s first-party claude.ai check and turns the runner off (`cli_runner.py:329`). `CLAUDE_CONFIG_DIR`; `CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token` goes in `~/.env` (it has no `*_FILE` form); both are passed through by `cli_runner.py:63`. The `api` runner (`ANTHROPIC_API_KEY`). Turning the runner back on. In containers drafting runs on the host (`apply draft`, 018 C9) |
| Settings and secrets (`settings-and-secrets.md`) | `config.toml`: where it is (`jobhunter paths`), that it is never edited from the console. The generated **environment-variable table** (below). Host: `.env` lookup order from `config.env_files` (`$JOBHUNTER_ENV_FILE`, then `./.env` in the console's working directory, then `~/.env`; real environment variables win; an earlier file wins); create it private (`install -m 600 /dev/null ~/.env`); one `NAME=value` per line (a bare value is not read as a key); never commit it (gitignored, and the gitleaks hook). Containers: podman secrets mounted as `*_FILE`, `jobhunter container preflight`, `--replace`. How to check where a value came from without printing it: `jobhunter secrets status`, `jobhunter paths`, `jobhunter doctor`. The USAJOBS key and email. The generated table of what reloads and what needs a restart (B) |
| Nightly runs (`nightly-runs.md`) | The README "Running nightly" section, plus the containers' Quadlet timers (`jobhunter-ctr-*`), how to read the logs, `Persistent=true`, the trigger marker (S4), and why host timers and container timers must not both be on |
| Containers (`containers.md`) | `docs/containers.md`, moved; repository links rewritten per the rule above |
| Troubleshooting (`troubleshooting.md`) | Common errors, each with what it means and the fix: unknown key in `config.toml`; "your data is still in ..." (`migrate-paths`); `MigrationsPending` / `SchemaTooNew` (`upgrade`, version skew); `score: submit failed (Could not resolve authentication method)`; a malformed scorer; `spend cap reached`; `UsajobsAuthMissing`; Gmail `RefreshError` (re-auth); `Google client secrets file missing`; `PdfUnavailable`; the drafting runner's "claude is not installed" and "not a claude.ai subscription login" (runner turned off); 403 "host ... is not a loopback address" (the URL, `--allow-remote`); the extension's 401 (pair again); the console's 503. The entries for the config-watch notice and errors ([B](#addendum-b-config-watch)) belong to S12a, and the S7 skip line to S7: whichever of the two tickets and S11c lands second adds the entry. Every entry links `/setup` and `jobhunter doctor` |

**The environment-variable table** is generated from the code. It lists:

- the secrets in `secrets.DEFAULT_NAMES` plus `ANTHROPIC_AUTH_TOKEN` (S1), each with its `*_FILE`
  form;
- the variables named by `api_key_env` in each scorer section, and the registry's USAJOBS
  `key_env` and `email_env`;
- `config.PATH_ENV_VARS`, `JOBHUNTER_CONFIG` and `JOBHUNTER_ENV_FILE`;
- `JOBHUNTER_GOOGLE_CLIENT_SECRETS` and `JOBHUNTER_GMAIL_TOKEN_FILE` (`mail/auth.py`);
- `XDG_CONFIG_HOME`, `XDG_DATA_HOME` and `XDG_CACHE_HOME` (`xdg.py`);
- the Claude Code variables `cli_runner` passes through (`CLAUDE_CONFIG_DIR`,
  `CLAUDE_CODE_OAUTH_TOKEN`, a secret with no `*_FILE` form), the `CLAUDE_CODE_DISABLE_*`
  variables it forces on the child (`cli_runner.py:82`-`88`, "set by jobhunter, do not set"),
  and `ANTHROPIC_BASE_URL`, `CLAUDE_CODE_USE_BEDROCK` and `CLAUDE_CODE_USE_VERTEX` marked
  "never passed to the CLI child";
- marked "set by the units, do not set by hand": `JOBHUNTER_CONTAINER`,
  `JOBHUNTER_PUBLISHED_LOOPBACK_ONLY`, `JOBHUNTER_BUILD_COMMIT`, `JOBHUNTER_TRIGGER` (S4) and
  `JOBHUNTER_RESTART_EXIT` (S12b).

Columns: name, what reads it, secret or not, host place, container place, and whether a change
needs a restart. A test finds every string literal in `src/` shaped like
`(JOBHUNTER|ANTHROPIC|USAJOBS|OPENROUTER|OPENAI_COMPAT|LLAMA|CLAUDE|XDG)_[A-Z0-9_]+`. Each one
must be in the table or on a short, commented list of names that are not environment variables
(`ANTHROPIC_HAIKU`, `USAJOBS_MAX_DAYS`, ...). A new variable with one of these prefixes cannot
ship undocumented.

## Addendum B: config watch

Bug `ccb3b21`. You asked that the console notice when `config.toml` changes, offer to apply the
change, apply in place where that is safe, restart where it is not, and say why when the new
file is wrong. Today rule 6 tells you to restart by hand.

### What the console captures at start (checked in code)

`jobhunter console` loads settings once (`cli.py:1121`) and passes them to `create_app`.
Most routes read `request.app.state.settings` on every request (`app.py:258`; e.g.
`alerts_routes.py:35`, `packet_routes.py:187`, `pages_routes.py:113`, `ext_routes.py:157`),
so swapping that one attribute changes what they see. These do not:

| Captured at start | Where | Effect |
|---|---|---|
| The connection factory, from `paths.db_path` | `app.py:203` | Restart |
| The shared console lock, and the boot migration's data dir | `app.py:216`, `app.py:222` | Restart |
| The dashboard's profile loader, closed over `settings` (profile and resume paths) | `app.py:204`, `app.py:156` | Restart (paths) |
| `settings.scoring.weekly_cap_usd` in the dashboard KPIs, closed over | `app.py:354`, `app.py:400` | **S12a changes both to read `app.state.settings`**, so the weekly cap reloads |
| The extension pairing file, from `paths.data_dir` | `app.py:273` | Restart |
| The extension port, from `console.public_port` / the bind port | `app.py:272`, `cli.py:1139` | Restart |
| Bind host and port; `--host` / `--port` on the command line win over config | `cli.py:1122`, `uvicorn.run` at `cli.py:1144` | Restart, or no effect when given on the command line (below) |
| The process-wide fetch semaphore, sized by the first caller | `core/fetch/ratelimit.py:66` | `fetch.global_concurrency`: restart |
| Secrets: `.env` files loaded into `os.environ` with `override=False`, `*_FILE` read, names validated for the `api_key_env` names in config | `config.py:408`, `secrets.py:250` | Every secret, every `.env` change, and any `api_key_env` change: restart. Re-reading `.env` in place cannot work: `override=False` keeps the old value |
| A running console re-score keeps the settings object it started with | `rescore_routes.py:170` | Safe: a swap does not touch it |

### Which settings reload

`config.RELOADABLE`, a frozenset of dotted field paths. Everything else needs a restart. A
test enumerates every leaf field of `Settings` and requires each to be classified exactly once,
so a new field defaults to restart until someone looks at it.

| Reload in place | Restart |
|---|---|
| `fetch.respect_robots`, `default_rps`, `user_agent_contact`, `timeout_s`, `max_retries` (read per `FetchContext`: `core/fetch/context.py:140`, `165`, `168`, `382`, `484`) | `paths.*` (all five) |
| `scoring.screen_scorer`, `deep_scorer`, `daily_cap_usd`, `weekly_cap_usd`, `deep_shortlist`, `employer_rejection_days` | `console.host`, `port`, `public_port` |
| `scoring.openrouter.*`, `openai_compat.*`, `local.*` except `api_key_env` and `catalog_cache` (scorers are built per run) | `fetch.global_concurrency` |
| `mail.alerts_address`, `label`, `fallback_sender_domains`, `fallback_senders` (S9), `client_secrets_path` (read on use; the console only stats it) | `scoring.*.api_key_env`, `scoring.openrouter.catalog_cache` |
| `apply.*` (read per call: `packet_docs.py:255`) | Anything from the environment: `.env` files, `*_FILE`, `JOBHUNTER_*` paths, `JOBHUNTER_CONFIG` |
| `capture.disabled_hosts` (read per request: `ext_routes.py:238`) | Container mode, `--allow-remote` (command-line and unit settings, not config) |

**`console.host` and `console.port` given on the command line.** `--host` and `--port` win over
config (`cli.py:1122`), and the container unit always passes both (`Exec=console --host
127.0.0.1 --port 8808`). The console knows its own arguments. When a flag overrides a field,
a change to that field is listed as "no effect: set on the command line" and offers no Restart.
`console.public_port` has no flag and is a normal restart field.

The help page "Settings and secrets" carries this table as a generated block (A).

**Partial apply.** When a change has both kinds, Apply builds the new settings with every
restart field kept at its running value (`paths` and `console` whole, plus the nested restart
fields), swaps that in, and the notice stays as "2 settings still need a restart". A test
asserts the swapped object's restart fields equal the old ones.

### Detecting a change

- **No new timer, no inotify.** The check runs inside `GET /activity`, the nav poll merged with
  `edb9ba6` (`console/activity_routes.py`). It polls every 60 s when idle, every 3 s while work
  runs, and on `visibilitychange`, so switching back to the browser after saving in an editor
  checks at once. inotify is out: it does not see a bind mount's source being replaced, and the
  poll already exists.
- **Cheap.** At most once every 2 s per process (a lock and a monotonic timestamp, since several
  tabs poll), it `stat`s the config file (`config.config_file(None)`) and every candidate from
  `config.env_files()`, including ones that do not exist yet (a new `~/.env`). The fingerprint is
  `(exists, st_dev, st_ino, st_size, st_mtime_ns, st_ctime_ns, st_nlink)`. Unchanged: done.
  Changed: read and hash the file. Same hash, same inode, and outside container mode (a
  `touch`): adopt the new fingerprint and show nothing.
- **Baseline.** The watcher starts with **no** baseline, so the first check always loads the
  file and diffs it against the running settings: a save made between start-up and the first
  poll (perhaps hours later, for a console started at boot) is not missed. The `.env` digests
  are taken at start-up, in the CLI callback, while the files are loaded (`cli.py:187`).
- **Compare settings, not bytes.** A changed `config.toml` is loaded with `load_settings()`,
  the loader the CLI uses, and diffed field by field against the **running** settings
  (`app.state.settings`), not against the old file. A comment-only edit shows nothing.
- **`.env` files** are parsed with `dotenv_values` (which does not touch `os.environ`) and
  compared by key. Changed values are detected through a salted digest held only in memory
  (a random salt per process, never written or logged). Any `.env` change is restart-class.
  Key names are shown only when they look like a name (rules [below](#the-notice)).
- **Containers.** The single-file read-only mount pins the inode
  ([018](018-containers.md) D3). Three cases:
  - **An editor that saves in place** changes the content under the mount. The poll sees it, as
    on the host.
  - **An editor that saves by rename** leaves the console on the old inode. That inode is now
    unlinked on the host, so its `st_nlink` drops to 0.
  - **An editor that keeps a backup** (Emacs by default, or vim with `backup` set) renames the
    old file to `config.toml~` first. The pinned inode keeps `st_nlink` 1, with the same size,
    mtime and content, but its `st_ctime` changes.

  In container mode, `st_nlink == 0`, or a `st_ctime_ns` change on the same inode **while
  the size, mtime and content are unchanged (or the file cannot be read)**, means "config.toml may have been replaced on the host; this console cannot see
  the new file until it restarts". That notice offers Restart only (below) and suggests checking
  the file first with `jobhunter-ctr doctor`, which runs in a fresh container that mounts the
  new file. Neither a `touch` nor a `chmod` fires it: a `touch` changes the mtime, and the
  watcher also records `st_mode`, so a ctime change that comes with a mode change is taken as a
  `chmod` and ignored. S12b checks all three editor behaviours on your real deployment and records the
  results in the PR. The unit tests inject the stat function.

### Validating

The new file goes through `load_settings()`. If it fails, the console keeps running on the old
settings and the notice says so:

```
config.toml has errors; the console keeps the settings from 09:12.
  scoring.daily_cap_usd: Input should be a valid number
  config.toml line 14, column 9: not valid TOML
  [Details] [Dismiss]
```

**Error text never carries a value:**

- Pydantic errors are built from `ValidationError.errors()`, never `str(exc)`, which includes
  `input_value=...`. Each line is the location plus the message.
- A `value_error` message comes from a custom validator and may quote the input. It is shown
  only when its location is on `SHOWN_VALUES`; otherwise the line is the location plus "invalid
  value". The same rule applies to any message that contains `str(err["input"])`.
- TOML errors: on Python 3.13 `TOMLDecodeError` has no `lineno` or `colno`, and `str(exc)` can
  quote keys. Line and column are taken with the regex `\(at line (\d+), column (\d+)\)`, and
  the text is fixed: "not valid TOML".

The bar shows at most three lines; the details page shows them all. While the file is invalid,
`/setup` shows inventory row 1 as broken. That row was doctor-only, because the console could
not start with a broken file, but a console that is already running can now see one.

Scorer specs that `Scoring.scorer_problems()` flags do not block Apply: scoring fails soft.
They appear as warnings on the details page and on the scorer row. Their messages quote the
spec, which is on `SHOWN_VALUES`.

### The notice

A line in the `#activity` bar on every page, from the same fragment:

```
config.toml changed: 3 settings   [Apply now] [Restart console] [Details] [Dismiss]
```

- **Apply now** is shown when at least one reloadable setting changed. **Restart console** is
  shown when a restart-class one changed (and is not overridden on the command line), when
  `.env` changed, or when the container cannot see the new file. Both buttons appear when both
  kinds changed.
- **Details** opens `GET /config/changes`. It lists each changed setting by name, with its
  class (reloads, needs restart, or no effect). Old and new values appear **only for fields on
  an allowlist** (`config.SHOWN_VALUES`).
  - **On the allowlist:** the caps, scorer specs, `deep_shortlist`, `employer_rejection_days`,
    rps, timeouts, retries, `respect_robots`, `global_concurrency`, ports, the `apply` runner,
    models, effort and booleans, and `label`.
  - **Everything else shows "changed", never its value.** That includes
    `fetch.user_agent_contact` and `mail.alerts_address` (personal), `fallback_sender_domains`
    and `fallback_senders`, `capture.disabled_hosts` (shown as a count), the OpenRouter
    `provider`, `title` and `referer`, and all paths. Default is hidden: a field not on the
    list shows no value.
  - **`api_key_env` names** are shown only when both old and new match `^[A-Z][A-Z0-9_]{0,63}$`.
    Otherwise the line says "changed (not shown)". A key pasted into that field by mistake is
    never echoed.
  - **`.env` changes** show the file (with `$HOME` as `~`) and the key names added, removed or
    changed. A name is shown only when it matches `^[A-Z][A-Z0-9_]{0,63}$` **and** is a known
    secret name (`secrets.known_names`) or starts with `JOBHUNTER_`. `dotenv_values` reads a
    bare line (a key pasted without `NAME=`) as a key with no value, so such a line shows only
    as a count: "1 line that is not NAME=value". The same rule applies to the logs, the notice,
    the page and `/activity`.
- **Dismiss** hides this change. It is remembered in memory by the new file's hash, so a
  further edit shows again.
- After Apply: "Applied 3 settings at 14:05". Rule 6's restart notes on `/setup` then say
  "read at 14:05 (applied)" instead of "when the console started".

### Apply

`POST /config/apply` re-runs the check, so it applies exactly what is on disk now, not what the
page showed (if the file changed again it answers with the new notice). Then it assigns
`app.state.settings` once. One attribute assignment is atomic in CPython. A request already
running keeps the object it read, and the next request sees the new one. Apply is allowed while
work runs: a running re-score or extension score keeps its own settings, and the notice says so.
A pending re-score confirmed after a scorer change gets `PlanChanged` from the existing
plan-token check (`rescore_routes.py:152`) and shows the new plan.

### In-process work registry

A restart must not cut off paid or long work. The console runs such work in several places:

- the re-score thread (`RescoreManager`, `rescore_routes.py:156`);
- the extension's Score now threads (`ext-score-{gid}`, `ext_routes.py:484`). They make paid
  scorer calls, and the local scorer can take 900 s;
- in-request Score now (`capture_routes.py:141`, `packet_routes.py:731`);
- drafting through `generator.generate` (`packet_routes.py:455`), whose CLI runner leaves
  `_LIVE` (`cli_runner.py:246`) empty for a moment between the draft child and the
  entailment child.

So S12b adds **one registry**, `app.state.work`: a set of named entries under a lock.
`RescoreManager.start`, each ext-score thread, `score_now` and `generate` enter it on start and
leave it in a `finally`. The ext-score handler checks `restarting` and enters the registry
under the lock **before** `thread.start()`, and the thread leaves it when it ends; so does
`RescoreManager.start`. `POST /config/restart` takes the same lock. It sets
`app.state.restarting = True` **only if the registry is empty**, otherwise it answers 409 ("a
re-score / score / draft is running; restart when it finishes"), and the notice keeps offering
the button. While `restarting` is set, every one of those entry points refuses with 503
("console restarting"). That closes the window between the check and the shutdown, and covers
requests still in flight during the drain. After `server.serve()` returns, the console checks
the registry again and joins anything left (a bounded join) before it re-execs or exits.

A timer run in **another** process holding the run lock does not block a console restart: the
two are independent, and a restarted console does not take the run lock.

### Restart

`POST /config/restart`:

1. **Preflight in a child process.** Validating the file in the running process is not enough:
   that process holds the old `.env` values (which may set `JOBHUNTER_CONFIG` or a path
   variable) and the old code. So it runs
   `subprocess.run([sys.executable, "-c", <import jobhunter.console.app; secrets.startup(...); load_settings()>], env=pristine_env, cwd=<same>, timeout=60)`.
   If the child fails (a bad file, a missing dependency after a pull, an import error), it
   answers 409 with a fixed summary: the location and message for settings errors, otherwise
   the exception type. The old console keeps running. In container mode with the can't-see-file
   notice, the child cannot see the new file either, so this step is skipped. The button then
   asks for a confirm first: "Restart without checking the new file? If it has errors, the
   console will not come back until you fix it; check it with `jobhunter-ctr doctor`." The notice
   points at `jobhunter-ctr doctor`.
2. **The registry check** (above).
3. **Shutdown.** It answers 202 with a small page that polls `/healthz` and reloads when the new
   process answers. After 90 s without an answer, the page stops and says "the console did not
   come back; check the terminal (or `journalctl --user -u jobhunter-console`)". When the
   restart changes the bind host or port, the page shows the new URL and polls that one instead.
   After the response is sent (a background task), the console asks uvicorn to shut down
   gracefully. In-flight requests finish, and the lifespan releases the console lock and kills
   stray `claude` children (`app.py:234`).
4. **Then, by mode.** S12b replaces `uvicorn.run` (`cli.py:1144`) with `uvicorn.Server`, so the
   console command keeps a handle to the server and decides what to do after it returns:

| Situation | Restart does |
|---|---|
| Container, unit opted in (`JOBHUNTER_RESTART_EXIT` set) | Calls `logging.shutdown()`, flushes stdio, then `os._exit(EXIT_RESTART)`. `EXIT_RESTART = 42`, a code nothing else uses: uvicorn exits 3 on a startup failure (`uvicorn/config.py` `STARTUP_FAILURE`, also from `uvicorn.Server`), click uses 1 and 2, and `--wait` uses 75. `os._exit`, not `sys.exit`, so interpreter shutdown does not wait on non-daemon AnyIO workers or a pool left running (`ext_routes.py:329`). The unit gains `[Container] Environment=JOBHUNTER_RESTART_EXIT=42`, and `[Service] SuccessExitStatus=42` and `RestartForceExitStatus=42`. systemd restarts it although the base unit has `Restart=on-failure`, and `OnFailure=` (the failure notification) does not fire. A fresh container gets a fresh mount, so a file saved by rename is picked up. The base unit in `deploy/quadlet/` gains the three lines. Units already installed get them from `jobhunter container preflight` as a drop-in (`45-jobhunter-restart.conf`, the pattern S4 uses for the trigger marker). You approved this (2026-10-11); it needs one preflight run and `systemctl --user daemon-reload` |
| Container, not opted in | No button. The notice shows `systemctl --user restart jobhunter-console`, to run on the host. Exiting 0 would leave the console down (`Restart=on-failure`), and a non-zero exit would send a failure notification |
| Host (run by hand, or under any supervisor) | Calls `logging.shutdown()`, flushes stdio, then **re-execs**: `os.execve(sys.executable, [sys.executable, *sys.orig_argv[1:]], pristine_env)`. `pristine_env` is a copy of `os.environ` taken at the very top of the CLI callback (`cli.py:180`), before `.env` files and `*_FILE` were loaded, so the new process reads `.env` afresh. Without it the old values would stay, because `load_dotenv(override=False)` does not replace them. The PID stays the same, so a supervisor sees no exit. A re-exec runs the code on disk but does **not** run `uv sync`, which `uv run` would; the child preflight catches a missing dependency, and the help page says to run `uv sync` after a pull that changes dependencies |

**Drain time.** On the host, the drain (`timeout_graceful_shutdown`) is 200 s, longer than the
drafting timeout `cli_runner.TIMEOUT_S` (180 s). The registry already refuses a restart while a
draft runs, so this is a backstop. In a container, uvicorn closes the listener at once, so
`/healthz` fails from the start. The unit's health check (`HealthInterval=30s`,
`HealthRetries=3`, `HealthOnFailure=kill`) kills the container on the third consecutive failure,
which can come as early as (HealthRetries − 1) × HealthInterval = 60 s after the listener
closes. The container drain is therefore capped at 30 s, which leaves room for the lifespan and
the exit. A kill would count as a failure and send the notification.

A re-exec runs the code that is on disk now, as a manual restart would: after a `git pull` with
new migrations, boot migrates after a backup (host, no marker) or refuses with
`MigrationsPending` (marker). The notice warns when the code on disk differs from the running
one (version and commit from `ops/version`).

### Who may press the buttons

`POST /config/apply`, `/config/restart` and `/config/dismiss` go through the existing middleware
(loopback `Host` on every request, same-origin `Origin` on writes:
[017](017-assisted-apply.md#local-api-security)). Apply and Restart also require the **peer
address** (`request.client.host`) to be loopback, the check `/setup/live` uses. Under
`--allow-remote` they still work from a loopback peer, and a LAN client gets 403. This differs
from `/setup/live`, which refuses everything under `--allow-remote`. Why: Apply and Restart spend
nothing and use no credential. They only act on a file you wrote, so the risk is a LAN client
restarting your console, and the peer check prevents that. When the browser's own peer is not
loopback (the pasta fallback, 018 D11), the buttons are hidden and the notice gives the command
instead. Dismiss needs only the middleware. `GET /config/changes` is readable wherever `/setup`
is, and shows names and allowlisted values only, under the rules above.

### What it does not change

- **Only the console watches.** Every other command loads settings when its process starts:
  `jobhunter run` calls `load_settings()` (`cli.py:300`) and loads `.env` in the CLI callback
  (`cli.py:187`), each time a timer starts it. In containers each timer starts a new container
  with a new mount. The help page states one detail: a run started with `--wait` reads the
  config **before** it waits for the run lock (`cli.py:300`, lock at `cli.py:325`), so a run
  already waiting uses the file as it was when it started.
- `config.toml` is still never written by the console.
- Secrets are still never typed into the console. A secret change still means editing `.env`
  or the podman secret, then Restart (host) or the 018 sequence (containers).

### Testing (B)

- Classification: every `Settings` leaf is in `RELOADABLE` or restart, exactly once.
- Swap: build the app with settings A, apply B with every reloadable field changed, and assert
  that pages show B: the dashboard weekly cap (fails on today's closure), `/costs`' packet cap,
  the re-score panel's scorer, `/alerts`' placeholder warning, and the extension's
  `disabled_hosts`. Restart fields keep A after a partial apply. A `--port` override makes a
  `console.port` change "no effect".
- Detection, with an injected stat and clock:
  - a touch or a comment edit shows nothing;
  - a rename-save on the host is seen;
  - in container mode, `st_nlink == 0` and a backup-keeping save (ctime changed, content not)
    both give the can't-see notice;
  - a new `~/.env` is seen;
  - an edit made before the first poll is seen;
  - the 2 s throttle holds with three pollers.
- Invalid file: the old settings stay. A sentinel placed in a bad value of every type never
  appears in the page, the fragment or captured logs. The test is parametrised over every custom
  validator found by introspecting `Settings`. A TOML error shows line and column only.
- No value leaks: set the contact, alerts address, `disabled_hosts`, OpenRouter `referer`, an
  `api_key_env` value and a `.env` key to a sentinel, plus a bare `.env` line holding the
  sentinel. Change each, and assert the sentinel never appears in `/activity`,
  `/config/changes` or the logs.
- Registry: 409 while a re-score, an ext-score thread, an in-flight `score_now` or a `generate`
  runs; after Restart is accepted, a re-score POST, Score now and Generate get 503.
- Restart, with `os.execve`, `os._exit` and the preflight child patched:
  - assert the argv, and that the env lacks a key only a `.env` file set;
  - under `JOBHUNTER_RESTART_EXIT` the console exits with 42;
  - a server that never started exits with something other than 42;
  - a failing preflight child gives 409 with no value in the body;
  - a non-loopback peer gets 403, with and without `--allow-remote`.
- Unit files: the base console unit has `Environment=JOBHUNTER_RESTART_EXIT=42` under
  `[Container]` and both exit-status lines under `[Service]`; preflight writes the drop-in with
  the same sections.

## Addendum C: activity log (`/log`)

Added 2026-10-12 for bug `be1d232`: you could not tell whether a re-score ran (five requests
failed and nothing outside the `/prefs` panel showed it), or how many jobs came in and were
scored since yesterday. Read-only; no new table, no migration.

### Route

`GET /log`, `?kind=` one of `all` (default), `run`, `rescore`, `batch`, `deep`, `mail`,
`capture`, `draft`. `/activity` is the nav band fragment, so the page is `/log`. In the nav
("Log") and linked from `/setup`. Unknown `kind` is read as `all`. Responses are `no-store`.

### Events

Newest first, each with a local time (`localtime` filter), a kind label, a one-line summary and
a status. Each kind is fetched with its own `ORDER BY id DESC LIMIT 100` (or the newest 100
rows by `rowid`), merged, and cut to 150, so the page costs a handful of small reads however
large the database is.

| Kind | Source | Shows |
|---|---|---|
| `run` | `run` | trigger (`timer`, `manual`, or "unknown" for old rows), stages, exit status, duration, per-stage counts from `run.counts`, failed-source count from `run_source` |
| `rescore` | `rescore_request` | scorer (via `scorer_label`), scope, scored / errored / total, cost, status, error |
| `batch` | `score_batch` | a "submitted" event (`submitted_at`, scorer, tier, request count) and a "collected" event (`collected_at`, result counts) |
| `deep` | `deep_request` | mode, scorer, done / total, new reports, spend, status, error |
| `mail` | `mail_message` | messages processed in the same minute, with entry counts |
| `capture` | `capture_log` | extension captures: host, method, outcome |
| `draft` | `draft_run` | packet drafting runs: kind, runner, outcome, duration, error, and the document's API-equivalent cost when known |

A `run` with no `ended_at` is **running** only when it is the newest `run` row, the run
lock is held (`runlock.probe`, the read-only look the band uses), the holder's command is
`run`, and the holder's start time is within 5 minutes of the row's `started_at`. Every other
job (console re-score, deep pass, `score`, `backfill`) takes the same lock, so a held lock alone
proves nothing. Anything else unfinished is **interrupted**. A run that ended `degraded` is
**partial**; any other non-`ok` exit is **failed**. A drafting row with no outcome is
**running** or **stopped** by `draft_runs`' own rule (its claim is held or free). A subscription
(CLI) drafting run shows "subscription" in the cost column, not a price.

### Privacy

Nothing that is a secret, an email address, or a key-shaped scorer spec is rendered. Scorer
specs go through `config.shown_value` / `hidden_label` (the `scorer_label` filter). Stored
error text is shown through one redaction: email addresses become `(email)`, any token that
`config.hidden_value` calls key-shaped becomes `(hidden)`, URL query strings are dropped, and
phone numbers become `(number)`, `Bearer`/`Basic` credentials and `key=`/`token=`-style
assignments are masked, `%40` is read as `@`, query strings (with or without a scheme) are
dropped, and the result is cut to 200 characters. Capture hosts and stage counts are shown as stored.

### Today card

A "Last 24 hours" panel on `/`, with a link to `/log`:

- **Last run:** when, result and duration, or "running since ...", or "interrupted".
- **Since yesterday** (the last 24 hours from now): jobs pulled in (`job.first_seen_at`), new
  groups (`job_group.created_at`), **new jobs that passed the prefilter** (`first_seen_at` in
  the window and a `passed = 1` result under the current `filter_version`; a profile change
  rewrites `evaluated_at` for every job, so evaluations are never counted), and scored
  (`fit_score.created_at`). A row exactly 24 h old counts; an older one does not.
- **Failures, shown separately** because they overlap and are never summed: refused or cut off
  (`score_failure.created_at`), re-score errors (`rescore_request.errored` summed over requests
  finished in the window, with the number of failed requests), and batch errors (`errored` plus
  `expired` in the `counts` of batches collected in the window).
- **Unscored backlog:** `screen.count_eligible(..., new_only=True)`, the same rule the daily
  run uses to pick groups. Shown as "?" when the profile cannot load.
- **Scoring spend:** the sum of `fit_score.cost_usd` created in the window. Next to it,
  **spend today and yesterday (all calls)** from `llm_spend`, which has only a day, so it also
  counts drafting and refused calls. Links to `/costs`. A query that fails shows "?".

### Testing (C)

Each event kind renders; running versus interrupted (a real `RunLock` held by `run` and by
`console re-score`); partial; subscription cost; new-jobs-only prefilter count; failures from
re-scores and batches; the window at the 24 h boundary; a
sentinel email, key-shaped scorer and key-shaped error text never appear; an empty database;
the kind filter; `/log` in the nav and linked from `/setup`.

## Proposed tickets

Not filed yet; the PM files them after you approve. Labels `area:console` or `area:ops`;
worker and reviewer per the CLAUDE.md table (the reviewer is always stronger).

| # | Ticket | Difficulty | Worker / reviewer | Depends on |
|---|---|---|---|---|
| S0 | Every profile load through `load_profile_for` (runner, `_score_mode`, `eval`, `llm bench`, backfill); test that only `scoring/profile.py` passes `paths.resume_path`. PR note: if the YAML names a different resume, `scoring_version` changes for the run, `--rescore-pending` and `--deep`; existing jobs re-score only when you ask | normal | sonnet / opus | |
| S1 | `readiness.py`: `Check`, modes, every local check, `credential_for`, `ANTHROPIC_AUTH_TOKEN` as a known secret, `_absolute_path_count` moved to `core/dbpaths.py`, the keyring-raises fixture, no-leak and no-subprocess tests. Resume row compares both resolutions until S0 | normal | sonnet / opus | |
| S2 | `jobhunter doctor`: output, `-v`, `--json`, `--group`, exit codes, guard exemptions, read-only DB, `--containers` with `build_preflight_context()` and fixed ids; console refusal messages point at `doctor` | normal | sonnet / opus | S1 |
| S3a | `/setup` page and its routes (`starter-prefs`, `resume`, `dismiss`), the email-alerts guide rows with the Cloud steps from `CLOUD_STEPS`, target paths and commands inline, mode-aware fixes (`--replace` when the secret exists) and host paths, restart notes; no `/help` links (S11e); dark mode and 375 px | normal | sonnet / opus | S1 |
| S3b | Banner on `/` and `/inbox` replacing the profile banner, Hide, first-run redirect (and the `GET /` test updates), `init` and `console` hints | normal | sonnet / opus | S3a |
| S4 | Snapshot: sectioned `readiness.json` with a lock and per-section writes; `doctor --snapshot`; `run` writes inputs after the lock and the outcome at the end; `JOBHUNTER_TRIGGER=timer` in the base units, a preflight-written trigger drop-in for installed Quadlet units, `schedule install`/`status` for the host unit, a `run.trigger` column with old rows `NULL`, the unmarked-unit fallback; `not_run` outcome; host section; console effective status (worse of two, newer inputs clear an outcome, unknown rules) | hard (several processes, freshness rules, a migration) | opus / opus at higher effort | S2, S3a, S7 |
| S5 | `mail auth` writes `gmail-auth.json` (issued_at, storage kind; `--from-local` keeps the old date); the token-age rules and `RefreshError` mapping in the Gmail row | normal | sonnet / opus | S1 |
| S6 | Live checks: route gated on a loopback peer address (403 under `--allow-remote`), buttons, `jobhunter-ctr doctor --live` hand-off in host-managing mode, `doctor --live`; booleans only; uncached `auth_status()`; OpenRouter only on `openrouter.ai`; tests prove no model call and no leak | hard (credit paths, credentials) | opus / opus at higher effort | S2, S3a |
| S7 | Score stage asks `credential_for` first and prints the skip line on `missing`; exit code unchanged; the skip line's troubleshooting entry (if `troubleshooting.md` exists, else S11c adds it) | normal | sonnet / opus | S1 |
| S8 | Docs: first-run pointers to `/setup`, `doctor` and `/help/getting-started` in `README.md` and the containers help page. (`docs/gmail.md` is dropped: `help/email-alerts.md`, S11c, is the repository copy) | mechanical | haiku / sonnet | S2, S3a, S11a, S11b, S11d |
| S9 | `mail.fallback_senders` (exact sender addresses, `from:<address>` filters in `mail setup`); `/alerts` rows for LinkedIn and Craigslist (sign-up steps, address rule, "read when the parser lands" with the count); the domain-wide fallback warning; the re-read requirement added to `430a0e7` and `b0ac328` | normal | sonnet / opus | |
| S10 | USAJOBS with no credentials configured is a skip (`unavailable()`, like the mail source), not a failure; a rejected key still fails (approved 2026-10-11) | normal | sonnet / opus | |
| S11a | Help mechanism: `console/help/` package folder, `markdown-it-py` as a direct dependency, renderer (`html: False` explicit, marker lines stripped before parsing, `slug()` matching GitHub, topic and repository link rewriting, mtime cache), `GET /help` and `/help/{topic}` with a fixed topic list, Help in the nav, generated-block mechanism and `regenerate()` (blank lines around markers), the drift tests over every help file (commands, `[section] key`, links and anchors, repository files, unit names, `<you>` placeholders and the sender allowlist, no `jh:` in rendered output, sentinel render, works with no profile and during a 503). Moves README's "Running nightly" and "Browser extension" into `nightly-runs.md` and `browser-extension.md`; README gets the Help table; `CLAUDE.md` gets the help link. Dark mode, 375 px | normal | sonnet / opus | |
| S11b | Help content: `getting-started`, `profile-and-resume`, `scorers` (moves README "Choosing a scorer"; generated provider table; the Anthropic key), `openrouter`, `costs-and-credits`, `drafting` (Claude Code login). Every claim checked against the code by the reviewer | normal | sonnet / opus | S11a, S2, S3a |
| S11c | Help content: `email-alerts` (Gmail end to end, one heading per step; generated `CLOUD_STEPS` block), `settings-and-secrets` (generated environment-variable table with `CLAUDE_*` and `XDG_*`, and its source-scan test), `troubleshooting` (incl. the drafting runner's errors). `doctor --live` appears only in prose until S6 | normal | sonnet / opus | S11a, S2, S3a |
| S11d | Move `docs/containers.md` to `help/containers.md` (keeping history), leave a pointer, rewrite its repository links per the renderer rule, retarget `test_docs_containers.py` | mechanical | haiku / sonnet | S11a |
| S11e | Help links from `/setup`: `Fix.help` on every check id, built with `slug()`, with a test that each one resolves to an existing heading; `GET /setup/gmail` as a 303 to `/help/email-alerts#step-1-google-client-json` and `/alerts`' `SETUP_DOC` pointed there; the step-1 help link and the `/help/containers#secrets` link; `/setup`'s internal-link test extended to `/help`; the "New here?" link on `/setup`; `init`, `console` and `doctor -v` name the help topic (after S3b, which edits the same hint lines) | mechanical | haiku / sonnet | S2, S3a, S3b, S11b, S11c, S11d |
| S12a | Config watch: the fingerprint poll inside `/activity` (2 s throttle, ctime and nlink, no initial baseline, `.env` digests at start-up), load and diff against the running settings, `RELOADABLE` and `SHOWN_VALUES` with the classify-every-field test, the command-line override rule for host and port, value-free errors (custom-validator and TOML rules), name-shaped-only `.env` keys and `api_key_env` values, the notice line, `GET /config/changes`, `POST /config/apply` (partial apply keeps restart fields), `/config/dismiss`, the weekly-cap closures at `app.py:354` and `app.py:400` moved to `app.state.settings`, the loopback-peer gate, row 1 and the rule 6 note on `/setup`, the generated reload table in `settings-and-secrets.md` and the config-watch troubleshooting entries (it creates either file with just its part if S11c has not landed; whichever lands second rebases) | hard (live settings swap under concurrent requests, privacy of values) | opus / opus at higher effort | S1, S3a, S11a |
| S12b | Restart: the in-process work registry (re-score, ext-score threads, `score_now`, `generate`) and the `restarting` flag with 409/503; the preflight child under the pristine environment; `uvicorn.Server` in `console`, drain 200 s host / 30 s container, then `logging.shutdown()` and re-exec (host) or `os._exit(42)` under `JOBHUNTER_RESTART_EXIT` (containers); the base console unit's three lines in the right sections and preflight's `45-jobhunter-restart.conf` drop-in; container replaced-file detection (nlink 0, ctime with a backup), checked with three editor behaviours on the real deployment; the 202 page with its 90 s timeout and new-URL case | hard (process lifecycle, money paths, systemd, containers) | opus / opus at higher effort | S12a |

Order: S0, S1, S9, S10 and S11a can start together; then S2, S3a, S5 and S7 after S1; S3b after
S3a; S4 after S2, S3a and S7; S6 after S2 and S3a. S11d after S11a; S11b and S11c after S11a, S2
and S3a; S8 after S2, S3a, S11a, S11b and S11d; S11e after S2, S3a, S3b, S11b, S11c and S11d. S12a after
S1, S3a and S11a, then S12b. (`75dacda` and `edb9ba6`, which S11b and S12a used to wait on, are
merged.)

**Estimate**, by the calibrated rule (ticket wall-clock: mechanical 0.75 h, normal 1.4 h, hard
2.1 h; schedule = sum / 3, plus 30%, range 0.7x to 1.6x; floor = the longest dependency chain):

- Sum: 12 normal (16.8 h) + 4 hard (8.4 h) + 3 mechanical (2.25 h) = 27.45 h.
- Schedule: 27.45 / 3 = 9.15 h, plus 30% = **about 11.9 h** of continuous run, range about 8.3
  to 19 h.
- Floor: the longest chain is S1, S3a, S12a, S12b: 1.4 + 1.4 + 2.1 + 2.1 = 7.0 h. The next are
  S1, S2, S11c, S11e and S1, S3a, S3b, S11e (4.95 h each). 7.0 h is below the range's low end
  (8.3 h), so it does not bind.
- Tokens, per ticket (normal 0.6M, mechanical 0.5M, hard 2.0M): 12 x 0.6 + 3 x 0.5 + 4 x 2.0 =
  **about 17M**.
- The addenda (S11a to S12b, seven tickets) cannot run as a separate set: S11b, S11c, S11e and
  S12a wait on S1, S2, S3a and S3b. Their share is 9.9 h of ticket time and about 6.8M tokens.
- Not included: waiting for PRs to merge between phases, and your review time.

**Later, not proposed now:** preferences in the database instead of `preferences.yaml`, if you
find you never hand-edit the YAML (it would reverse 014's file-as-source-of-truth decision).

## Open questions

None. Answered 2026-10-11:

- S10 (USAJOBS with no credentials is a skip): approved.
- Help stays plain Markdown files under `src/jobhunter/console/help/`, the single source,
  rendered by the console. `README.md` **and** `CLAUDE.md` link to them, so you and the agents
  share the same text.
- Move README's user sections and `docs/containers.md` into help: yes.
- A Restart button for the container console, with the unit lines: yes.

## History

### Revision 4b: final check

One blocking problem: S3a linked `/help` pages that S11a, S11c and S11d create, while S11c
waits on S3a. That made a cycle, and the console would have shipped 404 links. The
`/setup/gmail` redirect, the `SETUP_DOC` change and the `/setup` help links move to S11e. S3a
keeps the steps, paths and commands inline. Also: S8 waits on S11b; the container ctime notice
needs an unchanged or unreadable file; a Restart that skips the child check asks to confirm;
`CLAUDE_CODE_DISABLE_*` joins the environment table; the config-watch and S7 troubleshooting
entries belong to S12a and S7; the ext-score thread registers under the lock before it starts;
README's Layout and migration sections stay for now; the `load_dotenv` citation is corrected.

### Revision 4a: review of the addenda

A review found 15 issues (4 blocking), all addressed:

- **Restart refusal.** It now uses an in-process work registry. The registry covers the
  re-score, the extension's Score now threads, in-request Score now and drafting, and a
  `restarting` flag refuses new paid work, so nothing paid is cut off and the check cannot race.
- **Secret-shaped names.** `.env` key names and `api_key_env` values are shown only when they
  look like a variable name. A key pasted without `NAME=` is counted, never shown.
- **Markers.** Raw HTML off escapes the `jh:` markers instead of dropping them, so the renderer
  strips marker lines before parsing and a test checks the output.
- **Dependencies.** S11b, S11c, S11e, S8 and S12a gained the dependencies their content and tests
  need. The floor is now 7.0 h, and the standalone addenda estimate is dropped.
- **Smaller:** restart preflight in a child under the new environment; exit code 42, not 3
  (uvicorn's startup failure); a 30 s container drain with the health-check arithmetic, and
  `os._exit`; ctime for editors that keep a backup; value-free errors for custom validators and
  TOML; host and port set on the command line count as "no effect"; no startup baseline gap;
  repository links in moved pages go to GitHub; one Gmail how-to (`/setup/gmail` redirects);
  `CLAUDE_*` and `XDG_*` in the environment table; the Anthropic key and Claude Code login
  covered; `<you>` placeholders, GitHub slugs and unit locations in the drift tests.

Your answers (2026-10-11): help is plain Markdown in the package, linked from `README.md` and
`CLAUDE.md`; the move into help, yes; the container Restart button, yes.

### Revision 4: help and config watch

Two addenda for your 2026-10-11 requests, plus additions from the PM the same day (Gmail
end to end, OpenRouter, environment variables and secrets):

- [Addendum A](#addendum-a-help-by-topic) (bug `e2f8c10`): help pages as packaged Markdown
  rendered by the console and linked from the README; text that must match the code is
  generated and test-checked; reachable from the nav, every `/setup` row and the first-run page;
  no search. Tickets S11a to S11e. S8 no longer writes `docs/gmail.md`.
- [Addendum B](#addendum-b-config-watch) (bug `ccb3b21`): the console checks `config.toml`
  and the `.env` files on the existing `/activity` poll, validates the new file with the same
  loader, and applies reloadable settings in place or restarts (re-exec on the host, a dedicated
  exit code under Quadlet). It refuses while a re-score or a drafting run would be cut off. Values
  are shown only for an allowlist. Tickets S12a and S12b. Rule 6 points here.

### Revision 3a: final check

One blocking problem: units installed before S4 never get the timer marker (Quadlet units are
copied by hand and `upgrade` copies none), so "timers not running" would have been a permanent
false alarm. Preflight now writes the marker as a drop-in, `schedule install`/`status` handle the
host unit, an unmarked unit falls back to the any-run rule with a re-run hint, and old rows
migrate to `NULL`. Also: live buttons shown only to a loopback peer (pasta, D11);
`run --stage prefilter,score` labelled as spending credits; a `not_run` outcome; the LinkedIn
sender read from an alert's From line; the resume folder's mtime in the cache key; the
`functools.cache` limit stated.

### Revision 3: second review and your S10 answer

A re-review found 17 issues (3 blocking), all addressed:

- The Gmail how-to is served by the console (`GET /setup/gmail`, from `CLOUD_STEPS`) instead of a
  repository doc it cannot serve; `/alerts`' dead `SETUP_DOC` link points there too.
- The LinkedIn step no longer suggests a `linkedin.com` domain filter, which would hide recruiter
  mail: S9 adds exact-sender filters (`mail.fallback_senders`). Alerts received before a parser
  lands are re-read from Gmail once when it does.
- `readiness.json` is sectioned with a lock, so writers do not erase each other; a manual
  `doctor --snapshot` never stands in for the nightly run; a newer run clears an old outcome.
- Timer runs are marked with `JOBHUNTER_TRIGGER=timer` and a `run.trigger` column, so a manual
  run does not hide dead timers.
- Preflight lines get ids at every call, with a catch-all that keeps severity.
- The live route checks the peer address and refuses everything under `--allow-remote`.
- Smaller: `ANTHROPIC_CONFIG_DIR` and the SDK's `active_config` pointer; `legacy_data`'s git call
  allowed in plain `doctor`; caches that make the 50 ms budget real; `--replace` for existing
  secrets; host-managing `doctor --live` defers to `jobhunter-ctr`; S4 depends on S7; internal
  links only in the link test; estimates on the calibrated rule.

Your answer (2026-10-11): S10 approved.

### Revision 2: adversarial review and your answers

An adversarial review found 31 issues (8 blocking), all addressed:

- S0 covered only the runner; `score` (including the collect and rescore timers), `eval` and
  `llm bench` have the same explicit resume path. Widened, plus a container check for absolute
  YAML resume paths.
- The Anthropic credential rule did not match the installed SDK (auth tokens, profiles); the
  constructor does not raise, so the observed message is `submit failed`.
- Two modes were not enough: a host managing containers now has its own mode.
- The snapshot could not override a console "ok" while the nightly run failed to score; it now
  can (effective status, outcome codes, written after the lock, freshness from the `run` table).
- Inline fixes named routes that cannot work (`/prefs/save` with no fields, `/prefs/resume`
  returning a full page); four new routes are specified, Hide included, with the live route
  loopback-only.
- Email alerts are a numbered guide with LinkedIn and Craigslist steps and the `/alerts`
  checklist.
- Container secret commands now match 018 (`read -rs`, preflight, daemon-reload, restart); the
  host `.env` is created private first.
- Local checks no longer use the keyring, a subprocess, `chmod` or a tree walk; `doctor` runs
  preflight only with `--containers`, and its FAILs are essential when the marker exists.
- Live checks return booleans only; the Gmail check does not echo `mail status` keys, the runner
  check is uncached and never turns the runner off, OpenRouter's masked `label` is never shown.
- The run already exits 1 on any failed source; the Why section and the inventory say so, and S10
  is proposed.
- Smaller: rows 1-3 are doctor-only; restart notes on config rows; the redirect uses "missing or
  broken"; Gmail `due` state and `--from-local`; `unknown` semantics; S3 split; ticket order made
  consistent; the snapshot moved to S4.

Your answers (2026-10-11): the User-Agent contact is essential; a run with no usable scorer
exits 0; `/` redirects to `/setup` on first run only.
