// After an Apply link opens the employer's page in a new tab, the "Did you apply?" banner in
// this tab is stale: it was rendered at page load. When the user comes back, fetch only the
// banner and swap it into its slot. The rest of the page is left alone, so typed text survives.
(function () {
  "use strict";

  var slot = document.getElementById("did-apply-slot");
  if (!slot) return;
  var gid = slot.dataset.gid;
  var opened = false;
  var busy = false;

  document.addEventListener("click", function (ev) {
    var a = ev.target.closest && ev.target.closest('a[href^="/apply/"]');
    if (a) opened = true;
  }, true);
  document.addEventListener("jh:apply-opened", function () { opened = true; });

  function refresh() {
    if (!opened || busy || document.visibilityState === "hidden") return;
    busy = true;
    fetch("/job/" + gid + "/did-apply", { credentials: "same-origin" })
      .then(function (r) {
        if (!r.ok) throw new Error("status " + r.status);
        return r.text();
      })
      .then(function (html) {
        slot.innerHTML = html;
        if (window.htmx) window.htmx.process(slot);
        opened = false;
      })
      .catch(function () { /* keep the flag: the next return tries again */ })
      .finally(function () { busy = false; });
  }
  document.addEventListener("visibilitychange", refresh);
  window.addEventListener("focus", refresh);
})();
