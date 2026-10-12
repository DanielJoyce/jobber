# 022 — Extension apply assist: fill on click, resume and letter buttons, applied on submit

Status: **draft for review, revision 4** (bug `d23b093`). Revision 1 was written 2026-10-11
against `main` at `8836891`. Revision 2 (2026-10-12) takes your answers to revision 1's four questions,
three new requests, and the review's findings; revision 3 takes the re-review; revision 4 drops
the contact-header redaction at your word and fixes the rest ([History](#history)).
017 phase 1 (1a to 1e) is merged; nothing of 017 phase 2 is built. No code yet.

You asked (2026-10-11) for three things in jobhunter's extension: a way to generate a tailored
resume or cover letter for the job you are viewing; small buttons for that on the resume and
cover-letter fields of the application form, as Jobright's **Customize** button did; and, when
you submit the form, jobhunter recording that you applied. Later the same day you said you are
turning Jobright off, so jobhunter's extension becomes your only apply helper and must also do
what Jobright did: **fill the form from your saved answers when you click**. On 2026-10-12 you
added: Fill on **any** site from the toolbar icon, and a **Record submits** switch apart from
**Show buttons**. (You also asked to keep the resume's contact header out of model requests, then
dropped it the same day: "Let's not worry about it, I already use job services that have my
name." The resume goes to models as 017 sends it.)

This spec is built on [017](017-assisted-apply.md). It reuses 017 phase 2's design (content
scripts on the ATS hosts, the side panel, the two-step snapshot, `act.js` and `safeDispatch`, the
never-store list, the `/ext/v1/` security) and changes it where your decisions require:

| You get | How it works | Never |
|---|---|---|
| **Fill** | On a Greenhouse, Lever, Ashby or Workable form (Workday when you enable it), click the jobhunter icon or the small **Fill** pill on the form. On **any other site**, the toolbar icon offers **Fill this page** for the page you are on. The side panel lists every visible field: what it will fill and from which saved answer, what the site already filled, and what is yours. **Fill** writes the ticked rows | Fill on page load, touch salary, EEO, consent or signature fields, write a hidden or disabled field, overwrite a field that has a value (except the named actions in [Overwrites](#overwrites)), click Submit |
| **Resume and letter buttons** | On the four ATSs (and Workday), a small jobhunter button sits beside the resume upload, the cover-letter upload and the cover-letter text box. Click it: the side panel shows the packet for this job, which runner drafts it and what that costs, and waits for **Generate**. When the draft passes the checks, the panel shows each changed line with its cited source; **Attach** puts the PDF in that upload, or **Insert** puts the letter text in that box | Spend without your confirm, attach a draft with an unsupported line or an unticked "partly" line |
| **Applied on submit** | On the four ATSs (and Workday), when **you** press Submit and the site shows its confirmation, the extension tells jobhunter, which records `applied` for the matched job. Each ATS asks you first; after 3 submits where your answer matched, it offers to record on its own. A notice offers **Undo** | Record a submit the site rejected, a submit a page script made, guess the job, or click Submit itself |

**jobhunter still never submits an application** ([017](017-assisted-apply.md#non-goals),
[008](008-compliance.md#what-we-will-not-build)). The extension has no code path to Submit; this
spec adds a passive listener that notices *your* submit, nothing that causes one.

## Decisions

| Question | Choice | Why |
|---|---|---|
| 017's phase 2 gate (8 ready packets in 4 weeks, then a spike) | **Waived** for everything in this spec (your decision, 2026-10-11). The offline spike stays, as ticket A1 | You no longer have another helper, so there is nothing to measure the need against |
| Coexisting with Jobright ([017](017-assisted-apply.md#coexisting-with-jobright)) | **Dropped** | Your decision: Jobright is off |
| What Fill fills | Your saved answers, **including a contact block** (name, email, phone, city) and a sponsorship yes/no, plus saved custom answers. Gaps only | Your answer (2026-10-12): store the contact block locally on `/prefs` |
| Does a fill session need a packet? | **No.** A session belongs to the matched application. A packet adds the resume to attach and the drafts to insert | Many applications are quick ones with your base resume |
| Fill outside the four ATSs | **Fill this page** from the toolbar icon on any site, through the existing `activeTab` grant: top frame only, no new permission, best-effort matching, no buttons and no submit watch there | Your request (2026-10-12) |
| When the buttons appear | **On page load, on the supported ATS hosts only**, in one overlay in a closed shadow root ([Buttons](#where-the-buttons-appear)) | Your answer (2026-10-12) |
| Switches | Per ATS in the options page: **Show buttons** (on: the resume and letter buttons and the **Fill** pill) and **Record submits** (on: the submit listeners, the in-page ask prompt and the in-page applied notice), independent. Content scripts learn both from the `forms/get` answer. **Show buttons** off removes the buttons and the pill only; **Record submits** off removes the listeners, the prompt and the notice. Neither removes the Submit outline, which belongs to a fill session ([One overlay](#where-the-buttons-appear)). Auto mode is stored on the console and **survives toggling**: turning Record submits back on resumes in the mode it had | Your request (2026-10-12) |
| Where spending is confirmed | **Only in extension pages** (the side panel or the extension's own popup window). The worker accepts spend and write messages only from extension pages ([Sender policy](#sender-policy)) | A page can script clicks on anything in its own DOM; it cannot reach an extension page |
| Which runner drafts | The **effective** drafting runner (`models.toml` over `config.toml`, as 020): `cli` by default (`claude -p` on your subscription, no API cost). The panel names the runner and model before **Generate**; an API run shows its amount and needs its own click | [017 CLI runner](017-assisted-apply.md#cli-runner), [020](020-model-selection.md) |
| Attaching a generated version from the panel | Allowed when the checker report is clean **and** the entailment pass ran and found no unsupported line; every "partly" line needs your tick in the panel, which shows each changed line beside its cited source. Anything else opens the packet page | Your answer (2026-10-12), with the stricter rule |
| What counts as "your submit" | A trusted pointer click on the Submit control, or a trusted Enter in a field of that form, within about 1 second, with `navigator.userActivation.isActive`, and (for a form) the `SubmitEvent.submitter` being that control ([The rule](#starting-an-attempt)) | A trusted `submit` event alone does not prove it: `button.click()` and `requestSubmit()` from page script also fire one |
| What counts as "applied" | Your submit, then the site's confirmation, on the same tab and frame, within 90 seconds, for a job matched with the session's posting key | No false positives |
| How auto-recording is switched on | Each ATS starts in **ask** mode. After 3 asked submits where your answer agreed with the confirmation rule and none disagreed, the panel offers **Record <ATS> submits automatically**; your click turns it on, for that ATS, form variant and rule version | Your answer (2026-10-12) |
| Undo | One click, for 7 days, from the page notice, the popup or the pipeline page ([Undo](#undo)) | A wrong `applied` must be cheap to take back |
| Event source | `application_event.source = 'manual'` with a fixed note, plus an `ext_applied` row that holds the undo data. No change to the `source` CHECK | Changing a CHECK means rebuilding `application_event`; 017 said "no new `application_event.source`" |
| Fill this page: where it refuses | `[capture] disabled_hosts`, the console's own origin, LinkedIn and Indeed (017: the extension never acts on linkedin.com; Easy Apply is theirs), and Workday hosts until **Enable for Workday** | Keeps 017's rule; Workday needs the step-aware session |
| Distribution | **Load unpacked**, as now | [Store review](#store-review) |

Recorded on `d23b093`.

## What changes in 017

| 017 says | 022 says |
|---|---|
| Phase 2 is built only if the gate passes | Gate waived; built as the tickets below |
| Coexist with Jobright | No Jobright |
| `fill_session.packet_id NOT NULL`; Start needs a `ready` packet with a rendered PDF | `fill_session.application_id NOT NULL`, `packet_id` optional ([Data](#data)) |
| `submit_seen` is a hint, never an `applied` event | Recorded as `applied` under [Recording applied](#3-recording-applied), with undo |
| Content scripts never act on page load | They draw the buttons at load and, with **Record submits** on, add passive listeners. They write no field and send nothing to the console without a click or your submit |
| Multi-step forms (Workday, NEOGOV) need their own design | Designed here for Workday ([Workday](#workday-and-multi-step-forms)); NEOGOV stays checklist-only |
| Row 6 fills links, notice period, relocation, work authorization | Adds the contact block and sponsorship ([Answer bank](#the-answer-bank)) |
| Row 7 pastes only saved answers on this packet | Also `/prefs` custom answers and earlier answers ([Answer bank](#the-answer-bank)) |

A note in 017's status line points here. Everything else in 017 phase 2 stands; "as 017" means
that section unchanged.

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
   ├ locate.js        finds the named fields; one overlay          apply/ats_forms.py (data)
   ├ act.js           the only writer (setValue, setChecked,       apply/page_match.py
   │                  attach, safeDispatch)                        apply/fill_classify.py
   └ submit.js        passive listeners: your submit, then          apply/labels.py (never-store)
                      the confirmation or error markers            generator start/run (202)
 Any other site: the same scripts injected into the top frame      tracking.mark_applied
   on your icon click (activeTab), without submit.js              ext_applied, fill_session
        │ chrome.runtime messages only (sender policy)
        ▼
 service worker (stateless relay; frame registry and pending
 attempts in storage.session) ── fetch, bearer token ─────────►
        ▲
 side panel (or popup window) ── session, Fill, Generate, Attach, Undo
```

| Part | Does | Does not |
|---|---|---|
| **`content/frame.js`** with `locate.js`, `act.js`, `submit.js` | Declared in the manifest for the required ATS hosts, `all_frames: true`, `run_at: document_idle`; registered with `scripting.registerContentScripts` for an optional host you granted; injected into the top frame on **Fill this page** elsewhere. Announces its frame; gets this ATS's form data from the worker's cache; draws the overlay; on a message, snapshots fields (017's two steps) and writes one approved value; watches for your submit | Use the network; read field values except in the console-cleared snapshot step; act on page messages; keep values; decide never-store; send spend or write messages |
| **Service worker** (`background.js`) | As 1e: the only part that talks to the console. New: the [sender policy](#sender-policy), the frame registry per tab, pending submit attempts, assist state for the popup and badge, re-injection after a reload | Hold a session in globals |
| **Side panel** (`panel/`) | As 017: the fill session, bound to its tab. New: the assist card, the draft estimate and **Generate**, **Attach**, **Insert**, the ask question, **Undo** | Run in the page |
| **Console** | `apply/ats_forms.py`, `apply/page_match.py`, the fill routes, the draft routes that return 202, `/ext/v1/applied`, undo | Trust anything the page says beyond field descriptors and URLs |

Plain JavaScript modules, no build step, as 1e. Classification, mapping, never-store, job
matching and the applied rule live in Python, so the tested code is the deciding code.

### Hosts

Required hosts are the four public ATSs, taken from `pipeline/ats_rules.py` (a test keeps the
manifest equal to it):

| ATS | Hosts | Form | Embedded |
|---|---|---|---|
| Greenhouse | `boards.greenhouse.io`, `job-boards.greenhouse.io`, `job-boards.eu.greenhouse.io` | One page (`#app` on the posting) | `/embed/job_app?for=&token=` iframe on employer pages |
| Lever | `jobs.lever.co`, `jobs.eu.lever.co` | One page, `/<company>/<id>/apply` | Rare; handled the same way |
| Ashby | `jobs.ashbyhq.com` | One page, `/<org>/<id>/application` | `.../embed` iframe on employer pages |
| Workable | `apply.workable.com` | One page, `/<account>/j/<code>/apply` | Rare |
| **Workday** (optional) | `*.myworkdayjobs.com`, `*.myworkdaysite.com` | Several steps behind **Next**, after you sign in | No |

Workday is an `optional_host_permissions` entry, granted by your click on **Enable for Workday**
in the options page. NEOGOV stays checklist-only. The employer's own top page is never read on
these sites; on an employer page with an embedded form, the content script runs in the ATS
iframe only.

### Sender policy

The worker handles `chrome.runtime.onMessage` through one table, `MESSAGE_POLICY` in
`background.js`: each message type names the senders allowed to send it. Anything not in the
table, or from the wrong sender, is refused and logged (type and sender kind, no payload).

| Sender kind | How the worker knows | May send |
|---|---|---|
| **ATS content script** | `sender.id` is this extension, `sender.tab` is set, `sender.url`'s host is a granted ATS host, and `sender.frameId` is the frame that announced itself for that `documentId`. The host and the ATS are taken from `sender.url`, never from the payload | `frame/hello`, `forms/get`, `panel/open` (open the panel at a field; no write), `submit/attempt`, `submit/outcome`, `ask/answer` and `applied/undo` (both undoable, no spend), `frame/bye`. `ask/answer` and `applied/undo` are accepted only for attempts and records the worker holds **for that same tab** (`sender.tab.id`) |
| **Injected generic frame** (Fill this page) | As above, but the tab and frame 0 are ones the worker injected into after your icon click, on a host that is not an ATS host | Only `frame/hello`, `forms/get`, `frame/bye`. Everything else on such a page is driven from the side panel |
| **Extension page** | `sender.id` is this extension and `sender.url` starts with `chrome.runtime.getURL("")` (the side panel, the popup, the popup window, the options page) | Everything else: `session/*`, `fill/write`, `match/pick`, `draft/estimate`, `draft/generate`, `packet/ready`, `file/attach`, `text/insert`, `ats/mode`, `capture/*`, `pair` |

A Python test parses `MESSAGE_POLICY` and fails if any spend type (`draft/generate`), any write
type (`fill/write`, `file/attach`, `text/insert`) or any mode or pairing type allows a content
script, or if a handler exists for a type not in the table. The e2e sends each content-only and
page-only type from the wrong context and expects a refusal.

The console checks too: `submit/outcome` is refused (409) unless it names an attempt id the
console received from `submit/attempt` within the last 90 seconds, for the same posting key and
frame host. `ask/answer` is accepted only for an attempt whose question is open.

## 1. Fill on click

### Flow on the four ATSs

1. **Open the form.** Through **Open application** on the packet or job page (so 015's click
   log works), from an email, or by browsing. Nothing happens on load except the overlay and,
   with **Record submits** on, the passive listeners.
2. **Click** the toolbar icon (the popup shows **Fill this form** when an ATS frame announced
   itself) or the **Fill** pill at the top of a recognized form. Either opens the side panel for
   this tab.
3. **Match the job** ([Matching](#matching-the-page-to-a-job)).
4. **Start.** The console opens a `fill_session` for the matched application, creating the
   application at `preparing` if there is none, with an event note **"fill session started
   (extension)"**, added to `tracking.AUTO_NOTES` so it never outranks a later status. Cap 10
   sessions a day, hard cap 25, idle close after 2 hours, as 017.
5. **Snapshot before**, in 017's two steps.
6. **Resume.** **Attach resume** for a version that passes the [attach rule](#attach-rule), or
   attach your own file and click **Attached**, or skip. Then settle on `parse_done`, as 017.
7. **Snapshot after** and classify ([below](#classification)).
8. The panel shows **Will fill** (ticked), **Filled already**, **Needs you** (yours, differs,
   listed) and **Drafts**. Each row shows the label and **where its value comes from**
   (`/prefs: contact.email`, `this packet: "Why Acme?" v2`), never the value itself. **Fill**
   writes the ticked rows, one field and one value at a time.
9. **Stop before Submit.** The Submit control gets the dashed outline, "jobhunter stops here.
   You submit."

### Fill this page (any other site)

On a site without the content scripts, the popup offers **Fill this page** next to **Send to
jobhunter**, except where it refuses: hosts in `[capture] disabled_hosts`, the console's own
origin (`127.0.0.1`, `localhost` or `[::1]` on the console port), every LinkedIn and Indeed host
(017: the extension never acts on linkedin.com, and Easy Apply is their own form), and Workday
hosts before **Enable for Workday** (the popup offers that instead, because Workday's steps need
the [step-aware session](#workday-and-multi-step-forms)). The refusal list comes from `version`,
with LinkedIn and Indeed also built into the extension as 1e's notice hosts are. NEOGOV is not
refused: Fill this page works there per page, best effort, as on any site. The click grants `activeTab` for the tab, as capture does. The worker injects
`frame.js`, `locate.js` and `act.js` (not `submit.js`) into **frame 0** only, with the console's
**generic** form data (label synonyms for contact, links and the yes/no answers; no selectors),
and opens the side panel. Then steps 3 to 8 as above, with these limits:

| | Fill this page |
|---|---|
| Frames | The top frame only. A form in a cross-origin iframe is not reachable; the panel says so ("this form is inside a frame from <host>; jobhunter can fill it only on the supported ATSs") |
| Matching | Best effort: the page URL, normalized by `dedupe_url.normalize_apply_url`, against `job.page_url`, `job.url` and `job.apply_url` (rule 3 below), deciding only when `capture.identifies_posting` says the URL names one posting; otherwise you pick or **Add this job** (1e capture). Never decided by employer and title |
| Resume | No buttons; **Attach resume** from the panel on a file input the generic data recognizes, else attach by hand |
| Grant ends | `activeTab` ends when the tab navigates. A multi-page form needs a new icon click per page; the panel says "click the jobhunter icon on the new page" |
| Submit | No watch. The panel offers **Mark applied** after you submit, your click ([Mark applied](#mark-applied-by-hand)) |
| Never-store | Unchanged: decided in Python, first |

### The answer bank

Fill draws only on what you saved. Nothing in this table is ever sent to a model (it joins 017's
**Never sent** list). Values travel from the console to the extension one field at a time, at the
moment of the write, and the extension keeps none of them.

| Source | Holds | Fill behaviour |
|---|---|---|
| `/prefs` Application answers (existing) | Links, notice period, relocation, remote preference, work authorization yes/no | Mapped fields ticked by default |
| `/prefs` **Contact** (new) | First name, last name, preferred name, email, phone, city, state or region, country | Mapped fields ticked by default. **Copy from my resume header** fills the boxes from your resume's header for you to check and save; it never saves by itself |
| `/prefs` **Sponsorship** (new) | "Will you require visa sponsorship?" yes/no | Ticked only when the question matches a pattern in `labels.py` whose **polarity** is known ("Will you require sponsorship?" takes the stored value; "Can you work without sponsorship?" takes its inverse). Any other wording is suggested, unticked |
| `/prefs` custom answers (existing) | Templates by normalized question | Ticked on an exact normalized-question match, **except** an answer promoted from a packet whose text names that packet's employer (`answers.promote` now stores `from_employer`; the answer is ticked only on a job at that same employer, else suggested) |
| Earlier answers on other packets (existing) | Your answers to the same question for other jobs | **Suggested, never ticked** |
| This packet's saved answers and drafts | Drafts and your edits | **Insert** on your click, as 017 row 7 |

**Not in the bank, ever:** the [never-store list](017-assisted-apply.md#never-store-list),
passwords, references' contact details, criminal history. City, state and country are allowed;
the street address line and postal code are not. The contact block is checked by the same
`Answers` model on load and save.

### Classification

017's [table](017-assisted-apply.md#field-classification), in order, with these changes:

| Row | Change |
|---|---|
| 0 (new, before row 1) | **Not visible or not editable** → `listed`, never ticked, never written. A field is fillable only if `checkVisibility({checkOpacity: true, checkVisibilityCSS: true})` is true, its box is at least 4 by 4 px and inside the document's scroll area, it and its ancestors have no `aria-hidden="true"` or `inert`, and it is not `disabled` or `readOnly`. The content script reports these facts in the descriptor; the console decides. Honeypot fields (hidden traps for bots) fall here |
| 6 `fill` | Adds the contact block and sponsorship. Names map by `ats_forms.py` field names first, then `labels.py` synonyms. A single "Full name" field gets first plus last name. Phone fields split into country code and number are `listed`. Phone format: the stored value as typed; a field with `pattern` or `inputmode=numeric` gets digits only, and a field that rejects both becomes `listed` |
| 7 `paste` | Adds `/prefs` custom answers and earlier answers, with the tick defaults above |
| 8 `draft` | **Draft** on a free-text question needs a packet (question drafts belong to a packet). Without one, the row offers **Prepare** (free) first. The request is the `question_draft` kind on the draft routes ([Drafting in the background](#drafting-in-the-background)) |

Only rows 6 and 7 can be ticked, with the defaults above. **Fill** writes only ticked rows.

**Right before each write** the content script re-checks row 0 (visible, enabled, editable) and
emptiness; a field that fails either is not written and is reclassified.

### Overwrites

Fill and Insert never write a field that has a value. The only exceptions, each one field on
your click: **Use packet value** and **Restore previous value** on a `differs` row (017 rows 3
and 5), and **Attach** on a file input that already holds a file, which first asks "Replace
<filename> with <your file>?" with **Replace** and **Cancel**.

### Fill mechanics and the stop before Submit

As 017 ([Fill mechanics](017-assisted-apply.md#fill-mechanics), [The stop before
Submit](017-assisted-apply.md#the-stop-before-submit)). The lint and the e2e submit counters
apply to every fixture here, including the button, Fill-this-page and submit-watch paths. The
submit watch adds listeners, never dispatches, and never calls `preventDefault`,
`stopPropagation` or `stopImmediatePropagation` (lint).

## 2. Resume and letter buttons

### Where the buttons appear

`apply/ats_forms.py` names, per ATS, the fields a button goes beside:

| Field kind | Examples | Button | Click does |
|---|---|---|---|
| `resume_file` | Greenhouse "Resume/CV", Ashby's "Autofill from resume" box and Resume field, Lever "Resume/CV", Workday "Upload" on My Experience | **jobhunter resume** | Opens the assist card for the resume |
| `letter_file` | Greenhouse and Lever "Cover letter" uploads | **jobhunter letter** | Assist card for the letter, as a PDF |
| `letter_text` | A cover-letter textarea | **jobhunter letter** | Assist card for the letter, as text |
| `resume_text` | "Paste your resume" textarea | **jobhunter resume** | Assist card, as text |

The data names targets by selector and label synonym, so a renamed field on a supported ATS is
still found. A field the data does not name gets no button. `locate.js` reads element types,
`name`, `id`, label text and the visibility facts, never values. Hidden fields get no button.

**One overlay.** The overlay host exists while it has something to show: buttons or the pill
(Show buttons on and a named field found), the Submit outline (a fill session is open on this
frame), or the ask prompt or applied notice (Record submits on and one is due). It is created on
the first of these and removed when none is left, so with Show buttons off and no session open a
form carries no jobhunter element at all. The content script adds it as one host element, the last child of `body`, with a
closed shadow root, `position: absolute` over the page, `pointer-events: none` except on its
buttons. Each button is a `<button type="button">` with an inline SVG icon (no image files, so
no `web_accessible_resources`), placed beside its field from `getBoundingClientRect()` and
re-placed on scroll, resize and form re-render (a `ResizeObserver` and a `MutationObserver`).
Nothing is inserted inside the ATS's own widgets, so a React re-render cannot remove or duplicate
it (A1 checks this on a React fixture). The buttons answer only `isTrusted` clicks and follow
`prefers-color-scheme`.

**After an extension reload** the worker re-injects the content scripts into open tabs on the
granted ATS hosts (`chrome.tabs.query` with those URL patterns, allowed by the host permissions,
then `scripting.executeScript` with `allFrames: true`). **Orphan removal:** each content script
opens a long-lived `chrome.runtime.connect` port to the worker at start. A reload disconnects
it; the script's `port.onDisconnect` handler **waits about 250 ms** and then checks
`chrome.runtime?.id` (A1 found the id is still set inside the handler after a reload and clears
within about 50 ms, so an immediate check would keep the orphan); when it is gone the script
removes its overlay host element and every listener it added (it keeps them in one list for
this). Each overlay click handler also checks `chrome.runtime?.id` first, so a click on a stale
button does nothing. An idle worker stop (Chrome stops the worker after about 30 seconds) also
disconnects **every** port; then `chrome.runtime.id` is still set, the script keeps its overlay,
and it reconnects **lazily**: only when it next needs the worker (a click, a submit attempt, a
prompt answer), so idle tabs do not wake the worker. A1 checks both disconnect cases. A re-injected script finds an
existing overlay host with its marker attribute and replaces it.

### Opening the panel

A button click sends `panel/open` with the field key. The worker must call
`chrome.sidePanel.open({tabId})` synchronously in the message handler, before any `await`, to
keep the user-gesture context; whether a gesture relayed from a content script counts at all is
an A1 spike item. If it does not, the worker opens the same page in a small extension window
(`chrome.windows.create({type: "popup"})`, no permission needed), bound to the tab. Either way
every confirm lives in an extension page.

### The assist card

| State | Card shows | Next step |
|---|---|---|
| Job not matched | "Which job is this?" with candidates, or **Add this job** | Pick, or Add |
| No application or packet | The job, "No packet yet" | **Prepare** (free) |
| The job has no posting text | "This job has no description, so a tailored version cannot be drafted" | **Add the description** (1e capture in the frame, or 1e **Add this description**); **Use base resume** still works |
| No resume version yet, letter field clicked | "The letter will be drafted against your base resume" | **Generate letter**, or **Generate resume first** |
| No version of this kind | The [runner line](#runner-and-cost); for a letter, the employer notes box (optional, as 017) | **Generate** |
| Drafting | "Drafting on your subscription, started 14:02"; the console page shows the same | Wait; the card polls |
| Draft stopped (console restarted) | "The draft did not finish; nothing was charged on the subscription" (or the API amount the log shows) | **Generate again** |
| Draft failed | The reason; on the CLI runner, the API offer with its estimate | **Run on the API (≈ $0.12)**, its own click |
| Version fails the attach rule | "v3: 2 lines unsupported" or "entailment did not run" | **Review in jobhunter** |
| Version passes the checker and entailment | Every changed line beside its cited source lines; "partly" lines with a tick box each; change notes | **Attach v3** (or **Insert v3**), enabled when every "partly" box is ticked; **Review in jobhunter** |
| As above, packet not ready and every document passes | As above | **Mark ready and attach** |
| No PDF possible | "PDF export needs the browser extra" | **Insert as text** where there is a text field; else **Open the files** |
| Use base resume | Always shown, free | **Attach base resume** |

#### Attach rule

A generated or edited version may be attached or inserted from the panel only when:

1. its `check_report` has no unsupported item and none waiting for **This is true** (017's
   checker, unchanged);
2. the entailment pass ran on this version and returned no `no` line (the panel calls these
   "unsupported");
3. every `partly` line has your tick, given in the panel or on the packet page. Ticks are stored
   in the version's `check_report` as `entail_ack` items and carry forward only for unchanged
   lines, like **This is true**.

With entailment turned off (`[apply] entailment_check = false`) or failed, the panel does not
offer **Attach**; the packet page's export and your own attach still work. The base resume
needs none of this.

**Edited versions.** An edit (`origin = 'edited'`) runs no entailment pass: `write_version`
only carries verdicts forward for lines whose text is unchanged. An edited line has no verdict,
so rule 2 cannot pass for it from the panel. The card says "v4 has 3 edited lines without a
fact check" and routes you to **Review in jobhunter**, where each edited line needs your tick
(**I checked this line**, stored as `entail_ack`) before the panel offers **Attach**. No
entailment run is started by an edit, so editing never spends.

**Attach** exports the version if needed (017 Export rules), asks the console for the file, and
sets it on **the field the button sits beside** with 017's attach methods and `attach_ok` check,
after the visibility re-check and the replace question. **Insert** writes `export.to_text` into
the text field only if empty, else offers **Copy**; a `maxlength` shorter than the text is
refused with both lengths shown. Re-attaching after you corrected fields warns, as 017.

Each attach or insert is logged on the fill session (`report`: field key, kind, document id and
version, no content), so the applied record knows what was sent.

### Runner and cost

Before **Generate** the card shows one line, from `generator.estimate` and 020's
`model_access`, for the **effective** runner and model (`models.toml` over `config.toml`):

| Runner | Line | Confirm |
|---|---|---|
| `cli` (default) | "Drafted on your Claude subscription with `claude -p` (opus): $0 on your subscription; about $0.12 at API prices" | **Generate** |
| `cli`, on paid extra usage | "Your subscription is on paid extra usage: this draft is charged, ≈ $0.12; apply cap $0.88 left today" | **Generate, spend ≈ $0.12** |
| `api` | "Drafted on the Anthropic API (claude-opus-5-5), ≈ $0.12; apply cap $0.88 left today" | **Generate, spend ≈ $0.12** |
| `models.toml` drafting value invalid | 020's fallback notice ("models.toml's drafting model is invalid; using opus from config.toml") above the line | as the effective runner |
| Over the apply cap | "Over today's apply cap ($1.00)" | None; **Use base resume** still works |
| Console in a container | "Drafting runs on the host: `jobhunter apply draft 123`" | None; **Open packet** |
| No runner available | 020's no-model-access message with its help link | None |

The card's call site is registered in `tests/test_model_access_call_sites.py`. A CLI failure never
falls through to the API. There is no "don't ask again".

### Drafting in the background

Generation takes up to the CLI's 180 seconds plus the entailment pass; an `/ext/v1/` route must
answer within 20 seconds. So:

- `generator.generate` splits into `start(...)` (preflight, estimate check against the token,
  and the `inflight` claim, synchronously) and `run_claimed(...)` (the runner call, the version
  write or the failure). `generate` stays their composition for the packet page and
  `apply draft`.
- **Claim handoff.** `inflight.claim` is a context manager today. A7 adds an object form,
  `inflight.Claim.take(data_dir, key)` returning a held claim with `release()` (idempotent), and
  re-expresses the context manager on it. `start` takes it in the request thread and passes the
  object to the draft thread, which releases it in its `finally`, as `deep_routes` does with the
  run lock. The request thread releases it only when `start` fails after taking it.
- **Stored state.** `start` writes a `draft_run` row (packet, kind, question key, runner, started
  time); `run_claimed` sets its outcome. A row with no outcome and no live thread after a restart
  reads as **stopped**.
- Routes: `packets/{id}/estimate {kind, question}`; `packets/{id}/generate {kind, question,
  runner, token, confirm_paid, notes, action_id}` (409 before anything runs on a changed estimate,
  a held claim or a refusal, else **202** with the run on a daemon thread named
  `ext-draft-<id>`, with its own connection); `packets/{id}/draft-status {kind, question}`
  (`drafting`, `done` with version, checker and entailment summary, `failed` with the API offer,
  `stopped`). `kind` is `resume`, `cover_letter` or `question_draft`.
- **Lifecycle.** The runner is reached through `app.state` (`app.state.packet_runner`, the CLI
  runner, and the existing `packet_api_client_factory`), so tests replace both. The lifespan
  shutdown joins live `ext-draft-*` threads (up to the CLI timeout) before closing. `start`
  enters 019 S12b's work registry (`app.state.work`, on `main`) **before** it takes the claim and
  writes the `draft_run` row; the draft thread leaves it in its `finally` (and `start` leaves it
  if it fails), so a Restart answers 409 meanwhile. While a restart is under way the registry
  refuses new entries and the `generate` route answers **503** ("jobhunter is restarting; try
  again in a moment"), with nothing claimed or written.

## 3. Recording applied

### What gets recorded

In one `BEGIN IMMEDIATE` transaction, through a new `tracking.mark_applied(conn, group_id, at,
note, data_dir, attached=None)` that the "Did you apply?" *Yes* path (`detail.answer_prompt`) also
uses:

- an `application_event` `applied`, `source = 'manual'`, note "submitted in browser, <ATS>
  confirmation seen (jobhunter extension)" (or "marked applied in the extension" for **Mark
  applied**), `at` = **the console's time when `submit/attempt` arrived**;
- the application at `applied` (created if the group had none);
- the packet attach: `attach_sent_packet`, extended so that when the packet is not `ready` it
  attaches the versions this tab's session attached or inserted;
- an `ext_applied` row with the undo data ([Data](#data)).

An application already at `applied` or later gets **no new event**; the answer says "already
recorded", the `ext_applied` row has no `event_id`, and **no Undo is offered** for it.

**Confirmation emails.** The mail matcher does not propose a confirmation email for an
application already at `applied` (`mail/match.py::_worth_event`), so an extension record means
no confirmation proposal later. Acknowledgements and rejections are proposed as today.

### Signals

Per ATS, as data in `apply/ats_forms.py`, recorded from fixtures and checked by your own submits:

| Signal | How the content script sees it |
|---|---|
| **Your submit** | See [Starting an attempt](#starting-an-attempt) |
| **Not a submit** | Controls the data names as step or other controls: **Next**, **Back**, **Save and continue**, **Save draft**, the posting page's **Apply** that scrolls to the form, search, file pickers |
| **Confirmation** | Per ATS, one or more of: a URL rule on the next document in the same frame (paths such as `/confirmation` or `/thanks`, confirmed at fixture capture); an element rule (a named confirmation container appearing while the form disappears). Never a bare text match such as "Thank you" |
| **Request finished** | A `PerformanceObserver` `resource` entry for the ATS's submit endpoint with `responseStatus` 2xx. Supporting only |
| **Validation failure** | Error markers after the attempt: the data's error selectors, a rise in `[aria-invalid="true"]`, the form still present with Submit enabled |
| **Challenge** | A CAPTCHA frame, a "verify you are human" view, an email-code prompt |

Each rule carries a **rule version** and the data names the **form variant** it applies to
(Greenhouse classic, Greenhouse `job-boards`, Greenhouse embed, and so on).

### Starting an attempt

An attempt starts only when **all** of these hold:

1. **A trusted gesture on this form**: a `pointerup` or `click` with `isTrusted` on the Submit
   control the data names (or a submit-type control of the form), **or** a `keydown` Enter with
   `isTrusted` in a text field of the same form, recorded by a capture-phase listener;
2. **within about 1 second**, followed by:
   - **for a native form**: the form's `submit` event (which fires only after the browser's
     constraint validation passed, so an invalid form never starts an attempt) whose
     `SubmitEvent.submitter` is that control, or for Enter the form's default button (or `null`
     when the form has none);
   - **for a form that submits by `fetch` from a `type=button`**: the same trusted click is the
     start;
3. and `navigator.userActivation.isActive` is true at that moment.

`button.click()`, `form.requestSubmit()`, `form.submit()` and synthetic events from page script
fail rule 1 (no trusted gesture on that control in the window), so they never start an attempt,
even though the `submit` event they cause has `isTrusted` true. Two gestures within 5 seconds on
the same form document make one attempt.

On a start, the content script sends `submit/attempt` (attempt id, ATS, variant, posting key,
frame host). The console stores it for 90 seconds; the worker keeps it in `storage.session` per
tab and frame (`frameId` stays the same when a frame navigates, so the next document's content
script finds it).

### The rule

| What follows your submit | Result |
|---|---|
| Confirmation within 90 s, ATS variant in **auto** mode at this rule version, job matched through the **session's posting key** to exactly one group | **Recorded.** Notice with **Undo** |
| Confirmation within 90 s, **ask** mode | **Asked** ([Asking](#asking)) |
| Validation failure markers | Nothing recorded; the attempt is dropped |
| Challenge | Nothing recorded; the panel says the tab is yours |
| Nothing within 90 s | Nothing recorded. The badge shows `?` and the popup offers "Submitted on Greenhouse at 14:05? **Mark applied**" for 30 minutes |
| Confirmation view with no pending attempt | Nothing recorded, nothing asked |
| Request finished 2xx but no confirmation rule fired | As "nothing within 90 s" |
| Job not matched, matched to several groups, or the frame's posting key differs from the session's | Asked "Which job did you apply to?"; recorded only on your pick |
| ATS variant without a confirmation rule | Asked, always |

#### Asking

The question must be seen. When an asked outcome arrives, the worker:

- sets the badge to `?` on the tab;
- tells the content script to show a small prompt in the overlay, "jobhunter: did your
  application to <title> go through? **Yes, mark applied** · **No**", whose buttons answer only
  `isTrusted` clicks and send `ask/answer` (undoable, so allowed from a content script);
- calls `chrome.action.openPopup()` when Chrome allows it (A1 records whether it does without a
  gesture); the popup shows the same question.

Every answer writes a `submit_observation` row ([Data](#data)): ATS, variant, rule version,
which signals fired, your answer. No URL, field or value.

#### Ask mode and auto mode

Mode is kept per **(ATS, variant, rule version)** in `ats_record_mode`. When a key has at least 3
observations where you said **Yes** and the confirmation rule fired, and none where the rule fired
and you said **No**, the panel offers **Record <ATS> submits automatically**; your click sets it
to `auto`. A new rule version starts at `ask` again. An **Undo** of an automatic record with the
reason **"I did not apply"** sets that key back to `ask`; an Undo with **"Wrong job"** does not
(the rule was right; the match was wrong) and instead clears the `ats_posting_ref` that made the
match.

#### Mark applied by hand

The panel (and the popup for 30 minutes after an attempt) has **Mark applied** for the session's
job. It records through the same route with mode `mark`, never changes an ATS's mode, and is how
**Fill this page** sites are recorded.

### Matching the page to a job

`apply/page_match.py::match(conn, frame_url, tab_id, session_id=None)`. The worker supplies
`tab_id`; the console never trusts a tab id from page content. For a **content-script** message
it is `sender.tab.id`. For an **extension-page** message (the side panel or the popup window),
`sender.tab` is undefined or is the popup window's own tab, so the worker passes the tab it holds
as **bound to that panel or window** (the `?tab=` binding it opened it with). `fill_session.tab_id`
is set at Start by the same rule:

| # | Evidence | Strength |
|---|---|---|
| 1 | The fill session or assist card already bound this tab and frame to an application, **and** on an ATS the frame's posting key equals the session's (both non-NULL), or on **Fill this page** (posting key NULL, which never compares equal) `tab_id` equals the session's `tab_id` and the page URL normalized by `normalize_apply_url` equals the session's `start_url` | Decides |
| 2 | An `ats_posting_ref` row for the posting key (your earlier pick or add); ATS pages only | Decides |
| 3 | On an ATS, the posting key equals the key of a group's `apply_link.final_url` or `start_url`, a `job.url`, `job.apply_url` or `job.page_url`, or a `job_board_ref.apply_url`. On **Fill this page**, the page URL normalized by `dedupe_url.normalize_apply_url` equals one of those URLs normalized the same way, **and** `capture.identifies_posting(url)` is true | Decides when exactly one group matches; otherwise asks |
| 4 | An `apply_click` in the last 24 hours whose `final_url` has this posting key | Decides when exactly one group matches |
| 5 | Anything else, or several groups | Never decides: you pick, or **Add this job** |

The **posting key** is the ATS, variant-independent, plus its posting id from `ats_rules`
(Greenhouse board and job id, with an embed's `for` and `token` mapped by
`paste.direct_board_url`; Lever company and posting id; Ashby organization and job id; Workable
account and shortcode; Workday tenant and requisition path). Employer and title never match.

A recorded event keeps the **rule number** that matched, the posting key and the session id in
`ext_applied`, so a wrong match can be traced.

**Add this job** runs 1e's `capture/extract.js` in the frame and sends the facts to the existing
`capture` route. A pick or an add writes `ats_posting_ref`.

### What you see

- **On the page**, after a record: a notice in the overlay for 20 seconds, "jobhunter: marked
  applied, <title> at <employer> · Undo · ×", trusted clicks only. It belongs to **Record
  submits**, as the ask prompt does (both appear only where the submit listeners run), not to
  **Show buttons**.
- **The badge** shows ✓ until the tab navigates.
- **The popup** shows "Recorded: applied to … at 14:05 · Undo" for 24 hours.
- **The console**: the event on `/pipeline/{id}` and `/job/{id}` carries "recorded by the
  extension" and **Undo** for 7 days.

### Undo

`ext/v1` `applied/{id}/undo {reason}` and console `POST /applied/{id}/undo` (same-origin).
`reason` is `not_applied` or `wrong_job`. In one transaction:

| Check or step | Rule |
|---|---|
| Stale | Refused with "this record changed; edit it on the pipeline page" when the `ext_applied` row has no `event_id`, is already undone, its event no longer exists, or its application is not the one the event now belongs to (a nightly merge moved it) |
| Later events | Refused with "later events exist; edit it on the pipeline page" if the application has any event with a **higher id** than the recorded one |
| The event | Deleted by id. The one exception to "past events are never touched" |
| `resume_version`, `cover_letter_path` | Restored to the prior values **only if they still hold what the record set**; otherwise left |
| `attachment` rows the record added | Deleted, by id |
| An application the record created | Deleted only if nothing else refers to it: no remaining `application_event`, `application_packet`, `fill_session`, `contact`, `attachment`, `mail_proposal`, `rejection` or `followup_log` row. Otherwise kept |
| A kept application with no events left | `rebuild_status` raises on zero events, so undo first writes one `interested` event with note "applied undone (extension)", which is in `AUTO_NOTES`; then rebuilds |
| A kept application with events left | `rebuild_status` |
| `ext_applied` | `undone_at` and `undo_reason` set; a resend of the same attempt id answers `undone` and records nothing |
| Mode | `not_applied` on an automatic record: that key back to `ask`. `wrong_job`: the `ats_posting_ref` row for the key deleted, mode unchanged |

**Achievements.** 021 computes from these tables. Within 24 hours of an unlock's `detected_at`,
021 revokes a tier that the fresh computation no longer reaches, so an undo within a day takes
back what the record earned. After 24 hours 021's unlocks are permanent and an undo does not
revoke them. This spec keeps 021's rule as it is.

### No false positives: the cases

Each row is a fixture and an e2e test:

| Case | Expected |
|---|---|
| Submit with a required field empty (constraint validation stops the `submit` event) | No attempt |
| Submit, server answers with errors on the form | Attempt dropped |
| Submit, CAPTCHA appears | Nothing recorded |
| Page script calls `button.click()`, `requestSubmit()` or `form.submit()` (its `submit` event has `isTrusted` true) | No attempt: no trusted gesture on the control within 1 s |
| Page script calls `requestSubmit()` right after your trusted click elsewhere on the page | No attempt: the gesture was not on the Submit control, and `submitter` is not it |
| A synthetic click on Submit | No attempt (`isTrusted` false) |
| Your trusted click on Submit of a `fetch` form, confirmation appears | Recorded (auto) or asked |
| Your Enter in a field, native form submits, navigation to the confirmation URL | Recorded or asked |
| Confirmation URL opened directly | Nothing |
| **Next**, **Save draft**, **Back** on a multi-step form | No attempt |
| The posting page's **Apply** button | No attempt |
| Embedded Greenhouse form, confirmation inside the iframe | Recorded or asked, matched by the embed's posting key |
| Session bound to job A, frame now shows job B's form | Asked which job |
| Two tabs, two forms, one submit | Only the submitting tab |
| `submit/outcome` with an unknown attempt id, or from another frame | Refused by the console |
| Same attempt sent twice | One event |
| Already applied | No new event, no Undo |
| Undo, then a resend | Nothing recorded |

## Workday and multi-step forms

- **Grant.** **Enable for Workday** requests the optional hosts and registers the content scripts
  (`persistAcrossSessions: true`). Until then Workday gets the checklist only.
- **Sign-in is yours.** Nothing runs on sign-in, account and password pages (`ats_forms.py`
  marks them).
- **Steps.** The application's steps replace each other without navigation. `ats_forms.py` names
  the step by its heading. The session survives **your** Next within the same tenant and
  requisition. Each step gets its own snapshots; Fill works per step.
- **Attach** only on My Experience, once; parsed history that splits or merges jobs shows as
  `differs`.
- **Voluntary Disclosures and Self Identify** are never-store; every field there is `yours`.
- **Autosave.** Workday saves a draft of each step to the employer as you go, so a value Fill
  writes reaches Workday before you submit ([Privacy](#what-leaves-the-browser)).
- **Submit** is the Review step's Submit only. Next, Back and Save and Continue never start an
  attempt. The confirmation rule is recorded at fixture capture; ask mode until your submits
  confirm it.

## 4. Privacy

### What leaves the browser

| Data | Goes to | When |
|---|---|---|
| Nothing | anywhere | On page load: the overlay is drawn from form data the worker already cached |
| Frame URL (ATS posting URL) and field kind | console on 127.0.0.1 | On a button, Fill or **Fill this page** click |
| Field descriptors (label, name, id, type, option texts, visibility facts), then values of console-cleared fields | console, in memory for the session | On **Start** and after the attach, as 017 |
| ATS, variant, posting key, frame host, attempt time, which signals fired | console | On your submit and its outcome. No field, no value, no page text |
| Posting facts (1e capture) | console | Only on **Add this job** |
| A field's label, help text and options | the drafting runner | Only on **Draft**, as 017 |
| Resume (as 017 sends it, header included), posting, employer notes | the drafting runner, the scorer | On **Generate**, and on scoring as today |
| Every value Fill or Insert writes, and every attached file | **the site**: page scripts can read a field the moment it is written, and Workday autosaves steps to the employer, before and whether or not you submit | On **Fill**, **Insert** or **Attach**, your click |
| Contact block, `/prefs` answers, saved answers | the extension, one value per write, then the site as above | Only on **Fill** or **Insert**. **Never to any model** |
| Screenshots, page HTML, cookies, other tabs, history | nowhere | never |

The panel shows where each value comes from, not the value, so a screen share of the panel shows
no contact details.

### The contact block

- Stored only in `preferences.yaml` in your profile dir (gitignored), under `answers.contact`.
- Never sent to a model, never in a log line, `capture_log`, `fill_session.report`,
  `ext_applied` or `submit_observation`; the panel shows its key, not its value.
- In the browser a value exists only in the message that writes it, then in the form field.
- Tests and fixtures use plain canary values (`canaryfirst`, `canary.contact@invalid.test`), never
  a real-looking address; a test sets them only in the `/prefs` contact block and asserts they
  appear in no model request body, log record or table. The test's resume fixture must not contain
  them, or the resume (which goes to models as 017 sends it) would make the test fail for the
  wrong reason.

### Never-store and "yours" fields

- Decided only in Python, first, at every step: the fields step, classification row 1, every
  write request, **Draft**, every answer-bank save.
- The extension stores no value. The submit watch reads no values. Records and reports hold
  keys, labels and actions, never values.
- Salary, EEO and self-ID, consent and signature rows are listed as "This one is yours" and can
  never be ticked.

### Local API

Unchanged from 1e ([017 Local API security](017-assisted-apply.md#local-api-security)), plus the
[sender policy](#sender-policy). Content scripts never use the network (lint).

### In-page surfaces

The buttons, the **Fill** pill, the Submit outline, the ask prompt and the applied notice live in
the one closed-shadow overlay, answer only `isTrusted` clicks, and show only the job title and
employer the page already has. Their presence tells a site an extension is installed, as
Jobright's buttons did. **Show buttons** off removes the buttons and the **Fill** pill (Fill
still works from the icon); **Record submits** off removes the ask prompt and the applied notice;
the Submit outline belongs to an open fill session and shows whatever the switches say.

## 5. Permissions and manifest

| Key | Today (1e) | 022 | Why |
|---|---|---|---|
| `description` | "Send the job posting you are viewing ... Reads a page only when you click." | "Send job postings to your local jobhunter console and fill application forms on your click. Never submits." | The old text is no longer true on the ATS hosts |
| `permissions` | `activeTab`, `scripting`, `storage`, `contextMenus` | adds **`sidePanel`** | The panel |
| `host_permissions` | `http://127.0.0.1/*` | adds the seven required ATS hosts (from `ats_rules`) | Content scripts there, iframes included |
| `optional_host_permissions` | none | `https://*.myworkdayjobs.com/*`, `https://*.myworkdaysite.com/*` | Workday, on your click |
| `content_scripts` | none | one entry: the required ATS hosts, `all_frames: true`, `run_at: document_idle`, `match_about_blank: false`, isolated world | Buttons, Fill, submit watch |
| `side_panel` | none | `default_path: panel/index.html` | |
| Still forbidden | `<all_urls>`, other host wildcards, `tabs`, `webNavigation`, `cookies`, `webRequest`, `declarativeNetRequest`, `debugger`, `downloads`, `history`, `notifications`, `clipboardRead`, `externally_connectable`, `web_accessible_resources` | unchanged | The lint checks the exact lists |

**Fill this page** needs nothing new: `activeTab` plus `scripting`, as capture.

On reload Chrome shows the new host access on the extension's card; unpacked extensions are not
disabled for a permission increase.

### Store review

You load the extension unpacked; this spec keeps it that way. If it were ever published: host
permissions plus content scripts on third-party sites mean Chrome Web Store's in-depth review and
a justification per host; a privacy policy and "Limited Use" disclosures for form and personal
data; single purpose ("help you apply to jobs") fits; the manifest `key` is dropped from a store
package, so pairing would move to the store ID; and the reviewer would check that nothing submits
for the user, which the lint and e2e already prove. A listing would be its own decision and spec.

## Data

Migrations take the next free numbers when implemented.

```sql
CREATE TABLE fill_session (
  id             INTEGER PRIMARY KEY,
  application_id INTEGER NOT NULL REFERENCES application(id),
  packet_id      INTEGER REFERENCES application_packet(id),   -- NULL: no packet
  ats            TEXT NOT NULL,                               -- 'generic' on Fill this page
  posting_key    TEXT,                                        -- NULL on Fill this page
  start_url      TEXT,                                        -- normalize_apply_url of the page; Fill this page
  tab_id         INTEGER,                                     -- the Chrome tab, for generic matching
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

CREATE TABLE draft_run (
  id            INTEGER PRIMARY KEY,
  packet_id     INTEGER NOT NULL REFERENCES application_packet(id),
  kind          TEXT NOT NULL,
  question_key  TEXT NOT NULL DEFAULT '',
  runner        TEXT NOT NULL,
  started_at    TEXT NOT NULL,
  finished_at   TEXT,
  outcome       TEXT CHECK (outcome IN ('done', 'failed')),  -- NULL + no thread = stopped
  doc_id        INTEGER,
  error         TEXT
);

CREATE TABLE ext_applied (
  id                    INTEGER PRIMARY KEY,
  action_id             TEXT NOT NULL UNIQUE,  -- the attempt id
  application_id        INTEGER NOT NULL,      -- no REFERENCES: undo may delete the application
  event_id              INTEGER,               -- NULL when 'already recorded' (no Undo)
  job_group_id          INTEGER NOT NULL,
  ats                   TEXT NOT NULL,
  variant               TEXT,
  rule_version          TEXT,
  posting_key           TEXT,
  session_id            INTEGER,
  match_rule            INTEGER NOT NULL,      -- 1-5 from Matching, or 0 for your pick
  mode                  TEXT NOT NULL CHECK (mode IN ('auto', 'asked', 'mark')),
  created_application   INTEGER NOT NULL DEFAULT 0,
  set_resume_version    TEXT,                  -- what the record set, to restore only if unchanged
  prior_resume_version  TEXT,
  set_cover_letter      TEXT,
  prior_cover_letter    TEXT,
  attachment_ids        TEXT NOT NULL DEFAULT '[]',
  recorded_at           TEXT NOT NULL,
  undone_at             TEXT,
  undo_reason           TEXT CHECK (undo_reason IN ('not_applied', 'wrong_job'))
);

CREATE TABLE submit_observation (
  id           INTEGER PRIMARY KEY,
  ats          TEXT NOT NULL,
  variant      TEXT NOT NULL,
  rule_version TEXT NOT NULL,
  observed_at  TEXT NOT NULL,
  url_rule     INTEGER NOT NULL,
  element_rule INTEGER NOT NULL,
  request_2xx  INTEGER,
  errors_seen  INTEGER NOT NULL,
  answer       TEXT NOT NULL CHECK (answer IN ('yes', 'no'))
);

CREATE TABLE ats_record_mode (
  ats          TEXT NOT NULL,
  variant      TEXT NOT NULL,
  rule_version TEXT NOT NULL,
  mode         TEXT NOT NULL CHECK (mode IN ('ask', 'auto')),
  set_at       TEXT NOT NULL,
  reason       TEXT,
  PRIMARY KEY (ats, variant, rule_version)
);

CREATE TABLE ats_posting_ref (
  posting_key TEXT PRIMARY KEY,
  job_id      INTEGER NOT NULL REFERENCES job(id),  -- a job, so merges carry it, as job_board_ref
  set_by      TEXT NOT NULL CHECK (set_by IN ('pick', 'add')),
  seen_at     TEXT NOT NULL
);
```

`answers:` gains `contact:` (first, last, preferred name, email, phone, city, region, country;
each optional, length-limited), `requires_sponsorship: bool | None`, and an optional
`from_employer` on each custom answer. No `Profile` change.

Nightly merges: `fill_session` and `ext_applied` follow `application` in
`dedupe_url._merge_application`, as packets do (a test with an extension-recorded application on
the losing side; undo of it afterwards answers "stale").

## Routes

All `/ext/v1/` routes are POST with 1e's checks and answer within 20 seconds. Write routes take an
`action_id`.

| Route | Does |
|---|---|
| `forms` | Form data for all supported ATSs and the generic entry, versioned; cached by the worker |
| `match`, `match/pick` | Matching; your pick writes `ats_posting_ref` |
| `sessions`, `sessions/{sid}/fields`, `.../snapshot`, `.../write`, `.../finish` | 017's fill flow, bound to an application |
| `packets/{id}/estimate`, `generate` (202), `draft-status` | Drafting: resume, cover letter, question draft |
| `packets/{id}/ready` | Mark ready (existing rule) |
| `packets/{id}/entail-ack` | Your tick on a "partly" line |
| `packets/{id}/file {kind, format}` | The exported PDF (base64 plus SHA-256) or plain text, only for a version that passes the attach rule (else 409) |
| `submit/attempt`, `submit/outcome` | The attempt, then its outcome; auto mode records, otherwise the question |
| `applied` | Records on your **Yes**, **Mark applied** or a pick |
| `applied/{id}/undo` | Undo |
| `ats/mode` | **Record automatically** |

Console (same-origin): `POST /applied/{id}/undo`; the `/prefs` Contact and Sponsorship section
and **Copy from my resume header**; per-ATS automatic-recording lines on `/prefs` with
observation counts and **Back to ask**.

## Testing

All offline, under `tests/conftest.py`'s guard. **No test launches the real `claude`:** the CLI
runner is reached only through `app.state.packet_runner`; the e2e harness installs the fake
runner at module scope before the app starts; the existing autouse guard still makes the binary
lookup raise; and a fixture fails any test that ends with an `ext-draft-*` thread alive. The
Anthropic client is a fake; a draft without a confirm fails the test because the fake records the
call. The browser network is blocked by `--host-resolver-rules`, as 1e.

| Area | How |
|---|---|
| Fixtures | `tests/fixtures/ats_forms/<ats>/`: hand-written pages in each ATS's shape (Greenhouse classic, `job-boards`, embed on an employer page; Lever; Ashby with the Autofill box, and its embed; Workable; Workday steps; a generic employer form for **Fill this page**), each with confirmation, server-error and challenge states; a **honeypot** form (a hidden `email2`, an off-screen field, a zero-size field, an `aria-hidden` block, a `disabled` field); a **late filler** page whose script fills a field 1 s after load; a React page that re-renders the form. Captured public empty forms only with your approval, by hand in a browser, never fetched by tooling (Greenhouse `/embed/` is disallowed to crawlers); stripped as 017 says. Canary contact values only; the fixture lint applies |
| Form data | Selectors and synonyms find the fields on every fixture; a renamed label is found by synonym; step and other controls are not-submit |
| Matching | Each ATS's posting, form and embed URLs give one key; on Fill this page, `job.page_url` matches through `normalize_apply_url` and decides only when `identifies_posting` is true (a careers-home URL asks); a generic session binds only its own tab and starting URL (two NULL posting keys never match); one group decides; two ask; a session with another posting key asks; employer and title never match; a pick survives a merge |
| Answer bank | Contact and sponsorship round-trip; never-store label in `contact:` refused; Copy from header proposes only; sponsorship polarity vectors ("require", "without", negated, ambiguous: ticked, inverted, suggested); a promoted answer naming another employer is not ticked |
| Classification | 017's tests plus: row 0 on every honeypot field (never ticked, never written, even when its label is "Email"); contact rows ticked; the late filler's field is not overwritten; split phone `listed`; never-store beats contact synonyms |
| Drafting | `start` refuses with 409 before any runner call on a stale token, a held claim and over the cap; 202 then `done` with the fake; the claim lock is held by the thread until it ends; `stopped` after a simulated restart; a CLI failure ends in `failed` with no SDK call; overage needs `confirm_paid`; container refuses; `question_draft` needs a packet; the call-site registry includes the card; shutdown joins the thread |
| Attach and insert | File size and SHA-256 equal the export; only the clicked field; replace question on a filled file input; insert into a non-empty textarea refused; `maxlength` refused; `file` refuses a version with a `no` line, an unticked `partly` line or no entailment; an edited version with untouched lines keeps their verdicts, and its edited lines route to Review in jobhunter until ticked; an edit starts no entailment run (the fakes see no call) |
| Sender policy | The `MESSAGE_POLICY` test above; e2e: each page-only type from a content script refused, each content type from an unexpected frame refused, every type but `frame/hello`, `forms/get` and `frame/bye` refused from an injected generic frame, an ATS named in a payload ignored in favour of `sender.url`, `ask/answer` and `applied/undo` for another tab's record refused; console refuses `submit/outcome` without a matching attempt |
| Fill this page refusals | No injection on a `disabled_hosts` host, the console's own origin, a LinkedIn or Indeed host, or a Workday host before Enable for Workday (the popup offers Enable instead); NEOGOV fixture fills per page |
| Switches | Show buttons off: no button or pill, and with no session open and nothing to prompt no overlay host element at all; with a session open the Submit outline still shows; Record submits off: no submit listeners, no ask prompt, no in-page notice; both learned from `forms/get`; auto mode unchanged after toggling Record submits off and on |
| Submit watch | Every row of [No false positives](#no-false-positives-the-cases), end to end; the ask prompt appears in the overlay and the badge; ask records only on Yes; the auto offer only after 3 agreeing observations; a rule version change returns to ask; **Record submits** off adds no listeners |
| Record and undo | Through the routes: event at attempt time, status, created application, packet attach, `ext_applied`; already-applied gives no event and no Undo; undo restores only unchanged fields; refused after a later event by id; refused when stale after a merge; deletes a created application only with no references (one test per referencing table); a kept application with no events gets the `interested` event and a valid status; `wrong_job` clears the posting ref and keeps the mode; `not_applied` on an automatic record returns to ask; achievements revoked within 24 h, kept after |
| Workday | Steps fixture: session survives Next, per-step snapshots, attach on My Experience only, disclosures `yours`, Next never an attempt |
| Lint | Manifest lists exactly as above; no network APIs in `content/`; no `preventDefault`, `stopPropagation` or `stopImmediatePropagation` in `submit.js`; 017's submit-path list outside `act.js`; an `isTrusted` check in every overlay click handler; HTML sinks forbidden everywhere |
| e2e (`e2e` marker) | 1e's harness with the test manifest adding the fixture hosts: overlay appears with no console request carrying page data; panel or fallback window opens; Generate with the fake then Attach; Fill on each fixture with the submit counters at zero, then the positive control; **Fill this page** on the generic fixture through the popup message; re-injection after an extension reload and the orphan's overlay gone; the worker stopped mid-attempt and the attempt still resolves |

**Acceptance (live, with you, no spend unless you choose):** one real application per ATS: the
buttons sit on the right fields, Fill fills only what it should, the attach is accepted, the
outline is on the real Submit, and after you submit the question appears and your answer is
logged. Confirmation rules are adjusted from what those submits show.

## A1 spike findings

Ticket A1 (`9e2c202`), run 2026-10-12 offline in Playwright's Chromium 153, headless, on
hand-written fixtures. The runnable proof is `tests/e2e/test_spike_022_a1.py` with its throwaway
extension and pages in `tests/e2e/spike_022_a1/` (`uv run pytest -m e2e tests/e2e/test_spike_022_a1.py -s`
prints each measurement as a `SPIKE` line; it takes about 2 minutes because of the 5-second
activation waits). Fixtures encode our model of an ATS and a render-everything-again stand-in for
React (React is not vendored), so the live check still decides real ATS behaviour.

| Question | Result | Consequence |
|---|---|---|
| Content scripts on ATS hosts and in an embedded iframe | Run in the top frame and in a cross-origin `boards.greenhouse.io` iframe on an employer page, each with its own `sender.url` and `frameId` (host permissions granted by the manifest) | As designed |
| `frameId` across a frame navigation | Same `frameId`, new `documentId` | Key sessions on tab and `frameId`; never on the document |
| `responseStatus` in an ATS frame | A request made by the page is in the frame's timeline with its status (200, 422). A cross-origin one without `Timing-Allow-Origin` reads 0. A `fetch` the content script makes itself is **not in the page timeline at all** | The confirmation watch can use the page's own same-origin requests only; it must not rely on its own fetches |
| `sidePanel.open` from a relayed click | Works when called first in the worker's message handler. With one `await` before it: "may only be called in response to a user gesture". Also works when the click woke a stopped worker | The synchronous rule stays and is now a tested requirement; lint it |
| Window fallback | `chrome.windows.create({type: "popup"})` works with no permission | Kept as the fallback |
| Panel observable headless | Not as a Playwright page (`context.pages` never lists it). It is visible through `chrome.runtime.getContexts({contextTypes: ["SIDE_PANEL"]})` (the context outlives the tab) and through the panel page's own `runtime.sendMessage` to the worker | e2e asserts on the panel's hello message, not on a page; the fallback window is an ordinary page and can be driven |
| `action.openPopup()` without a gesture | Resolves with no error (Chrome 127 and later). Headless shows nothing to observe | Keep the call best-effort; the badge and overlay prompt carry the question; real behaviour is a live check |
| Overlay on a re-rendering form | One host under `body`, outside the framework's root. Five full re-renders: no duplicate, same placement. A banner that shifts the layout: the button follows. The page clearing `body` removes the host and the `MutationObserver` re-adds exactly one | As designed; the observer must re-add a removed host |
| Re-injection after a reload | The worker's `onInstalled` handler re-injects with `scripting.executeScript({allFrames: true, files})` into open tabs on granted hosts, top frame and iframe; the new script replaces the host (marker attribute) and the orphan's is gone, never two | As designed |
| Orphan removal | `port.onDisconnect` fires on a reload, but `chrome.runtime.id` is **still set inside the handler** and is undefined 50 ms later | **Design change:** defer the id check (about 250 ms), now in [After an extension reload](#where-the-buttons-appear) |
| Idle worker stop | Every port disconnects, `runtime.id` stays set, the overlay stays, no reconnect until the next need; the next click reconnects (second port) and the message goes through | As designed |
| `setAccessLevel` on `storage.local` | Default: a content script reads the token. After `TRUSTED_CONTEXTS`: content-script reads and writes fail ("Access to storage is not allowed from this context"); extension pages still read it | The worker sets it on startup, before any content script can ask; A2 tests it |
| Base64 file transfer, `DataTransfer` | 64 KB and 3 MB arrive byte-identical (SHA-256 computed by the page's own `change` handler); `input.files` set, the page sees `change`, no submit | Works; sizes beyond a few MB were not tried (message limit is much larger) |
| `safeDispatch` | A helper that refuses a form, any button without `type="button"`, `input` submit and image, `formaction`, `[type=submit]`, a label for one, a wrapper containing one, and any element whose text reads submit, apply or send, refused all 11 submit-like targets and allowed the 9 safe ones; refuses `submit` events and Enter keydown; nothing reached the page's submit or click listeners | The A2/A3 helper starts from this classification |
| `registerContentScripts` for an optional host | Registers without error even when the host is not granted, but nothing injects (`permissions.contains` is false). With the host granted (a test manifest stand-in), the registered script injects. `permissions.request` cannot be driven headless (no answer without a prompt) | A11 registers only after `permissions.request` resolves true; the grant itself is a live check |
| `isTrusted` and `userActivation` on submits | See below | **Design change** to the A9 signals |

**Submit signals, measured** (page and content script saw identical values):

| How the form was submitted | `click.isTrusted` | `submit.isTrusted` | `userActivation.isActive` at submit | `submitter` |
|---|---|---|---|---|
| Real click on Submit | true | **true** | true | the button |
| Real click on a button outside the form (`form=`) | true | true | true | that button |
| Enter in a text field | true (synthesized default-button click) | true | true | the default button |
| Page `button.click()` / content-script untrusted click | **false** | **true** | false | the button |
| `form.requestSubmit()` (page or content script) | no click | **true** | false | null |
| `form.requestSubmit(button)` | no click | true | false | the button |
| `form.submit()` | none | **no event** | none | none |
| `requestSubmit()` within 5 s of any real click elsewhere | none | true | **true** | null |

Consequences for A9:
- `submit.isTrusted` is **useless**: the browser dispatches a trusted `submit` event even for
  script-made submissions. The test is the `click` that caused it (`isTrusted` true) or, with no
  click, a null `submitter` plus no activation.
- `userActivation.isActive` alone is not enough: any real click in the last 5 seconds makes a
  scripted `requestSubmit()` look active. Require **trusted click on the submitter, or an
  Enter keydown, immediately before the submit event**, and a non-null `submitter`.
- `form.submit()` fires no event, so the submit listener never sees it. Such a submission is
  caught only by the navigation or confirmation watch; with neither, it is not recorded, which
  is the safe direction.
- A browser-initiated navigation and a runtime message from a gesture-carrying sender both
  activate the receiving frame for about 5 seconds (this is how the relayed click opens the
  panel), so a page that has just loaded counts as active.

**Harness notes for A2.** (1) Playwright's `page.evaluate` runs with a user gesture, and a
`page.goto` activates the frame for 5 seconds: a test that wants an inactive script submit must
defer it past 5 seconds with `setTimeout` inside the page, and a content script must delay its
own action, because the driver's message carries the gesture. (2) `chrome.runtime.reload()` leaves
the extension unloaded in this headless Chromium and a `--load-extension` one cannot be
uninstalled; load the extension through the browser CDP session (`Extensions.loadUnpacked`, with
`ignore_default_args=["--disable-extensions"]`) and reload with `Extensions.uninstall` then
`loadUnpacked` (a pinned `key` keeps the ID). (3) Serialise the test worker's `storage.session`
log writes; concurrent read-modify-writes lose entries. (4) A form `action` URL must be routed,
and a form that navigates its tab breaks later lookups; give the fixture form a hidden target
iframe.

## Failure modes

| Failure | Behaviour |
|---|---|
| Console down or extension unpaired | Overlay still drawn (data cached); a click says "Start jobhunter" or "Pair the extension"; a pending attempt is asked about if the console returns within 30 minutes, else forgotten |
| Form data stale after an ATS redesign | Fewer buttons, more `listed` fields, confirmation silent so submits are asked; a fixture refresh bug |
| Side panel cannot open from a page click | The extension window opens instead |
| Draft fails on the subscription | The API offer, its own click |
| Console restarts mid-draft | `draft-status` answers `stopped`; Generate again |
| No PDF export | Insert as text, or attach by hand |
| Site rejects the attach | 017's manual fallback |
| A page fakes a click on a jobhunter button, or calls Submit itself | Ignored, no attempt |
| A content script sends a spend or write message | Refused by the sender policy |
| A honeypot or hidden field matches a contact label | `listed`, never written |
| Tab closed between submit and confirmation | Attempt expires; the popup offers **Mark applied** for 30 minutes |
| Extension reloaded with ATS tabs open | Re-injected; the orphaned overlay removes itself |
| Wrong job recorded | **Undo** with "Wrong job", then pick the right one |
| Undo after you moved the application on | Refused; edit it on the pipeline page |
| **Fill this page** on a page that navigates | The grant ends; click the icon again |

## Deviations from earlier specs

| Spec | Today | Proposed | Why |
|---|---|---|---|
| [017](017-assisted-apply.md#phase-2-gate) | Phase 2 only after its gate | Gate waived | Your decision, 2026-10-11 |
| [017](017-assisted-apply.md#coexisting-with-jobright) | Coexist with Jobright | Removed | Your decision, 2026-10-11 |
| [017](017-assisted-apply.md#phase-2-data) | `submit_seen` is not an `applied` event | Confirmed submits record `applied`, with undo | Your request (d23b093) |
| [017](017-assisted-apply.md#architecture) | Content scripts do nothing on load | Overlay and passive listeners on load, no reads or sends | Buttons on the field are the feature |
| `console/tracking.py::add_event` | "Past events are never touched" | Undo deletes the one event the extension wrote, by id, within 7 days, only with no later event | A machine-recorded event you disown should go |
| [001](001-goals-and-scope.md#non-goals) | 017's clause: help with one form you chose, never submitting | Adds "and record that you applied when you submit" | Recording is tracking |

## Tickets

Filed in git-bug with labels `next`, `extension`, `area:apply`, `spec-022` and the difficulty and
agent labels below. **Each ticket updates the help topic for what it ships** (CLAUDE.md "User
help"); A12 is spec markings only. Worker and reviewer per the `CLAUDE.md` table.

| # | Ticket | Difficulty | Worker / reviewer | Depends on |
|---|---|---|---|---|
| A1 `9e2c202` | Offline spike: content scripts on ATS hosts and in an embedded iframe; `sidePanel.open` sync before `await` from a relayed click, else the window fallback; `action.openPopup` without a gesture; whether the panel is observable headless; `responseStatus` in a frame; overlay on a React fixture across re-renders; `frameId` stable across a frame navigation; re-injection after reload, the port-disconnect orphan signal, and an idle worker stop disconnecting every port with lazy reconnect; `registerContentScripts` for an optional host; `userActivation` and `SubmitEvent.submitter` on the fixtures; and 017's 2a items: `setAccessLevel` on `storage.local` hides the token from content scripts, the resume arrives byte-identical through base64 messaging, `safeDispatch` refuses every submit-like target on the fixtures | normal | sonnet / opus | |
| A2 `69849da` | Extension skeleton: manifest (description, sidePanel, ATS hosts, content-script entry, Workday optional hosts), `frame.js` announce, `forms` route and cache, `MESSAGE_POLICY` with its Python test, panel shell bound to the tab, popup **Fill this form**, Show buttons and Record submits switches delivered through `forms/get`, the **one body-level overlay** (host element, closed shadow root, placement and re-placement, `isTrusted` and `runtime.id` checks), re-injection and port-based orphan removal, lint, e2e harness with the fake runner at module scope; `browser-extension.md` | normal | sonnet / opus | A1 |
| A3 `467a606` | `ats_forms.py` (fields, synonyms, visibility facts, submit and other controls, variants, confirmation and error rules with versions, `parse_done`, `attach_ok`, generic entry), `page_match.py` with posting keys and `job.page_url`, `ats_posting_ref`, `match` routes, vectors | hard | opus / opus xhigh | |
| A4 `5ed37eb` | Answer bank: `contact`, `requires_sponsorship`, `from_employer` on promote, `/prefs` section, Copy from my resume header (a simple parser: the first lines, email and phone by pattern, proposed for you to check), sponsorship polarity vectors, never-sent tests; help | normal | sonnet / opus | |
| A5 `850a8ac` | Console fill: `fill_session` migration, Start note in `AUTO_NOTES`, sessions and snapshots in memory, `fill_classify` with row 0 and the 022 rows and tick defaults, value release, write refusal, caps, idle close, API contract tests | hard | opus / opus xhigh | A3, A4 |
| A7 `0aebb94` | Drafting in the background: `start` and `run_claimed`, claim handoff, `draft_run`, estimate, generate (202) and draft-status for resume, cover letter and question draft, runner lines from the effective settings with 020's line and fallback notice, call-site registry, `app.state` runner, thread join on shutdown, S12b registry, `inflight.Claim` object form, packet page shows a draft in progress; `drafting.md` | hard | opus / opus xhigh | |
| A6 `725855c` | Extension Fill: `locate.js`, stable keys, visibility re-check before each write, `act.js` and `safeDispatch`, Start, Fill, Insert, Draft (with Prepare when there is no packet), Use packet value, Submit outline, the **Fill** pill in the overlay, **Fill this page** through `activeTab` on frame 0 with the generic data and the refusal list; e2e with honeypot, late filler and submit counters; help | hard | opus / opus xhigh | A2, A5, A7 |
| A10 `22f4dde` | Recording backend: `tracking.mark_applied` shared with `answer_prompt`, `ext_applied` migration, `applied` and undo routes (extension and console) with every undo rule, the console's pipeline Undo, merge re-pointing; no extension UI (the panel's Mark applied is in A9); help | hard | opus / opus xhigh | A3, A5 |
| A8 `537b1a0` | Buttons and the assist card: the buttons in A2's overlay, panel or window, every card state, the attach rule with per-line sources and `partly` ticks (`entail-ack`), `ready` and `file` routes, replace question, Attach and Insert on the clicked field, Add this job in the frame, session report of attached versions, edited-version routing; e2e; help | hard | opus / opus xhigh | A2, A3, A5, A6, A7 |
| A9 `3641292` | Submit watch in ask mode: `submit.js` with the gesture, `userActivation` and `submitter` rules, pending attempts, confirmation and error watch across a frame navigation, `submit/attempt` and `submit/outcome` with the console's attempt check, the visible ask prompt (overlay, badge, popup), **Mark applied** in the panel and popup, `submit_observation`; every false-positive fixture; help (`browser-extension.md`: what counts as your submit, ask mode) | hard | opus / opus xhigh | A2, A3, A10 |
| A14 `2982a5c` | Auto mode: `ats_record_mode` keyed by ATS, variant and rule version, the offer after 3 agreeing observations, the applied notice with Undo, `not_applied` back to ask and `wrong_job` clearing the posting ref, `/prefs` lines; help | hard | opus / opus xhigh | A9, A10 |
| A11 `b7ebe3a` | Workday: Enable for Workday, registered scripts, step-aware sessions and snapshots, attach on My Experience, Review-only submit, Workday data and fixtures; help | hard | opus / opus xhigh | A6, A8, A9 |
| A12 `fbee458` | Spec markings: 017 sections marked superseded where 022 changes them, 009 roadmap line | mechanical | haiku / sonnet | |

**Order.** A1, A3, A4, A7 and A12 can start at once (four workers at a time). Then A2 (after
A1) and A5 (after A3, A4); then A6 and A10; then A8 and A9; then A14 and A11.

**Estimate**, by the calibrated rule (mechanical 0.75 h, normal 1.4 h, hard 2.1 h; schedule = sum
/ 3, plus 30%; range 0.7x to 1.6x; floor = the longest dependency chain):

- Thirteen tickets: 9 hard (18.9 h) + 3 normal (4.2 h) + 1 mechanical (0.75 h) = **23.85 h**.
- Schedule: 23.85 / 3 = 7.95 h, plus 30% = **about 10.3 h**; range 7.2 to 16.5 h.
- Floor: A3, A5, A6, A8, A11 = 5 x 2.1 = **10.5 h** (A3, A5, A10, A9, A14 is also 10.5 h). It is
  above the schedule, so it binds: **about 10.5 h**, range **10.5 to 16.5 h**.
- Tokens: 9 x 2.0M + 3 x 0.6M + 0.5M = **about 20.3M**.
- Not included: your live acceptance runs, fixture capture if you approve it, and PR merge waits.

## Open questions

None. Revision 1's four were answered on 2026-10-12: buttons on load; attach from the panel with
the stricter entailment rule; ask mode first, auto after 3 agreeing submits; contact block stored
locally on `/prefs`.

## History

### Revision 4 (2026-10-12)

The contact-header redaction (ticket A0, `e7c2bcd`) is dropped at your word: "Let's not worry
about it, I already use job services that have my name." The resume goes to models as 017 sends
it; the `/prefs` contact block stays local and is used only by Fill. Removed with it: the header
parser, placeholders and their render and factcheck rules, the boundary check, the What models
see preview, the header-only re-score text and the redaction canaries; A4 and A7 no longer wait
for A0. Also from the re-review: Show buttons off removes the buttons only, and the overlay host
exists only while something needs it (the Submit outline included); an idle worker stop
disconnects every port, so scripts reconnect lazily (an A1 item); `fill_session` gains
`start_url` and `tab_id`, and `match()` takes the tab id; draft threads always register in
S12b's work registry, now on `main`.

### Revision 3 (2026-10-12)

From the re-review of revision 2. Contact header: redaction now also runs over the assembled
request at the runner and scorer boundary and fails closed, because resume versions, entailment
context and your own notes carried the values; line maps pinned (real `Context.lines`, redacted
`Context.model_lines`, checks accept both, render replaces placeholders for every kind); the
whole header block is replaced line by line and only exact values are matched elsewhere, because
name and phone patterns misfired; rubric untouched; a preview and stated limits. Fill this page
refuses `disabled_hosts`, the console's origin, LinkedIn and Indeed (017's rule kept) and Workday
before it is enabled; matching through `normalize_apply_url` and `identifies_posting`; generic
sessions bound to their tab and URL; generic frames limited to three message types; records and
attempts scoped to their tab. Switches: Record submits owns the prompt and notice, both come via
`forms/get`, auto mode survives toggling. Edited versions route to review instead of running
entailment. `inflight.Claim` object form; the orphan mechanism stated. Graph: overlay in A2, the
Fill pill in A6, Mark applied in A9, A7 and A4 after A0; A1 regains 017's 2a items.

### Revision 2 (2026-10-12)

Your answers and three new requests (Fill on any site from the icon, contact header kept out of
model requests, a separate Record submits switch). From the review: an attempt needs a trusted
gesture on the Submit control plus `userActivation` and `submitter`, because a trusted `submit`
event alone does not prove a user submitted; a sender policy per message type in the worker, and
the console's attempt check; hidden and disabled fields are never filled (row 0, a honeypot
fixture); ticket dependencies fixed (recording backend before the submit watch, auto mode its own
ticket, A6 after A7 with question drafts, A8 after A5 and A6, help per ticket); tests can never
launch the real `claude`; undo defined against every table that refers to an application and the
zero-event case. Also folded in: posting key required from the session, wrong-job undo, match
details stored; auto mode per variant and rule version; a visible ask prompt; achievements and
undo; undo staleness, unchanged-only restore, attempt time, no Undo for already-recorded rows; the
confirmation-email claim corrected (`_worth_event` skips them); promoted answers and sponsorship
polarity; the site as a destination and source keys in the panel; the overwrite exceptions and
the replace question; re-injection and orphan cleanup; one body-level overlay; draft state,
claim handoff and two more card states; 020's effective runner, price line, fallback notice and
call-site registry; fixture rules (browser-only capture, late filler, canaries, digits-only
phones); more spike items; the Start note in `AUTO_NOTES`; the manifest description.

### Revision 1 (2026-10-11)

Written from d23b093's two comments: the three requested features, then the scope change when
you turned Jobright off.
