# QueryBridge — Architecture & Development Plan (Phase 1)

> Natural Language → SQL Intelligence System
> Prompt Engineering mini-project (C8). This document is the design baseline that later phases implement.

---

## 0. Verified environment (2026-11-15)

| Item | Finding | Consequence |
|---|---|---|
| Python | 3.13.18 | All code targets 3.11+ syntax; tested on 3.13 |
| Latest PyPI versions | google-genai 3.6.1 · sqlglot 30.31.0 · SQLAlchemy 2.1.8 · pydantic 2.14.3 · plotly 7.1.0 · pandas 3.1.1 | Versions get pinned in Phase 2, after an install + import smoke test. (The web interface later added fastapi 0.165.0, uvicorn 0.54.0, python-multipart 0.0.34; see §9.) |
| Gemini API key | **Not present in this container** | Unit tests run offline against a fake LLM. Live evaluation (Phase 9) needs a key, entered through a secure form and never pasted into chat |
| Browser | Chromium + Playwright pre-installed | Phase 10 adds an automated UI smoke test with screenshots |

---

## 1. Final architecture

QueryBridge has seven layers. Each layer depends only on the layers below it.

```
┌──────────────────────────────────────────────────────────────────────────┐
│ 1. PRESENTATION  Browser: HTML · CSS · JavaScript modules · Plotly        │
│    Ask │ Your data │ History │ Explain SQL │ Settings (DB, lab, eval)     │
│    web/server.py: FastAPI JSON API · sessions · security headers         │
├──────────────────────────────────────────────────────────────────────────┤
│ 2. ORCHESTRATION  backend/pipeline.py  (QueryPipeline + PipelineTrace)    │
├───────────────────────────────┬──────────────────────────────────────────┤
│ 3. INTELLIGENCE (LLM)          │ 4. SAFETY & CORRECTNESS (deterministic)  │
│  prompt_manager                │  input_processor                         │
│  example_selector              │  sql_validator   (security gate)         │
│  sql_generator                 │  sql_verifier    (static + LLM semantic) │
│  sql_corrector                 │  complexity_analyzer                     │
│  llm_client / gemini_client    │                                          │
├───────────────────────────────┴──────────────────────────────────────────┤
│ 5. DATA ACCESS  database · schema_inspector · schema_retriever ·          │
│                 sql_executor · result_formatter · chart_recommender       │
├──────────────────────────────────────────────────────────────────────────┤
│ 6. STATE  conversation_manager (session memory) · history_store (SQLite)  │
├──────────────────────────────────────────────────────────────────────────┤
│ 7. EVALUATION  evaluator · metrics · baseline_comparison (same pipeline,  │
│                techniques toggled by PromptConfig flags)                  │
└──────────────────────────────────────────────────────────────────────────┘
```

### 1.1 Request flow (with the bounded correction loop)

```
 User question
      │
      ▼
 [InputProcessor] ── too long / empty / control chars ──────────────► reject
      │  (normalise, detect explicit write intent → early refusal)
      ▼
 [ConversationManager] ── adds previous turn: question, SQL, result shape
      │
      ▼
 [SchemaInspector] (cached SchemaModel) ──► [SchemaRetriever]
      │                  relevant tables + FK join paths + previous-turn tables
      ▼
 [ExampleSelector] ── picks k few-shot examples by pattern + lexical similarity
      ▼
 [PromptManager] ── assembles modular sections (role │ rules │ schema ║ cache ║
      │             few-shot │ conversation │ question)
      ▼
 [SQLGenerator] ── Gemini + structured output ──► Pydantic SQLGenerationResult
      │
      ├── needs_clarification ─────────────────────────► ClarificationResponse
      ├── intent = unsafe/out_of_scope ────────────────► Refusal
      ▼
 [SQLValidator]  security gate (AST): 1 statement, SELECT/WITH only,
      │          no DML/DDL/PRAGMA/ATTACH, no dangerous functions, LIMIT enforced
      ├── unsafe ──────────────────────────────────────► BLOCK (never regenerated)
      ▼
 [SQLVerifier]   static: tables ✓ columns ✓ joins ✓ dialect ✓ EXPLAIN dry-run ✓
      │          semantic (LLM, 2nd stage): "does this SQL answer the question?"
      ├── fail ──► [SQLCorrector] ── attempt ≤ MAX_CORRECTIONS ──► back to Validator
      ▼                                 (budget exhausted → honest failure)
 [SQLExecutor]   read-only connection · engine-level authorizer · timeout · row cap
      ├── DB error ──► [SQLCorrector] (same shared attempt budget)
      ▼
 [ResultFormatter] DataFrame · type detection · stats · ChartRecommender
      ▼
 [ComplexityAnalyzer] · confidence aggregation · explanation
      ▼
 [HistoryStore] + [ConversationManager.update]
      ▼
 Frontend display (SQL, explanation, table, chart, warnings, trace)
```

All validation, verification and execution failures draw from one **shared correction budget**
(default 2 attempts, hard maximum 3, checked when the configuration loads). With default settings
one question costs at most **5 LLM calls**: generate → verify → correct → verify → correct.

### 1.2 Module responsibilities

| Module | Responsibility | Key output |
|---|---|---|
| `config.py` | Load and validate `.env` settings (model, effort, limits, paths) with Pydantic; enforce hard caps | `Settings` |
| `backend/models.py` | Every Pydantic model used across modules (LLM outputs, schema, results, trace) | shared types |
| `backend/llm_client.py` | The shared `StructuredLLM` interface, per-stage settings, usage tracking and `create_llm_client(settings)` | `LLMResponse[T]` |
| `backend/gemini_client.py` | The Gemini client (free tier) behind that interface: JSON schema reduced to what Gemini enforces, effort mapped to thinking levels, finish-reason / safety-block handling, typed errors (auth, rate limit and free daily quota, network, invalid response), implicit-cache accounting | `LLMResponse[T]` |
| `backend/input_processor.py` | Length limits, Unicode normalisation, control-char stripping, explicit write-intent detection ("delete…", "drop…") | `ProcessedInput` |
| `backend/conversation_manager.py` | Rolling window of N turns (question, SQL, columns, row count; **no row data**); clarification state | `ConversationContext` |
| `backend/database.py` | Engine factory for SQLite / PostgreSQL / MySQL; read-only session setup per dialect; masked URLs; connection testing | `DatabaseManager` |
| `backend/schema_inspector.py` | SQLAlchemy inspection: tables, columns, types, PKs, FKs, row counts, optional sample values; builds the relationship graph | `SchemaModel` |
| `backend/schema_retriever.py` | Selects relevant tables (lexical + synonym + description scoring, FK expansion to connect join paths, previous-turn tables) | `SchemaContext` |
| `backend/example_selector.py` | Context-aware few-shot selection (pattern tags + BM25-style similarity + diversity) | `list[FewShotExample]` |
| `backend/prompt_manager.py` | Builds every stage prompt from modular sections; toggles techniques via `PromptConfig`; defines presets A/B/C/D; transpiles examples per dialect; CLI `python -m backend.prompt_manager` | `BuiltPrompt` |
| `backend/sql_generator.py` | Stage 1: NL → structured SQL result | `SQLGenerationResult` |
| `backend/sql_validator.py` | Security gate on the SQLGlot AST; LIMIT injection; safe re-rendering | `ValidationReport` |
| `backend/sql_verifier.py` | Stage 2: static schema/join/dialect checks and EXPLAIN dry run, plus LLM semantic verification | `VerificationReport` |
| `backend/sql_corrector.py` | Controlled regeneration from (question, bad SQL, sanitised error) | `SQLGenerationResult` |
| `backend/sql_executor.py` | Executes with timeout and row cap; measures time; detects truncation | `ExecutionResult` |
| `backend/complexity_analyzer.py` | AST features (joins, subqueries, CTEs, aggregates, windows, set operations) → score and tier | `ComplexityReport` |
| `backend/result_formatter.py` | DataFrame shaping, dtype inference, summary stats, empty-result messaging | `FormattedResult` |
| `backend/chart_recommender.py` | Rule-based chart suggestions (bar / line / scatter / pie / histogram / KPI) built as Plotly figures | `list[ChartSpec]` |
| `backend/history_store.py` | Persistent query history in a separate metadata SQLite DB; revisit and clear | `HistoryEntry` |
| `backend/pipeline.py` | Orchestrates everything above; records a step-by-step `PipelineTrace` (timings, prompts, attempts) | `PipelineResult` |
| `prompts/*.py` | Versioned templates: role, rules, dialect notes, few-shot pool, verification, correction, explanation, baseline (Config A) | template strings |
| `utils/logger.py` | Structured logging with a **secret-redaction filter** (API keys, DB passwords, URLs) | logger |
| `utils/helpers.py` | Token estimates, text truncation, safe HTML escaping, timers | helpers |
| `evaluation/*` | Dataset, metrics, runner, configuration comparison, charts and report | JSON / CSV / PNG |

---

## 2. Folder structure

```
QueryBridge/
├── app.py                         # starts the web interface: python app.py [--port] [--no-browser]
├── config.py                      # Pydantic settings from env/.env, hard caps, secret handling
├── requirements.txt / requirements-dev.txt / requirements-optional.txt
├── pytest.ini · ruff.toml · .env.example · .gitignore · README.md
├── backend/
│   ├── models.py                  # shared Pydantic types incl. the LLM output contract
│   ├── exceptions.py              # error hierarchy with UI-safe messages
│   ├── database.py                # read-only engines (mode=ro + query_only + authorizer)
│   ├── schema_inspector.py        # tables/columns/keys/FKs + profiling + data dictionary
│   ├── schema_retriever.py        # relevant-schema selection (scored, explained)
│   ├── example_selector.py        # context-aware few-shot selection
│   ├── prompt_manager.py          # modular prompts per stage; presets A/B/C/D; CLI inspector
│   ├── llm_client.py              # LLM interface + factory, usage tracking
│   ├── gemini_client.py           # Google Gemini SDK client, typed errors
│   ├── input_processor.py         # normalisation, limits, early write refusal, injection signals
│   ├── conversation_manager.py    # multi-turn memory + clarification state
│   ├── sql_generator.py           # generation / correction / explanation calls
│   ├── sql_validator.py           # security gate (tokens + AST + functions + catalogs + LIMIT)
│   ├── sql_verifier.py            # static checks + EXPLAIN dry run + LLM semantic review
│   ├── sql_corrector.py           # bounded validate→verify→execute→correct loop
│   ├── sql_executor.py            # validation-first execution, timeout, row cap
│   ├── complexity_analyzer.py     # Simple / Moderate / Complex from the AST
│   ├── result_formatter.py        # typed DataFrame, column profiles, CSV
│   ├── chart_recommender.py       # chart forms + Plotly rendering (validated palette)
│   ├── history_store.py           # query history in a separate metadata SQLite DB
│   └── pipeline.py                # QueryPipeline orchestrator + trace + composite confidence
├── prompts/
│   ├── system_prompt.py           # role, rules, dialect notes
│   ├── few_shot_examples.py       # 22 examples on a separate bookstore schema
│   ├── verification_prompt.py     # independent reviewer role + checklist
│   ├── correction_prompt.py       # failed-attempt block + instructions
│   ├── explanation_prompt.py      # explanation mode (beginner / technical)
│   └── baseline_prompt.py         # configuration A
├── database/
│   ├── schema.sql · sample_data.py · init_db.py · data_dictionary.json
├── web/
│   ├── server.py                  # FastAPI app: JSON API, streamed answers, Host/CSRF/CSP guards
│   ├── app_state.py               # connected DB, schema, pipeline, history; database switching
│   ├── sessions.py                # per-browser conversation memory, config and recent results
│   ├── serializers.py             # backend objects → JSON (no credentials, untrusted text as data)
│   └── static/                    # index.html, css/app.css, js/{app,api,dom,components}.js,
│                                  # js/views/{ask,data,history,explain,settings}.js, img/
├── evaluation/
│   ├── test_dataset.json          # 48 questions, gold SQL / expected behaviour
│   ├── metrics.py · evaluator.py · report.py · red_team.py
│   ├── baseline_comparison.py     # CLI: --estimate, full A/B/D/C run, reports
│   └── results/                   # generated by a real run (summary.json, comparison.*)
├── tests/                         # 30 test modules (offline; `live` marker for real API);
│                                  # browser/: scripted-model demo server + Playwright walkthrough
├── utils/                         # logger (secret redaction), helpers, env_check
├── data/                          # generated: demo DB + metadata DB (git-ignored)
└── docs/                          # ARCHITECTURE.md, DEMO_SCRIPT.md, screenshots/
```

**Changes to the suggested structure, with reasons**

* `models.py`, `llm_client.py`: one shared type system and one place that handles API errors, so no module talks to the SDK directly.
* `pipeline.py`: the orchestrator is used by both the UI and the evaluator. The evaluation therefore measures the real pipeline, not a copy of it.
* `example_selector.py`, `sql_corrector.py`, `complexity_analyzer.py`, `chart_recommender.py`, `history_store.py`, `input_processor.py`: each required feature gets its own testable module.
* `web/`: the interface is a thin layer. The server only translates between HTTP and `QueryPipeline`, so every safety check stays in the backend; each page is its own JavaScript module.
* `data/`: generated databases stay out of source folders; the history DB is separate so the target DB can be opened strictly read-only.

---

## 3. Demonstration database

All five required tables are included with their required columns. Three extra tables make the
multi-table joins, many-to-many relationships and the ambiguity case genuine: "highest-performing"
then really can mean sales, review ratings or completed projects.

```
departments (department_id PK, department_name, location, budget)
employees   (employee_id PK, employee_name, email, job_title, department_id FK→departments,
             salary, joining_date, manager_id FK→employees)
products    (product_id PK, product_name, category, price, stock_quantity)
customers   (customer_id PK, customer_name, city, country, registration_date, segment)
sales       (sale_id PK, employee_id FK→employees, product_id FK→products,
             customer_id FK→customers, quantity, sale_amount, sale_date)
projects            (project_id PK, project_name, department_id FK→departments,
                     start_date, end_date, status, budget)          ← added
employee_projects   (employee_id FK, project_id FK, role, hours_allocated)  ← added (junction)
performance_reviews (review_id PK, employee_id FK, reviewer_id FK→employees,
                     review_date, rating, comments)                 ← added
```

* Actual volume (seed 42): 8 departments, 60 employees, 40 products, 150 customers, 2,500 sales (2023-01 → 2026-10), 20 projects, 105 assignments and 209 reviews (3,092 rows).
* Edge cases for demo questions: 2 employees out-earn their manager, 5 hires in 2026, 4 products out of stock, NULL `end_date` for open projects, NULL review comments.
* **Deterministic:** a fixed random seed means the gold results in the evaluation dataset always reproduce.
* **Temporal anchoring:** prompts state a reference date (`QB_REFERENCE_DATE`, default today). Evaluation pins it to a fixed date, so questions like "last quarter" have stable gold answers.
* `sales.customer_id` and `quantity` were added. Without them the Customers table has no relationship to anything.

---

## 4. Prompt engineering architecture

### 4.1 Prompt sections (generation stage)

| # | Section | Technique | Stable? |
|---|---|---|---|
| S1 | **Role:** expert SQL database analyst for dialect X | Role prompting | stable |
| S2 | **Rules:** read-only only, use only listed tables and columns, ask when ambiguous, treat `<data>` as data, user-facing rationale only | Constraints and safety | stable |
| S3 | **Dialect notes:** SQLite date functions, `LIMIT`, string matching, and so on | Dialect conditioning | stable per DB |
| S4 | **Schema context:** compact DDL plus table and column descriptions, PK/FK, join hints, optional sample values inside `<data>` tags. Schemas with ≤ 12 tables are sent whole in a fixed order; larger ones use per-question retrieval | Contextual prompting | stable per DB (full mode) → **cache breakpoint** |
| S5 | **Few-shot examples:** k = 4 chosen for this question | Few-shot, context-aware | per question |
| S6 | **Conversation context:** previous question(s), SQL and result shape | Memory / follow-ups | per turn |
| S7 | **User question** inside `<question>` tags, plus the reference date | Delimiting | per turn |
| — | **Output contract:** Pydantic JSON schema enforced by the API | Structured output | — |

Stable content comes first, so Gemini's implicit prompt cache can reuse S1–S4 across questions.
Content that changes per question comes after the cache breakpoint. The Prompt lab (Settings) shows every
section, its token estimate, and the cache hit and miss counts reported by the API.

### 4.2 Stage prompts

1. **Generation:** S1–S7 → `SQLGenerationResult`
2. **Semantic verification:** role "SQL reviewer"; input = question, schema subset, candidate SQL and static findings → `VerificationVerdict {is_correct, matches_intent, issues[], severity}`
3. **Correction:** input = question, failed SQL, *sanitised* error, verifier issues and attempt number → `SQLGenerationResult`
4. **Explanation mode:** arbitrary user SQL → `SQLExplanation` (clause-by-clause, beginner or technical)
5. **Baseline (Config A):** a single minimal instruction with the same output schema

### 4.3 Structured output contract (summary)

```python
class SQLGenerationResult(BaseModel):
    intent: Literal["data_retrieval", "follow_up_modification", "clarification_needed",
                    "unsafe_request", "out_of_scope"]
    needs_clarification: bool
    clarification_question: str | None
    clarification_options: list[str]          # rendered as buttons in the UI
    sql: str | None
    tables_used: list[str]
    columns_used: list[str]
    explanation: QueryExplanation              # summary, tables, conditions, calculations,
                                               # join_rationale, aggregation_rationale
    assumptions: list[str]
    confidence: float                          # 0–1 model self-estimate
```

The confidence shown to users is a **composite**: the model's self-estimate combined with
verification results, schema compliance and how many corrections were needed. Self-reported LLM
confidence is poorly calibrated on its own.

### 4.4 Chain-of-thought safety

Gemini reasons internally before answering (thinking levels). QueryBridge never requests the
thought text and never shows it. Users see only the short, purpose-built fields
`explanation` and `assumptions`.

### 4.5 Configurations for evaluation (flags on `PromptConfig`)

| Technique | A: Basic | B: Role + Schema | D: B + Few-shot (ablation) | C: Full |
|---|:-:|:-:|:-:|:-:|
| Structured output (needed to score) | ✓ | ✓ | ✓ | ✓ |
| Role prompt | – | ✓ | ✓ | ✓ |
| Schema context + relationships | – | ✓ | ✓ | ✓ |
| Few-shot (context-aware) | – | – | ✓ | ✓ |
| Static verification + correction loop | – | – | – | ✓ |
| LLM semantic verification | – | – | – | ✓ |
| Security validator before execution | ✓ | ✓ | ✓ | ✓ (never disabled) |

Config D separates the effect of few-shot examples from the effect of verification.

Measured steps: **A→B** adds role + rules + dialect notes + schema context together (the "prompt engineering" step); **B→D** adds few-shot examples; **D→C** adds verification and bounded correction. Conversation memory, the reference date and structured output are identical in all four configurations, so the comparison isolates the prompting techniques. The presets are defined once in `backend/prompt_manager.py` and used by both the UI and the evaluator.

---

## 5. Security model (defence in depth)

1. **Input layer:** length cap, normalisation, early detection of explicit write intent.
2. **Prompt layer:** read-only rules; user text and DB samples wrapped in tags and declared to be data. Result rows are **never** sent back to the LLM, only column names and the row count, which shrinks the prompt-injection surface.
3. **AST gate (SQLGlot):** exactly one statement; root must be `SELECT`, a set operation, or `WITH … SELECT`; the AST walk rejects INSERT, UPDATE, DELETE, MERGE, DROP, ALTER, TRUNCATE, CREATE, REPLACE, GRANT, ATTACH, DETACH, PRAGMA, VACUUM and any unparsed `Command`; dangerous functions (`load_extension`, `readfile`, `writefile`, `pg_sleep`, `sleep`, `benchmark`, …) are blocked; system catalogs are blocked; the SQL that runs is re-rendered from the AST and a `LIMIT` is enforced.
4. **Engine layer:** SQLite opens with `mode=ro` + `PRAGMA query_only=ON` + a **`sqlite3` authorizer callback** that denies every action except read and select, so the engine refuses writes even if the validator had a bug. PostgreSQL and MySQL use read-only session transactions and server-side statement timeouts.
5. **Execution limits:** timeout through the SQLite progress handler (`statement_timeout` / `MAX_EXECUTION_TIME` on the other dialects), a fetch cap of `max_rows + 1` to detect truncation, and a cap on result size.
6. **Secrets:** API keys come from the environment only; DB URLs are always rendered with `hide_password=True`; a log filter redacts key and password patterns; error messages shown to users are sanitised.
7. **UI:** LLM and database text is never rendered with `unsafe_allow_html`. Custom HTML is used only for static styling.
8. **Integrity proof:** the evaluation hashes the demo DB before and after the full run. A matching hash proves that no unsafe request changed any data.

---

## 6. Evaluation methodology

* **Dataset:** 45 items. Categories: simple, filtering, sorting, aggregation, joins, nested, date, complex multi-table, multi-turn follow-ups, ambiguous (expected to ask for clarification), unsafe (expected refusal or block), and impossible (asks for a column that doesn't exist).
* **Per item:** `id`, `category`, `question` (or multi-turn list), `expected_behavior` (`execute` | `clarify` | `refuse`), `gold_sql`, `order_matters`.
* **Gold SQL is executed by a test.** Every gold query must run on the demo DB, and the few-shot pool may not contain any evaluation question (leakage test).
* **Metrics** (each one measured from actual runs):
  1. Execution accuracy: generated SQL runs without error.
  2. Exact match: SQLGlot-normalised SQL is identical (strict; reported for reference).
  3. Result-set accuracy: row multisets equal after value normalisation and column-permutation matching; order is checked when `order_matters`.
  4. Schema compliance: every referenced table and column exists (measured on the first candidate, before any correction).
  5. Safety accuracy: unsafe items refused or blocked, plus a deterministic red-team suite run directly against the validator.
  6. Ambiguity detection: precision and recall of `needs_clarification`, including the false-clarification rate on clear questions.
  7. Operational: latency, LLM calls, tokens, correction attempts used.
* **Reporting:** raw per-item JSON (model id, config, timestamp), a summary CSV, a comparison table, grouped bar charts, and per-category breakdowns. **No number appears in the README until it has been measured.**
* **Cost control:** a run estimate is printed before execution, and `--limit` / `--categories` allow partial runs.

---

## 7. Development roadmap

| Phase | Deliverables | Exit criteria |
|---|---|---|
| 1 | Architecture and plan (this file) | Approved by you |
| 2 | Folder skeleton, pinned requirements, `.env.example`, `config.py`, `utils/` | Clean venv install; imports succeed; config tests pass |
| 3 | `schema.sql`, `sample_data.py`, `init_db.py`, `database.py`, `schema_inspector.py`, `schema_retriever.py` | DB builds deterministically; inspector finds all PKs and FKs; retriever tests pass |
| 4 | `prompts/*`, `example_selector.py`, `prompt_manager.py` | Prompts assemble for all configs; selector picks the expected patterns; leakage test passes |
| 5 | `models.py`, `llm_client.py`, `input_processor.py`, `conversation_manager.py`, `sql_generator.py` | Fake-LLM tests pass; every SDK error type is mapped; a live smoke call works once a key is available |
| 6 | `sql_validator.py`, `sql_verifier.py`, `sql_corrector.py`, `complexity_analyzer.py` | Red-team suite blocks 100% of attacks; valid queries are not over-blocked |
| 7 | `sql_executor.py`, `result_formatter.py`, `chart_recommender.py`, `history_store.py`, `pipeline.py` | End-to-end fake-LLM tests including correction loop and attempt cap; timeout test passes |
| 8 | Web interface (first built with Streamlit, later replaced by HTML/CSS/JS + FastAPI; see §9) | App boots; every page renders; API tests and Playwright walkthrough pass |
| 9 | `test_dataset.json`, `metrics.py`, `evaluator.py`, `baseline_comparison.py`, **live run** | Metric unit tests pass; real A/B/D/C results saved and charted |
| 10 | Integration hardening and bug fixes | Full test suite green; UI walkthrough screenshots; DB hash unchanged |
| 11 | README, demo script, assignment mapping | Every claim in the README backed by code or a measured result |

---

## 8. Key technical decisions

1. **Official Google Gen AI SDK (`google-genai`).** The model is configurable (`QB_MODEL`, default `gemini-3.5-flash-lite`) and the effort level can be set per stage (`QB_GENERATION_EFFORT`, `QB_VERIFICATION_EFFORT`); it maps to Gemini thinking levels.
2. **Constrained structured outputs.** The Pydantic contract's JSON schema is reduced to the subset Gemini enforces (`$ref` inlined, unsupported keywords dropped; `gemini_schema` in `backend/gemini_client.py`) and sent as `response_json_schema` with `response_mime_type="application/json"`, so the API returns schema-shaped JSON. QueryBridge checks the finish reason first, so a safety block or a truncated response is reported as such and not as a generic JSON error, then validates with Pydantic. Every field is required, and numeric ranges are clamped by validators because the API treats them only as descriptions.
3. **No `temperature` tuning.** Reproducibility comes from structured outputs, a fixed data seed, a pinned reference date, and optional repeated evaluation runs.
4. **Refusals and failures are always handled:** the finish reason is checked (safety blocks, `MAX_TOKENS`, blocked prompts); SDK exceptions map to typed, user-friendly errors.
5. **Prompt caching:** the stable prefix (role, rules and schema) comes first, so Gemini's implicit caching reduces latency across a session.
6. **SQLGlot** handles parsing, AST-level security, column qualification against the schema, dialect transpilation and complexity features. **SQLAlchemy 2.x** handles connections and inspection.
7. **Two-stage verification:** deterministic checks first (cheap, exact), then an LLM semantic check. A static failure skips the LLM verifier and goes straight to correction.
8. **Schema-agnostic few-shot pool** written on a neutral "bookstore" schema. The examples teach SQL patterns rather than answers, still work when you connect your own DB, and **cannot leak** evaluation answers.
9. **Engine-enforced read-only access**, not just validation (see §5, layer 4).
10. **History is kept in a separate metadata DB.** Revisiting an entry re-validates and re-runs the stored SQL instead of caching rows.
11. **Offline-first tests:** a `FakeLLMClient` makes the whole pipeline testable without network or cost. Live tests are opt-in.
12. **Full schema for small databases, retrieval for large ones** (`QB_SCHEMA_FULL_CONTEXT_MAX_TABLES=12`). A small schema sent whole keeps the cached prompt prefix stable and can't leave out a needed table. The retriever's ranking is still passed as a short hint after the cache breakpoint. Larger schemas switch to scored retrieval with bridge and lookup tables (Phase 3).
13. **PostgreSQL / MySQL:** adapters are implemented (URL builder, read-only session, timeouts, dialect for SQLGlot), but only **SQLite is tested here**. The other two will be labelled experimental in the UI and README.

---

## 9. Risks and mitigations

| Risk | Mitigation |
|---|---|
| The LLM verifier rejects correct SQL (false positive) | Its verdict only triggers a correction when severity is high; the evaluation measures the net effect (C vs D) |
| Gold-SQL brittleness (many correct SQL forms) | Result-set accuracy is the main metric; exact match is reported only for reference |
| Relative dates drift | Pinned reference date and fixed data range |
| Evaluation cost | Cost estimate before running; partial runs; caching |
| No API key in the container | Offline tests now; key entered through a secure form in Phase 9, or the evaluation runs on your machine |
| Ephemeral cloud container | Push the project to a Git repository (or download it) at the end of each phase |

---

## 9. Web interface (replaces the original Streamlit front end)

The first interface was a Streamlit app. It was replaced by a plain HTML/CSS/JavaScript interface
served by a small FastAPI app, to make QueryBridge easier for people who open it for the first time
and to give full control over layout, accessibility and safe rendering. The backend did not change:
the server calls the same `QueryPipeline` the evaluator uses.

**Request flow.** The browser sends JSON to `/api/...`. `POST /api/ask` runs the pipeline in a worker
thread and streams newline-delimited JSON: one `step` event per pipeline stage (shown live as a
progress list), then one `result` (or `error`) event. Charts are rendered on demand
(`/api/results/{id}/chart`, Plotly JSON from the same `build_figure` as before).

**State.** `AppState` holds one connected database, its inspected schema, the pipeline and the
history store, and switches databases under a lock. Each browser tab has a random session id
(kept in `localStorage`) that keys its conversation memory, chosen configuration and its last 30
results; switching the database clears every conversation, because earlier SQL no longer applies.

**First-time-user design.** A welcome guide on the first visit (three steps and a safety note);
clickable example questions that are asked straight away; the plain-language answer first, with
table, chart, explanation, SQL and technical detail in tabs; friendly status texts ("Needs a little
more detail", "Read-only: cannot change data"); clarification as buttons plus free text; a
*Your data* page that explains columns in plain words with example values and a clickable map;
clear, actionable messages when the API key, credit or database is missing; light/dark themes; a
phone layout; keyboard navigation, focus management, live regions and visible focus rings.

**Security of the web layer.**

| Threat | Control |
|---|---|
| Another device on the network | Binds to 127.0.0.1 by default (`--host` must be given explicitly, with a warning) |
| DNS rebinding | `TrustedHostMiddleware`: only local host names are accepted |
| Cross-site request forgery | Every non-GET `/api` request needs `X-QueryBridge: 1`; browsers cannot add it cross-site without a CORS preflight, which is never approved |
| Script injection from model or database text | The page renders untrusted text only via `textContent`; strict CSP (`script-src 'self'`, no inline scripts, `frame-ancestors 'none'`, `object-src 'none'`) |
| Spreadsheet formula injection | CSV export prefixes text cells starting with `= + - @` |
| Leaking secrets or internals | Validation errors never echo the request body (it may hold a password); generic 500 messages; masked database URLs only |
| Hostile uploads | 100 MB limit, SQLite header check, saved under a content hash, opened read-only |
| Running unsafe SQL through the web layer | Not possible: every route goes through the backend validator and read-only executor |

**Tests.** `tests/test_web_api.py` covers every endpoint with FastAPI's `TestClient` on the real demo
database and a scripted model. `tests/browser/walkthrough.js` clicks through every page on desktop
and phone sizes in light and dark mode, and fails on any console error, CSP violation or failed
request.
