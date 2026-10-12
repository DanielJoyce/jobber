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

## First run

After the install above:

```bash
uv run jobhunter init      # the data directory (0700) and an empty database (0600)
uv run jobhunter console   # then open http://127.0.0.1:8808/setup
uv run jobhunter doctor    # the same checks as /setup, in the terminal
```

`/setup` lists what is set up and what is still missing. `doctor` exits 0 when every essential
item is ok. For the order the steps come in, read [Getting started](http://127.0.0.1:8808/help/getting-started)
in the console (the same page as `src/jobhunter/console/help/getting-started.md`). Containers
instead of a host install: see *Running in containers* below.

## Help

User help lives in `src/jobhunter/console/help/`, one Markdown file per topic. The same
pages are served by the console at `http://127.0.0.1:8808/help`.

| Topic | What |
|---|---|
| [Getting started](src/jobhunter/console/help/getting-started.md) | The order a fresh install meets things |
| [Profile and resume](src/jobhunter/console/help/profile-and-resume.md) | Preferences, the resume, what re-scores |
| [Choosing a scorer](src/jobhunter/console/help/scorers.md) | Providers, keys and models |
| [Choosing models](src/jobhunter/console/help/models.md) | Per-use models, models.toml and the Models page |
| [OpenRouter and Jev](src/jobhunter/console/help/openrouter.md) | An account, a key and credits |
| [Costs and credits](src/jobhunter/console/help/costs-and-credits.md) | What spends money, and the caps |
| [Email alerts](src/jobhunter/console/help/email-alerts.md) | Gmail, step by step |
| [Browser extension](src/jobhunter/console/help/browser-extension.md) | Send a posting to the console |
| [Drafting](src/jobhunter/console/help/drafting.md) | The Claude Code login and the API runner |
| [Settings and secrets](src/jobhunter/console/help/settings-and-secrets.md) | config.toml, keys and environment variables |
| [Nightly runs](src/jobhunter/console/help/nightly-runs.md) | Scheduled runs and their logs |
| [Containers](src/jobhunter/console/help/containers.md) | Rootless podman on Fedora atomic |
| [Troubleshooting](src/jobhunter/console/help/troubleshooting.md) | Common errors and fixes |

## Choosing a scorer

Moved to the help page *Choosing a scorer* (see the Help table above, and
`/help/scorers` in the console). It covers `scoring.screen_scorer` and `deep_scorer`, the
providers and keys, the `jev:` rule and the Anthropic key.

## Running in containers

Optional: run the console and the nightly jobs as rootless podman containers on Fedora atomic.
Start at the *First run checklist* in [docs/containers.md](docs/containers.md).

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
