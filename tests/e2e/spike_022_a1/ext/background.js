// Spike worker. State lives in storage.session so a stopped worker loses nothing.
let chain = Promise.resolve(); // serialise: concurrent read-modify-writes lose entries
const log = (key, value) => {
  chain = chain.then(async () => {
    const cur = (await chrome.storage.session.get(key))[key] || [];
    cur.push(value);
    await chrome.storage.session.set({[key]: cur});
  });
  return chain;
};
chrome.storage.local.set({token: "SECRET-TOKEN"});
const ports = new Set();
chrome.runtime.onConnect.addListener((port) => {
  ports.add(port);
  port.onDisconnect.addListener(() => ports.delete(port));
  log("connects", {frameId: port.sender.frameId, url: port.sender.url});
});
chrome.runtime.onInstalled.addListener(async (d) => {
  const prior = (await chrome.storage.local.get("installed")).installed || [];
  prior.push(d.reason);
  await chrome.storage.local.set({installed: prior});
  if ((await chrome.storage.local.get("noreinject")).noreinject) return; // test switch
  // Re-inject into open tabs on the granted hosts after a reload.
  const tabs = await chrome.tabs.query({
    url: [
      "https://jobs.example-employer.test/*",
      "https://boards.greenhouse.io/*",
      "https://job-boards.greenhouse.io/*",
    ],
  });
  for (const t of tabs) {
    try {
      await chrome.scripting.executeScript({target: {tabId: t.id, allFrames: true}, files: ["content.js"]});
    } catch (e) {
      await log("reinject-errors", String(e));
    }
  }
});
chrome.runtime.onMessage.addListener((msg, sender, respond) => {
  switch (msg.type) {
    case "frame/hello":
      log("hellos", {frameId: sender.frameId, url: sender.url, documentId: sender.documentId, tab: sender.tab && sender.tab.id});
      respond({frameId: sender.frameId});
      return false;
    case "panel/hello":
      log("panel", msg.href);
      return false;
    case "panel/open-sync":
      // sidePanel.open is the first call, before any await.
      chrome.sidePanel.open({tabId: sender.tab.id}).then(
        () => log("panel-open", {mode: "sync", ok: true}),
        (e) => log("panel-open", {mode: "sync", ok: false, err: String(e)}),
      );
      respond({started: true});
      return false;
    case "panel/open-after-await":
      (async () => {
        await chrome.storage.session.get("x"); // a real async hop first
        try {
          await chrome.sidePanel.open({tabId: sender.tab.id});
          await log("panel-open", {mode: "after-await", ok: true});
        } catch (e) {
          await log("panel-open", {mode: "after-await", ok: false, err: String(e)});
        }
      })();
      respond({started: true});
      return false;
    case "window/open":
      chrome.windows
        .create({url: chrome.runtime.getURL("panel.html") + "?tab=" + sender.tab.id, type: "popup", width: 400, height: 600})
        .then((w) => log("window-open", {ok: true, id: w.id}), (e) => log("window-open", {ok: false, err: String(e)}));
      respond({started: true});
      return false;
    case "popup/open-nogesture":
      chrome.action.openPopup().then(
        () => log("openPopup", {ok: true}),
        (e) => log("openPopup", {ok: false, err: String(e)}),
      );
      respond({started: true});
      return false;
    case "spike/set-access":
      chrome.storage.local.setAccessLevel({accessLevel: msg.level}).then(
        () => respond({ok: true}),
        (e) => respond({ok: false, err: String(e)}),
      );
      return true;
    case "spike/resume": {
      const n = msg.size;
      const bytes = new Uint8Array(n);
      let x = 12345;
      for (let i = 0; i < n; i++) {
        x = (Math.imul(x, 1103515245) + 12345) & 0x7fffffff;
        bytes[i] = x & 0xff;
      }
      let bin = "";
      for (let i = 0; i < n; i += 0x8000) bin += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
      respond({name: "resume.pdf", type: "application/pdf", b64: btoa(bin), size: n});
      return false;
    }
    case "spike/set-flag":
      chrome.storage.local.set({[msg.key]: msg.value}).then(() => respond({ok: true}));
      return true;
    case "spike/ports":
      respond({ports: ports.size});
      return false;
    case "spike/register":
      chrome.scripting
        .registerContentScripts([{id: "wd", matches: msg.matches, js: ["content.js"], allFrames: true, runAt: "document_idle"}])
        .then(() => respond({ok: true}), (e) => respond({ok: false, err: String(e)}));
      return true;
    case "spike/has-permission":
      chrome.permissions.contains({origins: msg.origins}).then((r) => respond({granted: r}));
      return true;
    case "spike/reload":
      respond({ok: true});
      setTimeout(() => chrome.runtime.reload(), 50);
      return false;
  }
  return false;
});
