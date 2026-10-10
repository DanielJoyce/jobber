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

refresh();
