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
