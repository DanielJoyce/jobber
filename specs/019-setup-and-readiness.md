# 019 — First-run setup and readiness

Status: **draft for review** (bug `ba4d25f`). Written 2026-10-11.

You asked that on first launch jobhunter guide you through supplying your resume,
preferences, alert-email setup and the rest, and say what is missing and which features will not
work until it is supplied. This spec adds three things that share one set of checks:

- **`/setup`** in the console: every input jobhunter reads, each with a status (ok, missing,
  broken, optional), what it unlocks, and how to fix it. The fix is an inline form where the
  console can already do the job (resume upload, a starter `preferences.yaml`, extension
  pairing), and the exact command otherwise.
- **A banner** on Today (`/`) and the inbox, linking to `/setup`, shown while an essential is
  missing or broken. Essentials cannot be dismissed. Everything else can.
- **`jobhunter doctor`**: the same checks in the terminal. It exits non-zero when an essential is
  missing.

Nothing here adds a new input. It reports on the ones that exist and points at the fix that
already exists.

## Why

Missing inputs fail quietly today. The worst case: `scoring.screen_scorer` defaults to
`anthropic:claude-haiku-4-5`. With no `ANTHROPIC_API_KEY`, the daily run's score stage catches
the error, adds `score: no Anthropic client (...)` or `score: submit failed (...)` to the run
summary, logs a warning, and **exits 0** ("scoring trouble must not fail the ingest exit code",
`pipeline/runner.py`). The timer succeeds, `OnFailure=` never fires, and new jobs wait unscored
with nothing in the console to explain why. Most other inputs behave the same way (see the
inventory below): a log line, a skipped source, a default that looks real.

## Decisions already made (2026-10-11)

| Question | Answer | Why |
|---|---|---|
| Where preferences live | **`profile/preferences.yaml` stays the source of truth** ([014](014-preferences-console.md#source-of-truth-stays-a-file)) | You can hand-edit and diff it, and the pipeline reads it. Moving prefs into the database is out of scope; it can be a later ticket if you stop hand-editing YAML |
| Where settings live | **`config.toml` stays a file** | It is read before the database opens: it says where the database is. In a container it is mounted read-only ([018](018-containers.md) D3) |

## What exists today (checked in code, 2026-10-11)

Every setting, file, secret and outside dependency a feature needs, found by grepping
`src/jobhunter`. **Level** is how `/setup` treats it: **E** essential (banner, not dismissible),
**R** recommended (dismissible), **O** optional (shown only when your config chooses it, or under
"More").

| # | Input | Read by | Level | Without it, today | How it fails today |
|---|---|---|---|---|---|
| 1 | `config.toml` parses, no unknown keys | `config.load_settings` | E | Nothing runs | **Crash**: every command and the console stop with a TOML or pydantic error naming the key. Missing file is fine (defaults) |
| 2 | Old repo-relative data layout | `legacy_data.check` | E | Commands refuse | **Refusal** with a `migrate-paths` hint |
| 3 | Database present, schema current | `ops/version.database_state`, `core/db` | E | Console and commands | Host without the deployment marker: migrates on the next command (after a backup). With the marker (containers): **refusal**, `MigrationsPending` or `SchemaTooNew` |
| 4 | `fetch.user_agent_contact` | `Settings.user_agent`, sent by `FetchContext` on every request | E | Compliance ([008](008-compliance.md)): admins have no contact | **Silent**: the placeholder `<you>@example.com` goes out in every User-Agent |
| 5 | `preferences.yaml` present and valid | `scoring/profile.py`, runner, console | E | USAJOBS queries (`from_profile`), prefilter, scoring, buckets, packets | Run: **log warning** `profile unusable`, summary line `skipped (no profile ...)`, exit 0. Console: inbox banner; `/prefs` offers a skeleton (missing) or field errors (broken) |
| 6 | Resume: one readable, non-empty `.md` | `profile._resolve_resume` | E | Scoring, `scoring_version`, packet drafting | Same as 5: the whole profile fails to load. See [resume resolution](#resume-resolution-differs-between-run-and-console) |
| 7 | Screen scorer usable: `scoring.screen_scorer` and its credential | `scoring/scorers.py`, runner `_score_stage` | E | All fit scores, buckets A-G, the inbox's ordering | **Silent** (above). `anthropic:` needs `ANTHROPIC_API_KEY`; `openrouter:` and `jev:` need `OPENROUTER_API_KEY` (`scoring.openrouter.api_key_env`), else `ScorerError` caught as `score: failed`; `openai-compat:` needs its `api_key_env` unless set empty; `local:` needs the server up (and `LLAMA_API_KEY` if it wants one) |
| 8 | `scoring.daily_cap_usd`, `weekly_cap_usd` above 0 | `screen.remaining_budget` | E if 0 | Scoring | Summary line `spend cap reached`, exit 0. A cap reached today is shown as a note, not a fault |
| 9 | Profile content: `target_titles` or `queries`; `salary_floor`; `current_focus`, `done_with` | USAJOBS `queries_from_profile`, prefilter, rubric | R | Empty titles and queries: USAJOBS gets **no queries** and fetches nothing. No floor: `comp` is null. No focus: recency weighting has little to go on (bug `591fb3c`) | **Silent** |
| 10 | `USAJOBS_API_KEY`, `USAJOBS_EMAIL` | registry `auth.key_env`/`email_env`, `adapters/usajobs.py` | R | Federal jobs, the best source ([011](011-federal-and-geography.md)) | `UsajobsAuthMissing` each run: source marked failed in `source.status_note`, **log warning**, run continues |
| 11 | `mail.alerts_address` | `mail/setup.py`, `console/alerts.py`, `mail sample` | R | Gmail filter, the address the `/alerts` checklist tells you to sign up with | `mail setup` **refuses** the placeholder; `/alerts` shows a placeholder warning |
| 12 | Google OAuth client JSON | `mail.client_secrets_path`, `$JOBHUNTER_GOOGLE_CLIENT_SECRETS`; container `/run/secrets/google_client_secret.json` | R | Email ingest, `mail auth` | Run: mail source **skipped** with `Google client secrets file missing`. `mail auth` refuses |
| 13 | Gmail refresh token | keyring, else `~/.config/jobhunter/gmail_token.json`; container `$JOBHUNTER_GMAIL_TOKEN_FILE` (podman secret) | R | Email ingest | Missing: source **skipped**, `no Gmail token`. Expired (the app is in Testing, so a token lasts 7 days; [018](018-containers.md#gmail)): the mail source **fails every run** with Google's `RefreshError` in `source.status_note`. **No issue date is recorded anywhere**, so nothing warns before it lapses |
| 14 | Gmail label and filter | `mail setup` | R | Alerts stay in your inbox; the label is empty | Only `mail status` (a Gmail API call) can tell |
| 15 | Alert subscriptions on blocked boards | `/alerts` (`alert_signup`, `mail_message`) | R | Coverage of the 40-odd banks that block crawlers ([008](008-compliance.md)) | The checklist exists; nothing points you to it |
| 16 | Alert parsers | `mail/parsers`: `vos`, `joblink`, `neogov`, `nlx`, `generic` | info | LinkedIn alerts (bug `430a0e7`) and Craigslist (bug `b0ac328`) have no parser yet | **Silent**: their messages yield no jobs |
| 17 | Deep pass: `scoring.deep_scorer` (`anthropic:claude-opus-5`) | `score --deep`, `score --collect-pending` | R when the screen scorer is Anthropic, else O | Stage 3 shortlist | `score --deep` errors when run |
| 18 | Drafting runner ([017](017-assisted-apply.md)) | `apply.runner`: `cli` needs `claude` on `PATH` (`cli_runner.find_binary`) and a claude.ai login (`claude auth status`); `api` needs `ANTHROPIC_API_KEY`. Runner-off state in `<data dir>/apply-runner.json` | R | Generate on a packet (resume, letter, answers), the entailment check | Generate shows the error and offers the other runner; never switches by itself. Container console: drafting is deferred to the host (`apply draft`, C9) |
| 19 | `apply.daily_cap_usd` | `apply/generator.py` | info | Paid packet drafting | 0 refuses paid runs, by design. Shown as a note |
| 20 | Extension pairing | `<data dir>/extension.json` (`ext_pairing.status`) | O | Send to jobhunter from Chrome | The extension gets 401; `/prefs` has the Pair button |
| 21 | PDF export | `core/pdf.py` (the `browser` extra and Chromium) | O | Packet PDFs | `PdfUnavailable` with an install hint, on click |
| 22 | Scheduled runs | host: `systemd --user` timers (`schedule install`); containers: Quadlet timers | R | Nightly ingest and scoring | **Silent**: nothing runs. The `run` table's newest `started_at` shows it |
| 23 | Backups | `<data dir>/backups/` and `backups/auto/` (`core/backup.py`) | R | Recovery | Containers back up nightly (C6). Host mode has no automatic backup, only pre-migrate ones |
| 24 | Secrets valid, `.env` files private | `secrets.startup` report, `secrets.env_file_warnings` | R | Whichever feature uses the key | A bad value is **dropped with a warning** at CLI start, then the feature fails as if unset. A 0644 `.env` gets a warning |
| 25 | Data directory 0700, database 0600 | `ops/migrate_paths.secure_data` (run by `init`) | R | Privacy | **Silent** if loosened later |
| 26 | Container deployment: podman, mounts, SELinux labels, drop-ins, deployment marker, image at the host's schema, relative paths in the DB | `ops/preflight.py` (C13) | R in container deployments | The Quadlet units | `container preflight` prints FAIL lines; the units refuse to start |
| 27 | Console exposure | `console --allow-remote`, `console.host` | info | | A non-loopback bind is refused without the flag; with it, there is no auth |

### Resume resolution differs between run and console

`console` and `/prefs` load the profile through `load_profile_for`: the `resume_path` in
`preferences.yaml` wins and `paths.resume_path` is the fallback. The run's
`default_profile_loader` (`pipeline/runner.py`) passes `paths.resume_path` as an explicit
argument, so it **always** wins there, and the YAML value is ignored. With the YAML pointing
somewhere else, the console shows one resume and the nightly run scores with another, or finds
none and skips scoring with a log line. Ticket S0 fixes the run to use `load_profile_for`.
`/setup` and `doctor` resolve the resume the way the run does after S0. Until S0 lands they
report both paths when they differ, as broken.

## Rules for every check

1. **Local and free by default.** A check reads files, environment variables, `config.toml`,
   the database (read-only connection, like `database_state`), and `PATH`. No network, no
   model call, no subprocess other than `shutil.which`. Rendering `/setup` or running `doctor`
   never spends a credit and never contacts a third party. `doctor` on the host may also run
   `container preflight` in dry-run mode (local podman calls, see [Containers](#containers)).
   The page never runs a subprocess.
2. **Format, not validity.** A key's presence, source and shape can be checked locally: no
   whitespace (the C2 rules), and the expected prefix for Anthropic (`sk-ant-`) and OpenRouter
   (`sk-or-`) keys. A wrong prefix is a note ("doesn't look like an Anthropic key"), not broken.
3. **Live checks are separate and explicit.** See [Live checks](#live-checks).
4. **Never show a value.** No key, token, prefix, length, email address or contact string, on the
   page, in `doctor` output, in `--json`, or in a log. The page says "set (env)",
   "set (file /run/secrets/...)", "set (.env ~/.env)", "invalid (contains whitespace)" or
   "unset", reusing `SecretState.describe`. For `user_agent_contact` and `alerts_address` it
   says "set in config.toml" or "still the placeholder". Paths are shown, with `$HOME` as `~`.
5. **No stored free-form error text.** `source.status_note` holds `str(exc)`, which has carried
   secrets before ([018](018-containers.md) "Secret leak path"). `/setup` maps known failures to
   fixed messages (Google `RefreshError` or `invalid_grant`: "Gmail token expired or revoked";
   `UsajobsAuthMissing`: "USAJOBS credentials missing") and otherwise shows only the exception
   type and a link to `/sources`.
6. **Same answer everywhere.** One module computes the checks; the page, the banner and `doctor`
   only render them.
7. **Restart notes.** The console resolves secrets once, at start (`secrets.startup` in the CLI
   callback). The page says so next to every secret row: "read when the console started at
   HH:MM; restart the console after changing `~/.env` or a podman secret".

## The checks module

`src/jobhunter/readiness.py`:

```python
@dataclass(frozen=True)
class Check:
    id: str            # stable, e.g. "scorer.screen", "gmail.token"
    group: str         # Basics, Scoring, Sources, Email, Apply, Running, Containers
    level: str         # "essential" | "recommended" | "optional" | "info"
    status: str        # "ok" | "missing" | "broken" | "optional" | "unknown"
    title: str         # "Screen scorer"
    detail: str        # fixed wording, never a value
    unlocks: str       # "Fit scores and buckets for new jobs"
    fix: Fix | None    # an inline action id, a command, or a link
    source: str = ""   # where the value came from: "config.toml", "env", "podman secret"

def run_checks(settings, *, secrets_report, now, mode) -> list[Check]: ...
def essentials_ok(checks) -> bool: ...
```

Statuses:

| Status | Meaning |
|---|---|
| ok | Present and well-formed |
| missing | Not supplied |
| broken | Supplied but unusable: invalid YAML, a bad secret, the placeholder, an expired token, a schema that needs `upgrade` |
| optional | An optional input you have not set, which nothing in your config needs |
| unknown | This process cannot see it (a secret only the run container gets); shown with the last [snapshot](#containers) if there is one |

Checks reuse what exists: `secrets.Report` states, `secrets.env_file_warnings`,
`ops/version.database_state`, `legacy_data.check`, `ext_pairing.status`, `runner_state`,
`MailAlertsAdapter.unavailable`, `prefs_graceful.lenient_state` (the same missing/broken/resume
states `/prefs` shows), the `/alerts` checklist counts, and the pure helpers in
`ops/preflight.py` (`secret_matrix`, `host_timers_enabled`, `unlabelled_files`, the
absolute-path count). Whether a scorer needs a key comes from one new function in
`scoring/scorers.py`, `credential_for(spec, scoring) -> (env_name | None, needed: bool)`, which
the run's score stage also uses (ticket S7) so the two cannot disagree.

Each check costs a `stat`, an environment lookup or one small indexed query. `run_checks`
must finish in under 50 ms on your real data. The banner runs only the essential checks.

### Gmail token age

Nothing records when a token was issued, and the Testing-mode token lapses after 7 days. Ticket
S5 has `mail auth` write `<data dir>/gmail-auth.json` (0600): `issued_at` (UTC), where it was
stored (`keyring`, `file`, or `podman secret <name>`), never the token. The check then reads:

- ok, with "re-auth due about <date>" from day 0 to day 5;
- ok, flagged "re-auth due tomorrow" on day 6;
- broken after day 7, or when the latest mail source status is a `RefreshError`, whichever
  comes first. Fix: `jobhunter mail auth` (host) or
  `jobhunter mail auth --podman-secret jobhunter_gmail_token` (containers).

A token from before S5 shows "issue date unknown" and relies on the source status.

## `/setup`

Nav entry **Setup**, after Prefs. Groups in the order you meet them on a fresh install:

| Group | Rows (inventory #) |
|---|---|
| Basics | Config file (1), data layout (2), database (3), contact for the User-Agent (4), data permissions (25) |
| Profile | Preferences (5), resume (6), profile completeness (9) |
| Scoring | Screen scorer (7), spend caps (8), deep pass (17) |
| Sources | USAJOBS (10), scheduled runs (22), last run |
| Email alerts | Alerts address (11), Google client (12), Gmail token (13), label and filter (14), subscriptions (15), parsers (16) |
| Apply | Drafting runner (18), packet cap (19), extension (20), PDF export (21) |
| Running | Secrets and `.env` (24), backups (23), console exposure (27), containers (26) |

Each row: status word with an icon (never colour alone), the title, one line on what it unlocks,
where the value came from, and the fix. Rows that are ok collapse to one line; a group whose
rows are all ok collapses to its header. "Show everything" expands all.

```
Scoring
  ✕ missing   Screen scorer                       Fit scores and buckets for new jobs
              screen_scorer is not set in config.toml, so the default
              anthropic:claude-haiku-4-5 is used, and ANTHROPIC_API_KEY is unset.
              New jobs are not being scored.
              Fix, either:
                · add ANTHROPIC_API_KEY to ~/.env (then chmod 600 ~/.env), restart the console
                · or pick another scorer in config.toml, e.g.  [scoring] screen_scorer = "jev:typesafe/jev-1.13"
              [ Test key (free) ]  appears once a key is set

  ✓ ok        Spend caps                          $2.00/day, $10.00/week; $0.41 spent today
```

### Fixes on the page

The page writes nothing new; inline fixes post to routes that exist and pass the same checks.

| Row | Fix on `/setup` | Route reused |
|---|---|---|
| Preferences missing | **Create starter preferences** (the skeleton `/prefs` writes today), then a link to `/prefs` to fill it in | `/prefs/save` in the missing state |
| Preferences broken | The first error and **Fix in /prefs** (field errors and the raw YAML editor live there) | link |
| Resume missing | Upload (`.md` or `.txt`, 1 MB), same checks and messages | `POST /prefs/resume` |
| Extension unpaired | **Pair browser extension** (the one-time code panel) | `POST /prefs/ext/pair` |
| Alert subscriptions | Count not yet subscribed, link to `/alerts` | link |
| Profile completeness | Which fields are empty, link to the `/prefs` section | link |
| Everything else | The command or `config.toml` lines, with a copy button | none |

**No secret is ever typed into the console.** The page shows where a key goes and the command
for each mode: a line for `~/.env` on the host; on containers,
`printf '%s' "$KEY" | podman secret create jobhunter_anthropic -` followed by
`jobhunter container preflight`. Why: the console has no authentication, a form would put the
key in a request body and browser history, and in containers secrets belong to podman.

**`config.toml` is not edited from the page.** For the placeholder contact and alerts address
the page shows the file path and the exact lines to add. Why: it is read once at process
start and is a read-only mount in containers, and the decision above keeps it a file.

### Live checks

Each is a button on its row and part of `doctor --live`. All are free: they read account or
key metadata and never call a model. Each runs only on its own click, through the same
same-origin POST rule as every other console write.

| Row | Live check | Network | Cost |
|---|---|---|---|
| Anthropic key | `GET /v1/models` through the `anthropic` SDK | api.anthropic.com | free, no tokens |
| OpenRouter or Jev key | `GET /api/v1/key` | openrouter.ai | free; also shows the remaining credit limit if set |
| Local model | `/health` on the configured loopback URL (the existing `llm status`) | loopback | free |
| Gmail | Refresh the access token, then `users.labels.list` for the label (what `mail status` does) | Google | free (API quota only). Containers: the console cannot see the token; the row gives `jobhunter-ctr mail status` instead |
| Drafting runner (`cli`) | `claude auth status --json` (the existing `check_auth`) | may contact claude.ai | free |

The button label says "Test (free)". **No live check may spend credits.** A future check that
would must be labelled "Test (may spend credits, about $X)", must ask for confirmation, and is
never part of `doctor --live`. A live result is shown on the row with its time and is not
stored.

## Banner

On `/` and `/inbox` (Today and the inbox are where you land), above the page content:

```
  Setup: 2 essentials missing, so new jobs are not being scored.   [ Open setup ]
  Screen scorer · Resume
```

- Shown while any essential check is missing or broken. **Not dismissible.** It names the
  missing items and the main consequence, in one line.
- Below it, while no essential is missing, a quieter line for recommended items:
  "3 recommended items: USAJOBS key, Gmail token, scheduled runs. [Open setup] [Hide]". **Hide**
  dismisses those items; a dismissed item reappears if its status changes (for example ok to
  broken when a token expires). Dismissals live in `<data dir>/setup-dismissed.json` (0600,
  check id and the status it was dismissed at), so they survive restarts and browsers.
  Optional and info rows never appear in a banner.
- The inbox's existing "No profile yet" banner becomes this banner (one banner, not two).
- **First run.** While an essential is missing **and** the database has no jobs, `GET /`
  redirects (303) to `/setup`. Once any job exists, the redirect stops and only the banner
  remains, so the redirect never hides your data.
- `jobhunter init` ends with "next: open http://127.0.0.1:8808/setup, or run `jobhunter
  doctor`". `jobhunter console` prints one stderr line at start when essentials are missing.

## `jobhunter doctor`

```
$ jobhunter doctor
Basics
  ok       config file          ~/.config/jobhunter/config.toml
  BROKEN   User-Agent contact   still the placeholder; add [fetch] user_agent_contact to config.toml
Profile
  ok       preferences          ~/.local/share/jobhunter/profile/preferences.yaml
  MISSING  resume               no .md file in ~/.local/share/jobhunter/resume
...
2 essentials missing or broken, 4 recommended. Details: jobhunter doctor -v
```

| Option | Effect |
|---|---|
| (none) | Every local check; essentials and anything not ok are listed |
| `-v` | Every row, with what it unlocks and where the value came from |
| `--json` | The `Check` list as JSON, same no-value rules |
| `--live` | Also the free [live checks](#live-checks); prints what it is about to contact first |
| `--group NAME` | One group |

Exit codes: **0** every essential ok; **1** an essential missing or broken (with `--live`, also
an essential's live check failing); **2** usage error. Recommended items never change the exit
code, so `doctor` can gate scripts and the container wrapper without nagging.

`doctor` is added to `UNGUARDED_COMMANDS` and `MIGRATION_EXEMPT_COMMANDS`: it must run when the
data layout is old, the schema is pending or newer, or the config is broken, because those are
things it reports. It opens the database read-only and never migrates, writes, or backs up. It
writes one file, the [snapshot](#containers), and only with `--snapshot`.

## Containers

The console container gets only its own secrets ([018](018-containers.md#which-container-gets-which-secret)):
it cannot see the Gmail token, the Google client JSON or the USAJOBS key, which only the run
container gets. It cannot run podman, see host timers, or check SELinux labels. So:

- **Snapshot.** `jobhunter run` (host or container) starts with `doctor --snapshot`, which writes
  `<data dir>/readiness.json` (0600, atomic): each check's id, status, fixed message, `checked_at`
  and `mode`/unit. No values and no free-form text. The console shows a check it cannot run
  itself as **unknown**, plus the snapshot result: "as seen by the nightly run at 02:00". A
  snapshot older than 48 hours is labelled stale, which also says the timers are not running.
- **Visible from the console container:** everything in the data directory (preferences, resume,
  database, `extension.json`, `apply-runner.json`, backups, the deployment marker, `readiness.json`),
  `config.toml` (read-only mount), and the scorer secrets the console is given.
- **Host-only checks** (podman, Quadlet drop-ins, SELinux labels, host timers left enabled, image
  version against the host's schema) stay in `jobhunter container preflight`. On the host,
  `doctor` runs `preflight.run(ctx, dry_run=True)` when the deployment marker exists (or with
  `--containers`) and folds its lines into the Containers group, ok/WARN/FAIL mapped to
  ok/recommended-missing/broken. It never writes drop-ins or the marker; preflight still does
  that. `/setup` in the console container shows one Containers row: the snapshot result and
  "run `jobhunter container preflight` on the host".
- **Fix commands depend on the mode** (`container.is_container()`): host commands on the host,
  `jobhunter-ctr ...` or podman secret commands in containers, and drafting points at the
  host's `jobhunter apply draft` (C9).
- `jobhunter-ctr doctor` works as a one-off container like any other command.

## Console rules

- `/setup` and its POSTs sit behind the same middleware as every page: loopback `Host` on every
  request, same-origin `Origin` on writes ([017](017-assisted-apply.md#local-api-security), C1).
  `Cache-Control: no-store` like every page. With `--allow-remote` nothing changes: the page
  holds no values, and its writes are the ones `/prefs` already allows.
- Light and dark through the existing `app.css` tokens; checked at 375 px wide, where each row
  stacks (status and title, then detail, then the fix) with no horizontal scroll. Copy buttons
  have a visible fallback (a selectable `<code>` block).
- Rows link to the page that owns the fix (`/prefs#sec-resume`, `/prefs#sec-extension`, `/alerts`, `/sources`); a test
  requests every link `/setup` renders and expects 200.

## Testing

- No network: every live check takes an injected client (`httpx.MockTransport`, a mocked
  `anthropic` client, a fake `claude` on `PATH` as `cli_runner`'s tests do), and the conftest
  guard stays. A test asserts `run_checks` and `GET /setup` make no socket connection and no
  subprocess call (patch `subprocess.Popen` to raise).
- A table-driven test per inventory row: build the failing state in a temp home (no key, the
  placeholder, a 0644 `.env`, invalid YAML, two `.md` resumes, a pending schema with the marker,
  a `RefreshError` source status, an 8-day-old `gmail-auth.json`) and assert status, level and
  that the fix text names the right command.
- **No value leaks:** set every secret and contact field to a sentinel string; assert it never
  appears in the page HTML, `doctor` output, `--json`, `readiness.json` or captured logs.
- The motivating case end to end: default config, no `ANTHROPIC_API_KEY`, a valid profile:
  `doctor` exits 1 naming the screen scorer, the banner shows on `/` and `/inbox`, and the run
  summary says why scoring was skipped (S7).
- Container mode (`JOBHUNTER_CONTAINER=1`): Gmail rows are unknown without a snapshot and show
  the snapshot's status with one; fix commands are the container ones.
- Banner: dismissing a recommended item hides it; changing its status brings it back; essentials
  have no Hide control. The first-run redirect happens with zero jobs and stops after one.
- Browser check before merge, against a copy of your database (CLAUDE.md): light, dark, 375 px.

## Proposed tickets

Not filed yet; the PM files them after you approve. Labels `area:console` or `area:ops`;
worker and reviewer per the CLAUDE.md table (reviewer always stronger).

| # | Ticket | Difficulty | Worker / reviewer | Depends on |
|---|---|---|---|---|
| S0 | Run loads the profile with `load_profile_for`, so the YAML `resume_path` wins as in the console ([above](#resume-resolution-differs-between-run-and-console)). Note in the PR: if your YAML names a different resume than the data-dir one, `scoring_version` changes, and existing jobs only re-score if you ask | normal | sonnet / opus | |
| S1 | `readiness.py`: `Check`, `run_checks`, every local check in the inventory, `scorers.credential_for`, the no-value and no-network tests | normal | sonnet / opus | S0 for the resume row |
| S2 | `jobhunter doctor`: output, `-v`, `--json`, `--group`, exit codes, guard exemptions, read-only DB, host preflight dry-run folded in, `--snapshot` | normal | sonnet / opus | S1 |
| S3 | `/setup` page, inline fixes reusing `/prefs` routes, banner on `/` and `/inbox` replacing the profile banner, dismissals, first-run redirect, `init` and `console` hints; dark mode and 375 px | normal | sonnet / opus | S1 |
| S4 | Snapshot: `run` writes `readiness.json` at start; console merges it for checks it cannot run (unknown, stale) | normal | sonnet / opus | S2, S3 |
| S5 | `mail auth` records `gmail-auth.json` (issued_at, storage kind); token-age check; `RefreshError` mapped to "expired or revoked" | normal | sonnet / opus | S1 |
| S6 | Live checks: Anthropic models list, OpenRouter key info, local `/health`, Gmail labels, `claude auth status`; row buttons and `doctor --live`; tests prove none calls a model | hard (credit paths) | opus / opus at higher effort | S2, S3 |
| S7 | Score stage checks the credential before trying (`credential_for`) and says `score: skipped: no ANTHROPIC_API_KEY for anthropic:claude-haiku-4-5 (see jobhunter doctor)`; exit code unchanged | normal | sonnet / opus | S1 |
| S8 | Docs: first-run section in `README.md` and `docs/containers.md` pointing at `/setup` and `doctor` | mechanical | haiku / sonnet | S2, S3 |

About 4-6 days of worker time. S0, S5 and S1 can start together; S3 and S2 in parallel after S1.

**Later, not proposed now:** preferences in the database instead of `preferences.yaml`, if you
find you never hand-edit the YAML (it would reverse 014's file-as-source-of-truth decision).

## Open questions

1. **Is the User-Agent contact an essential?** It only affects courtesy to board admins, not
   whether anything works. **Default: yes, essential**, because every request otherwise carries
   a placeholder address, and 008 says we identify ourselves honestly.
2. **Should a run with no usable scorer fail** (non-zero exit, so the `OnFailure=` notification
   fires)? **Default: no.** Ingest still did useful work; the banner, `doctor` and the S7 summary
   line carry the message. Say if you want the notification.
3. **First-run redirect from `/` to `/setup`** while essentials are missing and there are no
   jobs. **Default: yes**, stopping as soon as one job exists.
