// /models (specs/020). A refused save answers 409 (models.toml changed elsewhere) or 422 (an
// invalid value) with the row and its message; htmx 2 does not swap 4xx by default, so let
// these through for /models routes only.
document.addEventListener("htmx:beforeSwap", function (ev) {
  var xhr = ev.detail && ev.detail.xhr;
  if (!xhr || [409, 422].indexOf(xhr.status) === -1) return;
  if (!/\/models(\/|$)/.test(new URL(xhr.responseURL || "", location.href).pathname)) return;
  ev.detail.shouldSwap = true;
  ev.detail.isError = false;
});

// Save + re-run (specs/020 M5). The buttons live in the preview region, which a preview in
// flight may replace before a queued save goes out (and with it the clicked button's own
// name/value). So each click records its intent in the row form's hidden rerun field, which
// is outside that region and is read when the save is sent.
document.addEventListener(
  "click",
  function (ev) {
    var btn = ev.target && ev.target.closest && ev.target.closest("button[data-rerun]");
    if (!btn || !btn.form) return;
    var field = btn.form.querySelector("input.mo-rerun-intent");
    if (field) field.value = btn.getAttribute("data-rerun") === "1" ? "1" : "";
  },
  true
);
