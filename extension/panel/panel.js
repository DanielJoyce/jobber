// The side panel shell (specs/022 "Opening the panel"): bound to one tab by ?tab=<id>, or by the
// active tab of its window when opened without one. It shows what the worker knows about that
// tab's ATS frames. Every confirm and write of later tickets lives in this extension page.

const bound = document.getElementById("bound");
const detail = document.getElementById("detail");

async function tabId() {
  const given = Number(new URLSearchParams(location.search).get("tab"));
  if (Number.isInteger(given) && given > 0) {
    return given;
  }
  const [active] = await chrome.tabs.query({active: true, currentWindow: true});
  return active ? active.id : null;
}

async function render(id) {
  const st = await chrome.runtime.sendMessage({type: "panel/state", tab: id});
  if (!st || st.state !== "ok") {
    bound.textContent = "Could not read this tab.";
    return;
  }
  bound.textContent = st.ats ? "Bound to this " + st.ats + " tab." : "Bound to this tab.";
  document.getElementById("site").textContent = st.host || "unknown";
  document.getElementById("field").textContent = st.field || "none chosen";
  detail.hidden = false;
}

(async () => {
  const id = await tabId();
  if (id === null) {
    bound.textContent = "No tab to bind to.";
    return;
  }
  await render(id);
  // A click on another jobhunter button while the panel is open names a new field.
  chrome.storage.onChanged.addListener((changes, area) => {
    if (area === "session" && changes.panelField) {
      render(id);
    }
  });
  chrome.tabs.onRemoved.addListener((gone) => {
    if (gone === id) {
      bound.textContent = "That tab was closed.";
      detail.hidden = true;
    }
  });
})();
