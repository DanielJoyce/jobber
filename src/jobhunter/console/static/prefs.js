// /prefs: show weight sliders normalized to 1.0 as they move (the server normalizes on save).
(function () {
  "use strict";

  function updateWeights() {
    var sliders = document.querySelectorAll("input[data-weight]");
    var total = 0;
    sliders.forEach(function (s) { total += Number(s.value) || 0; });
    sliders.forEach(function (s) {
      var key = s.name.replace(/^w\./, "");
      var out = document.querySelector('[data-weight-out="' + key + '"]');
      if (!out) return;
      var share = total > 0 ? (Number(s.value) || 0) / total : 0;
      out.textContent = share.toFixed(2);
    });
  }

  document.addEventListener("input", function (ev) {
    if (ev.target && ev.target.matches && ev.target.matches("input[data-weight]")) updateWeights();
  });
  document.addEventListener("DOMContentLoaded", updateWeights);
})();
