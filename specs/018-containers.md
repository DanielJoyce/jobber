# 018 — Running jobhunter in rootless podman

Status: **draft for review, revision 3** (bug `671bbf0`). Written 2026-10-10; revised twice the
same day after adversarial reviews and your answers ([History](#history)).

You asked for jobhunter to run under rootless podman: an image, a SQLite store, compose, config
that makes sense inside a container (XDG home does not), and secrets (API keys, Google
credentials) handled securely. This spec settles those, and the [tickets](#phased-tickets) are
filed in git-bug.

Container mode is opt-in; `uv run jobhunter ...` and `jobhunter schedule install` keep working.
Some tickets change host behaviour too, all as safety fixes: the console checks `Host` on every
request (C1), secret values are validated (C2), a run lock (C5), a schema version guard with a
backup before migrating (C12), and data-dir-relative paths in the database (C14). Once you
switch to containers, host commands stop migrating the database on their own (C12).

**018 is authoritative for container networking, mounts and secrets.** Where spec 017 (being
revised in parallel) describes how the console or the extension behaves inside a container,
this spec wins; 017 references C1 for the all-routes `Host` check.

## What exists today (checked in code, 2026-10-10)

| Thing | Today | Where |
|---|---|---|
| Runtime | Python 3.13 via `uv`, `uv.lock` committed, hatchling build with `readme = "README.md"` | `pyproject.toml` |
| Console | FastAPI via uvicorn, `127.0.0.1:8808`; a non-loopback bind needs `--allow-remote`; since 017 phase 1d (`46f2589`) **every** request, GETs included, must carry a loopback `Host` (`localhost` or a loopback IP literal such as `127.0.0.1` or `[::1]`) or gets **403**, and a browser POST must also be same-origin. Other names that reach the console, such as `foo.localhost`, `0.0.0.0` or an `/etc/hosts` alias, get 403 too unless the console runs with `--allow-remote` | `cli.py console`, `console/app.py` |
| Paths | XDG: config `~/.config/jobhunter`, data `~/.local/share/jobhunter`, cache `~/.cache/jobhunter`; overrides `JOBHUNTER_DATA_DIR`, `_CACHE_DIR`, `_DB_PATH`, `_PROFILE_DIR`, `_RESUME_PATH`, `JOBHUNTER_CONFIG` | `xdg.py`, `config.py` |
| Absolute paths in the DB | `attachment.path` (stored `.resolve()`d, so `/home/<you>` becomes `/var/home/<you>` on Fedora atomic), `application.cover_letter_path`, 017's `packet_document.rendered_path` | `console/tracking.py`, `core/migrations`, specs/017 |
| Migrations | `db.migrate()` applies any missing migration on every command and at console start; no backup first; no check for a database newer than the code | `core/db.py` |
| Secrets | Read from `os.environ` at use time; `.env` files loaded at startup from `$JOBHUNTER_ENV_FILE`, `./.env`, `~/.env` (real env wins). Your `~/.env` is mode 0644 | `config.py load_env_files` |
| Secret names | `ANTHROPIC_API_KEY` (bare `anthropic.Anthropic()`), `OPENROUTER_API_KEY`, `OPENAI_COMPAT_API_KEY`, `LLAMA_API_KEY` (each an `api_key_env` setting), `USAJOBS_API_KEY`, `USAJOBS_EMAIL` (registry `auth.key_env`/`email_env`) | `config.py`, `sources/adapters/usajobs.py` |
| Secret leak path | httpx refuses a header value with a trailing `\n`, `\r`, space or tab and puts the **whole value** in the exception text; several places log or store `str(exc)` | `scoring/scorers.py`, `scoring/screen.py`, `scoring/decisions.py`, `core/fetch/context.py` |
| Gmail | Installed-app OAuth, app in Google "Testing" mode. Client JSON at `~/.config/jobhunter/google_client_secret.json` or `$JOBHUNTER_GOOGLE_CLIENT_SECRETS`. Refresh token in the OS keyring, else `~/.config/jobhunter/gmail_token.json` (0600); this host uses the file | `mail/auth.py` |
| gcloud | **None.** No `gcloud` CLI, no Application Default Credentials, no service account, no `google-cloud-*` package | grep of `src/`, `pyproject.toml`, `scripts/` |
| SQLite | WAL, `busy_timeout=5000` | `core/db.py` |
| Backups | Manual, in `<data dir>/backups/`, named like `jobhunter-YYYYMMDD-HHMMSS-pre-<what>.db` | CLAUDE.md habit |
| Scheduling | `systemd --user` timers from `jobhunter schedule install`: run 02:00, collect then rescore-pending 03:30, verify Sun 04:00, `OnFailure=jobhunter-notify@`; `schedule uninstall` removes all, notify unit included | `ops/schedule.py`, `ops/units/` |
| Run exclusivity | **None** | gap, C5 |
| Playwright | Optional `browser` extra; no source is `tier: browser`; 017 PDF export uses headless Chromium when present | `core/fetch/context.py`, specs/017 |
| Drafting (017) | `jobhunter apply draft <packet_id>` runs `claude -p` on the host, SDK fallback | specs/017 |

## Goals

1. One image that runs every jobhunter command, with no secrets and no personal data in it.
2. Console and the scheduled runs as rootless containers on Fedora atomic, SELinux enforcing.
3. The database and files stay readable by host tools (`sqlite3`, the host CLI).
4. Secrets reach the process as files from podman secrets: never in a compose file, an env
   file, an image layer, a unit file, `podman inspect`, or a log. Each container gets only the
   secrets it uses. (At rest, podman's default secret store is no stronger than a 0600 file;
   see [At rest](#at-rest).)
5. Host and container code never disagree silently about the database schema.
6. Everything testable without the network and without paid calls.

Non-goals: Kubernetes, multi-user, remote access to the console, the 017 phase 2 browser agent
in a container, a registry (local builds only), a local LLM container (deferred, C10).

## Your decisions (2026-10-10)

| Question | Answer |
|---|---|
| Compose or Quadlet | Quadlet is supported; podman-compose is the convenience option |
| Store | Bind-mount the existing directories, relabelled for SELinux |
| Gmail | The app stays in Testing; re-auth weekly with one host command |
| Drafting in container mode | Runs on the host via `jobhunter apply draft` |
| Browser image | Added when 017 phase 2 starts; not the default |
| Registry | Local builds only, no GHCR |
| Attachments | Uploads are copied into the data directory; `~/Documents` is not mounted |
| Automatic backups | 7 daily + 4 weekly |

## Decisions

| # | Decision | Reason |
|---|---|---|
| D1 | **Quadlet** is the supported way to run it; `compose.yaml` for **podman-compose** is the convenience option | Your answer. Quadlet ships with podman on Fedora atomic and gives timers, `journalctl`, `OnFailure=`, `Persistent=true`. `podman compose` on this host delegates to docker-compose, which refuses `external: true` secrets outside swarm, so compose means podman-compose |
| D2 | **No pod.** The console is one container that publishes its port; backup, run, collect, rescore and verify are oneshot containers with no published port and no `[Install]` | A pod's service `Wants=` every member by default, so starting it would start the paid run; oneshots bound to a pod would also fail whenever port 8808 is busy |
| D3 | **Bind-mount the existing host directories** at fixed container paths: data `/data`, cache `/cache`, and **only the file** `config.toml` at `/config/config.toml:ro`. The units set all five `JOBHUNTER_*` path variables explicitly | Your answer; no data copy, host `sqlite3` and the host CLI keep working. The config directory also holds `gmail_token.json` and `google_client_secret.json`, so it is never mounted. Setting all five variables overrides any host `[paths]` (or `~`, which means `/home/jobhunter` inside) in the shared `config.toml` |
| D4 | SELinux label **`:z` (shared), never `:Z`** | `:Z` gives each container a private label, and the next container to start locks the others out. Files moved in with `mv` keep their old label: copy, don't move ([SELinux](#selinux)) |
| D5 | **`UserNS=keep-id:uid=1000,gid=1000`**, image user `jobhunter` uid 1000 | Files created in the data directory are owned by you on the host at their 0600/0700 modes |
| D6 | **Explicit container mode, `JOBHUNTER_CONTAINER=1`**, set in the image; never auto-detected | `/run/.containerenv` also exists in toolbox and distrobox, where the host layout is right |
| D7 | Container-mode defaults: data `/data`, cache `/cache`, config `/config/config.toml`, Google client JSON `/run/secrets/google_client_secret.json` | Defaults only; precedence in 002 is unchanged. A bare `podman run` (CI, a quick try) gets them |
| D8 | Secrets are **podman secrets mounted as files** (`type=mount`, mode 0400, uid 1000), named by **`<NAME>_FILE`**, resolved at CLI start and validated ([rules](#_file-resolution-and-validation-c2)) | Files stay out of `podman inspect`, units and image layers. One resolver keeps every `os.environ.get(...)` reader working, including the Anthropic SDK. Validation closes the httpx path that would log a key with a stray `\r\n` |
| D9 | **Each unit gets only its secrets**, one Quadlet drop-in per secret, written (and removed) by `jobhunter container preflight` for secrets that exist ([matrix](#which-container-gets-which-secret)) | podman refuses a container that names a missing secret; drop-ins also give least privilege |
| D10 | Gmail auth runs **on the host only**: `jobhunter mail auth --podman-secret jobhunter_gmail_token`. Container mode refuses `mail auth` and never reads a token from `/config` | Your answer (weekly re-auth, one host command). The container has no browser or keyring |
| D11 | Console reachability: keep the `127.0.0.1` bind inside the container with `Network=pasta:--host-lo-to-ns-lo`. Fallback, if the host check shows that fails: `0.0.0.0` inside, allowed only with `JOBHUNTER_PUBLISHED_LOOPBACK_ONLY=1` in the unit. Publish `127.0.0.1:8808` only. C1 refuses a non-loopback `Host` on **every** request (host and container) | pasta by default sends host-loopback connections to the container's public address. The explicit variable keeps an interlock against a mistaken `-p 8808:8808` on a firewall zone that opens high ports. The Host check stops DNS rebinding for reads; a LAN client can forge `Host`, so the publish address remains the real control |
| D12 | A **run lock**: `flock` on one fd of `<db_path>.run.lock`; timer-started jobs wait for it, interactive ones report and exit ([Run lock](#run-lock)) | Host and container timers during a switch, or a catch-up after downtime, could otherwise double-submit paid batches or silently skip a job |
| D13 | Profile and resume live in the data directory, **read-write** | The console edits `profile/preferences.yaml` (014) and takes resume uploads |
| D14 | Playwright is a **separate build target** (`browser`), headless shell only, **added when 017 phase 2 starts** | Your answer. 017 phase 2's Attach resume needs the rendered PDF, so the browser image is a precondition of that phase, not of this one |
| D15 | **Schema version guard on every connection**, and **no automatic migration in a container deployment**: migrations run only through `jobhunter upgrade` (or `db migrate`), after a backup ([Version skew](#version-skew)) | The host checkout fast-forwards on every merge; if host commands kept migrating, the containers would refuse to start after each merge. One explicit upgrade step keeps code and schema in step |
| D16 | Drafting in container mode **runs on the host** via `jobhunter apply draft <packet_id>` (017). The container has no `claude` CLI, no `~/.claude` mount, and no SDK drafting | Your answer. Mounting `~/.claude` would hand a subscription credential to a long-running network-facing container; no SDK path means no silent paid fallback |
| D17 | Automatic backups in `<data dir>/backups/auto/`, named in UTC, kept **7 daily + 4 weekly**; pre-migrate backups keep the newest 3; pruning touches only exact name patterns in that directory | Your answer for the schedule. Manual `-pre-<what>` backups already in `backups/` are rollback points and must never be pruned |
| D18 | The database stores **paths relative to the data directory** (C14); uploads are copied into `<data dir>/attachments/` | Absolute paths cannot match on both sides: `%h` is `/home/<you>` while stored paths are `.resolve()`d to `/var/home/<you>`, and the container sees `/data`. Relative paths make the mount point irrelevant. Your answer: `~/Documents` is not mounted |

## Image

`Containerfile` at the repo root, multi-stage; the sketch shows shape, C3 owns the file.

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

# Added when 017 phase 2 starts (D14):
# FROM builder AS builder-browser   ->  uv sync --locked --no-dev --no-editable --extra browser
# FROM runtime AS browser           ->  that venv, ENV PLAYWRIGHT_BROWSERS_PATH=/opt/pw,
#                                       playwright install --with-deps --only-shell chromium
```

| Topic | Rule |
|---|---|
| Base pinning | Every `FROM` carries `@sha256:` (tested); digests bumped by hand in a `chore:` PR |
| Non-root | Final `USER` is 1000 |
| What is copied | `pyproject.toml`, `uv.lock`, `README.md` (hatchling needs it), `src/`. Nothing else |
| Never in the image | `.env*`, `config.toml`, `*.local.toml`, `data/`, `profile/`, `resume/`, `*.migrated-*/`, `*token*.json`, `*client_secret*.json`, `*.pem`, `.git`, `.claude/`, `tests/`, `specs/`. `.containerignore` uses `**/` prefixes (its patterns anchor at the context root, unlike `.gitignore`), tested on a fixture tree |
| Secrets in build | None; no `ARG`/`ENV` named like a key, token or secret (tested) |
| Size budget | Default ≤ 450 MB uncompressed (C8 fails over budget; revisited after the first build) |
| Health | No `HEALTHCHECK` in the image; the console unit's `HealthCmd=` fetches `/healthz` (C1: `SELECT 1` and the version guard, no writes, no templates) with `Host: 127.0.0.1:8808` |
| Tags | `localhost/jobhunter:<commit>` plus `localhost/jobhunter:current`, which the units use; local only |

## Container mode

`JOBHUNTER_CONTAINER=1` (C1) changes defaults and refusals only:

| Behaviour | Host (unchanged) | Container mode |
|---|---|---|
| config file | `$XDG_CONFIG_HOME/jobhunter/config.toml` | `/config/config.toml` (single-file read-only mount) |
| data, cache | XDG | `/data`, `/cache` (the units also set the five path variables, D3) |
| Google client JSON default | `~/.config/jobhunter/google_client_secret.json` | `/run/secrets/google_client_secret.json` |
| Gmail token | keyring, else `~/.config/jobhunter/gmail_token.json` | only `$JOBHUNTER_GMAIL_TOKEN_FILE` |
| `jobhunter paths` | XDG sources | first line `mode: container`, source `container default` |
| `.env` files | `$JOBHUNTER_ENV_FILE`, `./.env`, `~/.env` | only `$JOBHUNTER_ENV_FILE` (discouraged) |
| legacy-data guard | runs | skipped (no checkout, no `git`) |
| `migrate-paths`, `schedule`, `mail auth`, `container preflight`, `upgrade`, `db restore`, `apply draft` | work | refuse with the host command |
| console bind | `127.0.0.1`; others need `--allow-remote` | the same, plus `0.0.0.0` with `JOBHUNTER_PUBLISHED_LOOPBACK_ONLY=1` (D11 fallback) |

A single-file mount pins the inode: an editor that saves by rename leaves the container on the
old file until it restarts.

## Paths in the database

C14, D18. Paths the database stores (`attachment.path`, `application.cover_letter_path`, 017's
`packet_document.rendered_path`) become relative to the data directory and are resolved against
the current one when read. Uploads are copied into `<data dir>/attachments/`.
`jobhunter db relativize-paths` (host, dry run by default) rewrites existing rows: paths under
the data directory or its realpath (`/home/<you>/...` and `/var/home/<you>/...` alike) become
relative; files outside it are copied into `attachments/` with a sha256 check first. Preflight
refuses to switch while any absolute path remains. 017 writes `rendered_path` relative from
the start (noted on its bug, `7fa29db`).

## Secrets

### Mechanism

```
podman secret create jobhunter_openrouter ./key.txt && shred -u ./key.txt
<password-manager-command> | podman secret create jobhunter_openrouter -
```

Never put a key on the command line. A drop-in written by preflight:

```
# ~/.config/containers/systemd/jobhunter-ctr-run.container.d/50-jobhunter_openrouter.conf
# written by jobhunter container preflight; removed by it when the secret is gone
[Container]
Secret=jobhunter_openrouter,type=mount,target=/run/secrets/openrouter_api_key,uid=1000,gid=1000,mode=0400
Environment=OPENROUTER_API_KEY_FILE=/run/secrets/openrouter_api_key
```

The environment variable name comes from config (`api_key_env` of each scorer, the registry's
`key_env`/`email_env`), not from a fixed list, so a renamed variable still matches.

**After any secret change** (create, replace, remove): rerun `jobhunter container preflight`,
then `systemctl --user daemon-reload` and `systemctl --user restart jobhunter-console`. Preflight
writes drop-ins for new secrets and deletes the ones it wrote (marked by the header line) for
secrets that are gone; it never touches a drop-in it did not write. Oneshots pick up changes at
their next run.

`type=env` secrets would sit in the container's initial environment, visible to `podman exec
... env`; files are used instead. C8 checks the `inspect` claim with a fake value.

### `*_FILE` resolution and validation (C2)

At CLI start, in this order:

1. Load `.env` files as today, recording which names came from them.
2. For each known secret name `N` with `N_FILE` set: read the file. If `N` is set in the real
   environment, refuse (conflict). If `N` came from a `.env` file, the file wins, with a warning
   naming the `.env` file.
3. Validate every known secret, whatever its source: strip leading and trailing whitespace;
   an empty value counts as **unset** (so a `.env` placeholder like `OPENROUTER_API_KEY=` is
   fine); a value with any whitespace or control character inside, or a file that is missing,
   unreadable or over 64 KiB, is **invalid**.
4. An invalid secret is removed from the environment with a one-line warning naming the
   variable and reason. Other commands go on; whatever needs that secret reports it as
   missing. Nothing invalid is ever sent.

Messages name variables, files and reasons only: never a value, prefix or length. `jobhunter
secrets status` lists every known name as `set (env)`, `set (file PATH)`, `set (.env PATH)`,
`invalid (reason)` or `unset`, and runs to the end whatever it finds. It and preflight warn when
a `.env` file being read is group- or world-readable (yours is 0644) and print the `chmod 600`.

Values end up in `os.environ`, so child processes inherit them. The child that matters,
`claude -p`, belongs to `jobhunter apply draft` on the host; 017's runner starts it with an
allowlisted environment that drops `ANTHROPIC_*` and every known secret name.

### Which container gets which secret

| Secret (podman name) | console | ctr-run | ctr-collect | ctr-rescore | ctr-verify | ctr-backup | `jobhunter-ctr` |
|---|---|---|---|---|---|---|---|
| `jobhunter_anthropic` | yes (Re-score now) | yes | yes | yes | | | yes |
| `jobhunter_openrouter` | yes | yes | | yes | | | yes |
| `jobhunter_openai_compat` | yes | yes | | yes | | | yes |
| `jobhunter_llama` | yes | yes | | yes | | | yes |
| `jobhunter_usajobs_key`, `jobhunter_usajobs_email` | | yes | | | yes | | yes |
| `jobhunter_google_client` (`JOBHUNTER_GOOGLE_CLIENT_SECRETS`) | | yes | | | | | yes |
| `jobhunter_gmail_token` (`JOBHUNTER_GMAIL_TOKEN_FILE`, C7) | | yes | | | | | yes |

### Gmail

- **Client JSON**: podman secret `jobhunter_google_client`, mounted at
  `/run/secrets/google_client_secret.json`.
- **Refresh token**: `jobhunter mail auth --podman-secret jobhunter_gmail_token` on the host
  (C7), weekly (the app stays in Testing). It checks podman works before consent, through the
  host-escape chain (`podman`, `flatpak-spawn --host podman`, `distrobox-host-exec podman`);
  pipes the token to `podman secret create --replace` on stdin; if that fails after consent,
  stores the token locally as today, with a warning, so consent is not lost. On success nothing
  is kept locally unless `--keep-local`. Then rerun preflight only if the secret is new.
- **Other mail commands** (`mail status`, `match`, `setup`, `sample`) run in a one-off
  container through `jobhunter-ctr mail ...` ([one-off commands](#one-off-commands));
  `mail sync` is part of the nightly run.
- Preflight warns while `~/.config/jobhunter/gmail_token.json` or `google_client_secret.json`
  still exist on the host and prints the `rm` line; it never deletes them.

### At rest

podman's default `file` driver keeps secrets base64-encoded in a 0600 file under
`~/.local/share/containers/storage/secrets/`; `podman secret inspect --showsecret` prints them.
That equals today's `~/.env` and `gmail_token.json`. What podman secrets add is keeping values
out of units, `inspect`, logs and image layers. C11 documents excluding that directory from home
backups and the `pass` or `shell` drivers for encryption at rest.

## Running it

### Quadlet

| File | Installed to | What |
|---|---|---|
| `deploy/quadlet/jobhunter-console.container` | `~/.config/containers/systemd/` | `ContainerName=jobhunter-console`, `PublishPort=127.0.0.1:8808:8808`, `Network=pasta:--host-lo-to-ns-lo`, `Exec=console`, `HealthCmd=` on `/healthz`, `Restart=on-failure`, `WantedBy=default.target` |
| `deploy/quadlet/jobhunter-ctr-backup.container` | same | `Exec=db backup --auto` |
| `deploy/quadlet/jobhunter-ctr-run.container` | same | `Exec=run --wait 4h`; `Requires=`/`After=jobhunter-ctr-backup.service` |
| `deploy/quadlet/jobhunter-ctr-collect.container` | same | `Exec=score --collect-pending --wait 2h`; `OnSuccess=jobhunter-ctr-rescore.service` |
| `deploy/quadlet/jobhunter-ctr-rescore.container` | same | `Exec=score --rescore-pending --wait 2h` |
| `deploy/quadlet/jobhunter-ctr-verify.container` | same | `Exec=sources verify --wait 2h` |
| `deploy/systemd/jobhunter-ctr-{run,collect,verify}.timer` | `~/.config/systemd/user/` | 02:00 (`RandomizedDelaySec=10m`), 03:30, Sun 04:00; `Persistent=true` |
| `deploy/systemd/jobhunter-ctr-notify@.service` | `~/.config/systemd/user/` | own copy of the notify unit, so `schedule uninstall` cannot remove the `OnFailure=` target |

Common to every `.container`: `Image=localhost/jobhunter:current`,
`ContainerName=jobhunter-ctr-<job>`, `UserNS=keep-id:uid=1000,gid=1000`,
`Volume=%h/.local/share/jobhunter:/data:z`, `Volume=%h/.cache/jobhunter:/cache:z`,
`Volume=%h/.config/jobhunter/config.toml:/config/config.toml:ro,z`,
`Environment=` for `JOBHUNTER_DATA_DIR=/data`, `JOBHUNTER_DB_PATH=/data/jobhunter.db`,
`JOBHUNTER_PROFILE_DIR=/data/profile`, `JOBHUNTER_RESUME_PATH=/data/resume`,
`JOBHUNTER_CACHE_DIR=/cache`, `Timezone=local`,
`ExecStartPre=/usr/bin/mkdir -p -m 0700 %h/.local/share/jobhunter %h/.cache/jobhunter`
(podman refuses a missing bind source; this runs on the host),
`OnFailure=jobhunter-ctr-notify@%n.service`. Oneshots: `Type=oneshot`, **no `[Install]`**;
only timers start them. No `Secret=` in base files (D9). `loginctl enable-linger $USER` keeps
timers running while logged out.

### One-off commands

C15. Preflight writes `~/.local/bin/jobhunter-ctr`, a short script that runs `podman run --rm
-it` on `localhost/jobhunter:current` with the run unit's mounts, user namespace and the
secrets in the `jobhunter-ctr` column. Use it for `mail status|match|setup|sample`,
`secrets status`, `sources list`, and anything else interactive after the switch.

### Compose (convenience, podman-compose only)

`compose.yaml`: a `console` service (`127.0.0.1:8808:8808`, `userns_mode:
keep-id:uid=1000,gid=1000`, the mounts above) and a `run` service under a `manual` profile.
`x-podman: in_pod: false` (podman refuses `--userns` with `--pod`). Secrets are `external: true`
podman secrets. Use `podman-compose` directly, not `podman compose`. No scheduler.

### Local LLM (deferred, C10)

Not in this round. First step if wanted: check on the host how a container reaches a
llama-server on host loopback under pasta.

## SQLite store

- One file, many processes, as on the host; WAL and file locks work across containers that
  bind-mount the same file on one kernel and a local filesystem. Never NFS/SMB or a VM share.
- Host backup tools must not copy the live `jobhunter.db`, `-wal`, `-shm`: back up `backups/`
  and exclude the live files, or call `jobhunter db backup` from a pre-backup hook.

### Run lock

C5, D12. `flock(LOCK_EX)` on one fd held for the whole job, on `<db_path>.run.lock`; pid, host,
command and start time are written through that same fd.

| Job | Lock held by another job |
|---|---|
| Timer-started `run`, `collect`, `rescore-pending`, `verify` (`--wait D`) | wait up to D; on timeout exit non-zero so `OnFailure=` fires. After downtime, `Persistent=true` fires several timers at once; each waits its turn instead of being skipped |
| Interactive `run`, `score`, `--rescore-pending` (no `--wait`) | exit 0 with "another jobhunter job is running (pid, host, since)"; nothing spent |
| Console Re-score now | takes the lock non-blocking for the paid part; if held, the panel says so and offers retry |
| Holder older than 6 h | exit 75 so a hung job is reported |

### Backups

C6 on top of C12's helper (online backup API, then `integrity_check`):

- `db backup --auto` (the `jobhunter-ctr-backup` oneshot, before every nightly run) writes
  `backups/auto/jobhunter-auto-YYYYMMDDTHHMMSSZ.db` (UTC, 0600) and keeps the newest backup of
  each of the last 7 days plus the newest of each of the last 4 ISO weeks, among files matching
  exactly `^jobhunter-auto-\d{8}T\d{6}Z\.db$` in `backups/auto/`. About 0.9 GB at today's 86 MB.
- C12 writes `backups/auto/pre-migrate-<from>-<to>-<UTC>Z.db` before any migration and keeps
  the newest 3 of that exact pattern; it skips the backup when the database is new (version 0).
- `db backup` without `--auto`, or `--to PATH`, writes a manual backup and never prunes.
  Nothing outside those two patterns in `backups/auto/` is ever deleted.

### Restore

`jobhunter db restore PATH` runs on the host only, with the console and timers stopped:

1. `systemctl --user stop jobhunter-console jobhunter-ctr-run.timer jobhunter-ctr-collect.timer
   jobhunter-ctr-verify.timer`; it waits for any running `jobhunter-ctr-*` service.
2. Takes the run lock (C5); backs up the current database (manual name).
3. Copies the backup into the live path **through the SQLite backup API** (never a rename over
   a WAL database); temp files inside the data directory, so they carry the right label.
4. `integrity_check`; prints row counts before and after.

Restoring an older-schema backup in a container deployment leaves it at that schema: nothing
migrates it until `jobhunter upgrade` ([below](#version-skew)).

### Version skew

C12, C15, D15. The host checkout moves forward on every merge; the image moves when rebuilt.

- **Guard, on every connection** (the console opens one per request): code refuses a database
  that holds a migration it does not know: "database is newer than this jobhunter (code has N,
  database has M)". An old console stops writing the moment the schema moves.
- **Deployment marker.** Preflight writes `<data dir>/deployment.toml` (`mode = "container"`)
  when you switch. While it exists, **no command migrates on its own**, on the host or in a
  container. A command whose code has pending migrations refuses: "run `jobhunter upgrade`".
  Without the marker (host-only installs), migration stays automatic, now with the
  pre-migrate backup.
- **Exempt** (neither migrate nor refuse on pending migrations; still refuse a newer database
  where they would write): `version`, `paths`, `secrets status`, `container preflight`,
  `db backup`, `db restore`, `upgrade`, `db migrate`.
- **`jobhunter upgrade`** (host, C15): clean checkout check, stop console and timers, wait for
  running jobs, backup, build `localhost/jobhunter:<commit>` and retag `:current`,
  `jobhunter-ctr db migrate`, restart the console, re-enable timers, print `jobhunter version`
  on both sides. A failure stops and prints how to restore the pre-migrate backup and retag the
  previous image. `--dry-run` prints the steps.
- **Rollback**: restore a pre-migrate backup and retag the previous image. Because nothing
  migrates on its own, the next host command refuses instead of re-migrating; check out the
  matching commit or run `upgrade` again when ready.

### SELinux

`:z` labels the bind-mounted directories `container_file_t`. Files created later inherit it;
files **moved** in keep their old label and the containers get EACCES. Copy instead, or run the
`chcon` command preflight prints for any file under the data or cache directory without
`container_file_t`. `restorecon -R ~/.local/share` resets the label; podman relabels at the next
start, but a running console gets EACCES until restarted. Optional, documented by C11:
`semanage fcontext -a -t container_file_t '<home>/.local/share/jobhunter(/.*)?'`.

## Moving to containers

On the host:

1. `jobhunter db backup` and `jobhunter db relativize-paths --apply` (C14).
2. `jobhunter schedule uninstall`.
3. Create the podman secrets you use; `jobhunter mail auth --podman-secret
   jobhunter_gmail_token` if you use Gmail.
4. `podman build --build-arg BUILD_COMMIT=$(git rev-parse HEAD) -t
   localhost/jobhunter:$(git rev-parse --short HEAD) . && podman tag
   localhost/jobhunter:$(git rev-parse --short HEAD) localhost/jobhunter:current`
5. `cp deploy/quadlet/*.container ~/.config/containers/systemd/` and
   `cp deploy/systemd/* ~/.config/systemd/user/`.
6. `jobhunter container preflight` (C13): creates missing directories (0700) and an empty
   `config.toml` if absent; checks ownership, labels, `.env` modes; refuses while host timers
   are enabled, absolute paths remain in the DB, or host and image versions differ; writes the
   drop-ins, `jobhunter-ctr` and the deployment marker; prints the remaining commands.
7. `systemctl --user daemon-reload`, start the console, enable the timers
   ([Host verification](#host-verification)).

Going back to the host install: `jobhunter container preflight --leave` removes the marker,
drop-ins and wrapper (after stopping the units), then `jobhunter schedule install`.

## Drafting

D16, C9. In container mode the packet page does not draft. It shows the host command,
`jobhunter apply draft <packet_id>`, with a copy button, and refreshes when the draft lands
(the host writes to the same database; paths are relative, D18). The container never calls the
SDK for drafting. 017 owns `apply draft` and the `claude -p` environment allowlist; 018 relies on
both.

## The 017 Chrome extension

The extension talks to `http://127.0.0.1:8808`. The container provides: `127.0.0.1:8808`
published, IPv4 only (the extension uses `127.0.0.1`, not `localhost`); the same app code, so
the C1 `Host` check on every request and the `Origin` rules, never `--allow-remote`; packet
files resolved from relative paths (D18); rendered PDFs once the browser image exists (D14).

## Testing

Workers run without a podman runtime. They can run pytest, ruff, `uv build --wheel` in a temp
directory holding only the copied files, and, when present, `/usr/libexec/podman/quadlet -dryrun
-user` with `QUADLET_UNIT_DIRS` and `systemd-analyze --user verify` (tests set a short
`XDG_RUNTIME_DIR` under `/tmp` for it; both skip when the binary is missing).

| Layer | Where | What |
|---|---|---|
| Unit | pytest, sandbox | C1 container defaults and refusals, `Host` check on GET, `/healthz`; C2 values ending `\r\n`, ` \n`, `\n\n`, `\t`, empty placeholder treated as unset, `.env` versus `N_FILE`, one invalid value not stopping `secrets status`, a fake value absent from all output, logs and stored errors; C3 Containerfile rules and a fixture tree for `.containerignore`; C5 two processes with a counting mock scorer, `--wait`, stale holder, console path; C6 7+4 pruning on a fixture directory holding `-pre-jev`-shaped manual names and `pre-migrate-*` files; C7 fake `podman` (argv versus stdin, missing binary, non-zero exit); C12 guard per connection, marker blocks auto-migrate, exempt list, pre-migrate backup and version-0 skip; C13 drop-ins written and removed, foreign drop-ins untouched; C14 `/home` versus `/var/home` symlinked twin; C15 command order and rollback output with fakes |
| Generator | pytest, sandbox when `quadlet` exists (C4) | dry run succeeds; names; no `[Install]` on oneshots; nothing `Wants=` a `jobhunter-ctr-*` oneshot; no `Secret=` in base files; publish `127.0.0.1` only; no `.timer`/`.service` under `deploy/quadlet/`; timers match the host schedule |
| Image smoke | GitHub Actions (C8) | build; size; with `--network=none` and explicit `--name`: `--help`, `id -u` = 1000, `paths` container mode, `init` on a fresh volume, `/healthz` 200 from inside, fake secret ending `\r\n` resolves and is absent from `podman inspect` and `podman logs` (each check fails if podman fails), no personal or token files in the image; hadolint |
| Host | you, below | SELinux, keep-id, pasta loopback, real secrets, timers |

No step contacts a job board or a paid API; CI has no real secrets.

### Host verification

Run on the host after C3, C4, C13, C14 and C15 land. Expected output in comments.

```
jobhunter upgrade --dry-run                              # lists build, tag, migrate, restart steps
podman run --rm localhost/jobhunter:current paths        # first line: mode: container ; data_dir /data
podman run --rm --entrypoint id localhost/jobhunter:current -u   # 1000
jobhunter container preflight                            # every line ok; drop-ins and wrapper written
/usr/libexec/podman/quadlet -dryrun -user | grep '^---'  # console plus six -ctr- services
/usr/libexec/podman/quadlet -dryrun -user | grep -c 'Wants=jobhunter-ctr'   # 0
systemctl --user daemon-reload
systemctl --user start jobhunter-console
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8808/healthz      # 200 (000: use the D11 fallback)
curl -s -o /dev/null -w '%{http_code}\n' -H 'Host: evil.example' http://127.0.0.1:8808/   # 403
ss -ltn | grep 8808                                      # 127.0.0.1:8808 only
podman ps -a --filter name=jobhunter-ctr --format '{{.Names}}'   # empty: no oneshot was created
journalctl --user -u 'jobhunter-ctr-*' -b --no-pager | grep -c Started   # 0
ls -lZ ~/.local/share/jobhunter/jobhunter.db             # owner you, -rw-------, container_file_t:s0
podman exec jobhunter-console jobhunter secrets status   # set (file ...) or unset; no values
podman inspect jobhunter-console >/dev/null && podman inspect jobhunter-console | grep -c -e sk-ant -e sk-or   # 0
podman exec jobhunter-console sh -c 'ls /config /run/secrets'   # config.toml; scorer keys only
jobhunter-ctr mail status                                # token found (file /run/secrets/...)
systemctl --user enable --now jobhunter-ctr-run.timer jobhunter-ctr-collect.timer jobhunter-ctr-verify.timer
systemctl --user list-timers 'jobhunter-ctr-*'           # three timers with next run times
jobhunter run                                            # host: refuses or runs only at the same schema; never migrates
```

If `/healthz` prints `000`, switch the console unit to the D11 fallback (`Exec=console --host
0.0.0.0`, `Environment=JOBHUNTER_PUBLISHED_LOOPBACK_ONLY=1`) and report it so the spec records
which works.

## Docs

C11 writes `docs/containers.md`: build and tags, secrets (file or stdin, never argv; rerun
preflight, daemon-reload and restart after any change; at-rest property; `pass`/`shell`
drivers; excluding podman's store from backups; `chmod 600 ~/.env`), the switch and the way
back, Quadlet and podman-compose, `jobhunter-ctr`, `jobhunter upgrade` and rollback, backups
(host tools back up `backups/` only), restore, SELinux (`cp` not `mv`, `chcon`, `semanage`),
weekly Gmail re-auth, and the host verification above. It amends 002 *Where files live* and
*Process model* to point here.

## Phased tickets

All labelled `area:containers`; model per CLAUDE.md (reviewer one tier stronger).

| Phase | Ticket | Id | Model | Effort | Depends on |
|---|---|---|---|---|---|
| 1 | C1 Container mode, `Host` check on every request, `/healthz`, D11 interlock | `5d7d73e` | sonnet | 0.5-1 d | |
| 1 | C5 Run lock (`flock`, `--wait`) | `b4c13eb` | opus | 0.5-1 d | |
| 1 | C12 Schema guard per connection, deployment marker, no auto-migrate, backup helper | `ef185ba` | opus | 1 d | |
| 1 | C14 Data-dir-relative paths in the DB | `6da0aac` | opus | 1 d | C12 (backup helper) |
| 1b | C2 `*_FILE` secrets, validation, `secrets status` | `efb6104` | sonnet | 0.5-1 d | C1 (same lines; serial) |
| 1b | C6 Automatic backups (7+4) and safe restore | `5a2063c` | opus | 0.5-1 d | C5, C12 |
| 2 | C3 Containerfile and `.containerignore` | `e591f11` | sonnet | 0.5 d | C1 |
| 2 | C7 Gmail token as a podman secret, host only | `257edaa` | sonnet | 0.5-1 d | C1, C2 |
| 2 | C13 Preflight, drop-ins, marker, `.env` mode check | `9a1e1b7` | sonnet | 1 d | C1, C2, C12, C14 |
| 2 | C15 `jobhunter upgrade` and `jobhunter-ctr` | `94b1613` | opus | 1 d | C12, C13 |
| 3 | C4 Quadlet units, systemd timers, compose.yaml | `1cbc6b9` | sonnet | 1 d | C1, C2, C3, C5, C6, C7, C12, C13, C14 |
| 3 | C8 CI image build and smoke test | `7b24373` | sonnet | 0.5-1 d | C1, C2, C3 |
| 4 | C11 Docs | `166a4c1` | haiku | 0.5 d | C4, C6, C7, C13, C15 |
| with 017 | C9 Container console defers drafting to the host | `9910432` | sonnet | 0.25 d | C1, 017 `apply draft` |
| deferred | C10 Local LLM access from containers | `0adb58b` | sonnet | 0.5-1 d | C4; only if wanted |

About 10-13 days of worker time. Phase 1 changes nothing about how you run jobhunter except
the safety fixes listed at the top.

## Open questions

None blocking. Two small ones:

1. **Pre-migrate backups**: keep the newest 3 (decided here)? Say if you want more.
2. **`jobhunter upgrade` and dirty checkouts**: it refuses on uncommitted changes. OK, or allow
   `--allow-dirty` for local experiments (the image would then not match any commit)?

## History

### Revision 3: review round 2 and your answers

A second Opus review (2026-10-10) found 8 of the 10 earlier blocking themes resolved, 2 partly,
and 3 new blocking issues; you answered the open questions the same day. All taken:

- **Version skew**: guard on every connection; a deployment marker stops all automatic
  migration in a container deployment; exempt commands listed; `jobhunter upgrade` (new C15)
  backs up, builds, migrates and restarts in order; restored backups are not re-migrated (D15).
- **Paths**: "same absolute path" was wrong (`/home` versus `/var/home`, `.resolve()`d rows).
  The DB now stores data-dir-relative paths (new C14, D18), so data mounts at `/data` again;
  uploads are copied into the data dir (your answer).
- **Drafting**: the command is 017's `jobhunter apply draft <packet_id>`; container mode never
  drafts, it shows that host command (your answer). C9 back to sonnet: no paid path left in it;
  its container fake-claude test is dropped (017 tests the environment allowlist).
- **Minor**: 018 authoritative for container networking and mounts (noted on `7fa29db`); C1 owns
  the all-routes `Host` check and `/healthz`; preflight removes its own stale drop-ins, docs say
  rerun preflight, daemon-reload, restart; drop-in variable names from config; mail commands via
  `jobhunter-ctr`; pre-migrate backups pruned to 3 and skipped at version 0; C2 order (`.env`
  first, empty means unset, invalid values dropped with a warning, `secrets status` never
  aborts); timer jobs wait for the lock so catch-up runs are not skipped; `mkdir -m 0700`; short
  `XDG_RUNTIME_DIR` for `systemd-analyze`; the host-change list now includes C1 and C14;
  `.env` mode warning; stronger host checks (`podman ps -a`, journal); C12 owns the backup
  helper and C6 reuses it.
- **Your answers**: Quadlet supported, podman-compose convenience; bind mounts; Gmail stays in
  Testing with weekly re-auth; local builds only (`:current` tag); browser image when 017 phase
  2 starts; 7 daily + 4 weekly backups. Answered open questions removed.

### Revision 2: adversarial review

An Opus review (2026-10-10) raised 18 blocking findings (about 11 distinct) and 32 minor ones;
all were taken. Only `config.toml` is mounted; `*_FILE` values are stripped and validated;
per-unit secret drop-ins; no pod; timers and a renamed notify unit in `~/.config/systemd/user`;
`ContainerName=`; collect then rescore via `OnSuccess=`; a backup oneshot; `ExecStartPre`
creates bind sources; podman-compose with `in_pod: false`; schema guard (C12); automatic
backups in `backups/auto/` with exact patterns; restore through the backup API; `flock`;
`README.md` in the image; `**/` ignore patterns; loopback bind via pasta with an explicit
fallback; Host check on every request; host-only Gmail auth; C6 split (C13), `import-volume`
dropped, C10 deferred. Sketch units passed `quadlet -dryrun -user` (podman 5.8.4) in the agent
sandbox.
