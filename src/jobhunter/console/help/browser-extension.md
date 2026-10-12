# Browser extension

"Send to jobhunter" captures the job posting you are viewing into your local console, where you
confirm before it is scored. A posting is read only when you click (activeTab). On the application
pages of Greenhouse, Lever, Ashby and Workable the extension also runs when the page loads, to
draw its buttons (below); it writes nothing into the form, reads none of your answers, and sends
nothing about the page. It never submits a form.

1. Chrome 120 or newer. Open `chrome://extensions`, turn on **Developer mode**, click **Load
   unpacked**, and choose the `extension/` folder of this checkout. There is no build step.
2. The manifest pins the extension ID, so it stays the same across reloads. After `git pull`,
   press the reload icon on its card.
3. Start the console (`jobhunter console`). On `/prefs`, open **Browser extension** and click
   **Pair browser extension**, or run `jobhunter ext pair`. The code works once, for 10 minutes.
4. Open the extension's options page (right-click its toolbar icon, then **Options**). Enter the
   console port (default 8808, or the port you run the console on) and the code.
5. Check or revoke the pairing from the terminal:

```bash
jobhunter ext status    # whether it is paired; never prints the token
jobhunter ext unpair    # revoke the token on the console
```

To send a page, use the toolbar icon, the right-click menu **Send to jobhunter**, or
Alt+Shift+J (change the shortcut at `chrome://extensions/shortcuts`). On LinkedIn and Indeed
pages, a one-time notice comes first, and nothing is read until you accept it.

## Buttons on application forms

On Greenhouse, Lever, Ashby and Workable application pages (the pages on `boards.greenhouse.io`,
`job-boards.greenhouse.io`, `job-boards.eu.greenhouse.io`, `jobs.lever.co`, `jobs.eu.lever.co`,
`jobs.ashbyhq.com` and `apply.workable.com`, including a form embedded on an employer's page)
the extension draws a small **jobhunter resume** or **jobhunter letter** button beside the resume
and cover letter fields. The buttons live in one element at the end of the page, inside a closed
shadow root, so the site's own scripts cannot reach or duplicate them. Clicking one opens the
jobhunter side panel for that tab (or a small window when Chrome will not open the panel). The
panel is only a shell for now: it names the tab, the site and the field you chose. Drafting and
attaching come in later steps.

The options page has two switches for each site, independent of each other:

- **Show buttons**: draws the buttons. Off removes them, and the page then carries no element of
  the extension at all.
- **Record submits**: whether the extension may watch for your own submit so that jobhunter can
  ask whether you applied. Nothing watches yet; the switch is stored for that later step.

Both start on. They apply to open tabs straight away.

The extension asks the console for the field list of each site (a request that carries nothing
from the page) and keeps the answer, so the buttons still appear when the console is not running.
Pairing is still needed for the first answer.

After you reload the extension at `chrome://extensions`, a form that was already open in its own
tab gets its buttons back without a page reload. A form embedded on an employer's page needs that
page reloaded once. The old buttons remove themselves.

The pairing token is readable only by the extension's own pages and its background worker; the
scripts that run on web pages cannot read it, and the worker refuses every spend or write request
from them. Only the extension's own pages (the popup, options and the side panel) can ask for
those.

Containers: if the console runs in a container that publishes a different host port, set
`public_port` under `[console]` to that host port, the one the browser connects to. The extension
API accepts only a loopback Host on that port; unset, it uses `console.port`. For general container
setup, see [Containers](containers.md).

## Which job a form belongs to

Fill and recording applied (spec 022, being built) need to know which saved job an application
form is for. The console decides only from strong evidence, in this order:

1. The fill session you started in this tab: on Greenhouse, Lever, Ashby, Workable or Workday,
   the form must be for the same posting; on **Fill this page** elsewhere, it must be the same
   tab and the same page address you started on.
2. Your earlier pick for this posting.
3. Exactly one saved job links to this posting (its posting, apply or page address, or the link
   jobhunter resolved). On other sites the page address must name one posting by itself: it
   needs a job number or id, and an address whose job is in a `#/` route or inside a redirect
   link never decides, so you pick instead.
4. Exactly one **Open application** click from jobhunter in the last 24 hours led here.

Otherwise it asks you to pick the job, or to add it. Employer and job title never decide a
match. If you pick, jobhunter remembers that posting for next time.
