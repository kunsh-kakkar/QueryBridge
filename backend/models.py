"""
Shared Pydantic models for QueryBridge.

This module is the common type system used by every backend module, so all
data passed between pipeline stages is validated:

* connection and schema models (Phase 3);
* the **LLM output contract**: the structured outputs the model must return
  (Phase 4, enforced by the API through ``messages.parse``);
* prompt-construction models: technique configuration, sections, few-shot
  examples and conversation turns (Phase 4).
"""

from __future__ import annotations

import json
import math
import re
from collections import deque
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator

from utils.helpers import estimate_tokens

ColumnCategory = Literal["integer", "decimal", "text", "date", "datetime", "boolean", "other"]


# =============================================================================
# Connection
# =============================================================================
class ConnectionStatus(BaseModel):
    """Result of a connectivity test (safe to display)."""

    ok: bool
    dialect: str
    message: str
    latency_ms: float | None = None
    server_version: str | None = None
    read_only_enforced: bool = False


# =============================================================================
# Data dictionary (optional human-written descriptions)
# =============================================================================
class TableDescription(BaseModel):
    description: str | None = None
    synonyms: list[str] = Field(default_factory=list)
    columns: dict[str, str] = Field(default_factory=dict)


class DataDictionary(BaseModel):
    """Business descriptions and synonyms that enrich the inspected schema.

    For the demo database this is ``database/data_dictionary.json``; users
    can supply a similar JSON file for their own database.
    """

    database: str | None = None
    description: str | None = None
    tables: dict[str, TableDescription] = Field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path) -> "DataDictionary":
        return cls.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))

    def for_table(self, table: str) -> TableDescription | None:
        lowered = table.lower()
        for name, entry in self.tables.items():
            if name.lower() == lowered:
                return entry
        return None


# =============================================================================
# Schema
# =============================================================================
class ColumnInfo(BaseModel):
    name: str
    data_type: str
    category: ColumnCategory = "other"
    nullable: bool = True
    is_primary_key: bool = False
    foreign_key: str | None = Field(default=None, description="'table.column' this column references")
    default: str | None = None
    description: str | None = None
    sample_values: list[str] = Field(default_factory=list, description="Untrusted DB content")
    distinct_count: int | None = None
    min_value: str | None = None
    max_value: str | None = None


class ForeignKeyInfo(BaseModel):
    columns: list[str]
    referred_table: str
    referred_columns: list[str]
    name: str | None = None


class TableInfo(BaseModel):
    name: str
    is_view: bool = False
    columns: list[ColumnInfo]
    primary_key: list[str] = Field(default_factory=list)
    foreign_keys: list[ForeignKeyInfo] = Field(default_factory=list)
    row_count: int | None = None
    description: str | None = None
    synonyms: list[str] = Field(default_factory=list)

    @property
    def column_names(self) -> list[str]:
        return [c.name for c in self.columns]

    def column(self, name: str) -> ColumnInfo | None:
        lowered = name.lower()
        return next((c for c in self.columns if c.name.lower() == lowered), None)


class Relationship(BaseModel):
    """A foreign-key edge: ``from_table.from_columns -> to_table.to_columns``."""

    model_config = ConfigDict(frozen=True)

    from_table: str
    from_columns: tuple[str, ...]
    to_table: str
    to_columns: tuple[str, ...]

    @property
    def is_self_reference(self) -> bool:
        return self.from_table.lower() == self.to_table.lower()

    def join_condition(self, from_alias: str | None = None, to_alias: str | None = None) -> str:
        left = from_alias or self.from_table
        right = to_alias or self.to_table
        return " AND ".join(
            f"{left}.{fc} = {right}.{tc}" for fc, tc in zip(self.from_columns, self.to_columns)
        )

    def involves(self, table: str) -> bool:
        return table.lower() in (self.from_table.lower(), self.to_table.lower())

    def other(self, table: str) -> str:
        return self.to_table if self.from_table.lower() == table.lower() else self.from_table


class SchemaModel(BaseModel):
    """Structured representation of an inspected database."""

    dialect: str
    database_name: str
    description: str | None = None
    tables: list[TableInfo]
    inspected_at: str
    warnings: list[str] = Field(default_factory=list)

    _by_name: dict[str, TableInfo] | None = PrivateAttr(default=None)

    # --- lookups -------------------------------------------------------------
    def _index(self) -> dict[str, TableInfo]:
        if self._by_name is None:
            self._by_name = {t.name.lower(): t for t in self.tables}
        return self._by_name

    def table(self, name: str) -> TableInfo | None:
        return self._index().get(name.lower())

    def has_table(self, name: str) -> bool:
        return name.lower() in self._index()

    @property
    def table_names(self) -> list[str]:
        return [t.name for t in self.tables]

    @property
    def total_rows(self) -> int:
        return sum(t.row_count or 0 for t in self.tables)

    @property
    def total_columns(self) -> int:
        return sum(len(t.columns) for t in self.tables)

    # --- relationship graph --------------------------------------------------
    @property
    def relationships(self) -> list[Relationship]:
        return [
            Relationship(
                from_table=t.name,
                from_columns=tuple(fk.columns),
                to_table=fk.referred_table,
                to_columns=tuple(fk.referred_columns),
            )
            for t in self.tables
            for fk in t.foreign_keys
        ]

    def relationships_among(self, tables: list[str] | set[str]) -> list[Relationship]:
        wanted = {name.lower() for name in tables}
        return [
            r for r in self.relationships
            if r.from_table.lower() in wanted and r.to_table.lower() in wanted
        ]

    def neighbors(self, table: str) -> set[str]:
        """Tables directly connected to ``table`` by a foreign key (either direction)."""
        return {
            r.other(table) for r in self.relationships
            if r.involves(table) and not r.is_self_reference
        }

    def find_join_path(self, source: str, target: str) -> list[Relationship] | None:
        """Shortest chain of FK relationships connecting two tables (BFS).

        Returns ``[]`` when source == target and ``None`` when they are not
        connected. Self-references are ignored.
        """
        source_key, target_key = source.lower(), target.lower()
        if source_key == target_key:
            return []
        edges: dict[str, list[Relationship]] = {}
        for rel in self.relationships:
            if rel.is_self_reference:
                continue
            edges.setdefault(rel.from_table.lower(), []).append(rel)
            edges.setdefault(rel.to_table.lower(), []).append(rel)
        queue: deque[tuple[str, list[Relationship]]] = deque([(source_key, [])])
        visited = {source_key}
        while queue:
            current, path = queue.popleft()
            for rel in edges.get(current, []):
                nxt = rel.other(current).lower()
                if nxt in visited:
                    continue
                if nxt == target_key:
                    return path + [rel]
                visited.add(nxt)
                queue.append((nxt, path + [rel]))
        return None

    # --- interop -------------------------------------------------------------
    def to_sqlglot_schema(self) -> dict[str, dict[str, str]]:
        """``{table: {column: type}}`` mapping for SQLGlot's qualifier."""
        return {t.name: {c.name: c.data_type for c in t.columns} for t in self.tables}


class SchemaContext(BaseModel):
    """The part of the schema selected for one question (retriever output)."""

    tables: list[TableInfo]
    relationships: list[Relationship]
    selection_mode: Literal["full", "retrieved", "fallback"]
    ranked_tables: list[str] = Field(
        default_factory=list, description="Most relevant tables first (hint for the prompt)"
    )
    scores: dict[str, float] = Field(default_factory=dict)
    reasons: dict[str, list[str]] = Field(default_factory=dict)
    omitted_tables: list[str] = Field(default_factory=list)

    @property
    def table_names(self) -> list[str]:
        return [t.name for t in self.tables]


# =============================================================================
# LLM output contract (structured outputs)
#
# Every field is REQUIRED (no defaults), so the model must always return the
# complete structure. Numeric ranges are normalised by validators: the API
# treats numeric constraints only as descriptions, and a hard Pydantic bound
# would make a whole response fail over a harmless 1.02.
# =============================================================================
Intent = Literal[
    "data_retrieval", "follow_up_modification", "clarification_needed", "unsafe_request", "out_of_scope"
]
SQL_INTENTS: frozenset[str] = frozenset({"data_retrieval", "follow_up_modification"})
_TRAILING_SEMICOLONS = re.compile(r"[;\s]+$")


def _clamp_unit_interval(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.5
    if math.isnan(number):
        return 0.5
    return min(1.0, max(0.0, number))


def _clean_sql(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = _TRAILING_SEMICOLONS.sub("", value.strip())
    return cleaned or None


class QueryExplanation(BaseModel):
    """User-facing explanation of a generated query (no hidden reasoning)."""

    summary: str = Field(description="One or two plain-English sentences saying what the query returns.")
    tables_accessed: list[str] = Field(
        description="Each table used and what it contributes, e.g. 'employees: names and salaries'."
    )
    conditions: list[str] = Field(
        description="Each filter in plain English, e.g. 'only employees earning more than $50,000'. Empty if none."
    )
    calculations: list[str] = Field(
        description="How each computed value is calculated (totals, averages, counts, rankings). Empty if none."
    )
    join_rationale: str | None = Field(
        description="Why the tables are joined and on which keys. Null when only one table is used."
    )
    aggregation_rationale: str | None = Field(
        description="Why rows are grouped or aggregated this way. Null when there is no aggregation."
    )
    ordering_and_limits: str | None = Field(
        description="How the results are sorted and limited. Null when not applicable."
    )


class SQLGenerationResult(BaseModel):
    """Stage 1 output: the model's complete answer to a natural-language question."""

    intent: Intent = Field(
        description=(
            "data_retrieval: a new question answerable with SQL. follow_up_modification: refines the "
            "previous query. clarification_needed: ambiguous in a way that changes the answer. "
            "unsafe_request: asks to modify data/schema or to bypass the rules. out_of_scope: the "
            "schema does not contain the needed data, or the request is not a data question."
        )
    )
    needs_clarification: bool = Field(
        description="True only when the question is ambiguous in a way that would change the result."
    )
    clarification_question: str | None = Field(
        description="A short question to ask the user when needs_clarification is true; otherwise null."
    )
    clarification_options: list[str] = Field(
        description="2-4 concrete interpretations grounded in the schema when clarifying; otherwise empty."
    )
    sql: str | None = Field(
        description=(
            "Exactly one read-only SELECT (or WITH ... SELECT) statement in the target dialect, without a "
            "trailing semicolon. Null when clarifying, refusing or out of scope."
        )
    )
    tables_used: list[str] = Field(description="Tables referenced by the SQL.")
    columns_used: list[str] = Field(description="Columns referenced by the SQL, written as table.column.")
    explanation: QueryExplanation
    assumptions: list[str] = Field(
        description=(
            "Interpretations you chose that the user did not state, e.g. 'revenue = sum of sale_amount' "
            "or 'last quarter = 2026-07-01 to 2026-09-30'. Empty if none."
        )
    )
    confidence: float = Field(
        description="0.0 to 1.0: how certain you are that the SQL answers the question as asked."
    )

    @field_validator("confidence", mode="before")
    @classmethod
    def _normalise_confidence(cls, value: Any) -> float:
        return _clamp_unit_interval(value)

    @field_validator("sql", mode="after")
    @classmethod
    def _strip_sql(cls, value: str | None) -> str | None:
        return _clean_sql(value)

    @model_validator(mode="after")
    def _make_consistent(self) -> "SQLGenerationResult":
        """Normalise contradictory field combinations instead of failing the whole response."""
        if self.intent == "clarification_needed":
            self.needs_clarification = True
        if self.needs_clarification and self.intent in SQL_INTENTS:
            self.intent = "clarification_needed"
        if self.needs_clarification and not (self.clarification_question or "").strip():
            self.clarification_question = "Could you clarify what exactly you would like to see?"
        if not self.needs_clarification:
            self.clarification_question = None
            self.clarification_options = []
        self.clarification_options = [o.strip() for o in self.clarification_options if o.strip()][:4]
        return self

    @property
    def wants_execution(self) -> bool:
        """True when the response proposes SQL that should go through validation and execution."""
        return self.intent in SQL_INTENTS and not self.needs_clarification and self.sql is not None


VerificationCategory = Literal["schema", "join", "filter", "aggregation", "ordering", "logic", "dialect", "safety", "other"]
Severity = Literal["low", "medium", "high"]


class VerificationIssue(BaseModel):
    category: VerificationCategory = Field(description="Which aspect of the query is wrong.")
    severity: Severity = Field(
        description="high: wrong result or invalid SQL; medium: wrong in some cases; low: minor/cosmetic."
    )
    description: str = Field(description="The concrete problem, citing the clause or column involved.")


class VerificationVerdict(BaseModel):
    """Stage 2 output: the semantic self-verification of a candidate query."""

    is_correct: bool = Field(
        description="True when the SQL is valid for this schema and dialect and answers the question."
    )
    matches_intent: bool = Field(
        description="True when the result has the right entities, filters, grouping, ordering and limit."
    )
    issues: list[VerificationIssue] = Field(description="Concrete problems found; empty when there are none.")
    corrected_sql: str | None = Field(
        description="A corrected read-only SELECT when a high-severity issue can be fixed with confidence; else null."
    )
    summary: str = Field(description="One sentence summarising the verdict.")
    confidence: float = Field(description="0.0 to 1.0: certainty of this verdict.")

    @field_validator("confidence", mode="before")
    @classmethod
    def _normalise_confidence(cls, value: Any) -> float:
        return _clamp_unit_interval(value)

    @field_validator("corrected_sql", mode="after")
    @classmethod
    def _strip_sql(cls, value: str | None) -> str | None:
        return _clean_sql(value)

    @property
    def has_blocking_issue(self) -> bool:
        return (not self.is_correct) or (not self.matches_intent) or any(
            issue.severity == "high" for issue in self.issues
        )


class ClauseExplanation(BaseModel):
    clause: str = Field(description="The SQL fragment being explained, e.g. 'WHERE e.salary > 50000'.")
    explanation: str = Field(description="What this fragment does, in plain English.")


class SQLExplanationResult(BaseModel):
    """Output of the query-explanation mode (explain any SQL the user pastes)."""

    summary: str = Field(description="Two or three sentences: what the query returns and how.")
    steps: list[ClauseExplanation] = Field(
        description="Clause-by-clause explanation in logical execution order (FROM/JOIN, WHERE, GROUP BY, "
                    "HAVING, SELECT, ORDER BY, LIMIT)."
    )
    tables_used: list[str] = Field(description="Tables the query reads.")
    columns_used: list[str] = Field(description="Columns the query references, as table.column.")
    potential_issues: list[str] = Field(
        description="Possible problems (NULL handling, duplicate rows from joins, performance, dialect). Empty if none."
    )
    modifies_data: bool = Field(description="True if the statement would change data, schema or settings.")


# =============================================================================
# Prompt construction
# =============================================================================
class PromptConfig(BaseModel):
    """Which prompt-engineering techniques are active.

    The UI and the evaluator use the same pipeline; only these flags differ
    between configurations A/B/C/D.
    """

    model_config = ConfigDict(frozen=True)

    name: str = "C"
    label: str = "Full pipeline"
    description: str = ""
    use_baseline_prompt: bool = False
    use_role_prompt: bool = True
    use_rules: bool = True
    use_dialect_notes: bool = True
    use_schema_context: bool = True
    include_descriptions: bool = True
    include_sample_values: bool = True
    use_few_shot: bool = True
    few_shot_k: int = Field(default=4, ge=0, le=10)
    few_shot_strategy: Literal["dynamic", "static"] = "dynamic"
    use_conversation_context: bool = True
    use_static_verification: bool = True
    use_semantic_verification: bool = True
    use_error_correction: bool = True
    max_correction_attempts: int = Field(default=2, ge=0, le=3)

    def techniques(self) -> list[str]:
        """Human-readable list of the active techniques (for reports and the UI)."""
        if self.use_baseline_prompt:
            active = ["Basic instruction"]
        else:
            active = [label for flag, label in [
                (self.use_role_prompt, "Role prompting"),
                (self.use_rules, "Constraints & safety rules"),
                (self.use_dialect_notes, "Dialect conditioning"),
                (self.use_schema_context, "Schema context"),
            ] if flag]
        if self.use_few_shot and self.few_shot_k > 0:
            active.append(f"Few-shot ({self.few_shot_strategy}, k={self.few_shot_k})")
        if self.use_conversation_context:
            active.append("Conversation memory")
        active.append("Structured output")
        if self.use_static_verification:
            active.append("Static SQL verification")
        if self.use_semantic_verification:
            active.append("LLM self-verification")
        if self.use_error_correction and self.max_correction_attempts > 0:
            active.append(f"Error correction (max {self.max_correction_attempts})")
        return active


class FewShotExample(BaseModel):
    """One demonstration in the few-shot pool (written on a separate schema)."""

    model_config = ConfigDict(frozen=True)

    id: str
    question: str
    intent: Intent = "data_retrieval"
    sql: str | None = None
    explanation: str
    tags: frozenset[str]
    trigger_tags: frozenset[str] = Field(
        default_factory=frozenset,
        description="If set, the example is only eligible when the question shows one of these tags.",
    )
    assumptions: tuple[str, ...] = ()
    clarification_question: str | None = None
    clarification_options: tuple[str, ...] = ()
    previous_question: str | None = None
    previous_sql: str | None = None
    reference_date: str | None = None
    dialect_sql: tuple[tuple[str, str], ...] = Field(
        default=(),
        description="Hand-written (dialect, sql) overrides where automatic transpilation is not reliable.",
    )

    def sql_override(self, dialect: str) -> str | None:
        return dict(self.dialect_sql).get(dialect)

    @property
    def is_behavior_example(self) -> bool:
        """Clarification / refusal / missing-data demonstrations (no SQL)."""
        return self.sql is None


class SelectedExample(BaseModel):
    example: FewShotExample
    score: float
    matched_tags: list[str]
    reason: str


class ConversationTurn(BaseModel):
    """What memory keeps about a previous turn. Never the result rows."""

    question: str
    intent: str | None = None
    sql: str | None = None
    tables_used: list[str] = Field(default_factory=list)
    result_columns: list[str] = Field(default_factory=list)
    row_count: int | None = None
    status: Literal["success", "empty", "clarification", "refused", "error"] = "success"
    clarification_question: str | None = None


PromptStage = Literal["generation", "verification", "correction", "explanation"]


class PromptSection(BaseModel):
    """A labelled piece of a prompt, kept separate for inspection and caching."""

    key: str
    title: str
    technique: str
    location: Literal["system", "user"]
    text: str
    cache_breakpoint: bool = False

    @property
    def token_estimate(self) -> int:
        return estimate_tokens(self.text)


class BuiltPrompt(BaseModel):
    """A fully assembled prompt plus the metadata the Prompt Lab displays."""

    stage: PromptStage
    config_name: str
    sections: list[PromptSection]
    selected_examples: list[SelectedExample] = Field(default_factory=list)
    schema_tables: list[str] = Field(default_factory=list)
    schema_mode: str | None = None

    def section(self, key: str) -> PromptSection | None:
        return next((s for s in self.sections if s.key == key), None)

    def has_section(self, key: str) -> bool:
        return self.section(key) is not None

    @property
    def system_sections(self) -> list[PromptSection]:
        return [s for s in self.sections if s.location == "system"]

    @property
    def user_sections(self) -> list[PromptSection]:
        return [s for s in self.sections if s.location == "user"]

    @property
    def system_text(self) -> str:
        return "\n\n".join(s.text for s in self.system_sections)

    @property
    def user_text(self) -> str:
        return "\n\n".join(s.text for s in self.user_sections)

    def render(self) -> str:
        """The whole prompt as one readable document (Prompt Lab / debugging)."""
        parts = []
        for s in self.sections:
            marker = "  [cache breakpoint]" if s.cache_breakpoint else ""
            parts.append(f"===== {s.location.upper()} · {s.title} ({s.technique}){marker} =====\n{s.text}")
        return "\n\n".join(parts)

    @property
    def total_token_estimate(self) -> int:
        return sum(s.token_estimate for s in self.sections)


# =============================================================================
# LLM call accounting
# =============================================================================
class LLMCallRecord(BaseModel):
    """One API call, recorded for the pipeline trace, the UI and the evaluation."""

    stage: PromptStage
    model: str
    served_model: str | None = None
    effort: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    latency_ms: float = 0.0
    stop_reason: str | None = None
    request_id: str | None = None
    fallback_used: bool = False
    estimated_cost_usd: float | None = None

    @property
    def total_input_tokens(self) -> int:
        return self.input_tokens + self.cache_creation_input_tokens + self.cache_read_input_tokens

    @property
    def cache_hit_ratio(self) -> float:
        total = self.total_input_tokens
        return self.cache_read_input_tokens / total if total else 0.0
