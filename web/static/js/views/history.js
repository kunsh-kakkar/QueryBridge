// "History": every question asked, with the SQL that ran. Re-running makes no AI call.

import { api } from "../api.js";
import {
  answerCard, button, callout, codeBlock, confirmDialog, emptyState, setBusy, statusPill, toast,
} from "../components.js";
import { clear, debounce, formatCost, formatDuration, h, plural, timeAgo, uniqueId } from "../dom.js";

export function render(main, ctx) {
  const page = h("div", { class: "page" });
  page.append(h("div", { class: "page-head" },
    h("h1", { tabindex: "-1", text: "History" }),
    h("p", { text: "Every question you have asked, with the query that ran. Run a saved query again to see today's data (no AI needed)." })));

  const searchId = uniqueId("search");
  const statusId = uniqueId("status");
  const search = h("input", { class: "input", id: searchId, type: "search", placeholder: "Search questions and SQL…", autocomplete: "off" });
  const statusSelect = h("select", { class: "select", id: statusId }, h("option", { value: "", text: "All results" }));
  const clearButton = button("Clear history", { kind: "danger-outline", iconName: "trash" });
  page.append(h("div", { class: "history-tools" },
    h("div", { class: "field" }, h("label", { class: "field-label", for: searchId, text: "Search" }), search),
    h("div", { class: "field" }, h("label", { class: "field-label", for: statusId, text: "Show" }), statusSelect),
    clearButton));
  const stats = h("p", { class: "muted small", aria: { live: "polite" } });
  const listBox = h("div");
  page.append(stats, listBox);
  main.append(page);

  let statusesLoaded = false;

  async function load() {
    const params = new URLSearchParams({ limit: "200" });
    if (search.value.trim()) params.set("search", search.value.trim());
    if (statusSelect.value) params.set("status", statusSelect.value);
    let data;
    try {
      data = await api(`/api/history?${params}`);
    } catch (e) {
      clear(listBox).append(callout("bad", "alert", h("p", { text: e.message })));
      return;
    }
    if (!statusesLoaded) {
      for (const [value, text] of Object.entries(data.statuses)) statusSelect.append(h("option", { value, text }));
      statusesLoaded = true;
    }
    const s = data.stats;
    stats.textContent = `${plural(s.total, "question")} saved · showing ${data.entries.length}` +
      (s.estimated_cost_usd ? ` · estimated AI cost so far ${formatCost(s.estimated_cost_usd)}` : "");
    clearButton.disabled = s.total === 0;
    clear(listBox);
    if (!data.entries.length) {
      listBox.append(emptyState(s.total === 0
        ? { iconName: "history", title: "No questions yet", text: "Questions you ask will be listed here, so you can run them again later.",
          action: button("Ask your first question", { kind: "primary", iconName: "chat", onClick: () => ctx.navigate("#/ask") }) }
        : { iconName: "search", title: "Nothing matches", text: "Try a different search or show all results." }));
      return;
    }
    listBox.append(h("ul", { class: "history-list" }, data.entries.map((entry) => h("li", null, entryCard(entry)))));
  }

  function entryCard(entry) {
    const card = h("div", { class: "card history-item" });
    const detailId = uniqueId("history-detail");
    const detail = h("div", { class: "history-detail", id: detailId });
    detail.hidden = true;
    const sub = [timeAgo(entry.created_at)];
    if (entry.clarified_with) sub.push(`you clarified: “${entry.clarified_with}”`);
    if (entry.row_count !== null && entry.row_count !== undefined) sub.push(plural(entry.row_count, "row"));
    if (entry.execution_time_ms !== null && entry.execution_time_ms !== undefined) sub.push(`ran in ${formatDuration(entry.execution_time_ms)}`);
    if (entry.config_name && entry.config_name !== "direct") sub.push(`configuration ${entry.config_name}`);
    if (entry.config_name === "direct") sub.push("SQL run directly");
    sub.push(entry.database);
    const toggle = h("button", { class: "history-summary", type: "button", aria: { expanded: "false", controls: detailId } },
      h("span", { class: "history-q", text: entry.question }),
      statusPill(entry.status, entry.status_text, entry.tone),
      h("span", { class: "history-sub" }, sub.map((x) => h("span", { text: x }))));
    toggle.addEventListener("click", () => {
      const open = detail.hidden;
      detail.hidden = !open;
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
      if (open && !detail.firstChild) fillDetail();
    });

    function fillDetail() {
      if (entry.message) detail.append(h("p", { class: "muted", text: entry.message }));
      if (entry.sql) detail.append(codeBlock(entry.sql));
      else detail.append(h("p", { class: "muted small", text: "No SQL was run for this question." }));
      const output = h("div", { style: { marginTop: "14px" } });
      const rerun = button("Run again", { kind: "primary", iconName: "play", size: "sm", disabled: !entry.sql,
        title: "Check and run the saved SQL again. No AI call is made." });
      rerun.addEventListener("click", async () => {
        setBusy(rerun, true, "Running…");
        try {
          const result = await api(`/api/history/${entry.id}/rerun`, { method: "POST" });
          clear(output).append(answerCard(result, { showQuestion: false, onExplain: ctx.explainSql }));
        } catch (e) {
          clear(output).append(callout("bad", "alert", h("p", { text: e.message })));
        } finally {
          setBusy(rerun, false);
        }
      });
      const askAgain = button("Ask again", { iconName: "chat", size: "sm", title: "Put the question on the Ask page to get a fresh answer",
        onClick: () => ctx.askWith(entry.question) });
      const remove = button("Delete", { kind: "danger-outline", iconName: "trash", size: "sm" });
      remove.addEventListener("click", async () => {
        if (!(await confirmDialog({ title: "Delete this entry?", text: `“${entry.question}” will be removed from your history.` }))) return;
        try {
          await api(`/api/history/${entry.id}`, { method: "DELETE" });
          toast("Entry deleted.");
          load();
        } catch (e) {
          toast(e.message, "bad");
        }
      });
      detail.append(h("div", { class: "btn-row", style: { marginTop: "12px" } }, rerun, askAgain, remove), output);
    }
    card.append(toggle, detail);
    return card;
  }

  search.addEventListener("input", debounce(load, 300));
  statusSelect.addEventListener("change", load);
  clearButton.addEventListener("click", async () => {
    if (!(await confirmDialog({ title: "Clear all history?", ok: "Delete everything",
      text: "Every saved question and its SQL will be deleted permanently. Your database is not affected." }))) return;
    try {
      const result = await api("/api/history", { method: "DELETE" });
      toast(`Deleted ${plural(result.removed, "entry", "entries")}.`);
      load();
    } catch (e) {
      toast(e.message, "bad");
    }
  });
  listBox.append(h("div", { class: "skeleton", style: { height: "70px" } }));
  load();
}
