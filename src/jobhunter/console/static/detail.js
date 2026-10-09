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
      if (link) window.location.href = link.getAttribute("href");
    },
    b: function () {
      window.location.href = "/inbox";
    },
  });
})();
