// Packet page (specs/017 phase 1b): confirm before a paid run, copy buttons.
(function () {
  "use strict";

  var root = document.querySelector(".packet[data-packet]");
  if (!root) return;

  // A button that spends money (an API run, or a CLI run on paid extra usage) asks first.
  // data-paid marks the paid-extra-usage CLI run, which the server refuses without
  // confirm_paid=1. Every other submit clears it, so a stale value never rides along.
  root.addEventListener("click", function (ev) {
    var btn = ev.target.closest("button[type=submit]");
    if (!btn || !btn.form) return;
    var msg = btn.getAttribute("data-confirm");
    if (msg && !window.confirm(msg)) {
      ev.preventDefault();
      return;
    }
    var paid = btn.form.elements.namedItem("confirm_paid");
    if (paid) paid.value = btn.getAttribute("data-paid") === "1" ? "1" : "";
  });

  root.addEventListener("click", function (ev) {
    var btn = ev.target.closest("button.copy-btn");
    if (!btn) return;
    var text = btn.getAttribute("data-copy") || "";
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function () {
        btn.textContent = "Copied";
      });
    }
  });
})();
