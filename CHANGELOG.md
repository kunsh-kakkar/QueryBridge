# Changelog

Dated list of changes since Milestone 1 (newest first).

## 2026-11-27

- Removed the *Behind the scenes* tab from answers, to keep the answer view simple. Each answer now has the Table, Chart, How I got this and SQL tabs; the exact prompt for any question is still shown in Settings → Prompt Lab.

## 2026-11-22 (Milestone 2 submission)

- Switched the language model to **Google Gemini** on the free Gemini API tier (`backend/gemini_client.py`). Gemini 3.8 Flash returned "high demand" (503) and timeout (504) errors; Gemini 3.6 Flash worked (about 24 s per question) but its free daily limit ran out during testing; the final model is **Gemini 3.5 Flash-Lite** (about 8 s per question).
- Structured JSON output for every prompt through Gemini's response schema, so answers always parse.
- Deployed the prototype on Render (`render.yaml`), with a "hosted" mode that turns off connecting other databases from the browser.
- Ran the Milestone 2 tests: 14 test inputs through all four prompt versions (v1–v4) on Gemini; results in `test-data/outputs/`.
- Added the Milestone 2 repository layout: `prompts/v1`, `prompts/v2`, `prompts/v3`, `prompts/final`, `code/`, `test-data/`, `docs/`, and this changelog.
- Added the Milestone 2 PDF and the supporting technical report to `docs/`.

## 2026-11-16 (core system built; date taken from the files' timestamps)

- Renamed the project from **QueryMate** (Milestone 1 title) to **QueryBridge**.
- Built the prompt pipeline: schema inspection with a data dictionary, schema retrieval, dynamic few-shot example selection, and the prompt manager with four configurations: A (basic), B (role + rules + schema), D (+ few-shot), C (full pipeline).
- Added the reviewer prompt (P2), the bounded repair loop (P3) and the Explain SQL prompt (P4).
- Added code safety checks: a read-only SQL validator (sqlglot), table/column existence checks, an EXPLAIN dry run, a read-only database connection, a query timeout and a row limit.
- Built the web interface (FastAPI + HTML/JS): Ask, Your data, History, Explain SQL and Settings (Prompt Lab) pages, with result tables and charts.
- Created the evaluation dataset (48 questions with gold SQL, including ambiguous, impossible and unsafe questions), the evaluation harness and the automated tests.
- Replaced the Milestone 1 plan of Streamlit + LangChain with FastAPI and direct API calls, to control every prompt section exactly.

## 2026-10-09 (Milestone 1)

- Submitted the Milestone 1 proposal: problem C-8 (natural language to SQL), project title "QueryMate: Natural Language to SQL Assistant", and the zero-shot baseline prompt (v1).
