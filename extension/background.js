// jobhunter service worker (specs/017 phase 1e). The only part of the extension that talks to
// the console. It reads the token from storage on every request and keeps nothing that
// matters in globals: Chrome stops an idle worker after about 30 seconds, and the console's
// own state is the truth (the popup rebuilds from /status).
//
// Besides capture, the worker serves the ATS content scripts (specs/022): it answers their
// hello and form-data requests, relays the panel open, re-injects them after a reload, and
// refuses every message that MESSAGE_POLICY does not allow from that sender.
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
const VERSION_TTL_MS = 30 * 1000; // short: the off list and pairing must not go stale
const MAX_ENTRIES = 50;
const FETCH_TIMEOUT_MS = 25 * 1000; // every console route answers within 20 s
const CONSOLE_PATHS = /^\/(job|packet)\/\d+$|^\/apply\/new(\?url=[^#]*)?$|^\/captured$|^\/prefs$/;

// The pairing token is for trusted contexts (this worker and the extension's own pages) only.
chrome.storage.local.setAccessLevel({accessLevel: "TRUSTED_CONTEXTS"}).catch(() => {});
// When this worker started (the e2e harness checks that a stopped worker really restarted).
chrome.storage.session.set({bootAt: Date.now()}).catch(() => {});

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
    await chrome.storage.session.remove("cached");
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
  if (resp.status === 401 || resp.status === 403 || resp.status === 426) {
    await chrome.storage.session.remove("cached");
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
  if (!(await config()).token) {
    throw new ApiError("unpaired", "Pair the extension first: open its options page.");
  }
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
  // The fragment stays: hash-routed boards change the job in it.
  return tabId + " " + u.origin + u.pathname + (q ? "?" + q : "") + u.hash;
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
    // Only an answer that wrote something is shown again; a preview, a possible match or
    // a same-job offer wrote nothing, so reopening captures again (a new selection counts).
    const kept = fresh && ["added", "existing", "linked"].includes(entry.outcome);
    if (kept && recent.has(key)) {
      const seen = recent.get(key);
      const repeat = Boolean(seen.shown);
      seen.shown = true;
      return Object.assign({}, seen, {repeat});
    }
    if (kept && entry.card) {
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

function forget(key) {
  return serial(async () => {
    const all = await entries();
    delete all[key];
    await chrome.storage.session.set({entries: all});
  });
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

// ── messages: the sender policy (specs/022 "Sender policy") ──────────────────

async function openConsole(path) {
  if (!CONSOLE_PATHS.test(path)) {
    throw new ApiError("error", "not a console page");
  }
  const {port} = await config();
  await chrome.tabs.create({url: "http://127.0.0.1:" + port + path});
}

// The one table of who may send what. Kinds: "ats" (a content script on a granted ATS host, in a
// frame that announced itself), "generic" (a frame this worker injected into after your click on
// a non-ATS site), "page" (the extension's own pages). Anything not here, or from a kind not
// listed, is refused. No spend or write type, and no mode or pairing type, lists a content
// script (tests/test_extension_message_policy.py keeps it so).
const deepFreeze = (table) => Object.freeze(Object.fromEntries(
  Object.entries(table).map(([type, kinds]) => [type, Object.freeze(kinds)])));
const MESSAGE_POLICY = deepFreeze({
  "frame/hello": ["ats", "generic"],
  "forms/get": ["ats", "generic"],
  "frame/bye": ["ats", "generic"],
  "panel/open": ["ats"],
  "submit/attempt": ["ats"],
  "submit/outcome": ["ats"],
  "ask/answer": ["ats"],
  "applied/undo": ["ats"],
  "open": ["page"],
  "again": ["page"],
  "acceptNotice": ["page"],
  "add": ["page"],
  "describe": ["page"],
  "link": ["page"],
  "fetch": ["page"],
  "estimate": ["page"],
  "status": ["page"],
  "prepare": ["page"],
  "score": ["page"],
  "openConsole": ["page"],
  "pair": ["page"],
  "pairStatus": ["page"],
  "unpair": ["page"],
  "panel/state": ["page"],
  "switches/get": ["page"],
  "switches/set": ["page"],
  "session/start": ["page"],
  "fill/write": ["page"],
  "match/pick": ["page"],
  "draft/estimate": ["page"],
  "draft/generate": ["page"],
  "packet/ready": ["page"],
  "file/attach": ["page"],
  "text/insert": ["page"],
  "ats/mode": ["page"]
});

// The required ATS hosts, from pipeline/ats_rules.py (a test keeps this and the manifest equal
// to it). Workday joins when you grant it (ticket A11).
const ATS_HOSTS = {
  greenhouse: ["boards.greenhouse.io", "job-boards.greenhouse.io", "job-boards.eu.greenhouse.io"],
  lever: ["jobs.lever.co", "jobs.eu.lever.co"],
  ashby: ["jobs.ashbyhq.com"],
  workable: ["apply.workable.com"],
};
const FORMS_TTL_MS = 10 * 60 * 1000;
const PANEL_PATH = "panel/index.html";
const SWITCH_DEFAULT = {show_buttons: true, record_submits: true};

function atsOfUrl(url) {
  let u;
  try {
    u = new URL(url);
  } catch (e) {
    return null;
  }
  if (u.protocol !== "https:") {
    return null;
  }
  for (const [ats, hosts] of Object.entries(ATS_HOSTS)) {
    if (hosts.includes(u.hostname.toLowerCase())) {
      return ats;
    }
  }
  return null;
}

// What can be known without awaiting: this extension's id, the page kind, the frame facts.
function syncKind(sender) {
  if (!sender || sender.id !== chrome.runtime.id) {
    return null;
  }
  if (typeof sender.url === "string" && sender.url.startsWith(chrome.runtime.getURL(""))) {
    return "page";
  }
  if (!sender.tab || typeof sender.frameId !== "number" || !sender.documentId
      || typeof sender.url !== "string") {
    return null;
  }
  return atsOfUrl(sender.url) ? "ats" : "other";
}

// ── the frame registry (storage.session: survives an idle worker stop) ───────

const frameKey = (tabId, frameId) => tabId + ":" + frameId;

async function sessionMap(name) {
  const got = await chrome.storage.session.get(name);
  return got[name] || {};
}

function updateMap(name, fn) {
  return serial(async () => {
    const map = await sessionMap(name);
    fn(map);
    await chrome.storage.session.set({[name]: map});
  });
}

// The full check, with the registry: a frame is "ats" only if it announced itself with the
// document id it now sends from; "generic" only on a document we injected into ourselves.
async function classify(type, sender) {
  const kind = syncKind(sender);
  if (kind === "page" || kind === null) {
    return kind;
  }
  const reg = (await sessionMap("frames"))[frameKey(sender.tab.id, sender.frameId)];
  const announced = Boolean(reg) && reg.documentId === sender.documentId;
  if (kind === "ats") {
    return type === "frame/hello" || (announced && !reg.generic) ? "ats" : "unannounced";
  }
  const injected = (await sessionMap("genericDocs"))[sender.tab.id];
  if (sender.frameId !== 0 || injected !== sender.documentId) {
    return null;
  }
  return type === "frame/hello" || (announced && reg.generic) ? "generic" : "unannounced";
}

// Never logs a payload: the type and the sender kind only.
function refused(type, kind) {
  console.warn("jobhunter: refused message", String(type).slice(0, 40), kind);
}

const HANDLERS = {};
const notBuilt = async () => ({state: "unavailable", message: "not built yet"});

HANDLERS["frame/hello"] = async (msg, sender, kind) => {
  await updateMap("frames", (frames) => {
    frames[frameKey(sender.tab.id, sender.frameId)] = {
      documentId: sender.documentId,
      host: hostOf(sender.url),
      ats: kind === "ats" ? atsOfUrl(sender.url) : null,
      generic: kind === "generic",
      at: Date.now(),
    };
  });
  return {state: "ok", frameId: sender.frameId};
};

HANDLERS["frame/bye"] = async (msg, sender) => {
  await updateMap("frames", (frames) => {
    const key = frameKey(sender.tab.id, sender.frameId);
    if (frames[key] && frames[key].documentId === sender.documentId) {
      delete frames[key];
    }
  });
  return {state: "ok"};
};

chrome.tabs.onRemoved.addListener((tabId) => {
  updateMap("frames", (frames) => {
    for (const key of Object.keys(frames)) {
      if (key.startsWith(tabId + ":")) {
        delete frames[key];
      }
    }
  }).catch(() => {});
  updateMap("genericDocs", (docs) => {
    delete docs[tabId];
  }).catch(() => {});
  updateMap("panelField", (fields) => {
    delete fields[tabId];
  }).catch(() => {});
});

// ── form data (the console's `forms` route, cached) and the switches ─────────

async function formsData() {
  if (!(await config()).token) {
    throw new ApiError("unpaired", "Pair the extension first: open its options page.");
  }
  const {formsCache} = await chrome.storage.local.get("formsCache");
  if (formsCache && Date.now() - formsCache.at < FORMS_TTL_MS) {
    return formsCache;
  }
  try {
    const r = await api("forms", {});
    if (r.status === 200 && r.data && r.data.forms) {
      const fresh = {at: Date.now(), version: String(r.data.version), forms: r.data.forms};
      await chrome.storage.local.set({formsCache: fresh});
      return fresh;
    }
  } catch (e) {
    if (e && e.state === "unpaired") {
      throw e;
    }
  }
  if (formsCache) {
    return formsCache; // console down or refused: the overlay is drawn from what we have
  }
  throw new ApiError("unreachable", "No form data yet: start the jobhunter console.");
}

async function switchesAll() {
  const {switches} = await chrome.storage.local.get("switches");
  const out = {};
  for (const ats of Object.keys(ATS_HOSTS)) {
    const mine = (switches && switches[ats]) || {};
    out[ats] = {
      show_buttons: typeof mine.show_buttons === "boolean" ? mine.show_buttons
        : SWITCH_DEFAULT.show_buttons,
      record_submits: typeof mine.record_submits === "boolean" ? mine.record_submits
        : SWITCH_DEFAULT.record_submits,
    };
  }
  return out;
}

// Tells every frame we know of to ask for its form data and switches again.
async function broadcastFormsChanged() {
  const frames = await sessionMap("frames");
  const tabs = new Set(Object.keys(frames).map((k) => Number(k.split(":")[0])));
  for (const id of tabs) {
    chrome.tabs.sendMessage(id, {type: "forms/changed"}).catch(() => {});
  }
}

// The ATS and the host come from the sender's URL, never from the payload.
HANDLERS["forms/get"] = async (msg, sender, kind) => {
  const ats = kind === "ats" ? atsOfUrl(sender.url) : null;
  let data;
  try {
    data = await formsData();
  } catch (e) {
    return {state: (e && e.state) || "error", message: String((e && e.message) || e)};
  }
  const forms = data.forms || {};
  const off = {show_buttons: false, record_submits: false};
  return {
    state: "ok",
    ats,
    version: data.version,
    forms: ats ? (forms.ats || {})[ats] || null : null,
    generic: kind === "generic" ? forms.generic || null : null,
    switches: ats ? (await switchesAll())[ats] : off,
  };
};

HANDLERS["switches/get"] = async () => ({state: "ok", switches: await switchesAll()});

HANDLERS["switches/set"] = async (msg) => {
  if (!Object.hasOwn(ATS_HOSTS, msg.ats)) {
    throw new ApiError("error", "unknown ATS");
  }
  const all = await switchesAll();
  for (const key of ["show_buttons", "record_submits"]) {
    if (typeof msg[key] === "boolean") {
      all[msg.ats][key] = msg[key];
    }
  }
  await chrome.storage.local.set({switches: all});
  await broadcastFormsChanged();
  return {state: "ok", switches: all};
};

// ── the side panel, bound to a tab ───────────────────────────────────────────

// Called from the message listener before any await: chrome.sidePanel.open keeps the click's
// user gesture only when it is the first thing the handler does (A1 measured; the lint keeps it).
function openPanel(tabId) {
  const path = PANEL_PATH + "?tab=" + tabId;
  chrome.sidePanel.setOptions({tabId, path, enabled: true}).catch(() => {});
  chrome.sidePanel.open({tabId}).catch(() => {
    // Without a gesture the panel cannot open: the same page in a small extension window.
    chrome.windows.create({url: chrome.runtime.getURL(path), type: "popup", width: 420,
      height: 680}).catch(() => {});
  });
}

HANDLERS["panel/state"] = async (msg) => {
  const tab = Number(msg.tab);
  if (!Number.isInteger(tab)) {
    throw new ApiError("error", "no tab");
  }
  try {
    await chrome.tabs.get(tab);
  } catch (e) {
    return {state: "gone", tab};
  }
  const frames = Object.entries(await sessionMap("frames"))
    .filter(([key]) => key.startsWith(tab + ":"))
    .map(([key, f]) => ({frameId: Number(key.split(":")[1]), host: f.host, ats: f.ats}));
  const ats = (frames.find((f) => f.ats) || {}).ats || null;
  const top = frames.find((f) => f.frameId === 0);
  const pending = (await sessionMap("panelField"))[tab] || null;
  return {state: "ok", tab, ats, host: top ? top.host : (frames[0] || {}).host || null, frames,
    field: pending && pending.field};
};

// ── re-injection after an extension reload or first install ──────────────────

// Content scripts are not injected into tabs that were already open. The tabs on a granted ATS
// host are found by their address (the host permissions allow the query) and get the script in
// every frame. An ATS iframe on an employer page is not reached: Chrome refuses an all-frames
// injection when the top frame's host is not granted, and finding the frame ids would need
// `webNavigation`, which stays off. Reload such a page once after reloading the extension.
async function reinject() {
  const patterns = Object.values(ATS_HOSTS).flat().map((h) => "https://" + h + "/*");
  let tabs = [];
  try {
    tabs = await chrome.tabs.query({url: patterns});
  } catch (e) {
    return;
  }
  for (const t of tabs) {
    try {
      await chrome.scripting.executeScript({target: {tabId: t.id, allFrames: true},
        files: ["content/frame.js"]});
    } catch (e) {
      // the tab is gone or navigated away
    }
  }
}

chrome.runtime.onInstalled.addListener((details) => {
  if (details.reason === "install" || details.reason === "update") {
    reinject();
  }
});

// Fill this page (ticket A6) injects into frame 0 only, after the icon click; the worker notes
// the document so the sender policy can tell it from any other script.
async function injectGeneric(tab) {
  const res = await chrome.scripting.executeScript({target: {tabId: tab.id},
    files: ["content/frame.js"]});
  const doc = res && res[0] && res[0].documentId;
  if (doc) {
    await updateMap("genericDocs", (docs) => {
      docs[tab.id] = doc;
    });
  }
  return {documentId: doc || null};
}

// The long-lived port each content script opens only lets it detect an orphaned state; it
// carries nothing. Anything but a content script is hung up on.
chrome.runtime.onConnect.addListener((port) => {
  const kind = syncKind(port.sender);
  if (kind !== "ats" && kind !== "other") {
    port.disconnect();
  }
});

// ── the handlers for the extension's own pages (popup, options, panel) ───────

HANDLERS["open"] = (msg) => captureTab(msg.tab, {trigger: "popup", again: false});
HANDLERS["again"] = (msg) => captureTab(msg.tab, {trigger: "popup", again: true});
HANDLERS["acceptNotice"] = async (msg) => {
  await acceptNotice(msg.board);
  return captureTab(msg.tab, {trigger: "popup", again: true});
};
HANDLERS["add"] = async (msg) => {
  const r = await api("capture/add", msg.body);
  if (r.status === 200) {
    await updateEntry(msg.key, r.data);
  }
  return r;
};
HANDLERS["describe"] = async (msg) => {
  const r = await api("groups/" + Number(msg.group) + "/describe", msg.body);
  if (r.status === 200) {
    await updateEntry(msg.key, {outcome: "existing", group: r.data.group});
  }
  return r;
};
HANDLERS["link"] = async (msg) => {
  const r = await api("link", msg.body);
  if (msg.key) {
    recent.delete(msg.key);
    if (r.status === 200) {
      await forget(msg.key); // the stored answer named a group that may be gone now
    }
  }
  return r;
};
HANDLERS["fetch"] = (msg) => api("capture/fetch", msg.body);
for (const name of ["estimate", "status", "prepare"]) {
  HANDLERS[name] = (msg) => api("groups/" + Number(msg.group) + "/" + name,
    {job_id: msg.job || null});
}
HANDLERS["score"] = (msg) => api("groups/" + Number(msg.group) + "/score", msg.body);
HANDLERS["openConsole"] = async (msg) => {
  await openConsole(msg.path);
  return {ok: true};
};
HANDLERS["pair"] = async (msg) => {
  const port = Number(msg.port) || DEFAULT_PORT;
  await chrome.storage.local.set({port});
  await chrome.storage.session.remove("cached");
  const r = await api("pair", {code: String(msg.code || "")}, {auth: false});
  if (r.status === 200 && r.data && r.data.token) {
    await chrome.storage.local.set({token: r.data.token});
    await broadcastFormsChanged();
    return {paired: true};
  }
  return {paired: false, message: (r.data && r.data.error) || ("console answered " + r.status)};
};
HANDLERS["pairStatus"] = async () => {
  const {token, port} = await config();
  return {paired: Boolean(token), port};
};
HANDLERS["unpair"] = async () => {
  await chrome.storage.local.remove(["token", "formsCache"]);
  await chrome.storage.session.remove("cached");
  await broadcastFormsChanged();
  return {paired: false};
};
// Spend and write types and the submit watch land with their tickets; listed already so the
// policy table is complete and the refusals can be tested.
for (const name of ["session/start", "fill/write", "match/pick", "draft/estimate",
  "draft/generate", "packet/ready", "file/attach", "text/insert", "ats/mode",
  "submit/attempt", "submit/outcome", "ask/answer", "applied/undo"]) {
  HANDLERS[name] = notBuilt;
}

HANDLERS["panel/open"] = async () => ({state: "ok"}); // the listener itself answers it

function fail(err) {
  return {
    state: err && err.state ? err.state : "error",
    message: String((err && err.message) || err),
  };
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg || typeof msg.type !== "string" || !Object.hasOwn(MESSAGE_POLICY, msg.type)) {
    refused(msg && msg.type, syncKind(sender));
    sendResponse({state: "refused", message: "not a request this extension accepts"});
    return false;
  }
  const allowed = MESSAGE_POLICY[msg.type];
  // The quick check first: a sender whose kind the type does not list is refused (and logged)
  // whether or not it announced itself; "unannounced" is only for a kind that could send it.
  const quick = syncKind(sender);
  if (!allowed.includes(quick === "other" ? "generic" : quick)) {
    refused(msg.type, quick);
    sendResponse({state: "refused", message: "not allowed from here"});
    return false;
  }
  if (msg.type === "panel/open") {
    // Synchronously, before any await, so the click's gesture is still there (the registry
    // check would need an await; the facts checked here are the sender's own).
    const kind = syncKind(sender);
    if (!allowed.includes(kind)) {
      refused(msg.type, kind);
      sendResponse({state: "refused", message: "not allowed from here"});
      return false;
    }
    openPanel(sender.tab.id);
    updateMap("panelField", (fields) => {
      fields[sender.tab.id] = {field: String(msg.field || "").slice(0, 80), at: Date.now()};
    }).catch(() => {});
    sendResponse({state: "ok"});
    return false;
  }
  classify(msg.type, sender).then((kind) => {
    if (kind === "unannounced") {
      sendResponse({state: "unannounced"});
    } else if (!allowed.includes(kind)) {
      refused(msg.type, kind);
      sendResponse({state: "refused", message: "not allowed from here"});
    } else {
      HANDLERS[msg.type](msg, sender, kind).then(sendResponse, (err) => sendResponse(fail(err)));
    }
  }, (err) => sendResponse(fail(err)));
  return true;
});

function onMenu(info, tab) {
  if (!tab || info.menuItemId !== "send-to-jobhunter") {
    return null;
  }
  const trigger = info.selectionText ? "menu-selection" : "menu-page";
  const t = {id: tab.id, url: tab.url, windowId: tab.windowId, openerTabId: tab.openerTabId};
  const run = captureTab(t, {trigger, selectionText: info.selectionText || null, again: true});
  run.then((res) => setBadge(tab.id, res && res.state === "result"))
    .catch(() => setBadge(tab.id, false));
  // Shows the stored result; if Chrome refuses, the user clicks the icon to see it.
  chrome.action.openPopup().catch(() => {});
  return run;
}

chrome.contextMenus.onClicked.addListener((info, tab) => {
  onMenu(info, tab);
});

// For the e2e harness and debugging from the worker console: the same path the popup takes.
self.jobhunterCapture = (tab, opts) => captureTab(tab, opts || {trigger: "popup"});
// The context-menu handler itself (the e2e cannot click a context menu).
self.jobhunterMenu = (info, tab) => onMenu(info, tab);
// Fill this page's injection (ticket A6 calls it after the icon click) and the e2e harness.
self.jobhunterInjectGeneric = (tab) => injectGeneric(tab);
