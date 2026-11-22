"""
Prompt construction: modular, configurable and inspectable.

``PromptManager`` assembles each stage's prompt from separate sections, so
every prompt-engineering technique can be switched on or off by
``PromptConfig`` and shown in the Prompt Lab:

    SYSTEM  [S1 role] [S2 rules] [S3 dialect notes] [S4 schema context] <- cache breakpoint
    USER    [S5 few-shot examples] [S6 conversation history] [S7 context] [S8 question]

Design decisions
----------------
* **Stable prefix, volatile suffix.** Everything that is the same for every
  question about a database (role, rules, dialect, full schema) is in the
  system prompt, rendered deterministically, with a cache breakpoint after
  the schema. Everything that changes per question (selected examples,
  history, reference date, retrieval hint, question) comes after it. In
  retrieval mode the schema differs per question, so the breakpoint moves to
  the last stable section.
* **Delimited untrusted input.** The question, history and database values
  are wrapped in XML-style tags. Tag-like ``<`` sequences inside them are
  neutralised, so content cannot close a tag and pose as instructions.
* **Same system prompt for correction.** The correction stage reuses the
  generation system prompt byte for byte, so it hits the prompt cache.
* **Presets A/B/C/D** are defined here so the UI and the evaluator use
  exactly the same definitions.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from datetime import date
from functools import lru_cache

import sqlglot

from backend.example_selector import ExampleSelector, looks_like_follow_up
from backend.models import (
    BuiltPrompt,
    ColumnInfo,
    ConversationTurn,
    FewShotExample,
    PromptConfig,
    PromptSection,
    Relationship,
    SchemaContext,
    SchemaModel,
    SelectedExample,
    TableInfo,
)
from prompts.baseline_prompt import baseline_prompt
from prompts.correction_prompt import CORRECTION_INSTRUCTIONS, failed_attempt_block
from prompts.explanation_prompt import ExplanationLevel, explainer_role, explanation_instructions
from prompts.few_shot_examples import BOOKSTORE_DDL
from prompts.system_prompt import RESPONSE_REMINDER, RULES, dialect_display_name, dialect_notes, role_prompt
from prompts.verification_prompt import VERIFICATION_TASK, verification_checklist, verifier_role
from utils.helpers import truncate_text

# =============================================================================
# Technique presets (evaluation configurations)
# =============================================================================
PRESETS: dict[str, PromptConfig] = {
    "A": PromptConfig(
        name="A", label="Basic prompt",
        description="A single instruction. No role, rules, schema context or examples; no verification.",
        use_baseline_prompt=True, use_role_prompt=False, use_rules=False, use_dialect_notes=False,
        use_schema_context=False, include_descriptions=False, include_sample_values=False,
        use_few_shot=False, few_shot_k=0, use_static_verification=False,
        use_semantic_verification=False, use_error_correction=False, max_correction_attempts=0,
    ),
    "B": PromptConfig(
        name="B", label="Role + schema",
        description="Role prompt, rules, dialect notes and the full schema context. No examples, no verification.",
        use_few_shot=False, few_shot_k=0, use_static_verification=False,
        use_semantic_verification=False, use_error_correction=False, max_correction_attempts=0,
    ),
    "D": PromptConfig(
        name="D", label="Role + schema + few-shot",
        description="Configuration B plus context-aware few-shot examples (ablation: no verification).",
        use_static_verification=False, use_semantic_verification=False,
        use_error_correction=False, max_correction_attempts=0,
    ),
    "C": PromptConfig(
        name="C", label="Full pipeline",
        description="Role, rules, schema, few-shot, static + LLM verification and bounded error correction.",
    ),
}
EVALUATION_ORDER: tuple[str, ...] = ("A", "B", "D", "C")


def get_preset(name: str) -> PromptConfig:
    try:
        return PRESETS[name.upper()]
    except KeyError:
        raise ValueError(f"Unknown configuration '{name}'. Choose one of {', '.join(PRESETS)}.") from None


def preset_from_settings(settings, name: str = "C") -> PromptConfig:
    """A preset adjusted by the user's settings (k, correction budget, LLM verification)."""
    preset = get_preset(name)
    if preset.use_baseline_prompt:
        return preset
    updates: dict[str, object] = {}
    if preset.use_few_shot:
        updates["few_shot_k"] = settings.few_shot_k
        updates["use_few_shot"] = settings.few_shot_k > 0
    if preset.use_error_correction:
        updates["max_correction_attempts"] = settings.max_correction_attempts
    if preset.use_semantic_verification:
        updates["use_semantic_verification"] = settings.enable_semantic_verification
    updates["include_sample_values"] = preset.include_sample_values and settings.include_sample_values
    return preset.model_copy(update=updates)


# =============================================================================
# Rendering helpers
# =============================================================================
_TAG_LIKE = re.compile(r"<(?=/?[A-Za-z!?])")


def neutralize_tags(text: str) -> str:
    """Neutralise tag-like '<' in untrusted text so it cannot open or close prompt tags.

    Only ``<`` followed by a letter, '/', '!' or '?' changes ('a < b' is kept),
    and '&' is left alone, so values such as 'Research & Development' reach
    the model exactly as stored and can be copied into SQL literals.
    """
    return _TAG_LIKE.sub("&lt;", text)


def _attr(value: str) -> str:
    return value.replace('"', "'").replace("<", "").replace(">", "")


def _column_definition(column: ColumnInfo, single_pk: bool) -> str:
    parts = [column.name, column.data_type]
    if column.is_primary_key and single_pk:
        parts.append("PRIMARY KEY")
    elif not column.nullable:
        parts.append("NOT NULL")
    if column.foreign_key:
        table, _, target = column.foreign_key.partition(".")
        parts.append(f"REFERENCES {table}({target})")
    return " ".join(parts)


def _column_comment(column: ColumnInfo, include_descriptions: bool, include_profile: bool) -> str:
    notes: list[str] = []
    if include_descriptions and column.description:
        notes.append(column.description.rstrip("."))
    if include_profile:
        if column.min_value is not None and column.max_value is not None:
            notes.append(f"Range: {neutralize_tags(column.min_value)} to {neutralize_tags(column.max_value)}")
        if column.sample_values:
            complete = column.distinct_count is not None and column.distinct_count <= len(column.sample_values)
            label = "Values" if complete else "Example values"
            values = " | ".join(neutralize_tags(v) for v in column.sample_values)
            notes.append(f"{label}: <data>{values}</data>")
    return "; ".join(notes)


def render_table(table: TableInfo, *, include_descriptions: bool = True, include_profile: bool = True) -> str:
    kind = "VIEW" if table.is_view else "TABLE"
    header = f"-- {table.name}"
    if table.row_count is not None:
        header += f": {table.row_count:,} rows"
    if table.is_view:
        header += " (view)"
    if include_descriptions and table.description:
        header += f". {table.description}"
    single_pk = len(table.primary_key) == 1
    items: list[tuple[str, str]] = [
        (_column_definition(c, single_pk), _column_comment(c, include_descriptions, include_profile))
        for c in table.columns
    ]
    if len(table.primary_key) > 1:
        items.append((f"PRIMARY KEY ({', '.join(table.primary_key)})", ""))
    lines = [header, f"CREATE {kind} {table.name} ("]
    for i, (definition, comment) in enumerate(items):
        separator = "," if i < len(items) - 1 else ""
        lines.append(f"  {definition}{separator}" + (f"  -- {comment}" if comment else ""))
    lines.append(");")
    return "\n".join(lines)


def junction_tables(tables: Sequence[TableInfo]) -> dict[str, tuple[str, ...]]:
    """Tables whose composite key is made of foreign keys to 2+ tables (many-to-many links)."""
    found: dict[str, tuple[str, ...]] = {}
    for table in tables:
        fk_columns = {col for fk in table.foreign_keys for col in fk.columns}
        targets = tuple(dict.fromkeys(fk.referred_table for fk in table.foreign_keys))
        if len(table.primary_key) > 1 and set(table.primary_key) <= fk_columns and len(targets) >= 2:
            found[table.name] = targets
    return found


def render_relationships(relationships: Sequence[Relationship], tables: Sequence[TableInfo]) -> str:
    if not relationships:
        return "Relationships: none documented between the tables shown."
    lines = ["Relationships (use these join conditions):"]
    for rel in relationships:
        condition = rel.join_condition()
        if rel.is_self_reference:
            lines.append(f"- {condition}  (self-reference: join {rel.from_table} to itself using two aliases)")
        else:
            lines.append(f"- {condition}  (many {rel.from_table} rows to one {rel.to_table} row)")
    for name, targets in junction_tables(tables).items():
        lines.append(f"- {name} is a junction table linking {' and '.join(targets)} (many-to-many).")
    return "\n".join(lines)


def render_schema(
    context: SchemaContext,
    *,
    dialect: str,
    database_name: str,
    description: str | None,
    total_tables: int,
    include_descriptions: bool = True,
    include_profile: bool = True,
) -> str:
    lines = [f'<database_schema dialect="{dialect_display_name(dialect)}" database="{_attr(database_name)}">']
    if include_descriptions and description:
        lines.append(f"About this database: {description}")
    if context.selection_mode != "full":
        lines.append(f"Only the {len(context.tables)} tables most relevant to this question are shown "
                     f"({len(context.tables)} of {total_tables}).")
    lines.append("")
    for table in context.tables:
        lines.append(render_table(table, include_descriptions=include_descriptions, include_profile=include_profile))
        lines.append("")
    lines.append(render_relationships(context.relationships, context.tables))
    lines.append("</database_schema>")
    return "\n".join(lines)


_SQLGLOT_DIALECTS = {"sqlite": "sqlite", "postgresql": "postgres", "mysql": "mysql"}


@lru_cache(maxsize=256)
def transpile_example_sql(sql: str, dialect: str) -> str:
    """Translate an example (written in SQLite) to the target dialect; keep it unchanged on failure."""
    target = _SQLGLOT_DIALECTS.get(dialect)
    if target is None or target == "sqlite":
        return sql
    try:
        return sqlglot.transpile(sql, read="sqlite", write=target)[0]
    except (sqlglot.errors.ParseError, sqlglot.errors.UnsupportedError, IndexError):
        return sql


# SQLite-only constructs that must never reach a prompt for another dialect.
SQLITE_ONLY_SYNTAX = re.compile(r"\b(date|datetime|time)\(\s*'[^']*'\s*,\s*'[+-]|\bjulianday\(|\bstrftime\(",
                                re.IGNORECASE)


def example_sql_for_dialect(example: FewShotExample, dialect: str) -> str | None:
    """The example's SQL in the target dialect (hand-written override, else transpiled)."""
    if example.sql is None:
        return None
    return example.sql_override(dialect) or transpile_example_sql(example.sql, dialect)


def render_example(example: FewShotExample, dialect: str) -> str:
    response: dict[str, object] = {"intent": example.intent}
    if example.sql is None:
        response["needs_clarification"] = example.intent == "clarification_needed"
        if example.clarification_question:
            response["clarification_question"] = example.clarification_question
            response["clarification_options"] = list(example.clarification_options)
        response["sql"] = None
    else:
        response["sql"] = example_sql_for_dialect(example, dialect)
    response["explanation"] = example.explanation
    response["assumptions"] = list(example.assumptions)
    lines = [f'<example id="{example.id}">']
    if example.reference_date:
        lines.append(f"<reference_date>{example.reference_date}</reference_date>")
    if example.previous_question:
        lines.append(f"<previous_question>{example.previous_question}</previous_question>")
        if example.previous_sql:
            lines.append(f"<previous_sql>{transpile_example_sql(example.previous_sql, dialect)}</previous_sql>")
    lines.append(f"<question>{example.question}</question>")
    lines.append(f"<response>{json.dumps(response, ensure_ascii=False)}</response>")
    lines.append("</example>")
    return "\n".join(lines)


def render_examples(selected: Sequence[SelectedExample], dialect: str) -> str:
    body = "\n\n".join(render_example(s.example, dialect) for s in selected)
    return (
        "<examples>\n"
        "These examples use a DIFFERENT database (a bookstore). They only demonstrate SQL patterns and "
        "the response style (responses are abridged). Never use their tables or columns in your answer; "
        "use only <database_schema>.\n"
        f"<example_schema>\n{BOOKSTORE_DDL}\n</example_schema>\n\n"
        f"{body}\n"
        "</examples>"
    )


def render_history(turns: Sequence[ConversationTurn]) -> str:
    lines = [
        "<conversation_history>",
        "Earlier turns of this conversation, oldest first. Use them only to resolve references in the "
        "new question.",
    ]
    for number, turn in enumerate(turns, 1):
        lines.append(f'<turn number="{number}">')
        lines.append(f"question: {neutralize_tags(truncate_text(turn.question, 500))}")
        if turn.status == "clarification" and turn.clarification_question:
            lines.append(f"assistant asked for clarification: {neutralize_tags(turn.clarification_question)}")
        elif turn.status == "refused":
            lines.append("result: refused (not a read-only request), no SQL")
        if turn.sql:
            lines.append(f"sql: {turn.sql}")
        if turn.status in ("success", "empty"):
            columns = ", ".join(turn.result_columns) or "unknown"
            lines.append(f"result: {turn.row_count if turn.row_count is not None else '?'} rows; columns: {columns}")
        elif turn.status == "error":
            lines.append("result: the query failed")
        lines.append("</turn>")
    lines.append("</conversation_history>")
    return "\n".join(lines)


# =============================================================================
# Prompt manager
# =============================================================================
class PromptManager:
    """Builds the prompts for generation, verification, correction and explanation."""

    def __init__(
        self,
        schema: SchemaModel,
        *,
        selector: ExampleSelector | None = None,
        reference_date: date | None = None,
        max_history_turns: int = 5,
        dialect: str | None = None,
    ) -> None:
        self.schema = schema
        self.dialect = dialect or schema.dialect
        self.selector = selector or ExampleSelector()
        self._reference_date = reference_date
        self.max_history_turns = max_history_turns
        self._full_context = SchemaContext(
            tables=list(schema.tables), relationships=schema.relationships, selection_mode="full"
        )
        self._schema_cache: dict[tuple, str] = {}

    @property
    def reference_date(self) -> date:
        return self._reference_date or date.today()

    # ------------------------------------------------------------ sections
    def _schema_text(self, context: SchemaContext, config: PromptConfig) -> str:
        key = (tuple(context.table_names), context.selection_mode,
               config.include_descriptions, config.include_sample_values)
        if key not in self._schema_cache:
            self._schema_cache[key] = render_schema(
                context,
                dialect=self.dialect,
                database_name=self.schema.database_name,
                description=self.schema.description,
                total_tables=len(self.schema.tables),
                include_descriptions=config.include_descriptions,
                include_profile=config.include_sample_values,
            )
        return self._schema_cache[key]

    def _generation_system(self, config: PromptConfig, context: SchemaContext) -> list[PromptSection]:
        if config.use_baseline_prompt:
            return [PromptSection(key="instruction", title="Basic instruction",
                                  technique="Baseline (no prompt engineering)", location="system",
                                  text=baseline_prompt(self.dialect), cache_breakpoint=True)]
        sections: list[PromptSection] = []
        if config.use_role_prompt:
            sections.append(PromptSection(key="role", title="Role", technique="Role prompting",
                                          location="system", text=role_prompt(self.dialect)))
        if config.use_rules:
            sections.append(PromptSection(key="rules", title="Rules", technique="Constraints & safety rules",
                                          location="system", text=RULES))
        if config.use_dialect_notes:
            sections.append(PromptSection(key="dialect", title="Dialect notes", technique="Dialect conditioning",
                                          location="system", text=dialect_notes(self.dialect)))
        if config.use_schema_context:
            sections.append(PromptSection(key="schema", title="Database schema", technique="Contextual prompting",
                                          location="system", text=self._schema_text(context, config)))
        self._place_cache_breakpoint(sections, context)
        return sections

    @staticmethod
    def _place_cache_breakpoint(sections: list[PromptSection], context: SchemaContext) -> None:
        """Breakpoint after the schema when it is stable (full mode), otherwise after the last stable part."""
        if not sections:
            return
        stable = [s for s in sections if s.key != "schema" or context.selection_mode == "full"]
        if stable:
            target = stable[-1]
            index = sections.index(target)
            sections[index] = target.model_copy(update={"cache_breakpoint": True})

    def _context_section(self, *, hint_tables: Sequence[str], is_follow_up: bool, include_hint: bool) -> PromptSection:
        lines = ["<context>",
                 f"Reference date (today): {self.reference_date.isoformat()}",
                 f"SQL dialect: {dialect_display_name(self.dialect)}"]
        if include_hint and hint_tables:
            lines.append("Likely relevant tables (a heuristic hint; verify against the schema): "
                         + ", ".join(hint_tables))
        if is_follow_up:
            lines.append("The question appears to refer to the previous turn in <conversation_history>.")
        lines.append("</context>")
        return PromptSection(key="context", title="Context", technique="Temporal & retrieval context",
                             location="user", text="\n".join(lines))

    @staticmethod
    def _question_section(question: str, reminder: str = RESPONSE_REMINDER) -> PromptSection:
        return PromptSection(key="question", title="User question", technique="Delimited untrusted input",
                             location="user",
                             text=f"<question>\n{neutralize_tags(question.strip())}\n</question>\n\n{reminder}")

    def _history_section(self, history: Sequence[ConversationTurn]) -> PromptSection:
        return PromptSection(key="history", title="Conversation history", technique="Conversation memory",
                             location="user", text=render_history(history))

    def _recent(self, history: Sequence[ConversationTurn], config: PromptConfig) -> list[ConversationTurn]:
        if not config.use_conversation_context or self.max_history_turns <= 0:
            return []
        return list(history)[-self.max_history_turns:]

    def _is_follow_up(self, question: str, history: Sequence[ConversationTurn], explicit: bool | None) -> bool:
        if explicit is not None:
            return explicit and bool(history)
        return bool(history) and looks_like_follow_up(question)

    # ------------------------------------------------------------ stages
    def build_generation_prompt(
        self,
        question: str,
        *,
        config: PromptConfig,
        schema_context: SchemaContext | None = None,
        history: Sequence[ConversationTurn] = (),
        is_follow_up: bool | None = None,
    ) -> BuiltPrompt:
        context = schema_context or self._full_context
        turns = self._recent(history, config)
        follow_up = self._is_follow_up(question, turns, is_follow_up)

        sections = self._generation_system(config, context)
        selected: list[SelectedExample] = []
        if config.use_few_shot and config.few_shot_k > 0 and not config.use_baseline_prompt:
            selected = self.selector.select(
                question, config.few_shot_k, strategy=config.few_shot_strategy,
                is_follow_up=follow_up, likely_table_count=len(context.ranked_tables),
            )
            if selected:
                sections.append(PromptSection(key="examples", title="Few-shot examples",
                                              technique=f"Few-shot prompting ({config.few_shot_strategy})",
                                              location="user", text=render_examples(selected, self.dialect)))
        if turns:
            sections.append(self._history_section(turns))
        sections.append(self._context_section(hint_tables=context.ranked_tables, is_follow_up=follow_up,
                                              include_hint=config.use_schema_context and not config.use_baseline_prompt))
        sections.append(self._question_section(question))
        return BuiltPrompt(stage="generation", config_name=config.name, sections=sections,
                           selected_examples=selected,
                           schema_tables=context.table_names if config.use_schema_context and not config.use_baseline_prompt else [],
                           schema_mode=context.selection_mode)

    def build_correction_prompt(
        self,
        question: str,
        *,
        failed_sql: str | None,
        problems: Sequence[str],
        attempt: int,
        max_attempts: int,
        config: PromptConfig,
        schema_context: SchemaContext | None = None,
        history: Sequence[ConversationTurn] = (),
        is_follow_up: bool | None = None,
    ) -> BuiltPrompt:
        context = schema_context or self._full_context
        turns = self._recent(history, config)
        follow_up = self._is_follow_up(question, turns, is_follow_up)
        sections = self._generation_system(config, context)  # identical prefix -> cache hit
        if turns:
            sections.append(self._history_section(turns))
        sections.append(self._context_section(hint_tables=context.ranked_tables, is_follow_up=follow_up,
                                              include_hint=config.use_schema_context and not config.use_baseline_prompt))
        sections.append(self._question_section(question, reminder=""))
        cleaned = [neutralize_tags(truncate_text(p, 500)) for p in problems]
        sections.append(PromptSection(
            key="failed_attempt", title=f"Failed attempt {attempt}/{max_attempts}",
            technique="Error-driven correction", location="user",
            text=failed_attempt_block(sql=failed_sql, problems=cleaned, attempt=attempt, max_attempts=max_attempts)
                 + "\n\n" + CORRECTION_INSTRUCTIONS + "\n\n" + RESPONSE_REMINDER,
        ))
        return BuiltPrompt(stage="correction", config_name=config.name, sections=sections,
                           schema_tables=context.table_names, schema_mode=context.selection_mode)

    def build_verification_prompt(
        self,
        question: str,
        *,
        sql: str,
        schema_context: SchemaContext | None = None,
        static_findings: Sequence[str] = (),
        history: Sequence[ConversationTurn] = (),
        config: PromptConfig | None = None,
    ) -> BuiltPrompt:
        config = config or PRESETS["C"]
        context = schema_context or self._full_context
        turns = self._recent(history, config)
        sections = [
            PromptSection(key="role", title="Reviewer role", technique="Role prompting (independent reviewer)",
                          location="system", text=verifier_role(self.dialect)),
            PromptSection(key="checklist", title="Review checklist", technique="Self-verification checklist",
                          location="system", text=verification_checklist(self.dialect)),
            PromptSection(key="dialect", title="Dialect notes", technique="Dialect conditioning",
                          location="system", text=dialect_notes(self.dialect)),
            PromptSection(key="schema", title="Database schema", technique="Contextual prompting",
                          location="system", text=self._schema_text(context, config)),
        ]
        self._place_cache_breakpoint(sections, context)
        if turns:
            sections.append(self._history_section(turns))
        sections.append(self._context_section(hint_tables=(), is_follow_up=False, include_hint=False))
        sections.append(self._question_section(question, reminder=""))
        findings = "\n".join(f"- {neutralize_tags(f)}" for f in static_findings) or "- all automated checks passed"
        sections.append(PromptSection(
            key="candidate", title="Candidate SQL + automated findings", technique="Self-verification",
            location="user",
            text=f"<candidate_sql>\n{neutralize_tags(sql)}\n</candidate_sql>\n\n"
                 f"<automated_checks>\nResults of deterministic checks already run by the system:\n{findings}\n"
                 f"</automated_checks>\n\n{VERIFICATION_TASK}",
        ))
        return BuiltPrompt(stage="verification", config_name=config.name, sections=sections,
                           schema_tables=context.table_names, schema_mode=context.selection_mode)

    def build_explanation_prompt(
        self,
        sql: str,
        *,
        level: ExplanationLevel = "beginner",
        schema_context: SchemaContext | None = None,
        include_schema: bool = True,
    ) -> BuiltPrompt:
        context = schema_context or self._full_context
        config = PRESETS["C"]
        sections = [
            PromptSection(key="role", title="Explainer role", technique="Role prompting",
                          location="system", text=explainer_role(self.dialect)),
            PromptSection(key="guidelines", title="Explanation guidelines", technique="Audience conditioning",
                          location="system", text=explanation_instructions(level)),
        ]
        if include_schema:
            sections.append(PromptSection(key="schema", title="Database schema", technique="Contextual prompting",
                                          location="system", text=self._schema_text(context, config)))
        self._place_cache_breakpoint(sections, context)
        sections.append(PromptSection(
            key="sql", title="SQL to explain", technique="Delimited untrusted input", location="user",
            text=f"<sql_to_explain>\n{neutralize_tags(sql.strip())}\n</sql_to_explain>\n\n"
                 f"Explain this query for a {level} reader. {RESPONSE_REMINDER}",
        ))
        return BuiltPrompt(stage="explanation", config_name=f"explain-{level}", sections=sections,
                           schema_tables=context.table_names if include_schema else [],
                           schema_mode=context.selection_mode if include_schema else None)


# =============================================================================
# CLI: inspect a prompt without calling the API
#   python -m backend.prompt_manager "Show me the top 5 employees" --config C
# =============================================================================
def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    from backend.database import DatabaseManager
    from backend.schema_inspector import SchemaInspector
    from backend.schema_retriever import SchemaRetriever
    from config import get_settings
    from database.init_db import ensure_database

    parser = argparse.ArgumentParser(description="Render a QueryBridge prompt (no API call).")
    parser.add_argument("question")
    parser.add_argument("--config", default="C", choices=list(PRESETS), help="technique configuration")
    parser.add_argument("--stage", default="generation", choices=["generation", "verification", "explanation"])
    parser.add_argument("--sql", default="SELECT 1", help="SQL for the verification/explanation stages")
    parser.add_argument("--retrieval", action="store_true", help="force per-question schema retrieval")
    parser.add_argument("--summary", action="store_true", help="only print the section table")
    args = parser.parse_args(argv)

    settings = get_settings()
    if settings.is_demo_database:
        ensure_database(settings.demo_db_path)
    db = DatabaseManager.from_settings(settings)
    schema = SchemaInspector.from_settings(db, settings).inspect()
    retriever = SchemaRetriever.from_settings(schema, settings)
    context = retriever.retrieve(args.question, mode="retrieved" if args.retrieval else "auto")
    manager = PromptManager(schema, reference_date=settings.effective_reference_date,
                            max_history_turns=settings.conversation_turns)
    config = preset_from_settings(settings, args.config)

    if args.stage == "generation":
        prompt = manager.build_generation_prompt(args.question, config=config, schema_context=context)
    elif args.stage == "verification":
        prompt = manager.build_verification_prompt(args.question, sql=args.sql, schema_context=context)
    else:
        prompt = manager.build_explanation_prompt(args.sql, schema_context=context)

    if not args.summary:
        print(prompt.render())
        print()
    print(f"Configuration {config.name} ({config.label}) - stage: {prompt.stage} - schema mode: {prompt.schema_mode}")
    print(f"{'section':<16}{'where':<8}{'~tokens':>8}  technique")
    for section in prompt.sections:
        flag = "  <- cache breakpoint" if section.cache_breakpoint else ""
        print(f"{section.key:<16}{section.location:<8}{section.token_estimate:>8}  {section.technique}{flag}")
    print(f"{'TOTAL':<24}{prompt.total_token_estimate:>8}  (estimate: ~4 characters per token)")
    if prompt.selected_examples:
        print("Few-shot examples: " + ", ".join(
            f"{s.example.id} [{s.reason}]" for s in prompt.selected_examples))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
