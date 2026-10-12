# Settings and secrets

jobhunter keeps two kinds of configuration apart. **Settings** (where files live, caps, which
scorer) are in `config.toml`. **Secrets** (API keys) are never in `config.toml`: they come from
the environment, a `.env` file, or, in containers, podman secrets. Nothing on this page shows a
value, and none of the commands it names prints one.

## config.toml

`jobhunter paths` prints where `config.toml` is, where each folder is, and where each value came
from. The file is never edited from the console: it is read once when the console starts, so
after you edit it, restart the console. (`/prefs` edits `preferences.yaml`, which is a different
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
| `config.toml` | Restart the console. Scheduled runs read the new file at their next start |
| A `.env` file, a `NAME_FILE` or a podman secret | Restart the console: it reads them at start and keeps the first value |
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
