"""
Turn backend objects into plain JSON for the browser.

Everything returned here is data: the front end inserts it with
``textContent`` only, never as HTML, because answers, SQL, explanations and
cell values come from the LLM or the database and are untrusted.
Credentials never appear (the database URL is always the masked form).
"""

from __future__ import annotations

import math
import re
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

import pandas as pd
import sqlglot

from backend.history_store import HistoryEntry
from backend.models import BuiltPrompt, LLMCallRecord, PromptConfig, SchemaModel, TableInfo
from backend.pipeline import PipelineResult
from backend.sql_validator import SQLGLOT_DIALECTS, ValidationReport

# Plain-language labels shown to people who have never used the app.
STATUS_TEXT: dict[str, str] = {
    "success": "Answered",
    "empty": "No matching rows",
    "clarification": "Needs a little more detail",
    "refused": "Read-only: cannot change data",
    "out_of_scope": "Not in this database",
    "blocked": "Blocked for safety",
    "failed": "Could not answer",
    "error": "Something went wrong",
}
STATUS_TONE: dict[str, str] = {
    "success": "good", "empty": "neutral", "clarification": "info", "refused": "warn",
    "out_of_scope": "warn", "blocked": "bad", "failed": "bad", "error": "bad",
}
CATEGORY_TEXT: dict[str, str] = {
    "integer": "Whole number", "decimal": "Number", "text": "Text", "date": "Date",
    "datetime": "Date and time", "boolean": "Yes / no", "other": "Other",
}
DIALECT_NAMES = {"sqlite": "SQLite", "postgresql": "PostgreSQL", "mysql": "MySQL"}


# ------------------------------------------------------------------ values
def jsonable(value: Any) -> Any:
    """A JSON-safe version of one cell value."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        # JavaScript numbers are exact only up to 2**53; keep larger integers exact as text.
        return value if abs(value) <= 2 ** 53 else str(value)
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Decimal):
        return float(value) if value.is_finite() else None
    if isinstance(value, pd.Timestamp):
        if pd.isna(value):
            return None
        return value.date().isoformat() if value == value.normalize() else value.isoformat(sep=" ")
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, (date, time)):
        return value.isoformat()
    if isinstance(value, (timedelta, pd.Timedelta)):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<binary data, {len(bytes(value))} bytes>"
    if isinstance(value, str):
        return value
    item = getattr(value, "item", None)  # numpy scalars
    if callable(item):
        try:
            return jsonable(item())
        except (TypeError, ValueError):
            pass
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return str(value)


def frame_rows(frame: pd.DataFrame) -> list[list[Any]]:
    return [[jsonable(v) for v in row] for row in frame.itertuples(index=False, name=None)]


# ------------------------------------------------------------------ pieces
def config_json(config: PromptConfig) -> dict[str, Any]:
    return {"name": config.name, "label": config.label, "description": config.description,
            "techniques": config.techniques()}


def validation_json(report: ValidationReport | None) -> dict[str, Any] | None:
    if report is None:
        return None
    return {
        "is_safe": report.is_safe, "is_blocked": report.is_blocked, "messages": report.messages,
        "warnings": list(report.warnings), "statement_type": report.statement_type,
        "referenced_tables": list(report.referenced_tables), "limit_added": report.limit_added,
        "applied_limit": report.applied_limit,
    }


def llm_call_json(call: LLMCallRecord) -> dict[str, Any]:
    return {
        "stage": call.stage, "model": call.served_model or call.model, "effort": call.effort,
        "input_tokens": call.input_tokens, "cache_read_tokens": call.cache_read_input_tokens,
        "cache_write_tokens": call.cache_creation_input_tokens, "output_tokens": call.output_tokens,
        "latency_ms": round(call.latency_ms, 1), "estimated_cost_usd": call.estimated_cost_usd,
        "fallback_used": call.fallback_used, "stop_reason": call.stop_reason,
    }


def prompt_json(prompt: BuiltPrompt) -> dict[str, Any]:
    return {
        "stage": prompt.stage, "config_name": prompt.config_name,
        "token_estimate": prompt.total_token_estimate, "schema_mode": prompt.schema_mode,
        "schema_tables": list(prompt.schema_tables),
        "sections": [{"key": s.key, "title": s.title, "technique": s.technique, "location": s.location,
                      "tokens": s.token_estimate, "cache_breakpoint": s.cache_breakpoint, "text": s.text}
                     for s in prompt.sections],
        "examples": [{"id": s.example.id, "question": s.example.question, "score": round(s.score, 3),
                      "matched": list(s.matched_tags), "reason": s.reason}
                     for s in reversed(prompt.selected_examples)],
    }


def translations(sql: str | None, dialect: str) -> dict[str, str]:
    """The executed SQL shown in the other dialects (for reference only; never executed)."""
    if not sql or dialect not in SQLGLOT_DIALECTS:
        return {}
    out: dict[str, str] = {}
    for target in SQLGLOT_DIALECTS:
        if target == dialect:
            continue
        try:
            out[DIALECT_NAMES[target]] = sqlglot.transpile(sql, read=SQLGLOT_DIALECTS[dialect],
                                                           write=SQLGLOT_DIALECTS[target], pretty=True)[0]
        except sqlglot.errors.SqlglotError:
            continue
    return out


def _kpi(result: PipelineResult) -> dict[str, Any] | None:
    formatted = result.formatted
    if formatted is None or not formatted.is_scalar:
        return None
    column = formatted.dataframe.columns[0]
    profile = formatted.profile(column)
    return {"label": str(column).replace("_", " ").strip().capitalize(),
            "value": jsonable(formatted.dataframe.iloc[0, 0]),
            "kind": profile.kind if profile else "text"}


# ------------------------------------------------------------------ results
def result_json(result: PipelineResult, result_id: str, *, dialect: str) -> dict[str, Any]:
    formatted = result.formatted
    table = None
    if formatted is not None:
        frame = formatted.dataframe
        kinds = {p.name: p.kind for p in formatted.profiles}
        table = {
            "columns": [{"name": str(c), "kind": kinds.get(str(c), "text")} for c in frame.columns],
            "rows": frame_rows(frame),
            "row_count": formatted.row_count,
            "truncated": formatted.truncated,
            "max_rows": result.execution.max_rows if result.execution else None,
            "execution_time_ms": round(formatted.execution_time_ms, 2),
        }
    explanation = None
    if result.explanation is not None:
        e = result.explanation
        explanation = {
            "summary": e.summary, "tables_accessed": list(e.tables_accessed), "conditions": list(e.conditions),
            "calculations": list(e.calculations), "join_rationale": e.join_rationale,
            "aggregation_rationale": e.aggregation_rationale, "ordering_and_limits": e.ordering_and_limits,
        }
    generated_sql = result.generation.sql if result.generation else None
    executed = result.sql if result.ok else None
    return {
        "id": result_id,
        "question": result.question,
        "status": result.status,
        "status_text": STATUS_TEXT.get(result.status, result.status),
        "tone": STATUS_TONE.get(result.status, "neutral"),
        "message": result.message,
        "ok": result.ok,
        "config_name": result.config_name,
        "is_follow_up": result.is_follow_up,
        "kpi": _kpi(result),
        "table": table,
        "summary_line": formatted.summary if formatted is not None else None,
        "charts": [{"index": i, "type": c.chart_type, "title": c.title, "reason": c.reason, "y": c.y,
                    "measures": list(c.measures)} for i, c in enumerate(result.charts) if c.chart_type != "kpi"],
        "sql": result.pretty_sql or result.sql,
        "executed_sql": executed,
        "generated_sql": generated_sql if generated_sql and generated_sql.strip() != (result.sql or "").strip()
        else None,
        "dialect": DIALECT_NAMES.get(dialect, dialect),
        "row_limit_added": bool(result.validation and result.validation.limit_added),
        "translations": translations(executed, dialect),
        "explanation": explanation,
        "assumptions": list(result.generation.assumptions) if result.generation else [],
        "tables_used": result.tables_used if result.ok else [],
        "columns_used": result.columns_used if result.ok else [],
        "confidence": result.confidence,
        "confidence_label": result.confidence_label,
        "complexity": ({"level": result.complexity.level, "score": result.complexity.score,
                        "reasons": list(result.complexity.reasons)} if result.complexity else None),
        "corrections": result.refinement.corrections_used if result.refinement else 0,
        "clarification": ({"question": result.clarification_question or result.message,
                           "options": list(result.clarification_options)}
                          if result.status == "clarification" else None),
        "warnings": list(result.warnings),
        "problems": list(result.problems),
        "llm_calls": len(result.llm_calls),
        "tokens": {"input": result.total_tokens[0], "output": result.total_tokens[1]},
        "estimated_cost_usd": result.estimated_cost,
        "total_ms": result.trace.total_ms,
        "history_id": result.history_id,
    }


def details_json(result: PipelineResult) -> dict[str, Any]:
    """The 'behind the scenes' view: every step, prompt, model call and check."""
    verification = None
    if result.verification is not None:
        v = result.verification
        semantic = None
        if v.semantic is not None:
            semantic = {"blocking": v.semantic.has_blocking_issue, "confidence": v.semantic.confidence,
                        "summary": v.semantic.summary,
                        "issues": [{"severity": i.severity, "category": i.category, "description": i.description}
                                   for i in v.semantic.issues]}
        verification = {
            "checks": [{"name": c.name, "passed": c.passed, "severity": c.severity, "message": c.message}
                       for c in v.checks],
            "semantic": semantic, "semantic_skipped_reason": v.semantic_skipped_reason,
            "explain_plan": list(v.explain_plan),
        }
    attempts = []
    if result.refinement is not None:
        attempts = [{"number": a.number, "source": a.source, "outcome": a.outcome, "problems": list(a.problems),
                     "sql": a.sql, "executed_sql": a.executed_sql} for a in result.refinement.attempts]
    context = None
    if result.schema_context is not None:
        c = result.schema_context
        context = {"mode": c.selection_mode, "tables": c.table_names, "ranked": list(c.ranked_tables),
                   "omitted": list(c.omitted_tables)}
    return {
        "steps": [{"name": s.name, "status": s.status, "duration_ms": s.duration_ms, "detail": s.detail}
                  for s in result.trace.steps],
        "total_ms": result.trace.total_ms,
        "llm_calls": [llm_call_json(c) for c in result.llm_calls],
        "prompts": [prompt_json(p) for p in result.prompts],
        "verification": verification,
        "validation": validation_json(result.validation),
        "attempts": attempts,
        "schema_context": context,
        "problems": list(result.problems),
    }


# ------------------------------------------------------------------ schema
def _column_json(column) -> dict[str, Any]:
    if column.sample_values:
        values = list(column.sample_values)
        value_note = None
    else:
        values = []
        value_note = None
        if column.min_value is not None:
            value_note = f"{column.min_value} to {column.max_value}"
        elif column.distinct_count is not None:
            value_note = f"{column.distinct_count:,} different values"
    return {
        "name": column.name, "type": column.data_type, "category": column.category,
        "friendly_type": CATEGORY_TEXT.get(column.category, "Other"),
        "nullable": column.nullable, "primary_key": column.is_primary_key, "foreign_key": column.foreign_key,
        "description": column.description, "examples": values, "value_note": value_note,
    }


def table_json(table: TableInfo, schema: SchemaModel) -> dict[str, Any]:
    links = []
    for rel in schema.relationships:
        if rel.from_table.lower() == table.name.lower():
            links.append({"direction": "out", "table": rel.to_table, "columns": list(rel.from_columns),
                          "target_columns": list(rel.to_columns), "condition": rel.join_condition()})
        elif rel.to_table.lower() == table.name.lower():
            links.append({"direction": "in", "table": rel.from_table, "columns": list(rel.from_columns),
                          "target_columns": list(rel.to_columns), "condition": rel.join_condition()})
    return {
        "name": table.name, "is_view": table.is_view, "row_count": table.row_count,
        "description": table.description, "synonyms": list(table.synonyms),
        "primary_key": list(table.primary_key), "columns": [_column_json(c) for c in table.columns],
        "links": links,
    }


def schema_json(schema: SchemaModel, *, label: str, dictionary_name: str | None) -> dict[str, Any]:
    return {
        "label": label, "dialect": DIALECT_NAMES.get(schema.dialect, schema.dialect),
        "database_name": schema.database_name, "description": schema.description,
        "inspected_at": schema.inspected_at, "warnings": list(schema.warnings),
        "dictionary_name": dictionary_name,
        "totals": {"tables": len(schema.tables), "columns": schema.total_columns, "rows": schema.total_rows,
                   "relationships": len(schema.relationships)},
        "tables": [table_json(t, schema) for t in schema.tables],
        "relationships": [{"from": r.from_table, "to": r.to_table, "columns": list(r.from_columns)}
                          for r in schema.relationships],
    }


# ------------------------------------------------------------------ history
_CLARIFIED = re.compile(r"^(?P<question>.*?) \(clarification - (?P<asked>.*) -> (?P<answer>.*)\)$", re.DOTALL)


def split_clarified(question: str) -> tuple[str, str | None]:
    """'Q (clarification - asked -> answer)' as stored by the pipeline -> ('Q', 'answer')."""
    match = _CLARIFIED.match(question)
    return (match["question"], match["answer"]) if match else (question, None)


def history_json(entry: HistoryEntry) -> dict[str, Any]:
    question, clarified = split_clarified(entry.question)
    return {
        "id": entry.id, "created_at": entry.created_at, "database": entry.database_label,
        "question": question, "clarified_with": clarified, "sql": entry.sql, "status": entry.status,
        "status_text": STATUS_TEXT.get(entry.status, entry.status), "tone": STATUS_TONE.get(entry.status, "neutral"),
        "row_count": entry.row_count, "execution_time_ms": entry.execution_time_ms, "config_name": entry.config_name,
        "confidence": entry.confidence, "complexity": entry.complexity, "corrections": entry.corrections,
        "llm_calls": entry.llm_calls, "estimated_cost_usd": entry.estimated_cost, "message": entry.message,
    }
