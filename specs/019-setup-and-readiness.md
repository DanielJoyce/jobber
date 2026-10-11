# 019 — First-run setup and readiness

Status: **draft for review, revision 2** (bug `ba4d25f`). Written 2026-10-11; revised the same
day after an adversarial review and your answers ([History](#history)).

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

- **Silent.** `scoring.screen_scorer` defaults to `anthropic:claude-haiku-4-5`. With no
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

### Exit codes of the run

The two rules sit side by side and stay as they are: a failed **source** makes `jobhunter run`
exit 1 (existing behaviour, [004](004-ingest-pipeline.md)), a skipped or failed **score stage**
never does (your decision above). What changes is that both are explained: `/setup`, the banner
and `doctor` name the cause ("nightly run failing: USAJOBS credentials missing"; "new jobs are
not being scored: no Anthropic credential"). Ticket S10 makes an **unconfigured** source (no
USAJOBS credentials at all) a skip, the way the email source already skips without a Gmail
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
| 17 | Deep pass: `scoring.deep_scorer` (`anthropic:claude-opus-5`) | `score --deep`, `score --collect-pending` | R with an Anthropic screen scorer, else O | Stage 3 shortlist | `score --deep` errors when run |
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
   explicit action: a live-check click, `doctor --live`, or `doctor --containers`.
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
   ([018](018-containers.md) D3), which the restart also fixes.

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

Each check costs a `stat`, an environment lookup or one small indexed query. `run_checks` must
finish in under 50 ms on your real data. `cheap_only=True` (the banner) runs the essentials plus
the recommended rows marked cheap: everything in the table except the alert-subscription counts
and the backup listing.

### Scorer credentials

One new function in `scoring/scorers.py`, `credential_for(spec, scoring) -> Credential`, used by
the checks and by the run's score stage (S7), so the two cannot disagree. Its answer is
`present`, `missing` or `unknown` plus the variable names involved:

| Provider | Present when | Notes |
|---|---|---|
| `anthropic:` (and bare names) | `ANTHROPIC_API_KEY` or `ANTHROPIC_AUTH_TOKEN` set; or `ANTHROPIC_PROFILE` or the workload-identity variables (`ANTHROPIC_IDENTITY_TOKEN[_FILE]` with the federation ids) set; or an active profile file exists in the SDK's config directory (a `stat`, never read) | What `anthropic` 1.13 resolves, in its order. Anything only the SDK can judge is `present` with the note "via SDK profile, not checked locally". `missing` only when none exists. `ANTHROPIC_AUTH_TOKEN` is not a `*_FILE` name today (S1 adds it to `secrets.DEFAULT_NAMES`) |
| `openrouter:`, `jev:`, `decisions:` | the variable named by `scoring.openrouter.api_key_env` | `missing` otherwise |
| `openai-compat:` | the variable named by `scoring.openai_compat.api_key_env`, or that name set empty | Server reachability is a live check |
| `local:` | always `present` for the key (optional); the server is a live check | `unknown` until checked live |

S7 skips the score stage only on `missing`, with `score: skipped: no Anthropic credential for
anthropic:claude-haiku-4-5 (see jobhunter doctor)`; `present` and `unknown` try as today.

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

- **Inputs.** `jobhunter run` writes `<data dir>/readiness.json` (0600, atomic) **after it takes
  the run lock**, so a run that times out on the lock writes nothing. It records each check's id,
  effective status, fixed message, `checked_at`, the mode, and the writer: `timer` when started
  by systemd (`INVOCATION_ID` set), otherwise `manual`. No values, no free-form text.
- **Outcome.** At the end the run adds the score stage's result as a fixed code (`scored`,
  `skipped_no_credential`, `cap_reached`, `failed:<exception type>`, `no_profile`) and each
  failed source's mapped reason. The `run` table stores only counts, so this is how the console
  learns that the last run did not score.
- **On demand.** `jobhunter doctor --snapshot` writes the inputs part; `jobhunter-ctr doctor
  --snapshot` does it inside a one-off container, which sees every secret the wrapper gets, to
  refresh `unknown` rows without waiting for the night.
- **Host section.** On the host, `jobhunter doctor --containers --snapshot` adds a `host`
  section with preflight's [mapped results](#jobhunter-doctor), which the console container shows
  on its Containers row with the time. Without it the row says "run `jobhunter doctor
  --containers` on the host".

**Effective status.** A snapshot is fresh when written within 36 hours. For a row the console
cannot check, the effective status is the fresh snapshot's, else `unknown`. For a row both can
check (scorer, caps, preferences, resume and the rest), the effective status is **the worse of
the two**, and a disagreement is shown: "the console sees ANTHROPIC_API_KEY; the nightly run at
02:00 did not". A fresh outcome of `skipped_no_credential` or `no_profile` makes the scorer or
profile row broken whatever the console sees. `essentials_ok`, the banner and dismissals all use
the effective status.

**Timers not running** is judged from the `run` table (no run with `started_at` in the last 36
hours), not from the snapshot's age, since a manual run also writes one.

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
              anthropic:claude-haiku-4-5 is used, and no Anthropic credential is set.
              New jobs are not being scored (last run 02:00: skipped, no credential).
              Fix, either:
                · add ANTHROPIC_API_KEY to ~/.env, then restart the console
                · or pick another scorer in config.toml, e.g.  [scoring] screen_scorer = "jev:typesafe/jev-1.13"
              Read when the console started at 09:12.

  ✓ ok        Spend caps                          $2.00/day, $10.00/week; $0.41 spent today
```

### Writes from the page

`/setup` adds four POST routes and reuses one. All go through the existing middleware (loopback
`Host` and same-origin `Origin` on writes, [017](017-assisted-apply.md#local-api-security), C1).
None takes a secret.

| Route | Does | Notes |
|---|---|---|
| `POST /setup/starter-prefs` | Writes `SKELETON` (`prefs_graceful.py`) through `write_preferences_data` (atomic, 0600), then 303 to `/prefs?created=1` | Refuses with 409 if the file exists. Not `/prefs/save`: that route builds the file from form fields, and a bare POST gets weights of 0 (422) and loses the skeleton's exclusions. Test: the written YAML loads to the same data as `SKELETON` |
| `POST /setup/resume` | The `/prefs` upload's checks (`check_upload`: `.md`/`.txt`, 1 MB, UTF-8) and write helper, writing a path relative to the profile folder; answers with the refreshed resume row (htmx) or 303 to `/setup` | Not `/prefs/resume`, which renders the whole prefs page |
| `POST /setup/dismiss` | Body: one or more `check_id`, each validated against the fixed id list, recommended or due rows only; writes `<data dir>/setup-dismissed.json` atomically at 0600 (id plus the effective status dismissed at) | 422 for an essential or unknown id |
| `POST /setup/live/{check_id}` | One [live check](#live-checks); `check_id` from the fixed list | Requires a loopback `Host` **even with `--allow-remote`**, like `/ext/`; the buttons are hidden under `--allow-remote` |
| `POST /prefs/ext/pair` (reused) | The one-time pairing code panel; it already answers with the `_ext_pair.html` fragment | |

Everything else is a link (`/prefs#sec-pay` and the other sections, `/alerts`, `/sources`) or
the exact command with a copy button and a selectable `<code>` fallback.

**No secret is ever typed into the console.** Why: the console has no authentication, a form
puts the key in a request body and browser history, and in containers secrets belong to podman.
The page shows where each key goes, per mode:

- **host**: create the file private first, then edit it:
  `[ -e ~/.env ] || install -m 600 /dev/null ~/.env`, then add `ANTHROPIC_API_KEY=...` with an
  editor, then restart the console.
- **container / host managing containers**: the form in `docs/containers.md`, which keeps the
  key off the command line and out of history, then the full [018](018-containers.md#mechanism)
  sequence:

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
| 1 | Google client JSON (12) | the file exists (container: the run snapshot says so) | The ten-minute Cloud console procedure from [012](012-email-ingest.md#setting-up-gmail), as a docs how-to (`docs/gmail.md`, written in S8) linked from the row, with the file's target path for the mode (`podman secret create jobhunter_google_client <file>` for containers) |
| 2 | Alerts address (11) | not the placeholder | The `[mail] alerts_address` line, your `+jobs` address, and the restart note |
| 3 | Gmail token (13) | [Gmail token](#gmail-token) rules | `jobhunter mail auth`, or `--podman-secret jobhunter_gmail_token` |
| 4 | Label and filter (14) | an alert arrived under the label in the last 14 days (`mail_message`); otherwise `unknown` until the live check | `jobhunter mail setup` (or `jobhunter-ctr mail setup`); it prints filter XML for manual import if you skip the settings scope |
| 5 | Board subscriptions (15) | `/alerts` counts: subscribed, receiving, stale | Link to `/alerts`, the existing checklist, which gives each board's sign-up link, criteria and the address to use |
| 6 | LinkedIn job alerts (16) | alerts from LinkedIn's sender arrived under the label | Create alerts in LinkedIn (Jobs, search, "Set alert"). LinkedIn mails the address on your account: add your `+jobs` address there if LinkedIn accepts it; if not, record it on `/alerts` as rejecting `+` and add LinkedIn's alert sender to `mail.fallback_sender_domains`, which files it under the label. Until bug `430a0e7` lands these are kept but yield no jobs; the row says how many are waiting |
| 7 | Craigslist saved searches (16), optional | as 6 | A Craigslist account, a saved search with email alerts, the same address rule. Parser pending (bug `b0ac328`); low priority |

Steps 6 and 7 also become rows on `/alerts` (S9), since they are not registry sources and the
checklist does not list them today. jobhunter never fetches linkedin.com or craigslist.org
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
Gmail button is replaced by `jobhunter-ctr mail status`.

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
fixed ids (`podman`, `bind_sources`, `labels`, `host_timers`, `db_paths`, `version_skew`,
`env_files`, `base_units`, `quadlet`) with ok/warn/fail and fixed messages. With the marker
present, a FAIL is **essential** (the units will not start) and a WARN recommended; without it,
both are recommended. Without `--containers`, host-managing mode shows the Containers row as
`unknown` unless a fresh host section exists.

## Console rules

- `/setup` and its routes sit behind the existing middleware: loopback `Host` on every request
  and same-origin `Origin` on writes unless `--allow-remote`. Under `--allow-remote` a LAN
  client can read `/setup` (no values, but which keys are set) and post the three non-live
  writes, exactly as it can already edit `/prefs`; the live-check route keeps the loopback-only
  door, since it uses your credentials. `Cache-Control: no-store` like every page.
- Light and dark through the existing `app.css` tokens; checked at 375 px wide, where each row
  stacks (status and title, then detail, then the fix) with no horizontal scroll.
- A test requests every link `/setup` renders and expects 200.

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
- Banner: Hide dismisses a recommended row; a status change brings it back; essentials have no
  Hide and `/setup/dismiss` refuses them.
- Browser check before merge, against a copy of your database (CLAUDE.md): light, dark, 375 px.

## Proposed tickets

Not filed yet; the PM files them after you approve. Labels `area:console` or `area:ops`;
worker and reviewer per the CLAUDE.md table (the reviewer is always stronger).

| # | Ticket | Difficulty | Worker / reviewer | Depends on |
|---|---|---|---|---|
| S0 | Every profile load through `load_profile_for` (runner, `_score_mode`, `eval`, `llm bench`, backfill); test that only `scoring/profile.py` passes `paths.resume_path`. PR note: if the YAML names a different resume, `scoring_version` changes for the run, `--rescore-pending` and `--deep`; existing jobs re-score only when you ask | normal | sonnet / opus | |
| S1 | `readiness.py`: `Check`, modes, every local check, `credential_for`, `ANTHROPIC_AUTH_TOKEN` as a known secret, `_absolute_path_count` moved to `core/dbpaths.py`, the keyring-raises fixture, no-leak and no-subprocess tests. Resume row compares both resolutions until S0 | normal | sonnet / opus | |
| S2 | `jobhunter doctor`: output, `-v`, `--json`, `--group`, exit codes, guard exemptions, read-only DB, `--containers` with `build_preflight_context()` and fixed ids; console refusal messages point at `doctor` | normal | sonnet / opus | S1 |
| S3a | `/setup` page and its routes (`starter-prefs`, `resume`, `dismiss`), the email-alerts guide, mode-aware fixes and host paths, restart notes; dark mode and 375 px | normal | sonnet / opus | S1 |
| S3b | Banner on `/` and `/inbox` replacing the profile banner, Hide, first-run redirect (and the `GET /` test updates), `init` and `console` hints | normal | sonnet / opus | S3a |
| S4 | Snapshot: `doctor --snapshot`, `run` writes inputs after the lock and the outcome at the end, host section from `--containers`; console effective status (worse of two, unknown rules), disagreement text | hard (two processes, freshness rules) | opus / opus at higher effort | S2, S3a |
| S5 | `mail auth` writes `gmail-auth.json` (issued_at, storage kind; `--from-local` keeps the old date); the token-age rules and `RefreshError` mapping in the Gmail row | normal | sonnet / opus | S1 |
| S6 | Live checks: route with the loopback-only door, buttons, `doctor --live`; booleans only; uncached `auth_status()`; OpenRouter only on `openrouter.ai`; tests prove no model call and no leak | hard (credit paths, credentials) | opus / opus at higher effort | S2, S3a |
| S7 | Score stage asks `credential_for` first and prints the skip line on `missing`; exit code unchanged | normal | sonnet / opus | S1 |
| S8 | Docs: `docs/gmail.md` from 012's Gmail setup; first-run section in `README.md` and `docs/containers.md` pointing at `/setup` and `doctor` | mechanical | haiku / sonnet | S2, S3a |
| S9 | `/alerts` rows for LinkedIn and Craigslist alerts (sign-up steps, address rule, "parser pending" with the count waiting) | normal | sonnet / opus | |
| S10 | USAJOBS with no credentials configured is a skip (`unavailable()`, like the mail source), not a failure; a rejected key still fails | normal | sonnet / opus | |

Order: S0, S1, S9 and S10 can start together; then S2, S3a, S5 and S7 after S1; S3b after S3a;
S4 and S6 last. About 6-8 days of worker time.

**Later, not proposed now:** preferences in the database instead of `preferences.yaml`, if you
find you never hand-edit the YAML (it would reverse 014's file-as-source-of-truth decision).

## Open questions

1. **S10, missing USAJOBS credentials as a skip.** Today a fresh install pages you every night
   for a source you have not set up. **Default: yes, skip when unconfigured**; `/setup` and the
   banner's recommended line still show it.

## History

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
