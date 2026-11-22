// Safe DOM helpers. Text always goes in through textContent / text nodes, never innerHTML,
// because answers, SQL and cell values come from the AI model or the database (untrusted).

const SVG_NS = "http://www.w3.org/2000/svg";
const PROPERTIES = new Set(["value", "checked", "selected", "disabled", "hidden", "multiple"]);

/**
 * h("div", {class: "card", on: {click: fn}, aria: {label: "x"}}, "text", child, [more])
 * Attributes named on* are refused (use the `on` map), and URL attributes must be same-site or https.
 */
export function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  applyAttrs(el, attrs);
  append(el, children);
  return el;
}

function safeUrl(value) {
  const text = String(value).trim();
  if (text.startsWith("#") || text.startsWith("/")) return text;
  try {
    const url = new URL(text, window.location.origin);
    if (url.origin === window.location.origin || url.protocol === "https:") return url.href;
  } catch (e) { /* fall through */ }
  return "#";
}

function applyAttrs(el, attrs) {
  if (!attrs) return;
  for (const [key, value] of Object.entries(attrs)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") el.className = Array.isArray(value) ? value.filter(Boolean).join(" ") : value;
    else if (key === "text") el.textContent = String(value);
    else if (key === "on") for (const [event, handler] of Object.entries(value)) el.addEventListener(event, handler);
    else if (key === "dataset") Object.assign(el.dataset, value);
    else if (key === "aria") for (const [name, v] of Object.entries(value)) {
      if (v !== undefined && v !== null) el.setAttribute(`aria-${name}`, String(v));
    }
    else if (key === "style" && typeof value === "object") Object.assign(el.style, value);
    else if (/^on/i.test(key)) throw new Error("Use the `on` map for event handlers");
    else if (key === "href" || key === "src" || key === "action") el.setAttribute(key, safeUrl(value));
    else if (PROPERTIES.has(key)) el[key] = value;
    else el.setAttribute(key, value === true ? "" : String(value));
  }
}

export function append(el, children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

export function clear(el) {
  while (el.firstChild) el.removeChild(el.firstChild);
  return el;
}

export function svg(tag, attrs = {}, ...children) {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === undefined || value === null) continue;
    if (key === "text") el.textContent = String(value);
    else if (key === "on") for (const [event, handler] of Object.entries(value)) el.addEventListener(event, handler);
    else if (/^on/i.test(key) || key === "href") throw new Error("Unsafe SVG attribute");
    else el.setAttribute(key, String(value));
  }
  for (const child of children.flat()) if (child) el.append(child);
  return el;
}

// ------------------------------------------------------------------ icons (24px, stroke style)
const ICONS = {
  chat: [["path", { d: "M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" }]],
  database: [["ellipse", { cx: 12, cy: 5, rx: 9, ry: 3 }], ["path", { d: "M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5" }],
    ["path", { d: "M3 12c0 1.66 4 3 9 3s9-1.34 9-3" }]],
  history: [["path", { d: "M3 12a9 9 0 1 0 2.64-6.36L3 8" }], ["path", { d: "M3 3v5h5" }], ["path", { d: "M12 7v5l3 2" }]],
  code: [["path", { d: "M16 18l6-6-6-6" }], ["path", { d: "M8 6l-6 6 6 6" }]],
  sliders: [["path", { d: "M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3M1 14h6M9 8h6M17 16h6" }]],
  help: [["circle", { cx: 12, cy: 12, r: 10 }], ["path", { d: "M9.1 9a3 3 0 0 1 5.8 1c0 2-3 3-3 3" }], ["path", { d: "M12 17h.01" }]],
  sun: [["circle", { cx: 12, cy: 12, r: 4 }],
    ["path", { d: "M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41" }]],
  moon: [["path", { d: "M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" }]],
  contrast: [["circle", { cx: 12, cy: 12, r: 9 }], ["path", { d: "M12 3a9 9 0 0 1 0 18z", fill: "currentColor" }]],
  check: [["path", { d: "M20 6L9 17l-5-5" }]],
  checkCircle: [["circle", { cx: 12, cy: 12, r: 10 }], ["path", { d: "M8 12l3 3 5-6" }]],
  x: [["path", { d: "M18 6L6 18M6 6l12 12" }]],
  alert: [["path", { d: "M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" }],
    ["path", { d: "M12 9v4" }], ["path", { d: "M12 17h.01" }]],
  info: [["circle", { cx: 12, cy: 12, r: 10 }], ["path", { d: "M12 16v-4" }], ["path", { d: "M12 8h.01" }]],
  copy: [["rect", { x: 9, y: 9, width: 13, height: 13, rx: 2 }], ["path", { d: "M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" }]],
  download: [["path", { d: "M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" }], ["path", { d: "M7 10l5 5 5-5" }], ["path", { d: "M12 15V3" }]],
  upload: [["path", { d: "M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" }], ["path", { d: "M17 8l-5-5-5 5" }], ["path", { d: "M12 3v12" }]],
  play: [["path", { d: "M7 4l13 8-13 8z" }]],
  refresh: [["path", { d: "M21 12a9 9 0 1 1-2.64-6.36L21 8" }], ["path", { d: "M21 3v5h-5" }]],
  trash: [["path", { d: "M3 6h18" }], ["path", { d: "M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" }],
    ["path", { d: "M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6" }]],
  chevronDown: [["path", { d: "M6 9l6 6 6-6" }]],
  chevronLeft: [["path", { d: "M15 18l-6-6 6-6" }]],
  chevronRight: [["path", { d: "M9 18l6-6-6-6" }]],
  arrowRight: [["path", { d: "M5 12h14" }], ["path", { d: "M12 5l7 7-7 7" }]],
  table: [["rect", { x: 3, y: 3, width: 18, height: 18, rx: 2 }], ["path", { d: "M3 9h18M3 15h18M9 3v18" }]],
  chart: [["path", { d: "M3 3v18h18" }], ["path", { d: "M8 17v-6M13 17V7M18 17v-4" }]],
  search: [["circle", { cx: 11, cy: 11, r: 7 }], ["path", { d: "M21 21l-4.35-4.35" }]],
  shield: [["path", { d: "M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" }], ["path", { d: "M9 12l2 2 4-4" }]],
  lock: [["rect", { x: 4, y: 11, width: 16, height: 10, rx: 2 }], ["path", { d: "M8 11V7a4 4 0 0 1 8 0v4" }]],
  link: [["path", { d: "M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71" }],
    ["path", { d: "M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71" }]],
  menu: [["path", { d: "M3 6h18M3 12h18M3 18h18" }]],
  plus: [["path", { d: "M12 5v14M5 12h14" }]],
  send: [["path", { d: "M22 2L11 13" }], ["path", { d: "M22 2l-7 20-4-9-9-4z" }]],
  sparkle: [["path", { d: "M12 3l1.9 5.8L20 11l-6.1 2.2L12 19l-1.9-5.8L4 11l6.1-2.2z" }]],
  key: [["circle", { cx: 7.5, cy: 15.5, r: 4.5 }], ["path", { d: "M10.7 12.3L21 2M16 7l3 3M19 4l2 2" }]],
  layers: [["path", { d: "M12 2l10 5-10 5L2 7z" }], ["path", { d: "M2 17l10 5 10-5" }], ["path", { d: "M2 12l10 5 10-5" }]],
  book: [["path", { d: "M2 4h7a3 3 0 0 1 3 3v14a2 2 0 0 0-2-2H2z" }], ["path", { d: "M22 4h-7a3 3 0 0 0-3 3v14a2 2 0 0 1 2-2h8z" }]],
  flask: [["path", { d: "M9 3h6" }], ["path", { d: "M10 3v6L4 19a2 2 0 0 0 1.7 3h12.6a2 2 0 0 0 1.7-3L14 9V3" }]],
  gauge: [["path", { d: "M12 14l4-4" }], ["path", { d: "M3.34 19a10 10 0 1 1 17.32 0" }]],
  followUp: [["path", { d: "M9 10l-5 5 5 5" }], ["path", { d: "M20 4v7a4 4 0 0 1-4 4H4" }]],
  inbox: [["path", { d: "M22 12h-6l-2 3h-4l-2-3H2" }],
    ["path", { d: "M5.45 5.11L2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z" }]],
  ban: [["circle", { cx: 12, cy: 12, r: 10 }], ["path", { d: "M4.93 4.93l14.14 14.14" }]],
  edit: [["path", { d: "M12 20h9" }], ["path", { d: "M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4z" }]],
  sort: [["path", { d: "M8 9l4-4 4 4M16 15l-4 4-4-4" }]],
  sortAsc: [["path", { d: "M12 19V5M5 12l7-7 7 7" }]],
  sortDesc: [["path", { d: "M12 5v14M19 12l-7 7-7-7" }]],
  server: [["rect", { x: 2, y: 3, width: 20, height: 8, rx: 2 }], ["rect", { x: 2, y: 13, width: 20, height: 8, rx: 2 }],
    ["path", { d: "M6 7h.01M6 17h.01" }]],
  folder: [["path", { d: "M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z" }]],
};

export function icon(name, { label, size, className } = {}) {
  const parts = ICONS[name] || ICONS.info;
  const el = svg("svg", {
    viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", "stroke-width": 2, "stroke-linecap": "round",
    "stroke-linejoin": "round", class: ["icon", className].filter(Boolean).join(" "),
    width: size, height: size, focusable: "false",
  }, ...parts.map(([tag, attrs]) => svg(tag, attrs)));
  if (label) { el.setAttribute("role", "img"); el.setAttribute("aria-label", label); }
  else el.setAttribute("aria-hidden", "true");
  return el;
}

/** Replace every <span data-icon="name"> in static HTML with its SVG. */
export function hydrateIcons(root = document) {
  for (const holder of root.querySelectorAll("[data-icon]")) {
    if (holder.firstChild) continue;
    holder.append(icon(holder.dataset.icon));
  }
}

// ------------------------------------------------------------------ formatting
const intFormat = new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 });
const decFormat = new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 });
const smallFormat = new Intl.NumberFormat(undefined, { maximumSignificantDigits: 4 });

export function formatNumber(value) {
  if (value === null || value === undefined || value === "") return "";
  const n = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(n)) return String(value);
  if (Number.isInteger(n)) return intFormat.format(n);
  if (Math.abs(n) < 1) return smallFormat.format(n);
  return decFormat.format(n);
}

/** Display text for one table cell; identifiers and years keep their digits as-is. */
export function formatCell(value, kind) {
  if (value === null || value === undefined) return null;
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (typeof value === "number") {
    if (kind === "identifier" || kind === "categorical") return String(value);
    return formatNumber(value);
  }
  return String(value);
}

export function formatDuration(ms) {
  if (ms === null || ms === undefined) return "";
  if (ms < 1) return "under 1 ms";
  if (ms < 1000) return `${Math.round(ms)} ms`;
  return `${(ms / 1000).toFixed(ms < 10000 ? 1 : 0)} s`;
}

export function formatCost(usd) {
  if (usd === null || usd === undefined) return "unknown";
  if (usd === 0) return "$0";
  return usd < 0.01 ? `$${usd.toFixed(4)}` : `$${usd.toFixed(2)}`;
}

const relative = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
export function timeAgo(iso) {
  if (!iso) return "";
  const date = new Date(/[zZ]|[+-]\d\d:?\d\d$/.test(iso) ? iso : `${iso}Z`);
  if (Number.isNaN(date.getTime())) return iso;
  const seconds = Math.round((date.getTime() - Date.now()) / 1000);
  const steps = [["year", 31536000], ["month", 2592000], ["week", 604800], ["day", 86400], ["hour", 3600], ["minute", 60]];
  for (const [unit, size] of steps) {
    if (Math.abs(seconds) >= size) return relative.format(Math.round(seconds / size), unit);
  }
  return "just now";
}

export function plural(count, word, many) {
  return `${formatNumber(count)} ${count === 1 ? word : (many || `${word}s`)}`;
}

export function humanize(name) {
  const text = String(name || "").replace(/_/g, " ").trim();
  return text.charAt(0).toUpperCase() + text.slice(1);
}

export function debounce(fn, wait = 250) {
  let timer;
  return (...args) => { clearTimeout(timer); timer = setTimeout(() => fn(...args), wait); };
}

export function isDarkTheme() {
  const theme = document.documentElement.dataset.theme;
  if (theme === "dark") return true;
  if (theme === "light") return false;
  return window.matchMedia("(prefers-color-scheme: dark)").matches;
}

let uid = 0;
export function uniqueId(prefix = "qb") {
  uid += 1;
  return `${prefix}-${uid}`;
}
