# Email alerts

Job boards send you alerts by email; jobhunter reads them from a Gmail label and turns them into
postings. It stores only message metadata, never the bodies, and it never fetches the boards'
own pages. This page is the whole Gmail setup, in seven steps. In the console, the same steps
appear as rows on `/setup` (the Email alerts group) and `/alerts`; `jobhunter doctor` lists them
too. Do them in order: each step needs the one before.

Commands run on the **host**, in the checkout. In container mode only `jobhunter mail auth`
must run on the host (it needs your browser), and it stores the token as a podman secret, so
the host keeps no token. Every other mail command then runs through the wrapper: write
`jobhunter-ctr mail setup` and `jobhunter-ctr mail status` where this page says
`jobhunter mail setup` and `jobhunter mail status`. See [Containers](containers.md#gmail).

Use a separate `+jobs` address of your own Gmail account, such as `<you>+jobs@gmail.com`: mail
sent there still reaches your inbox, and a filter can tell it apart.

## Step 1: Google client JSON

jobhunter reads your mail through a Google Cloud project that you own. It is free, needs no
billing, and takes about ten minutes. The steps below are generated from the code that prints
them when the file is missing.

<!-- jh:begin cloud-steps -->

```text
To create a Google OAuth client (one time, free, no billing needed):
  1. Open https://console.cloud.google.com/ and create a project (e.g. "jobhunter").
  2. APIs & Services -> Library -> search "Gmail API" -> Enable.
  3. APIs & Services -> OAuth consent screen: user type External, fill the required fields,
     and add your own Google account under "Test users" (leave the app in Testing).
  4. APIs & Services -> Credentials -> Create credentials -> OAuth client ID ->
     Application type "Desktop app". Download the JSON.
  5. Save it as ~/.config/jobhunter/google_client_secret.json
     (or point JOBHUNTER_GOOGLE_CLIENT_SECRETS at it, or set mail.client_secrets_path in config.toml).
Then run: jobhunter mail auth
```

<!-- jh:end cloud-steps -->

Where the downloaded file goes:

- **Host.** The default is `google_client_secret.json` in the config folder (`jobhunter paths`
  shows the folder). To keep it elsewhere, set `[mail] client_secrets_path` in `config.toml`, or
  set `JOBHUNTER_GOOGLE_CLIENT_SECRETS` to its path (the variable wins).
- **Containers.** Make it a podman secret, once:
  `podman secret create jobhunter_google_client <path to the file>`. The units mount it and
  set `JOBHUNTER_GOOGLE_CLIENT_SECRETS` for you. Keep the original file on the host: the weekly
  re-authorisation (step 3) needs it.

Keep the file private and never commit it. It names your project, and the repository refuses
commits that look like secrets.

## Step 2: Alerts address

Set the address that alerts go to, in `config.toml` (the console never edits that file):

```toml
[mail]
alerts_address = "<you>+jobs@gmail.com"
```

Restart the console after you edit `config.toml`: it reads the file once, at start. While the
value is still the placeholder, `jobhunter mail setup` refuses to run.

## Step 3: Gmail token

Authorise jobhunter to read the label and manage its own filters:

```
jobhunter mail auth
```

A browser opens for Google's consent screen. If none opens, the command prints a URL to open on
any machine; `--no-browser` prints it without trying, and `--manual` lets you paste back the
address your browser ends on when it cannot reach the loopback page. The token is stored in
your OS keyring, else in a private file (mode 0600). Next to it jobhunter keeps
`gmail-auth.json` in the data folder: just the date it was issued and where it is stored, never
the token. `/setup` uses it to show the token's age.

**For containers**, store the token as a podman secret, from the host:

```
jobhunter mail auth --podman-secret jobhunter_gmail_token
```

If you already authorised locally, move that token instead of consenting again:

```
jobhunter mail auth --podman-secret jobhunter_gmail_token --from-local
```

`--keep-local` also keeps a local copy. Then run `jobhunter container preflight --yes` once, and
restart the console, as the command's closing message says.

**Weekly re-authorisation.** While your Google app is in Testing (the state step 1 leaves it
in), Google expires the refresh token after 7 days. Repeat the same `mail auth` command weekly.
For containers it replaces the secret in place and nothing else is needed. When it lapses, runs
report "Gmail token expired or revoked" ([Troubleshooting](troubleshooting.md#gmail-token-expired-or-revoked)) and
the mail source is skipped until you re-authorise; nothing else stops.

## Step 4: Label and filter

Create the label and the filters that file your alerts under it:

```
jobhunter mail setup
```

It is safe to repeat. `--dry-run` shows what it would create and writes nothing. If you did not
grant the settings scope, or prefer to do it by hand, print the filter XML and import it in
Gmail (Settings, Filters and blocked addresses, Import filters):

```
jobhunter mail setup --xml
```

The label defaults to `jobhunter/alerts` (`[mail] label`). The row on `/setup` turns green
when an alert has arrived under the label in the last 14 days.

## Step 5: Board subscriptions

Subscribe to each board's email alerts using your `+jobs` address. The checklist on `/alerts`
(in the console) lists every board with its sign-up link, the criteria to use and the address
to give. Subscribed, receiving and stale are counted there.

## Step 6: LinkedIn job alerts

LinkedIn alerts come from one sender, and the same site also sends personal mail, so route
exactly that sender and nothing else:

1. In LinkedIn, run a job search and choose "Set alert". Do **not** make the `+jobs` address
   your LinkedIn primary email: every LinkedIn notification would follow it.
2. Open one real alert in your inbox and read its From line. It is normally
   `jobalerts-noreply@linkedin.com`; confirm it, because the sender can change.
3. Add that exact address to `[mail] fallback_senders` in `config.toml`, restart the console,
   and run `jobhunter mail setup` again. It turns each entry into a `from:<address>` filter.

Never put `linkedin.com` in `[mail] fallback_sender_domains`: that becomes a filter on everything the `linkedin.com` domain sends,
which pulls InMail, recruiter and connection messages out of your inbox and stores their
subjects as alerts. `/setup` and `/alerts` warn about a domain entry that matches a site which
also sends personal mail.

LinkedIn alert messages are kept under the label; their parser is not written yet, so they
yield no postings for now. They are read once when it lands.

## Step 7: Craigslist saved searches

Optional and low priority. Create a Craigslist account and a saved search with email alerts,
then route its exact sender through `[mail] fallback_senders` the same way as step 6: read the
From line of a real alert, add that address, run `jobhunter mail setup` again. Do not route the
whole domain.

## How to tell it works

- `jobhunter mail status` (in containers `jobhunter-ctr mail status`) prints the token location, whether the label and each filter exist,
  and how many messages sit under the label. It changes nothing.
- Alerts arrive in Gmail under the label (open the label in Gmail).
- In the console, the Email alerts rows on `/setup` go green one by one, and `/alerts` shows
  per-board counts.
- `jobhunter doctor` prints the same rows in the terminal.

Something red? See [Troubleshooting](troubleshooting.md), or open `/setup` in the console.
