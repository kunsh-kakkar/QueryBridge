// "Explain SQL": paste any SQL; QueryBridge checks it is read-only, explains it, and runs it only on request.

import { api } from "../api.js";
import { answerCard, button, callout, list, setBusy } from "../components.js";
import { clear, h, uniqueId } from "../dom.js";

const DEFAULT_SQL = `SELECT d.department_name, ROUND(AVG(e.salary), 2) AS average_salary
FROM employees AS e
JOIN departments AS d ON d.department_id = e.department_id
GROUP BY d.department_name
HAVING COUNT(*) > 5
ORDER BY average_salary DESC`;

let draft = null;

export function render(main, ctx) {
  const llmReady = ctx.status ? ctx.status.llm.ready : false;
  const page = h("div", { class: "page" });
  page.append(h("div", { class: "page-head" },
    h("h1", { tabindex: "-1", text: "Explain a SQL query" }),
    h("p", { text: "Paste any SQL query. QueryBridge first checks whether it is safe (read-only), then explains it step by step in plain English. It only runs if you click “Run it”." })));

  const sqlId = uniqueId("sql");
  const textarea = h("textarea", { class: "textarea mono", id: sqlId, rows: "8", spellcheck: "false", autocomplete: "off" });
  const incoming = ctx.takeSql();
  textarea.value = incoming || draft || ctx.lastSql() || (ctx.status && ctx.status.database && ctx.status.database.is_demo ? DEFAULT_SQL : "");
  textarea.addEventListener("input", () => { draft = textarea.value; });

  let level = "beginner";
  const levels = h("div", { class: "segmented", role: "radiogroup", aria: { label: "Explanation style" } });
  const levelButtons = [["beginner", "Simple"], ["technical", "Technical"]].map(([value, text]) => {
    const b = h("button", { type: "button", role: "radio", text, aria: { checked: value === level ? "true" : "false" } });
    b.addEventListener("click", () => {
      level = value;
      levelButtons.forEach((x) => x.setAttribute("aria-checked", x === b ? "true" : "false"));
    });
    return b;
  });
  levels.append(...levelButtons);

  const explainButton = button("Explain it", { kind: "primary", iconName: "book", disabled: !llmReady });
  const checkButton = button("Check safety only", { iconName: "shield" });
  const runButton = button("Run it", { iconName: "play" });
  const card = h("div", { class: "card" },
    h("div", { class: "field" }, h("label", { class: "field-label", for: sqlId, text: "SQL query" }), textarea),
    h("div", { class: "btn-row", style: { marginTop: "14px", justifyContent: "space-between" } },
      h("div", { class: "btn-row" }, h("span", { class: "small muted", text: "Explanation style:" }), levels),
      h("div", { class: "btn-row" }, checkButton, runButton, explainButton)));
  if (!llmReady) card.append(h("p", { class: "small muted", style: { marginTop: "10px", marginBottom: "0" },
    text: "Explanations need an AI API key (see Settings). Checking and running work without one." }));
  page.append(card);
  const output = h("div", { class: "stack section", aria: { live: "polite" } });
  page.append(output);
  main.append(page);

  const sql = () => textarea.value.trim();
  const need = () => {
    if (sql()) return true;
    clear(output).append(callout("warn", "edit", h("p", { text: "Paste a SQL query first." })));
    textarea.focus();
    return false;
  };

  function verdict(validation) {
    if (validation.is_safe) {
      return callout("good", "shield", h("p", null, h("strong", { text: "Safe to run. " }),
        "This is a read-only query: it can only look at data, not change it."),
      validation.warnings.length ? list(validation.warnings) : null,
      validation.limit_added ? h("p", { class: "small", text: `A row limit of ${validation.applied_limit} is added when it runs.` }) : null);
    }
    return callout("bad", "ban", h("p", null, h("strong", { text: validation.is_blocked ? "Not allowed. " : "This query has a problem. " }),
      validation.is_blocked ? "QueryBridge will not run it because it could change data or reach outside the database."
        : "The database would not accept it as written."), list(validation.messages));
  }

  checkButton.addEventListener("click", async () => {
    if (!need()) return;
    setBusy(checkButton, true, "Checking…");
    try {
      const data = await api("/api/sql/check", { method: "POST", body: { sql: sql() } });
      clear(output).append(verdict(data.validation));
    } catch (e) {
      clear(output).append(callout("bad", "alert", h("p", { text: e.message })));
    } finally {
      setBusy(checkButton, false);
    }
  });

  explainButton.addEventListener("click", async () => {
    if (!need()) return;
    setBusy(explainButton, true, "Explaining…");
    try {
      const data = await api("/api/sql/explain", { method: "POST", body: { sql: sql(), level } });
      clear(output).append(verdict(data.validation));
      if (data.error) output.append(callout("bad", "alert", h("p", { text: data.error })));
      if (data.explanation) output.append(explanationCard(data.explanation));
    } catch (e) {
      clear(output).append(callout("bad", "alert", h("p", { text: e.message })));
    } finally {
      setBusy(explainButton, false);
    }
  });

  runButton.addEventListener("click", async () => {
    if (!need()) return;
    setBusy(runButton, true, "Running…");
    try {
      const result = await api("/api/sql/run", { method: "POST", body: { sql: sql() } });
      clear(output).append(answerCard(result, { showQuestion: false }));
    } catch (e) {
      clear(output).append(callout("bad", "alert", h("p", { text: e.message })));
    } finally {
      setBusy(runButton, false);
    }
  });
}

function explanationCard(e) {
  const card = h("div", { class: "card" }, h("h2", { text: "What this query does" }), h("p", { class: "lead", text: e.summary }));
  if (e.steps.length) {
    card.append(h("h3", { text: "Step by step" }),
      h("ol", { class: "explain-steps" }, e.steps.map((step) => h("li", null,
        h("div", null, h("pre", { text: step.clause }), h("p", { style: { margin: "0" }, text: step.explanation }))))));
  }
  if (e.tables_used.length) card.append(h("p", null, h("strong", { text: "Tables used: " }), e.tables_used.join(", ")));
  if (e.potential_issues.length) card.append(callout("warn", "alert", h("p", null, h("strong", { text: "Things to watch out for" })), list(e.potential_issues)));
  if (e.modifies_data) card.append(callout("bad", "ban", h("p", { text: "This statement would change data. QueryBridge will never run it." })));
  return card;
}
