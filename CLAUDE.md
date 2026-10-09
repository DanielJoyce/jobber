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

- **Tests never touch the network.** Use saved fixtures. Live probing is for discovery only.
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
