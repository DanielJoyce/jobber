# 018 — Running jobhunter in rootless podman

Status: **draft for review, revision 2** (bug `671bbf0`). Written 2026-10-10; revised the same
day after an adversarial review ([History](#history)).

You asked for jobhunter to run under rootless podman: an image, a SQLite store, compose, config
that makes sense inside a container (XDG home does not), and secrets (API keys, Google
credentials) handled securely. This spec settles those, and the [tickets](#phased-tickets) are
filed in git-bug.

Nothing here changes how jobhunter runs on the host today, except three safety fixes that help
the host too: a run lock ([C5](#phased-tickets)), a schema version guard with a backup before
migrating ([C12](#phased-tickets)), and stricter secret validation ([C2](#phased-tickets)).
Container mode is opt-in, and `uv run jobhunter ...` and `jobhunter schedule install` keep
working.

## What exists today (checked in code, 2026-10-10)

| Thing | Today | Where |
|---|---|---|
| Runtime | Python 3.13 via `uv`, `uv.lock` committed, hatchling build with `readme = "README.md"` | `pyproject.toml` |
| Console | FastAPI via uvicorn, `127.0.0.1:8808`; a non-loopback bind needs `--allow-remote`; POSTs from a browser must carry a loopback `Host` and a same-origin `Origin`; GETs are not checked | `cli.py console`, `console/app.py` `check_host`, `cross_site_reason` |
| Paths | XDG: config `~/.config/jobhunter`, data `~/.local/share/jobhunter`, cache `~/.cache/jobhunter`; overrides `JOBHUNTER_DATA_DIR`, `_CACHE_DIR`, `_DB_PATH`, `_PROFILE_DIR`, `_RESUME_PATH`, `JOBHUNTER_CONFIG` | `xdg.py`, `config.py` `PATH_ENV_VARS` |
| Absolute paths in the DB | `attachment.path`, `application.cover_letter_path`, and 017's `packet_document.rendered_path` store absolute paths | `core/migrations`, `console/tracking.py`, specs/017 |
| Legacy guard | Every command but `paths`, `migrate-paths`, `init`, `schedule` looks for an old `data/jobhunter.db` (uses `git rev-parse`) | `cli.py _load_env`, `legacy_data.py` |
| Migrations | `db.migrate()` applies any missing migration on every command and at console start; no backup first; no check for a database newer than the code | `core/db.py` |
| Secrets | Read from `os.environ` at use time; `.env` files loaded at startup from `$JOBHUNTER_ENV_FILE`, `./.env`, `~/.env` (real env wins) | `config.py load_env_files` |
| Secret names | `ANTHROPIC_API_KEY` (bare `anthropic.Anthropic()`), `OPENROUTER_API_KEY`, `OPENAI_COMPAT_API_KEY`, `LLAMA_API_KEY` (each an `api_key_env` setting), `USAJOBS_API_KEY`, `USAJOBS_EMAIL` (registry `auth.key_env`/`email_env`) | `config.py`, `sources/adapters/usajobs.py` |
| Secret leak path | httpx refuses a header value with a trailing `\n`, `\r`, space or tab and puts the **whole value** in the exception text; several places log or store `str(exc)` | `scoring/scorers.py`, `scoring/screen.py`, `scoring/decisions.py`, `core/fetch/context.py` |
| Gmail | Installed-app OAuth. Client secrets JSON at `mail.client_secrets_path` (default `~/.config/jobhunter/google_client_secret.json`) or `$JOBHUNTER_GOOGLE_CLIENT_SECRETS`. Refresh token in the OS keyring, else `~/.config/jobhunter/gmail_token.json` (0600). This host uses the file fallback | `mail/auth.py` |
| gcloud | **None.** No `gcloud` CLI, no Application Default Credentials, no service account, no `google-cloud-*` package. The only Google credentials are the OAuth client JSON and the Gmail refresh token | grep of `src/`, `pyproject.toml`, `scripts/` |
| SQLite | WAL, `busy_timeout=5000` | `core/db.py` |
| Backups | Manual, in `<data dir>/backups/`, named like `jobhunter-YYYYMMDD-HHMMSS-pre-<what>.db` | CLAUDE.md habit |
| Scheduling | `systemd --user` timers from `jobhunter schedule install`: run 02:00, collect then rescore-pending 03:30, verify Sun 04:00, `OnFailure=jobhunter-notify@`. `schedule uninstall` removes all of them, notify unit included | `ops/schedule.py`, `ops/units/` |
| Run exclusivity | **None.** Two `jobhunter run` processes can run at once | gap, [C5](#phased-tickets) |
| Playwright | Optional `browser` extra; no source is `tier: browser` today; 017 PDF export uses headless Chromium when present | `core/fetch/context.py`, specs/017 |
| Local LLM | `scripts/setup-local-llm.sh`: llama-server or Ollama on host loopback, key in `~/.env` as `LLAMA_API_KEY` | specs/016 |
| Drafting (017, being revised) | `claude -p` on the host, SDK fallback | specs/017 |

## Goals

1. One image that runs every jobhunter command, with no secrets and no personal data in it.
2. Console and the scheduled runs as rootless containers on Fedora atomic, SELinux enforcing.
3. The database and files stay readable by host tools (`sqlite3`, the host CLI).
4. Secrets reach the process as files from podman secrets: never in a compose file, an env
   file, an image layer, a unit file, `podman inspect`, or a log. Each container gets only the
   secrets it uses. (At rest, podman's default secret store is no stronger than a 0600 file;
   see [At rest](#at-rest).)
5. Everything testable without the network and without paid calls.

Non-goals: Kubernetes, multi-user, remote access to the console, running the 017 phase 2
browser agent in a container, a local LLM container in this round (deferred, C10).

## Decisions

| # | Decision | Reason |
|---|---|---|
| D1 | **Quadlet** (systemd user units generated from `.container` files) is the recommended way to run it. A `compose.yaml` for **podman-compose** is kept for ad-hoc and dev use | Quadlet ships with podman on Fedora atomic; compose needs a provider installed. Quadlet gives timers, `journalctl`, `OnFailure=`, `Persistent=true` and `podman auto-update`, matching how the host install already works (002). Compose has no timer. `podman compose` delegates to docker-compose when that is installed (it is on this host), and docker-compose refuses `external: true` secrets outside swarm, so compose here means podman-compose only |
| D2 | **No pod.** The console is one container that publishes its port; run, collect, rescore, verify and backup are separate oneshot containers with no published port and no `[Install]` section | A pod's service `Wants=` every member by default (`StartWithPod=true`), so starting it would start the paid run. Oneshots that `BindsTo=` a pod would also fail whenever port 8808 is busy. The pod existed only for the optional LLM, now deferred (C10) |
| D3 | **Bind-mount the existing host directories.** Data at the **same absolute path** inside the container as on the host (`%h/.local/share/jobhunter:%h/.local/share/jobhunter:z`, with `JOBHUNTER_DATA_DIR` set to it). Cache at `/cache`. Config: **only the file** `config.toml` at `/config/config.toml:ro`, never the directory | No data migration. Host `sqlite3` and the host CLI keep working on the same files. The DB stores absolute paths (attachments, cover letters, 017 PDFs); one path on both sides keeps rows written by the host readable in the container and the other way round. The config directory also holds `gmail_token.json` and `google_client_secret.json`, so mounting it would hand both to every container, the console included |
| D4 | SELinux label **`:z` (shared), never `:Z`** | `:Z` gives each container a private MCS label; the next container to start relabels the directory and locks the others out. Host processes running as `unconfined_t` still read `container_file_t` files. Files moved in with `mv` keep their old label: copy, don't move ([SELinux](#selinux)) |
| D5 | **`UserNS=keep-id:uid=1000,gid=1000`**, image user `jobhunter` uid 1000 | Your host UID maps to the image user, so files created in the data directory are owned by you on the host at their 0600/0700 modes |
| D6 | **Explicit container mode, `JOBHUNTER_CONTAINER=1`**, set in the image; never auto-detected | Auto-detecting `/run/.containerenv` would misfire in toolbox and distrobox, which have that file and where the host layout is right |
| D7 | Container-mode defaults: data `/data`, cache `/cache`, config file `/config/config.toml`, Google client JSON `/run/secrets/google_client_secret.json`. The `JOBHUNTER_*` variables still win, and the Quadlet units set `JOBHUNTER_DATA_DIR` per D3 | Container mode adds *defaults*, not a new precedence rule. `/data` is what a bare `podman run` (CI, a quick try) gets |
| D8 | Secrets are **podman secrets mounted as files** (`type=mount`, mode 0400, uid 1000) and named by **`<NAME>_FILE`** variables, resolved once at CLI start. Whitespace around a value is stripped; a value with whitespace or control characters inside is refused, whatever its source | Files never show in `podman inspect` (only the secret name does), in a unit, or in an image layer. One resolver keeps every existing `os.environ.get(...)` reader working, including the Anthropic SDK. The validation closes the httpx path that would put a key with a stray `\r\n` into logs, the DB and the console |
| D9 | **Each unit gets only its secrets**, through one Quadlet drop-in per secret, written by `jobhunter container preflight` only for secrets that exist ([matrix](#which-container-gets-which-secret)) | podman refuses to create a container that names a missing secret, so fixed `Secret=` lines would break every install that lacks one provider. Drop-ins also give least privilege: the console never gets the Gmail token |
| D10 | Gmail: `mail auth` runs **on the host only**. `jobhunter mail auth --podman-secret jobhunter_gmail_token` stores the refresh token as a podman secret; container mode refuses `mail auth` and never reads a token from `/config` | The host has the browser; the container has no keyring and no browser. A single place to authenticate avoids a second token copy under the data directory, which host backup tools would pick up |
| D11 | Console reachability: **first choice**, keep binding `127.0.0.1` inside the container and give the console container `Network=pasta:--host-lo-to-ns-lo` so host-loopback connections land on the container's loopback. **Fallback**, if the host check shows that does not work: bind `0.0.0.0` inside, allowed only when `JOBHUNTER_PUBLISHED_LOOPBACK_ONLY=1` is set in the unit, never implied by container mode. Either way, publish `127.0.0.1:8808` only, and refuse a non-loopback `Host` on every request, not only POSTs | pasta's default sends host-loopback connections to the container's public address, so a loopback bind is unreachable without that option; whether podman passes it through cleanly is a host check, not something to assume. Keeping an interlock means a mistaken `-p 8808:8808` on a Fedora firewall zone that opens high ports does not silently expose paid actions to the LAN. The Host check stops DNS rebinding for reads too; it is not auth against a LAN client, which can forge `Host`, so the publish address remains the real control |
| D12 | A **run lock**: `flock` on one fd of `<db_path>.run.lock`, held by `run`, `score` submit, `--rescore-pending` and the console's Re-score now; `collect` waits for it ([Run lock](#run-lock)) | With host and container timers both enabled during a switch, two runs could submit two paid batches. `flock` holds across containers that bind-mount the same file on one kernel; POSIX `fcntl` locks are dropped when any other fd on the file is closed in the holder |
| D13 | Profile and resume live in the data directory, **read-write** | The console edits `profile/preferences.yaml` (014) and takes resume uploads (`POST /prefs/resume`) |
| D14 | Playwright is a **separate build target** (`browser`), headless shell only | No source is `tier: browser`. 017 PDF export needs it, and 017 phase 2's Attach resume needs that PDF, so the browser image is required before the 017 phase 2 gate (open question 5) |
| D15 | **Version skew guard** (C12): code refuses a database with a migration it does not know, and every migration is preceded by an automatic backup | The host checkout and the image share one DB. Without the guard, an older image (or a rollback) would run against a newer schema, and the first command of a newer one would migrate with no way back |
| D16 | `claude -p` drafting stays **on the host** through a CLI command (`jobhunter packet draft <id>`, owned by 017); **no `~/.claude` mount**; in containers drafting uses the SDK only on an explicit click | Mounting `~/.claude` gives a subscription credential, history and MCP config to a long-running network-facing container for a few drafts a week. A silent switch from subscription to API would spend credits without asking |
| D17 | Automatic backups go to `<data dir>/backups/auto/`, named in UTC, and pruning only ever touches that directory and that exact name pattern | Manual `jobhunter-...-pre-<what>.db` backups already sit in `backups/` with the same prefix; they are rollback points and must never be pruned. `python:3.13-slim` has no local timezone, so container and host names would interleave wrongly in local time |

## Image

`Containerfile` at the repo root, multi-stage. The sketch shows shape, not final syntax; C3 owns
the real file.

```
FROM ghcr.io/astral-sh/uv:<ver>@sha256:<digest> AS uv

FROM docker.io/library/python:3.13-slim-trixie@sha256:<digest> AS builder
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project      # dependency layer, cached
COPY src/ src/
RUN uv sync --locked --no-dev --no-editable

FROM builder AS builder-browser
RUN uv sync --locked --no-dev --no-editable --extra browser

FROM docker.io/library/python:3.13-slim-trixie@sha256:<digest> AS runtime
ARG BUILD_COMMIT=unknown
RUN groupadd -g 1000 jobhunter && useradd -u 1000 -g 1000 -m -d /home/jobhunter jobhunter \
 && mkdir -p /data /cache /config && chown 1000:1000 /data /cache
COPY --from=builder /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH JOBHUNTER_CONTAINER=1 PYTHONUNBUFFERED=1 \
    JOBHUNTER_BUILD_COMMIT=$BUILD_COMMIT
LABEL org.opencontainers.image.revision=$BUILD_COMMIT
USER 1000:1000
EXPOSE 8808
ENTRYPOINT ["jobhunter"]
CMD ["console"]

FROM runtime AS browser
USER 0
COPY --from=builder-browser /app/.venv /app/.venv
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/pw
RUN playwright install --with-deps --only-shell chromium && rm -rf /var/lib/apt/lists/*
USER 1000:1000
```

| Topic | Rule |
|---|---|
| Base pinning | Every `FROM` carries `@sha256:` (tested). Digests are bumped by hand in a `chore:` PR, monthly or for a CVE |
| Non-root | Final `USER` is 1000 in both targets |
| What is copied | `pyproject.toml`, `uv.lock`, `README.md` (hatchling needs it to build the wheel), `src/`. Nothing else |
| Never in the image | `.env*`, `config.toml`, `*.local.toml`, `data/`, `profile/`, `resume/`, `*.migrated-*/`, `*token*.json`, `*client_secret*.json`, `*.pem`, `.git`, `.claude/`, `tests/`, `specs/`. `.containerignore` uses `**/` prefixes, because its patterns anchor at the context root, unlike `.gitignore`; hatchling has no `.gitignore` in the build context and packages every file under `src/jobhunter` |
| Secrets in build | None; no `ARG`/`ENV` named like a key, token or secret (tested) |
| Size budget | Default ≤ 450 MB uncompressed; `browser` ≤ 1.1 GB. C8 prints the size and fails over budget; revisited after the first real build |
| Health | No `HEALTHCHECK` in the image; the console unit has `HealthCmd=` with a Python one-liner fetching `/` with `Host: 127.0.0.1:8808` |
| Version | `JOBHUNTER_BUILD_COMMIT` and the OCI revision label carry the commit (C12 compares it with the host checkout) |

## Container mode

`JOBHUNTER_CONTAINER=1` (C1) changes defaults and refusals only:

| Behaviour | Host (unchanged) | Container mode |
|---|---|---|
| config file | `$XDG_CONFIG_HOME/jobhunter/config.toml` | `/config/config.toml` (single-file read-only mount) |
| data dir | `~/.local/share/jobhunter` | `/data`; the Quadlet units set `JOBHUNTER_DATA_DIR` to the host path (D3) |
| cache | `~/.cache/jobhunter` | `/cache` |
| Google client JSON default | `~/.config/jobhunter/google_client_secret.json` | `/run/secrets/google_client_secret.json` |
| Gmail token | keyring, else `~/.config/jobhunter/gmail_token.json` | only `$JOBHUNTER_GMAIL_TOKEN_FILE`; never keyring, never `/config` |
| `jobhunter paths` | sources like `default (XDG_DATA_HOME)` | first line `mode: container`, source `container default` |
| `.env` files | `$JOBHUNTER_ENV_FILE`, `./.env`, `~/.env` | only `$JOBHUNTER_ENV_FILE` (discouraged) |
| legacy-data guard | runs | skipped (no checkout, no `git`) |
| `migrate-paths`, `schedule`, `mail auth`, `container preflight` | work | refuse: "run this on the host" with the command |
| console bind | `127.0.0.1`; other hosts need `--allow-remote` | the same, plus `0.0.0.0` when `JOBHUNTER_PUBLISHED_LOOPBACK_ONLY=1` (D11 fallback only) |
| Host check | POSTs only | every request when `allow_remote` is off, host and container alike |

A single-file mount pins the inode: an editor that saves by rename leaves the container on the
old file until it restarts. The console reads `config.toml` at start anyway.

## Secrets

### Mechanism

```
podman secret create jobhunter_openrouter ./key.txt && shred -u ./key.txt
# or from a password manager, on stdin:
<password-manager-command> | podman secret create jobhunter_openrouter -
```

Never put a key on the command line (shell history, `ps`). A drop-in written by preflight:

```
# ~/.config/containers/systemd/jobhunter-ctr-run.container.d/50-openrouter.conf
[Container]
Secret=jobhunter_openrouter,type=mount,target=/run/secrets/openrouter_api_key,uid=1000,gid=1000,mode=0400
Environment=OPENROUTER_API_KEY_FILE=/run/secrets/openrouter_api_key
```

`type=env` secrets also stay out of `podman inspect` but sit in the initial environment, visible
to `podman exec ... env`; files are preferred. C8 checks the inspect claim with a fake value.

A replaced secret (`--replace`) reaches only newly created containers: the oneshots pick it up
at their next run; the console needs `systemctl --user restart jobhunter-console`.

### `*_FILE` resolution and validation (C2)

At CLI start, before `.env` loading: for each known secret name `N`, if `N_FILE` is set, read
the file and set `N` in the process environment. Known names: `ANTHROPIC_API_KEY`,
`OPENROUTER_API_KEY`, `OPENAI_COMPAT_API_KEY`, `LLAMA_API_KEY`, `USAJOBS_API_KEY`,
`USAJOBS_EMAIL`, plus every `api_key_env` in config and every `key_env` / `email_env` in the
registry.

- Strip all leading and trailing whitespace (`\r\n`, `\n\n`, spaces, tabs).
- Refuse a value that then contains any whitespace or control character, is empty, or comes
  from a file that is missing, unreadable or over 64 KiB; refuse `N` and `N_FILE` both set.
- The same validation runs on every known secret **whatever its source** (real env, `.env`),
  on the host too.
- Errors name the variable and the path only: never the value, a prefix, or a length.

Values end up in `os.environ`, so child processes inherit them and same-UID processes can read
`/proc/<pid>/environ`. That is today's `.env` exposure and is not visible to `podman inspect`.
One child matters: `claude -p` would bill `ANTHROPIC_API_KEY` instead of the subscription.
017's runner starts it with an allowlisted environment that drops `ANTHROPIC_*` and every known
secret name (017 revision 6); 018 relies on that and C9 tests it from the container side.

`jobhunter secrets status` lists each known name as `set (env)`, `set (file PATH)`,
`set (.env)`, `invalid (reason)` or `unset`. Never values, prefixes or lengths.

### Which container gets which secret

| Secret (podman name) | Env in the drop-in | console | ctr-run | ctr-collect | ctr-rescore | ctr-verify | ctr-backup |
|---|---|---|---|---|---|---|---|
| `jobhunter_anthropic` | `ANTHROPIC_API_KEY_FILE` | yes (Re-score now) | yes | yes | yes | | |
| `jobhunter_openrouter` | `OPENROUTER_API_KEY_FILE` | yes | yes | | yes | | |
| `jobhunter_openai_compat` | `OPENAI_COMPAT_API_KEY_FILE` | yes | yes | | yes | | |
| `jobhunter_llama` | `LLAMA_API_KEY_FILE` | yes | yes | | yes | | |
| `jobhunter_usajobs_key`, `jobhunter_usajobs_email` | `USAJOBS_API_KEY_FILE`, `USAJOBS_EMAIL_FILE` | | yes | | | yes | |
| `jobhunter_google_client` | `JOBHUNTER_GOOGLE_CLIENT_SECRETS=/run/secrets/google_client_secret.json` | | yes | | | | |
| `jobhunter_gmail_token` | `JOBHUNTER_GMAIL_TOKEN_FILE` (new, C7) | | yes | | | | |

C13 writes the drop-ins from `podman secret ls` (names only) and this matrix; a secret that
does not exist gets no drop-in, so no unit fails for a provider you do not use.

### Gmail

- **Client JSON** is the podman secret `jobhunter_google_client`, mounted at
  `/run/secrets/google_client_secret.json` in the run container only.
- **Refresh token.** Run `jobhunter mail auth --podman-secret jobhunter_gmail_token` on the
  host (C7). Before the consent flow it checks podman works, through the same host-escape chain
  the browser opener uses (`podman`, `flatpak-spawn --host podman`, `distrobox-host-exec
  podman`), so a toolbox without podman fails before you click anything. The token is piped to
  `podman secret create --replace jobhunter_gmail_token -` on stdin, never argv, never printed.
  If that fails after consent, the token is stored locally as today (keyring, else the 0600
  file) with a warning, so the consent is not lost. On success nothing is kept locally unless
  `--keep-local`.
- After the switch, preflight warns while `~/.config/jobhunter/gmail_token.json` or
  `google_client_secret.json` still exist on the host and prints the `rm` line; it never
  deletes.
- The OAuth app is in Google "Testing" mode (012), where refresh tokens expire after about seven
  days, so re-auth is a routine one-command step. See open question 3.

### At rest

podman's default `file` secret driver keeps values base64-encoded in a 0600 file under
`~/.local/share/containers/storage/secrets/`; `podman secret inspect --showsecret` prints them.
That is equivalent to today's `~/.env` and `gmail_token.json`, not encrypted. What podman
secrets add is keeping values out of units, `inspect`, logs and image layers. C11 documents
excluding that directory from home backups and the `pass` or `shell` drivers (for example
`secret-tool` / libsecret) for encryption at rest.

## Running it

### Quadlet (recommended)

Two directories, because the Quadlet generator reads only its own unit types and systemd does
not read `~/.config/containers/systemd/` (C4):

| File | Installed to | What |
|---|---|---|
| `deploy/quadlet/jobhunter-console.container` | `~/.config/containers/systemd/` | `ContainerName=jobhunter-console`, `PublishPort=127.0.0.1:8808:8808`, `Network=pasta:--host-lo-to-ns-lo` (D11), `UserNS=keep-id:uid=1000,gid=1000`, `Exec=console`, `HealthCmd=`, `[Service] Restart=on-failure`, `[Install] WantedBy=default.target` |
| `deploy/quadlet/jobhunter-ctr-backup.container` | same | `Exec=db backup --auto`, oneshot |
| `deploy/quadlet/jobhunter-ctr-run.container` | same | `Exec=run`; `[Unit] Requires=` and `After=jobhunter-ctr-backup.service` |
| `deploy/quadlet/jobhunter-ctr-collect.container` | same | `Exec=score --collect-pending`; `[Unit] OnSuccess=jobhunter-ctr-rescore.service` |
| `deploy/quadlet/jobhunter-ctr-rescore.container` | same | `Exec=score --rescore-pending` |
| `deploy/quadlet/jobhunter-ctr-verify.container` | same | `Exec=sources verify` |
| `deploy/systemd/jobhunter-ctr-run.timer`, `-collect.timer`, `-verify.timer` | `~/.config/systemd/user/` | 02:00 with `RandomizedDelaySec=10m`, 03:30, Sun 04:00; `Persistent=true` |
| `deploy/systemd/jobhunter-ctr-notify@.service` | `~/.config/systemd/user/` | copy of the host notify unit under its own name, so `jobhunter schedule uninstall` (which deletes `jobhunter-notify@.service`) cannot break `OnFailure=` |

Common to every `.container`: `Image=localhost/jobhunter:<tag>`,
`ContainerName=jobhunter-ctr-<job>` (the default `systemd-<unit>` name would break every
`podman exec` in this spec), `UserNS=keep-id:uid=1000,gid=1000`,
`Volume=%h/.local/share/jobhunter:%h/.local/share/jobhunter:z`,
`Environment=JOBHUNTER_DATA_DIR=%h/.local/share/jobhunter`,
`Volume=%h/.cache/jobhunter:/cache:z`,
`Volume=%h/.config/jobhunter/config.toml:/config/config.toml:ro,z`,
`[Service] ExecStartPre=/usr/bin/mkdir -p %h/.local/share/jobhunter %h/.cache/jobhunter`
(podman, unlike docker, refuses a missing bind source; `ExecStartPre` runs on the host),
`OnFailure=jobhunter-ctr-notify@%n.service`, `Timezone=local`. Oneshots carry
`[Service] Type=oneshot` and **no `[Install]`**; only timers start them. No `Secret=` lines in
the base files: secrets come from drop-ins (D9).

The collect chain replaces the host unit's two `ExecStart=` lines (Quadlet takes one `Exec=`)
without a new CLI mode. `%h` assumes default XDG bases; edit if yours differ.
`loginctl enable-linger $USER` keeps timers running while logged out, as on the host.

### Compose (option, podman-compose only)

`compose.yaml` for ad-hoc and dev: a `console` service (`127.0.0.1:8808:8808`,
`userns_mode: keep-id:uid=1000,gid=1000`, the mounts above with `:z`) and a `run` service
under a `manual` profile (`podman-compose run --rm run`). It sets `x-podman: in_pod: false`,
because podman-compose puts services in a pod by default and podman refuses `--userns` with
`--pod`. Secrets are `external: true` podman secrets. Use `podman-compose` directly
(`rpm-ostree install podman-compose` or `uv tool install podman-compose`), not `podman
compose`, which on this host delegates to docker-compose. No scheduler in compose.

### Local LLM (deferred, C10)

Not in this round. If wanted later: a digest-pinned llama.cpp server container reached from
the run and console containers. A llama-server already running on host loopback is not
reachable from a container by default; how to reach it under pasta is the first thing C10
checks on the host.

## SQLite store

- **One file, many processes**, as on the host. WAL needs a shared `-shm` mapping and file
  locks; both work across containers that bind-mount the same file on one kernel and local
  filesystem. Never put the data directory on NFS/SMB or a VM-shared folder.
- **Host backup tools** (restic, borg, Déjà Dup) must not copy the live `jobhunter.db`,
  `-wal` and `-shm`: they read them at different moments. Back up `backups/` and exclude the
  live files, or run `jobhunter db backup` from the tool's pre-backup hook.

### Run lock

C5, D12. `flock(LOCK_EX)` on one fd kept open for the whole job, on `<db_path>.run.lock` (keyed
on the database, which can live outside the data dir). Metadata (pid, hostname, command, start
time) is written through that same fd.

| Job | Behaviour when the lock is held |
|---|---|
| `run`, `score` submit, `--rescore-pending` | exit 0 with "another jobhunter run is in progress (pid, host, since)"; nothing spent |
| `--collect-pending` | waits up to 2 h, then exits non-zero so `OnFailure=` fires; collecting spends nothing but must not be skipped silently after a long or catch-up run |
| console Re-score now | takes the lock non-blocking for the paid part; if held, the panel says a run is in progress and offers to retry |
| any job, holder older than 6 h | exit non-zero (75) so a hung run is reported instead of every later timer quietly exiting 0 |

### Backups

C6, D17. `jobhunter db backup` uses SQLite's online backup API, then `integrity_check`:

- `--auto` (the `jobhunter-ctr-backup` oneshot, before every nightly run) writes
  `backups/auto/jobhunter-auto-YYYYMMDDTHHMMSSZ.db` (UTC, 0600) and prunes to the newest 14
  files matching exactly `^jobhunter-auto-\d{8}T\d{6}Z\.db$` in `backups/auto/`. Nothing else is
  ever deleted. At about 86 MB per copy today, that is about 1.2 GB.
- Without `--auto`, or with `--to PATH`, it writes a manual backup and never prunes.
- C12 writes `backups/auto/pre-migrate-<from>-<to>-<UTC>Z.db` before any migration; those are
  not pruned by the nightly rule either (different name).

### Restore

`jobhunter db restore PATH` runs **on the host** with the console and timers stopped; container
mode refuses it (a container cannot see other processes):

1. `systemctl --user stop jobhunter-console jobhunter-ctr-*.timer`, then wait for any running
   `jobhunter-ctr-*` service to finish.
2. Take the run lock (C5).
3. Back up the current database (manual name).
4. Copy the backup **into the live path through the SQLite backup API**, which handles the WAL;
   never rename a file over a WAL database (a leftover `-wal` would be replayed onto it). Temp
   files are created inside the data directory so they carry the right SELinux label.
5. `integrity_check`, then print row counts before and after.

### Version skew

C12, D15. The host checkout moves forward on every merge; the image moves when you rebuild.
Both open one database.

- Code refuses a DB that holds a migration it does not know: "database is newer than this
  jobhunter (code has N, database has M); update the image or the checkout". The console shows
  this instead of starting.
- Every migration is preceded by an automatic backup (above), so a forward migration can be
  undone with `db restore`.
- `jobhunter version` prints the package version, the commit, and the highest migration number;
  preflight compares host and image.
- Upgrade order: rebuild the image from the commit the host is on, then restart the console.
  Rolling the image back past a migration means restoring the matching `pre-migrate-*` backup.

### SELinux

`:z` relabels the bind-mounted directories `container_file_t` at container start. Then:

- Files created later in those directories inherit the label. Files **moved** in (`mv` from
  `~/Downloads`) keep their old label and the containers get EACCES. Copy instead (`cp`), or
  relabel with the `chcon` command preflight prints.
- `restorecon -R ~/.local/share` would reset the label; podman relabels at the next start, but
  a running console gets EACCES until it restarts. Optional permanent fix, printed by C11:
  `semanage fcontext -a -t container_file_t '<home>/.local/share/jobhunter(/.*)?'`.

## Moving to containers

With bind mounts (D3) there is no data copy. The switch, all on the host:

1. `jobhunter db backup` (C6).
2. `jobhunter schedule uninstall`, so the host timers stop.
3. Create the podman secrets you use; run `jobhunter mail auth --podman-secret
   jobhunter_gmail_token` if you use Gmail.
4. `podman build --build-arg BUILD_COMMIT=$(git rev-parse HEAD) -t localhost/jobhunter:dev .`
5. Install the files: `cp deploy/quadlet/*.container ~/.config/containers/systemd/` and
   `cp deploy/systemd/* ~/.config/systemd/user/`.
6. `jobhunter container preflight` (C13): creates missing directories (0700) and an empty
   `config.toml` if absent, checks ownership and labels, refuses while host timers are enabled,
   compares versions (C12), writes the secret drop-ins, prints the remaining commands.
7. `systemctl --user daemon-reload`, start the console, enable the timers
   ([Host verification](#host-verification)).

Old repo-relative data (`./data`) goes through `jobhunter migrate-paths` on the host first.

A **named volume** is not supported in this revision: host tools could not read it without
`podman unshare`, and absolute paths in the DB would no longer match the host. Dropped with the
`import-volume` command the first draft proposed.

## Drafting with `claude -p`

D16, C9. Spec 017 drafts with `claude -p` on the host. 018 needs from 017:

- a CLI command, `jobhunter packet draft <packet_id>`, that drafts one packet with the same
  checks and caps as the console path, so drafting needs no second console on the host (which
  could not share port 8808 with the container);
- the runner starting `claude` with an allowlisted environment that drops `ANTHROPIC_*` and
  every known secret name (017 revision 6), so the subscription path never bills the API.

In containers: the `claude` CLI is not in the image and `~/.claude` is not mounted. The packet
page says "drafting here uses the Anthropic API at about $X under the `[apply]` cap" with a
button, and shows the host command as the alternative. It never switches silently. Because the
data directory has the same path on both sides (D3), a PDF drafted on the host is served by the
container console unchanged. C9 is blocked on that 017 command.

## The 017 Chrome extension

The extension (017) talks to `http://127.0.0.1:8808`. The container provides:

- `127.0.0.1:8808` published, IPv4 only; the extension uses `127.0.0.1`, not `localhost`;
- the same app code, so the same `Host` loopback check (now on every request) and `Origin`
  rules; whatever rule 017 adds for `chrome-extension://<id>` applies unchanged; never
  `--allow-remote`;
- packet files at the same absolute path as on the host (D3);
- rendered PDFs only with the `browser` image (D14); without it, Attach resume has nothing to
  fetch, so the browser image is a precondition of the 017 phase 2 gate.

## Testing

Workers run in a container without a podman runtime. What they can still run there: pytest,
ruff, `uv build --wheel` in a temp directory holding only the files the Containerfile copies,
and, when the binaries exist (they do in the current sandbox), `/usr/libexec/podman/quadlet
-dryrun -user` with `QUADLET_UNIT_DIRS` pointed at the repo files and `systemd-analyze --user
verify` for the timers. Those tests skip when the binary is missing.

| Layer | Where | What |
|---|---|---|
| Unit | pytest, worker sandbox | container-mode defaults and refusals, Host check on GET (C1); `*_FILE` resolution with values ending `\r\n`, ` \n`, `\n\n`, `\t`, and a capture of all output, logs and stored errors asserting a fake value appears nowhere (C2); Containerfile rules and a fixture tree proving `.containerignore` excludes nested `.env`, `token.json`, `client_secret.json` (C3); run lock with two processes, a counting mock scorer, collect waiting, stale holder (C5); backup naming, prune never touching `jobhunter-20261009-160902-pre-jev.db`-shaped names, restore with an uncheckpointed WAL present (C6); fake `podman` recording argv and stdin, missing binary, non-zero exit keeps the token (C7); schema guard and pre-migrate backup (C12); drop-ins and preflight with fake outputs (C13) |
| Generator | pytest, sandbox, skipped without `quadlet` (C4) | dry run succeeds; container names; no `[Install]` on oneshots; no unit `Wants=` a `jobhunter-ctr-*` oneshot; no `Secret=` in base files; publish `127.0.0.1` only; no `.timer`/`.service` under `deploy/quadlet/`; timers match the host schedule |
| Image smoke | GitHub Actions `ubuntu-latest` (C8) | build both targets (browser on `main` only); sizes; with `--network=none`: `--help`, `id -u` = 1000, `paths` shows container mode, `init` on a fresh volume, console answers `/` with 200 from inside, a fake secret with a trailing `\r\n` resolves and `secrets status` says set while `podman inspect` and `podman logs` lack it (each check fails if the podman command itself fails), no `.env`/`data`/`profile`/`resume`/`token` files in the image; hadolint. Ubuntu's podman is 4.x and is not used for Quadlet; Quadlet is covered by the generator tests and the host |
| Host | you, commands below | real SELinux, keep-id, pasta loopback, secrets, timers |

No smoke step contacts a job board or a paid API; CI has no real secrets.

### Host verification

Run on the host after C3, C4 and C13 land. Expected output in comments.

```
podman build --build-arg BUILD_COMMIT=$(git rev-parse HEAD) -t localhost/jobhunter:dev .
                                                         # ends with the image id; no README error
podman run --rm localhost/jobhunter:dev paths            # first line: mode: container ; data_dir /data
podman run --rm --entrypoint id localhost/jobhunter:dev -u   # 1000
jobhunter container preflight                            # every line ok; lists drop-ins written
/usr/libexec/podman/quadlet -dryrun -user | grep '^---'  # ---jobhunter-console.service--- and the six -ctr- services
/usr/libexec/podman/quadlet -dryrun -user | grep -c 'Wants=jobhunter-ctr'   # 0
systemctl --user daemon-reload
systemctl --user start jobhunter-console
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8808/   # 200  (if 000: D11 fallback, see below)
curl -s -o /dev/null -w '%{http_code}\n' -H 'Host: evil.example' http://127.0.0.1:8808/   # 400
ss -ltn | grep 8808                                      # 127.0.0.1:8808 only, never 0.0.0.0 or *
systemctl --user list-units 'jobhunter-ctr-*' --all      # oneshots inactive (dead): starting the console started none
ls -lZ ~/.local/share/jobhunter/jobhunter.db             # owner you, -rw-------, container_file_t:s0 (no c-categories)
podman exec jobhunter-console jobhunter secrets status   # names with set (file ...) or unset; no values
podman inspect jobhunter-console >/dev/null && podman inspect jobhunter-console | grep -c -e sk-ant -e sk-or   # 0 (fails loudly if inspect fails)
podman exec jobhunter-console sh -c 'ls /config; ls /run/secrets'   # config.toml only; scorer keys only, no gmail token
systemctl --user enable --now jobhunter-ctr-run.timer jobhunter-ctr-collect.timer jobhunter-ctr-verify.timer
systemctl --user list-timers 'jobhunter-ctr-*'           # three timers with next run times
```

If the first `curl` prints `000`, the pasta loopback option did not work: switch the console unit
to the D11 fallback (`Exec=console --host 0.0.0.0` with
`Environment=JOBHUNTER_PUBLISHED_LOOPBACK_ONLY=1`), and report it so the spec records which.

## Docs

C11 writes `docs/containers.md`: build, secrets (from a file or stdin, never argv; at-rest
property; excluding podman's secret store from home backups; `pass`/`shell` drivers; restart
the console after rotating a key), the switch steps, Quadlet and podman-compose, backups (host
tools back up `backups/` only), restore, SELinux (`cp` not `mv`, `chcon`, optional `semanage`),
Gmail re-auth, upgrade order and rollback, and the host verification above. It amends 002
*Where files live* and *Process model* to point here.

## Phased tickets

All labelled `area:containers`; model per CLAUDE.md (reviewer one tier stronger).

| Phase | Ticket | Id | Model | Effort | Depends on |
|---|---|---|---|---|---|
| 1 | C1 Container mode (paths, refusals, Host check on every request, D11 interlock) | `5d7d73e` | sonnet | 0.5-1 d | |
| 1 | C5 Run lock (`flock`), host and containers | `b4c13eb` | opus | 0.5-1 d | |
| 1 | C12 Schema version guard and backup before migrate | `ef185ba` | opus | 0.5-1 d | C6 naming only |
| 1b | C2 `*_FILE` secrets, validation, `secrets status` | `efb6104` | sonnet | 0.5-1 d | C1 (same lines in `cli.py`/`config.py`; serial) |
| 1b | C6 Backup (auto dir, exact prune) and safe restore | `5a2063c` | opus | 0.5-1 d | C5 |
| 2 | C3 Containerfile and `.containerignore` | `e591f11` | sonnet | 0.5 d | C1 |
| 2 | C7 Gmail token as a podman secret, host only | `257edaa` | sonnet | 0.5-1 d | C1, C2 |
| 2 | C13 Host preflight and per-secret drop-ins | `9a1e1b7` | sonnet | 0.5-1 d | C1, C12 |
| 3 | C4 Quadlet units, systemd timers, compose.yaml | `1cbc6b9` | sonnet | 1 d | C1, C2, C3, C6, C7, C12, C13 |
| 3 | C8 CI image build and smoke test | `7b24373` | sonnet | 0.5-1 d | C1, C2, C3 |
| 4 | C11 Docs | `166a4c1` | haiku | 0.5 d | C4, C6, C7, C13 |
| later | C9 Drafting backend in container mode | `9910432` | opus | 0.5 d | C1, 017 `packet draft` and env scrub |
| deferred | C10 Local LLM container | `0adb58b` | sonnet | 0.5-1 d | C4; only if wanted |

About 7-11 days of worker time. Phase 1 lands with no change to how you run jobhunter today, and
C5 and C12 are worth landing on their own.

## Open questions

1. **Quadlet over compose.** Quadlet as the supported path and podman-compose as the
   convenience option, given compose is not in Fedora atomic's base image and `podman compose`
   here delegates to docker-compose?
2. **Bind mounts of your directories,** relabelled `container_file_t`, with the data directory
   at the same path inside the container. OK? (A named volume is dropped.)
3. **Gmail token expiry.** In Testing mode the refresh token lasts about a week. Re-auth weekly
   with one host command, or publish the OAuth app to "In production" (unverified, personal use)
   so the token stops expiring?
4. **Drafting in containers.** "API with a click, or `jobhunter packet draft` on the host": right?
   Or a dedicated drafting container with `~/.claude` mounted read-only?
5. **Browser image.** It is needed before the 017 phase 2 gate. Make it the default image now
   (about +500 MB), or switch when phase 2 is close?
6. **Image registry.** Local builds only (`localhost/jobhunter`), or push to GHCR from CI for
   `podman auto-update`? Pushing publishes the image (no personal data in it, by design), and
   auto-update would need the version guard's upgrade order.
7. **Attachments.** In containers only the data directory is visible, so attaching a file from
   `~/Documents` fails. Copy attachments into the data directory first (the console could do
   that on upload), or mount `~/Documents` read-only into the console?
8. **Backup space.** 14 nightly copies is about 1.2 GB today. Fewer, or a weekly tier?

## History

### Revision 2: adversarial review

An Opus review (2026-10-10) raised 18 blocking findings (about 11 distinct) and 32 minor ones.
All were taken; none was rejected. In short:

- **Config mount** now mounts only `config.toml`; container mode never reads a token or client
  JSON from `/config` (D3, D7, D10). Preflight warns about the host copies.
- **Secrets**: whitespace stripped and inner whitespace or control characters refused from any
  source, errors without values, tests with `\r\n` and friends (D8). Per-unit drop-ins for
  existing secrets only, least privilege matrix (D9). At-rest property stated.
- **Units**: no pod, so starting the console starts nothing paid (D2); oneshots have no
  `[Install]`; timers and a renamed notify unit go to `~/.config/systemd/user/`;
  `ContainerName=` set; collect then rescore via `OnSuccess=`; a backup oneshot before the run;
  missing bind sources created by `ExecStartPre`; compose is podman-compose with
  `in_pod: false`.
- **Database**: version skew guard and pre-migration backup, new ticket C12 (D15); automatic
  backups in `backups/auto/` with an exact prune pattern and UTC names (D17); restore through
  the backup API on the host with everything stopped; `flock` with collect waiting and stale
  holders reported (D12); same-path data mount for absolute paths in the DB (D3).
- **Image**: `README.md` copied (hatchling), `**/` ignore patterns tested on a fixture tree, a
  buildable browser stage with `PLAYWRIGHT_BROWSERS_PATH` and `--only-shell`.
- **Console exposure**: keep the loopback bind via pasta's `--host-lo-to-ns-lo` if it works,
  otherwise `0.0.0.0` only with an explicit unit variable; Host check on every request (D11).
- **Drafting**: 018 states the interface it needs from 017 (`jobhunter packet draft`, env
  scrub in 017 revision 6); C9 relabelled hard/opus as a money path (D16).
- **Gmail**: host-only auth, podman reached through the host-escape chain and checked before
  consent, token kept locally if the secret write fails (D10).
- **Checked**: sketch units for the console (pasta option, single-file config mount,
  `Timezone=`, `ExecStartPre=`) and the run oneshot with a secret drop-in were run through
  `quadlet -dryrun -user` (podman 5.8.4) in the agent sandbox: they generate, the drop-in's
  `--secret` and `--env` land on the run container only, and nothing `Wants=` the oneshot.
  Runtime behaviour (pasta loopback, SELinux, keep-id) is still a host check.
- **Tickets**: C6 split (preflight is now C13), `import-volume` dropped, C10 deferred, C12
  added, C1 and C2 serialized, C4 depends on C7, C12 and C13; generator dry runs added to what
  workers can test without a podman runtime.
