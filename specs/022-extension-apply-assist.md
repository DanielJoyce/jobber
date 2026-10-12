# 022 — Extension apply assist: fill on click, resume and letter buttons, applied on submit

Status: **draft for review, revision 1** (bug `d23b093`). Written 2026-10-11 against `main` at
`8836891`, with 017 phase 1 (1a to 1e) merged and nothing of 017 phase 2 built. No code yet.

You asked (2026-10-11) for three things in jobhunter's extension: a way to generate a tailored
resume or cover letter for the job you are viewing; small buttons for that on the resume and
cover-letter fields of the application form, as Jobright's **Customize** button did; and, when
you submit the form, jobhunter recording that you applied. Later the same day you said you are
turning Jobright off, so jobhunter's extension becomes your only apply helper and must also do
what Jobright did: **fill the form from your saved answers when you click**.

This spec is built on [017](017-assisted-apply.md). It reuses 017 phase 2's design (content
scripts on the ATS hosts, the side panel, the two-step snapshot, `act.js` and `safeDispatch`, the
never-store list, the `/ext/v1/` security) and changes it where your two decisions require:

| You get | How it works | Never |
|---|---|---|
| **Fill** | On a Greenhouse, Lever, Ashby or Workable form (Workday later), click the jobhunter icon or the small **Fill** pill on the form. The side panel lists every field: what it will fill from your answers, what the site's resume parse already filled, and what is yours. **Fill** writes the ticked rows | Fill on page load, touch salary, EEO, consent or signature fields, overwrite a field that already has a value, click Submit |
| **Resume and letter buttons** | A small jobhunter button sits beside the resume upload, the cover-letter upload and the cover-letter text box. Click it: the side panel shows the packet for this job, which runner drafts it and what that costs, and waits for **Generate**. Once the draft passes the checker, **Attach** puts the PDF in that upload, or **Insert** puts the letter text in that box | Spend without your confirm, attach a draft with unchecked lines |
| **Applied on submit** | When you submit and the site shows its confirmation, the extension tells jobhunter, which records `applied` for the matched job. A notice on the page and in the popup offers **Undo** | Record a submit the site rejected, guess the job, or click Submit itself |

**jobhunter still never submits an application** ([017](017-assisted-apply.md#non-goals),
[008](008-compliance.md#what-we-will-not-build)). The extension has no code path to Submit; this
spec adds a passive listener that notices *your* submit, nothing that causes one.

## Decisions

| Question | Choice | Why |
|---|---|---|
| 017's phase 2 gate (8 ready packets in 4 weeks, then a spike) | **Waived** for everything in this spec (your decision, 2026-10-11). The offline spike stays, as ticket A1, because it measures Chrome behaviour the design depends on | You no longer have another helper, so there is nothing to measure the need against |
| Coexisting with Jobright ([017](017-assisted-apply.md#coexisting-with-jobright)) | **Dropped.** The Jobright mimic fixture, the "run Jobright first" checklist line and the corner-button offset go | Your decision: Jobright is off |
| What Fill fills | Your saved answers, **including a new contact block** (name, email, phone, city) and two yes/no answers, plus saved custom answers. Gaps only: never a field that has a value | Jobright filled contact fields; without it, a form whose resume parse misses them is left to you |
| Does a fill session need a packet? | **No.** A session belongs to the matched application. A packet adds the resume to attach and the drafts to insert | Many applications are quick ones with your base resume |
| When the buttons appear | **On page load, on the supported ATS hosts only**, drawn in a closed shadow root. Drawing them reads nothing from the form beyond where the named fields are and sends nothing anywhere ([Buttons](#where-the-buttons-appear)). Off per ATS in the options page | That is what you asked for (a button on the field, like Jobright's). The cost is that a site can see an extension is present ([Privacy](#in-page-surfaces)) |
| Where spending is confirmed | **Only in extension pages** (the side panel or the extension's own popup window), never in the page | A page can script clicks on anything in its own DOM. It cannot reach an extension page |
| Which runner drafts | `[apply] runner` as today (`cli` by default: `claude -p` on your subscription, no API cost). The panel names the runner and model before **Generate**; the API costs and the apply cap are shown before any API run, which needs its own click | [017 CLI runner](017-assisted-apply.md#cli-runner); unchanged money rules |
| Attaching a generated resume | Allowed from the panel when the version's checker report is clean (no unsupported line, none waiting for **This is true**). Anything else opens the packet page for review | The no-fabrication rule ([017](017-assisted-apply.md#no-fabrication-rule)) is unchanged; the panel is a second place to see a clean version, not a way around review |
| What counts as "applied" | **Your trusted submit, then the site's confirmation**, on the same tab and frame, within 90 seconds, on an ATS whose confirmation rule you have turned to **auto**. Every other case asks you | No false positives ([Recording applied](#3-recording-applied)) |
| How auto-recording is switched on | Each ATS starts in **ask** mode. After 3 asked submits where your answer agreed with the confirmation rule and none disagreed, the panel offers **Record Greenhouse submits automatically**; your click turns it on | The confirmation rules are recorded from hand-written fixtures and cannot be fully checked without submitting real applications. Your own submits check them |
| Undo | One click, for 7 days, from the page notice, the popup or the pipeline page. It removes the event the extension recorded and what it changed, and nothing else | A wrong `applied` must be cheap to take back |
| Event source | `application_event.source = 'manual'` with a fixed note, plus a new `ext_applied` row that holds the undo data. No change to the `source` CHECK | Changing a CHECK means rebuilding `application_event`; 017 already said "no new `application_event.source`" |
| Distribution | **Load unpacked**, as now. No Chrome Web Store listing | [Store review](#store-review) |

Recorded on `d23b093`.

## What changes in 017

| 017 says | 022 says |
|---|---|
| Phase 2 is built only if the gate passes | Gate waived; built as tickets A1 to A12 |
| Coexist with Jobright; corner buttons offset from Jobright's | No Jobright. jobhunter's own buttons sit on the field's corner |
| `fill_session.packet_id NOT NULL`; Start needs a `ready` packet with a rendered PDF | `fill_session.application_id NOT NULL`, `packet_id` optional; attach needs a clean, exported version ([Data](#data)) |
| `submit_seen` is a hint for "Did you apply?", never an `applied` event | Recorded as `applied` under the rule in [Recording applied](#3-recording-applied), with undo |
| Content scripts never act on page load | They draw the buttons at load and add passive submit listeners. They still write no field and send nothing without a click |
| Multi-step forms (Workday, NEOGOV) need their own design first | Designed here for Workday ([Workday](#workday-and-multi-step-forms)); NEOGOV stays checklist-only |
| Row 6 of [Field classification](017-assisted-apply.md#field-classification) fills links, notice period, relocation, work authorization | Adds the contact block and sponsorship ([Answer bank](#the-answer-bank)) |
| Row 7 pastes only saved answers on this packet | Also `/prefs` custom answers and your earlier answers to the same question ([Answer bank](#the-answer-bank)) |

A note in 017's status line points here. Everything else in 017 phase 2 stands; where this
spec says "as 017", it means that section unchanged.

**Correction to the brief.** 1e does not read inside iframes: `activeTab` covers the top frame
only, and `capture/extract.js` reports iframe `src` URLs, never their content
([017 Extraction](017-assisted-apply.md#extraction)). Reading an embedded Greenhouse or Ashby
form needs the ATS host permissions and `all_frames` content scripts that this spec adds.

## Architecture

```
 Chrome (your profile)                                            Console (127.0.0.1:8808)
 ─────────────────────                                            ────────────────────────
 ATS frame (top page or iframe on an employer page)              /ext/v1/*  POST only:
   content/frame.js   isolated world, all_frames, ATS hosts        token + Origin + Host
   ├ locate.js        finds the named fields, draws buttons        apply/ats_forms.py (data)
   ├ act.js           the only writer (setValue, setChecked,       apply/page_match.py
   │                  attach, safeDispatch)                        apply/fill_classify.py
   └ submit.js        passive listeners: your submit, then          apply/labels.py (never-store)
                      the confirmation or error markers            generator start/run (202)
        │ chrome.runtime messages only                             tracking.mark_applied
        ▼                                                          ext_applied, fill_session
 service worker (stateless relay, pending submit attempts in
 storage.session) ── fetch, bearer token ─────────────────────►
        ▲
 side panel (or popup window) ── session, Fill, Generate, Attach, Undo
```

| Part | Does | Does not |
|---|---|---|
| **`content/frame.js`** with `locate.js`, `act.js`, `submit.js` | Declared in the manifest for the required ATS hosts, `all_frames: true`, `run_at: document_idle`; registered with `scripting.registerContentScripts` for an optional host you granted. Announces its frame to the worker; asks the worker for this ATS's form data (cached, nothing from the page sent); draws the buttons; on a message, snapshots fields (017's two steps) and writes one approved value; watches for your submit | Use the network; read field values except in the console-cleared snapshot step; act on page messages; keep values; decide never-store |
| **Service worker** (`background.js`) | As 1e: the only part that talks to the console, stateless apart from `storage.session` entries. New: frame registry per tab, pending submit attempts, assist state for the popup and badge | Hold a session in globals |
| **Side panel** (`panel/`) | As 017: the fill session, bound to its tab. New: the assist card for a resume or letter field, the draft estimate and **Generate**, **Attach**, **Insert**, the applied notice with **Undo** | Run in the page |
| **Console** | `apply/ats_forms.py` (form data per ATS), `apply/page_match.py` (page to job), the fill routes, the draft routes that return 202, `/ext/v1/applied` and undo | Trust anything the page says beyond field descriptors and URLs |

Plain JavaScript modules, no build step, as 1e. Classification, mapping, never-store, job
matching and the applied rule live in Python, so the tested code is the deciding code.

### Hosts

Required hosts are the four public ATSs, taken from `pipeline/ats_rules.py` (a test keeps the
manifest equal to it, as 017 specified):

| ATS | Hosts | Form | Embedded |
|---|---|---|---|
| Greenhouse | `boards.greenhouse.io`, `job-boards.greenhouse.io`, `job-boards.eu.greenhouse.io` | One page (`#app` on the posting) | `/embed/job_app?for=&token=` iframe on employer pages |
| Lever | `jobs.lever.co`, `jobs.eu.lever.co` | One page, `/<company>/<id>/apply` | Rare; handled the same way |
| Ashby | `jobs.ashbyhq.com` | One page, `/<org>/<id>/application` | `.../embed` iframe on employer pages |
| Workable | `apply.workable.com` | One page, `/<account>/j/<code>/apply` | Rare |
| **Workday** (optional) | `*.myworkdayjobs.com`, `*.myworkdaysite.com` | Several steps behind **Next**, after you sign in | No |

Workday is an `optional_host_permissions` entry, granted by your click on **Enable for Workday**
in the options page (Chrome shows its own prompt). NEOGOV stays out (checklist only, as 017).

The employer's own top page is never read. On an employer page with an embedded form, the
content script runs inside the ATS iframe only, because only the iframe's host is in the
manifest.

## 1. Fill on click

### Flow

1. **Open the form.** Through **Open application** on the packet or job page (so 015's click
   log works), from an email, or by browsing. Nothing happens on load except the buttons and
   the passive listeners.
2. **Click** the toolbar icon (the popup shows **Fill this form** when an ATS frame announced
   itself, as 017 Flow step 2) or the **Fill** pill that sits at the top of a recognized
   application form. Either opens the side panel for this tab.
3. **Match the job** ([Matching](#matching-the-page-to-a-job)). The panel names the job, or asks
   which one, or offers **Add this job**.
4. **Start.** The console opens a `fill_session` for the matched application (creating the
   application at `preparing` if there is none, as **Prepare** does). Cap 10 sessions a day,
   hard cap 25, idle close after 2 hours, as 017.
5. **Snapshot before**, in 017's two steps: descriptors without values, the console clears the
   fields that may send values, then values for those only.
6. **Resume.** If the packet has a clean resume version, **Attach resume** (017
   [Resume attach](017-assisted-apply.md#resume-attach)); or attach your own file by hand and
   click **Attached**; or skip. Then settle on the ATS's `parse_done` signal, as 017.
7. **Snapshot after** and classify ([below](#classification)).
8. The panel shows **Will fill** (ticked), **Filled already** (by the parse or before), **Needs
   you** (yours, differs, listed) and **Drafts** (Insert or Draft). **Fill** writes the ticked
   rows, one field and one value at a time, each re-checked empty just before the write, as
   017 Flow step 8.
9. **Stop before Submit.** The Submit control gets the dashed outline, "jobhunter stops here.
   You submit." The session stays open for the submit watch ([Recording
   applied](#3-recording-applied)).

On a form you have filled before (a returning-candidate prefill), most rows are **Filled
already** and nothing is overwritten.

### The answer bank

Fill draws only on what you saved. Nothing in this table is ever sent to a model (it joins
017's **Never sent** list); values travel from the console to the extension one field at a time,
at the moment of the write, and the extension keeps none of them.

| Source | Holds | Fill behaviour |
|---|---|---|
| `/prefs` **Application answers**, existing | Links (portfolio, GitHub, LinkedIn, website), notice period, relocation, remote preference, work authorization yes/no | Mapped fields ticked by default |
| `/prefs` **Contact**, new | First name, last name, preferred name, email, phone, city, state or region, country | Mapped fields ticked by default. **Copy from my resume header** on `/prefs` fills the boxes from your resume's first lines for you to check and save; it never saves by itself |
| `/prefs` **Sponsorship**, new | "Will you require visa sponsorship?" yes/no | Ticked by default when the question matches a known pattern exactly (`labels.py`); otherwise suggested |
| `/prefs` custom answers, existing | Templates you saved or promoted, by normalized question | Ticked when the field's normalized label equals the saved question; suggested (unticked) on a synonym match |
| Earlier answers on other packets (`answers.earlier_answers`), existing | Your answers to the same question for other jobs | **Suggested, never ticked**: an answer to "Why do you want to work here?" names the other employer |
| This packet's saved answers and drafts, existing | Drafts and your edits | **Insert** on your click, as 017 row 7 |

**Not in the bank, ever:** the [never-store list](017-assisted-apply.md#never-store-list) (pay,
EEO and self-ID, citizenship, street address, ZIP or postal code, pronouns, date of birth, SSN,
consent, certification, signature), passwords, references' contact details, criminal history.
City, state and country are allowed; the street address line and postal code are not, and
`labels.py` already refuses them. The contact block is checked by the same `Answers` model on
load and save, so a never-store label cannot be added there by a hand edit.

### Classification

017's [table](017-assisted-apply.md#field-classification) unchanged in order. Row 1 (`yours`)
still beats every other row. Changes:

| Row | Change |
|---|---|
| 6 `fill` | Adds the contact block and sponsorship. Name fields map by `ats_forms.py` field names first (`first_name`, `last_name`, Workday's `legalNameSection_firstName`, ...), then `labels.py` synonyms. A single "Full name" field gets first plus last name. Phone fields that the site splits into country code and number are `listed`, not guessed. Email gets your contact email, never one parsed from the page |
| 7 `paste` | Adds `/prefs` custom answers and earlier answers, as in the table above, with their tick defaults |
| 8 `draft` | Unchanged: **Draft** sends the field's label, help text and options through the normal question-draft call |

**Which fields are ticked.** Only rows 6 and 7 can be ticked, and only with the defaults above.
You can untick any row, or tick a suggested one. **Fill** writes only ticked rows.

### Fill mechanics and the stop before Submit

As 017 ([Fill mechanics](017-assisted-apply.md#fill-mechanics), [The stop before
Submit](017-assisted-apply.md#the-stop-before-submit)): native setters with bubbling `input` and
`change`, one `click` for a checkbox through `safeDispatch`, no keyboard events, no
`chrome.debugger`, synthetic events that a site ignores make the field `listed`. The lint and the
e2e submit counters apply to every fixture in this spec, including the button and submit-watch
paths. The submit watch adds listeners, never dispatches, and never calls `preventDefault` or
`stopPropagation` (lint).

## 2. Resume and letter buttons

### Where the buttons appear

`apply/ats_forms.py` names, per ATS, the fields a button goes beside:

| Field kind | Examples | Button | Click does |
|---|---|---|---|
| `resume_file` | Greenhouse "Resume/CV" file input, Ashby's "Autofill from resume" box and its Resume field, Lever "Resume/CV", Workday "Upload" on My Experience | **jobhunter resume** | Opens the assist card for the resume |
| `letter_file` | Greenhouse and Lever "Cover letter" uploads | **jobhunter letter** | Opens the assist card for the letter, as a PDF |
| `letter_text` | A cover-letter textarea, or "Paste your cover letter" | **jobhunter letter** | Opens the assist card for the letter, as text |
| `resume_text` | "Paste your resume" textarea | **jobhunter resume** | Opens the assist card, as text |

The data names targets by selector and by label synonym ("Resume", "CV", "Résumé", "Cover
letter", "Motivation letter"), so a renamed field on a supported ATS is still found. A field the
data does not name gets no button. `locate.js` matches the data to the DOM, reading only
element types, `name`, `id` and label text, never values.

The button is a 20 px icon with a tooltip, drawn in a **closed shadow root** on a host element
placed beside the field (never inside the ATS's own widget), with `prefers-color-scheme` styles.
It answers only `isTrusted` clicks. A `MutationObserver` re-places it when the form re-renders.
The options page has **Show buttons on forms** per ATS, on by default.

### Opening the panel

A button click asks the worker to open the side panel for this tab with that field selected.
Chrome lets `sidePanel.open()` run only in response to a user action; whether a click relayed from
a content script counts is a spike item (A1). If it does not, the worker opens the same page in a
small extension window (`chrome.windows.create({type: "popup"})`, which needs no permission),
bound to the tab as the side panel is. Either way the confirm buttons live in an extension page.

### The assist card

The card shows the matched job and the packet state, and offers one next step:

| State | Card shows | Next step |
|---|---|---|
| Job not matched | "Which job is this?" with the candidates, or **Add this job** ([Matching](#matching-the-page-to-a-job)) | Pick, or Add |
| No application or packet | The job, "No packet yet" | **Prepare** (`packets.prepare`, free), then the Generate row |
| No version of this kind (letter: none yet) | Runner line ([below](#runner-and-cost)); for a letter, the employer notes box (one to three lines, optional, as 017) | **Generate** |
| Drafting | "Drafting on your subscription, started 14:02"; the console page shows the same | Wait; the card polls |
| Draft failed | The reason; on the CLI runner, the API offer with its estimate | **Run on the API (≈ $0.12)**, its own click |
| Version with unsupported lines | "v3: 2 lines need your review" | **Review in jobhunter** (opens `/packet/{id}#resume`) |
| Clean version | Its change notes, counts of lines kept, moved and omitted, and the entailment pass's "partly" and "no" count | **Attach v3** (or **Insert v3** for a text field); **Review in jobhunter** |
| Clean version, packet not ready and every document clean | As above | **Mark ready and attach** (one click does both); or **Attach v3** alone |
| No PDF possible (no `browser` extra, or a container without the Chromium target) | "PDF export needs the browser extra" | **Insert as text** where there is a text field; else **Open the files** (the packet page's export) and attach by hand |
| Use base resume | Always shown, free | **Attach base resume** (the packet's base version, exported) |

**Attach** exports the version if it is not exported yet (017 Export, with the "trusted only when
the Markdown matches" rule), asks the console for the file, and sets it on **the field the button
sits beside** with 017's attach methods (`datatransfer`, `drop`, `manual`) and `attach_ok` check.
**Insert** writes the plain text (`export.to_text`) into the text field, only if the field is
empty; otherwise the card offers **Copy**. A text field with a `maxlength` shorter than the text
is refused with the two lengths shown. Attaching again after you corrected fields warns, as 017,
that most ATSs re-parse and overwrite your corrections.

Each attach or insert is logged on the fill session (`report`: field key, kind, document id and
version, no content), so the applied record knows what was sent
([Recording](#what-gets-recorded)).

### Runner and cost

Before **Generate** the card shows one line, from `generator.estimate` and `model_access`
(020), for the configured runner:

| Runner | Line | Confirm |
|---|---|---|
| `cli` (default) | "Drafted on your Claude subscription with `claude -p` (opus). No API cost." | **Generate** |
| `cli`, subscription on paid extra usage | "Your subscription is on paid extra usage: this draft is charged, ≈ $0.12; apply cap $0.88 left today" | **Generate, spend ≈ $0.12** |
| `api` | "Drafted on the Anthropic API (claude-opus-5-5), ≈ $0.12; apply cap $0.88 left today" | **Generate, spend ≈ $0.12** |
| Over the apply cap | "Over today's apply cap ($1.00)" | None; **Use base resume** still works |
| Console in a container | "Drafting runs on the host: `jobhunter apply draft 123`" | None; **Open packet** |
| No runner available | 020's no-model-access message with its help link | None |

If 0f9f307 later adds other drafting providers, the line names that provider and its estimate
the same way. A CLI failure never falls through to the API: the card shows the API offer, which
is its own click with the amount on the button, as 017. There is no "don't ask again".

### Drafting in the background

Generation takes up to the CLI's 180-second timeout plus the entailment pass, and an `/ext/v1/`
route must answer within 20 seconds ([017 Popup state](017-assisted-apply.md#popup-state-and-triggers)).
So drafting from the extension follows Score it's shape:

- `generator.generate` is split into `start(...)` (preflight, estimate check against the token,
  the `inflight` claim, synchronously) and `run_claimed(...)` (the runner call, the version write
  or the failure). `generate` stays their composition for the packet page and `apply draft`.
- `POST /ext/v1/packets/{id}/estimate {kind}` returns the runner line, the amount, the cap left,
  any refusal, and a token.
- `POST /ext/v1/packets/{id}/generate {kind, runner, token, confirm_paid, notes, action_id}`
  calls `start`: a changed estimate, a held claim or a refusal answers 409 before anything runs.
  Otherwise **202**, and `run_claimed` runs on a daemon thread with its own connection, as
  `ext_routes.run_score` does.
- `POST /ext/v1/packets/{id}/draft-status {kind}` answers `drafting` (with start time), `done`
  (version, `ok`, unsupported count, change-note count), `failed` (reason, API offer), or
  `stopped` (a claim with no result after the console restarted).

## 3. Recording applied

### What gets recorded

On a confirmed submit the console writes, in one `BEGIN IMMEDIATE` transaction:

- an `application_event` `applied`, `source = 'manual'`, note "submitted in browser, <ATS>
  confirmation seen (jobhunter extension)", `at` = the console's time on receipt;
- the application at `applied` (created if the group had none, as "Did you apply?" *Yes* does);
- the packet attach: `attach_sent_packet`, extended so that when the packet is not `ready` it
  attaches **the versions this tab's session attached or inserted** (from the session report);
- an `ext_applied` row with the undo data ([Data](#data)).

The "Did you apply?" *Yes* path (`detail.answer_prompt`) and this path share one new function,
`tracking.mark_applied(conn, group_id, at, note, data_dir, attached=None)`, so the application
create, the status update and the packet attach cannot drift apart. An application already at
`applied` or later gets **no new event**; the answer says "already recorded" (as a stale *Yes*
does today, 950d5eb).

A later confirmation email still produces its usual proposal; accepting it adds the email's
event, which corroborates and does not double count (status uses the earliest `applied` date).

### Signals

All per ATS, as data in `apply/ats_forms.py`, recorded from fixtures and checked by your own
submits ([ask mode](#ask-mode-and-auto-mode)):

| Signal | How the content script sees it |
|---|---|
| **Your submit** | A `submit` event with `isTrusted` true on the application form, or an `isTrusted` `click` on the control the data names as Submit (for forms that submit by `fetch` from a `type=button`). Listeners are passive, capture phase, on the form's own document |
| **Not a submit** | Controls the data names as step or other controls: **Next**, **Back**, **Save and continue**, **Save draft**, the posting page's **Apply** that scrolls to the form, search boxes, file pickers. A click on them never starts an attempt |
| **Confirmation** | Per ATS, one or more of: a URL rule on the next document in the same frame (for example a `/confirmation` or `/thanks` path, to be confirmed at fixture capture); an element rule (a named confirmation container appearing while the form disappears); never a bare text match such as "Thank you", which posting pages also say |
| **Request finished** | A `PerformanceObserver` `resource` entry whose path matches the ATS's submit endpoint, with `responseStatus` 2xx (Chrome 109+, same-origin to the frame). Supporting only: never enough on its own |
| **Validation failure** | Error markers appearing after the attempt: the data's error selectors, a rise in `[aria-invalid="true"]`, the form still present with the Submit control enabled |
| **Challenge** | A CAPTCHA frame, a "verify you are human" view, an email-code prompt (017 [Captcha](017-assisted-apply.md#captcha-and-challenges)) |

### The rule

The worker keeps one **pending attempt** per tab and frame (`frameId` stays the same when a frame
navigates; the new document's content script asks the worker for it), in `storage.session`, for
90 seconds: attempt id, ATS, posting key, time, fill session id if any.

| What follows your submit | Result |
|---|---|
| Confirmation within 90 s, ATS in **auto** mode, job matched to exactly one group | **Recorded.** Notice with **Undo** |
| Confirmation within 90 s, ATS in **ask** mode | Asked: "Did your application go through? **Yes, mark applied** / No". Your answer records (or not) and is logged as an observation |
| Validation failure markers | Nothing recorded; the attempt is dropped. You fix the form and submit again, which is a new attempt |
| Challenge | Nothing recorded; the panel says the tab is yours. A confirmation after you solve it still counts if it comes within 90 s of a new trusted submit |
| Nothing within 90 s (no confirmation, no errors) | Nothing recorded. The popup and panel show "Submitted on Greenhouse at 14:05? **Mark applied**" for 30 minutes |
| Confirmation view with no pending attempt (you reloaded the thanks page, opened it from history, or a page shows it on load) | Nothing recorded, nothing asked |
| Request finished 2xx but no confirmation rule fired | As "nothing within 90 s": asked, never recorded |
| Job not matched, or matched to several groups | Asked: "Which job did you apply to?" with the candidates and **Add this job**; recorded only on your pick |
| ATS without a confirmation rule (a new layout, an unknown host you granted) | Asked, always |

**Ask mode and auto mode.** Each ATS starts in `ask`. Every answer to the question writes a
`submit_observation` row: the ATS, which signals were seen (URL rule, element rule, request
status, error markers), and your answer. No URL, no field, no value. When an ATS has at least 3
observations where you said **Yes** and the confirmation rule fired, and **no** observation where
the rule fired and you said **No**, the panel offers **Record <ATS> submits automatically**. Your
click sets the ATS to `auto`. Any later **Undo** of an automatic record for that ATS sets it
back to `ask` with a note on `/prefs`, so a rule that misfires once stops acting alone.

### Matching the page to a job

`apply/page_match.py::match(conn, frame_url, session_id=None)`, in this order:

| # | Evidence | Strength |
|---|---|---|
| 1 | The fill session or assist card already bound this tab and frame to an application | Decides |
| 2 | An `ats_posting_ref` row for the frame's **posting key** (your earlier pick) | Decides |
| 3 | The posting key equals the key of a group's `apply_link.final_url` or `start_url`, a `job.url` or `job.apply_url`, or a `job_board_ref.apply_url` | Decides when exactly one group matches |
| 4 | An `apply_click` in the last 24 hours whose `final_url` has this posting key | Decides when exactly one group matches |
| 5 | Anything else, or several groups | Never decides: you pick, or **Add this job** |

The **posting key** is the ATS and its posting id from `ats_rules` (Greenhouse board and job id,
with an embed's `for` and `token` mapped to the same key by `paste.direct_board_url`; Lever
company and posting id; Ashby organization and job id; Workable account and shortcode; Workday
tenant and requisition path). The form's URL (`/apply`, `/application`, `#app`) and the posting's
URL give the same key. Employer and title alone never match.

**Add this job** runs 1e's `capture/extract.js` in the ATS frame (the frame's host permission
allows it) and sends the facts to the existing `capture` route, so the posting text, dedupe and
add-or-preview rules are 1e's. A pick or an add writes `ats_posting_ref` for the key, so the next
submit on the same posting is matched without asking.

### What you see

- **On the page**, after a record: a small notice in a closed shadow root at the top corner of
  the ATS frame, for 20 seconds, "jobhunter: marked applied, Senior Platform Engineer at Acme ·
  Undo · ×". It shows only the job's title and employer, which the page already has.
- **The badge** shows ✓ on the tab until it navigates.
- **The popup** shows "Recorded: applied to … at 14:05 · Undo" for 24 hours.
- **The console**: the event on `/pipeline/{id}` and `/job/{id}` carries "recorded by the
  extension" and an **Undo** button for 7 days.

### Undo

`POST /ext/v1/applied/{id}/undo` from the extension, `POST /applied/{id}/undo` from the console
(same-origin, as other console writes). In one transaction, using the `ext_applied` row:

| Was | Undo does |
|---|---|
| The `applied` event | Deleted, by its id. The one exception to "past events are never touched": it is the event the extension wrote and you disowned. Refused, with "later events exist: edit it on the pipeline page", if any event was added to the application after it |
| The application's status | Rebuilt from the remaining events (`rebuild_status`) |
| An application the record created | Deleted when nothing else refers to it (no other event, packet, contact, attachment, rejection); otherwise kept at its rebuilt status |
| `resume_version`, `cover_letter_path` | Restored to the values before |
| `attachment` rows the record added | Deleted |
| `ext_applied` | `undone_at` set. A resend of the same attempt id answers `undone` and records nothing |
| The ATS mode | Set back to `ask` if the record was automatic |

Achievements (021) are computed from these tables, so they follow without a change.

### No false positives: the cases

Each row is a fixture and an e2e test ([Testing](#testing)):

| Case | Expected |
|---|---|
| Submit with a required field empty (client-side validation stops the submit event) | No attempt |
| Submit, server answers with errors shown on the form | Attempt dropped, nothing recorded |
| Submit, CAPTCHA appears | Nothing recorded |
| Submit by `fetch` from a `type=button`, confirmation container appears | Recorded (auto) or asked (ask) |
| Submit, navigation to the confirmation URL in the same frame | Recorded or asked |
| Confirmation URL opened directly, no attempt | Nothing |
| A page script calls `form.submit()` or dispatches a synthetic click on Submit | No attempt (`isTrusted` false, or no event) |
| **Next**, **Save draft**, **Back** on a multi-step form | No attempt |
| The posting page's **Apply** button | No attempt |
| Submit on an employer page's embedded Greenhouse form, confirmation inside the iframe | Recorded or asked, matched by the embed's posting key |
| Two tabs, two forms, one submit | Only the submitting tab's attempt |
| Same attempt sent twice (worker restart) | One event (`action_id` = attempt id) |
| Already applied (an email accepted earlier) | No new event, "already recorded" |
| Undo, then the worker resends the attempt | Nothing recorded |

## Workday and multi-step forms

017 left this for its own design; Workday is in your list, so it is here.

- **Grant.** **Enable for Workday** in the options page requests the optional hosts, then
  registers the same content scripts for them (`persistAcrossSessions: true`). Until then
  Workday gets the packet checklist only.
- **Sign-in is yours.** Workday asks you to sign in or create an account per employer. The
  extension does nothing on sign-in, account and password pages (`ats_forms.py` marks them).
- **Steps.** Workday's application is one single-page app whose steps (My Information, My
  Experience, Application Questions, Voluntary Disclosures, Self Identify, Review) replace each
  other without a navigation. `ats_forms.py` names the step by its heading element. The session
  survives **your** Next click within the same application (same tenant and requisition in the
  URL). Each step gets its own before and after snapshot when it appears; Fill works per step.
- **Attach** only on the step that holds the upload (My Experience), once. Workday parses it into
  work history there, and that history often splits or merges jobs: those rows show as
  `differs`, as 017 row 5.
- **Voluntary Disclosures and Self Identify** are never-store by label and options; every field
  on them is `yours`.
- **Submit** is the Review step's Submit only. Next, Back and Save and Continue never start an
  attempt. Workday's confirmation is a named element on the page after Submit (rule recorded at
  fixture capture; ask mode until your submits confirm it).
- **Buttons** go on My Experience's upload and any cover-letter upload Workday shows.

## 4. Privacy

### What leaves the browser

| Data | Goes to | When |
|---|---|---|
| Nothing | anywhere | On page load: the content script draws the buttons from form data the worker already cached |
| The frame URL (ATS posting URL) and field kind | console on 127.0.0.1 | On a button or Fill click, to match the job |
| Field descriptors (label, name, id, type, option texts), then values of console-cleared fields | console on 127.0.0.1, held in memory for the session | On **Start** and after the attach, as 017. Never-store fields send no value |
| ATS, posting key, attempt and confirmation times, which signals fired | console on 127.0.0.1 | On your submit (attempt) and its outcome. No field, no value, no page text |
| The posting facts (1e capture) | console on 127.0.0.1 | Only on **Add this job** |
| A field's label, help text and options | the drafting runner (your subscription, or the API) | Only on **Draft** for that field, as 017 |
| Resume, posting, employer notes | the drafting runner | Only on **Generate**, as 017 |
| Contact block, `/prefs` answers, saved answers | the extension, one value per write | Only on **Fill** or **Insert**, at the write. **Never to any model** |
| Screenshots, page HTML, cookies, other tabs, history | nowhere | never |

### Local API

Unchanged from 1e ([017 Local API security](017-assisted-apply.md#local-api-security)): POST only,
the paired token compared in constant time, the pinned extension `Origin`, a loopback `Host` even
with `--allow-remote`, `X-Jobhunter-Ext` with 426 below `min_extension`. Content scripts never use
the network (lint); only the worker calls the console, and the token stays in
`storage.local` restricted to trusted contexts. New routes are listed in [Routes](#routes).

### Never-store and "yours" fields

- Decided only in Python (`labels.never_store`), first, at every step: the fields step before
  values are sent, classification row 1, every write request (the console refuses a write to a
  field it classified `yours`), **Draft**, and every answer-bank save.
- The extension never stores a value, so it never stores a salary or EEO answer. The submit watch
  reads no values. The applied record, the session report and `submit_observation` hold keys,
  labels and actions, never values.
- Salary, EEO and self-ID, consent and signature rows are shown under **Needs you** with "This one
  is yours". They are never ticked and cannot be ticked.

### Contact information

Your name, email and phone are already in your resume header, which goes to the drafting runner
and the scorer as Stage 2 sends it ([008](008-compliance.md#personal-data)); that is unchanged.
The new contact block adds no new destination:

- Stored only in `preferences.yaml` in your profile dir (gitignored), under `answers.contact`.
- Never sent to a model, never in a log line, `capture_log`, `fill_session.report`,
  `ext_applied` or `submit_observation`. A test seeds placeholder values (`<you>@example.com`)
  and asserts they appear in no request body, log record or table other than the preferences
  file.
- In the browser, a value exists only in the message that writes it and then in the form field.
- Fixtures and tests use `example.com` addresses and `555-01xx` numbers only; the fixture lint
  refuses others.

### In-page surfaces

The buttons, the **Fill** pill, the Submit outline and the applied notice live in closed shadow
roots, answer only `isTrusted` clicks, and carry no data beyond the job title and employer the
page already shows. Their presence tells a site that an extension is installed, as Jobright's
buttons did; ATSs treat that as normal autofill. **Show buttons on forms** turns them off per ATS,
and Fill still works from the toolbar icon.

## 5. Permissions and manifest

| Key | Today (1e) | 022 | Why |
|---|---|---|---|
| `permissions` | `activeTab`, `scripting`, `storage`, `contextMenus` | adds **`sidePanel`** | The panel ([017 Architecture](017-assisted-apply.md#architecture)) |
| `host_permissions` | `http://127.0.0.1/*` | adds the seven required ATS hosts (from `ats_rules`) | Content scripts in those frames, including iframes on employer pages |
| `optional_host_permissions` | none | `https://*.myworkdayjobs.com/*`, `https://*.myworkdaysite.com/*` | Workday, on your click |
| `content_scripts` | none | one entry: the required ATS hosts, `all_frames: true`, `run_at: document_idle`, `match_about_blank: false`, isolated world | Buttons, Fill, submit watch |
| `side_panel` | none | `default_path: panel/index.html` | |
| Still forbidden | `<all_urls>`, host wildcards beyond the ATS ones, `tabs`, `webNavigation`, `cookies`, `webRequest`, `declarativeNetRequest`, `debugger`, `downloads`, `history`, `notifications`, `clipboardRead`, `externally_connectable`, `web_accessible_resources` | unchanged | The lint checks the exact lists; the 1e rule "no `content_scripts`" becomes "exactly this entry" |

On reload, Chrome shows the new host access on the extension's card ("Read and change your data
on boards.greenhouse.io and 6 other sites"). Unpacked extensions are not disabled for a
permission increase; you see the change on `chrome://extensions`.

### Store review

You load the extension unpacked; nothing here needs the Chrome Web Store, and this spec keeps it
that way. If it were ever published:

- Host permissions plus content scripts on third-party sites put it in Chrome Web Store's
  in-depth review, which takes longer and asks for a justification per host.
- It would need a privacy policy and the "Limited Use" disclosures for user data (form contents,
  personal information), even though nothing leaves the machine but 127.0.0.1 traffic.
- Single purpose ("help you apply to jobs") fits; the capture feature is part of it.
- The manifest `key` (the pinned ID) is dropped from a store package; pairing would move to the
  store ID.
- Fill on the user's click is a common extension category; the reviewer would still check that
  nothing submits on the user's behalf, which the lint and e2e tests already prove.

None of that is planned. A store listing would be its own decision and spec.

## Data

Migrations take the next free numbers when implemented (`0039` onwards today).

```sql
-- 017's fill_session, never built, created here with application_id instead of a required packet.
CREATE TABLE fill_session (
  id             INTEGER PRIMARY KEY,
  application_id INTEGER NOT NULL REFERENCES application(id),
  packet_id      INTEGER REFERENCES application_packet(id),   -- NULL: no packet
  ats            TEXT NOT NULL,
  posting_key    TEXT NOT NULL,
  host           TEXT NOT NULL,
  ext_version    TEXT,
  attach         TEXT CHECK (attach IN ('datatransfer', 'drop', 'manual', 'none')),
  attach_failed  INTEGER NOT NULL DEFAULT 0,
  started_at     TEXT NOT NULL,
  last_seen_at   TEXT NOT NULL,
  finished_at    TEXT,
  outcome        TEXT CHECK (outcome IN ('stopped_before_submit', 'submit_seen', 'applied',
                                         'challenge', 'aborted', 'error')),
  report         TEXT   -- JSON: field keys, labels, actions, attached document ids. Never values
);

CREATE TABLE ext_applied (
  id                    INTEGER PRIMARY KEY,
  action_id             TEXT NOT NULL UNIQUE,  -- the attempt id minted by the content script
  application_id        INTEGER NOT NULL,      -- no REFERENCES: undo may delete the application
  event_id              INTEGER,               -- NULL when 'already recorded'
  job_group_id          INTEGER NOT NULL,
  ats                   TEXT NOT NULL,
  mode                  TEXT NOT NULL CHECK (mode IN ('auto', 'asked', 'mark')),  -- mark: your Mark applied
  created_application   INTEGER NOT NULL DEFAULT 0,
  prior_resume_version  TEXT,
  prior_cover_letter    TEXT,
  attachment_ids        TEXT NOT NULL DEFAULT '[]',  -- JSON list added by the record
  recorded_at           TEXT NOT NULL,
  undone_at             TEXT
);

CREATE TABLE submit_observation (
  id          INTEGER PRIMARY KEY,
  ats         TEXT NOT NULL,
  observed_at TEXT NOT NULL,
  url_rule    INTEGER NOT NULL,   -- 1 when the confirmation URL rule fired
  element_rule INTEGER NOT NULL,
  request_2xx INTEGER,            -- NULL when not observable
  errors_seen INTEGER NOT NULL,
  answer      TEXT NOT NULL CHECK (answer IN ('yes', 'no'))
);

CREATE TABLE ats_record_mode (
  ats     TEXT PRIMARY KEY,
  mode    TEXT NOT NULL CHECK (mode IN ('ask', 'auto')),
  set_at  TEXT NOT NULL,
  reason  TEXT                     -- 'your click', 'undo of an automatic record'
);

CREATE TABLE ats_posting_ref (
  posting_key TEXT PRIMARY KEY,     -- 'greenhouse:<board>:<id>' and so on
  job_id      INTEGER NOT NULL REFERENCES job(id),  -- a job, so merges carry it, as job_board_ref
  set_by      TEXT NOT NULL CHECK (set_by IN ('pick', 'add')),
  seen_at     TEXT NOT NULL
);
```

`answers:` in `preferences.yaml` gains `contact:` (first, last, preferred name, email, phone,
city, region, country; each optional, length-limited) and `requires_sponsorship: bool | None`,
in the `Answers` model, with 014's round-trip write and the `/prefs` section. No `Profile`
change.

Nightly merges: `fill_session` and `ext_applied` hang off `application`, which
`dedupe_url._merge_application` already re-points for packets; it re-points these two the same
way (a test with an extension-recorded application on the losing side).

## Routes

All `/ext/v1/` routes are POST with 1e's checks and answer within 20 seconds. Write routes take an
`action_id`.

| Route | Does |
|---|---|
| `forms` | The form data for all supported ATSs (selectors, synonyms, signals, step names), versioned; the worker caches it per console version |
| `match` | `page_match.match` for a frame URL; candidates when not decided |
| `match/pick` | Your pick; writes `ats_posting_ref` |
| `sessions` (start), `sessions/{sid}/fields`, `.../snapshot`, `.../write`, `.../finish` | 017's fill flow, bound to an application |
| `packets/{id}/estimate`, `generate` (202), `draft-status` | Drafting from the card |
| `packets/{id}/ready` | Mark ready (the existing rule: every document clean) |
| `packets/{id}/file {kind, format}` | The exported PDF (base64 plus SHA-256) or plain text; exports first if needed (15-second limit, else 409 "export on the packet page") |
| `submit/attempt`, `submit/outcome` | Logs the attempt and its outcome; for auto mode with one match, records; otherwise returns the question |
| `applied` | Records on your **Yes** or **Mark applied** (or a pick) |
| `applied/{id}/undo` | Undo |
| `ats/{name}/mode` | Your click on **Record automatically** |

Console (same-origin): `POST /applied/{id}/undo`; the `/prefs` Contact and Sponsorship section
and **Copy from my resume header**; an "Automatic recording" line per ATS on `/prefs` under
Browser extension, with the observation counts and **Back to ask**.

## Testing

All offline, under `tests/conftest.py`'s guard. The Anthropic client and the `claude` CLI are
fakes ([017 Testing](017-assisted-apply.md#testing)); a test that would draft without a confirm
fails because the fake records the call. The browser network is blocked by
`--host-resolver-rules` as in 1e's e2e harness.

| Area | How |
|---|---|
| Fixtures | `tests/fixtures/ats_forms/<ats>/`: hand-written pages in the shape of each ATS's form (Greenhouse classic and `job-boards`, Greenhouse embed on an employer page, Lever, Ashby with the Autofill box, Ashby embed, Workable, Workday steps), each with a confirmation state, a server-error state and a challenge state; plus captured public empty forms when you approve the capture, stripped as 017 says. Synthetic names, `example.com` emails, `555-01xx` phones; the fixture lint applies |
| Form data | `ats_forms.py` selectors and synonyms find the resume, letter, contact and submit fields on every fixture; a fixture with a renamed label is still found by synonym; the step and other controls are classified as not-submit |
| Matching | `page_match` vectors: each ATS's posting URL, form URL and embed URL give the same key; one group decides; two groups ask; an `apply_click` decides only when alone; employer and title never match; a pick is remembered across a merge |
| Answer bank | Contact and sponsorship load, save and round-trip; a never-store label in `contact:` refused on save and dropped on load; Copy from header proposes and does not save; contact placeholders absent from every drafting request body (the CLI fake's stdin and the API fake's body), every log record and every database table |
| Classification | 017's tests plus: contact rows ticked; an earlier answer suggested, unticked; a `/prefs` custom answer ticked on exact match only; split phone fields `listed`; a never-store field never ticked even when its label also matches a contact synonym |
| Drafting | `start` refuses with 409 before any runner call on a stale token, a held claim and over the cap; 202 then `draft-status` reaches `done` with a fake CLI; a CLI failure ends in `failed` with the API offer and no SDK call; `confirm_paid` required on overage; container mode refuses; the packet page shows a draft in progress |
| Attach and insert | The received file's size and SHA-256 equal the export; attach targets the field beside the clicked button only; insert into a non-empty textarea refused; `maxlength` refused; an unsupported version cannot be attached through any route (409) |
| Submit watch | Every row of [No false positives](#no-false-positives-the-cases) on the fixtures, end to end through the worker and the console; ask mode records only on Yes; auto mode records once; the mode switch offered only after 3 agreeing observations and no disagreement; an undo of an automatic record returns the ATS to ask |
| Record and undo | Through the routes: event, status, created application, packet attach (ready and not ready with attached versions), `ext_applied`; already-applied gives no event; undo restores each field and deletes only what it added; undo refused after a later event; a resend after undo records nothing; achievements and the pipeline reflect the undo; a merge re-points `ext_applied` |
| Workday | Steps fixture: session survives Next, per-step snapshots, attach on My Experience only, disclosures `yours`, Next never an attempt, Review Submit is |
| Lint | Manifest lists exactly as [above](#5-permissions-and-manifest); no network APIs in `content/`; no `preventDefault`, `stopPropagation` or `stopImmediatePropagation` in `submit.js`; 017's submit-path list outside `act.js`; `isTrusted` check on every in-page click handler; HTML sinks forbidden everywhere |
| e2e (`e2e` marker) | 1e's harness with the test manifest adding the fixture hosts: buttons appear on each fixture without any console request carrying page data (request log checked); a button click opens the panel or the fallback window; Generate with a fake CLI then Attach; Fill on each fixture with the submit counters at zero; the user-submit positive control moves them; submit watch outcomes as above; the worker stopped mid-attempt (CDP) and the attempt still resolves |

**Acceptance (live, with you, no spend unless you choose):** on real postings you mean to apply
to, one per ATS: the buttons sit on the right fields, Fill fills only what it should, the attach
is accepted, the Submit outline is on the real Submit, and after you submit, the ask-mode
question appears and your answer is logged. The confirmation rules are adjusted from what those
submits show.

## Failure modes

| Failure | Behaviour |
|---|---|
| Console down or extension unpaired | Buttons still drawn (form data cached); a click says "Start jobhunter" or "Pair the extension"; the submit watch keeps the attempt and asks later if the console comes back within 30 minutes, else forgets it |
| Form data stale after an ATS redesign | Fewer buttons, more fields `listed`, confirmation rule silent so submits are asked; a fixture refresh bug |
| Side panel cannot open from a page click | The extension window opens instead |
| Draft fails on the subscription | The API offer, its own click |
| No PDF export available | Insert as text, or attach by hand from the packet page |
| Site rejects the attach | 017's manual fallback |
| A page fakes a click on a jobhunter button | Ignored (`isTrusted` false) |
| A page shows a confirmation view without a submit | No attempt, nothing recorded |
| Tab closed between submit and confirmation | The attempt expires; the popup offers **Mark applied** for 30 minutes |
| Worker stopped while an attempt is pending | The attempt is in `storage.session`; the new document's content script finds it |
| Wrong job recorded | **Undo**, then pick the right one |
| Two submits of the same form (a double click) | One attempt id per form document within 5 seconds; one event |
| Workday session lost after you navigate to another employer's tenant | Session ends; a new Fill starts one |

## Deviations from earlier specs

| Spec | Today | Proposed | Why |
|---|---|---|---|
| [017](017-assisted-apply.md#phase-2-gate) | Phase 2 only after its gate | Gate waived | Your decision, 2026-10-11 |
| [017](017-assisted-apply.md#coexisting-with-jobright) | Coexist with Jobright | Removed | Your decision, 2026-10-11 |
| [017](017-assisted-apply.md#phase-2-data) | `submit_seen` is not an `applied` event | Confirmed submits record `applied`, with undo | Your request (d23b093) |
| [017](017-assisted-apply.md#architecture) | Content scripts do nothing on load | Buttons and passive listeners on load, no reads or sends | Buttons on the field are the feature |
| `console/tracking.py::add_event` (007's event log) | "Past events are never touched" | Undo deletes the one event the extension wrote, by id, within 7 days, only when no later event exists | A machine-recorded event you disown should go, not be countered by another event |
| [001](001-goals-and-scope.md#non-goals) | 017's clause: help with one form you chose, never submitting | Unchanged in substance; adds "and record that you applied when you submit" | Recording is tracking, which 001 is about |

## Tickets

Filed in git-bug with labels `next`, `extension`, `area:apply`, `spec-022` and the difficulty and
agent labels below. Worker and reviewer per the `CLAUDE.md` table.

| # | Ticket | Difficulty | Worker / reviewer | Depends on |
|---|---|---|---|---|
| A1 | Offline spike on fixture pages: content scripts on ATS hosts and in an embedded iframe; `sidePanel.open()` from a relayed content-script click, else the popup-window fallback; `PerformanceResourceTiming.responseStatus` in an ATS frame; closed-shadow button placement and re-placement on re-render; `frameId` stable across a frame navigation; `registerContentScripts` for a granted optional host. Findings recorded on the bug | normal | sonnet / opus | |
| A2 | Extension skeleton: manifest (sidePanel, ATS hosts, the content-script entry, Workday optional hosts), `content/frame.js` frame announce, `forms` route and worker cache, panel shell bound to the tab, popup **Fill this form**, lint updates, e2e harness with fixture hosts | normal | sonnet / opus | A1 |
| A3 | `apply/ats_forms.py` data for the four ATSs (fields, synonyms, submit, step and other controls, confirmation and error rules, `parse_done`, `attach_ok`), `apply/page_match.py` with posting keys, `ats_posting_ref` migration, `match` and `match/pick` routes, vectors | hard | opus / opus xhigh | |
| A4 | Answer bank: `contact` and `requires_sponsorship` in `Answers`, the `/prefs` section, Copy from my resume header, never-sent tests, help update (`profile-and-resume.md` or the extension topic) | normal | sonnet / opus | |
| A5 | Console fill: `fill_session` migration, sessions and the two-step snapshot routes held in memory, `fill_classify` with the 022 row changes and tick defaults, per-field value release and write refusal, caps and idle close, API contract tests | hard | opus / opus xhigh | A3, A4 |
| A6 | Extension fill: `locate.js`, stable field keys, `act.js` with `safeDispatch`, Start, Fill, Insert, Draft, Use packet value, Submit outline, re-injection; e2e with submit counters on every fixture | hard | opus / opus xhigh | A2, A5 |
| A7 | Drafting in the background: `generator.start` and `run_claimed`, the `estimate`, `generate` (202) and `draft-status` routes, runner lines including overage and container, the packet page showing a draft in progress; money tests | hard | opus / opus xhigh | |
| A8 | Field buttons and the assist card: buttons in closed shadow roots, opening the panel or window, every card state, `ready` and `file` routes, Attach and Insert on the clicked field, Add this job through 1e capture in the frame, session report of attached versions; e2e | hard | opus / opus xhigh | A2, A3, A7 |
| A9 | Submit watch in ask mode: `submit.js` listeners, pending attempts in the worker, confirmation and error watching across a frame navigation, the question in panel and popup, Mark applied for 30 minutes, `submit/attempt` and `submit/outcome`, `submit_observation` migration; every false-positive fixture | hard | opus / opus xhigh | A2, A3 |
| A10 | Recording and undo: `tracking.mark_applied` shared with `answer_prompt`, `ext_applied` and `ats_record_mode` migrations, `attach_sent_packet` with attached versions, the `applied` and undo routes (extension and console), auto mode and its offer, the page notice, popup and pipeline Undo, merge re-pointing | hard | opus / opus xhigh | A9 |
| A11 | Workday: Enable for Workday, registered content scripts, step-aware sessions and snapshots, attach on My Experience, Review-only submit, Workday form data and fixtures | hard | opus / opus xhigh | A6, A8, A10 |
| A12 | Help and specs: `browser-extension.md` (Fill, buttons, applied and undo, ask and auto modes, Workday), `drafting.md` (drafting from the extension), README help table, 017 sections marked as superseded where 022 changes them, 009 roadmap line | mechanical | haiku / sonnet | A6, A8, A10 |

**Order.** A1, A3, A4 and A7 can start at once (four workers). Then A2; then A5 (after A3, A4),
A9 (after A2, A3) and A8 (after A2, A3, A7); then A6 and A10; then A11 and A12.

**Estimate**, by the calibrated rule (mechanical 0.75 h, normal 1.4 h, hard 2.1 h; schedule = sum
/ 3, plus 30%; range 0.7x to 1.6x; floor = the longest dependency chain):

- Twelve tickets: 8 hard (16.8 h) + 3 normal (4.2 h) + 1 mechanical (0.75 h) = **21.75 h**.
- Schedule: 21.75 / 3 = 7.25 h, plus 30% = **about 9.4 h**; range 6.6 to 15.1 h.
- Floor: A1, A2, A9, A10, A11 = 1.4 + 1.4 + 2.1 + 2.1 + 2.1 = 9.1 h, and A3, A5, A6, A11 = 8.4 h.
  The floor (9.1 h) is just under the schedule, so **about 9.4 h**, range **9.1 to 15.1 h**.
- Tokens: 8 x 2.0M + 3 x 0.6M + 0.5M = **about 18.3M**.
- Not included: your live acceptance runs (one real application per ATS), fixture capture if you
  approve it, and PR merge waits.

## Open questions

Each has a default; the spec is written to the default.

1. **Buttons on load.** Draw the resume and letter buttons as soon as a supported form loads
   (default), or only after you click the toolbar icon on that tab? On load is what you asked
   for; it also tells the site an extension is present.
2. **Attach from the panel.** Allow **Attach** of a generated version from the panel when its
   checker report is clean, with the change notes shown there (default), or always require the
   full review on the packet page first?
3. **Automatic recording.** Start each ATS in ask mode and offer automatic recording after 3
   agreeing submits (default), or record automatically from the first confirmed submit, with
   Undo as the only guard?
4. **Contact block.** Store your name, email, phone and city in `/prefs` answers so Fill can fill
   them (default), or leave those fields to each site's resume parse and to you?

## History

### Revision 1

Written from d23b093's two comments (2026-10-11): the three requested features, then the scope
change when you turned Jobright off. Checked against the 1e extension (`extension/`),
`console/ext_routes.py`, `apply/generator.py`, `apply/export.py`, `apply/answers.py`,
`apply/labels.py`, `console/tracking.py`, `console/detail.py::answer_prompt` and
`console/proposals.py` on `main` at `8836891`.
