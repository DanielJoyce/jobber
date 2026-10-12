# Settings and secrets

jobhunter keeps two kinds of configuration apart. **Settings** (where files live, caps, which
scorer) are in `config.toml`. **Secrets** (API keys) are never in `config.toml`: they come from
the environment, a `.env` file, or, in containers, podman secrets. Nothing on this page shows a
value, and none of the commands it names prints one.

## config.toml

`jobhunter paths` prints where `config.toml` is, where each folder is, and where each value came
from. The file is never edited from the console. A running console notices when you save it
and offers to apply the change, or says it needs a restart (see
[below](#config-changes-while-the-console-runs)). (`/prefs` edits `preferences.yaml`, which is a different
file and applies at once.) An unknown key is an error, not ignored: see
[Troubleshooting](troubleshooting.md#unknown-key-in-configtoml).

```
jobhunter paths
```

## Environment variables

Every variable jobhunter reads whose name starts with `JOBHUNTER_`, `ANTHROPIC_`, `OPENROUTER_`, `OPENAI_COMPAT_`, `LLAMA_`, `USAJOBS_`, `CLAUDE_` or `XDG_` is in this table (the usual `PATH` and proxy variables are not). It is generated from the code, and a test fails
if the code reads such a variable that is missing here. A variable from the environment is read once,
at start, so changing one needs a restart of the console (and applies to the next scheduled run
on its own). A secret `NAME` can instead be given as `NAME_FILE`, the path of a file holding the
value; containers use that form.

<!-- jh:begin env-vars -->

| Name | Read by | On the host | In containers | Needs restart |
|---|---|---|---|---|
| `ANTHROPIC_API_KEY` (secret; or `ANTHROPIC_API_KEY_FILE`) | the Anthropic scorers and the `api` drafting runner | `~/.env` or the environment | podman secret `jobhunter_anthropic` as `ANTHROPIC_API_KEY_FILE` | yes |
| `ANTHROPIC_AUTH_TOKEN` (secret; or `ANTHROPIC_AUTH_TOKEN_FILE`) | the Anthropic SDK's bearer-token alternative to the key | `~/.env` or the environment | not mounted | yes |
| `OPENROUTER_API_KEY` (secret; or `OPENROUTER_API_KEY_FILE`) | `openrouter:` and `jev:` scorers | `~/.env` or the environment | podman secret `jobhunter_openrouter` as `OPENROUTER_API_KEY_FILE` | yes |
| `OPENAI_COMPAT_API_KEY` (secret; or `OPENAI_COMPAT_API_KEY_FILE`) | `openai_compat:` scorers | `~/.env` or the environment | podman secret `jobhunter_openai_compat` as `OPENAI_COMPAT_API_KEY_FILE` | yes |
| `LLAMA_API_KEY` (secret; or `LLAMA_API_KEY_FILE`) | `local:` scorers (usually unset) | `~/.env` or the environment | podman secret `jobhunter_llama` as `LLAMA_API_KEY_FILE` | yes |
| `USAJOBS_API_KEY` (secret; or `USAJOBS_API_KEY_FILE`) | the USAJOBS source | `~/.env` or the environment | podman secret `jobhunter_usajobs_key` as `USAJOBS_API_KEY_FILE` | yes |
| `USAJOBS_EMAIL` (secret; or `USAJOBS_EMAIL_FILE`) | the USAJOBS source (the email you registered the key with) | `~/.env` or the environment | podman secret `jobhunter_usajobs_email` as `USAJOBS_EMAIL_FILE` | yes |
| `ANTHROPIC_PROFILE`, `ANTHROPIC_CONFIG_DIR`, `ANTHROPIC_FEDERATION_RULE_ID`, `ANTHROPIC_ORGANIZATION_ID`, `ANTHROPIC_IDENTITY_TOKEN`, `ANTHROPIC_IDENTITY_TOKEN_FILE` | the Anthropic SDK's other sign-in routes; jobhunter only checks that they are set | the environment | not mounted | yes |
| `JOBHUNTER_ENV_FILE` | which `.env` is read first | your choice | not used (secrets are podman secrets) | yes |
| `JOBHUNTER_CONFIG` | which `config.toml` is read | your choice | not used (the config is `/config/config.toml`) | yes |
| `JOBHUNTER_DATA_DIR` | overrides `[paths] data_dir` | optional | set by the units, do not set | yes |
| `JOBHUNTER_CACHE_DIR` | overrides `[paths] cache_dir` | optional | set by the units, do not set | yes |
| `JOBHUNTER_DB_PATH` | overrides `[paths] db_path` | optional | set by the units, do not set | yes |
| `JOBHUNTER_PROFILE_DIR` | overrides `[paths] profile_dir` | optional | set by the units, do not set | yes |
| `JOBHUNTER_RESUME_PATH` | overrides `[paths] resume_path` | optional | set by the units, do not set | yes |
| `JOBHUNTER_GOOGLE_CLIENT_SECRETS` | the Google OAuth client JSON (overrides `[mail] client_secrets_path`) | optional | podman secret `jobhunter_google_client` | yes |
| `JOBHUNTER_GMAIL_TOKEN_FILE` | the Gmail token, read from a file | not used (OS keyring, else a 0600 file) | podman secret `jobhunter_gmail_token` | yes |
| `XDG_CONFIG_HOME` | base of `~/.config/jobhunter` | optional | ignored | yes |
| `XDG_DATA_HOME` | base of `~/.local/share/jobhunter` | optional | ignored | yes |
| `XDG_CACHE_HOME` | base of `~/.cache/jobhunter` | optional | ignored | yes |
| `XDG_RUNTIME_DIR` | podman's socket directory, read by `container preflight` | set by your login session | not used | no |
| `CLAUDE_CONFIG_DIR` | where the Claude Code login lives; passed to the drafting runner | optional | not used (drafting runs on the host) | yes |
| `CLAUDE_CODE_OAUTH_TOKEN` | a subscription token from `claude setup-token`; passed to the drafting runner. A secret with no `_FILE` form | `~/.env` | not used | yes |
| `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` | set by jobhunter on the Claude Code child: do not set | - | - | - |
| `CLAUDE_CODE_DISABLE_ATTACHMENTS` | set by jobhunter on the Claude Code child: do not set | - | - | - |
| `CLAUDE_CODE_DISABLE_CLAUDE_MDS` | set by jobhunter on the Claude Code child: do not set | - | - | - |
| `CLAUDE_CODE_DISABLE_AUTO_MEMORY` | set by jobhunter on the Claude Code child: do not set | - | - | - |
| `ANTHROPIC_BASE_URL` | never passed to the Claude Code child | - | - | - |
| `CLAUDE_CODE_USE_BEDROCK` | never passed to the Claude Code child | - | - | - |
| `CLAUDE_CODE_USE_VERTEX` | never passed to the Claude Code child | - | - | - |
| `JOBHUNTER_CONTAINER` | container mode switch | - | set by the image, do not set | - |
| `JOBHUNTER_TRIGGER` | marks timer-started runs (`timer`); anything else is a manual run | set by the run and collect units, do not set | set by the run, collect and rescore units (and preflight's trigger drop-in), do not set | no |
| `JOBHUNTER_RESTART_EXIT` | the console's Restart exits 42 so systemd starts it again | - | set by the console unit (and preflight's restart drop-in), do not set | - |
| `JOBHUNTER_PUBLISHED_LOOPBACK_ONLY` | the console's published-port check | - | set by you only for the 0.0.0.0 fallback (see Containers, Troubleshooting) | - |
| `JOBHUNTER_BUILD_COMMIT` | the commit `jobhunter version` shows | - | set by the image, do not set | - |

<!-- jh:end env-vars -->

## Where to keep secrets on the host

Put them in a private `.env` file. The lookup order is:

1. the file named by `$JOBHUNTER_ENV_FILE`, if set;
2. `./.env` in the directory the console or command runs from;
3. `~/.env`.

A real environment variable beats every file, and an earlier file beats a later one. Create the
file so only you can read it, then add one `NAME=value` per line:

```
install -m 600 /dev/null ~/.env
```

A line that is only a bare value, with no `NAME=`, is not read as a key. Never commit the file:
`.env` is gitignored, and the commit hooks (gitleaks and a private-key check) refuse anything
that looks like a secret. Never paste a key on a command line, where shell history and `ps`
would keep it. `jobhunter secrets status` warns when a `.env` file is readable by others.

## Where to keep secrets in containers

Containers use podman secrets, one per key, mounted as files and pointed at by the `NAME_FILE`
variable. Create each from a hidden prompt, never from the command line:

```
read -rs KEY && printf '%s' "$KEY" | podman secret create jobhunter_openrouter - && unset KEY
```

To replace one, remove it and create it again (`podman secret rm NAME`, or `podman secret create
--replace`). After any secret change run `jobhunter container preflight --yes`, then
`systemctl --user daemon-reload` and restart `jobhunter-console`. The scheduled jobs pick the
change up at their next run; a running console only after the restart. The full list of secret
names and which unit gets which is in [Containers](containers.md#secrets).

## Checking where a value comes from

None of these prints a value or its length:

```
jobhunter secrets status
jobhunter paths
jobhunter doctor
```

`secrets status` shows each name as set from the environment, from a file, from a `.env`
file (with its path), invalid (with the reason) or unset. In a container, run
`jobhunter-ctr secrets status` to see what the container sees. In the console, `/setup` shows
the same state as readiness rows.

## Which changes need a restart

| You changed | What to do |
|---|---|
| `config.toml` | Apply now in the console's notice, or restart the console for the settings that need it ([table below](#config-changes-while-the-console-runs)). Scheduled runs read the new file at their next start |
| A `.env` file, a `NAME_FILE` or a podman secret | Restart the console (**Restart console** in the bar): it reads them at start and keeps the first value |
| `preferences.yaml` or the resume | Nothing: the console reads them on use (a re-score may be offered) |
| A host timer or container unit | Reload systemd as the command that changed it says |

## The keys

- **Anthropic API key** (`ANTHROPIC_API_KEY`). Scoring with `anthropic:` models and the `api`
  drafting runner use it. Create one in the Anthropic Console; keys start with `sk-ant-`. API
  credits are prepaid and separate from a claude.ai subscription. See
  [Choosing a scorer](scorers.md). `ANTHROPIC_AUTH_TOKEN` is the SDK's alternative.
- **Claude Code login** (drafting). The `cli` drafting runner uses your claude.ai
  subscription through the `claude` command, not an API key. Install Claude Code, run `claude`
  and log in. See [Drafting](drafting.md). `CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`)
  goes in `~/.env`; it has no `_FILE` form.
- **USAJOBS key and email** (`USAJOBS_API_KEY`, `USAJOBS_EMAIL`). Request a key on the USAJOBS
  developer site, using the email you give it here. Without both, the USAJOBS source is skipped.
- **OpenRouter key** (`OPENROUTER_API_KEY`, or the name in `[scoring.openrouter] api_key_env`).
  Needed for `openrouter:` and `jev:` scorers. See [OpenRouter and Jev](openrouter.md).

If something is missing, `/setup` in the console names the row and the fix, and
`jobhunter doctor` prints the same in the terminal.

## Config changes while the console runs

The console notices when you save `config.toml` or a `.env` file. It checks when a page polls
the bar under the nav (every minute, and at once when you switch back to the browser tab). A
line appears there: "config.toml changed: 3 settings", with **Apply now**, **Details** and
**Dismiss**.

- **Apply now** swaps in the settings that reload in place (the table below). A page you open
  next uses them; a re-score that is already running keeps the settings it started with.
- Settings that need a restart stay as they were, and the line then says how many still need
  one, with **Restart console** ([below](#restart-from-the-console)). Where the console cannot
  restart itself, the line gives the command instead: `Ctrl-C` and `jobhunter console` on the
  host, or `systemctl --user restart jobhunter-console` in containers.
- **Details** in the console lists each changed setting and whether it reloads. Values are
  shown only for caps, scorers, models, rates, timeouts, ports and switches. Anything personal
  (your contact, alerts address, sender lists), URLs and paths show only "changed".
- A scorer's endpoint (`base_url`, and `runtime` for local models) always needs a restart: the
  API key is read at start, so a new endpoint must come with the key read then, never the old
  one. While its `api_key_env` or a `.env` file has a change waiting, the rest of that
  `[scoring.*]` table waits for the restart too.
- A change to `models.toml` (the models you pick on the console's models page, or with
  `jobhunter models set`) is listed under that file and applies the same way.
- If `config.toml` disappears or is emptied, the console keeps its settings and says so; it
  never offers to apply the defaults.
- A `.env` change always needs a restart: secrets are read once, at start. Details names the
  keys that changed only when they are known secret names, never their values.
- If the new file has an error, the console keeps the settings it has, and the line says
  what is wrong (the setting and the problem, never the value). Fix the file and save again.
- `--host` and `--port` given on the command line win over `config.toml`, so a change to
  `console.host` or `console.port` then has no effect.

Apply and Restart work only from a browser on the machine running the console. Other commands
(`jobhunter run`, the timers) read `config.toml` each time they start and need nothing.

### Restart from the console

**Restart console** appears in the bar and on Details when a change needs a restart. It
starts the console again with `config.toml`, `models.toml` and every `.env` file read afresh:

1. It first checks, in a separate process, what a new console would read: `config.toml`,
   `models.toml` and the `.env` files, whether `config.toml` would go missing or empty (for
   example a `.env` that points `JOBHUNTER_CONFIG` elsewhere), the database path, and a moved
   host or port. If it finds a problem it says what is wrong (the setting and the problem,
   never the value) and keeps running. The check catches common mistakes, not everything: a
   console that still fails to start says why in its terminal, and you start it again by hand.
   While `config.toml` has errors the button is hidden.
2. It refuses while a re-score, a deep pass, a Score now, a draft or a live check runs, and
   lists what is running. Press it again when that is done. Once a restart is under way, any
   of those gets "console restarting". Ctrl-C in the console's terminal during a restart stops
   it instead.
3. The page waits for the new console and goes back to where you were when it answers
   (reloading the waiting page does not restart again). If the console does not come
   back within 90 seconds, the page says so: look at the terminal it runs in (or
   `journalctl --user -u jobhunter-console`).

On the host the console restarts in place, as the same process, with the same command line.
It runs the code that is on disk now; Details says so when that differs from the code it
started with. It does not run `uv sync`, so after a `git pull` that changes dependencies run
`uv sync` first (the check in step 1 catches a missing module, not every dependency change). In containers the console exits
and systemd starts it again; the unit needs three lines that preflight adds (see
[Containers](containers.md#restart-from-the-console)). A browser on another machine
(`--allow-remote`) gets the command instead of the button.

<!-- jh:begin config-reload -->

| Table in config.toml | After you save a change |
|---|---|
| `[paths]` | Restart the console |
| `[fetch]` | Apply now (reloads in place); except `global_concurrency`: restart the console |
| `[scoring]` | Apply now (reloads in place) |
| `[scoring.openai_compat]` | Apply now (reloads in place); except `base_url`, `api_key_env`: restart the console |
| `[scoring.openrouter]` | Apply now (reloads in place); except `base_url`, `api_key_env`, `catalog_cache`: restart the console |
| `[scoring.local]` | Apply now (reloads in place); except `runtime`, `base_url`, `api_key_env`: restart the console |
| `[apply]` | Apply now (reloads in place) |
| `[mail]` | Apply now (reloads in place) |
| `[console]` | Restart the console |
| `[capture]` | Apply now (reloads in place) |
| `.env` files, `*_FILE` and `JOBHUNTER_*` variables | Restart the console |

<!-- jh:end config-reload -->
