# Nightly runs

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
`journalctl --user -u jobhunter-run.service -n 20`. A run logs its progress there: each stage,
each source and each scoring batch. For the container units, the console terminal and how to
change the level, see [Troubleshooting](troubleshooting.md#where-the-logs-go-and-how-much-they-say).

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
the one that was told.

The email-alert source reads and writes the database itself, so it runs on its own after the
other sources. The journal gets a line per source as it finishes, for example
`list <source>: status=ok queries=3 pages=9 (cached 2) stubs=41 robots_skips=0 errors=0 in
62.0s`, and lines per stage. Lines from different sources interleave. Ctrl-C stops the run
within about one request: sources waiting on a rate limit give up at once, no source starts a
new request, and queued sources never start.
