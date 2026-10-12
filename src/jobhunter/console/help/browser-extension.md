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
