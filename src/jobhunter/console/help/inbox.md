# The Inbox

The Inbox lists every job you have not yet shortlisted or dismissed, grouped into buckets by
how well it fits you. The number at the left of each row is its overall score, 0 to 100.

## Hiding low scores

Type a number in the **Min score** box and press **Apply** to hide every job whose overall
score is below it. A job exactly at the minimum stays.

- The page says how many jobs it hid, for example "37 below 60 hidden". The bucket counts
  and chips do not include those jobs.
- Every scored job has an overall score, so none is exempt. Jobs scored from an email alert
  summary are filtered like the rest.
- The box combines with the state, bucket and triaged filters, and the chips keep it. Bulk
  shortlist and dismiss do not reload the page, so the filter stays as you left it.
- The console remembers the last value in a browser cookie, so the next visit to the Inbox
  starts with it applied. **Clear** (or applying an empty box) forgets it.
- You can also pass it in the address: `/inbox?min=60`. A value above 100 counts as 100, a
  negative one as 0, and text that is not a number is ignored.
