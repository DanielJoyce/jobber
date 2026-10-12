# Getting started

The order a fresh install meets things, the same order as the groups on `/setup` in the
console. You can stop after any step: each one unlocks something on its own.

Containers instead of a host install? Start at the first-run checklist in
[Containers](containers.md), then come back here for the steps that are the same.

## Install

```bash
uv sync
scripts/setup-hooks.sh     # secret-scanning git hooks; asks before downloading Go
```

Your files live outside the checkout, in the XDG folders. `jobhunter paths` shows each one,
where its value came from and whether it exists.

## Create the data folder

```bash
jobhunter init
```

This creates the data directory (owner-only) and an empty database. It is safe to run again: it
says "already set up" and re-checks permissions. If your data is still in the old
repo-relative folders it refuses and tells you to run `jobhunter migrate-paths` instead.

## Open the console and use /setup

```bash
jobhunter console
```

Then open `http://127.0.0.1:8808/setup` in your browser. The console is on this machine only;
the address and port come from `[console]` in `config.toml`. The `/setup` page in the console
lists what jobhunter needs, marks each item as essential, recommended or optional, and puts
the fix next to the row. Work down it:

1. **Contact for the User-Agent.** `[fetch] user_agent_contact` in `config.toml`. Every request
   jobhunter makes names a contact, so a site owner can reach you. Use `<you>@example.com` as
   the shape. The placeholder goes out as-is until you change it.
2. **Preferences and resume.** `/setup` can write a starter `preferences.yaml` and take your
   resume upload. See [Profile and resume](profile-and-resume.md).
3. **A scorer and its key.** Scoring is what turns postings into ranked fits. See
   [Choosing a scorer](scorers.md); the money side is in
   [Costs and credits](costs-and-credits.md).
4. **The first run** (below).
5. **Scheduled runs** (below).
6. **Optional parts:** [email alerts](email-alerts.md), a USAJOBS key, the
   [browser extension](browser-extension.md) and [drafting](drafting.md).

Secrets (API keys) and `config.toml` are read when the console starts, so restart it after
changing either. Where keys go is in [Settings and secrets](settings-and-secrets.md).

## Check from the terminal

```bash
jobhunter doctor
jobhunter doctor -v
```

`doctor` runs the same checks as `/setup`. It is free and local: no network, no model call, and
it prints no key or address. `-v` shows every row with what it unlocks and where to fix it. The
exit status is 0 when every essential is fine, 1 when an essential is missing or broken, and 2
on a usage error. Recommended and optional items never change it.

`jobhunter doctor --live` also asks each service whether your setup works: whether your scorer
key is accepted (and, for OpenRouter, the credit left on it), whether a local model answers,
whether Gmail accepts your token and has the label and filters, and whether `claude` is logged
in with claude.ai. It prints what it will contact first, calls no model and spends nothing, and
shows only yes or no answers. The same checks are the **Test (free)** buttons on `/setup`, which
appear only in a browser on this machine; a button shows "checked moments ago" for a few seconds
after a check, and rows that share a service (the two Gmail rows, or the scorers on one
provider) share that wait, so nothing can hammer a service. Gmail is only read, never changed. OpenRouter is asked only over
https. With `--group NAME`, `--live` checks only that group's rows, and a failing live check on
an essential row shows in the summary line as well as the exit status. With containers, run
`jobhunter-ctr doctor --live`.

## The first run

```bash
jobhunter run
```

`run` fetches from your sources, resolves descriptions, filters, and submits new jobs to the
scorer. Scoring a batch is collected later:

```bash
jobhunter score --collect-pending
```

Scoring spends money, so look at [Costs and credits](costs-and-credits.md) first. The spend
caps limit it, and `/costs` in the console shows what was spent. If scoring is not set up, the
run still fetches and says that it skipped scoring.

## Scheduled runs

```bash
jobhunter schedule install --dry-run
jobhunter schedule install --enable
jobhunter schedule status
```

These install the systemd user timers that run the two commands above each night. The details,
logs and what to do when you move the checkout are in [Nightly runs](nightly-runs.md). In
containers the timers come from the Quadlet units instead; do not turn on both.

## See what ran

Two places answer "did it run, and what did it do?":

- The **Last 24 hours** card on Today (`/`): when the last run happened and how it ended
  (or "running since ..."), how many jobs were pulled in and grouped since this time
  yesterday, how many of those new jobs passed the prefilter (a preferences change re-checks
  old jobs, and those are not counted), and how many were scored. Failures are listed
  separately: refused or cut off by the scorer, errors in re-score requests, and errors in
  score batches. Then the unscored backlog (what the next nightly run will score), the scoring
  spend in the window, and all spend for today and yesterday. The card links to
  the Costs page (`/costs`) and the log.
- The **Log** page (`/log`, in the nav): every run, re-score, score batch, deep pass, mail
  fetch, extension capture and drafting run, newest first, in your local time. Pick a kind to
  filter. A run that never finished shows "running" only while the nightly `run` itself holds the
  run lock (a re-score or other job holding it does not count) and "interrupted" otherwise;
  a run that finished with some sources failing is "partial". Error text is shortened and stripped of email addresses
  and anything that looks like a key.

## When something is wrong

`/setup` and `jobhunter doctor` say what is missing and why. Common errors and their fixes are
in [Troubleshooting](troubleshooting.md).
