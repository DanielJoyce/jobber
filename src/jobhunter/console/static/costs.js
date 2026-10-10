// /costs: a button with data-confirm asks first (the paid-usage Clear, as on the packet page).
(function () {
  "use strict";
  document.addEventListener("click", function (ev) {
    var btn = ev.target.closest("button[data-confirm]");
    if (btn && !window.confirm(btn.getAttribute("data-confirm"))) ev.preventDefault();
  });
})();
