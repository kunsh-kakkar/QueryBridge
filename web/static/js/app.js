// QueryBridge single-page app: routing, theme, welcome guide and the status panel.

import { api } from "./api.js";
import { callout } from "./components.js";
import { clear, h, hydrateIcons, icon, plural } from "./dom.js";
import * as ask from "./views/ask.js";
import * as data from "./views/data.js";
import * as explain from "./views/explain.js";
import * as historyView from "./views/history.js";
import * as settings from "./views/settings.js";

const ROUTES = {
  ask: { title: "Ask a question", view: ask },
  data: { title: "Your data", view: data },
  history: { title: "History", view: historyView },
  explain: { title: "Explain SQL", view: explain },
  settings: { title: "Settings", view: settings },
};

const main = document.getElementById("main");
const sidebar = document.querySelector(".sidebar");
const menuToggle = document.querySelector(".menu-toggle");
let prefillQuestion = null;
let pendingSql = null;
let renderToken = 0;

const ctx = {
  status: null,
  navigate(hash) {
    if (window.location.hash === hash) route();
    else window.location.hash = hash;
  },
  takePrefill() {
    const value = prefillQuestion;
    prefillQuestion = null;
    return value;
  },
  takeSql() {
    const value = pendingSql;
    pendingSql = null;
    return value;
  },
  lastSql: () => ask.lastSql(),
  askWith(question) {
    prefillQuestion = question;
    ctx.navigate("#/ask");
  },
  explainSql(sql) {
    pendingSql = sql;
    ctx.navigate("#/explain");
  },
  async refreshStatus() {
    try {
      ctx.status = await api("/api/status");
    } catch (e) {
      ctx.status = null;
      drawStatus(e.message);
      return null;
    }
    drawStatus();
    return ctx.status;
  },
  onDatabaseChanged() {
    data.invalidate();
    ask.resetConversation();
    ctx.refreshStatus();
  },
};

// ------------------------------------------------------------------ sidebar status
function drawStatus(error) {
  const box = document.getElementById("status-box");
  clear(box);
  const line = (tone, iconName, ...content) => h("p", { class: `status-line ${tone}` }, icon(iconName), h("span", null, ...content));
  if (error) {
    box.append(line("bad", "alert", "Not connected to QueryBridge. ", h("a", { href: "#/ask", text: "Retry", on: { click: () => ctx.refreshStatus() } })));
    return;
  }
  const s = ctx.status;
  if (s.database) {
    box.append(line("good", "database", h("strong", { text: s.database.label }), h("br"),
      `${plural(s.database.tables, "table")} · read-only`));
  } else {
    box.append(line("bad", "database", "No database. ", h("a", { href: "#/settings/database", text: "Connect one" })));
  }
  box.append(s.llm.ready
    ? line("good", "sparkle", "AI ready")
    : line("warn", "key", "API key needed. ", h("a", { href: "#/settings/answers", text: "How to add it" })));
  if (s.config && s.config.name !== "C") box.append(line("warn", "sliders", `Configuration ${s.config.name} (not the full pipeline)`));
}

// ------------------------------------------------------------------ theme
const THEMES = ["auto", "light", "dark"];
const THEME_LABELS = { auto: "Theme: automatic", light: "Theme: light", dark: "Theme: dark" };
const THEME_ICONS = { auto: "contrast", light: "sun", dark: "moon" };

function currentTheme() {
  try { return localStorage.getItem("qb.theme") || "auto"; } catch (e) { return "auto"; }
}

function applyTheme(choice) {
  if (choice === "auto") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = choice;
  const buttonEl = document.getElementById("theme-button");
  clear(buttonEl).append(icon(THEME_ICONS[choice]), h("span", { class: "theme-label", text: THEME_LABELS[choice] }));
  buttonEl.title = "Switch between automatic, light and dark";
  document.dispatchEvent(new CustomEvent("qb:themechange"));
}

document.getElementById("theme-button").addEventListener("click", () => {
  const next = THEMES[(THEMES.indexOf(currentTheme()) + 1) % THEMES.length];
  try { localStorage.setItem("qb.theme", next); } catch (e) { /* not saved */ }
  applyTheme(next);
});
window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
  if (currentTheme() === "auto") document.dispatchEvent(new CustomEvent("qb:themechange"));
});

// ------------------------------------------------------------------ welcome guide
const welcome = document.getElementById("welcome-dialog");
function openWelcome() {
  welcome.returnValue = "";
  welcome.showModal();
}
welcome.addEventListener("close", () => {
  try { localStorage.setItem("qb.welcomed", "1"); } catch (e) { /* not saved */ }
  if (welcome.returnValue === "data") ctx.navigate("#/data");
  else if (welcome.returnValue === "ask") {
    ctx.navigate("#/ask");
    setTimeout(() => { const box = main.querySelector("textarea"); if (box) box.focus(); }, 50);
  }
});
document.getElementById("help-button").addEventListener("click", openWelcome);

// ------------------------------------------------------------------ mobile menu
menuToggle.addEventListener("click", () => {
  const open = !sidebar.classList.contains("open");
  sidebar.classList.toggle("open", open);
  menuToggle.setAttribute("aria-expanded", open ? "true" : "false");
});
function closeMenu() {
  sidebar.classList.remove("open");
  menuToggle.setAttribute("aria-expanded", "false");
}
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && sidebar.classList.contains("open")) { closeMenu(); menuToggle.focus(); }
});

// ------------------------------------------------------------------ router
function parseHash() {
  const parts = window.location.hash.replace(/^#\/?/, "").split("/").filter(Boolean);
  const name = ROUTES[parts[0]] ? parts[0] : "ask";
  let param = null;
  try { param = parts[1] ? decodeURIComponent(parts.slice(1).join("/")) : null; } catch (e) { param = null; }
  return { name, param };
}

let firstRoute = true;
async function route() {
  const { name, param } = parseHash();
  const token = ++renderToken;
  closeMenu();
  for (const link of document.querySelectorAll(".nav a")) {
    if (link.dataset.route === name) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  document.title = `${ROUTES[name].title} · QueryBridge`;
  clear(main);
  try {
    await ROUTES[name].view.render(main, ctx, param);
  } catch (e) {
    if (token !== renderToken) return;
    clear(main).append(h("div", { class: "page" }, callout("bad", "alert", h("p", { text: `This page could not be shown: ${e.message}` }))));
  }
  if (token !== renderToken) return;
  if (!firstRoute) {
    const heading = main.querySelector("h1");
    (heading || main).focus({ preventScroll: true });
    window.scrollTo({ top: 0 });
  }
  firstRoute = false;
}
window.addEventListener("hashchange", route);

// ------------------------------------------------------------------ start
hydrateIcons();
applyTheme(currentTheme());
(async () => {
  await ctx.refreshStatus();
  if (!ctx.status) {
    clear(main).append(h("div", { class: "page" }, callout("bad", "alert",
      h("p", { text: "QueryBridge is not responding. Check that the app is still running, then reload this page." }))));
    return;
  }
  await route();
  let welcomed = false;
  try { welcomed = localStorage.getItem("qb.welcomed") === "1"; } catch (e) { welcomed = true; }
  if (!welcomed) openWelcome();
})();
