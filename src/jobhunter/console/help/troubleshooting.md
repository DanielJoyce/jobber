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
