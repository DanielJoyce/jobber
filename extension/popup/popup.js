// The popup: shows what the console decided, and the next actions. Every request goes
// through the service worker (closing the popup does not cancel one). Page and model text is
// untrusted: it is only ever set with textContent or a form field's value.

const params = new URLSearchParams(location.search);
const main = document.getElementById("main");
const statusLine = document.getElementById("status");
let tab = null;
let current = null; // {key, response, facts}
let pollTimer = null;

const NOTICES = {
  linkedin: "LinkedIn's User Agreement forbids browser plugins that scrape or copy its data. " +
    "Sending one posting you are reading, for your own use, is close to copy and paste, but it " +
    "is a plugin reading their page and the risk is to your account. jobhunter reads nothing " +
    "here until you accept. Better still: open the employer's own page and send that.",
  indeed: "Indeed's terms forbid automated collection of its content. Sending one posting you " +
    "are reading, for your own use, is close to copy and paste, but the risk is to your " +
    "account. jobhunter reads nothing here until you accept. Better still: open the " +
    "employer's own page and send that.",
};

function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === "class") {
      node.className = v;
    } else if (k === "onclick") {
      node.addEventListener("click", v);
    } else if (v !== null && v !== undefined && v !== false) {
      node.setAttribute(k, v === true ? "" : String(v));
    }
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) {
      continue;
    }
    node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
  }
  return node;
}

function clear() {
  main.replaceChildren();
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

function setStatus(text, cls) {
  statusLine.textContent = text;
  statusLine.className = cls || "muted";
}

function send(msg) {
  return chrome.runtime.sendMessage(msg);
}

function aid() {
  return crypto.randomUUID();
}

function openConsole(path) {
  return send({type: "openConsole", path});
}

async function currentTab() {
  const forced = params.get("tab");
  if (forced) {
    return chrome.tabs.get(Number(forced));
  }
  const [t] = await chrome.tabs.query({active: true, currentWindow: true});
  return t;
}

function tabInfo() {
  return {id: tab.id, url: tab.url || "", windowId: tab.windowId, openerTabId: tab.openerTabId};
}

async function start(type) {
  clear();
  setStatus("Reading this page…");
  try {
    tab = await currentTab();
  } catch (e) {
    tab = null;
  }
  if (!tab) {
    setStatus("No tab to read.", "err");
    return;
  }
  const res = await send({type, tab: tabInfo()});
  render(res);
}

// ── rendering ────────────────────────────────────────────────────────────────

function render(res) {
  clear();
  if (!res) {
    setStatus("No answer from the extension's worker; try again.", "err");
    return;
  }
  switch (res.state) {
    case "unpaired":
      setStatus("Not paired", "warn");
      main.append(el("p", {}, res.message || "Pair the extension with your console first."),
        el("button", {class: "primary", onclick: () => chrome.runtime.openOptionsPage()},
          "Open options"));
      return;
    case "unreachable":
    case "outdated":
    case "error":
      setStatus(res.state === "error" ? "Error" : "Not available", "err");
      main.append(el("p", {}, res.message || "Something went wrong."));
      return;
    case "disabled":
      setStatus("Capture is off here", "warn");
      main.append(el("p", {}, "Capture is turned off for " + res.host +
        " ([capture] disabled_hosts in config.toml). Nothing was read."));
      return;
    case "notice":
      setStatus("Read this first", "warn");
      main.append(
        el("p", {id: "notice"}, NOTICES[res.board] || NOTICES.linkedin),
        el("div", {class: "row"},
          el("button", {class: "primary", id: "accept-notice", onclick: async () => {
            clear();
            setStatus("Reading this page…");
            render(await send({type: "acceptNotice", board: res.board, tab: tabInfo()}));
          }}, "I understand, send it"),
          el("button", {onclick: () => window.close()}, "Cancel")),
      );
      return;
    case "restricted":
      renderRestricted(res);
      return;
    case "stored":
      current = {key: res.key, response: null, facts: null};
      renderStored(res.entry);
      return;
    case "result":
      current = {key: res.key, response: res.response, facts: res.facts};
      renderResponse(res.response, Boolean(res.repeat));
      return;
    default:
      setStatus("Unexpected answer", "err");
  }
}

function renderRestricted(res) {
  setStatus("Chrome does not allow reading this page", "warn");
  main.append(el("p", {id: "restricted"},
    "Chrome does not let extensions read this page (a PDF, a chrome:// page or the Web Store). " +
    "Paste it on New packet instead."));
  if (/^https?:/.test(res.url || "")) {
    main.append(el("button", {class: "primary", id: "open-new-packet", onclick: () =>
      openConsole("/apply/new?url=" + encodeURIComponent(res.url))}, "Open New packet"));
  }
}

function cardBox(g, method) {
  const meta = [g.employer, g.location].filter(Boolean).join(" · ");
  const box = el("section", {class: "card", id: "card"},
    el("h2", {id: "card-title"}, g.title || "(no title)"),
    el("div", {id: "card-employer"}, meta),
    g.salary ? el("div", {}, g.salary) : null,
    g.posted_at ? el("div", {class: "muted"}, "posted " + String(g.posted_at).slice(0, 10)) : null,
    el("div", {class: "muted", id: "card-length"},
      (g.has_description ? g.description_chars + " characters of description" : "no description yet") +
      (method ? " · from " + method : "") + (g.host ? " · " + g.host : "")),
    g.first_lines ? el("div", {class: "lines"}, g.first_lines) : null,
    el("div", {class: "row", id: "card-score"},
      g.scored
        ? el("span", {class: "badge ok"}, (g.bucket_name || g.bucket) + (g.verdict ? " · " + g.verdict : ""))
        : el("span", {class: "badge"}, "not scored")),
  );
  return box;
}

function groupActions(g, {describe = false} = {}) {
  const row = el("div", {class: "row", id: "actions"},
    el("button", {id: "open", onclick: () => openConsole(g.path)}, "Open"));
  if (!g.scored && g.score_on_request) {
    row.append(el("button", {id: "score-it", onclick: () => scoreIt(g)}, "Score it"));
  }
  row.append(el("button", {id: "prepare", onclick: () => prepare(g)}, "Prepare packet"));
  if (describe) {
    row.append(el("button", {id: "describe-" + g.group_id, onclick: () => describeGroup(g)},
      "Add this description"));
  }
  return row;
}

function renderStored(entry) {
  const g = entry.card;
  setStatus("Already in jobhunter", "ok");
  main.append(cardBox(g), groupActions(g), el("div", {id: "score-box"}));
  refreshStatus(g);
}

function renderResponse(r, repeat = false) {
  const label = {
    added: ["Added", "ok"],
    existing: ["Already in jobhunter", "ok"],
    linked: ["Already in jobhunter", "ok"],
    possible: ["Looks like a posting you have", "warn"],
    same_job: ["Same job?", "warn"],
    previewed: ["Check this, then Add", "warn"],
  }[r.outcome] || [r.outcome, "muted"];
  setStatus(repeat && r.outcome === "added" ? "Already in jobhunter" : label[0], label[1]);
  if (r.message && r.outcome !== "added") {
    main.append(el("p", {id: "message"}, r.message));
  }
  if (r.group) {
    main.append(cardBox(r.group, r.method),
      groupActions(r.group, {describe: r.describe.includes(r.group.group_id)}),
      el("div", {id: "score-box"}));
    refreshStatus(r.group);
  }
  if (r.candidates && r.candidates.length) {
    main.append(candidatesBox(r));
  }
  if (r.offer) {
    main.append(offerBox(r));
  }
  if (r.outcome === "previewed") {
    main.append(previewForm(r, false));
  }
  if (r.board && r.board.apply_mode === "easy_apply") {
    main.append(el("p", {class: "muted"}, "Easy Apply: you apply on the board itself."));
  }
  if (r.fetch && !r.group) {
    main.append(fetchBox(r));
  }
}

function candidatesBox(r) {
  const list = el("ul", {id: "candidates"});
  for (const c of r.candidates) {
    list.append(el("li", {},
      el("strong", {}, c.title), " · " + c.employer + (c.why ? " (" + c.why + ")" : ""), " ",
      el("button", {class: "link", onclick: () => openConsole(c.path)}, "Open"),
      r.describe.includes(c.group_id)
        ? el("button", {class: "link", id: "describe-" + c.group_id,
          onclick: () => describeGroup(c)}, " Add this description")
        : null));
  }
  const box = el("section", {class: "card"}, list);
  const row = el("div", {class: "row"});
  if (r.outcome === "same_job") {
    const [a, b] = r.candidates;
    row.append(el("button", {class: "primary", id: "link-them", onclick: () => link({
      a: a.group_id, b: b ? b.group_id : null,
      board_key: r.board ? r.board.key : null, apply_url: r.board ? r.board.apply_url : null,
    })}, "Link them"));
  }
  row.append(el("button", {id: "add-as-new", onclick: () => {
    box.after(previewForm(r, true));
  }}, "Add as new"));
  box.append(row);
  return box;
}

function offerBox(r) {
  const o = r.offer;
  const box = el("section", {class: "card", id: "offer"}, el("p", {}, o.message));
  const row = el("div", {class: "row"},
    el("button", {class: "primary", id: "offer-link", onclick: () => link({
      a: o.group_id, b: r.group ? r.group.group_id : null, board_key: o.board_key,
      apply_url: r.url || null,
    })}, "Link them"),
    el("button", {id: "offer-not-same", onclick: () => r.group
      ? link({a: o.group_id, b: r.group.group_id, not_same: true})
      : box.remove()}, "Not the same"));
  box.append(row);
  return box;
}

function fetchBox(r) {
  const f = r.fetch;
  if (f.kind === "select") {
    return el("p", {class: "muted", id: "select-hint"},
      "The posting is in an embedded " + f.host + " form, which jobhunter may not fetch: " +
      "select its text, right-click your selection, then Send to jobhunter.");
  }
  return el("p", {}, el("button", {id: "fetch", onclick: async (ev) => {
    ev.target.disabled = true;
    const res = await send({type: "fetch", body: {action_id: aid(), url: f.url}});
    if (res.status === 200) {
      fillPreview({text: res.data.text, title: res.data.title, employer: res.data.employer,
        source: "fetch"});
    } else {
      showError((res.data && res.data.error) || res.message || "Fetch failed");
    }
    ev.target.disabled = false;
  }}, "Fetch from " + f.host));
}

// ── preview and Add ─────────────────────────────────────────────────────────

let previewState = null;

function previewForm(r, forceNew) {
  const p = r.preview;
  const old = document.getElementById("preview");
  if (old) {
    old.remove();
  }
  if (!p) {
    return el("p", {class: "err"}, "Nothing to add.");
  }
  previewState = {source: p.selection && !p.page_description ? "selection" : "edited",
    index: p.posting_index, forceNew};
  const title = el("input", {id: "pv-title"});
  title.value = p.title || "";
  const employer = el("input", {id: "pv-employer"});
  employer.value = p.employer || "";
  const text = el("textarea", {id: "pv-description"});
  text.value = p.description || "";
  const form = el("section", {class: "card", id: "preview"});
  if (p.reasons && p.reasons.length) {
    form.append(el("ul", {class: "muted"}, p.reasons.map((x) => el("li", {}, x))));
  }
  if (p.postings && p.postings.length) {
    const picker = el("select", {id: "pv-posting"},
      el("option", {value: ""}, "Pick the posting on this page…"),
      p.postings.map((c) => el("option", {value: String(c.index)}, c.title + " · " + c.employer)));
    picker.addEventListener("change", () => {
      const c = p.postings.find((x) => String(x.index) === picker.value);
      if (c) {
        title.value = c.title;
        employer.value = c.employer;
        text.value = c.description || text.value;
        previewState.index = c.index;
        previewState.source = "structured";
      }
    });
    form.append(el("label", {}, "Posting", picker));
  }
  form.append(el("label", {}, "Title", title), el("label", {}, "Employer", employer));
  if (p.selection && p.page_description) {
    form.append(el("div", {class: "row"},
      el("button", {id: "use-page", onclick: () => {
        text.value = p.page_description;
        previewState.source = "edited";
      }}, "Use the page's description"),
      el("button", {id: "use-selection", onclick: () => {
        text.value = p.selection;
        previewState.source = "selection";
      }}, "Use my selection")));
  }
  form.append(el("label", {}, "Description (check it; remove anything that is not the posting)", text),
    el("p", {class: "muted"}, "Or select the posting's text on the page, right-click it and " +
      "choose Send to jobhunter."),
    el("div", {class: "row"},
      el("button", {class: "primary", id: "add", onclick: () => add(title.value, employer.value,
        text.value)}, forceNew ? "Add as new" : "Add")));
  return form;
}

function fillPreview({text, title, employer, source}) {
  if (!document.getElementById("preview") && current && current.response) {
    main.append(previewForm(current.response, false));
  }
  const t = document.getElementById("pv-description");
  if (t && text) {
    t.value = text;
  }
  const ti = document.getElementById("pv-title");
  if (ti && title && !ti.value) {
    ti.value = title;
  }
  const em = document.getElementById("pv-employer");
  if (em && employer && !em.value) {
    em.value = employer;
  }
  if (previewState) {
    previewState.source = source;
  }
}

async function add(title, employer, description) {
  if (!current || !current.facts) {
    showError("Capture again first.");
    return;
  }
  const body = {
    action_id: aid(),
    facts: current.facts,
    posting_index: previewState && previewState.index !== undefined ? previewState.index : null,
    title, employer, description,
    source: (previewState && previewState.source) || "edited",
    force_new: Boolean(previewState && previewState.forceNew),
  };
  const res = await send({type: "add", key: current.key, body});
  if (res.status === 200) {
    current.response = res.data;
    clear();
    renderResponse(res.data);
  } else {
    showError((res.data && res.data.error) || res.message || "Add failed");
  }
}

async function describeGroup(g) {
  const p = current && current.response && current.response.preview;
  const body = {
    action_id: aid(),
    facts: current.facts,
    posting_index: p ? p.posting_index : null,
    description: p ? p.description : "",
    job_id: g.job_id || null,
  };
  const res = await send({type: "describe", group: g.group_id, key: current.key, body});
  if (res.status === 200) {
    clear();
    setStatus("Description added", "ok");
    main.append(cardBox(res.data.group), groupActions(res.data.group), el("div", {id: "score-box"}));
  } else {
    showError((res.data && res.data.error) || res.message || "Could not add the description");
  }
}

async function link(body) {
  body.action_id = aid();
  const res = await send({type: "link", body});
  if (res.status === 200) {
    clear();
    setStatus(res.data.outcome === "linked_groups" ? "Linked" : "Not linked", "ok");
    main.append(el("p", {id: "link-result"}, res.data.message));
    if (res.data.group_id) {
      main.append(el("button", {onclick: () => openConsole("/job/" + res.data.group_id)}, "Open"));
    }
  } else if (res.status === 409) {
    showError((res.data && res.data.error) || "Scoring in progress; link when it finishes");
  } else if (res.status === 404) {
    showError("One of these postings was removed or merged; capture again.");
  } else {
    showError((res.data && res.data.error) || res.message || "Link failed");
  }
}

// ── Score it, status, Prepare packet ────────────────────────────────────────

function scoreBox() {
  let box = document.getElementById("score-box");
  if (!box) {
    box = el("div", {id: "score-box"});
    main.append(box);
  }
  box.replaceChildren();
  return box;
}

async function follow(g, res) {
  // A merged-away group: follow it to where its posting lives now.
  if (res && res.status === 404 && res.data && res.data.gone) {
    if (res.data.current_group) {
      g.group_id = res.data.current_group;
      g.path = "/job/" + res.data.current_group;
      return true;
    }
    showError("This posting was removed or merged away.");
  }
  return false;
}

async function scoreIt(g) {
  const box = scoreBox();
  box.append(el("p", {class: "muted"}, "Getting the estimate…"));
  let res = await send({type: "estimate", group: g.group_id, job: g.job_id});
  if (await follow(g, res)) {
    res = await send({type: "estimate", group: g.group_id, job: g.job_id});
  }
  if (res.status !== 200) {
    box.replaceChildren(el("p", {class: "err"}, (res.data && res.data.error) || res.message || "No estimate"));
    return;
  }
  showEstimate(g, res.data);
}

function showEstimate(g, est, note) {
  const box = scoreBox();
  if (note) {
    box.append(el("p", {class: "warn"}, note));
  }
  box.append(el("section", {class: "card", id: "estimate"},
    el("p", {}, "One Stage 2 screen with " + est.scorer + ", estimated $" +
      est.estimated_usd.toFixed(4) + (est.cost_source === "default" ? " (assumed)" : " (measured)") +
      ", charged to your scoring caps (remaining $" + Math.max(est.remaining_usd, 0).toFixed(2) + ")."),
    est.notice ? el("p", {class: "muted", id: "notice-line"}, est.notice) : null,
    el("p", {class: "muted", id: "prefilter-line"}, est.prefilter_line),
    est.refusal ? el("p", {class: "err", id: "refusal"}, est.refusal) : null,
    el("button", {class: "primary", id: "confirm-score", disabled: Boolean(est.refusal),
      onclick: () => confirmScore(g, est)}, est.confirm_label)));
}

async function confirmScore(g, est) {
  const body = {action_id: aid(), token: est.token, job_id: g.job_id || null};
  const res = await send({type: "score", group: g.group_id, body});
  if (res.status === 202) {
    showStatus(g, {state: "in_progress", message: "Scoring"});
    poll(g);
  } else if (res.status === 409 && res.data && res.data.estimate) {
    showEstimate(g, res.data.estimate, "The estimate changed; check it and confirm again.");
  } else {
    scoreBox().append(el("p", {class: "err"}, (res.data && res.data.error) || res.message || "Not scored"));
  }
}

function showStatus(g, st) {
  const box = scoreBox();
  const text = st.state === "scored"
    ? "Scored: " + (st.bucket_name || st.bucket) + (st.verdict ? " · " + st.verdict : "")
    : st.message;
  box.append(el("p", {id: "score-status", class: st.state === "error" || st.state === "stopped"
    ? "err" : st.state === "scored" ? "ok" : "muted"}, text));
  if (st.state === "stopped" || st.state === "error") {
    box.append(el("button", {onclick: () => scoreIt(g)}, "Retry"));
  }
}

async function refreshStatus(g) {
  if (g.scored) {
    return;
  }
  let res = await send({type: "status", group: g.group_id, job: g.job_id});
  if (await follow(g, res)) {
    res = await send({type: "status", group: g.group_id, job: g.job_id});
  }
  if (res.status !== 200) {
    return;
  }
  if (res.data.state !== "not_scored") {
    showStatus(g, res.data);
  }
  if (res.data.state === "in_progress" || res.data.state === "submitted") {
    poll(g);
  }
}

function poll(g) {
  if (pollTimer) {
    clearInterval(pollTimer);
  }
  pollTimer = setInterval(async () => {
    const res = await send({type: "status", group: g.group_id, job: g.job_id});
    if (res.status !== 200) {
      return;
    }
    showStatus(g, res.data);
    if (!["in_progress"].includes(res.data.state)) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  }, 2000);
}

async function prepare(g) {
  let res = await send({type: "prepare", group: g.group_id, job: g.job_id});
  if (await follow(g, res)) {
    res = await send({type: "prepare", group: g.group_id, job: g.job_id});
  }
  if (res.status === 200) {
    await openConsole(res.data.path);
  } else {
    showError((res.data && res.data.error) || res.message || "Could not prepare a packet");
  }
}

function showError(text) {
  const old = document.getElementById("error");
  if (old) {
    old.remove();
  }
  main.prepend(el("p", {class: "err", id: "error", role: "alert"}, text));
}

document.getElementById("again").addEventListener("click", () => start("again"));
document.getElementById("options").addEventListener("click", () => chrome.runtime.openOptionsPage());
start("open");
