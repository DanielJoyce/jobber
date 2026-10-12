# Running jobhunter in rootless podman

For one user on Fedora atomic with SELinux enforcing. The design and the reasons live in
[specs/018-containers.md](../../../../specs/018-containers.md); this page is the how-to. Container mode
is opt-in: `uv run jobhunter ...` and `jobhunter schedule install` keep working if you never
switch.

Every `jobhunter ...` command in a code block here is checked against the real command line by
`tests/test_docs_containers.py` and the help drift tests, so a renamed command or flag fails the tests. Expected output
is what the code prints; paths and numbers differ on your machine.

Run everything on the **host**, in the checkout, unless a step says otherwise. A line starting
with `jobhunter` means `uv run jobhunter` from the checkout (or the installed script).

## First run checklist

Run these once, in order. Stop at the first line that does not print what is shown and look in
[Troubleshooting](#troubleshooting). Lines marked optional are for features you may not use; skip those. Keys
are typed at a hidden prompt and piped on stdin: never on the command line, never in a file.

```
podman --version                                        # podman version 5.x (5.0 or newer)
loginctl enable-linger "$USER"                          # no output
loginctl show-user "$USER" --property=Linger            # Linger=yes
jobhunter db backup                                     # a backup file under backups/
jobhunter db relativize-paths                           # dry run: lists paths, changes nothing
jobhunter db relativize-paths --apply                   # backs up first, then rewrites
jobhunter schedule uninstall                            # host timers removed
podman build --build-arg BUILD_COMMIT=$(git rev-parse HEAD) -t localhost/jobhunter:$(git rev-parse --short HEAD) .
podman tag localhost/jobhunter:$(git rev-parse --short HEAD) localhost/jobhunter:current
read -rs KEY && printf '%s' "$KEY" | podman secret create jobhunter_anthropic - && unset KEY   # optional: paste key, Enter; prints a secret id
read -rs KEY && printf '%s' "$KEY" | podman secret create jobhunter_openrouter - && unset KEY   # optional (OpenRouter scorer)
read -rs KEY && printf '%s' "$KEY" | podman secret create jobhunter_usajobs_key - && unset KEY  # optional (USAJOBS source)
read -rs KEY && printf '%s' "$KEY" | podman secret create jobhunter_usajobs_email - && unset KEY  # optional (USAJOBS source)
podman secret create jobhunter_google_client ~/.config/jobhunter/google_client_secret.json   # optional (Gmail)
jobhunter mail auth --podman-secret jobhunter_gmail_token   # optional (Gmail): browser consent, then the secret is stored
mkdir -p ~/.config/containers/systemd ~/.config/systemd/user ~/.local/bin
cp deploy/quadlet/*.container ~/.config/containers/systemd/
cp deploy/systemd/* ~/.config/systemd/user/
jobhunter container preflight --dry-run                 # every line ok or warn; no FAIL
jobhunter container preflight --yes                     # same, then writes drop-ins and the marker
jobhunter ctr-script --write                            # wrote ~/.local/bin/jobhunter-ctr (...)
systemctl --user daemon-reload                          # no output
systemctl --user start jobhunter-console                # no output
curl -s http://127.0.0.1:8808/healthz                   # {"ok":true,"schema_version":N}
curl -s -o /dev/null -w '%{http_code}\n' -H 'Host: evil.example' http://127.0.0.1:8808/   # 403
ss -ltn | grep 8808                                     # 127.0.0.1:8808 only
systemctl --user enable --now jobhunter-ctr-run.timer jobhunter-ctr-collect.timer jobhunter-ctr-verify.timer
systemctl --user list-timers 'jobhunter-ctr-*'          # three timers, each with a next run time
jobhunter-ctr secrets status                            # set (file ...) or unset per name; no values
```

The sections below explain each step. If you have never run jobhunter, run `jobhunter init`
first: preflight fails when the database has no schema.

## Prerequisites

- podman 5 or newer. Quadlet applies `<unit>.container.d/*.conf` drop-ins, which the per-secret
  files rely on, only from podman 5. Check with `podman --version`.
- `loginctl enable-linger $USER`, so the user manager, the console and the timers keep running
  while you are logged out. `loginctl show-user "$USER" --property=Linger` prints `Linger=yes`.
- The checkout with a working `uv sync`, for the host-side commands.
- A database that exists (`jobhunter init` once if you have never run jobhunter).

Check your layout any time:

```
jobhunter paths
jobhunter version
```

The units mount exactly `~/.local/share/jobhunter` (data), `~/.cache/jobhunter` (cache) and the
single file `~/.config/jobhunter/config.toml`. A `[paths]` override or `JOBHUNTER_DATA_DIR`
pointing elsewhere makes preflight FAIL until you drop it. The config directory itself is never
mounted, because it also holds the Gmail token and client JSON.

## Build the image

```
podman build --build-arg BUILD_COMMIT=$(git rev-parse HEAD) -t localhost/jobhunter:$(git rev-parse --short HEAD) .
podman tag localhost/jobhunter:$(git rev-parse --short HEAD) localhost/jobhunter:current
```

The units run `localhost/jobhunter:current`; the `<commit>` tag stays so you can roll back.
Nothing is pushed anywhere. The image holds no secrets and no personal data.

The `Containerfile` pins its base images by digest. To pick up newer bases, run
`scripts/pin-image-digests.sh` (needs the network and `skopeo` or `podman`), review the diff to
`Containerfile`, commit it as a `chore:` change, and rebuild. `scripts/pin-image-digests.sh
--check` exits 1 while any all-zero placeholder digest remains.

Check the result: the first command's first line says `mode: container`, the second prints
`1000`.

```
podman run --rm localhost/jobhunter:current paths
podman run --rm --entrypoint id localhost/jobhunter:current -u
```

## Secrets

podman secrets keep keys out of unit files, `podman inspect`, logs and image layers. Create one
per key you use, on stdin. Never put a key on the command line (shell history, `ps`) and
never write it to a file (least of all in the checkout; `shred` does nothing useful on btrfs).
`read -rs` takes the key at a hidden prompt, so it stays out of shell history too. The Google
client JSON is already a file, so `podman secret create` reads it from where it is.

| podman secret | Holds | Used by |
|---|---|---|
| `jobhunter_anthropic` | `ANTHROPIC_API_KEY` | console, run, collect, rescore, `jobhunter-ctr` |
| `jobhunter_openrouter` | the OpenRouter key | console, run, rescore, `jobhunter-ctr` |
| `jobhunter_openai_compat` | the OpenAI-compatible key | console, run, rescore, `jobhunter-ctr` |
| `jobhunter_llama` | the local-server key | console, run, rescore, `jobhunter-ctr` |
| `jobhunter_usajobs_key`, `jobhunter_usajobs_email` | USAJOBS credentials | run, verify, `jobhunter-ctr` |
| `jobhunter_google_client` | `google_client_secret.json` | run, `jobhunter-ctr` |
| `jobhunter_gmail_token` | the Gmail refresh token | run, `jobhunter-ctr` |

Each container gets only the secrets in its row: preflight writes one Quadlet drop-in per secret
that exists.

```
read -rs KEY && printf '%s' "$KEY" | podman secret create jobhunter_openrouter - && unset KEY
<password-manager-command> | podman secret create jobhunter_openrouter -
```

A trailing newline in a file is harmless: jobhunter strips leading and trailing whitespace. A
value with whitespace or a control character inside is rejected as invalid, and `secrets
status` says so by variable name and reason, never by value. To replace a secret, `podman
secret rm NAME` and create it again (Gmail uses `--replace` itself, below).

**After any secret change (create, replace, remove):**

```
jobhunter container preflight --yes
systemctl --user daemon-reload
systemctl --user restart jobhunter-console
```

Oneshot jobs pick the change up at their next run; a running console only after the restart.

Check what is set, without printing values:

```
jobhunter secrets status
jobhunter-ctr secrets status
```

The first reads the host's environment and `.env` files, the second what a container sees. Each
name is `set (env)`, `set (file PATH)`, `set (.env PATH)`, `invalid (reason)` or `unset`.

### Caveats at rest

podman's default driver stores secrets base64-encoded (not encrypted) in a mode 0600 file under
`~/.local/share/containers/storage/secrets/`, and `podman secret inspect --showsecret` prints
them. That is no stronger than `~/.env` or a 0600 token file; what you gain is keeping values out
of units, `inspect`, logs and images. So:

- Exclude `~/.local/share/containers/storage/secrets/` from home-directory backups.
- For encryption at rest, podman's `pass` or `shell` secret drivers can be configured (see `man
  containers.conf`, `[secrets]`); jobhunter works with any driver.
- Make any `.env` file private: `chmod 600 ~/.env`. Preflight and `secrets status` warn while a
  `.env` file they read is group- or world-readable.

## Gmail

Gmail auth runs on the host only; the container has no browser or keyring. The Google app stays
in Testing mode, so the refresh token expires after 7 days. Repeat one command weekly:

```
jobhunter mail auth --podman-secret jobhunter_gmail_token
```

It checks that podman works before asking for consent, runs the browser consent, pipes the token
to `podman secret create --replace` on stdin, and keeps no local copy unless you add
`--keep-local`. The client JSON (`~/.config/jobhunter/google_client_secret.json`) stays on the
host because this command needs it; the containers get a copy through the
`jobhunter_google_client` secret. If podman fails after you consented, the token is stored
locally with a warning, so the consent is not lost. Move it into the secret later without new
consent:

```
jobhunter mail auth --podman-secret jobhunter_gmail_token --from-local
```

Rerun preflight (plus daemon-reload and restart, as above) only the first time, when the secret
is new. Preflight warns while `~/.config/jobhunter/gmail_token.json` still exists next to a
working secret and prints the `rm` line; it never deletes it. On a machine without a browser,
`--manual` and `--no-browser` work as on the host install.

## Move the data over: relative paths

The database used to store absolute file paths (`/home/<you>/...`, which on Fedora atomic is
really `/var/home/<you>/...`), and those cannot match inside a container. Convert them:

```
jobhunter db backup
jobhunter db relativize-paths
jobhunter db relativize-paths --apply
```

The first writes a backup, the second is a dry run that lists every path and what it becomes,
the third backs up again and rewrites. Files outside the data directory are copied into
`<data dir>/attachments/` after a sha256 check. Rows that name a missing file are reported and
left alone, and preflight refuses to switch while any absolute path remains.

## Install the units

```
mkdir -p ~/.config/containers/systemd ~/.config/systemd/user ~/.local/bin
cp deploy/quadlet/*.container ~/.config/containers/systemd/
cp deploy/systemd/* ~/.config/systemd/user/
```

Two directories, two kinds of file. Quadlet `.container` files go in
`~/.config/containers/systemd/` (podman generates services from them). The `.timer` files and
the notify service go in `~/.config/systemd/user/`. The base `.container` files carry no secrets;
preflight adds the drop-ins.

The set: `jobhunter-console` (the web console, published on `127.0.0.1:8808` only) and the
oneshots `jobhunter-ctr-backup`, `-run`, `-collect`, `-rescore`, `-verify`. Oneshots have no
`[Install]`. Three timers start the jobs (run 02:00, collect 03:30, verify Sunday 04:00; a
missed run catches up, `Persistent=true`). The run unit pulls in `jobhunter-ctr-backup` first
(`Requires=`), and a successful collect starts `jobhunter-ctr-rescore` (`OnSuccess=`); backup and
rescore have no timer of their own. A `compose.yaml` exists as a convenience for `podman-compose`
(not `podman compose`, which delegates to docker-compose and refuses external secrets). It has
no scheduler; use Quadlet for everyday running.

## Preflight

```
jobhunter container preflight --dry-run
jobhunter container preflight --yes
jobhunter container preflight
```

`--dry-run` checks everything and writes nothing. `--yes` also writes the deployment marker
without asking; plain `container preflight` asks (and on a non-terminal, skips it and says to
rerun with `--yes`). Each line starts with `ok`, `warn` or `FAIL`; any FAIL means nothing is
written and the exit status is 1. Typical success output:

```
ok    podman reachable (podman)
ok    data dir /var/home/<you>/.local/share/jobhunter exists
ok    config file /var/home/<you>/.config/jobhunter/config.toml exists
ok    SELinux: data dir not labelled container_file_t yet (podman labels it at the first start, :z)
ok    no host timers from `jobhunter schedule install` are enabled
ok    every path stored in the database is relative to the data dir
ok    host code and image agree on schema 29
ok    database at schema 29
ok    base units found in /var/home/<you>/.config/containers/systemd
ok    podman secrets found: jobhunter_openrouter
ok    not created (their drop-ins are skipped): jobhunter_anthropic, jobhunter_llama, ...
ok    quadlet -dryrun -user generates every jobhunter unit with these drop-ins
ok    wrote /var/home/<you>/.config/containers/systemd/jobhunter-console.container.d/50-jobhunter_openrouter.conf
ok    wrote /var/home/<you>/.config/containers/systemd/jobhunter-ctr-run.container.d/50-jobhunter_openrouter.conf
ok    wrote /var/home/<you>/.config/containers/systemd/jobhunter-ctr-rescore.container.d/50-jobhunter_openrouter.conf
ok    wrote deployment marker /var/home/<you>/.local/share/jobhunter/deployment.toml: no command migrates on its own now
next:
  systemctl --user daemon-reload
  systemctl --user restart jobhunter-console
  systemctl --user enable --now jobhunter-ctr-run.timer jobhunter-ctr-collect.timer jobhunter-ctr-verify.timer
```

What each FAIL means:

| FAIL says | Meaning | Fix |
|---|---|---|
| `podman is not reachable` | no `podman` (it also tries `flatpak-spawn --host` and `distrobox-host-exec`) | install podman, or run on the host |
| `host timers still enabled` | `jobhunter schedule install` timers would run beside the containers | `jobhunter schedule uninstall` |
| `absolute or escaping path(s) in the database` | stored paths a container cannot open | `jobhunter db backup`, then `jobhunter db relativize-paths --apply` |
| `cannot read the image's version` | `localhost/jobhunter:current` missing or broken | build and tag the image |
| `schema skew` | host code and image carry different migrations | rebuild the image from this checkout |
| `database ... is at schema N, behind the code` | pending migration, and nothing migrates on its own after the switch | `jobhunter db migrate` |
| `database ... has no schema yet` | a brand-new database | `jobhunter init` |
| `host data dir is ... but the units mount ...`, `host database is ...` | a path override differs from the fixed mounts | drop the `[paths]` / `JOBHUNTER_DATA_DIR` / `db_path` override, or move the data |
| `... is owned by uid N, not you` | the containers map only your uid | `chown` it back to you |
| `base units not installed in ...` | the `.container` files are not copied | the `cp deploy/quadlet/*.container ...` line above |
| `quadlet -dryrun -user rejects the result` | the generator found a problem in the units or drop-ins | read the quoted reason and fix the unit file |
| `the config the units mount cannot be loaded` | `~/.config/jobhunter/config.toml` does not parse | fix the TOML |

Warnings worth acting on: `SELinux: N file(s) ... are not container_file_t` (see
[SELinux](#selinux)), a group- or world-readable `.env`, a leftover Gmail token file, and an
image built without a commit. Do not start the container units before the deployment marker
exists, or host and containers would both migrate on their own.

Preflight creates missing data and cache directories (mode 0700) and an empty `config.toml`.
It does not install `jobhunter-ctr`; the next section does.

### The jobhunter-ctr wrapper

```
jobhunter ctr-script
jobhunter ctr-script --write
```

The first prints the script; the second installs it as `~/.local/bin/jobhunter-ctr` (mode 0755)
and prints `wrote ... (mounts whichever of these exist: ...)`. The wrapper runs one command in
a throwaway container on `localhost/jobhunter:current` with the run unit's mounts, and mounts
each secret only if it exists when you run it, so it never needs rerunning after a secret
change. Use it for anything else after the switch:

```
jobhunter-ctr mail status
jobhunter-ctr mail match
jobhunter-ctr sources list
jobhunter-ctr secrets status
```

Drafting stays on the host. The container has no `claude` CLI and no SDK drafting, and the
console refuses a drafting request with 409. On a packet page the console shows the host
command to copy instead. First save what the draft should use, in the console: **Save notes
only** stores employer notes and **Save story facts** stores story facts; the host draft reads
both. Then run, in a host terminal:

```
jobhunter apply draft 42
jobhunter apply draft 42 --letter
jobhunter apply draft 42 --question -
jobhunter apply draft 42 --question-file ./question.txt
jobhunter apply draft 42 --letter --instruction "shorter"
```

The first drafts the resume, `--letter` the cover letter. `--question -` reads the custom
question from stdin (paste it, then press Ctrl-D), which avoids shell quoting problems;
`--question-file PATH` reads it from a file. Per-run instructions go on the host with
`--instruction`; the console hides those fields in container mode.

(`42` stands for a packet id.) The host-only commands (they refuse inside a container): `mail auth`, `schedule install|status|uninstall`,
`migrate-paths`, `apply draft`, `upgrade`, `ctr-script`, `db relativize-paths`, `db restore` and
`container preflight`.

## Start it and verify

```
systemctl --user daemon-reload
systemctl --user start jobhunter-console
```

Then, on the host:

```
curl -s http://127.0.0.1:8808/healthz
curl -s -o /dev/null -w '%{http_code}\n' -H 'Host: evil.example' http://127.0.0.1:8808/
ss -ltn | grep 8808
podman ps -a --filter name=jobhunter-ctr --format '{{.Names}}'
ls -lZ ~/.local/share/jobhunter/jobhunter.db
podman exec jobhunter-console jobhunter secrets status
podman exec jobhunter-console sh -c 'ls /config /run/secrets'
```

Expected:

- `healthz` prints `{"ok":true,"schema_version":N}`. `000` means the connection failed: see the
  pasta fallback in [Troubleshooting](#troubleshooting).
- The foreign `Host` request prints `403`.
- `ss` shows `127.0.0.1:8808` and nothing on `0.0.0.0:8808` or `[::]:8808`.
- `podman ps -a` prints nothing: no oneshot container exists until its timer fires.
- The database is owned by you, mode `-rw-------`, label `container_file_t:s0`.
- `/config` lists `config.toml`; `/run/secrets` lists only the console's scorer keys.

Then enable the timers and look at them:

```
systemctl --user enable --now jobhunter-ctr-run.timer jobhunter-ctr-collect.timer jobhunter-ctr-verify.timer
systemctl --user list-timers 'jobhunter-ctr-*'
```

`list-timers` shows three timers, each with a NEXT time (about 02:00, 03:30 and Sunday 04:00).
Logs: `journalctl --user -u jobhunter-console -n 50` and
`journalctl --user -u 'jobhunter-ctr-*' -b`. A failing job fires `OnFailure=` and sends a
desktop notification through the notify unit.

## Backups

Automatic backups run before every nightly run (the `jobhunter-ctr-backup` unit runs
`db backup --auto`). They live in `~/.local/share/jobhunter/backups/auto/` as
`jobhunter-auto-YYYYMMDDTHHMMSSZ.db` (UTC, mode 0600) and keep the newest backup of each of the
last 7 days plus the newest of each of the last 4 ISO weeks. Migrations also write
`pre-migrate-<from>-<to>-<UTC>Z.db` there, keeping the newest 3. Pruning touches only files
with those exact names, so your own backups are never deleted.

Make a manual backup any time; it is safe while the console runs and is never pruned:

```
jobhunter db backup
jobhunter db backup --to /path/to/backups/
```

Manual backups land in `backups/jobhunter-<UTC>Z.db`. `jobhunter upgrade` adds
`backups/jobhunter-<stamp>-pre-upgrade.db`.

**Host backup tools must not copy the live database** (`jobhunter.db`, `-wal`, `-shm`): a copy
taken mid-write can be corrupt. Back up the `backups/` directory only and exclude the live
files, or call `jobhunter db backup --to ...` from a pre-backup hook and copy that file.

## Restore

Restore runs on the host only. Stop the console and the timers first, and let any running
`jobhunter-ctr-*` job finish (the second command lists active ones):

```
systemctl --user stop jobhunter-console jobhunter-ctr-run.timer jobhunter-ctr-collect.timer jobhunter-ctr-verify.timer
systemctl --user list-units 'jobhunter-ctr-*' --state=active
jobhunter db restore ~/.local/share/jobhunter/backups/auto/jobhunter-auto-20261010T020000Z.db
```

Restore takes the run lock, saves the current database as
`backups/jobhunter-<UTC>Z-pre-restore.db`, writes the backup in through SQLite's backup API
(never a file rename over a WAL database), runs `integrity_check`, and prints row counts before
and after. It refuses a backup newer than this code, and refuses while it sees the console or
another process using the database (`--force` overrides; do not). An older-schema backup stays
at its old schema until you run `jobhunter upgrade`. Then start things again:

```
systemctl --user start jobhunter-console
systemctl --user start jobhunter-ctr-run.timer jobhunter-ctr-collect.timer jobhunter-ctr-verify.timer
```

## Upgrade and rollback

The checkout moves forward on every merge; the image only when you rebuild. After updating the
code:

```
jobhunter upgrade --dry-run
jobhunter upgrade
```

`--dry-run` prints every step and command and runs nothing. The real run refuses a checkout with
uncommitted changes in the image's inputs (`Containerfile`, `.containerignore`, `pyproject.toml`,
`uv.lock`, `README.md`, `src/`) unless you add `--allow-dirty`, which tags the build
`<commit>-dirty`. In order, it:

1. builds `localhost/jobhunter:<commit>` first, so a failed build changes nothing;
2. stops the timers and waits (up to `--wait`, default 30m) for running `jobhunter-ctr-*` jobs;
3. takes the run lock, stops the console, and checks nothing else has the database open;
4. backs up to `backups/jobhunter-<stamp>-pre-upgrade.db` (never pruned);
5. retags `:current`, runs `jobhunter db migrate` in a one-off container, and checks that host
   and image report the same version;
6. starts again only the units that were active, and prints `jobhunter version` for both sides.

If the wait times out, a lock is held, or you press Ctrl-C, it starts back what it stopped and
changes nothing. If it fails after stopping things, the console and timers stay stopped and it
prints a **Rollback** block (retag the previous image, `jobhunter db restore` the pre-upgrade
backup, start the units). A rerun keeps the first rollback point (`upgrade-pending.json` in the
data directory). Done by hand, the rollback is:

```
systemctl --user stop jobhunter-console
podman tag localhost/jobhunter:<previous-commit> localhost/jobhunter:current
jobhunter db restore ~/.local/share/jobhunter/backups/jobhunter-<stamp>-pre-upgrade.db
systemctl --user start jobhunter-console
```

`<previous-commit>` is listed by `podman images localhost/jobhunter`. Nothing migrates on its
own, so after a rollback the next host command refuses rather than re-migrating; check out the
matching commit, or run `jobhunter upgrade` again when ready. `jobhunter version` shows the
code, migration and database versions without writing anything.

## Leave container mode

```
systemctl --user disable --now jobhunter-ctr-run.timer jobhunter-ctr-collect.timer jobhunter-ctr-verify.timer
systemctl --user stop jobhunter-console
jobhunter container preflight --leave
rm ~/.config/containers/systemd/jobhunter-*.container
rm ~/.config/systemd/user/jobhunter-ctr-*
systemctl --user daemon-reload
jobhunter schedule install --enable
```

Order matters. Disable the timers and stop the console first: `--leave` refuses while any
container unit is active or any container timer is enabled. It then removes the drop-ins
preflight wrote and `deployment.toml`, so host commands migrate on their own again. Removing the
`.container` files stops the console unit from starting at login, and removing the `-ctr-` timer
files keeps them from coming back; after `daemon-reload`, `jobhunter schedule install --enable`
writes the host timers and enables them. It does not remove `~/.local/bin/jobhunter-ctr` or the
podman secrets; delete those yourself (`podman secret rm NAME`) if you want them gone.

## SELinux

The units mount with `:z`, which labels the directories `container_file_t`. Files created later
inherit it. A file you **move** in with `mv` keeps its old label and the container gets EACCES,
so use `cp`, then delete the original:

```
cp ~/Downloads/resume.pdf ~/.local/share/jobhunter/resume/
```

If preflight warns that files are not `container_file_t`, run the `chcon -R -t container_file_t
<dir>` line it prints. `restorecon -R ~/.local/share` resets labels to the host default; the
next container start relabels, and a running console gets EACCES until restarted. To make the
label permanent for tools that call `restorecon`:

```
sudo semanage fcontext -a -t container_file_t '/var/home/<you>/.local/share/jobhunter(/.*)?'
restorecon -R ~/.local/share/jobhunter
```

## Troubleshooting

**`/healthz` prints `000`, or the console never answers.** The default `Network=pasta:
--host-lo-to-ns-lo` should let host loopback reach the container. If it does not on your
machine, edit `~/.config/containers/systemd/jobhunter-console.container`: replace the `Exec=`
line with `Exec=console --host 0.0.0.0 --port 8808` and add
`Environment=JOBHUNTER_PUBLISHED_LOOPBACK_ONLY=1`. The port stays published on `127.0.0.1`
only; the variable is the interlock that lets the console bind `0.0.0.0` inside the container.
Then `systemctl --user daemon-reload` and `systemctl --user restart jobhunter-console`. Never
publish the port on another address.

**403 in the browser or from `curl`.** The console checks `Host` on every request and accepts
only `localhost` or a loopback IP literal. `foo.localhost`, `0.0.0.0`, your hostname or an
`/etc/hosts` alias all get 403. Open `http://127.0.0.1:8808/` or `http://localhost:8808/`.

**EACCES or "permission denied" on a file in the data directory.** A file moved in with `mv`
has the wrong label: see [SELinux](#selinux).

**`database schema is behind this jobhunter ... run jobhunter upgrade`.** The code is newer than
the database, and nothing migrates on its own in a container deployment. Run `jobhunter upgrade`
(or `jobhunter db migrate`, which backs up first).

**`database is newer than this jobhunter (code has N, database has M)`.** A newer jobhunter
migrated the database. Rebuild the image or update the checkout so they match, or restore a
backup that matches.

**`another jobhunter job is running (pid, host, since)`.** The run lock: a nightly job, an
upgrade or another interactive command holds it. Wait; `systemctl --user list-units
'jobhunter-ctr-*'` shows what is running. Timer-started jobs wait for the lock themselves;
interactive ones exit 0 having spent nothing. A holder older than 6 hours makes the next job
exit with status 75 so a hung job is reported.

**`error: jobhunter X does not run in container mode`.** `mail auth`, `schedule`,
`migrate-paths`, `apply draft`, `upgrade`, `ctr-script`, `db relativize-paths`, `db restore` and
`container preflight` are host-only.
Run them on the host, not through `jobhunter-ctr`.

**The console ignores an edited `config.toml`.** The units bind-mount that single file; an
editor that saves by renaming it leaves the container on the old file. Run
`systemctl --user restart jobhunter-console`.

**A key shows `invalid (...)` in `secrets status`.** The value has whitespace or a control
character inside, or the file is missing or over 64 KiB. Recreate the secret from a clean file,
then rerun preflight, daemon-reload and restart as above.
