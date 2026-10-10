# 018 — Running jobhunter in rootless podman

Status: **draft for review** (bug `671bbf0`). Written 2026-10-10.

You asked for jobhunter to run under rootless podman: an image, a SQLite store, compose, config
that makes sense inside a container (XDG home does not), and secrets (API keys, Google
credentials) handled securely. This spec settles those, and the [tickets](#phased-tickets) are
filed in git-bug.

Nothing here changes how jobhunter runs on the host today. Container mode is opt-in, and the
host install (`uv run jobhunter ...`, `jobhunter schedule install`) keeps working.

## What exists today (checked in code, 2026-10-10)

| Thing | Today | Where |
|---|---|---|
| Runtime | Python 3.13 via `uv`, `uv.lock` committed | `pyproject.toml` |
| Console | FastAPI via uvicorn, `127.0.0.1:8808`; a non-loopback bind needs `--allow-remote`; POSTs from a browser must carry a loopback `Host` and a same-origin `Origin` | `cli.py console`, `console/app.py` `check_host`, `cross_site_reason` |
| Paths | XDG: config `~/.config/jobhunter`, data `~/.local/share/jobhunter`, cache `~/.cache/jobhunter`; overrides `JOBHUNTER_DATA_DIR`, `_CACHE_DIR`, `_DB_PATH`, `_PROFILE_DIR`, `_RESUME_PATH`, `JOBHUNTER_CONFIG` | `xdg.py`, `config.py` `PATH_ENV_VARS` |
| Legacy guard | Every command but `paths`, `migrate-paths`, `init`, `schedule` looks for an old `data/jobhunter.db` (uses `git rev-parse`) | `cli.py _load_env`, `legacy_data.py` |
| Secrets | Read from `os.environ` at use time; `.env` files loaded at startup from `$JOBHUNTER_ENV_FILE`, `./.env`, `~/.env` (real env wins) | `config.py load_env_files` |
| Secret names | `ANTHROPIC_API_KEY` (bare `anthropic.Anthropic()`), `OPENROUTER_API_KEY`, `OPENAI_COMPAT_API_KEY`, `LLAMA_API_KEY` (each an `api_key_env` setting), `USAJOBS_API_KEY`, `USAJOBS_EMAIL` (registry `auth.key_env`/`email_env`) | `config.py`, `sources/adapters/usajobs.py` |
| Gmail | Installed-app OAuth. Client secrets JSON at `mail.client_secrets_path` or `$JOBHUNTER_GOOGLE_CLIENT_SECRETS`. Refresh token in the OS keyring, else `~/.config/jobhunter/gmail_token.json` (0600). Loopback flow on a random port, or `--manual` paste flow (no local server) | `mail/auth.py` |
| gcloud | **None.** No `gcloud` CLI, no Application Default Credentials, no service account, no `google-cloud-*` package. The only Google credentials are the OAuth client JSON and the Gmail refresh token above | grep of `src/`, `pyproject.toml`, `scripts/` |
| SQLite | WAL, `busy_timeout=5000` | `core/db.py` |
| Scheduling | `systemd --user` timers rendered by `jobhunter schedule install`: run 02:00, collect 03:30, verify Sun 04:00, `OnFailure=jobhunter-notify@` | `ops/schedule.py`, `ops/units/` |
| Run exclusivity | **None.** Two `jobhunter run` processes can run at once | (gap; see [C5](#phased-tickets)) |
| Playwright | Optional `browser` extra; no source in the registry is `tier: browser` today; spec 017 PDF export uses headless Chromium when present | `core/fetch/context.py`, `specs/017` |
| Local LLM | `scripts/setup-local-llm.sh`: llama-server or Ollama on host loopback, key in `~/.env` as `LLAMA_API_KEY` | specs/016 |
| Drafting (017 rev 5, in progress) | `claude -p` on the host, SDK fallback | specs/017 (parallel draft) |

## Goals

1. One image that runs every jobhunter command, with no secrets and no personal data in it.
2. Console and the scheduled runs as rootless containers on Fedora atomic, SELinux enforcing.
3. The database and files stay readable by host tools (`sqlite3`, backups, the host CLI).
4. Secrets reach the process as files from podman secrets: never in a compose file, an env
   file, an image layer, `podman inspect`, or a log.
5. Everything testable without the network and without paid calls.

Non-goals: Kubernetes, multi-user, remote access to the console, running the 017 phase 2
browser agent in a container.

## Decisions

| # | Decision | Reason |
|---|---|---|
| D1 | **Quadlet** (systemd user units generated from `.container`/`.pod`) is the recommended way to run it. A `compose.yaml` is kept for ad-hoc and dev use | Quadlet ships with podman on Fedora atomic; compose needs `podman-compose` or `docker-compose` layered or installed in a toolbox. Quadlet gives timers, `journalctl`, `OnFailure=`, `Persistent=true` and `podman auto-update` for free, matching how the host install already works (002). Compose has no timer, and its `secrets:` either point at plain files on disk or (`external: true`) depend on which compose implementation `podman compose` delegates to |
| D2 | **One pod** (`jobhunter.pod`) holds the console, the oneshot run containers and the optional LLM | Containers in a pod share a network namespace, so the LLM server stays bound to `127.0.0.1` (016's rule) and the default `[scoring.local]` URL works unchanged. One `PublishPort=127.0.0.1:8808:8808` on the pod |
| D3 | **Bind-mount the existing host XDG directories**, not a named volume: `~/.local/share/jobhunter:/data:z`, `~/.cache/jobhunter:/cache:z`, `~/.config/jobhunter:/config:ro,z` | No data migration at all; host `sqlite3`, backups and the host CLI keep working on the same files; the host can still run a command (for example `claude -p` drafting, [D10](#drafting-with-claude--p)). Named volume documented as an option ([Moving to containers](#moving-to-containers)) |
| D4 | SELinux label **`:z` (shared), never `:Z`** | `:Z` gives each container a private MCS label; the second container to start would relabel the directory and lock the first out. Console and run share the data directory, so they need the shared label. Host processes running as `unconfined_t` still read `container_file_t` files |
| D5 | **`UserNS=keep-id:uid=1000,gid=1000`** with image user `jobhunter` uid 1000 | Your host UID maps to the image user regardless of what it is, so files created in `/data` are owned by you on the host, at their 0600/0700 modes. No `chown`, no `podman unshare` |
| D6 | **Explicit container mode, `JOBHUNTER_CONTAINER=1`**, set in the image; never auto-detected | Auto-detecting `/run/.containerenv` would misfire inside toolbox and distrobox, which have that file and where the host-style XDG layout is right |
| D7 | Fixed paths in container mode: data `/data`, cache `/cache`, config `/config` (read-only). Existing `JOBHUNTER_*` path variables still win | Container mode is a new *default*, not a new precedence rule; the precedence in 002 stays as written |
| D8 | Secrets are **podman secrets mounted as files** (`type=mount`, mode 0400, owned by uid 1000) and named by **`<NAME>_FILE`** env vars, resolved once at CLI start | Files never appear in `podman inspect` (only the secret name does), in the unit, or in an image layer. One resolver keeps every existing `os.environ.get(...)` reader working, including the Anthropic SDK's own `ANTHROPIC_API_KEY` lookup |
| D9 | Gmail refresh token: **a podman secret written from the host** by `jobhunter mail auth --podman-secret NAME`; fallback a 0600 file under `/data/state/` written by `mail auth --manual` inside the container | The host has a browser and a keyring; the container has neither. The token is only read in use (`build_service` never rewrites it), so a read-only secret fits. Keeping it off the data directory keeps it out of anything that copies `/data` |
| D10 | `claude -p` drafting stays **on the host**; **no `~/.claude` mount**; in container mode drafting uses the SDK only on an explicit click, never as a silent fallback | Mounting `~/.claude` hands a whole subscription credential (and its settings, history and MCP config) to a long-running network-facing container for a feature used a few times a week. A silent switch from subscription to API would spend credits without asking |
| D11 | Console binds `0.0.0.0` **inside** the container in container mode without `--allow-remote`; the Host/Origin loopback check is unchanged | Rootless port forwarding (pasta) delivers to the container's interface, not its loopback, so a loopback bind is unreachable. The host side publishes on `127.0.0.1` only, and the browser (and the 017 extension) still sends `Host: 127.0.0.1:8808`, so `cross_site_reason` keeps refusing DNS-rebinding and cross-site POSTs exactly as on the host |
| D12 | A **run lock** (`fcntl` on `<data dir>/run.lock`) for `run`, `score` submit/collect and `--rescore-pending`, on host and in containers | Host timers and container timers both enabled during a switch could otherwise submit two paid batches at once. `fcntl` locks hold across containers that bind-mount the same file on one kernel |
| D13 | Profile and resume live in `/data`, **read-write** | The console edits `profile/preferences.yaml` (014) and takes resume uploads (`POST /prefs/resume`); read-only mounts would break both. The brief suggested read-only; it would only fit the run containers, which write the database in the same directory anyway |
| D14 | Playwright/Chromium is a **separate build target** (`browser`), not the default | No source is `tier: browser` today; Chromium adds roughly 600-700 MB. Spec 017 PDF export needs it, and falls back to print-to-PDF HTML without it |

## Image

`Containerfile` at the repo root, multi-stage:

```
# builder
FROM ghcr.io/astral-sh/uv:<ver>@sha256:<digest> AS uv
FROM docker.io/library/python:3.13-slim-trixie@sha256:<digest> AS builder
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project      # dependency layer, cached
COPY src/ src/
RUN uv sync --locked --no-dev --no-editable

# runtime
FROM docker.io/library/python:3.13-slim-trixie@sha256:<digest> AS runtime
RUN groupadd -g 1000 jobhunter && useradd -u 1000 -g 1000 -m -d /home/jobhunter jobhunter \
 && mkdir -p /data /cache /config && chown 1000:1000 /data /cache
COPY --from=builder /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH JOBHUNTER_CONTAINER=1 PYTHONUNBUFFERED=1
USER 1000:1000
EXPOSE 8808
ENTRYPOINT ["jobhunter"]
CMD ["console", "--host", "0.0.0.0"]

# optional
FROM runtime AS browser
USER 0
RUN uv pip ... install the browser extra; playwright install --with-deps chromium (browsers in /opt/pw)
USER 1000:1000
```

The sketch shows shape, not final syntax; C3 owns the real file.

| Topic | Rule |
|---|---|
| Base pinning | Every `FROM` carries `@sha256:`. A tag alone is refused by `tests/test_containerfile.py`. Digests are bumped by hand in a `chore:` PR (monthly, or for a CVE); Renovate is an option later |
| Non-root | Final `USER` is 1000, never 0. No `sudo`, no setuid helpers added |
| Never in the image | `.env*`, `config.toml`, `data/`, `profile/`, `resume/`, `*.migrated-*/`, `token*.json`, `*.pem`, `.git`, `.claude/`, `tests/`, `specs/`. Enforced by `.containerignore` (mirrors `.gitignore`'s personal and secret entries) **and** by copying only `pyproject.toml`, `uv.lock` and `src/` |
| Secrets in build | None. No `ARG`/`ENV` named like a key, token or secret (tested). Nothing jobhunter needs at build time is secret |
| Size budget | Default target ≤ 450 MB uncompressed; `browser` ≤ 1.3 GB. C8 prints the size and fails over budget; the numbers are revisited after the first real build |
| Health | No `HEALTHCHECK` in the image; the console unit uses `HealthCmd=` with a Python one-liner fetching `/` on loopback |

## Container mode

`JOBHUNTER_CONTAINER=1` (C1) changes defaults only:

| Behaviour | Host (unchanged) | Container mode |
|---|---|---|
| config file | `$XDG_CONFIG_HOME/jobhunter/config.toml` | `/config/config.toml` (read-only mount) |
| data dir, db, profile, resume | `~/.local/share/jobhunter/...` | `/data`, `/data/jobhunter.db`, `/data/profile`, `/data/resume` |
| cache | `~/.cache/jobhunter` | `/cache` |
| `jobhunter paths` | sources like `default (XDG_DATA_HOME)` | first line `mode: container`, source `container default` |
| `.env` files | `$JOBHUNTER_ENV_FILE`, `./.env`, `~/.env` | only `$JOBHUNTER_ENV_FILE` (discouraged; use secrets) |
| legacy-data guard | runs | skipped (no checkout, no `git` in the image) |
| `migrate-paths`, `schedule install/uninstall` | work | refuse: "run this on the host" |
| keyring | tried first | not tried |
| `mail auth` | loopback server or `--manual` | `--manual` only (or `--podman-secret` on the host) |
| console bind | `127.0.0.1`; other hosts need `--allow-remote` | `0.0.0.0` allowed without `--allow-remote` (D11) |

An explicit `JOBHUNTER_DATA_DIR` and friends, `JOBHUNTER_CONFIG`, or `[paths]` in
`/config/config.toml` still win, so a non-standard layout is a config change, not a code change.
Paths inside `config.toml` must be container paths; the host config file is shared read-only,
so if it sets `[paths]` for the host, set the `JOBHUNTER_*_DIR` variables in the units to
override them.

## Secrets

### Mechanism

```
printf '%s' "$KEY" | podman secret create jobhunter_openrouter -    # or: podman secret create jobhunter_openrouter ./key.txt
```

Use stdin from a password manager or a file you then delete; never put a key on the command
line (shell history, `ps`). In the unit:

```
Secret=jobhunter_openrouter,type=mount,target=/run/secrets/openrouter_api_key,uid=1000,gid=1000,mode=0400
Environment=OPENROUTER_API_KEY_FILE=/run/secrets/openrouter_api_key
```

`type=env` secrets also keep the value out of `podman inspect`, but put it in the container's
initial environment, visible to `podman exec ... env`; files are preferred. C8 verifies the
inspect claim with a fake value instead of trusting it.

### `*_FILE` resolution (C2)

At CLI start, before `.env` loading: for each known secret name `N`, if `N_FILE` is set, read
the file (one trailing newline stripped) and set `N` in the process environment. Known names:
`ANTHROPIC_API_KEY`, `OPENROUTER_API_KEY`, `OPENAI_COMPAT_API_KEY`, `LLAMA_API_KEY`,
`USAJOBS_API_KEY`, `USAJOBS_EMAIL`, plus every `api_key_env` in config and every `key_env` /
`email_env` in the registry. Refused, naming the variable and path but never the value: both
`N` and `N_FILE` set; file missing, unreadable, empty or over 64 KiB.

Putting the value in `os.environ` means child processes inherit it and same-UID processes can
read `/proc/<pid>/environ`. That is the same exposure as today's `.env` loading and it is not
visible from `podman inspect`; a per-call accessor would avoid it but would touch every reader
for little gain in a single-user container.

`jobhunter secrets status` lists each known name as `set (env)`, `set (file /run/secrets/...)`,
`set (.env)` or `unset`. Never values, prefixes or lengths. Works on the host too.

| Secret | podman secret | Env in unit |
|---|---|---|
| Anthropic API key | `jobhunter_anthropic` | `ANTHROPIC_API_KEY_FILE` |
| OpenRouter / Jev key | `jobhunter_openrouter` | `OPENROUTER_API_KEY_FILE` |
| USAJOBS key and email | `jobhunter_usajobs_key`, `jobhunter_usajobs_email` | `USAJOBS_API_KEY_FILE`, `USAJOBS_EMAIL_FILE` |
| Local LLM key | `jobhunter_llama` | `LLAMA_API_KEY_FILE` (and the LLM container reads the same secret) |
| Google OAuth client JSON | `jobhunter_google_client` | `JOBHUNTER_GOOGLE_CLIENT_SECRETS=/run/secrets/google_client_secret.json` (existing variable; already a path) |
| Gmail refresh token | `jobhunter_gmail_token` | `JOBHUNTER_GMAIL_TOKEN_FILE` (new, C7) |

`ant auth login` profiles are not available in the container; the Anthropic SDK uses
`ANTHROPIC_API_KEY` there. Tests use fake values and fake files; none touches podman.

### Gmail

- **Client secrets JSON** is a podman secret, mounted read-only and pointed to by the existing
  `JOBHUNTER_GOOGLE_CLIENT_SECRETS`.
- **Refresh token (recommended path).** Run `jobhunter mail auth --podman-secret
  jobhunter_gmail_token` on the host. The usual browser flow runs on the host; the token is
  piped to `podman secret create --replace jobhunter_gmail_token -` on stdin (never argv, never
  printed) and not kept in the keyring unless `--keep-local`. Each nightly run is a fresh
  container, so it sees a replaced secret; the console, which does not call Gmail, needs no
  restart.
- **Fallback.** `podman exec -it jobhunter-console jobhunter mail auth --manual`: the existing
  paste flow (no local server, so no extra published port). With no keyring and `/config`
  read-only, the token goes to `/data/state/gmail_token.json` (0600, dir 0700).
- The OAuth app is in Google "Testing" mode (012), where refresh tokens expire after about seven
  days, so re-auth is a routine step; one host command keeps it cheap. See open question 3.

## Running it

### Quadlet (recommended)

Files in `deploy/quadlet/`, copied to `~/.config/containers/systemd/` (C4):

| File | What |
|---|---|
| `jobhunter.pod` | `PublishPort=127.0.0.1:8808:8808`, `UserNS=keep-id:uid=1000,gid=1000` |
| `jobhunter-console.container` | `Pod=jobhunter.pod`, `Exec=console --host 0.0.0.0`, `Restart=on-failure`, health check, volumes, secrets |
| `jobhunter-ctr-run.container` + `.timer` | `Exec=run`, `[Service] Type=oneshot`, timer `02:00` with `RandomizedDelaySec=10m`, `Persistent=true` |
| `jobhunter-ctr-collect.container` + `.timer` | `Exec=score --collect-pending` then `--rescore-pending` (two `ExecStart` are not available to Quadlet, so a small `jobhunter score --collect-then-rescore` or two units; C4 decides) at 03:30 |
| `jobhunter-ctr-verify.container` + `.timer` | `Exec=sources verify`, Sun 04:00 |
| `jobhunter-llm.container` | optional, C10 |

All use `Image=localhost/jobhunter:<tag>`, `Volume=%h/.local/share/jobhunter:/data:z`,
`Volume=%h/.cache/jobhunter:/cache:z`, `Volume=%h/.config/jobhunter:/config:ro,z`,
`OnFailure=jobhunter-notify@%n.service` (the host unit from `jobhunter schedule install`, or
copied from `src/jobhunter/ops/units/`). `%h` assumes default XDG bases; edit if yours differ.
The names carry `-ctr-` so they never collide with the host's `jobhunter-run.service`.
`loginctl enable-linger $USER` keeps timers running while logged out, as on the host.

### Compose (option)

`compose.yaml` for ad-hoc and dev: a `console` service (`127.0.0.1:8808:8808`, `userns_mode:
keep-id:uid=1000,gid=1000`, the same three bind mounts with `:z`) and a `run` service under a
`manual` profile (`podman compose run --rm run`). Secrets are `external: true`; if your compose
implementation does not support external secrets with podman, use Quadlet. No scheduler in
compose: scheduled runs belong to Quadlet or a host timer calling `podman run`.

### Local LLM (option, C10)

A digest-pinned llama.cpp server image in the pod, bound to `127.0.0.1:8080` inside the pod,
model directory mounted read-only, `LLAMA_API_KEY` from the same secret, GPU via CDI only when
the host has it. A llama-server already running on the host's loopback is not reachable from
the pod by default; which pasta option maps host loopback is to be verified in C10 on the host.

## SQLite store

- **One file, many processes**, as on the host: the console and each oneshot run open
  `/data/jobhunter.db` in WAL mode. WAL needs a shared `-shm` mapping and POSIX locks; both work
  across containers that bind-mount the same file on one kernel and local filesystem. Never put
  `/data` on NFS/SMB or a VM-shared folder.
- **Run lock** (D12, C5): one writer job at a time across host and containers; a second one
  exits 0 with "already running" and spends nothing.
- **Backups** (C6): `jobhunter db backup` uses SQLite's online backup API into
  `/data/backups/jobhunter-YYYYMMDD-HHMMSS.db` (0600) with `integrity_check`; the nightly run
  takes one first and keeps the newest 14 automatic backups. Never `cp` a live WAL database.
  Because `/data` is your host directory, host backup tools see it directly.
- **Restore**: `jobhunter db restore PATH` refuses while the run lock is held or another process
  has the database open; stop the pod first (`systemctl --user stop jobhunter-pod`). It backs
  up the current database, then restores, then verifies.

## Moving to containers

With bind mounts (D3) there is no data copy. The switch is:

1. On the host: `jobhunter db backup` (C6), then `jobhunter schedule uninstall` so host timers
   stop.
2. `jobhunter container preflight` (C6): directories exist and are yours, labels reported, no
   host timers enabled, prints the install commands.
3. Create secrets, build the image, install the Quadlet files, start the pod.

Old repo-relative data (`./data`) must go through `jobhunter migrate-paths` on the host first;
container mode refuses to migrate.

**Named-volume option.** For a volume instead of your home directory:
`podman volume create jobhunter-data`, then
`podman run --rm -v jobhunter-data:/data -v ~/.local/share/jobhunter:/src:ro,z
localhost/jobhunter jobhunter db import-volume /src` (C6), which copies the database through the
backup API and the other files with sha256 checks. Host tools then need `podman unshare` or
`podman volume export` to read it, which is why it is not the default.

## Drafting with `claude -p`

Spec 017 rev 5 drafts with `claude -p` on the host, SDK as fallback. In containers (D10, C9):

- The `claude` CLI is not in the image and `~/.claude` is not mounted.
- The packet page in a container says drafting here uses the Anthropic API at the shown
  estimate under the `[apply]` cap, and needs a click; it never switches silently.
- To keep using the subscription, run the host command (017's draft command) on the host;
  it writes to the same bind-mounted data directory, guarded by SQLite locking.

## The 017 Chrome extension

The extension (017 rev 5, parallel) talks to `http://127.0.0.1:8808`. The container provides:

- the pod publishes `127.0.0.1:8808` only, IPv4; the extension must use `127.0.0.1`, not
  `localhost` (which may try `::1` first);
- the same app code, so the same `Host` loopback and `Origin` checks. Requests arrive with
  `Host: 127.0.0.1:8808` and pass; whatever origin rule 017 adds for the extension
  (`chrome-extension://<id>`) applies unchanged. The container never runs with `--allow-remote`;
- packet files under `/data/packets/` that the host's Chrome reaches through the console, never
  a host path the container cannot see.

## Testing

**No podman in the agent sandbox.** Workers implement in a container without podman. Every
ticket says which checks the worker runs (unit tests, file-parse tests, ruff) and which need CI
or you on the host.

| Layer | Where | What |
|---|---|---|
| Unit | pytest, worker sandbox | container-mode defaults and refusals (C1); `*_FILE` resolution and that a fake value never appears in output or logs (C2); Containerfile/.containerignore rules (C3); Quadlet and compose files parsed: 127.0.0.1 publish, keep-id, `:z`, no inline secrets, oneshot, timer schedules equal the host units (C4); run lock with two processes and a counting mock scorer (C5); backup/restore on a tmp WAL db (C6); fake `podman` on `PATH` recording argv and stdin (C7) |
| Image smoke | GitHub Actions `ubuntu-latest`, podman preinstalled (C8) | build; size; with `--network=none`: `--help`, `id -u` = 1000, `paths` shows container mode, `init` on a fresh volume, console answers `/` with 200 (fetched from inside with Python urllib), fake secret: `secrets status` says set while `podman inspect` and `podman logs` lack the value, no `.env`/`data`/`profile`/`resume` in the image; hadolint |
| Host | you, commands below | Quadlet generation, SELinux, real secrets, real timers |

No smoke step contacts a job board or a paid API; CI has no real secrets.

### Host verification

Run on the host after C3 and C4 land (expected output in comments):

```
podman build -t localhost/jobhunter:dev .                # ends: Successfully tagged localhost/jobhunter:dev
podman run --rm localhost/jobhunter:dev paths            # first line: mode: container ; data_dir /data
podman run --rm --entrypoint id localhost/jobhunter:dev -u   # 1000
jobhunter container preflight                            # all checks ok; no host timers enabled
cp deploy/quadlet/* ~/.config/containers/systemd/
/usr/libexec/podman/quadlet -dryrun -user | grep '^---'  # ---jobhunter-console.service--- and the -ctr- units, no errors
systemctl --user daemon-reload
systemctl --user start jobhunter-pod jobhunter-console
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8808/   # 200
ss -ltn | grep 8808                                      # 127.0.0.1:8808 only, never 0.0.0.0 or *
ls -lZ ~/.local/share/jobhunter/jobhunter.db             # owner you, -rw-------, container_file_t:s0 (no c-categories)
podman exec jobhunter-console jobhunter secrets status   # names with set (file ...) or unset, no values
podman inspect jobhunter-console | grep -c -i 'sk-'      # 0
systemctl --user enable --now jobhunter-ctr-run.timer jobhunter-ctr-collect.timer jobhunter-ctr-verify.timer
systemctl --user list-timers 'jobhunter-ctr-*'           # three timers with next run times
```

## Docs

C11 writes `docs/containers.md` from this spec (build, secrets, Quadlet, switching, backups,
Gmail re-auth, upgrade and rollback by image tag, the host checks above) and amends 002's
*Where files live* and *Process model* to point here.

## Phased tickets

All labelled `area:containers`; model per CLAUDE.md (reviewer one tier stronger).

| Phase | Ticket | Id | Model | Effort | Depends on |
|---|---|---|---|---|---|
| 1 | C1 Container path mode (`JOBHUNTER_CONTAINER=1`) | `5d7d73e` | sonnet | 0.5-1 d | |
| 1 | C2 `*_FILE` secrets and `jobhunter secrets status` | `efb6104` | sonnet | 0.5 d | |
| 1 | C5 Single-writer run lock, host and containers | `b4c13eb` | opus | 0.5-1 d | |
| 2 | C3 Containerfile and .containerignore | `e591f11` | sonnet | 0.5 d | C1 |
| 2 | C7 Gmail token storage in containers | `257edaa` | sonnet | 0.5-1 d | C1, C2 |
| 2 | C6 SQLite backup/restore, host preflight, volume import | `5a2063c` | opus | 1-1.5 d | C1, C5 |
| 3 | C4 Quadlet units and compose.yaml | `1cbc6b9` | sonnet | 0.5-1 d | C1, C2, C3 |
| 3 | C8 CI image build and smoke test | `7b24373` | sonnet | 0.5-1 d | C1, C2, C3 |
| 4 | C11 Docs | `166a4c1` | haiku | 0.5 d | C4, C6, C7 |
| later | C10 Optional local LLM container in the pod | `0adb58b` | sonnet | 0.5-1 d | C4 |
| later | C9 Drafting backend in container mode | `9910432` | sonnet | 0.5 d | C1, 017 drafting code |

Total about 6-9 days of worker time. Phases 1 and 2 land with no change to how you run
jobhunter today; C5 is worth landing on its own.

## Open questions

1. **Quadlet over compose.** OK to make Quadlet the supported path and compose the convenience
   option, given compose is not in Fedora atomic's base image?
2. **Bind mounts of your XDG directories** (relabelled `container_file_t` by `:z`) rather than a
   named volume. OK? A later `restorecon -R ~/.local/share` would reset the label; podman
   relabels again at the next start.
3. **Gmail token expiry.** In Testing mode the refresh token lasts about a week. Re-auth weekly
   with one host command, or publish the OAuth app to "In production" (unverified, personal use)
   so the token stops expiring?
4. **Drafting in containers.** Is "API with a click, or run the draft command on the host" right,
   or would you rather mount `~/.claude` read-only into a dedicated drafting container?
5. **Browser image.** Build the `browser` target by default (bigger image, PDF export works in
   the console), or keep it optional?
6. **Image registry.** Local builds only (`localhost/jobhunter`), or push to GHCR from CI for
   `podman auto-update`? Pushing publishes the image (no personal data in it, by design).
