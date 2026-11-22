// Applies the saved light/dark choice before the page paints (avoids a flash of the wrong theme).
(function () {
  var choice = "auto";
  try { choice = localStorage.getItem("qb.theme") || "auto"; } catch (e) { /* storage blocked */ }
  if (choice === "light" || choice === "dark") {
    document.documentElement.setAttribute("data-theme", choice);
  }
})();
