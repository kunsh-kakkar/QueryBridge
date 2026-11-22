// "Ask a question": the home page. Plain-English question in, verified answer out.

import { ApiError, api, askStream } from "../api.js";
import {
  announce, answerCard, apiKeySteps, button, callout, confirmDialog, disclosure, progressCard, setBusy, statusPill, toast,
} from "../components.js";
import { clear, h, icon, uniqueId } from "../dom.js";

// Answers survive moving between pages (kept for this browser tab).
const state = { answers: [], busy: false, draft: "", progress: null };
let view = null; // the page currently on screen (a question may finish after the user moved away and back)

export function resetConversation() {
  state.answers = [];
}

export function lastSql() {
  const withSql = [...state.answers].reverse().find((r) => r.executed_sql);
  return withSql ? (withSql.generated_sql || withSql.executed_sql) : null; // the model's SQL, without the safety cap
}

const HOW_IT_WORKS = [
  ["chat", "Ask in plain English", "Type a question as you would ask a colleague. No SQL needed."],
  ["shield", "QueryBridge writes a safe query", "An AI model writes the SQL. QueryBridge checks it only reads data before running it."],
  ["table", "See the answer and why", "Get a table, a chart when useful, and a plain explanation of how it was found."],
];

export function render(main, ctx) {
  const status = ctx.status;
  const maxChars = status ? status.limits.max_question_chars : 1000;
  const llmReady = status ? status.llm.ready : false;
  const page = h("div", { class: "page" });

  page.append(h("div", { class: "page-head" },
    h("h1", { tabindex: "-1", text: "Ask your data a question" }),
    h("p", { text: "Type a question in everyday English. QueryBridge turns it into a safe database query, checks it, runs it and explains the answer." })));

  if (status && !status.database) {
    page.append(callout("bad", "database",
      h("p", null, h("strong", { text: "No database is connected. " }), status.connection_error || ""),
      h("p", null, h("a", { href: "#/settings/database", text: "Connect a database in Settings" }))));
  } else if (status && !llmReady) {
    page.append(callout("warn", "key",
      h("h3", { text: "One setup step left: add a free Gemini API key" }),
      apiKeySteps(status.llm),
      h("p", { class: "small", text: "Until then you can still explore Your data, browse History and run SQL in Explain SQL." })));
  }

  // ---------------------------------------------------------------- question box
  const inputId = uniqueId("question");
  const hintId = uniqueId("hint");
  const textarea = h("textarea", {
    class: "textarea", id: inputId, rows: "3", maxlength: String(maxChars), autocomplete: "off",
    placeholder: "e.g. Which 5 employees have the highest salaries?", aria: { describedby: hintId },
  });
  textarea.value = ctx.takePrefill() || state.draft;
  const counter = h("span", { class: "char-count" });
  const updateCounter = () => {
    const n = textarea.value.length;
    counter.textContent = `${n} / ${maxChars}`;
    counter.classList.toggle("over", n > maxChars * 0.9);
    state.draft = textarea.value;
  };
  textarea.addEventListener("input", updateCounter);
  updateCounter();

  const askButton = button("Ask", { kind: "primary", iconName: "send", size: "lg", type: "submit", disabled: !llmReady || !status?.database });
  const form = h("form", { class: "card ask-box", aria: { label: "Ask a question" } },
    h("label", { class: "field-label", for: inputId, text: "Your question" }),
    h("div", { style: { marginTop: "6px" } }, textarea),
    h("div", { class: "ask-actions" },
      h("p", { class: "ask-hint", id: hintId }, "Press ", h("kbd", { text: "Enter" }), " to ask · ",
        h("kbd", { text: "Shift" }), " + ", h("kbd", { text: "Enter" }), " for a new line · ", counter),
      askButton));
  page.append(form);

  textarea.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      form.requestSubmit();
    }
  });
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const question = textarea.value.trim();
    if (!question) {
      toast("Type a question first.");
      textarea.focus();
      return;
    }
    run({ question }, question);
  });

  // ---------------------------------------------------------------- examples
  if (status && status.examples.length) {
    page.append(h("div", { class: "examples" },
      h("p", { class: "examples-title", text: llmReady ? "Not sure what to ask? Click an example:" : "Example questions:" }),
      h("div", { class: "chips" }, status.examples.map((example) => h("button", {
        class: "chip", type: "button", on: {
          click: () => {
            textarea.value = example;
            updateCounter();
            if (llmReady && !state.busy) run({ question: example }, example);
            else textarea.focus();
          },
        },
      }, icon("arrowRight"), h("span", { text: example }))))));
  }

  // ---------------------------------------------------------------- conversation
  const conversationHead = h("div", { class: "conversation-head" });
  const answersBox = h("div", { class: "stack" });
  const intro = h("div");
  page.append(intro, conversationHead, answersBox);
  main.append(page);

  function drawIntro() {
    clear(intro);
    if (state.answers.length || state.busy) return;
    if (status && status.conversation.turns > 0) {
      intro.append(h("div", { style: { marginTop: "24px" } }, callout("info", "followUp",
        h("p", { text: `You are continuing an earlier conversation (${status.conversation.turns} question${status.conversation.turns === 1 ? "" : "s"}), so follow-up questions can refer to it.` }),
        button("Start a new conversation", { size: "sm", iconName: "plus", onClick: newConversation }))));
    }
    intro.append(h("div", { class: "how-grid", aria: { label: "How it works" } }, HOW_IT_WORKS.map(([iconName, title, text]) =>
      h("div", { class: "how-card" }, h("span", { class: "how-icon" }, icon(iconName)),
        h("div", null, h("h3", { text: title }), h("p", { text }))))));
  }

  function drawHead() {
    clear(conversationHead);
    if (!state.answers.length && !state.busy) return;
    conversationHead.append(
      h("h2", { text: "Answers" }),
      button("New conversation", { iconName: "plus", size: "sm", onClick: newConversation,
        title: "Forget earlier questions so the next one starts fresh" }));
    if (state.answers.length === 1 && !state.busy) {
      conversationHead.append(h("p", { class: "muted small", style: { flexBasis: "100%", margin: "0" },
        text: "Tip: ask a follow-up such as “now only show the Sales department” and QueryBridge will build on this answer." }));
    }
  }

  function drawAnswers() {
    clear(answersBox);
    const [latest, ...earlier] = [...state.answers].reverse();
    if (latest) answersBox.append(answerCard(latest, { onClarify: clarify, onExplain: ctx.explainSql }));
    if (earlier.length) {
      answersBox.append(h("h3", { class: "muted", style: { marginTop: "24px", fontSize: "0.95rem" }, text: "Earlier in this conversation" }));
      for (const result of earlier) {
        const summary = disclosure(result.question, answerCard(result, { showQuestion: false, onExplain: ctx.explainSql }));
        summary.querySelector("summary").prepend(statusPill(result.status, result.status_text, result.tone));
        answersBox.append(summary);
      }
    }
  }

  async function newConversation() {
    if (state.busy) return;
    if (state.answers.length > 1 && !(await confirmDialog({
      title: "Start a new conversation?", ok: "Start new",
      text: "The answers on this page will be cleared and follow-up questions will start fresh. Your history is kept.",
    }))) return;
    try {
      await api("/api/session/reset", { method: "POST" });
    } catch (e) {
      toast(e.message, "bad");
      return;
    }
    state.answers = [];
    if (ctx.status) ctx.status.conversation.turns = 0;
    drawAll();
    textarea.focus();
    announce("Started a new conversation.");
  }

  function clarify(answer) {
    run({ clarification_answer: answer }, answer);
  }

  function drawAll() {
    drawIntro();
    drawHead();
    drawAnswers();
  }
  view = { page, answersBox, askButton, textarea, updateCounter, drawAll, drawHead, intro, ctx };
  drawAll();
  if (state.busy && state.progress) {
    setBusy(askButton, true, "Working…");
    answersBox.prepend(state.progress.el);
  }
}

async function run(payload, displayText) {
  if (state.busy || !view) return;
  state.busy = true;
  const ctx = view.ctx;
  setBusy(view.askButton, true, "Working…");
  clear(view.intro);
  view.drawHead();
  const progress = progressCard(displayText);
  state.progress = progress;
  view.answersBox.prepend(progress.el);
  progress.el.scrollIntoView({ behavior: "smooth", block: "nearest" });
  announce("Working on your question.");
  let result = null;
  let failure = null;
  try {
    const final = await askStream(payload, (event) => { if (event.type === "step") progress.step(event.label); });
    if (final.type === "result") result = final.result;
    else throw new ApiError(final.message || "Something went wrong.");
  } catch (e) {
    failure = e.message;
  } finally {
    state.busy = false;
    state.progress = null;
  }
  const current = view; // may be a newer page than the one the question was asked on
  if (!current.page.isConnected) {
    if (result) state.answers.push(result);
    if (failure) toast(`Your question could not be answered: ${failure}`, "bad");
    else if (result) toast("Your answer is ready on the Ask page.");
    return;
  }
  setBusy(current.askButton, false);
  if (failure) {
    progress.el.replaceWith(h("article", { class: "card fade-in" },
      callout("bad", "alert", h("p", null, h("strong", { text: "No answer this time. " }), failure))));
    announce(`Error: ${failure}`);
    current.drawHead();
    return;
  }
  state.answers.push(result);
  if (!result.clarification) {
    current.textarea.value = "";
    current.updateCounter();
  }
  current.drawAll();
  const card = current.answersBox.querySelector(".answer");
  if (card) card.scrollIntoView({ behavior: "smooth", block: "start" });
  announce(result.clarification ? `QueryBridge needs more detail: ${result.clarification.question}`
    : `Answer ready. ${result.message || result.status_text}`);
  ctx.refreshStatus();
  if (result.clarification) {
    const first = current.answersBox.querySelector(".clarify .chip, .clarify input");
    if (first) first.focus({ preventScroll: true });
  }
}
