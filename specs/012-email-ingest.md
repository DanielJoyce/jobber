# 012 — Email alert ingest (Pass 2)

Most state job banks disallow crawling ([008](008-compliance.md#verified-robotstxt-survey-2026-10-09)),
and every one of them offers email job alerts. Pass 2 turns those alerts into jobs in the same
pipeline, without the alerts ever reaching your main inbox.

## The dedicated address

**Every job-alert subscription uses `<you>+jobs@gmail.com`**: your Gmail address with a `+jobs`
suffix. The real address lives only in your local, gitignored config.

Gmail delivers mail for any `+suffix` variant to the same mailbox, and a filter can match the
exact variant. One filter therefore catches alerts from every board, present and future:

```
Matches:  to:(<you>+jobs@gmail.com)
Do this:  Skip Inbox · Apply label "jobhunter/alerts" · Never send to Spam
```

- **Your main view stays clean.** Alerts never touch the inbox.
- **Never send to Spam** matters because bulk alert mail is exactly what spam filters like to
  catch, and a silently spam-binned alert is a silently missed job.
- **Fallback for forms that reject `+`.** Some sign-up forms wrongly refuse `+` in an address.
  For those, subscribe with the plain address and add a second filter on the sender's domain
  (`from:(@workintexas.com)`) with the same actions. `jobhunter mail setup` keeps the list of
  sender-domain filters in config.
- **Address recorded in config,** not hard-coded:

```toml
[mail]
alerts_address = "<you>+jobs@gmail.com"
label = "jobhunter/alerts"
fallback_sender_domains = []    # filled in as you hit forms that reject "+"
```

## Why the app talks to Gmail directly

Checked 2026-10-09: the Gmail connector available to Claude can create labels, apply or remove
labels on existing mail, and search. **It cannot create, list or edit filters.** It can tidy mail
after the fact but can't route new mail.

Gmail's own API can (`users.settings.filters.create`). The app therefore runs its own OAuth
flow, separate from the Claude connector:

| Scope | Used for |
|---|---|
| `gmail.readonly` | Reading messages under `jobhunter/alerts` |
| `gmail.labels` | Creating the label |
| `gmail.settings.basic` | Creating the filters |

No send scope and no modify scope on other mail. The app reads one label and manages its own
filters, nothing more. The refresh token is stored in the OS keyring, never in the repo or a
config file.

`jobhunter mail setup` is idempotent. It creates the label if it's missing, creates the `+jobs`
filter if it's missing, adds any `fallback_sender_domains` filters, and lists what it found. If
you'd rather not grant the settings scope, it prints the filter XML for manual import under Gmail
Settings → Filters and Blocked Addresses → Import filters.

## Setting up Gmail

One time, about ten minutes. No billing is involved.

1. Create a project at <https://console.cloud.google.com/>.
2. APIs & Services, Library: enable the **Gmail API**.
3. OAuth consent screen: user type External; add your own Google account as a **Test user** and
   leave the app in Testing (it is only ever you).
4. Credentials, Create credentials, **OAuth client ID**, application type **Desktop app**.
   Download the JSON and save it as `~/.config/jobhunter/google_client_secret.json` (or set
   `mail.client_secrets_path`, or the env var `JOBHUNTER_GOOGLE_CLIENT_SECRETS`). It stays
   outside the repo.
5. Set `mail.alerts_address` in `~/.config/jobhunter/config.toml` to your real `+jobs` address.
6. `jobhunter mail auth` starts a one-shot server on `127.0.0.1`, always prints the full
   authorization URL ("If no browser opened, open this URL in any browser on this machine."),
   and tries to open it. The refresh token goes to the OS keyring (service `jobhunter`, key
   `gmail`); if no keyring backend exists (containers) it goes to
   `~/.config/jobhunter/gmail_token.json` (mode 0600) with a one-line warning. `mail status`
   says which. Never printed.

   **Containers (toolbox/distrobox, no browser installed).** Openers are tried in order, the
   first success wins: Python `webbrowser` (only if a real, non-text browser is registered),
   `xdg-open`, `flatpak-spawn --host xdg-open`, `distrobox-host-exec xdg-open`, then the
   desktop portal (`gdbus ... org.freedesktop.portal.OpenURI.OpenURI`). If none works, open
   the printed URL in the host's browser; toolbox shares the host network, so the redirect to
   `127.0.0.1` reaches the waiting server. `--port N` fixes the loopback port; `--no-browser`
   skips the openers. If the network is not shared, use `jobhunter mail auth --manual`: no
   server; after approving, the browser tries to load `http://127.0.0.1:8765/?state=...&code=...`
   (it may show an error page), and you paste that full URL back at the prompt. The state is
   checked; a stale or wrong paste is rejected. Google retired the copy-the-code flow, so this
   loopback paste is the fallback. `--manual --port N` changes the port in the URL.
   **Production mode.** In Testing, refresh tokens expire after 7 days. Set the consent
   screen's publishing status to **In production** (no verification needed for personal use of
   an unverified app); sign-in then shows an "unverified app" warning, click Advanced, then
   "Go to jobhunter (unsafe)". Tokens then last until revoked. Re-run `mail auth` if Google
   says the token is invalid.
7. `jobhunter mail setup --dry-run`, then `jobhunter mail setup`; `jobhunter mail status` checks.

Prefer not to grant `gmail.settings.basic`? `jobhunter mail setup --xml` prints a filter file
needing no OAuth; import it under Gmail Settings, Filters and Blocked Addresses.

Privacy notes: the three scopes above are the only ones requested; there is no send scope and no
modify scope. The refresh token never touches the repo, config files or logs. The OAuth client
secret is not a user secret for an installed app, but is still kept out of the repo. Mail content
is read only from the `jobhunter/alerts` label and processed locally. Filters use the Gmail
action shape `addLabelIds: [<label>]`, `removeLabelIds: ["INBOX", "SPAM"]`.

## Pipeline

The `mailalerts` adapter is an ordinary source in the registry
([003](003-sources-and-adapters.md)):

```
jobhunter/alerts label
   → fetch new messages since watermark (Gmail historyId)
   → identify sending family by sender domain + body signals
   → family parser extracts N job entries per email
   → JobStub per entry → normal pipeline (normalize → dedupe → prefilter → score)
```

Alert emails usually bundle several jobs. Each parser is tested against saved real emails in
`tests/fixtures/mail/<family>/`, captured as alerts arrive during M8.

Dedupe matters more here than anywhere else. A job that arrives by email may already have been
collected from national NLx or USAJOBS **with its full description**. When dedupe matches an
email stub to an existing job, the full record wins and the email adds nothing but a sighting.

## The partial-description problem

This is the important design constraint in Pass 2.

An alert email gives a title, an employer, a location, a link, and sometimes a short snippet. The
full description lives behind the link, on the board's own site. **On a host whose `robots.txt`
disallows that path, the `resolve` stage does not fetch it.** Following an emailed link by script
is still automated access to a disallowed path.

How the hosts compare:

| Family | Detail pages | Resolve? |
|---|---|---|
| JobLink (7) | Only `/search/jobs` and `/search/resumes` are disallowed; detail paths are not | **Yes.** Full description |
| VOS (~23 blocked) | `Disallow: /` | **No** |
| PA, MA | `Disallow: /` | **No** |
| Open set | Allowed | Yes, but these are collected directly anyway |

When the description can't be fetched, three things happen:

1. **Dedupe first.** VOS postings may also be syndicated to national NLx; that's plausible but
   unverified. A match there supplies the full description legitimately. M3's NLx coverage measurement says how often this works.
2. **Otherwise the job is scored as `description: partial`.** Stage 2 sees the title, employer,
   location and snippet, and the rubric is told the description is missing. The result is
   capped at bucket B and badged *partial: open to confirm*, so a guess is never presented as a
   bullseye.
3. **Your click finishes the job.** Pressing `o` in the console opens the posting in your
   browser, which is ordinary human browsing. If the job looks promising, a *paste description*
   box on the job page takes the text and re-scores it in full.

The data model needs one addition to `job` in [005](005-data-model.md):
`description_completeness TEXT NOT NULL DEFAULT 'full'`, with values `full | partial | pasted`.

## Subscribing

Subscribing to alerts is manual: one form per board, with your search criteria. The console
gets an `/alerts` checklist listing every blocked source, its alert sign-up URL, the address to
use (`+jobs`, or the plain address if it's on the fallback list), the criteria to enter (derived
from your profile's queries), and whether alerts have actually arrived from it. A source subscribed
more than three days ago with no alerts is flagged, since that usually means the sign-up failed
or the mail went to spam.

Use broad criteria. The board's alert filter is just keyword search again, and Stage 2 does the
real filtering.
