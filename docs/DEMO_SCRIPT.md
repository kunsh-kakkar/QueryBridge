# QueryBridge: demonstration script (about 10 minutes)

**Before the session**

1. `cp .env.example .env` and set `GEMINI_API_KEY` (a free key from Google AI Studio, aistudio.google.com).
2. `python -m database.init_db` and `python -m utils.env_check` (must end with `RESULT: READY`).
3. Optional: run `python -m evaluation.baseline_comparison` beforehand (about $5) so **Settings → Evaluation** shows measured results.
4. `python app.py`. The browser opens at http://127.0.0.1:8501. To show the welcome guide again, click **How it works** in the sidebar.

---

## 1. The problem and the promise (1 min) · Welcome guide

* "Employees can't write SQL. QueryBridge lets them ask in English, and it never writes to the database."
* Walk through the three steps on the welcome guide, and the **Your data is safe** note.
* Point at the sidebar status: the database name, **8 tables · read-only**, and **AI ready**.

## 2. A first question (1.5 min) · Ask a question

* Click the example **"Show me the top 5 employees with the highest salaries."** It is asked straight away.
* While it runs, the progress card ticks through the stages: *reading the question → finding the tables → writing the query with AI → checking it is safe and correct, then running it → preparing the answer*.
* Show the **plain-language answer** at the top, then the **Table** tab (sort a column, filter, Download CSV) and the **SQL** tab (copy; switch to the PostgreSQL view).
* **How I got this:** the tables, filters, calculations and assumptions, plus the confidence explained in words. These are user-facing explanations, not hidden reasoning.

## 3. Prompt engineering made visible (2 min) · Prompt lab

* Open **Settings → Prompt lab**, switch the configuration **A → B → D → C** and watch the prompt grow from about 60 to about 4,200 tokens. Under *Worked examples chosen*: "examples come from a *different* (bookstore) schema and are chosen by SQL pattern. Here, top-N."
* Type "Which employees earn more than their manager?" and show that the self-join example is picked.

## 4. Conversation memory (1 min)

* Ask **"Show employees earning more than 50,000."**, then **"Now sort them by department."**
* Point at the **Follow-up** tag. The SQL keeps the filter and adds a join plus ORDER BY. Earlier answers stay listed under *Earlier in this conversation*.

## 5. Ambiguity (1 min)

* Ask **"Show me the highest-performing employees."**
* QueryBridge asks how performance should be measured and offers buttons. Click **Total sales amount**, and it answers with the clarified meaning. In **History** the entry shows *you clarified: "Total sales amount"*.

## 6. Safety (1.5 min)

* Ask **"Delete all sales from 2023."**: refused immediately, *with no AI call at all*.
* Open **Explain SQL**, paste `SELECT 1; DROP TABLE employees` and click **Run it**: *Blocked for safety*. **Check safety only** gives the same verdict without running anything.
* Mention the layers: validator AST checks, then the database opened read-only with an authorizer, then a timeout and row cap. The red-team suite blocks 63/63 attacks, and the evaluation proves the database fingerprint is unchanged. **Settings → About & safety** lists the same points in plain words.

## 7. Schema understanding (0.5 min) · Your data

* Open **employees**: what each column means, example values, how it connects to other tables, and the clickable map. Click **Show 10 example rows**. "This is exactly the context the model receives."

## 8. Evaluation (1 min) · Settings → Evaluation

* If you ran it: compare **A (basic) / B (role + schema) / D (+ few-shot) / C (full)** across result-set accuracy, schema compliance, safety and ambiguity F1, plus cost and calls per question.
* Explain: "A has no schema, so it must guess names. B grounds it. D adds patterns. C adds verification and bounded correction at extra cost."
* If you did not run it: the page says so and shows the commands; say honestly that the numbers come only from a real run.

## Backup answers to likely questions

| Question | Answer |
|---|---|
| "Can it run a DELETE if the model is tricked?" | No. The validator blocks it; even if it didn't, SQLite is opened `mode=ro` with `query_only` and an authorizer. Each layer alone refuses writes (tested). |
| "Does it send my data to the LLM?" | Only the schema and short value lists. Result rows are never sent. |
| "What if the model is wrong?" | Static checks and an EXPLAIN dry run catch invalid SQL; an independent reviewer checks intent; at most 2 corrections; the confidence and assumptions show the uncertainty. |
| "Does it work on other databases?" | Any SQLite file (Settings → Database → upload). PostgreSQL/MySQL adapters exist but are experimental. |
| "Could another website on my computer use it?" | No. It only answers requests from its own page: other Host names are rejected, state-changing requests need a custom header, and a strict Content-Security-Policy is set. |
| "How much does a question cost?" | Typically 2 calls in configuration C (generate + review); the Settings page shows the tokens and estimated cost used so far. |
