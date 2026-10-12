# 022 — Extension apply assist: fill on click, resume and letter buttons, applied on submit

Status: **draft for review, revision 2** (bug `d23b093`). Revision 1 was written 2026-10-11
against `main` at `8836891`. Revision 2 (2026-10-12, rebased on `1f4c08a`) takes your answers to
revision 1's four questions, three new requests, and the review's findings ([History](#history)).
017 phase 1 (1a to 1e) is merged; nothing of 017 phase 2 is built. No code yet.

You asked (2026-10-11) for three things in jobhunter's extension: a way to generate a tailored
resume or cover letter for the job you are viewing; small buttons for that on the resume and
cover-letter fields of the application form, as Jobright's **Customize** button did; and, when
you submit the form, jobhunter recording that you applied. Later the same day you said you are
turning Jobright off, so jobhunter's extension becomes your only apply helper and must also do
what Jobright did: **fill the form from your saved answers when you click**. On 2026-10-12 you
added: Fill on **any** site from the toolbar icon; your resume's **contact header kept out of
everything sent to a model**; and a **Record submits** switch apart from **Show buttons**.

This spec is built on [017](017-assisted-apply.md). It reuses 017 phase 2's design (content
scripts on the ATS hosts, the side panel, the two-step snapshot, `act.js` and `safeDispatch`, the
never-store list, the `/ext/v1/` security) and changes it where your decisions require:

| You get | How it works | Never |
|---|---|---|
| **Fill** | On a Greenhouse, Lever, Ashby or Workable form (Workday when you enable it), click the jobhunter icon or the small **Fill** pill on the form. On **any other site**, the toolbar icon offers **Fill this page** for the page you are on. The side panel lists every visible field: what it will fill and from which saved answer, what the site already filled, and what is yours. **Fill** writes the ticked rows | Fill on page load, touch salary, EEO, consent or signature fields, write a hidden or disabled field, overwrite a field that has a value (except the named actions in [Overwrites](#overwrites)), click Submit |
| **Resume and letter buttons** | On the four ATSs (and Workday), a small jobhunter button sits beside the resume upload, the cover-letter upload and the cover-letter text box. Click it: the side panel shows the packet for this job, which runner drafts it and what that costs, and waits for **Generate**. When the draft passes the checks, the panel shows each changed line with its cited source; **Attach** puts the PDF in that upload, or **Insert** puts the letter text in that box | Spend without your confirm, attach a draft with an unsupported line or an unticked "partly" line |
| **Applied on submit** | On the four ATSs (and Workday), when **you** press Submit and the site shows its confirmation, the extension tells jobhunter, which records `applied` for the matched job. Each ATS asks you first; after 3 submits where your answer matched, it offers to record on its own. A notice offers **Undo** | Record a submit the site rejected, a submit a page script made, guess the job, or click Submit itself |
| **Contact header kept local** | Your name, email and phone are replaced by placeholders in every request to any model (scoring and drafting) and put back on your machine when a document is rendered or exported | Send them to a model |

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
| Switches | Per ATS in the options page: **Show buttons** (on) and **Record submits** (on), independent. Off means no buttons, or no submit listeners at all | Your request (2026-10-12) |
| Where spending is confirmed | **Only in extension pages** (the side panel or the extension's own popup window). The worker accepts spend and write messages only from extension pages ([Sender policy](#sender-policy)) | A page can script clicks on anything in its own DOM; it cannot reach an extension page |
| Which runner drafts | The **effective** drafting runner (`models.toml` over `config.toml`, as 020): `cli` by default (`claude -p` on your subscription, no API cost). The panel names the runner and model before **Generate**; an API run shows its amount and needs its own click | [017 CLI runner](017-assisted-apply.md#cli-runner), [020](020-model-selection.md) |
| Attaching a generated version from the panel | Allowed when the checker report is clean **and** the entailment pass ran and found no unsupported line; every "partly" line needs your tick in the panel, which shows each changed line beside its cited source. Anything else opens the packet page | Your answer (2026-10-12), with the stricter rule |
| What counts as "your submit" | A trusted pointer click on the Submit control, or a trusted Enter in a field of that form, within about 1 second, with `navigator.userActivation.isActive`, and (for a form) the `SubmitEvent.submitter` being that control ([The rule](#starting-an-attempt)) | A trusted `submit` event alone does not prove it: `button.click()` and `requestSubmit()` from page script also fire one |
| What counts as "applied" | Your submit, then the site's confirmation, on the same tab and frame, within 90 seconds, for a job matched with the session's posting key | No false positives |
| How auto-recording is switched on | Each ATS starts in **ask** mode. After 3 asked submits where your answer agreed with the confirmation rule and none disagreed, the panel offers **Record <ATS> submits automatically**; your click turns it on, for that ATS, form variant and rule version | Your answer (2026-10-12) |
| Undo | One click, for 7 days, from the page notice, the popup or the pipeline page ([Undo](#undo)) | A wrong `applied` must be cheap to take back |
| Event source | `application_event.source = 'manual'` with a fixed note, plus an `ext_applied` row that holds the undo data. No change to the `source` CHECK | Changing a CHECK means rebuilding `application_event`; 017 said "no new `application_event.source`" |
| Contact header and models | **Replaced by placeholders** in every model request (screen, deep pass, Jev and Decisions scorers, drafting, entailment, question drafts, `llm bench`); put back locally when rendering and exporting. `scoring_version` keeps hashing the full resume, so nothing is re-scored | Your request (2026-10-12). Reverses 017's "resume sent as Stage 2 sends it" |
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
| The resume goes to models "as Stage 2 already sends it", header included | The contact header is replaced by placeholders ([Contact header](#contact-header-kept-local)) |

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
| **Console** | `apply/ats_forms.py`, `apply/page_match.py`, the fill routes, the draft routes that return 202, `/ext/v1/applied`, undo, `documents.model_lines` | Trust anything the page says beyond field descriptors and URLs |

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
| **Content script** | `sender.id` is this extension, `sender.tab` is set, `sender.url`'s host is a granted ATS host (or a tab and frame the worker injected into for **Fill this page**, frame 0 only), and `sender.frameId` is the frame that announced itself for that `documentId` | `frame/hello`, `forms/get`, `panel/open` (open the panel at a field; no write), `submit/attempt`, `submit/outcome`, `ask/answer` and `applied/undo` (both undoable, no spend), `frame/bye` |
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
jobhunter**. The click grants `activeTab` for the tab, as capture does. The worker injects
`frame.js`, `locate.js` and `act.js` (not `submit.js`) into **frame 0** only, with the console's
**generic** form data (label synonyms for contact, links and the yes/no answers; no selectors),
and opens the side panel. Then steps 3 to 8 as above, with these limits:

| | Fill this page |
|---|---|
| Frames | The top frame only. A form in a cross-origin iframe is not reachable; the panel says so ("this form is inside a frame from <host>; jobhunter can fill it only on the supported ATSs") |
| Matching | Best effort: the page URL and `job.page_url`, `job.url`, `job.apply_url` (rule 3 below), else you pick or **Add this job** (1e capture). Never decided by employer and title |
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

**One overlay.** The content script adds one host element as the last child of `body`, with a
closed shadow root, `position: absolute` over the page, `pointer-events: none` except on its
buttons. Each button is a `<button type="button">` with an inline SVG icon (no image files, so
no `web_accessible_resources`), placed beside its field from `getBoundingClientRect()` and
re-placed on scroll, resize and form re-render (a `ResizeObserver` and a `MutationObserver`).
Nothing is inserted inside the ATS's own widgets, so a React re-render cannot remove or duplicate
it (A1 checks this on a React fixture). The buttons answer only `isTrusted` clicks and follow
`prefers-color-scheme`.

**After an extension reload** the worker re-injects the content scripts into open tabs on the
granted ATS hosts (`chrome.tabs.query` with those URL patterns, allowed by the host permissions,
then `scripting.executeScript` with `allFrames: true`). An orphaned script, whose
`chrome.runtime.id` is gone, removes its overlay and its listeners at once.

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
- **Claim handoff.** `start` takes the `inflight` lock in the request thread and passes the held
  lock object to the draft thread, which releases it in its `finally`, as `deep_routes` does with
  the run lock. The request thread never releases it after a successful start.
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
  shutdown joins live `ext-draft-*` threads (up to the CLI timeout) before closing. If 019 S12b's
  work registry (`app.state.work`) is on `main` when this lands, each draft thread registers there
  before it starts and leaves in its `finally`, so a Restart answers 409 meanwhile.

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

`apply/page_match.py::match(conn, frame_url, session_id=None)`:

| # | Evidence | Strength |
|---|---|---|
| 1 | The fill session or assist card already bound this tab and frame to an application, **and the frame's posting key equals the session's** | Decides |
| 2 | An `ats_posting_ref` row for the posting key (your earlier pick or add) | Decides |
| 3 | The posting key (or, on **Fill this page**, the normalized page URL) equals the key of a group's `apply_link.final_url` or `start_url`, a `job.url`, `job.apply_url` or `job.page_url`, or a `job_board_ref.apply_url` | Decides when exactly one group matches |
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
  applied, <title> at <employer> · Undo · ×", trusted clicks only.
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
| Resume (contact header replaced), posting, employer notes | the drafting runner, the scorer | On **Generate**, and on scoring as today |
| Every value Fill or Insert writes, and every attached file | **the site**: page scripts can read a field the moment it is written, and Workday autosaves steps to the employer, before and whether or not you submit | On **Fill**, **Insert** or **Attach**, your click |
| Contact block, `/prefs` answers, saved answers | the extension, one value per write, then the site as above | Only on **Fill** or **Insert**. **Never to any model** |
| Screenshots, page HTML, cookies, other tabs, history | nowhere | never |

The panel shows where each value comes from, not the value, so a screen share of the panel shows
no contact details.

### Contact header kept local

`documents.model_lines(resume_text)` returns the numbered resume (`L1`...`Ln`, same keys as
`numbered_resume`) with contact details replaced:

| Replaced | How found | Placeholder |
|---|---|---|
| Your name | The header's name-shaped first line (`export.person_name`), and each later occurrence of that exact name | `[NAME]` |
| Email addresses | A standard address pattern, anywhere in the resume | `[EMAIL]` |
| Phone numbers | Patterns with separators, `+` country codes and **digits-only runs of 10 to 15 digits**, anywhere in the resume | `[PHONE]` |

Every model-bound request builds the resume from `model_lines`: Stage 2 screen (all scorers,
batch and sync), the deep pass, the Jev and Decisions scorers (`scoring/decisions.py` today sends
`profile.resume_text`), packet drafting and regenerate, cover letters, question drafts, the
entailment pass and `llm bench`. A test seeds a resume with canary values (`Canaryname
Testperson`, `canary.contact.7f3a@invalid.test`, `5550100199`, `555-0100-199`) and asserts that
no request body from any scorer or runner fake contains them; a new model call site that skips
`model_lines` fails it.

Local rendering puts them back: rendering and export map the model's header citations to the
**real** lines (they already render the header from `lines_ctx`; that map is now the unredacted
one), and replace `[NAME]`, `[EMAIL]` and `[PHONE]` in letters and drafts with your local values.
The factcheck reads the same redacted lines the model read, so citations still check.
`Profile.scoring_version` keeps hashing the full resume, so this change re-scores nothing; prompt
caches miss once.

### The contact block

- Stored only in `preferences.yaml` in your profile dir (gitignored), under `answers.contact`.
- Never sent to a model, never in a log line, `capture_log`, `fill_session.report`,
  `ext_applied` or `submit_observation`; the panel shows its key, not its value.
- In the browser a value exists only in the message that writes it, then in the form field.
- Tests and fixtures use the canary values above, never a real-looking address.

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
Jobright's buttons did. **Show buttons** off removes them; Fill still works from the icon.

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
| Matching | Each ATS's posting, form and embed URLs give one key; `job.page_url` matches on Fill this page; one group decides; two ask; a session with another posting key asks; employer and title never match; a pick survives a merge |
| Answer bank | Contact and sponsorship round-trip; never-store label in `contact:` refused; Copy from header proposes only; sponsorship polarity vectors ("require", "without", negated, ambiguous: ticked, inverted, suggested); a promoted answer naming another employer is not ticked |
| Contact header | `model_lines` vectors (name lines, emails, separated and digits-only phones, a phone-like date range left alone); the canary test over every scorer and runner fake; export and the packet page show the real header; `scoring_version` unchanged |
| Classification | 017's tests plus: row 0 on every honeypot field (never ticked, never written, even when its label is "Email"); contact rows ticked; the late filler's field is not overwritten; split phone `listed`; never-store beats contact synonyms |
| Drafting | `start` refuses with 409 before any runner call on a stale token, a held claim and over the cap; 202 then `done` with the fake; the claim lock is held by the thread until it ends; `stopped` after a simulated restart; a CLI failure ends in `failed` with no SDK call; overage needs `confirm_paid`; container refuses; `question_draft` needs a packet; the call-site registry includes the card; shutdown joins the thread |
| Attach and insert | File size and SHA-256 equal the export; only the clicked field; replace question on a filled file input; insert into a non-empty textarea refused; `maxlength` refused; `file` refuses a version with a `no` line, an unticked `partly` line or no entailment |
| Sender policy | The `MESSAGE_POLICY` test above; e2e: each page-only type from a content script refused, each content type from an unexpected frame refused; console refuses `submit/outcome` without a matching attempt |
| Submit watch | Every row of [No false positives](#no-false-positives-the-cases), end to end; the ask prompt appears in the overlay and the badge; ask records only on Yes; the auto offer only after 3 agreeing observations; a rule version change returns to ask; **Record submits** off adds no listeners |
| Record and undo | Through the routes: event at attempt time, status, created application, packet attach, `ext_applied`; already-applied gives no event and no Undo; undo restores only unchanged fields; refused after a later event by id; refused when stale after a merge; deletes a created application only with no references (one test per referencing table); a kept application with no events gets the `interested` event and a valid status; `wrong_job` clears the posting ref and keeps the mode; `not_applied` on an automatic record returns to ask; achievements revoked within 24 h, kept after |
| Workday | Steps fixture: session survives Next, per-step snapshots, attach on My Experience only, disclosures `yours`, Next never an attempt |
| Lint | Manifest lists exactly as above; no network APIs in `content/`; no `preventDefault`, `stopPropagation` or `stopImmediatePropagation` in `submit.js`; 017's submit-path list outside `act.js`; an `isTrusted` check in every overlay click handler; HTML sinks forbidden everywhere |
| e2e (`e2e` marker) | 1e's harness with the test manifest adding the fixture hosts: overlay appears with no console request carrying page data; panel or fallback window opens; Generate with the fake then Attach; Fill on each fixture with the submit counters at zero, then the positive control; **Fill this page** on the generic fixture through the popup message; re-injection after an extension reload and the orphan's overlay gone; the worker stopped mid-attempt and the attempt still resolves |

**Acceptance (live, with you, no spend unless you choose):** one real application per ATS: the
buttons sit on the right fields, Fill fills only what it should, the attach is accepted, the
outline is on the real Submit, and after you submit the question appears and your answer is
logged. Confirmation rules are adjusted from what those submits show.

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
| [017 decisions](017-assisted-apply.md#decisions-recorded-on-7fa29db), [008](008-compliance.md#personal-data) | Resume sent to models with its header | Contact header replaced by placeholders everywhere | Your request, 2026-10-12 |
| `console/tracking.py::add_event` | "Past events are never touched" | Undo deletes the one event the extension wrote, by id, within 7 days, only with no later event | A machine-recorded event you disown should go |
| [001](001-goals-and-scope.md#non-goals) | 017's clause: help with one form you chose, never submitting | Adds "and record that you applied when you submit" | Recording is tracking |

## Tickets

Filed in git-bug with labels `next`, `extension`, `area:apply`, `spec-022` and the difficulty and
agent labels below. **Each ticket updates the help topic for what it ships** (CLAUDE.md "User
help"); A12 is spec markings only. Worker and reviewer per the `CLAUDE.md` table.

| # | Ticket | Difficulty | Worker / reviewer | Depends on |
|---|---|---|---|---|
| A0 `e7c2bcd` | Contact header kept local: `documents.model_lines`, every model call site through it (screen, deep, Jev, Decisions, drafting, entailment, question drafts, `llm bench`), local re-add on render and export, placeholder substitution in letters, canary test over all fakes, `scoring_version` unchanged; help (`profile-and-resume.md`) | hard | opus / opus xhigh | |
| A1 `9e2c202` | Offline spike: content scripts on ATS hosts and in an embedded iframe; `sidePanel.open` sync before `await` from a relayed click, else the window fallback; `action.openPopup` without a gesture; whether the panel is observable headless; `responseStatus` in a frame; overlay on a React fixture across re-renders; `frameId` stable across a frame navigation; re-injection after reload; `registerContentScripts` for an optional host; `userActivation` and `SubmitEvent.submitter` on the fixtures | normal | sonnet / opus | |
| A2 `69849da` | Extension skeleton: manifest (description, sidePanel, ATS hosts, content-script entry, Workday optional hosts), `frame.js` announce, `forms` route and cache, `MESSAGE_POLICY` with its Python test, panel shell bound to the tab, popup **Fill this form**, Show buttons and Record submits switches, re-injection and orphan cleanup, lint, e2e harness with the fake runner at module scope; `browser-extension.md` | normal | sonnet / opus | A1 |
| A3 `467a606` | `ats_forms.py` (fields, synonyms, visibility facts, submit and other controls, variants, confirmation and error rules with versions, `parse_done`, `attach_ok`, generic entry), `page_match.py` with posting keys and `job.page_url`, `ats_posting_ref`, `match` routes, vectors | hard | opus / opus xhigh | |
| A4 `5ed37eb` | Answer bank: `contact`, `requires_sponsorship`, `from_employer` on promote, `/prefs` section, Copy from my resume header, sponsorship polarity vectors, never-sent tests; help | normal | sonnet / opus | |
| A5 `850a8ac` | Console fill: `fill_session` migration, Start note in `AUTO_NOTES`, sessions and snapshots in memory, `fill_classify` with row 0 and the 022 rows and tick defaults, value release, write refusal, caps, idle close, API contract tests | hard | opus / opus xhigh | A3, A4 |
| A7 `0aebb94` | Drafting in the background: `start` and `run_claimed`, claim handoff, `draft_run`, estimate, generate (202) and draft-status for resume, cover letter and question draft, runner lines from the effective settings with 020's line and fallback notice, call-site registry, `app.state` runner, thread join on shutdown, S12b registry if present, packet page shows a draft in progress; `drafting.md` | hard | opus / opus xhigh | |
| A6 `725855c` | Extension Fill: `locate.js`, stable keys, visibility re-check before each write, `act.js` and `safeDispatch`, Start, Fill, Insert, Draft (with Prepare when there is no packet), Use packet value, Submit outline, **Fill this page** through `activeTab` on frame 0 with the generic data; e2e with honeypot, late filler and submit counters | hard | opus / opus xhigh | A2, A5, A7 |
| A10 `22f4dde` | Recording backend: `tracking.mark_applied` shared with `answer_prompt`, `ext_applied` migration, `applied` and undo routes (extension and console) with every undo rule, **Mark applied** in the panel, pipeline Undo, merge re-pointing; help | hard | opus / opus xhigh | A3, A5 |
| A8 `537b1a0` | Buttons and the assist card: overlay, panel or window, every card state, the attach rule with per-line sources and `partly` ticks (`entail-ack`), `ready` and `file` routes, replace question, Attach and Insert on the clicked field, Add this job in the frame, session report of attached versions; e2e | hard | opus / opus xhigh | A2, A3, A5, A6, A7 |
| A9 `3641292` | Submit watch in ask mode: `submit.js` with the gesture, `userActivation` and `submitter` rules, pending attempts, confirmation and error watch across a frame navigation, `submit/attempt` and `submit/outcome` with the console's attempt check, the visible ask prompt (overlay, badge, popup), `submit_observation`; every false-positive fixture | hard | opus / opus xhigh | A2, A3, A10 |
| A14 `2982a5c` | Auto mode: `ats_record_mode` keyed by ATS, variant and rule version, the offer after 3 agreeing observations, the applied notice with Undo, `not_applied` back to ask and `wrong_job` clearing the posting ref, `/prefs` lines; help | hard | opus / opus xhigh | A9, A10 |
| A11 `b7ebe3a` | Workday: Enable for Workday, registered scripts, step-aware sessions and snapshots, attach on My Experience, Review-only submit, Workday data and fixtures; help | hard | opus / opus xhigh | A6, A8, A9 |
| A12 `fbee458` | Spec markings: 017 sections marked superseded where 022 changes them, 009 roadmap line, 008 personal-data line for the contact header | mechanical | haiku / sonnet | |

**Order.** A0, A1, A3, A4, A7 and A12 can start at once (four workers at a time). Then A2 (after
A1) and A5 (after A3, A4); then A6 and A10; then A8 and A9; then A14 and A11.

**Estimate**, by the calibrated rule (mechanical 0.75 h, normal 1.4 h, hard 2.1 h; schedule = sum
/ 3, plus 30%; range 0.7x to 1.6x; floor = the longest dependency chain):

- Fourteen tickets: 10 hard (21 h) + 3 normal (4.2 h) + 1 mechanical (0.75 h) = **25.95 h**.
- Schedule: 25.95 / 3 = 8.65 h, plus 30% = **about 11.2 h**; range 7.9 to 18.0 h.
- Floor: A3, A5, A6, A8, A11 = 5 x 2.1 = **10.5 h** (A3, A5, A10, A9, A14 is 10.5 h too). It is
  under the schedule, so **about 11.2 h**, range **10.5 to 18.0 h**.
- Tokens: 10 x 2.0M + 3 x 0.6M + 0.5M = **about 22.3M**.
- Not included: your live acceptance runs, fixture capture if you approve it, and PR merge waits.

## Open questions

None. Revision 1's four were answered on 2026-10-12: buttons on load; attach from the panel with
the stricter entailment rule; ask mode first, auto after 3 agreeing submits; contact block stored
locally on `/prefs`.

## History

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
