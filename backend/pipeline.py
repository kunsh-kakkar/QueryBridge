"""
QueryPipeline: the orchestrator used by both the UI and the evaluator.

    question
      -> InputProcessor          clean, limit, refuse explicit writes (no LLM call)
      -> ConversationManager     follow-up detection, clarification merge, previous tables
      -> SchemaRetriever         relevant tables (full schema for small DBs) + hint
      -> SQLGenerator            Gemini + structured output (PromptConfig chooses the techniques)
      -> intent handling         clarification / unsafe / out-of-scope answers
      -> SQLCorrector            validator -> static checks -> LLM reviewer -> executor,
                                 with a bounded correction budget
      -> ResultFormatter         typed DataFrame, column profiles, summary
      -> ChartRecommender        chart suggestions
      -> ComplexityAnalyzer      Simple / Moderate / Complex
      -> composite confidence    model self-estimate + verification + corrections + warnings
      -> ConversationManager / HistoryStore

Every stage is timed in a ``PipelineTrace`` that the UI shows step by step.
Failures at any stage become a ``PipelineResult`` with a clear status and a
UI-safe message. Exceptions never reach the user unhandled, and credentials
never appear.

``run_sql`` re-runs SQL from history (validate -> execute -> format) with no
LLM call; ``explain_sql`` powers the query explanation mode.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from backend.chart_recommender import ChartSpec, recommend_charts
from backend.complexity_analyzer import ComplexityReport, analyze_complexity
from backend.conversation_manager import ConversationManager
from backend.database import DatabaseManager
from backend.example_selector import ExampleSelector
from backend.exceptions import InputValidationError, LLMError
from backend.history_store import HistoryEntry, HistoryStore
from backend.input_processor import InputProcessor
from backend.llm_client import StructuredLLM, create_llm_client
from backend.models import (
    BuiltPrompt,
    ConversationTurn,
    LLMCallRecord,
    PromptConfig,
    QueryExplanation,
    SchemaContext,
    SchemaModel,
    SQLExplanationResult,
    SQLGenerationResult,
)
from backend.prompt_manager import PromptManager, preset_from_settings
from backend.result_formatter import EMPTY_RESULT_MESSAGE, FormattedResult, format_result
from backend.schema_inspector import SchemaInspector
from backend.schema_retriever import SchemaRetriever
from backend.sql_corrector import RefinementResult, SQLCorrector
from backend.sql_executor import ExecutionResult, QueryExecutionError, SQLExecutor
from backend.sql_generator import SQLGenerator
from backend.sql_validator import SQLValidator, ValidationReport
from backend.sql_verifier import SQLVerifier, VerificationReport
from config import Settings
from prompts.explanation_prompt import ExplanationLevel
from utils.helpers import Timer
from utils.logger import get_logger

logger = get_logger(__name__)

PipelineStatus = Literal["success", "empty", "clarification", "refused", "out_of_scope", "blocked", "failed", "error"]
StepStatus = Literal["ok", "warning", "failed", "skipped"]


@dataclass
class TraceStep:
    name: str
    status: StepStatus
    duration_ms: float
    detail: str = ""


@dataclass
class PipelineTrace:
    steps: list[TraceStep] = field(default_factory=list)

    def add(self, name: str, status: StepStatus, duration_ms: float, detail: str = "") -> None:
        self.steps.append(TraceStep(name, status, round(duration_ms, 1), detail))

    @property
    def total_ms(self) -> float:
        return round(sum(s.duration_ms for s in self.steps), 1)


@dataclass
class PipelineResult:
    question: str
    status: PipelineStatus
    message: str
    config_name: str
    processed_question: str | None = None
    generation: SQLGenerationResult | None = None
    sql: str | None = None
    pretty_sql: str | None = None
    explanation: QueryExplanation | None = None
    execution: ExecutionResult | None = None
    formatted: FormattedResult | None = None
    charts: list[ChartSpec] = field(default_factory=list)
    complexity: ComplexityReport | None = None
    confidence: float | None = None
    confidence_label: str | None = None
    validation: ValidationReport | None = None
    verification: VerificationReport | None = None
    refinement: RefinementResult | None = None
    schema_context: SchemaContext | None = None
    clarification_question: str | None = None
    clarification_options: list[str] = field(default_factory=list)
    is_follow_up: bool = False
    warnings: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    llm_calls: list[LLMCallRecord] = field(default_factory=list)
    prompts: list[BuiltPrompt] = field(default_factory=list)
    trace: PipelineTrace = field(default_factory=PipelineTrace)
    history_id: int | None = None

    @property
    def ok(self) -> bool:
        return self.status in ("success", "empty")

    @property
    def tables_used(self) -> list[str]:
        if self.verification and self.verification.referenced_tables:
            return self.verification.referenced_tables
        if self.validation and self.validation.referenced_tables:
            return self.validation.referenced_tables
        return self.generation.tables_used if self.generation else []

    @property
    def columns_used(self) -> list[str]:
        if self.verification and self.verification.referenced_columns:
            return self.verification.referenced_columns
        return self.generation.columns_used if self.generation else []

    @property
    def total_tokens(self) -> tuple[int, int]:
        return (sum(c.total_input_tokens for c in self.llm_calls), sum(c.output_tokens for c in self.llm_calls))

    @property
    def estimated_cost(self) -> float | None:
        costs = [c.estimated_cost_usd for c in self.llm_calls if c.estimated_cost_usd is not None]
        return round(sum(costs), 6) if costs else None


def composite_confidence(
    model_confidence: float,
    *,
    verification: VerificationReport | None,
    corrections_used: int,
    warning_count: int,
    complexity: ComplexityReport | None,
) -> float:
    """Blend the model's self-estimate with objective signals (self-reported confidence alone is poorly calibrated)."""
    score = model_confidence
    if verification is not None and verification.semantic is not None:
        verdict = verification.semantic
        score = (0.6 * score + 0.4 * verdict.confidence) if not verdict.has_blocking_issue else score * 0.6
    if verification is not None:
        score -= 0.05 * sum(1 for c in verification.checks if not c.passed and c.severity == "warning")
    score -= 0.10 * corrections_used
    score -= 0.03 * min(warning_count, 3)
    if complexity is not None and complexity.level == "Complex":
        score -= 0.05
    return round(min(0.99, max(0.05, score)), 2)


def confidence_label(score: float, threshold: float) -> str:
    if score >= 0.8:
        return "High"
    return "Medium" if score >= threshold else "Low"


class QueryPipeline:
    def __init__(
        self,
        *,
        db: DatabaseManager,
        schema: SchemaModel,
        llm: StructuredLLM,
        config: PromptConfig,
        reference_date: date | None = None,
        max_rows: int = 1000,
        query_timeout_seconds: float = 10.0,
        max_question_chars: int = 1000,
        conversation_turns: int = 5,
        full_context_max_tables: int = 12,
        schema_max_tables: int = 6,
        confidence_threshold: float = 0.6,
        history: HistoryStore | None = None,
        selector: ExampleSelector | None = None,
    ) -> None:
        self.db = db
        self.schema = schema
        self.llm = llm
        self.config = config
        self.confidence_threshold = confidence_threshold
        self.history = history
        self.input_processor = InputProcessor(max_chars=max_question_chars)
        self.retriever = SchemaRetriever(schema, full_context_max_tables=full_context_max_tables,
                                         max_tables=schema_max_tables)
        self.prompts = PromptManager(schema, selector=selector, reference_date=reference_date,
                                     max_history_turns=conversation_turns)
        self.generator = SQLGenerator(llm, self.prompts)
        self.validator = SQLValidator(schema.dialect, max_rows=max_rows)
        self.verifier = SQLVerifier(schema, db=db, llm=llm, prompt_manager=self.prompts)
        self.corrector = SQLCorrector(self.generator, self.validator, self.verifier)
        self.executor = SQLExecutor(db, self.validator, timeout_seconds=query_timeout_seconds)

    @classmethod
    def from_settings(cls, settings: Settings, *, db: DatabaseManager | None = None,
                      schema: SchemaModel | None = None, llm: StructuredLLM | None = None,
                      config_name: str = "C", history: HistoryStore | None = None) -> "QueryPipeline":
        db = db or DatabaseManager.from_settings(settings)
        schema = schema or SchemaInspector.from_settings(db, settings).inspect()
        return cls(
            db=db, schema=schema, llm=llm or create_llm_client(settings),
            config=preset_from_settings(settings, config_name),
            reference_date=settings.effective_reference_date, max_rows=settings.max_result_rows,
            query_timeout_seconds=settings.query_timeout_seconds, max_question_chars=settings.max_question_chars,
            conversation_turns=settings.conversation_turns,
            full_context_max_tables=settings.schema_full_context_max_tables,
            schema_max_tables=settings.schema_max_tables,
            confidence_threshold=settings.confidence_warning_threshold, history=history,
        )

    @property
    def database_label(self) -> str:
        return self.db.label

    # ================================================================ ask
    def ask(
        self,
        question: str,
        *,
        conversation: ConversationManager | None = None,
        config: PromptConfig | None = None,
        clarification_answer: str | None = None,
        on_step: Callable[[str], None] | None = None,
    ) -> PipelineResult:
        """Answer a question. ``on_step`` receives a short label as each stage starts (UI progress)."""
        def step(label: str) -> None:
            if on_step is not None:
                on_step(label)

        config = config or self.config
        conversation = conversation if conversation is not None else ConversationManager(max_turns=0)
        original_question = question
        if clarification_answer is not None:
            question = conversation.resolve_clarification(clarification_answer)
        result = PipelineResult(question=original_question, status="error", message="", config_name=config.name)
        trace = result.trace

        # --- 1. input --------------------------------------------------------
        step("Checking your question…")
        timer = Timer().start()
        try:
            processed = self.input_processor.process(question)
        except InputValidationError as exc:
            trace.add("Input check", "failed", timer.stop(), exc.user_message)
            result.message = exc.user_message
            return result
        result.processed_question = processed.text
        result.warnings.extend(processed.warnings)
        if processed.should_refuse:
            trace.add("Input check", "failed", timer.stop(), f"Refused before calling the AI: {processed.write_reason}")
            result.status = "refused"
            result.message = ("QueryBridge is read-only, so it cannot change or delete data. "
                              "You can ask to see the rows instead (for example, “Show the sales from 2023”).")
            return self._finish(result, conversation)
        detail = "Clean" if not processed.injection_signals else f"Signals: {', '.join(processed.injection_signals)}"
        trace.add("Input check", "warning" if processed.injection_signals else "ok", timer.stop(), detail)

        # --- 2. memory + schema retrieval ----------------------------------------
        step("Selecting the relevant schema…")
        timer = Timer().start()
        history = conversation.history()
        is_follow_up = conversation.is_follow_up(processed.text) if clarification_answer is None else False
        result.is_follow_up = is_follow_up
        previous = conversation.previous_tables() if is_follow_up else []
        context = self.retriever.retrieve(processed.text, previous_tables=previous)
        result.schema_context = context
        trace.add("Schema selection", "ok", timer.stop(),
                  f"{context.selection_mode} schema · {len(context.tables)} tables"
                  + (f" · likely: {', '.join(context.ranked_tables[:4])}" if context.ranked_tables else "")
                  + (" · follow-up" if is_follow_up else ""))

        # --- 3. generation -------------------------------------------------------
        step("Generating SQL with AI…")
        timer = Timer().start()
        try:
            outcome = self.generator.generate(processed.text, config=config, schema_context=context,
                                              history=history, is_follow_up=is_follow_up)
        except LLMError as exc:
            trace.add("SQL generation", "failed", timer.stop(), exc.user_message)
            result.message = exc.user_message
            result.problems = [exc.detail] if exc.detail else []
            return self._finish(result, conversation)
        result.generation = outcome.result
        result.llm_calls.append(outcome.call)
        result.prompts.append(outcome.prompt)
        trace.add("SQL generation", "ok", timer.stop(),
                  f"intent {outcome.result.intent} · {len(outcome.prompt.selected_examples)} examples · "
                  f"{outcome.call.output_tokens} output tokens")

        if not outcome.result.wants_execution and not outcome.problems:
            return self._finish(self._non_sql_answer(result, outcome.result, processed.text, conversation),
                                conversation)

        # --- 4. validate / verify / execute / correct --------------------------------
        step("Validating, verifying and running the query…")
        timer = Timer().start()
        refinement = self.corrector.refine(processed.text, outcome, config=config, schema_context=context,
                                           history=history, is_follow_up=is_follow_up,
                                           execute=self.executor.as_hook())
        elapsed = timer.stop()
        result.refinement = refinement
        result.generation = refinement.generation
        result.validation = refinement.validation
        result.verification = refinement.verification
        result.llm_calls = refinement.llm_calls
        result.prompts = refinement.prompts
        result.warnings.extend(w for w in refinement.warnings if w not in result.warnings)
        self._trace_refinement(trace, refinement, elapsed)

        if refinement.status == "blocked":
            result.status = "blocked"
            result.message = ("The generated SQL was blocked by the security validator and was not run: "
                              + (refinement.problems[0] if refinement.problems else "unsafe statement"))
            result.problems = refinement.problems
            return self._finish(result, conversation)
        if refinement.status == "no_sql":
            return self._finish(self._non_sql_answer(result, refinement.generation, processed.text, conversation),
                                conversation)
        if refinement.status == "failed" or refinement.execution is None or not refinement.execution.ok:
            result.status = "failed"
            result.problems = refinement.problems
            result.sql = refinement.validation.safe_sql if refinement.validation else refinement.generation.sql
            used = refinement.corrections_used
            result.message = ("QueryBridge could not produce a working query for this question"
                              + (f" after {used} correction attempt{'s' if used != 1 else ''}" if used else "")
                              + ". Try rephrasing it, or name the tables or columns you mean.")
            return self._finish(result, conversation)

        # --- 5. format, chart, assess ----------------------------------------------
        step("Formatting results…")
        timer = Timer().start()
        execution: ExecutionResult = refinement.execution.result
        result.execution = execution
        result.sql = execution.sql
        result.pretty_sql = refinement.validation.pretty_sql if refinement.validation else execution.sql
        result.explanation = refinement.generation.explanation
        result.formatted = format_result(execution)
        result.charts = recommend_charts(result.formatted)
        result.complexity = analyze_complexity(execution.sql, self.schema.dialect)
        result.warnings.extend(w for w in execution.warnings if w not in result.warnings)
        result.confidence = composite_confidence(
            refinement.generation.confidence, verification=refinement.verification,
            corrections_used=refinement.corrections_used, warning_count=len(result.warnings),
            complexity=result.complexity)
        result.confidence_label = confidence_label(result.confidence, self.confidence_threshold)
        if result.confidence < self.confidence_threshold:
            result.warnings.append("Low confidence: please review the SQL and the assumptions before relying on "
                                   "this result.")
        if result.formatted.is_empty:
            result.status, result.message = "empty", EMPTY_RESULT_MESSAGE
        else:
            result.status, result.message = "success", result.explanation.summary
        trace.add("Results & charts", "ok", timer.stop(),
                  f"{result.formatted.summary} · {len(result.charts)} chart suggestion(s) · "
                  f"{result.complexity.level} query")
        return self._finish(result, conversation)

    # ================================================================ other entry points
    def run_sql(self, sql: str, *, question: str = "Re-run of saved SQL") -> PipelineResult:
        """Validate and execute SQL directly (history re-runs). Makes no LLM call."""
        result = PipelineResult(question=question, status="error", message="", config_name="direct")
        timer = Timer().start()
        validation = self.validator.validate(sql)
        result.validation = validation
        if not validation.is_safe:
            result.trace.add("Validation", "failed", timer.stop(), "; ".join(validation.messages))
            result.status = "blocked" if validation.is_blocked else "failed"
            result.message = validation.messages[0] if validation.messages else "Invalid SQL."
            result.problems = validation.messages
            return result
        result.trace.add("Validation", "ok", timer.stop(), "read-only SELECT")
        timer = Timer().start()
        try:
            execution = self.executor.execute(sql)
        except QueryExecutionError as exc:
            result.trace.add("Execution", "failed", timer.stop(), exc.detail or exc.user_message)
            result.status, result.message = "failed", exc.user_message
            result.problems = [exc.detail] if exc.detail else []
            return result
        result.trace.add("Execution", "ok", timer.stop(), f"{execution.row_count} rows")
        result.execution, result.sql, result.pretty_sql = execution, execution.sql, validation.pretty_sql
        result.formatted = format_result(execution)
        result.charts = recommend_charts(result.formatted)
        result.complexity = analyze_complexity(execution.sql, self.schema.dialect)
        result.warnings = list(validation.warnings) + [w for w in execution.warnings if w not in validation.warnings]
        result.status = "empty" if result.formatted.is_empty else "success"
        result.message = EMPTY_RESULT_MESSAGE if result.formatted.is_empty else result.formatted.summary
        return result

    def explain_sql(self, sql: str, *, level: ExplanationLevel = "beginner") -> tuple[
            SQLExplanationResult | None, ValidationReport, str | None]:
        """Explain any SQL in plain English; returns (explanation, validation, error message)."""
        validation = self.validator.validate(sql)
        try:
            outcome = self.generator.explain(sql, level=level)
        except LLMError as exc:
            return None, validation, exc.user_message
        return outcome.result, validation, None

    # ================================================================ helpers
    def _non_sql_answer(self, result: PipelineResult, generation: SQLGenerationResult, question: str,
                        conversation: ConversationManager) -> PipelineResult:
        result.generation = generation
        result.explanation = generation.explanation
        if generation.needs_clarification:
            result.status = "clarification"
            result.clarification_question = generation.clarification_question
            result.clarification_options = list(generation.clarification_options)
            result.message = generation.clarification_question or "Could you clarify your question?"
            conversation.ask_for_clarification(question, result.message, result.clarification_options)
        elif generation.intent == "unsafe_request":
            result.status = "refused"
            result.message = generation.explanation.summary or "QueryBridge only runs read-only queries."
        else:
            result.status = "out_of_scope"
            result.message = generation.explanation.summary or "This question cannot be answered from this database."
        return result

    @staticmethod
    def _trace_refinement(trace: PipelineTrace, refinement: RefinementResult, elapsed: float) -> None:
        per_attempt = elapsed / max(1, len(refinement.attempts))
        labels = {"generation": "Validate & verify", "correction": "Corrected query", "reviewer": "Reviewer fix"}
        for attempt in refinement.attempts:
            status: StepStatus = "ok" if attempt.outcome == "passed" else (
                "skipped" if attempt.outcome == "no_sql" else "failed")
            detail = attempt.outcome.replace("_", " ")
            if attempt.problems:
                detail += f": {attempt.problems[0]}"
            trace.add(f"{labels[attempt.source]} (attempt {attempt.number})", status, per_attempt, detail)

    def _finish(self, result: PipelineResult, conversation: ConversationManager) -> PipelineResult:
        if result.status != "clarification":
            turn_status = {"success": "success", "empty": "empty", "refused": "refused", "out_of_scope": "refused",
                           "blocked": "refused"}.get(result.status, "error")
            conversation.add_turn(ConversationTurn(
                question=result.processed_question or result.question,
                intent=result.generation.intent if result.generation else None,
                sql=result.sql if result.ok else None,
                tables_used=result.tables_used if result.ok else [],
                result_columns=list(result.formatted.dataframe.columns) if result.formatted else [],
                row_count=result.formatted.row_count if result.formatted else None,
                status=turn_status,
            ))
        if self.history is not None:
            try:
                tokens_in, tokens_out = result.total_tokens
                result.history_id = self.history.add(HistoryEntry(
                    database_label=self.database_label, question=result.processed_question or result.question,
                    sql=result.sql, status=result.status,
                    intent=result.generation.intent if result.generation else None,
                    row_count=result.formatted.row_count if result.formatted else None,
                    execution_time_ms=result.execution.execution_time_ms if result.execution else None,
                    config_name=result.config_name, confidence=result.confidence,
                    complexity=result.complexity.level if result.complexity else None,
                    corrections=result.refinement.corrections_used if result.refinement else 0,
                    llm_calls=len(result.llm_calls), input_tokens=tokens_in, output_tokens=tokens_out,
                    estimated_cost=result.estimated_cost, message=result.message))
            except Exception as exc:  # noqa: BLE001 - history must never break an answer
                logger.warning("Could not save history: %s", type(exc).__name__)
                result.warnings.append("This query could not be saved to history.")
        return result
