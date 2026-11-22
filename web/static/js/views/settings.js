// "Settings": database connection, how answers are made (technique configuration and API key),
// the prompt lab, measured evaluation results, and the active settings.

import { api } from "../api.js";
import {
  apiKeySteps, button, callout, codeBlock, disclosure, drawFigure, jsonBlock, list, pill, promptView, setBusy, tiles, toast,
} from "../components.js";
import { clear, formatCost, formatDuration, formatNumber, h, icon, isDarkTheme, uniqueId } from "../dom.js";

const SECTIONS = [
  ["database", "Database", "database"],
  ["answers", "How answers are made", "sparkle"],
  ["lab", "Prompt lab", "flask"],
  ["evaluation", "Evaluation", "gauge"],
  ["about", "About & safety", "shield"],
];

const FRIENDLY_CONFIGS = {
  A: ["Basic", "A one-line instruction, with no description of the database. Kept as a baseline to measure how much the other techniques help."],
  B: ["With database knowledge", "Adds an expert role, safety rules, notes on the database type and a description of every relevant table."],
  D: ["With worked examples", "Everything in B plus a few worked question-and-SQL examples picked to match your question."],
  C: ["Full pipeline", "Everything in D plus automatic checks, an AI self-review of each query and up to 2 automatic corrections."],
};

export function render(main, ctx, section) {
  const active = SECTIONS.some(([id]) => id === section) ? section : "database";
  const page = h("div", { class: "page" });
  page.append(h("div", { class: "page-head" },
    h("h1", { tabindex: "-1", text: "Settings" }),
    h("p", { text: "Choose which database QueryBridge reads, how answers are produced, and look under the hood." })));
  page.append(h("nav", { class: "subnav", aria: { label: "Settings sections" } }, SECTIONS.map(([id, label, iconName]) =>
    h("a", { href: `#/settings/${id}`, aria: { current: id === active ? "page" : null } }, icon(iconName), h("span", { text: label })))));
  const body = h("div");
  page.append(body);
  main.append(page);
  ({ database, answers, lab, evaluation, about })[active](body, ctx);
}

// ====================================================================== database
async function database(body, ctx) {
  const current = h("div", { class: "card" }, h("div", { class: "skeleton", style: { height: "80px" } }));
  body.append(current);

  async function drawCurrent() {
    let info;
    try {
      info = await api("/api/connection");
    } catch (e) {
      clear(current).append(callout("bad", "alert", h("p", { text: e.message })));
      return;
    }
    clear(current);
    const db = info.database;
    if (!db) {
      current.append(h("h2", { text: "No database connected" }),
        callout("bad", "alert", h("p", { text: info.error || "Choose a database below." })));
      return;
    }
    const reset = button("Switch back to the sample database", { iconName: "refresh", size: "sm" });
    reset.addEventListener("click", async () => {
      setBusy(reset, true, "Switching…");
      try {
        const result = await api("/api/connection/reset", { method: "POST" });
        toast(`Connected to ${result.label}.`);
        ctx.onDatabaseChanged();
        drawCurrent();
      } catch (e) {
        toast(e.message, "bad");
        setBusy(reset, false);
      }
    });
    current.append(
      h("div", { class: "card-head" },
        h("div", null, h("p", { class: "muted small", style: { margin: "0" }, text: "Connected database" }), h("h2", { text: db.label })),
        db.is_demo ? null : reset),
      h("div", { class: "pills" },
        pill(db.ok ? "Connected" : "Not reachable", db.ok ? "good" : "bad", db.ok ? "checkCircle" : "alert"),
        db.read_only ? pill("Read-only: cannot change data", "info", "lock") : pill("Read-only not confirmed", "warn", "alert"),
        pill(`${db.dialect}${db.server_version ? ` ${db.server_version}` : ""}`, "neutral", "database"),
        pill(`${db.tables} tables`, "neutral", "table"),
        db.latency_ms !== null ? pill(`responds in ${formatDuration(db.latency_ms)}`, "neutral") : null,
        db.dictionary_name ? pill(`Descriptions: ${db.dictionary_name}`, "neutral", "book") : null),
      db.is_demo ? h("p", { class: "muted small", style: { marginTop: "10px", marginBottom: "0" },
        text: "This is the sample company database that comes with QueryBridge: departments, employees, products, customers, sales, projects and reviews." }) : null,
      h("p", { class: "muted small mono wrap-anywhere", style: { marginTop: "8px", marginBottom: "0" }, text: db.masked_url }));
  }
  drawCurrent();

  if (ctx.status && ctx.status.hosted) {
    body.append(h("div", { class: "card" }, h("h2", { text: "Connect a different database" }),
      callout("info", "lock", h("p", { text: "This copy of QueryBridge is hosted online and shared by everyone who opens it, "
        + "so it always uses the sample company database and connecting another database is turned off. "
        + "To ask questions about your own database, run QueryBridge on your own computer." }))));
    return;
  }

  // ---------------------------------------------------------------- connect another
  const connectCard = h("div", { class: "card" });
  body.append(connectCard);
  connectCard.append(h("h2", { text: "Connect a different database" }),
    h("p", { class: "muted", text: "QueryBridge always connects read-only. Your current conversation is cleared when you switch, because earlier answers would no longer apply." }));
  const methods = [
    ["upload", "Upload a SQLite file", "Choose a .db, .sqlite or .sqlite3 file from your computer.", "upload"],
    ["path", "Use a file on this computer", "Type the full path to a SQLite file (nothing is copied).", "folder"],
    ["server", "PostgreSQL or MySQL server", "Experimental. Use a database account that can only read.", "server"],
  ];
  let method = "upload";
  const group = uniqueId("method");
  const methodBox = h("div", { class: "connect-methods", role: "radiogroup", aria: { label: "How do you want to connect?" } });
  for (const [id, title, text, iconName] of methods) {
    const input = h("input", { type: "radio", name: group, value: id, checked: id === method });
    input.addEventListener("change", () => { method = id; drawForm(); });
    methodBox.append(h("label", { class: "choice" }, input,
      h("span", { class: "choice-title" }, icon(iconName), title), h("span", { class: "choice-text", text })));
  }
  connectCard.append(methodBox);
  const formHolder = h("div");
  connectCard.append(formHolder);

  function field(label, input, hint) {
    const id = uniqueId("f");
    input.id = id;
    return h("div", { class: "field" }, h("label", { class: "field-label", for: id, text: label }), input,
      hint ? h("p", { class: "field-hint", text: hint }) : null);
  }

  function drawForm() {
    clear(formHolder);
    const form = h("form", { class: "stack" });
    const error = h("p", { class: "form-error", role: "alert" });
    const dictionary = h("input", { class: "input file-input", type: "file", name: "dictionary", accept: ".json,application/json" });
    let fields;
    if (method === "upload") {
      fields = [field("SQLite database file", h("input", { class: "input file-input", type: "file", name: "file", accept: ".db,.sqlite,.sqlite3", required: true }),
        "Up to 100 MB. A copy is stored in QueryBridge's data folder.")];
    } else if (method === "path") {
      fields = [field("Path to the SQLite file", h("input", { class: "input mono", type: "text", name: "path", required: true, placeholder: "/Users/you/Documents/shop.db", autocomplete: "off", spellcheck: "false" }))];
    } else {
      fields = [
        h("div", { class: "field-row" },
          field("Database type", h("select", { class: "select", name: "engine" },
            h("option", { value: "postgresql", text: "PostgreSQL" }), h("option", { value: "mysql", text: "MySQL" }))),
          field("Host", h("input", { class: "input", name: "host", placeholder: "db.example.com", autocomplete: "off" })),
          field("Port", h("input", { class: "input", name: "port", type: "number", min: "0", max: "65535", placeholder: "default" }))),
        h("div", { class: "field-row" },
          field("Database name", h("input", { class: "input", name: "database", required: true, autocomplete: "off" })),
          field("Username", h("input", { class: "input", name: "username", autocomplete: "username" })),
          field("Password", h("input", { class: "input", name: "password", type: "password", autocomplete: "current-password" }))),
        h("p", { class: "field-hint", text: "Needs the optional drivers: pip install -r requirements-optional.txt. The password is kept in memory only and never shown or logged." }),
      ];
    }
    const submit = button("Connect", { kind: "primary", iconName: "link", type: "submit" });
    form.append(...fields,
      disclosure("Optional: add descriptions of your tables (data dictionary)", h("div", null,
        field("Data dictionary (JSON)", dictionary, "Descriptions and other names for your tables and columns help QueryBridge understand questions. Same format as database/data_dictionary.json."))),
      error, h("div", { class: "btn-row" }, submit));
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      error.textContent = "";
      const data = new FormData(form);
      if (method === "upload" && !(data.get("file") && data.get("file").size)) { error.textContent = "Choose a database file first."; return; }
      if (method === "path" && !String(data.get("path") || "").trim()) { error.textContent = "Type the path to the file."; return; }
      if (method === "server" && !String(data.get("database") || "").trim()) { error.textContent = "Type the database name."; return; }
      if (method === "server" && !data.get("port")) data.set("port", "0");
      if (!(data.get("dictionary") && data.get("dictionary").size)) data.delete("dictionary");
      setBusy(submit, true, "Connecting…");
      try {
        const result = await api(`/api/connection/${method}`, { method: "POST", form: data });
        toast(`Connected to ${result.label}: ${result.tables} tables found.`);
        ctx.onDatabaseChanged();
        form.reset();
        drawCurrent();
      } catch (e) {
        error.textContent = e.message;
      } finally {
        setBusy(submit, false);
      }
    });
    formHolder.append(form);
  }
  drawForm();
}

// ====================================================================== answers
async function answers(body, ctx) {
  const status = ctx.status;
  if (!status) return;
  const ai = h("div", { class: "card" });
  body.append(ai);
  ai.append(h("h2", { text: "AI model" }));
  if (status.llm.ready) {
    ai.append(callout("good", "checkCircle", h("p", null, h("strong", { text: "Ready. " }), "QueryBridge uses ", h("code", { text: status.llm.model }),
      ` through your ${status.llm.provider_name} API key.`)));
  } else {
    ai.append(callout("warn", "key", h("h3", { text: `Add your ${status.llm.provider_name} API key to ask questions` }),
      apiKeySteps(status.llm, { copyEnv: true }),
      h("p", { class: "small", text: "The key stays on your computer: it is never shown in the page or written to logs." })));
  }
  if (status.llm.provider === "gemini") {
    ai.append(h("p", { class: "small muted", text: "Gemini free tier: no charge, but each model has a daily request limit, "
      + "and a question uses up to 5 requests (usually 2). On the free tier Google may use what QueryBridge sends "
      + "(your questions and the database's table structure and example values) to improve its products." }));
  }
  const usage = status.usage;
  if (usage && usage.calls) {
    ai.append(h("h3", { style: { marginTop: "16px" }, text: "Usage since the app started" }),
      tiles([["AI calls", formatNumber(usage.calls)], ["Input tokens", formatNumber(usage.input_tokens)],
        ["Output tokens", formatNumber(usage.output_tokens)], ["Cache hits", `${Math.round(usage.cache_hit_ratio * 100)}%`],
        ["Estimated cost", formatCost(usage.estimated_cost_usd)]]));
  }

  const configCard = h("div", { class: "card" });
  body.append(configCard);
  configCard.append(h("h2", { text: "Answer quality" }),
    h("p", { class: "muted", text: "QueryBridge can use more or fewer prompt-engineering techniques. The full pipeline is the most reliable; the others exist so you can compare them (see Evaluation)." }));
  const group = uniqueId("config");
  const choices = h("div", { class: "choice-list", role: "radiogroup", aria: { label: "Technique configuration" } });
  for (const config of status.configs) {
    const [title, text] = FRIENDLY_CONFIGS[config.name] || [config.label, config.description];
    const input = h("input", { type: "radio", name: group, value: config.name, checked: config.name === status.config.name });
    input.addEventListener("change", async () => {
      try {
        const result = await api("/api/session/config", { method: "POST", body: { name: config.name } });
        status.config = result.config;
        toast(`Now using configuration ${config.name}: ${title}.`);
      } catch (e) {
        toast(e.message, "bad");
      }
    });
    choices.append(h("label", { class: "choice" }, input,
      h("span", { class: "choice-title" }, h("span", { text: `${config.name} · ${title}` }), config.name === "C" ? pill("Recommended", "good") : null),
      h("span", { class: "choice-text", text }),
      disclosure("Techniques used", list(config.techniques))));
  }
  configCard.append(choices,
    h("p", { class: "muted small", style: { marginTop: "12px", marginBottom: "0" }, text: "This choice applies to this browser only and to new questions." }));
}

// ====================================================================== prompt lab
function lab(body, ctx) {
  const status = ctx.status;
  const card = h("div", { class: "card" });
  body.append(card);
  card.append(h("h2", { text: "Prompt lab" }),
    h("p", { class: "muted", text: "See exactly what QueryBridge would send to the AI model for any question. Nothing is sent from this page, so it is free to use." }));
  const form = h("form", { class: "stack" });
  const question = h("input", { class: "input", name: "question", value: status && status.examples[0] ? status.examples[0] : "", autocomplete: "off" });
  const select = (name, options, value) => h("select", { class: "select", name },
    options.map(([v, t]) => h("option", { value: v, text: t, selected: v === value })));
  const fieldEl = (label, input) => {
    const id = uniqueId("lab");
    input.id = id;
    return h("div", { class: "field" }, h("label", { class: "field-label", for: id, text: label }), input);
  };
  const configs = (status ? status.configs : []).map((c) => [c.name, `${c.name} · ${(FRIENDLY_CONFIGS[c.name] || [c.label])[0]}`]);
  const historyBox = h("input", { type: "checkbox", name: "with_history" });
  const submit = button("Build the prompt", { kind: "primary", iconName: "flask", type: "submit" });
  form.append(fieldEl("Question", question),
    h("div", { class: "field-row" },
      fieldEl("Configuration", select("config", configs, status ? status.config.name : "C")),
      fieldEl("Stage", select("stage", [["generation", "Writing the SQL"], ["verification", "AI self-review"], ["explanation", "Explaining SQL"]], "generation")),
      fieldEl("Schema sent", select("mode", [["auto", "Automatic"], ["full", "All tables"], ["retrieved", "Only relevant tables"]], "auto"))),
    h("label", { class: "checkbox" }, historyBox, h("span", { text: "Include this conversation (as a follow-up)" })),
    h("div", { class: "btn-row" }, submit));
  card.append(form);
  const output = h("div", { class: "stack section" });
  body.append(output);

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = new FormData(form);
    setBusy(submit, true, "Building…");
    try {
      const r = await api("/api/prompt-preview", { method: "POST", body: {
        question: String(data.get("question") || ""), config: data.get("config"), stage: data.get("stage"),
        mode: data.get("mode"), with_history: historyBox.checked } });
      drawLab(output, r);
    } catch (e) {
      clear(output).append(callout("bad", "alert", h("p", { text: e.message })));
    } finally {
      setBusy(submit, false);
    }
  });
  form.requestSubmit();
}

function drawLab(output, r) {
  clear(output);
  output.append(tiles([["Sections", String(r.prompt.sections.length)], ["Estimated tokens", formatNumber(r.prompt.token_estimate)],
    ["Cached part", r.cacheable_tokens ? `≈ ${formatNumber(r.cacheable_tokens)}` : "none"],
    ["Schema", `${r.context.tables.length} tables · ${r.context.mode}`]]));
  if (r.sample_sql) output.append(h("div", { class: "card" }, h("h3", { text: "SQL used for this stage" }), codeBlock(r.sample_sql)));
  output.append(h("div", { class: "card" }, h("h2", { text: "The prompt" }), promptView(r.prompt)));
  const scores = h("div", { class: "table-wrap" }, h("table", { class: "plain" },
    h("thead", null, h("tr", null, ["Table", "Score", "Sent", "Why"].map((x) => h("th", { scope: "col", text: x })))),
    h("tbody", null, r.context.scores.map((s) => h("tr", null, h("td", { text: s.table }), h("td", { class: "num", text: s.score.toFixed(2) }),
      h("td", { text: s.selected ? "yes" : "no" }), h("td", { text: s.why.join("; ") }))))));
  output.append(h("div", { class: "card" }, h("h2", { text: "Which tables were chosen" }),
    h("p", { class: "muted small", text: "Every table gets a transparent score from words in the question. With few tables, all are sent and the ranking is a hint." }), scores));
  const api_ = h("div", { class: "card" }, h("h2", { text: "API request" }));
  if (r.request) api_.append(h("p", { class: "muted small", text: "The API request arguments (the prompt text is shortened to its structure)." }), jsonBlock(r.request));
  else api_.append(h("p", { class: "muted small", text: "Request preview is available when an AI provider is configured." }));
  if (r.output_schema) api_.append(disclosure(`Output format enforced by the API${r.contract ? ` (${r.contract})` : ""}`, jsonBlock(r.output_schema)));
  output.append(api_);
}

// ====================================================================== evaluation
async function evaluation(body) {
  const card = h("div", { class: "card" }, h("div", { class: "skeleton", style: { height: "80px" } }));
  body.append(card);
  let data;
  try {
    data = await api(`/api/evaluation?dark=${isDarkTheme()}`);
  } catch (e) {
    clear(card).append(callout("bad", "alert", h("p", { text: e.message })));
    return;
  }
  clear(card);
  card.append(h("h2", { text: "How much does each technique help?" }));
  if (!data.summary) {
    card.append(callout("info", "gauge",
      h("p", { text: "No evaluation has been run yet, so there are no results to show. Results appear here only after a real, measured run against the test questions (QueryBridge never shows made-up numbers)." }),
      h("p", { class: "small", text: "To run it, open a terminal in the QueryBridge folder (with the virtual environment active) and run:" })),
    codeBlock("python -m evaluation.baseline_comparison --estimate   # shows the expected cost first\npython -m evaluation.baseline_comparison              # runs configurations A, B, D and C"));
    return;
  }
  const s = data.summary;
  card.append(tiles([["Test questions", String(s.questions ?? "—")], ["Model", s.model || "—"],
    ["Run on", String(s.generated_at || "—").slice(0, 10)], ["Database unchanged", s.database_unchanged ? "Yes" : "No"]]));
  const box = h("div", { class: "chart-box", role: "img", aria: { label: "Bar chart comparing the configurations on each metric; the same numbers are in the table below." } });
  card.append(h("div", { class: "section" }, box));
  drawFigure(box, data.figure).catch(() => box.replaceWith(callout("warn", "alert", h("p", { text: "The chart could not be drawn; the table below has the same numbers." }))));
  const metricNames = data.rows.length ? Object.keys(data.rows[0].metrics) : [];
  const pct = (v) => (v === null || v === undefined ? "n/a" : `${(v * 100).toFixed(1)}%`);
  card.append(h("h3", { class: "section", text: "Comparison table" }), h("div", { class: "table-wrap" }, h("table", { class: "plain" },
    h("thead", null, h("tr", null, ["Configuration", ...metricNames, "Avg. AI calls", "Avg. time (s)", "Cost"].map((x) => h("th", { scope: "col", text: x })))),
    h("tbody", null, data.rows.map((row) => h("tr", null, h("td", { text: row.configuration }),
      metricNames.map((m) => h("td", { class: "num", text: pct(row.metrics[m]) })),
      h("td", { class: "num", text: row.avg_llm_calls ?? "" }), h("td", { class: "num", text: row.avg_latency_s ?? "" }),
      h("td", { class: "num", text: row.cost_usd !== null && row.cost_usd !== undefined ? formatCost(row.cost_usd) : "" })))))));
  if (s.red_team) card.append(h("p", { class: "muted small", style: { marginTop: "10px" }, text: `Security test: ${s.red_team.blocked} of ${s.red_team.attacks} attack queries were blocked by the validator.` }));
  if (s.notes && s.notes.length) card.append(h("h3", { class: "section", text: "Notes" }), list(s.notes));
}

// ====================================================================== about
async function about(body, ctx) {
  const card = h("div", { class: "card" });
  body.append(card);
  card.append(h("h2", { text: "How QueryBridge keeps your data safe" }),
    list([
      "The database is opened read-only, and every query is checked before it runs: only single SELECT queries are allowed.",
      "Queries that could change data, read files, or reach system tables are blocked, even if the AI writes them.",
      "Every query has a time limit and a row limit, so a mistake cannot overload the database.",
      "Text stored in your database is treated as data, never as instructions to the AI.",
      ...(ctx.status && ctx.status.hosted ? [
        "The API key is kept on the server: it is never shown in the page or written to logs.",
        "This copy is public: anyone with the link can use it, everyone shares one question history, "
          + "and it only reads the built-in sample database.",
      ] : [
        "Your API key and database password stay on this computer and are never shown in the page or written to logs.",
        "The app only accepts connections from this computer.",
      ]),
    ]),
    h("p", { class: "muted small", text: `Prompt version ${ctx.status ? ctx.status.prompt_version : ""}.` }));
  const settings = h("div", { class: "card" }, h("h2", { text: "Active settings" }),
    h("p", { class: "muted small", text: "Values come from the .env file (secrets are hidden)." }), h("div", { class: "skeleton", style: { height: "60px" } }));
  body.append(settings);
  try {
    const info = await api("/api/connection");
    settings.lastChild.replaceWith(jsonBlock(info.settings));
  } catch (e) {
    settings.lastChild.replaceWith(callout("bad", "alert", h("p", { text: e.message })));
  }
}
