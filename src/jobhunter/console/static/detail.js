(function () {
  "use strict";

  var root = document.querySelector(".detail[data-gid]");
  if (!root || !window.jh) return;

  window.jh.registerKeys({
    o: function () {
      var url = root.dataset.postingUrl;
      if (url) window.open(url, "_blank", "noopener,noreferrer");
    },
    A: function () {
      var link = document.getElementById("apply-link");
      if (link) {
        window.open(link.getAttribute("href"), "_blank", "noopener");
        document.dispatchEvent(new CustomEvent("jh:apply-opened"));
      }
    },
    b: function () {
      window.location.href = "/inbox";
    },
    p: function () {
      var link = document.getElementById("prepare-link");
      if (link) { window.location.href = link.getAttribute("href"); return; }
      var form = document.getElementById("prepare-form");
      if (form) form.submit();
    },
  });
})();
