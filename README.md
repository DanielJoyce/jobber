# jobber

A single-user job-search system. It collects postings from US state job banks and USAJOBS,
resolves full descriptions, uses an LLM to judge fit against your *current* experience rather
than keyword overlap, sorts results into compatibility buckets, and tracks applications in a
local web console.

**Status:** design complete, implementation starting. See [`specs/`](specs/README.md).

## Layout

| Path | What |
|---|---|
| `specs/` | Design documents, read in numbered order |

Your files live outside the checkout, in the XDG directories (`jobhunter paths` shows each one,
where it came from and whether it exists):

| Default location | What |
|---|---|
| `~/.config/jobhunter/` (`$XDG_CONFIG_HOME`) | `config.toml`, the Gmail token and client secret |
| `~/.local/share/jobhunter/` (`$XDG_DATA_HOME`) | `resume/`, `profile/` (with `preferences.yaml`), `jobhunter.db`, `backups/` |
| `~/.cache/jobhunter/` (`$XDG_CACHE_HOME`) | the raw HTTP cache and the model price catalog |

Paths set in `config.toml` (`[paths]`) win over the defaults, and the environment variables
`JOBHUNTER_DATA_DIR`, `JOBHUNTER_CACHE_DIR`, `JOBHUNTER_DB_PATH`, `JOBHUNTER_PROFILE_DIR` and
`JOBHUNTER_RESUME_PATH` win over the file. The repo's `resume/`, `profile/` and `data/` are gitignored
and no longer the default.

A fresh install needs nothing more: the first command creates the data directory (mode 0700)
and the database (0600). `jobhunter init` does that explicitly.

**Moving from the old layout** (`./data`, `./profile`, `./resume` in the checkout). jobhunter does
not fall back to the old folders. While an unmigrated `data/jobhunter.db` exists in the main
checkout (found through git, so a run from any worktree finds the same one) or in the working
directory, every command except `paths`, `migrate-paths`, `init` and `schedule` stops with
"your data is still in ...", so nothing can start a second, empty database. To move:

```bash
jobhunter migrate-paths                # dry run: lists every copy and rename, changes nothing
systemctl --user stop jobhunter-run.timer jobhunter-collect.timer jobhunter-verify.timer
# and stop `jobhunter console`: --apply refuses while anything has the database open
jobhunter migrate-paths --apply        # copy, verify, then rename the old folders
systemctl --user start jobhunter-run.timer jobhunter-collect.timer jobhunter-verify.timer
```

`--apply` copies the database with SQLite's backup API and copies `data/backups`, `data/cache`,
`profile/` and `resume/`. It checks the copies (`integrity_check`, the same tables and row
counts, the same sha256 for every file), makes the new files owner-only, then renames the old
folders to `data.migrated-YYYYMMDD`, `profile.migrated-YYYYMMDD` and `resume.migrated-YYYYMMDD`
(each with a `MOVED.txt`). It deletes nothing: remove the `.migrated` folders by hand once you
are satisfied (it prints the `rm -rf` line). If a check fails, the copies are removed and the old
folders are left as they were. A second run says "nothing to migrate". `--from DIR` takes the old
folders from another directory. If a database already exists at the new location while the old
one is unmigrated, commands and `migrate-paths` stop and ask you to choose; jobhunter never picks
one silently.

## Setup

```bash
uv sync
scripts/setup-hooks.sh     # secret-scanning git hooks; asks before downloading Go
```

## Running nightly

The nightly run is a systemd **user** timer, not cron, so logs go to the journal and a failure
triggers a notification. The unit templates live in `src/jobhunter/ops/units/`; the install
command fills in absolute paths (the project venv's `jobhunter` and this checkout).

| Unit | When | Runs |
|---|---|---|
| `jobhunter-run.timer` | 02:00 local, up to 10 min random delay | `jobhunter run` (ingest through score submit) |
| `jobhunter-collect.timer` | 03:30 local | `jobhunter score --collect-pending` |
| `jobhunter-verify.timer` | Sunday 04:00 local | `jobhunter sources verify` (still a stub on main; reports "not implemented") |

Timers use `Persistent=true`, so a run missed while the machine was off catches up. Every
service has `OnFailure=jobhunter-notify@%n.service`, which sends a `notify-send` notification
when it is installed and always writes to the journal.

```bash
jobhunter schedule install --dry-run   # print the units and systemctl commands; write nothing
jobhunter schedule install             # write ~/.config/systemd/user/jobhunter-*; print the commands
jobhunter schedule install --enable    # same, then run daemon-reload and enable --now the timers
jobhunter schedule status              # systemctl --user list-timers 'jobhunter-*' (read-only)
jobhunter schedule uninstall           # disable the timers and remove the unit files
```

Install does not run `systemctl` unless you pass `--enable`. Logs for a unit:
`journalctl --user -u jobhunter-run.service -n 20`.

Optional, to keep the timers running while you are logged out:
`loginctl enable-linger $USER`.

If you move the checkout or reinstall the venv, run `jobhunter schedule install` again so the
units point at the new paths.

## Work tracking

Issues live in [`git-bug`](https://github.com/git-bug/git-bug), stored in the repository itself:

```bash
git fetch origin 'refs/bugs/*:refs/bugs/*' 'refs/identities/*:refs/identities/*'
git bug bug              # list issues
git bug bug show <id>
```

Labels: `milestone:M0`–`M9`, `pass:1|2`, `area:*`, `difficulty:easy|medium|hard`, and
`agent:haiku|sonnet|opus|human` for who the task is assigned to. Agent conventions are in
[`CLAUDE.md`](CLAUDE.md).
