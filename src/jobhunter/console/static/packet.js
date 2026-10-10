// Packet page (specs/017 phase 1b): confirm before a paid run, one run at a time, copy buttons,
// and no silent loss of unsaved edits.
(function () {
  "use strict";

  var root = document.querySelector(".packet[data-packet]");
  if (!root) return;

  // Edits typed into an editor and not saved yet.
  root.addEventListener("input", function (ev) {
    var form = ev.target.closest("form.editor");
    if (form) form.dataset.dirty = "1";
  });

  root.addEventListener("click", function (ev) {
    var btn = ev.target.closest("button[type=submit]");
    if (!btn || !btn.form) return;
    // Confirm and Restore post their own forms; they would leave unsaved edits behind.
    if (btn.getAttribute("data-needs-clean") === "1") {
      var editor = btn.closest("form.editor");
      if (editor && editor.dataset.dirty === "1" && !window.confirm(
        "You have unsaved edits in this document; they will be lost. Save them first " +
        "(Cancel), or go on without them (OK)?")) {
        ev.preventDefault();
        return;
      }
    }
    // A button that spends money (an API run, or a CLI run on paid extra usage) asks first.
    var msg = btn.getAttribute("data-confirm");
    if (msg && !window.confirm(msg)) {
      ev.preventDefault();
      return;
    }
    // data-paid marks the paid-extra-usage CLI run, which the server refuses without
    // confirm_paid=1. Every other submit clears it, so a stale value never rides along.
    var paid = btn.form.elements.namedItem("confirm_paid");
    if (paid) paid.value = btn.getAttribute("data-paid") === "1" ? "1" : "";
  });

  // A run takes up to three minutes: one click, one run. Buttons are disabled once the form
  // is on its way (after the clicked button's own name/value is in the submission).
  root.addEventListener("submit", function (ev) {
    var form = ev.target;
    if (!form.matches("form.gen-form, form.inline")) return;
    var runner = ev.submitter && ev.submitter.getAttribute("data-runner");
    if (!runner && !(ev.submitter && ev.submitter.name === "runner")) return;
    if (form.dataset.busy === "1") {
      ev.preventDefault();
      return;
    }
    form.dataset.busy = "1";
    window.setTimeout(function () {
      form.querySelectorAll("button[type=submit]").forEach(function (b) { b.disabled = true; });
      var note = document.createElement("p");
      note.className = "muted busy-note";
      note.setAttribute("role", "status");
      note.textContent = "Running. This can take up to three minutes; keep this tab open.";
      form.appendChild(note);
    }, 0);
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
