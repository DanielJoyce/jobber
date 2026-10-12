// Options: pairing code and console port. The worker does the request and keeps the token.

const status = document.getElementById("pair-status");
const port = document.getElementById("port");
const code = document.getElementById("code");

function show(text, cls) {
  status.textContent = text;
  status.className = cls || "muted";
}

async function refresh() {
  const st = await chrome.runtime.sendMessage({type: "pairStatus"});
  port.value = String(st.port || 8808);
  show(st.paired ? "Paired with the console on port " + st.port + "." : "Not paired yet.",
    st.paired ? "ok" : "warn");
}

document.getElementById("pair-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  show("Pairing…");
  const res = await chrome.runtime.sendMessage({
    type: "pair",
    code: code.value.trim(),
    port: Number(port.value) || 8808,
  });
  code.value = "";
  if (res && res.paired) {
    show("Paired with the console on port " + (Number(port.value) || 8808) + ".", "ok");
  } else {
    show("Not paired: " + ((res && (res.message || res.state)) || "no answer"), "err");
  }
});

document.getElementById("unpair").addEventListener("click", async () => {
  await chrome.runtime.sendMessage({type: "unpair"});
  show("Unpaired. Run jobhunter ext unpair to revoke the token on the console too.", "warn");
});

// ── per-ATS switches: Show buttons and Record submits ───────────────────────

const SITES = {greenhouse: "Greenhouse", lever: "Lever", ashby: "Ashby", workable: "Workable"};

function checkbox(ats, key, checked) {
  const cell = document.createElement("td");
  const box = document.createElement("input");
  box.type = "checkbox";
  box.checked = checked;
  box.id = "sw-" + ats + "-" + key;
  box.setAttribute("aria-label", SITES[ats] + " " + key.replace("_", " "));
  box.addEventListener("change", async () => {
    await chrome.runtime.sendMessage({type: "switches/set", ats, [key]: box.checked});
  });
  cell.appendChild(box);
  return cell;
}

async function showSwitches() {
  const got = await chrome.runtime.sendMessage({type: "switches/get"});
  const body = document.querySelector("#switches tbody");
  body.replaceChildren();
  for (const [ats, label] of Object.entries(SITES)) {
    const sw = (got && got.switches && got.switches[ats]) || {};
    const row = document.createElement("tr");
    const name = document.createElement("th");
    name.scope = "row";
    name.textContent = label;
    row.appendChild(name);
    row.appendChild(checkbox(ats, "show_buttons", sw.show_buttons !== false));
    row.appendChild(checkbox(ats, "record_submits", sw.record_submits !== false));
    body.appendChild(row);
  }
}

refresh();
showSwitches();
