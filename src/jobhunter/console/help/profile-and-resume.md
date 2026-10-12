# Profile and resume

Two files describe you to jobhunter: `preferences.yaml` and one resume in Markdown. Both live in
your data folder (`jobhunter paths` shows where). They are never committed to the repository.

## preferences.yaml

`preferences.yaml` in the profile folder is the source of truth for what you want: salary
floor, states, target titles, weights, bucket thresholds, search queries, and the free-text
parts the model reads (your current focus, what you are done with, a short narrative).

You rarely edit it by hand. The `/prefs` page in the console is a form over the same file and
previews the effect of a change before you save it. If the file does not exist yet, `/setup`
in the console can write a starter file for you to fill in.

## The resume

Use one readable `.md` file. You can upload it in the console on `/setup`, or put it where
`jobhunter paths` says the resume belongs. The `resume_path` in `preferences.yaml` wins over
the default location. Inside containers it must be relative to the profile folder, because an
absolute path from the host does not exist there.

Keep dates on each role. The scorer weighs recent experience more than old experience, and it
can only do that when your resume says when you did what.

## Which edits are free and which re-score

Some facts are computed locally from scores already stored, so changing them applies at once
and costs nothing:

- salary floor and target, states and their ranking, remote settings
- title exclusions, employment types excluded
- the weights and the bucket thresholds

Search queries are free too, and take effect on the next run.

The spend caps and the deep-pass shortlist size are not in `preferences.yaml`. They are
`[scoring]` settings in `config.toml`: they cost nothing to change, but the console reads them
when it starts, so restart it after editing them.

What the model reads is different. Changing your resume, your current focus, what you are done
with, or your narrative changes the scoring version. New jobs are scored with the new text from
the next run. Jobs already scored are **not** re-scored automatically, because that spends
money. The `/prefs` page shows an estimate and asks you to confirm before it re-scores older
jobs. See [Costs and credits](costs-and-credits.md) for how the caps apply.

## Check it

```bash
jobhunter doctor -v
```

The profile and resume rows say whether the files are present and valid. A profile that cannot
load does not stop fetching: the run logs it and skips scoring.
