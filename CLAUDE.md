# jobber — agent conventions

Single-user job-search system. Design lives in `specs/` (read `specs/README.md` first, then the
spec sections your task names). The specs are the source of truth; if you must deviate, record
why on the bug (see Decisions below).

## Layout and stack

- Python 3.13 via `uv`. Package `jobhunter` under `src/jobhunter/`, CLI entry point `jobhunter`.
- Layout follows `specs/002-architecture.md#repository-layout`.
- SQLite (stdlib `sqlite3`, no ORM), pydantic v2, httpx, selectolax/lxml, typer, fastapi + jinja2 + htmx.
- Tests in `tests/`, mirroring the package. Fixtures in `tests/fixtures/`.

## Commands

```
uv sync                      # install
uv run ruff check . && uv run ruff format --check .
uv run pytest -q
```

All three must pass before you commit.

## Hard rules

- **Tests never touch the network.** Use saved fixtures and `httpx.MockTransport`. Live probing
  is for discovery only. `tests/conftest.py` enforces this: any non-localhost connection or DNS
  lookup in a test raises. Scorers spend the user's prepaid API credits, and the user requires
  that no test ever does so unless explicitly run. A test that truly needs a real service is
  marked `@pytest.mark.live`; it is deselected by default and also skipped unless
  `JOBHUNTER_LIVE_TESTS=1`. Never weaken or bypass the guard.
- **Only `jobhunter.core.fetch.FetchContext` does network I/O.** Adapters must not import
  `httpx` or `playwright` directly. Robots.txt is always respected (`specs/008-compliance.md`).
- **No personal data in the repo.** `resume/`, `profile/`, `data/`, `config.toml` are gitignored.
  Never commit an email address, phone number, or salary figure. Use placeholders like
  `<you>@example.com` in examples.
- **Anthropic API:** official `anthropic` SDK only. Model IDs: `claude-haiku-4-5` (screen),
  `claude-opus-5` (deep pass). Structured output via `output_config`; batch via
  `client.messages.batches`. Tests mock the client; never call the API in tests.
- Keep changes scoped to your bug. Don't reformat or refactor unrelated files.

## Git hooks

Secret scanning runs on every commit (gitleaks, private-key detection, a guard against
`resume/`, `profile/`, `data/`, `config.toml`). Set up once per clone with
`scripts/setup-hooks.sh`. It explains and asks before downloading anything.
**Never commit with `--no-verify`.** For a false positive, allowlist it in `.gitleaks.toml`
with a comment saying why. CI rescans the full history on every push.

## Git workflow

- Branch from latest `main`: `bug/<git-bug-short-id>-<key>`, e.g. `bug/6c55bec-scaffold`.
- Small, focused commits. Conventional prefixes: `feat:`, `fix:`, `test:`, `docs:`, `chore:`, `ci:`.
- Every commit message ends with a `Co-Authored-By` trailer naming **the model you actually
  are** (e.g. `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`), plus the
  `Claude-Session:` line if your environment provides one. Attribution must be accurate.
- Push your branch: `GIT_ASKPASS= git push -u origin <branch>`. Do not merge to `main`; the
  orchestrator opens and merges the PR.

## git-bug

Issues live in git-bug. Keep messages plain text on one line: no markdown, no heredocs.

```
git bug bug show <id>
git bug bug comment new <id> -m "Decision: ... Reason: ..."
```

Pushing bug refs needs plain git (git-bug's own push can't use the credential helper):
`GIT_ASKPASS= git push origin 'refs/bugs/*:refs/bugs/*' 'refs/identities/*:refs/identities/*'`

## Decisions

When a spec is ambiguous or you deviate from it, make the call, then record it as a comment on
your bug: `Decision: <what>. Reason: <why>.` Mention it in your final report.

## Process: orchestrator as PM, workers, adversarial review

Set by the user on 2026-10-09. Why: several merged changes shipped with defects their own
author's tests could not catch (a Sankey test asserted a link to a page that did not exist; a
template edit in the user's live checkout took /prefs down; a salary parser passed its tests but
missed common real-world shapes). Independent review by a stronger reviewer catches these.

### Roles

- **Orchestrator (PM).** The top-level session. Talks to the user, triages requests into
  git-bug issues, writes briefs, picks the worker model, runs reviews, decides what lands, and
  reports outcomes in plain language. The PM does not write feature code. It may make trivial
  fixes (one-line, no logic) and must still put them through review.
- **Workers.** Subagents that implement one bug each on their own branch in an isolated
  worktree, push the branch, and report. They never merge.
- **Reviewers.** Subagents that try to break a worker's change before it lands.

### Model and effort scoping

Pick the cheapest model that can do the task well, and label the bug to match
(`difficulty:*`, `agent:*`).

| Task | Worker | Reviewer |
|---|---|---|
| Mechanical (renames, copy, config, small template tweaks) | haiku | sonnet |
| Normal features and fixes | sonnet | opus |
| Hard (scoring, money paths, migrations, concurrency, parsing real-world data) | opus | opus at the next higher effort (high to xhigh, xhigh to max) |

The rule: **the reviewer is always stronger than the author**, either a stronger model or, for
opus, a higher reasoning effort. A model never reviews its own output at the same tier.

### Review gate (every PR, no exceptions)

Run as a workflow, not ad hoc. Shape: find, then adversarially verify, then decide.

1. **Find.** One or more reviewer agents read the diff and the code around it, with lenses:
   correctness on real data shapes, tests that assert behavior rather than implementation
   (would the test fail if the feature were broken?), money and credit paths (could this spend
   scorer credits unexpectedly?), privacy (personal data, secrets, network in tests), UI
   (links resolve, dark mode, phone width, cache), migrations against a copy of real-shaped data.
2. **Verify.** Each finding goes to a skeptic told to refute it, defaulting to refuted when
   unsure. Only findings that survive are reported.
3. **Decide.** Blocking findings go back to the worker (or a new worker) before merge.
   Non-blocking findings become git-bug issues labelled `review` and are linked in the PR.
   The PM merges only when CI is green and no blocking finding is open.

Review a change before merge. If something merged unreviewed, review it retroactively and file
what is found.

### Working-tree safety

- Never edit files in the main checkout. The user's console runs from it and reads templates
  from disk on every request, so a half-done edit there breaks the live app. Use a worktree.
- After merging, the main checkout is fast-forwarded only. Tell the user to restart the
  console when Python changed.

### Verification habits

- Before claiming a data fix works, measure it on a **copy** of the user's database
  (`sqlite3 data/jobhunter.db ".backup <scratch>/copy.db"`) and report before and after counts.
- Back up the real database before any write to it (`data/backups/`).
- Reproduce user-reported UI bugs in a browser against a copy of their data before fixing.
- Paid actions (scorer runs) are the user's call; give them the command and the estimate.

### Workflows

Multi-agent work runs through the Workflow tool (dynamic workflows). Keep each workflow to a
single phase (implement, or review, or research) so the PM stays in the loop between phases.
At most 4 workers implement at once.
