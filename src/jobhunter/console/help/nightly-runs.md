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
`journalctl --user -u jobhunter-run.service -n 20`.

Optional, to keep the timers running while you are logged out:
`loginctl enable-linger $USER`. Without it, timers that never fire are reported as "timers not
running" once the run unit was installed and its timer enabled more than 36 hours ago and no
timer-started run has been recorded.

If you move the checkout or reinstall the venv, run `jobhunter schedule install` again so the
units point at the new paths.
