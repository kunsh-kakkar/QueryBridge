# v1 - Basic prompt (configuration A)

Starting point: a single instruction with no role, rules, schema or examples. This is the zero-shot baseline from Milestone 1 (without the hand-written schema).

Step: P1 SQL generation. Model: Gemini 3.5 Flash-Lite (`gemini-3.5-flash-lite`), JSON output.

```text
===== SYSTEM · Basic instruction (Baseline (no prompt engineering))  [cache breakpoint] =====
Convert the user's question into a SQLite SQL query.

===== USER · Context (Temporal & retrieval context) =====
<context>
Reference date (today): {reference_date}
SQL dialect: SQLite
</context>

===== USER · User question (Delimited untrusted input) =====
<question>
{question}
</question>

Answer with the required JSON structure.
```
