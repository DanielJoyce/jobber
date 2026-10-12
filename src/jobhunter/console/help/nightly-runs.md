# Nightly runs

The nightly run is a systemd **user** timer, not cron, so logs go to the journal and a failure
triggers a notification. The unit templates live in `src/jobhunter/ops/units/`; the install
command fills in absolute paths (the project venv's `jobhunter` and this checkout).

| Unit | When | Runs |
|---|---|---|
| `jobhunter-run.timer` | 02:00 local, up to 10 min random delay | `jobhunter run` (ingest through score submit, then apply links) |
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
`journalctl --user -u jobhunter-run.service -n 20`. A run logs its progress there: each stage,
each source and each scoring batch. For the container units, the console terminal and how to
change the level, see [Troubleshooting](troubleshooting.md#where-the-logs-go-and-how-much-they-say).

## Apply links in the run

The last stage of `jobhunter run`, `apply_links`, runs after scoring and never holds it up. It
works out where Apply should go for the jobs you are most likely to open, so the job page and
the Apply button do not wait on a slow site the next morning:

- jobs you shortlisted (an application at **interested** or **preparing**), then
- jobs not yet triaged whose fit is in bucket **A** or **B**, best first.

Only jobs with no apply link yet are taken. A link you set by hand is never touched, and a job
whose link already stopped at a login page is never fetched again, here or anywhere else: set
the link by hand on the job page if you find the real one. Postings past their closing date
are skipped. Requests respect robots.txt and each site's rate limit, and nothing is charged:
no scorer runs in this stage.

If a site is having trouble when the run reaches it (it answers 429 or a 5xx error, asks for a
long pause, cannot be reached, or its robots.txt cannot be read) and no other link for the job
settled it, nothing is saved for that job: it is counted as `deferred` and tried again next
run, so a bad night never fixes a worse link in place.

At most `apply_links.per_run` jobs per run (default 200), with `apply_links.workers`
(default 4) at once, and no job starts after `apply_links.max_minutes` (default 20) so the run
does not keep the 03:30 collect waiting (it waits for the run at most two hours). Jobs not
reached wait for the next run (`left`, and `late` when time ran out). Set `per_run = 0` under
`[apply_links]` in `config.toml` to turn the stage off. To leave it out of one run, name the
stages you want: `jobhunter run --stage list,resolve,normalize,dedupe,prefilter,score`. What it did shows on
[/log](/log) with each run (`apply_links: tried …, live …, deferred …, errors …`); a failure
in this stage is logged and never fails the run. Ctrl-C stops it within about one request,
like the fetch stages. Jobs it did not reach still resolve when you open them.

Optional, to keep the timers running while you are logged out:
`loginctl enable-linger $USER`. Without it, timers that never fire are reported as "timers not
running" once the run unit was installed and its timer enabled more than 36 hours ago and no
timer-started run has been recorded.

If you move the checkout or reinstall the venv, run `jobhunter schedule install` again so the
units point at the new paths.

## How long a run takes

The fetch stages (list and resolve) work on several sources at once: up to
`fetch.parallel_sources` (default 6, never more than `fetch.global_concurrency`) in
`config.toml`. Politeness does not change with it. Each host still gets at most one request per
its rate limit (default one every 5 seconds) or its robots.txt `Crawl-delay`, whichever is
slower, and two sources on the same host share that host's limit. So a run takes about as long
as its slowest source rather than the sum of all of them. Set `parallel_sources = 1` to fetch
one source at a time as before.

If a host answers 429 or sends `Retry-After`, every source on that host waits it out, not just
the one that was told, including requests already waiting their turn. When the pause asked for
is longer than 10 minutes, sources on that host stop for this run (marked `suspect`) and try
again next run, rather than sit idle for it.

The email-alert source reads and writes the database itself, so it runs on its own after the
other sources. Each source logs to the journal as soon as its fetching ends, for example
`fetched <source>: 41 stubs from 3 queries in 62.0s`. A second line with its status
follows once its stubs are saved, for example `list <source>: status=ok queries=3 pages=9
(cached 2) stubs=41 robots_skips=0 errors=0 in 62.0s`. Stubs are saved in a fixed order, so
that second line can wait for a slower source ahead of it. Lines from different sources
interleave.

Ctrl-C stops the run within about one request. Sources waiting on a rate limit give up at once,
no source starts a new request, and queued sources never start. A request already in flight
can take up to `fetch.timeout_s` (default 20 seconds) to finish or time out.
