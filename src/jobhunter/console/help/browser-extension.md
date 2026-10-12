# Browser extension

"Send to jobhunter" captures the job posting you are viewing into your local console, where you
confirm before it is scored. It reads a page only when you click (activeTab), never on page load,
and it never fills or submits forms.

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
