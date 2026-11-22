// Reusable interface pieces. All untrusted text is inserted with h(...)/textContent (see dom.js).

import { api } from "./api.js";
import {
  clear, debounce, formatCell, formatCost, formatDuration, formatNumber, h, humanize, icon, isDarkTheme,
  plural, uniqueId,
} from "./dom.js";

// ------------------------------------------------------------------ small pieces
const STATUS_ICONS = {
  success: "checkCircle", empty: "inbox", clarification: "help", refused: "ban", out_of_scope: "search",
  blocked: "shield", failed: "alert", error: "alert",
};

export function pill(text, tone = "neutral", iconName = null, title = null) {
  return h("span", { class: `pill tone-${tone}`, title }, iconName ? icon(iconName) : null, text);
}

export function statusPill(status, text, tone) {
  return pill(text, tone, STATUS_ICONS[status] || "info");
}

export function callout(tone, iconName, ...children) {
  return h("div", { class: `callout callout-${tone}` }, icon(iconName), h("div", { class: "callout-body" }, ...children));
}

export function list(items, className) {
  return h("ul", { class: className }, items.map((item) => h("li", null, item)));
}

export function emptyState({ iconName = "inbox", title, text, action }) {
  return h("div", { class: "empty" },
    h("span", { class: "how-icon" }, icon(iconName)),
    h("h3", { text: title }),
    text ? h("p", { text }) : null,
    action || null);
}

export function tiles(items) {
  return h("div", { class: "tiles" }, items.map(([label, value]) =>
    h("div", { class: "tile" }, h("p", { class: "tile-label", text: label }), h("p", { class: "tile-value", text: value }))));
}

/** Show a spinner in a button made by button() while work is in progress. */
export function setBusy(el, busy, busyText = "Working…") {
  if (busy) {
    el.disabled = true;
    clear(el).append(h("span", { class: "spinner", aria: { hidden: "true" } }), h("span", { text: busyText }));
  } else {
    el.disabled = false;
    el.restore();
  }
}

/** A button whose content can be restored after a busy state. */
export function button(label, { kind = "secondary", iconName, size, onClick, type = "button", title, disabled } = {}) {
  const el = h("button", {
    class: ["btn", `btn-${kind}`, size ? `btn-${size}` : null], type, title, disabled,
    on: onClick ? { click: onClick } : undefined,
  });
  el.restore = () => { clear(el).append(...(iconName ? [icon(iconName)] : []), h("span", { text: label })); };
  el.restore();
  return el;
}

export function codeBlock(text, { copyLabel = "Copy" } = {}) {
  const pre = h("pre", null, h("code", { text }));
  const copy = button(copyLabel, { size: "sm", iconName: "copy", kind: "secondary" });
  copy.classList.add("copy-btn");
  copy.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(text);
      toast("Copied to the clipboard.");
    } catch (e) {
      const range = document.createRange();
      range.selectNodeContents(pre);
      const selection = window.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
      toast("Press Ctrl+C (or ⌘C) to copy the selected text.");
    }
  });
  return h("div", { class: "code-block" }, pre, copy);
}

export function jsonBlock(value) {
  return h("pre", { class: "json", text: JSON.stringify(value, null, 2) });
}

export function disclosure(summaryText, body, { open = false, iconName } = {}) {
  const details = h("details", { class: "disclosure" },
    h("summary", null, iconName ? icon(iconName) : null, h("span", { text: summaryText }), icon("chevronDown", { className: "chev" })),
    h("div", { class: "disclosure-body" }, body));
  if (open) details.open = true;
  return details;
}

// ------------------------------------------------------------------ toasts and dialogs
export function toast(message, tone = "neutral") {
  const holder = document.getElementById("toasts");
  if (!holder) return;
  const el = h("div", { class: `toast ${tone === "bad" ? "bad" : ""}` },
    icon(tone === "bad" ? "alert" : "checkCircle"), h("span", { text: message }));
  holder.append(el);
  setTimeout(() => el.remove(), tone === "bad" ? 7000 : 4000);
}

export function announce(message) {
  const el = document.getElementById("announcer");
  if (!el) return;
  el.textContent = "";
  setTimeout(() => { el.textContent = message; }, 50);
}

export function confirmDialog({ title, text, ok = "Delete" }) {
  const dialog = document.getElementById("confirm-dialog");
  dialog.querySelector("#confirm-title").textContent = title;
  dialog.querySelector("#confirm-text").textContent = text;
  dialog.querySelector("#confirm-ok").textContent = ok;
  dialog.returnValue = "";
  dialog.showModal();
  return new Promise((resolve) => {
    dialog.addEventListener("close", () => resolve(dialog.returnValue === "ok"), { once: true });
  });
}

// ------------------------------------------------------------------ tabs
/** tabs([{id, label, iconName, render(panel)}]) with lazy panels and arrow-key navigation. */
export function tabs(defs, { label = "Answer views" } = {}) {
  const list_ = h("div", { class: "tabs", role: "tablist", aria: { label } });
  const wrap = h("div", { class: "tabset" }, list_);
  const buttons = [];
  const panels = [];
  defs.forEach((def, index) => {
    const tabId = uniqueId("tab");
    const panelId = uniqueId("panel");
    const tab = h("button", {
      class: "tab", type: "button", role: "tab", id: tabId, tabindex: index === 0 ? "0" : "-1",
      aria: { selected: index === 0 ? "true" : "false", controls: panelId },
    }, def.iconName ? icon(def.iconName) : null, h("span", { text: def.label }));
    const panel = h("div", { class: "tab-panel", role: "tabpanel", id: panelId, tabindex: "0", aria: { labelledby: tabId } });
    panel.hidden = index !== 0;
    tab.addEventListener("click", () => select(index));
    tab.addEventListener("keydown", (event) => {
      const keys = { ArrowRight: index + 1, ArrowLeft: index - 1, Home: 0, End: defs.length - 1 };
      if (event.key in keys) {
        event.preventDefault();
        const next = (keys[event.key] + defs.length) % defs.length;
        select(next);
        buttons[next].focus();
      }
    });
    buttons.push(tab);
    panels.push(panel);
    list_.append(tab);
    wrap.append(panel);
  });
  const rendered = new Set();
  function select(index) {
    buttons.forEach((tab, i) => {
      tab.setAttribute("aria-selected", i === index ? "true" : "false");
      tab.tabIndex = i === index ? 0 : -1;
      panels[i].hidden = i !== index;
    });
    if (!rendered.has(index)) {
      rendered.add(index);
      defs[index].render(panels[index]);
    }
  }
  if (defs.length) select(0);
  wrap.select = select;
  return wrap;
}

// ------------------------------------------------------------------ data table
function csvCell(value) {
  if (value === null || value === undefined) return "";
  let text = String(value);
  // Spreadsheet formula injection: a text cell starting with = + - @ (or tab/CR) is prefixed with '
  if (typeof value === "string" && /^[=+\-@\t\r]/.test(text)) text = `'${text}`;
  return /[",\n\r]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
}

export function downloadCsv(columns, rows, filename = "querybridge_result.csv") {
  const lines = [columns.map((c) => csvCell(c.name)).join(",")];
  for (const row of rows) lines.push(row.map(csvCell).join(","));
  const blob = new Blob(["﻿" + lines.join("\r\n")], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = h("a", { href: url, download: filename });
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

const NUMERIC_KINDS = new Set(["numeric"]);

/** Sortable, filterable, paginated table with CSV download. */
export function dataTable({ columns, rows, pageSize = 25, filename, caption, compact = false }) {
  let filtered = rows;
  let sortIndex = -1;
  let sortDir = 1;
  let page = 0;

  const filterId = uniqueId("filter");
  const filter = h("input", { class: "input", type: "search", id: filterId, placeholder: "Filter rows…", autocomplete: "off" });
  const count = h("span", { class: "muted small", aria: { live: "polite" } });
  const tools = compact ? null : h("div", { class: "table-tools" },
    h("div", null, h("label", { class: "visually-hidden", for: filterId, text: "Filter the rows" }), filter),
    button("Download CSV", { iconName: "download", size: "sm", onClick: () => downloadCsv(columns, sortedRows(), filename) }));

  const table = h("table", { class: "data" });
  if (caption) table.append(h("caption", { class: "visually-hidden", text: caption }));
  const headRow = h("tr");
  const headers = columns.map((col, index) => {
    const sortBtn = h("button", { class: "sort-btn", type: "button", title: `Sort by ${col.name}` },
      h("span", { text: col.name }), icon("sort"));
    const th = h("th", { scope: "col", class: NUMERIC_KINDS.has(col.kind) ? "num" : null }, sortBtn);
    sortBtn.addEventListener("click", () => {
      if (sortIndex === index) sortDir = -sortDir; else { sortIndex = index; sortDir = 1; }
      page = 0;
      draw();
    });
    headRow.append(th);
    return th;
  });
  table.append(h("thead", null, headRow));
  const body = h("tbody");
  table.append(body);

  const pageInfo = h("span");
  const prev = button("Previous", { size: "sm", iconName: "chevronLeft", onClick: () => { page -= 1; draw(); } });
  const next = button("Next", { size: "sm", iconName: "chevronRight", onClick: () => { page += 1; draw(); } });
  const foot = h("div", { class: "table-foot" }, count, h("div", { class: "pager" }, prev, pageInfo, next));

  function compare(a, b) {
    if (a === null || a === undefined) return 1;
    if (b === null || b === undefined) return -1;
    if (typeof a === "number" && typeof b === "number") return (a - b) * sortDir;
    return String(a).localeCompare(String(b), undefined, { numeric: true, sensitivity: "base" }) * sortDir;
  }
  function sortedRows() {
    if (sortIndex < 0) return filtered;
    return [...filtered].sort((x, y) => compare(x[sortIndex], y[sortIndex]));
  }
  function draw() {
    const data = sortedRows();
    const pages = Math.max(1, Math.ceil(data.length / pageSize));
    page = Math.min(Math.max(page, 0), pages - 1);
    clear(body);
    for (const row of data.slice(page * pageSize, page * pageSize + pageSize)) {
      body.append(h("tr", null, row.map((value, i) => {
        const text = formatCell(value, columns[i].kind);
        return text === null
          ? h("td", { class: "null", text: "empty" })
          : h("td", { class: NUMERIC_KINDS.has(columns[i].kind) ? "num" : null, text });
      })));
    }
    headers.forEach((th, i) => {
      th.setAttribute("aria-sort", i === sortIndex ? (sortDir > 0 ? "ascending" : "descending") : "none");
      const btnIcon = th.querySelector(".icon");
      btnIcon.replaceWith(icon(i === sortIndex ? (sortDir > 0 ? "sortAsc" : "sortDesc") : "sort"));
    });
    const start = data.length ? page * pageSize + 1 : 0;
    const end = Math.min(data.length, (page + 1) * pageSize);
    count.textContent = `Showing ${formatNumber(start)}–${formatNumber(end)} of ${plural(data.length, "row")}` +
      (filtered.length !== rows.length ? ` (filtered from ${formatNumber(rows.length)})` : "");
    pageInfo.textContent = `Page ${page + 1} of ${pages}`;
    prev.disabled = page === 0;
    next.disabled = page >= pages - 1;
    foot.querySelector(".pager").hidden = pages <= 1;
  }
  filter.addEventListener("input", debounce(() => {
    const query = filter.value.trim().toLowerCase();
    filtered = !query ? rows : rows.filter((row) => row.some((v) => v !== null && String(v).toLowerCase().includes(query)));
    page = 0;
    draw();
  }, 150));
  draw();
  return h("div", { class: "data-table" }, tools, h("div", { class: "table-wrap" }, table), foot);
}

// ------------------------------------------------------------------ charts (Plotly, loaded on first use)
let plotlyPromise = null;
export function loadPlotly() {
  if (window.Plotly) return Promise.resolve(window.Plotly);
  if (!plotlyPromise) {
    plotlyPromise = new Promise((resolve, reject) => {
      const script = h("script", { src: "/vendor/plotly.min.js" });
      script.addEventListener("load", () => resolve(window.Plotly));
      script.addEventListener("error", () => { plotlyPromise = null; reject(new Error("chart library failed to load")); });
      document.head.append(script);
    });
  }
  return plotlyPromise;
}

const CHART_NAMES = { bar: "Bar chart", line: "Line chart", area: "Area chart", scatter: "Scatter plot", histogram: "Histogram" };

export async function drawFigure(box, figure) {
  const Plotly = await loadPlotly();
  const layout = { ...figure.layout, autosize: true };
  delete layout.width;
  await Plotly.react(box, figure.data, layout, {
    displaylogo: false, responsive: true, modeBarButtonsToRemove: ["lasso2d", "select2d", "autoScale2d"],
  });
}

function onThemeChange(element, handler) {
  const listener = () => {
    if (!element.isConnected) { document.removeEventListener("qb:themechange", listener); return; }
    handler();
  };
  document.addEventListener("qb:themechange", listener);
}

export function chartPanel(result) {
  const charts = result.charts;
  let chartIndex = 0;
  let measure = charts[0].y;
  const box = h("div", { class: "chart-box", role: "img", aria: { label: "Chart of the result" } });
  const caption = h("p", { class: "chart-caption" });
  const tools = h("div", { class: "chart-tools" });
  const status = h("div");

  async function draw() {
    clear(status);
    const spec = charts[chartIndex];
    const params = new URLSearchParams({ index: String(chartIndex), dark: String(isDarkTheme()) });
    if (measure) params.set("measure", measure);
    try {
      const chart = await api(`/api/results/${encodeURIComponent(result.id)}/chart?${params}`);
      clear(caption).append(h("strong", { text: chart.title }), h("span", { class: "muted", text: ` · ${chart.reason}` }));
      box.setAttribute("aria-label", `${CHART_NAMES[spec.type] || "Chart"}: ${chart.title}. The same data is in the Table tab.`);
      await drawFigure(box, chart.figure);
    } catch (e) {
      status.append(callout("bad", "alert", h("p", { text: `The chart could not be drawn: ${e.message}` }),
        button("Try again", { size: "sm", iconName: "refresh", onClick: draw })));
    }
  }

  function buildTools() {
    clear(tools);
    if (charts.length > 1) {
      const id = uniqueId("chart-type");
      const select = h("select", { class: "select", id }, charts.map((c, i) =>
        h("option", { value: String(i), text: CHART_NAMES[c.type] || humanize(c.type), selected: i === chartIndex })));
      select.addEventListener("change", () => { chartIndex = Number(select.value); measure = charts[chartIndex].y; buildTools(); draw(); });
      tools.append(h("div", { class: "field" }, h("label", { class: "field-label", for: id, text: "Chart type" }), select));
    }
    const spec = charts[chartIndex];
    if (spec.measures.length > 1 && spec.type !== "scatter") {
      const id = uniqueId("measure");
      const select = h("select", { class: "select", id }, spec.measures.map((m) =>
        h("option", { value: m, text: humanize(m), selected: m === measure })));
      select.addEventListener("change", () => { measure = select.value; draw(); });
      tools.append(h("div", { class: "field" }, h("label", { class: "field-label", for: id, text: "Show" }), select));
    }
  }
  buildTools();
  const wrap = h("div", null, tools, caption, status, box,
    h("p", { class: "muted small", text: "Hover over the chart to see exact values. The full data is in the Table tab." }));
  wrap.redraw = draw;
  draw();
  onThemeChange(wrap, () => { if (!wrap.closest("[hidden]")) draw(); else wrap.dataset.stale = "1"; });
  return wrap;
}

// ------------------------------------------------------------------ explanation ("How I got this")
function explainItem(iconName, title, content) {
  if (!content || (Array.isArray(content) && !content.length)) return null;
  return h("div", { class: "explain-item" },
    h("h4", null, icon(iconName), title),
    Array.isArray(content) ? list(content) : h("p", { text: content }));
}

const CONFIDENCE_TEXT = {
  High: "QueryBridge is confident this query matches your question: it passed the automatic checks.",
  Medium: "Probably right, but worth a quick look at the assumptions below.",
  Low: "Please check the SQL and the assumptions before relying on this answer.",
};

export function explanationPanel(result) {
  const e = result.explanation;
  const wrap = h("div");
  if (!e) {
    wrap.append(h("p", { class: "muted", text: "There is no explanation for this answer." }));
    return wrap;
  }
  wrap.append(h("p", { class: "lead", text: e.summary }));
  if (result.confidence_label) {
    const tone = { High: "good", Medium: "warn", Low: "bad" }[result.confidence_label];
    wrap.append(callout(tone === "good" ? "good" : tone === "warn" ? "warn" : "bad", "gauge",
      h("p", null, h("strong", { text: `Confidence: ${result.confidence_label} (${Math.round(result.confidence * 100)}%). ` }),
        CONFIDENCE_TEXT[result.confidence_label])));
  }
  const grid = h("div", { class: "explain-grid" },
    explainItem("table", "Tables it looked at", e.tables_accessed),
    explainItem("search", "Filters applied", e.conditions),
    explainItem("chart", "Calculations", e.calculations),
    explainItem("link", "How tables were combined", e.join_rationale),
    explainItem("layers", "How rows were grouped", e.aggregation_rationale),
    explainItem("sortDesc", "Sorting and limits", e.ordering_and_limits),
    explainItem("info", "Assumptions made", result.assumptions));
  wrap.append(grid);
  if (result.complexity) {
    wrap.append(h("p", { class: "muted small" },
      `This is a ${result.complexity.level.toLowerCase()} query`,
      result.complexity.reasons.length ? ` (${result.complexity.reasons.join(", ")}).` : "."));
  }
  return wrap;
}

// ------------------------------------------------------------------ SQL
export function sqlPanel(result, { onExplain } = {}) {
  const wrap = h("div");
  if (!result.sql) {
    wrap.append(h("p", { class: "muted", text: "No SQL was run for this question." }));
    return wrap;
  }
  const views = [[result.dialect, result.sql], ...Object.entries(result.translations || {})];
  const holder = h("div");
  const note = h("p", { class: "muted small" });
  const show = (index) => {
    clear(holder).append(codeBlock(views[index][1]));
    const cap = result.row_limit_added && result.table
      ? ` QueryBridge added the LIMIT as a safety cap, so at most ${formatNumber(result.table.max_rows)} rows are shown.` : "";
    note.textContent = index === 0
      ? (result.ok ? `This is exactly the ${result.dialect} query that was checked and run.${cap}`
        : "This query was not run.")
      : `Translated to ${views[index][0]} automatically, for reference only. Only the ${result.dialect} version was run.`;
    seg.querySelectorAll("button").forEach((b, i) => b.setAttribute("aria-pressed", i === index ? "true" : "false"));
  };
  const seg = h("div", { class: "segmented", role: "group", aria: { label: "Show the query as" } },
    views.map(([name], i) => h("button", { type: "button", text: name, on: { click: () => show(i) } })));
  wrap.append(h("p", { class: "small muted", text: "SQL is the language databases understand. You do not need to read it, but you can copy it." }));
  if (views.length > 1) wrap.append(h("div", { class: "btn-row", style: { marginBottom: "10px" } }, seg));
  wrap.append(holder, note);
  show(0);
  if (result.generated_sql) {
    wrap.append(disclosure("The AI's original version (before QueryBridge's safety rewrite)", codeBlock(result.generated_sql)));
  }
  if (onExplain) {
    wrap.append(h("div", { class: "btn-row", style: { marginTop: "12px" } },
      button("Explain this SQL step by step", { iconName: "book", size: "sm", onClick: () => onExplain(result.generated_sql || result.executed_sql || result.sql) })));
  }
  return wrap;
}

// ------------------------------------------------------------------ behind the scenes
const STEP_TONES = { ok: "good", warning: "warn", failed: "bad", skipped: "neutral" };

function plainTable(headers, rows, numeric = []) {
  return h("div", { class: "table-wrap" }, h("table", { class: "plain" },
    h("thead", null, h("tr", null, headers.map((t, i) => h("th", { scope: "col", class: numeric.includes(i) ? "num" : null, text: t })))),
    h("tbody", null, rows.map((row) => h("tr", null, row.map((cell, i) =>
      h("td", { class: numeric.includes(i) ? "num" : null }, cell === null || cell === undefined ? "" : cell)))))));
}

export function promptView(prompt) {
  const wrap = h("div");
  wrap.append(h("p", { class: "small muted" },
    `${prompt.stage} prompt · configuration ${prompt.config_name} · about ${formatNumber(prompt.token_estimate)} tokens` +
    (prompt.schema_mode ? ` · ${prompt.schema_mode} schema` : "")));
  wrap.append(plainTable(["Section", "Where", "Technique", "≈ tokens"],
    prompt.sections.map((s) => [s.title + (s.cache_breakpoint ? " (cached)" : ""), s.location, s.technique, formatNumber(s.tokens)]), [3]));
  if (prompt.examples.length) {
    wrap.append(h("h4", { style: { marginTop: "14px" }, text: "Worked examples chosen (few-shot)" }),
      plainTable(["Example", "Question (practice schema)", "Why chosen"],
        prompt.examples.map((x) => [x.id, x.question, x.reason])));
  }
  const sections = h("div", { style: { marginTop: "14px" } });
  for (const s of prompt.sections) {
    sections.append(disclosure(`${s.location === "system" ? "System" : "User"} · ${s.title}`,
      h("div", { class: "prompt-text" }, h("p", { class: "small muted", text: s.technique }), codeBlock(s.text))));
  }
  wrap.append(sections);
  return wrap;
}

export function detailsPanel(resultId) {
  const wrap = h("div", null, h("div", { class: "skeleton", style: { height: "120px" } }));
  api(`/api/results/${encodeURIComponent(resultId)}/details`).then((d) => {
    clear(wrap);
    wrap.append(h("p", { class: "small muted", text: "Everything QueryBridge did to produce this answer, for the curious. Nothing here is needed to use the answer." }));
    wrap.append(h("h4", { text: `Steps (${formatDuration(d.total_ms)} in total)` }),
      plainTable(["Step", "Result", "Time", "Detail"],
        d.steps.map((s) => [s.name, pill(s.status, STEP_TONES[s.status] || "neutral"), formatDuration(s.duration_ms), s.detail]), [2]));
    if (d.llm_calls.length) {
      const priced = d.llm_calls.filter((c) => c.estimated_cost_usd !== null && c.estimated_cost_usd !== undefined);
      const cost = priced.reduce((sum, c) => sum + c.estimated_cost_usd, 0);
      const costText = priced.length ? `, about ${formatCost(cost)}` : "";
      wrap.append(h("h4", { style: { marginTop: "18px" }, text: `AI calls (${d.llm_calls.length}${costText})` }),
        plainTable(["Stage", "Model", "Input tokens", "Cached", "Output tokens", "Time"],
          d.llm_calls.map((c) => [c.stage, c.model + (c.fallback_used ? " (fallback)" : ""), formatNumber(c.input_tokens),
            formatNumber(c.cache_read_tokens), formatNumber(c.output_tokens), formatDuration(c.latency_ms)]), [2, 3, 4, 5]));
    } else {
      wrap.append(h("p", { class: "muted small", style: { marginTop: "12px" }, text: "No AI model was used for this answer." }));
    }
    if (d.verification) {
      const v = d.verification;
      const checks = h("ul", { class: "links-list" }, v.checks.map((c) => h("li", null,
        icon(c.passed ? "checkCircle" : "alert", { label: c.passed ? "passed" : c.severity }),
        h("span", null, h("strong", { text: `${humanize(c.name)}: ` }), c.message))));
      wrap.append(h("h4", { style: { marginTop: "18px" }, text: "Automatic checks" }), checks);
      if (v.semantic) {
        wrap.append(callout(v.semantic.blocking ? "warn" : "good", "sparkle",
          h("p", null, h("strong", { text: "AI reviewer: " }),
            `${v.semantic.blocking ? "found problems" : "no blocking problems"} (${Math.round(v.semantic.confidence * 100)}% sure). `,
            v.semantic.summary),
          v.semantic.issues.length ? list(v.semantic.issues.map((i) => `${i.severity} · ${i.category}: ${i.description}`)) : null));
      } else if (v.semantic_skipped_reason) {
        wrap.append(h("p", { class: "muted small", text: `AI reviewer skipped: ${v.semantic_skipped_reason}` }));
      }
      if (v.explain_plan.length) {
        wrap.append(disclosure("Database query plan (EXPLAIN QUERY PLAN)", h("pre", { class: "json", text: v.explain_plan.join("\n") })));
      }
    }
    if (d.attempts.length > 1) {
      wrap.append(h("h4", { style: { marginTop: "18px" }, text: "Attempts (automatic corrections)" }),
        plainTable(["#", "Source", "Outcome", "Problem"],
          d.attempts.map((a) => [String(a.number), a.source, a.outcome.replace(/_/g, " "), a.problems[0] || ""])));
    }
    if (d.schema_context) {
      wrap.append(h("p", { class: "small muted", style: { marginTop: "12px" } },
        `Schema sent to the AI: ${d.schema_context.mode} (${d.schema_context.tables.join(", ")}).`));
    }
    if (d.prompts.length) {
      const prompts = h("div", { style: { marginTop: "18px" } }, h("h4", { text: "Prompts sent to the AI model" }));
      d.prompts.forEach((p, i) => prompts.append(disclosure(`${i + 1}. ${humanize(p.stage)} prompt (≈ ${formatNumber(p.token_estimate)} tokens)`, promptView(p))));
      wrap.append(prompts);
    }
  }).catch((e) => {
    clear(wrap).append(callout("bad", "alert", h("p", { text: e.message })));
  });
  return wrap;
}

// ------------------------------------------------------------------ the answer card
export function answerCard(result, { showQuestion = true, onClarify, onExplain, headingLevel = "h3" } = {}) {
  const card = h("article", { class: "card answer fade-in", aria: { label: `Answer: ${result.question}` } });
  const top = h("div", { class: "answer-top" });
  const pills = h("div", { class: "pills" }, statusPill(result.status, result.status_text, result.tone));
  if (result.is_follow_up) pills.append(pill("Follow-up", "info", "followUp", "Used the previous answer as context"));
  if (result.corrections) pills.append(pill(`Fixed automatically ${result.corrections}×`, "warn", "refresh",
    "The first query had a problem, so QueryBridge corrected it"));
  if (result.config_name === "direct") pills.append(pill("Your SQL, no AI", "neutral", "code"));
  top.append(pills);
  if (showQuestion) top.append(h(headingLevel, { class: "answer-question", text: result.question }));
  if (result.message && !result.clarification) top.append(h("p", { class: "answer-message", text: result.message }));

  if (result.kpi) {
    top.append(h("div", { class: "kpi" },
      h("p", { class: "kpi-label", text: result.kpi.label }),
      h("p", { class: "kpi-value", text: formatCell(result.kpi.value, result.kpi.kind) ?? "empty" })));
  }
  if (result.ok && result.table) {
    const meta = h("div", { class: "answer-meta" },
      h("span", null, icon("table"), plural(result.table.row_count, "row") + (result.table.truncated ? ` (only the first ${formatNumber(result.table.max_rows)} shown)` : "")),
      h("span", null, icon("history"), `ran in ${formatDuration(result.table.execution_time_ms)}`));
    if (result.confidence_label) meta.append(h("span", { title: CONFIDENCE_TEXT[result.confidence_label] }, icon("gauge"), `confidence ${result.confidence_label.toLowerCase()}`));
    if (result.llm_calls) meta.append(h("span", null, icon("sparkle"), `${plural(result.llm_calls, "AI call")} · ${formatDuration(result.total_ms)} total`));
    top.append(meta);
  }
  if (result.warnings.length) {
    top.append(callout("warn", "alert", h("p", null, h("strong", { text: "Please note" })), list(result.warnings)));
  }
  if (!result.ok && !result.clarification && result.status !== "refused" && result.status !== "out_of_scope") {
    const help = result.status === "blocked"
      ? "The query was stopped before it ran, so nothing in the database was touched."
      : "Try asking in a different way, or mention the table or column you mean (see Your data).";
    top.append(callout(result.tone === "bad" ? "bad" : "warn", "info", h("p", { text: help }),
      result.problems.length ? disclosure("Technical details", list(result.problems)) : null));
  }
  if (result.clarification) top.append(clarifyBlock(result, onClarify));
  card.append(top);

  const defs = [];
  const hasRows = result.table && result.table.row_count > 0;
  if (hasRows) {
    defs.push({ id: "table", label: "Table", iconName: "table", render: (p) => p.append(
      dataTable({ columns: result.table.columns, rows: result.table.rows, caption: result.question })) });
    if (result.charts.length) defs.push({ id: "chart", label: "Chart", iconName: "chart", render: (p) => p.append(chartPanel(result)) });
  }
  if (result.explanation && result.ok) defs.push({ id: "how", label: "How I got this", iconName: "book", render: (p) => p.append(explanationPanel(result)) });
  if (result.sql) defs.push({ id: "sql", label: "SQL", iconName: "code", render: (p) => p.append(sqlPanel(result, { onExplain })) });
  defs.push({ id: "details", label: "Behind the scenes", iconName: "layers", render: (p) => p.append(detailsPanel(result.id)) });
  const tabset = tabs(defs);
  card.append(tabset);
  // a chart drawn while its tab was hidden is redrawn when shown after a theme change
  tabset.addEventListener("click", () => {
    const stale = card.querySelector(".tab-panel:not([hidden]) [data-stale='1']");
    if (stale && stale.redraw) { delete stale.dataset.stale; stale.redraw(); }
  });
  return card;
}

function clarifyBlock(result, onClarify) {
  const c = result.clarification;
  const block = h("div", { class: "clarify" });
  block.append(callout("info", "help",
    h("p", null, h("strong", { text: "A quick question before I answer: " }), c.question),
    h("p", { class: "small", text: "Choose what you meant, or type it in your own words." })));
  const disableAll = () => block.querySelectorAll("button, input").forEach((el) => { el.disabled = true; });
  if (c.options.length) {
    block.append(h("div", { class: "chips", style: { marginTop: "12px" } }, c.options.map((option) =>
      h("button", { class: "chip", type: "button", on: { click: () => { disableAll(); onClarify && onClarify(option); } } },
        icon("arrowRight"), h("span", { text: option })))));
  }
  const inputId = uniqueId("clarify");
  const input = h("input", { class: "input", id: inputId, type: "text", maxlength: "500", placeholder: "Or describe what you meant…", autocomplete: "off" });
  const form = h("form", { class: "clarify-form" },
    h("label", { class: "visually-hidden", for: inputId, text: "Your answer" }), input,
    button("Send", { kind: "primary", iconName: "send", type: "submit" }));
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const answer = input.value.trim();
    if (!answer) { input.focus(); return; }
    disableAll();
    onClarify && onClarify(answer);
  });
  block.append(form);
  if (!onClarify) disableAll();
  return block;
}

// ------------------------------------------------------------------ API key help
// Numbered steps for adding the Gemini API key (`llm` is /api/status's `llm` object).
export function apiKeySteps(llm, { copyEnv = false } = {}) {
  const variable = (llm && llm.key_variable) || "GEMINI_API_KEY";
  return h("ol", null,
    h("li", null, "Create a free API key in Google AI Studio at ", h("strong", { text: "aistudio.google.com" }),
      " (sign in with a Google account)."),
    copyEnv ? h("li", null, "In the QueryBridge folder, copy ", h("code", { text: ".env.example" }), " to ",
      h("code", { text: ".env" }), " if you have not already.") : null,
    h("li", null, "Open the file ", h("code", { text: ".env" }), " in the QueryBridge folder and set ",
      h("code", { text: `${variable}=` }), " followed by your key."),
    h("li", null, "Stop the app (Ctrl+C in its terminal window) and start it again."));
}

// ------------------------------------------------------------------ progress while answering
const FRIENDLY_STEPS = [
  ["Checking your question…", "Reading your question"],
  ["Selecting the relevant schema…", "Finding the right tables"],
  ["Generating SQL with AI…", "Writing the database query with AI"],
  ["Validating, verifying and running the query…", "Checking it is safe and correct, then running it"],
  ["Formatting results…", "Preparing your answer"],
];

export function progressCard(question) {
  const items = FRIENDLY_STEPS.map(([, text]) => h("li", { dataset: { state: "pending" } },
    h("span", { class: "step-mark" }, h("span", { class: "step-dot" })), h("span", { text })));
  const card = h("article", { class: "card progress-card fade-in", aria: { busy: "true", label: "Working on your question" } },
    h("div", { class: "pills" }, pill("Working on it", "info", null)),
    h("h3", { class: "answer-question", text: question }),
    h("ol", null, items),
    h("p", { class: "muted small", style: { marginTop: "10px", marginBottom: "0" }, text: "This usually takes 5–30 seconds." }));
  function mark(index, state) {
    const li = items[index];
    li.dataset.state = state;
    const markEl = li.querySelector(".step-mark");
    clear(markEl).append(state === "done" ? icon("check") : state === "active"
      ? h("span", { class: "spinner", aria: { hidden: "true" } }) : h("span", { class: "step-dot" }));
  }
  let current = -1;
  return {
    el: card,
    step(label) {
      const index = FRIENDLY_STEPS.findIndex(([raw]) => raw === label);
      if (index < 0) return;
      for (let i = 0; i < index; i += 1) mark(i, "done");
      mark(index, "active");
      current = index;
    },
    get current() { return current; },
  };
}
