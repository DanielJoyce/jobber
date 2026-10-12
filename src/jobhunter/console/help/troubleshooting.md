# Troubleshooting

Two places tell you what is wrong before you read this page. In the console, `/setup` lists
every check with its fix. In a terminal, `jobhunter doctor` prints the same rows (add `-v` for
what each one unlocks). Each entry below names the message you see, what it means and the fix,
and ends with the same two pointers. None of the fixes spends API credits.

## Unknown key in config.toml

Loading fails with `Extra inputs are not permitted` and a section and key name. `config.toml`
rejects keys it does not know, so a misspelt or retired setting is an error rather than silently
ignored. Fix the key (or remove it) in `config.toml`; `jobhunter paths` shows where the file is.
Then restart the console. See [Settings and secrets](settings-and-secrets.md#configtoml). Check
with `/setup` or `jobhunter doctor`.

## Your data is still in the old folder

`error: your data is still in FOLDER; run jobhunter migrate-paths`. Your database and files
were made by an older version that kept them elsewhere. Move them once:

```
jobhunter migrate-paths
```

Or point `[paths]` in `config.toml` at the old folder. Check with `/setup` or `jobhunter doctor`.

## Migrations pending or database newer than this jobhunter

Two messages, one cause: the code and the database disagree about the schema.

- "database schema is behind this jobhunter": a container deployment never migrates on its own.
  Run `jobhunter upgrade` (it backs up first).
- "database is newer than this jobhunter": a newer version wrote the database. Update this
  checkout or image to match, or restore a backup made by this version.

The console answers 503 with the same text while this is true; see [The console
answers 503](#the-console-answers-503). In containers, host and image must be the same version
([Containers](containers.md#upgrade-and-rollback)). Check with `/setup` or `jobhunter doctor`.

## Score: submit failed (Could not resolve authentication method)

The Anthropic SDK found no credential. Set `ANTHROPIC_API_KEY` (or `ANTHROPIC_AUTH_TOKEN`) as
described in [Settings and secrets](settings-and-secrets.md#the-keys), restart the console, and
check with `jobhunter secrets status`. Survivors stay queued and are scored on the next run.
Check with `/setup` or `jobhunter doctor`.

## Score: skipped, no credential for the scorer

The score stage prints `score: skipped: no credential for SCORER (set NAME); survivors
stay queued`. It means the scorer you chose needs a key and none is set. Fetching, email and
the other stages ran normally, and the exit code is unchanged. Set the named variable (step by
step in [Settings and secrets](settings-and-secrets.md#where-to-keep-secrets-on-the-host)), then
the next run scores what waited. `jobhunter secrets status` shows which names are set, without
values. Check with `/setup` or `jobhunter doctor`.

## A malformed scorer

`score: skipped (REASON); survivors stay queued`. A scorer is written
`<provider>:<model>`; a bare name, an unknown provider, or `deep_scorer` that is not
`anthropic:` is refused before anything is sent. Only scoring is skipped: fetch and mail go on.
Fix `[scoring] screen_scorer` or `[scoring] deep_scorer` in `config.toml`; the accepted forms
are in [Choosing a scorer](scorers.md). Check with `/setup` or `jobhunter doctor`.

## A model use is blocked by the saved choice

A choice saved on the Models page or with `jobhunter models set` is not a value jobhunter can
use, so that use is blocked. It does not fall back to `config.toml` or the default, because that
could send jobs to a provider you switched away from. The message depends on where the use ran:

```text
score: skipped (models.toml screen_scorer is invalid ... (see /models or jobhunter models show)); survivors stay queued
scorer error: models.toml screen_scorer is invalid ... (see /models or jobhunter models show)
models.toml screen_scorer is invalid ... (see /models or jobhunter models show); the re-score stays queued
```

The first line is the run stage (and Score now). The second is `jobhunter score`, which stops
with exit code 2. The third is a queued re-score, which is left queued. Jobs and re-scores stay
queued in every case. The same wording with `deep_scorer` means the deep pass is blocked.

Run `jobhunter models show`. The line for the blocked use gives the problem. Then either pick a
valid model with `jobhunter models set USE VALUE`, or remove the saved choice with `jobhunter
models reset USE`. The use runs again on the next run or the next click. A blocked
drafting or fact-check choice does not stop drafting: the `config.toml` value or the default
applies, with a notice on the packet page. See [Choosing models](models.md#what-fail-closed-means).

## The models file is broken

`models.toml is not valid TOML` means the file in your data folder cannot be read at all. Both
screening and the deep pass are blocked, because what the file set cannot be known (see the
entry above). The Models page shows a Reset file button for this case. Run `jobhunter models
show` to see which file and which keys. To keep the file and start over, run `jobhunter models
reset-file`: it replaces the file with an empty one and keeps the old file beside it, named
`models.toml.broken-<time>`. Then choose the models again. If the file is valid TOML but holds a
value that is not allowed, `jobhunter models reset USE` removes just that key. Use `jobhunter
models reset-file --force` only to empty a file that is valid TOML. See [Choosing
models](models.md#where-choices-are-saved).

## Spend cap reached

`score: daily or weekly spend cap reached`. The scorer stopped because `[scoring] daily_cap_usd`
or `[scoring] weekly_cap_usd` was hit; new jobs stay queued. The next day or week resumes by
itself. To score more now, raise the cap in `config.toml` and restart the console. See
[Costs and credits](costs-and-credits.md). Check with `/setup` or `jobhunter doctor`.

## USAJOBS credentials missing

`UsajobsAuthMissing`, shown as "USAJOBS credentials missing". With no USAJOBS credentials at
all, the source is skipped; this message means one of the two is set but not both. Set
`USAJOBS_API_KEY` and `USAJOBS_EMAIL`
([Settings and secrets](settings-and-secrets.md#the-keys)). A key USAJOBS rejects fails the run
instead. Check with `/setup` or `jobhunter doctor`.

## Gmail token expired or revoked

A `RefreshError` from Google, shown as "Gmail token expired or revoked". Google expires the
token after 7 days while your app is in Testing, or you revoked it. Authorise again:

```
jobhunter mail auth
```

For containers, `jobhunter mail auth --podman-secret jobhunter_gmail_token` replaces the secret.
See [Email alerts](email-alerts.md#step-3-gmail-token). Check with `/setup` or `jobhunter doctor`.

## Google client secrets file missing

"Google client secrets file not found: PATH". The OAuth client JSON from your Google Cloud
project is not where jobhunter looks. Put it there, or point `JOBHUNTER_GOOGLE_CLIENT_SECRETS`
or `[mail] client_secrets_path` at it; in containers it is the podman secret
`jobhunter_google_client`. The steps to create it are in
[Email alerts](email-alerts.md#step-1-google-client-json). Check with `/setup` or
`jobhunter doctor`.

## PDF export is unavailable

`PdfUnavailable`: PDF export needs the browser extra. Run `uv sync --extra browser` and
`uv run playwright install chromium`, then restart the console. Meanwhile open the HTML file and
print it to PDF from your browser. Check with `/setup` or `jobhunter doctor`.

## Drafting: the claude command is not installed

The `cli` drafting runner runs the `claude` command and cannot find it. Install Claude Code on
the host and make sure `claude` is on the PATH of the console's environment, or switch
`[apply] runner` to `api` (that one uses API credits). Drafting runs on the host, not in
containers. See [Drafting](drafting.md). Check with `/setup` or `jobhunter doctor`.

## Drafting: not a claude.ai subscription login

`claude auth status does not show a claude.ai subscription login`. The runner only accepts a
claude.ai subscription login, so an API key, Bedrock or Vertex login turns it off rather than
risk billing an API key. Run `claude`, log in with your subscription, then press "Turn back on"
next to the runner message in the console. See [Drafting](drafting.md). Check with `/setup` or
`jobhunter doctor`.

## 403: host is not a loopback address

The console refuses any request whose Host header is not `localhost` or a loopback address; this
stops a web page from reaching it. Open it as `http://127.0.0.1:8808/` or
`http://localhost:8808/`. An alias, `0.0.0.0` or `foo.localhost` is refused. Only if you really
mean to expose it (there is no login) start it with `jobhunter console --allow-remote`. Check with `/setup` or
`jobhunter doctor`.

## The extension says 401: pair again

"not paired, or the pairing was revoked: pair again in options". The browser extension's
pairing no longer matches the console (it was unpaired, or the data folder changed). Open the
extension's options page and pair again; `jobhunter ext status` shows the current state. See
[Browser extension](browser-extension.md). Check with `/setup` or `jobhunter doctor`.

## The console answers 503

"jobhunter console stopped: ..." with the reason after it. The database is not usable by this
version, usually a schema mismatch (see the migration entry above). The help pages, including
this one, keep working. Fix the cause named in the message, then restart the console. Check with
`jobhunter doctor`, which reads the same state in a terminal.

## config.toml has errors (the console keeps running)

The bar under the nav says "config.toml has errors; the console keeps the settings from
09:12", with up to three lines such as `scoring.daily_cap_usd: Input should be a valid number`
or `config.toml line 14, column 9: not valid TOML`. The file you saved does not load, so the
console carries on with the settings it had. Nothing is lost.

Fix the line it names and save again; the message goes away at the next check. **Details** in
the console lists every error, and `/setup` in the console shows the settings file row as
broken until then. In the terminal, `jobhunter doctor` checks the same file. An error never
shows the value you typed, so a key pasted into the wrong place is not echoed.

```
jobhunter doctor
```

## Settings still need a restart

After **Apply now**, the bar may say "2 settings still need a restart". Those settings (paths,
the console's host and port, `fetch.global_concurrency`, the `api_key_env` names, and any
`.env` change) are only read when the console starts. Press **Restart console** in the bar.
Where the console cannot restart itself (a container unit without the restart lines, or a
browser on another machine), restart it by hand: `Ctrl-C` and `jobhunter console` on the host,
or on a container host:

```
systemctl --user restart jobhunter-console
```

If Restart says work is running, wait for the work it names (a re-score, deep pass, Score now,
draft or live check) and press it again. If it says the console would not start, fix the lines
it shows and save again.

[Settings and secrets](settings-and-secrets.md#config-changes-while-the-console-runs) has the
full table. `/setup` in the console and `jobhunter doctor` show what the restarted console will
read.

## config.toml may have been replaced (containers)

In containers, `config.toml` is mounted as a single file. An editor that saves by writing a new
file and renaming it (many do, and Emacs or vim with backups always do) leaves the running
console looking at the old file. The bar then says "config.toml may have been replaced on the
host; this console cannot see the new file until it restarts".

Check the new file first, in a fresh container that sees it, then restart the console:

```
jobhunter-ctr doctor
systemctl --user restart jobhunter-console
```

`/setup` in the console shows the result after the restart.

## No Apply button

**Apply now** appears only when a setting that reloads in place changed, and only in a browser
on the machine running the console (a browser elsewhere on your network gets the restart
command instead). A change to `console.host` or `console.port` has no effect when the console
was started with `--host` or `--port`. See `/setup` in the console, or run `jobhunter doctor`.
