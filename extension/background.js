// jobhunter service worker (specs/017 phase 1e). The only part of the extension that talks to
// the console. It reads the token from storage on every request and keeps nothing that
// matters in globals: Chrome stops an idle worker after about 30 seconds, and the console's
// own state is the truth (the popup rebuilds from /status).
//
// Capture runs only on a user gesture (the toolbar icon or Alt+Shift+J, which open the popup,
// or the right-click menu), under activeTab, and stops before any script runs in the page when
// the console is not paired or not reachable, the host is on the off list, or the LinkedIn /
// Indeed notice has not been accepted.

const DEFAULT_PORT = 8808;
const EXT_VERSION = chrome.runtime.getManifest().version;
// Built in so the notice applies even before the first /version answer; the console sends the
// same patterns (pipeline/board_ids.py), and a test keeps the two equal.
const NOTICE_HOSTS = [
  "(?:^|\\.)linkedin\\.com$",
  "(?:^|\\.)indeed\\.(?:com|co\\.[a-z]{2}|com\\.[a-z]{2}|[a-z]{2})$",
];
const IN_FLIGHT_MS = 60 * 1000;
const RESULT_MS = 10 * 60 * 1000;
const BOARD_WINDOW_MS = 30 * 60 * 1000;
const VERSION_TTL_MS = 5 * 60 * 1000;
const MAX_ENTRIES = 50;
const FETCH_TIMEOUT_MS = 25 * 1000; // every console route answers within 20 s
const CONSOLE_PATHS = /^\/(job|packet)\/\d+$|^\/apply\/new(\?url=[^#]*)?$|^\/captured$|^\/prefs$/;

// The pairing token is for trusted contexts (this worker and the extension's own pages) only.
chrome.storage.local.setAccessLevel({accessLevel: "TRUSTED_CONTEXTS"}).catch(() => {});

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.removeAll(() => {
    // No "link" context: capture never follows a link.
    chrome.contextMenus.create({
      id: "send-to-jobhunter",
      title: "Send to jobhunter",
      contexts: ["page", "selection"],
    });
  });
});

class ApiError extends Error {
  constructor(state, message, status = 0) {
    super(message);
    this.state = state;
    this.status = status;
  }
}

async function config() {
  const got = await chrome.storage.local.get(["token", "port"]);
  const port = Number(got.port) || DEFAULT_PORT;
  return {token: got.token || null, port};
}

async function api(path, body, {auth = true} = {}) {
  const {token, port} = await config();
  if (auth && !token) {
    throw new ApiError("unpaired", "Pair the extension first: open its options page.");
  }
  const headers = {"Content-Type": "application/json", "X-Jobhunter-Ext": EXT_VERSION};
  if (auth) {
    headers.Authorization = "Bearer " + token;
  }
  let resp;
  try {
    resp = await fetch("http://127.0.0.1:" + port + "/ext/v1/" + path, {
      method: "POST",
      headers,
      body: JSON.stringify(body || {}),
      signal: AbortSignal.timeout(FETCH_TIMEOUT_MS),
    });
  } catch (e) {
    throw new ApiError(
      "unreachable",
      "The jobhunter console is not reachable on port " + port + ". Start it with: jobhunter console",
    );
  }
  let data = null;
  try {
    data = await resp.json();
  } catch (e) {
    data = null;
  }
  if (resp.status === 401) {
    throw new ApiError("unpaired", "Not paired, or the pairing was revoked: pair again in options.", 401);
  }
  if (resp.status === 426) {
    throw new ApiError("outdated", "Reload the extension from your checkout (chrome://extensions).", 426);
  }
  return {status: resp.status, data};
}

// ── versions, off list, notices ───────────────────────────────────────────────

async function version() {
  const {cached} = await chrome.storage.session.get("cached");
  if (cached && Date.now() - cached.at < VERSION_TTL_MS) {
    return cached.data;
  }
  const r = await api("version", {});
  if (r.status !== 200 || !r.data) {
    throw new ApiError("error", "The console refused the version check (" + r.status + ").");
  }
  await chrome.storage.session.set({cached: {at: Date.now(), data: r.data}});
  return r.data;
}

function hostOf(url) {
  try {
    return new URL(url).hostname.toLowerCase();
  } catch (e) {
    return "";
  }
}

function noticeHost(host, patterns) {
  for (const p of patterns) {
    let re;
    try {
      re = new RegExp(p);
    } catch (e) {
      continue;
    }
    if (re.test(host)) {
      return p.includes("linkedin") ? "linkedin" : "indeed";
    }
  }
  return null;
}

function disabledHost(host, list) {
  return (list || []).some((d) => {
    const h = String(d).toLowerCase().replace(/^\.+/, "");
    return h && (host === h || host.endsWith("." + h));
  });
}

async function noticeAccepted(board) {
  const {notices} = await chrome.storage.local.get("notices");
  return Boolean(notices && notices[board]);
}

async function acceptNotice(board) {
  const {notices} = await chrome.storage.local.get("notices");
  const next = Object.assign({}, notices || {});
  next[board] = new Date().toISOString();
  await chrome.storage.local.set({notices: next});
}

// ── popup state: one entry per capture key, no page text ──────────────────────

let chain = Promise.resolve();
function serial(fn) {
  const run = chain.then(fn, fn);
  chain = run.catch(() => {});
  return run;
}

function captureKey(tabId, url, ver) {
  let u;
  try {
    u = new URL(url);
  } catch (e) {
    return tabId + " " + url;
  }
  const drop = new Set((ver && ver.tracking_params) || []);
  const prefixes = (ver && ver.tracking_prefixes) || ["utm_"];
  const kept = [];
  for (const [k, v] of u.searchParams) {
    const lk = k.toLowerCase();
    if (!drop.has(lk) && !prefixes.some((p) => lk.startsWith(p))) {
      kept.push([k, v]);
    }
  }
  const q = new URLSearchParams(kept).toString();
  return tabId + " " + u.origin + u.pathname + (q ? "?" + q : "");
}

async function entries() {
  const {entries: all} = await chrome.storage.session.get("entries");
  return all || {};
}

function saveEntry(key, entry) {
  return serial(async () => {
    const all = await entries();
    all[key] = entry;
    const keys = Object.keys(all).sort((a, b) => (all[a].at || 0) - (all[b].at || 0));
    while (keys.length > MAX_ENTRIES) {
      delete all[keys.shift()];
    }
    await chrome.storage.session.set({entries: all});
  });
}

chrome.tabs.onRemoved.addListener((tabId) => serial(async () => {
  const all = await entries();
  for (const key of Object.keys(all)) {
    if (key.startsWith(tabId + " ")) {
      delete all[key];
    }
  }
  await chrome.storage.session.set({entries: all});
}));

function cardOf(group) {
  if (!group) {
    return null;
  }
  // Short fields only: never the description or its first lines.
  return {
    group_id: group.group_id,
    job_id: group.job_id,
    title: group.title,
    employer: group.employer,
    location: group.location,
    salary: group.salary,
    posted_at: group.posted_at,
    host: group.host,
    scored: group.scored,
    bucket: group.bucket,
    bucket_name: group.bucket_name,
    verdict: group.verdict,
    packet_id: group.packet_id,
    has_description: group.has_description,
    description_chars: group.description_chars,
    score_on_request: group.score_on_request,
    path: group.path,
  };
}

async function setBadge(tabId, ok) {
  try {
    await chrome.action.setBadgeText({tabId, text: ok ? "✓" : "!"});
    await chrome.action.setBadgeBackgroundColor({tabId, color: ok ? "#2e7d32" : "#b3261e"});
  } catch (e) {
    // the tab may be gone
  }
}

// Responses kept in this worker's memory only, so an open popup shows a fresh menu capture
// without capturing again. Lost when the worker stops; the popup then rebuilds from status.
const recent = new Map();

async function boardCaptureFor(tab, url) {
  if (noticeHost(hostOf(url), NOTICE_HOSTS)) {
    return null;
  }
  const all = await entries();
  let best = null;
  for (const e of Object.values(all)) {
    if (!e.board || e.board.apply_mode !== "offsite" || e.board.apply_url || !e.card) {
      continue;
    }
    if (Date.now() - e.at > BOARD_WINDOW_MS) {
      continue;
    }
    const related = e.windowId === tab.windowId || (tab.openerTabId && e.tabId === tab.openerTabId);
    if (related && (!best || e.at > best.at)) {
      best = e;
    }
  }
  if (!best) {
    return null;
  }
  return {
    group_id: best.card.group_id,
    job_id: best.card.job_id || null,
    board_key: best.board.key,
    title: (best.card.title || "").slice(0, 1000),
    employer: (best.card.employer || "").slice(0, 1000),
    minutes_ago: Math.min(30, Math.floor((Date.now() - best.at) / 60000)),
  };
}

// Single flight per tab: a capture waits for the one before it on the same tab, so a menu
// capture followed by the popup opening, or a double shortcut, makes one request and the
// second trigger shows the first one's result.
const tabRuns = new Map();

function captureTab(tab, opts) {
  const prev = tabRuns.get(tab.id);
  const run = (async () => {
    if (prev) {
      await prev.catch(() => null);
    }
    return captureNow(tab, opts);
  })();
  tabRuns.set(tab.id, run);
  run.finally(() => {
    if (tabRuns.get(tab.id) === run) {
      tabRuns.delete(tab.id);
    }
  }).catch(() => {});
  return run;
}

async function captureNow(tab, {trigger, selectionText = null, again = false}) {
  const ver = await version(); // fail closed: no console, nothing is injected
  const host = hostOf(tab.url);
  if (!/^https?:/.test(tab.url || "")) {
    return {state: "restricted", url: tab.url || ""};
  }
  if (disabledHost(host, ver.disabled_hosts)) {
    return {state: "disabled", host};
  }
  const board = noticeHost(host, NOTICE_HOSTS.concat(ver.notice_hosts || []));
  if (board && !(await noticeAccepted(board))) {
    return {state: "notice", host, board}; // nothing read, a menu selection discarded
  }
  const key = captureKey(tab.id, tab.url, ver);
  if (!again) {
    const entry = (await entries())[key];
    const fresh = entry && entry.state === "result" && Date.now() - entry.at < RESULT_MS;
    if (fresh && recent.has(key)) {
      const seen = recent.get(key);
      const repeat = Boolean(seen.shown);
      seen.shown = true;
      return Object.assign({}, seen, {repeat});
    }
    if (fresh && entry.card && ["added", "existing", "linked"].includes(entry.outcome)) {
      return {state: "stored", key, entry};
    }
  }
  return doCapture(tab, key, {trigger, selectionText, again});
}

async function doCapture(tab, key, {trigger, selectionText, again}) {
  const before = (await entries())[key];
  // A request whose answer was lost is resent with the same action_id; anything else is a
  // new user action with a new one.
  const lost = !again && before && before.state === "in flight" && Date.now() - before.at >= IN_FLIGHT_MS;
  const actionId = lost ? before.action_id : crypto.randomUUID();
  await saveEntry(key, {state: "in flight", action_id: actionId, at: Date.now(), tabId: tab.id,
    windowId: tab.windowId});
  let injected;
  try {
    injected = await chrome.scripting.executeScript({
      target: {tabId: tab.id},
      files: ["capture/extract.js"],
    });
  } catch (e) {
    await saveEntry(key, {state: "error", at: Date.now(), tabId: tab.id, windowId: tab.windowId});
    return {state: "restricted", url: tab.url, message: String(e && e.message || e)};
  }
  const first = injected && injected[0];
  const facts = first && first.result;
  if (!facts || typeof facts !== "object") {
    return {state: "restricted", url: tab.url};
  }
  facts.trigger = trigger;
  if (selectionText) {
    facts.selection = String(selectionText).slice(0, 100000);
  }
  const boardCapture = await boardCaptureFor(tab, facts.url);
  const r = await api("capture", {action_id: actionId, facts, board_capture: boardCapture});
  if (r.status !== 200) {
    await saveEntry(key, {state: "error", at: Date.now(), tabId: tab.id, windowId: tab.windowId});
    await setBadge(tab.id, false);
    return {state: "error", message: (r.data && r.data.error) || ("console answered " + r.status)};
  }
  const resp = r.data;
  await saveEntry(key, {
    state: "result",
    action_id: actionId,
    documentId: first.documentId || null,
    at: Date.now(),
    tabId: tab.id,
    windowId: tab.windowId,
    outcome: resp.outcome,
    card: cardOf(resp.group),
    board: resp.board ? {key: resp.board.key, apply_mode: resp.board.apply_mode,
      apply_url: resp.board.apply_url} : null,
  });
  await setBadge(tab.id, resp.outcome !== "previewed");
  // A menu capture is shown by the popup that opens next; any later open is a repeat.
  const out = {state: "result", key, response: resp, facts, shown: trigger === "popup"};
  recent.set(key, out);
  while (recent.size > 10) {
    recent.delete(recent.keys().next().value);
  }
  return out;
}

async function updateEntry(key, resp) {
  if (!key || !resp) {
    return;
  }
  const all = await entries();
  const e = all[key];
  if (!e) {
    return;
  }
  e.state = "result";
  e.at = Date.now();
  e.outcome = resp.outcome || e.outcome;
  if (resp.group) {
    e.card = cardOf(resp.group);
  }
  await saveEntry(key, e);
  recent.delete(key);
}

// ── messages from the extension's own pages (popup, options) ─────────────────

async function openConsole(path) {
  if (!CONSOLE_PATHS.test(path)) {
    throw new ApiError("error", "not a console page");
  }
  const {port} = await config();
  await chrome.tabs.create({url: "http://127.0.0.1:" + port + path});
}

async function handle(msg) {
  switch (msg.type) {
    case "open":
      return captureTab(msg.tab, {trigger: "popup", again: false});
    case "again":
      return captureTab(msg.tab, {trigger: "popup", again: true});
    case "acceptNotice":
      await acceptNotice(msg.board);
      return captureTab(msg.tab, {trigger: "popup", again: true});
    case "add": {
      const r = await api("capture/add", msg.body);
      if (r.status === 200) {
        await updateEntry(msg.key, r.data);
      }
      return r;
    }
    case "describe": {
      const r = await api("groups/" + Number(msg.group) + "/describe", msg.body);
      if (r.status === 200) {
        await updateEntry(msg.key, {outcome: "existing", group: r.data.group});
      }
      return r;
    }
    case "link":
      return api("link", msg.body);
    case "fetch":
      return api("capture/fetch", msg.body);
    case "estimate":
    case "status":
    case "prepare":
      return api("groups/" + Number(msg.group) + "/" + msg.type, {job_id: msg.job || null});
    case "score":
      return api("groups/" + Number(msg.group) + "/score", msg.body);
    case "openConsole":
      await openConsole(msg.path);
      return {ok: true};
    case "pair": {
      const port = Number(msg.port) || DEFAULT_PORT;
      await chrome.storage.local.set({port});
      await chrome.storage.session.remove("cached");
      const r = await api("pair", {code: String(msg.code || "")}, {auth: false});
      if (r.status === 200 && r.data && r.data.token) {
        await chrome.storage.local.set({token: r.data.token});
        return {paired: true};
      }
      return {paired: false, message: (r.data && r.data.error) || ("console answered " + r.status)};
    }
    case "pairStatus": {
      const {token, port} = await config();
      return {paired: Boolean(token), port};
    }
    case "unpair":
      await chrome.storage.local.remove("token");
      return {paired: false};
    default:
      throw new ApiError("error", "unknown request");
  }
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (sender.id !== chrome.runtime.id || !msg || typeof msg.type !== "string") {
    return false;
  }
  handle(msg).then(sendResponse, (err) => sendResponse({
    state: err && err.state ? err.state : "error",
    message: String((err && err.message) || err),
  }));
  return true;
});

chrome.contextMenus.onClicked.addListener((info, tab) => {
  if (!tab || info.menuItemId !== "send-to-jobhunter") {
    return;
  }
  const trigger = info.selectionText ? "menu-selection" : "menu-page";
  const t = {id: tab.id, url: tab.url, windowId: tab.windowId, openerTabId: tab.openerTabId};
  captureTab(t, {trigger, selectionText: info.selectionText || null, again: true})
    .then((res) => setBadge(tab.id, res && res.state === "result"))
    .catch(() => setBadge(tab.id, false));
  // Shows the stored result; if Chrome refuses, the user clicks the icon to see it.
  chrome.action.openPopup().catch(() => {});
});

// For the e2e harness and debugging from the worker console: the same path the popup takes.
self.jobhunterCapture = (tab, opts) => captureTab(tab, opts || {trigger: "popup"});
