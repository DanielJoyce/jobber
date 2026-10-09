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

## Work tracking

Issues live in [`git-bug`](https://github.com/git-bug/git-bug), stored in the repository itself:

```bash
git bug pull origin      # fetch issues
git bug bug              # list them
```

Labels: `milestone:M0`–`M9`, `pass:1|2`, `area:*`, `difficulty:easy|medium|hard`, and
`agent:haiku|sonnet|opus` for the model each task is assigned to.
