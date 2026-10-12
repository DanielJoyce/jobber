// Copy buttons on /setup. The command stays visible and selectable if clipboard access fails.
document.addEventListener("click", function (ev) {
  var btn = ev.target.closest && ev.target.closest(".su-copy");
  if (!btn) return;
  var text = btn.getAttribute("data-copy") || "";
  var done = function () {
    var old = btn.textContent;
    btn.textContent = "Copied";
    setTimeout(function () { btn.textContent = old; }, 1500);
  };
  try {
    navigator.clipboard.writeText(text).then(done, function () {});
  } catch (e) {}
});

// A refused resume upload answers 413/415/422 with the row and its error; htmx 2 does not
// swap 4xx by default, so let this one through (as prefs.js does for section saves).
document.addEventListener("htmx:beforeSwap", function (ev) {
  var xhr = ev.detail && ev.detail.xhr;
  if (!xhr || [413, 415, 422].indexOf(xhr.status) === -1) return;
  if (!/\/setup\/resume$/.test(xhr.responseURL || "")) return;
  ev.detail.shouldSwap = true;
  ev.detail.isError = false;
});
