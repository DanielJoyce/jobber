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
| `resume/` | The resume the fit scorer reads. **Local only, gitignored** (contact details) |
| `profile/` | Your preferences (salary, states, narrative). **Local only, gitignored** |
| `data/` | SQLite database and raw HTTP cache. **Local only, gitignored** |

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
