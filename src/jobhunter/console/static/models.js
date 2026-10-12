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
