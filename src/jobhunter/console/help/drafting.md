# Drafting

Drafting writes a targeted resume, a cover letter, or an answer to an application question for
a packet (a job you are applying to). There are two runners, chosen by `[apply] runner` in
`config.toml`:

- `cli` (the default) runs the Claude Code program on your claude.ai subscription. No API
  credits are spent.
- `api` calls the Anthropic API with a key, and spends credits.

A failed `cli` run only offers the API run. It never switches by itself.

## The cli runner: log in to Claude Code

1. Install Claude Code so that `claude` is on your `PATH`.
2. Run `claude` once and log in with your **claude.ai subscription**.

jobhunter asks `claude auth status` before the first call of a run and requires a first-party
claude.ai login. Any other login (an API key, Bedrock, Vertex) fails that check, and the runner
turns itself off with the reason. The API key variables are removed from the environment the
child process sees, so an `ANTHROPIC_API_KEY` in your `~/.env` can never make a `cli` draft bill
the API.

Two things are passed through to Claude Code if you set them:

- `CLAUDE_CONFIG_DIR`, if your login lives somewhere other than the default.
- `CLAUDE_CODE_OAUTH_TOKEN`, a subscription token from `claude setup-token`. Put it in `~/.env`;
  it has no `*_FILE` form, so it is not available as a podman secret.

When the runner is off, the `/costs` page in the console shows the reason, with a **Turn back on**
button once you have fixed it (for example, by logging in again).

### Paid extra usage (the overage hold)

If your subscription reports that a call was charged as paid extra usage, jobhunter records a
hold. Further `cli` drafts wait for an explicit click (or a confirmation on the command line),
and that run is charged against `[apply] daily_cap_usd` like an API run. Turning the runner back
on does not clear the hold; clearing it is a separate button on `/costs` ("My plan has reset:
clear"), for when your plan is back within its limit.

## The api runner

Set `ANTHROPIC_API_KEY` as described in [Choosing a scorer](scorers.md), then either set
`runner = "api"` in `[apply]` or ask for it on one draft:

```bash
jobhunter apply draft 12 --runner api
```

Replace `12` with the packet id (the number in `/packet/12` in the console). The command shows
an estimate and asks before it spends; `--yes` skips the question. Spend is capped by
`[apply] daily_cap_usd` (default one dollar a day, separate from the scoring caps). A draft that
would pass the cap is refused before the call. See [Costs and credits](costs-and-credits.md).

## Other drafts

```bash
jobhunter apply draft 12 --letter
jobhunter apply draft 12 --question "Why do you want to work here?"
jobhunter apply draft 12 --instruction "shorter"
```

A question on the never-store list (personal details jobhunter must not keep) is
refused and never reaches a model. A years-of-experience question gets resume evidence, never a
number.

## The fact check

Every version, generated or edited, is checked for invention before it can be marked ready.
Each line must cite lines of your resume (letters and answers may also cite your notes, and
answers your stories). The check
catches a missing citation, a changed employer, title or date, a number, skill or credential
that is not in the cited lines, and a claim stronger than the source ("contributed to" turned
into "led"). A line it cannot support is shown as unsupported: fix it, or confirm "this is true,
keep it".

A second, advisory pass asks a small model whether the cited lines support each generated line
(`[apply] entailment_check`, on by default). It never clears a flag by itself. It cannot judge
every rephrasing, so each line shows what it cites for you to read.

## Containers

Drafting runs on the host, not in the container: `apply draft` is a host command, and the
`claude` login lives in your home folder. See [Containers](containers.md).
