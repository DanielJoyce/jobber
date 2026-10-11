// After an Apply link opens the employer's page in a new tab, the job or packet tab still shows
// the old "Did you apply?" state. When the user comes back to this tab, reload it once so the
// server renders the banner again. The flag lives only in this page, so the reload clears it.
(function () {
  "use strict";

  var opened = false;

  document.addEventListener("click", function (ev) {
    var a = ev.target.closest && ev.target.closest('a[href^="/apply/"]');
    if (a) opened = true;
  }, true);
  document.addEventListener("jh:apply-opened", function () { opened = true; });

  function back() {
    if (!opened || document.visibilityState === "hidden") return;
    opened = false;
    window.location.reload();
  }
  document.addEventListener("visibilitychange", back);
  window.addEventListener("focus", back);
})();
