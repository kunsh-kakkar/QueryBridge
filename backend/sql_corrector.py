"""
The bounded validate -> verify -> (execute) -> correct loop.

``SQLCorrector.refine`` takes the first generation and drives it to an
outcome with a **hard, shared correction budget**:

    candidate
      -> SQLValidator      security violation        -> BLOCKED (never regenerated)
                           syntax error              -> correct
      -> static checks     error                     -> correct          (if enabled)
      -> LLM reviewer      blocking issue            -> reviewer fix / correct   (if enabled)
      -> execute hook      database error            -> correct          (Phase 7 executor)
      -> SUCCESS

* Every failure type draws from the same budget:
  ``min(config.max_correction_attempts, HARD_MAX_CORRECTION_ATTEMPTS)``.
  There is no unbounded loop.
* The semantic reviewer runs at most ``max_semantic_calls`` times (default
  2), and never on SQL it wrote itself. With the default budget of 2
  corrections, a question costs at most 5 LLM calls:
  generate -> review -> correct -> review -> correct.
* A reviewer-supplied ``corrected_sql`` is used as the next candidate
  (saving a generation call) but still goes through the validator and the
  static checks.
* If the budget runs out while only the reviewer has concerns (the query is
  valid and runs), the result is returned *with a warning* rather than
  discarded. If the query is invalid or fails to run, the outcome is
  ``failed`` with the last problems.
* If a correction concludes that the question cannot be answered (out of
  scope, clarification, refusal), that honest answer is returned as is.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field

from backend.exceptions import LLMError
from backend.models import (
    SQL_INTENTS,
    BuiltPrompt,
    ConversationTurn,
    LLMCallRecord,
    PromptConfig,
    SchemaContext,
    SQLGenerationResult,
)
from backend.sql_generator import GenerationOutcome, SQLGenerator
from backend.sql_validator import SQLValidator, ValidationReport
from backend.sql_verifier import SQLVerifier, VerificationReport
from config import HARD_MAX_CORRECTION_ATTEMPTS
from utils.logger import get_logger

logger = get_logger(__name__)

AttemptSource = Literal["generation", "correction", "reviewer"]
AttemptOutcome = Literal["blocked", "invalid_sql", "static_failed", "review_failed", "execution_failed",
                         "passed", "no_sql"]
RefinementStatus = Literal["success", "blocked", "failed", "no_sql"]


@dataclass
class ExecutionAttempt:
    """What the execute hook reports back (the Phase 7 executor builds these)."""

    ok: bool
    error: str | None = None
    result: Any = None


ExecuteHook = Callable[[str], ExecutionAttempt]


class AttemptRecord(BaseModel):
    number: int
    source: AttemptSource
    sql: str | None = Field(description="The candidate SQL as written by the model (or reviewer)")
    outcome: AttemptOutcome
    problems: list[str] = Field(default_factory=list)
    executed_sql: str | None = Field(default=None, description="Validated, re-rendered SQL that ran (if any)")


@dataclass
class RefinementResult:
    status: RefinementStatus
    generation: SQLGenerationResult
    validation: ValidationReport | None = None
    verification: VerificationReport | None = None
    execution: ExecutionAttempt | None = None
    attempts: list[AttemptRecord] = field(default_factory=list)
    corrections_used: int = 0
    semantic_calls: int = 0
    llm_calls: list[LLMCallRecord] = field(default_factory=list)
    prompts: list[BuiltPrompt] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def final_sql(self) -> str | None:
        return self.validation.safe_sql if self.validation and self.validation.is_safe else None


class SQLCorrector:
    def __init__(self, generator: SQLGenerator, validator: SQLValidator, verifier: SQLVerifier,
                 *, max_semantic_calls: int = 2) -> None:
        self.generator = generator
        self.validator = validator
        self.verifier = verifier
        self.max_semantic_calls = max_semantic_calls

    def refine(
        self,
        question: str,
        initial: GenerationOutcome,
        *,
        config: PromptConfig,
        schema_context: SchemaContext | None = None,
        history: Sequence[ConversationTurn] = (),
        is_follow_up: bool | None = None,
        execute: ExecuteHook | None = None,
    ) -> RefinementResult:
        budget = min(config.max_correction_attempts, HARD_MAX_CORRECTION_ATTEMPTS) if config.use_error_correction else 0
        result = RefinementResult(status="failed", generation=initial.result,
                                  llm_calls=[initial.call], prompts=[initial.prompt])
        candidate_sql: str | None = initial.result.sql
        source: AttemptSource = "generation"
        number = 0

        while True:
            problems: list[str]
            outcome: AttemptOutcome

            # A deliberate non-SQL answer (clarify / refuse / out of scope) ends the loop. An answerable
            # intent with *missing* SQL is a defect: it goes to validation ("No SQL was produced") and correction.
            generation = result.generation
            if source != "reviewer" and (generation.intent not in SQL_INTENTS or generation.needs_clarification):
                result.status = "no_sql"
                result.attempts.append(AttemptRecord(number=number, source=source, sql=None, outcome="no_sql"))
                return result

            validation = self.validator.validate(candidate_sql)
            result.validation = validation
            result.warnings.extend(w for w in validation.warnings if w not in result.warnings)

            if validation.is_blocked:
                result.status = "blocked"
                result.problems = validation.messages
                result.attempts.append(AttemptRecord(number=number, source=source, sql=candidate_sql,
                                                     outcome="blocked", problems=validation.messages))
                logger.warning("Blocked unsafe SQL (attempt %d): %s", number, validation.messages[0])
                return result

            if not validation.is_safe:
                outcome, problems = "invalid_sql", validation.messages
            else:
                outcome, problems = self._verify_and_execute(
                    question, validation, source, result, config, schema_context, history, execute)
                if outcome == "passed":
                    result.status = "success"
                    result.attempts.append(AttemptRecord(number=number, source=source, sql=candidate_sql,
                                                         outcome="passed", executed_sql=validation.safe_sql))
                    return result

            result.attempts.append(AttemptRecord(number=number, source=source, sql=candidate_sql,
                                                 outcome=outcome, problems=problems))
            result.problems = problems

            # --- budget exhausted -------------------------------------------------
            if result.corrections_used >= budget:
                if outcome == "review_failed":
                    return self._accept_with_reviewer_warning(question, validation, result, execute, number, source)
                result.status = "failed"
                return result

            # --- next candidate ---------------------------------------------------
            result.corrections_used += 1
            number += 1
            verdict = result.verification.semantic if result.verification else None
            if outcome == "review_failed" and verdict is not None and verdict.corrected_sql:
                candidate_sql, source = verdict.corrected_sql, "reviewer"
                result.generation = result.generation.model_copy(update={
                    "sql": verdict.corrected_sql,
                    "assumptions": [*result.generation.assumptions,
                                    f"Adjusted by the AI reviewer: {verdict.summary}"],
                })
                continue
            try:
                corrected = self.generator.correct(
                    question, failed_sql=candidate_sql, problems=problems, attempt=result.corrections_used,
                    max_attempts=budget, config=config, schema_context=schema_context, history=history,
                    is_follow_up=is_follow_up)
            except LLMError as exc:
                result.status = "failed"
                result.problems = problems + [f"Correction unavailable: {exc.user_message}"]
                return result
            result.llm_calls.append(corrected.call)
            result.prompts.append(corrected.prompt)
            result.generation = corrected.result
            candidate_sql, source = corrected.result.sql, "correction"

    # ------------------------------------------------------------------ helpers
    def _verify_and_execute(
        self, question: str, validation: ValidationReport, source: AttemptSource, result: RefinementResult,
        config: PromptConfig, schema_context: SchemaContext | None, history: Sequence[ConversationTurn],
        execute: ExecuteHook | None,
    ) -> tuple[AttemptOutcome, list[str]]:
        if config.use_static_verification:
            report = self.verifier.static_verify(validation, question=question)
            result.verification = report
            if not report.static_passed:
                return "static_failed", report.problems
            can_review = (config.use_semantic_verification and source != "reviewer"
                          and result.semantic_calls < self.max_semantic_calls)
            if can_review:
                result.semantic_calls += 1
                self.verifier.semantic_verify(report, question=question, sql=validation.safe_sql, config=config,
                                              schema_context=schema_context, history=history)
                if report.semantic_call is not None:
                    result.llm_calls.append(report.semantic_call)
                if report.semantic_prompt is not None:
                    result.prompts.append(report.semantic_prompt)
                result.warnings.extend(w for w in report.warnings if w not in result.warnings)
                if not report.semantic_passed:
                    return "review_failed", report.problems
        if execute is not None:
            attempt = execute(validation.safe_sql)
            result.execution = attempt
            if not attempt.ok:
                return "execution_failed", [attempt.error or "The query failed to run."]
        return "passed", []

    def _accept_with_reviewer_warning(
        self, question: str, validation: ValidationReport, result: RefinementResult,
        execute: ExecuteHook | None, number: int, source: AttemptSource,
    ) -> RefinementResult:
        """Budget is spent and only the reviewer objects: run the valid query, but flag it."""
        if execute is not None:
            attempt = execute(validation.safe_sql)
            result.execution = attempt
            if not attempt.ok:
                result.status = "failed"
                result.problems = [attempt.error or "The query failed to run."]
                return result
        result.status = "success"
        result.warnings.append("The AI reviewer still had concerns about this query; check the result carefully.")
        result.attempts.append(AttemptRecord(number=number, source=source, sql=validation.original_sql,
                                             outcome="passed", problems=["accepted with reviewer concerns"],
                                             executed_sql=validation.safe_sql))
        return result
