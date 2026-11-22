// "Your data": what is in the connected database, explained for people who have never seen it.

import { api } from "../api.js";
import { button, callout, dataTable, disclosure, pill, setBusy, tiles } from "../components.js";
import { clear, formatNumber, h, humanize, icon, plural, svg } from "../dom.js";

let cache = null;

export function invalidate() {
  cache = null;
}

async function loadSchema() {
  if (!cache) cache = await api("/api/schema");
  return cache;
}

export async function render(main, ctx, selected) {
  const page = h("div", { class: "page" });
  page.append(h("div", { class: "page-head" },
    h("h1", { tabindex: "-1", text: "Your data" }),
    h("p", { text: "These are the tables QueryBridge can read. Pick a table to see what each column means, with real example values." })));
  const body = h("div", null, h("div", { class: "skeleton", style: { height: "90px" } }));
  page.append(body);
  main.append(page);

  let schema;
  try {
    schema = await loadSchema();
  } catch (e) {
    clear(body).append(callout("bad", "alert", h("p", { text: e.message }),
      h("p", null, h("a", { href: "#/settings/database", text: "Check the database connection in Settings" }))));
    return;
  }
  clear(body);
  const t = schema.totals;
  body.append(h("p", { class: "muted small" }, `${schema.label} · ${schema.dialect}`,
    schema.dictionary_name ? ` · descriptions from ${schema.dictionary_name}` : ""));
  body.append(tiles([["Tables", formatNumber(t.tables)], ["Columns", formatNumber(t.columns)],
    ["Rows of data", formatNumber(t.rows)], ["Links between tables", formatNumber(t.relationships)]]));
  for (const warning of schema.warnings) body.append(callout("warn", "alert", h("p", { text: warning })));
  if (!schema.tables.length) {
    body.append(callout("warn", "inbox", h("p", { text: "This database has no tables that QueryBridge can read." })));
    return;
  }

  const current = schema.tables.find((tb) => tb.name === selected) || schema.tables[0];
  const listEl = h("ul", { class: "table-list", aria: { label: "Tables" } });
  const detail = h("div", { class: "card" });
  const diagramHolder = h("div");

  function choose(name, { focus = false } = {}) {
    const table = schema.tables.find((tb) => tb.name === name);
    if (!table) return;
    history.replaceState(null, "", `#/data/${encodeURIComponent(name)}`);
    for (const item of listEl.querySelectorAll(".table-item")) {
      item.setAttribute("aria-current", item.dataset.name === name ? "true" : "false");
    }
    drawDetail(table);
    drawDiagram(name);
    if (focus) detail.querySelector("h2").focus();
  }

  for (const table of schema.tables) {
    listEl.append(h("li", null, h("button", {
      class: "table-item", type: "button", dataset: { name: table.name },
      aria: { current: table.name === current.name ? "true" : "false" },
      on: { click: () => choose(table.name, { focus: window.innerWidth < 1024 }) },
    },
    h("span", { class: "table-item-name" }, h("span", { text: table.name }),
      h("span", { class: "muted small nowrap", text: table.row_count !== null ? plural(table.row_count, "row") : "" })),
    table.description ? h("span", { class: "table-item-desc", text: table.description }) : null)));
  }

  function linkSentence(link, table) {
    const go = (name) => h("a", { href: `#/data/${encodeURIComponent(name)}`, text: name,
      on: { click: (event) => { event.preventDefault(); choose(name, { focus: true }); } } });
    const through = link.columns.join(", ");
    if (link.direction === "out" && link.table.toLowerCase() === table.name.toLowerCase()) {
      return h("span", null, `Each row can point to another row of this same table (through ${through}).`);
    }
    if (link.direction === "out") {
      return h("span", null, "Each row points to a row in ", go(link.table), ` (through ${through}).`);
    }
    if (link.table.toLowerCase() === table.name.toLowerCase()) return null;
    return h("span", null, "Rows in ", go(link.table), ` point here (through ${link.table}.${through}).`);
  }

  function drawDetail(table) {
    clear(detail);
    const preview = h("div");
    const previewButton = button("Show 10 example rows", { iconName: "table", size: "sm" });
    previewButton.addEventListener("click", async () => {
      setBusy(previewButton, true, "Loading…");
      try {
        const data = await api(`/api/schema/tables/${encodeURIComponent(table.name)}/preview`);
        clear(preview).append(h("h3", { style: { marginTop: "20px" }, text: "Example rows" }),
          dataTable({ columns: data.columns, rows: data.rows, compact: true, caption: `Example rows from ${table.name}` }));
        previewButton.remove();
      } catch (e) {
        clear(preview).append(callout("bad", "alert", h("p", { text: e.message })));
        setBusy(previewButton, false);
      }
    });
    const friendly = humanize(table.name).toLowerCase();
    detail.append(
      h("div", { class: "card-head" },
        h("div", null,
          h("h2", { tabindex: "-1", text: table.name + (table.is_view ? " (view)" : "") }),
          table.description ? h("p", { class: "muted", style: { margin: "4px 0 0" }, text: table.description }) : null),
        h("div", { class: "btn-row" }, previewButton,
          button("Ask about this table", { iconName: "chat", size: "sm", kind: "primary",
            onClick: () => ctx.askWith(`Show me 10 ${friendly}`) }))),
      h("div", { class: "pills" },
        table.row_count !== null ? pill(plural(table.row_count, "row"), "neutral", "table") : null,
        pill(plural(table.columns.length, "column"), "neutral"),
        table.synonyms.length ? pill(`Also called: ${table.synonyms.slice(0, 4).join(", ")}`, "info") : null));

    const rows = table.columns.map((c) => h("tr", null,
      h("td", null, h("strong", { class: "wrap-anywhere", text: c.name }),
        h("div", { class: "muted small", title: `Database type: ${c.type}`, text: c.friendly_type }),
        (c.primary_key || c.foreign_key) ? h("div", { class: "pills", style: { marginTop: "4px" } },
          c.primary_key ? pill("ID", "info", "key", "Primary key: identifies each row") : null,
          c.foreign_key ? pill(`links to ${c.foreign_key}`, "neutral", "link", "Foreign key") : null) : null),
      h("td", { text: c.description || "" }),
      h("td", { class: "example-values" },
        c.examples.length ? c.examples.slice(0, 6).join(", ") + (c.examples.length > 6 ? ", …" : "") : (c.value_note || ""))));
    detail.append(h("h3", { style: { marginTop: "18px" }, text: "Columns" }),
      h("div", { class: "table-wrap" }, h("table", { class: "plain" },
        h("caption", { class: "visually-hidden", text: `Columns of ${table.name}` }),
        h("thead", null, h("tr", null, ["Column", "Meaning", "Example values"].map((x) => h("th", { scope: "col", text: x })))),
        h("tbody", null, rows))));
    if (table.links.length) {
      detail.append(h("h3", { style: { marginTop: "18px" }, text: "Connected tables" }),
        h("ul", { class: "links-list" }, table.links.map((link) => {
          const sentence = linkSentence(link, table);
          return sentence ? h("li", null, icon("link"), sentence) : null;
        })));
    }
    detail.append(preview);
  }

  function drawDiagram(active) {
    clear(diagramHolder);
    if (schema.tables.length > 24 || !schema.relationships.length) return;
    diagramHolder.append(disclosure("Map of how the tables connect", relationshipDiagram(schema, active, (name) => choose(name, { focus: true })),
      { open: window.innerWidth >= 1024, iconName: "link" }));
  }

  body.append(h("div", { class: "section" }, diagramHolder),
    h("div", { class: "data-layout section" }, h("nav", { aria: { label: "Tables" } }, listEl), detail));
  drawDetail(current);
  drawDiagram(current.name);
}

/** Tables as boxes on a circle, foreign keys as arrows (built with DOM APIs, no HTML strings). */
function relationshipDiagram(schema, active, onSelect) {
  const tables = schema.tables;
  const W = 140;
  const H = 46;
  const degree = {};
  for (const rel of schema.relationships) {
    if (rel.from === rel.to) continue;
    for (const name of [rel.from, rel.to]) degree[name.toLowerCase()] = (degree[name.toLowerCase()] || 0) + 1;
  }
  const byDegree = [...tables].sort((a, b) => (degree[b.name.toLowerCase()] || 0) - (degree[a.name.toLowerCase()] || 0));
  const hub = tables.length >= 4 ? byDegree[0] : null;
  const ring = orderRing(tables.filter((t) => t !== hub), schema.relationships, hub);
  const n = ring.length;
  const radiusX = Math.max(190, n * 34);
  const radiusY = Math.max(120, n * 20);
  const cx = radiusX + W / 2 + 20;
  const cy = radiusY + H / 2 + 20;
  const pos = {};
  if (hub) pos[hub.name.toLowerCase()] = { x: cx, y: cy };
  ring.forEach((t, i) => {
    const angle = (2 * Math.PI * i) / n - Math.PI / 2;
    pos[t.name.toLowerCase()] = { x: cx + radiusX * Math.cos(angle), y: cy + radiusY * Math.sin(angle) };
  });
  const width = cx * 2;
  const height = cy * 2;
  const root = svg("svg", { viewBox: `0 0 ${width} ${height}`, class: "diagram", role: "group",
    "aria-label": "Diagram of the tables and the links between them" });
  const markerId = `arrow-${Math.random().toString(36).slice(2, 8)}`;
  root.append(svg("defs", {}, svg("marker", { id: markerId, viewBox: "0 0 10 10", refX: 9, refY: 5, markerWidth: 7, markerHeight: 7, orient: "auto-start-reverse" },
    svg("path", { d: "M0 0L10 5L0 10z" }))));

  const clip = (from, to) => {
    const dx = to.x - from.x;
    const dy = to.y - from.y;
    const t = Math.min(dx ? (W / 2 + 4) / Math.abs(dx) : Infinity, dy ? (H / 2 + 4) / Math.abs(dy) : Infinity);
    return { x: to.x - dx * t, y: to.y - dy * t };
  };
  for (const rel of schema.relationships) {
    const a = pos[rel.from.toLowerCase()];
    const b = pos[rel.to.toLowerCase()];
    if (!a || !b || a === b) continue;
    const start = clip(b, a);
    const end = clip(a, b);
    const isActive = [rel.from, rel.to].map((x) => x.toLowerCase()).includes(active.toLowerCase());
    const line = svg("line", { x1: start.x.toFixed(1), y1: start.y.toFixed(1), x2: end.x.toFixed(1), y2: end.y.toFixed(1),
      class: `edge${isActive ? " active" : ""}`, "marker-end": `url(#${markerId})` });
    line.append(svg("title", { text: `${rel.from} → ${rel.to} (${rel.columns.join(", ")})` }));
    root.append(line);
  }
  for (const t of tables) {
    const p = pos[t.name.toLowerCase()];
    const label = t.name.length > 18 ? `${t.name.slice(0, 17)}…` : t.name;
    const group = svg("g", { class: `node${t.name === active ? " active" : ""}`, tabindex: 0, role: "button",
      "aria-label": `${t.name}${t.row_count !== null ? `, ${t.row_count} rows` : ""}`,
      transform: `translate(${(p.x - W / 2).toFixed(1)} ${(p.y - H / 2).toFixed(1)})`,
      on: {
        click: () => onSelect(t.name),
        keydown: (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); onSelect(t.name); } },
      } },
    svg("title", { text: t.name }),
    svg("rect", { width: W, height: H, rx: 8 }),
    svg("text", { x: W / 2, y: 20, "text-anchor": "middle", text: label }),
    svg("text", { x: W / 2, y: 36, "text-anchor": "middle", class: "sub", text: t.row_count !== null ? `${formatNumber(t.row_count)} rows` : "" }));
    root.append(group);
  }
  return h("div", null, root,
    h("p", { class: "muted small", text: "Arrows point from a table to the table it refers to. Click a box to open that table." }));
}

/** Order the outer ring so tables that link to each other sit next to each other (fewer crossing lines). */
function orderRing(tables, relationships, hub) {
  if (tables.length < 3) return tables;
  const key = (name) => name.toLowerCase();
  const links = new Map(tables.map((t) => [key(t.name), new Set()]));
  for (const rel of relationships) {
    const a = key(rel.from);
    const b = key(rel.to);
    if (a !== b && links.has(a) && links.has(b)) { links.get(a).add(b); links.get(b).add(a); }
  }
  const hubLinked = (t) => hub && relationships.some((r) => [key(r.from), key(r.to)].includes(key(t.name))
    && [key(r.from), key(r.to)].includes(key(hub.name)));
  const remaining = [...tables].sort((a, b) => links.get(key(b.name)).size - links.get(key(a.name)).size
    || Number(hubLinked(b)) - Number(hubLinked(a)));
  const order = [remaining.shift()];
  while (remaining.length) {
    const last = key(order[order.length - 1].name);
    let index = remaining.findIndex((t) => links.get(last).has(key(t.name)));
    if (index < 0) index = 0;
    order.push(remaining.splice(index, 1)[0]);
  }
  return order;
}
