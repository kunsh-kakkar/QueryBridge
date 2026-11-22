"""
Stage 1 (generation), correction, and explanation mode.

``SQLGenerator`` connects the prompt manager to the LLM client. It builds the
right prompt for each stage, sends it with the right output contract, and
returns the validated result together with the exact prompt and the call
record, so the pipeline trace and the Prompt Lab can show what was sent and
what it cost.

Post-processing is deliberately small. The Pydantic model has already
normalised the output (clamped confidence, stripped semicolons, reconciled
clarification fields). Here we only remove duplicate table/column names and
make sure a response that claims to answer actually contains SQL.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from backend.llm_client import StructuredLLM
from backend.models import (
    BuiltPrompt,
    ConversationTurn,
    LLMCallRecord,
    PromptConfig,
    SchemaContext,
    SQLExplanationResult,
    SQLGenerationResult,
    SQL_INTENTS,
)
from backend.prompt_manager import PromptManager
from prompts.explanation_prompt import ExplanationLevel
from utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class GenerationOutcome:
    result: SQLGenerationResult
    prompt: BuiltPrompt
    call: LLMCallRecord
    problems: list[str]

    @property
    def is_usable(self) -> bool:
        """Contains SQL that should continue to validation."""
        return self.result.wants_execution and not self.problems


@dataclass
class ExplanationOutcome:
    result: SQLExplanationResult
    prompt: BuiltPrompt
    call: LLMCallRecord


def _dedupe(items: Sequence[str]) -> list[str]:
    seen: dict[str, str] = {}
    for item in items:
        cleaned = item.strip()
        if cleaned and cleaned.lower() not in seen:
            seen[cleaned.lower()] = cleaned
    return list(seen.values())


def _postprocess(result: SQLGenerationResult) -> tuple[SQLGenerationResult, list[str]]:
    result.tables_used = _dedupe(result.tables_used)
    result.columns_used = _dedupe(result.columns_used)
    problems: list[str] = []
    if result.intent in SQL_INTENTS and not result.needs_clarification and not result.sql:
        problems.append("The model reported an answerable question but returned no SQL.")
    return result, problems


class SQLGenerator:
    def __init__(self, llm: StructuredLLM, prompt_manager: PromptManager) -> None:
        self.llm = llm
        self.prompts = prompt_manager

    def generate(
        self,
        question: str,
        *,
        config: PromptConfig,
        schema_context: SchemaContext | None = None,
        history: Sequence[ConversationTurn] = (),
        is_follow_up: bool | None = None,
    ) -> GenerationOutcome:
        prompt = self.prompts.build_generation_prompt(
            question, config=config, schema_context=schema_context, history=history, is_follow_up=is_follow_up)
        response = self.llm.complete(prompt, SQLGenerationResult)
        result, problems = _postprocess(response.output)
        logger.info("Generated (config %s): intent=%s confidence=%.2f sql=%s",
                    config.name, result.intent, result.confidence, "yes" if result.sql else "no")
        return GenerationOutcome(result=result, prompt=prompt, call=response.record, problems=problems)

    def correct(
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
    ) -> GenerationOutcome:
        if attempt < 1 or attempt > max_attempts:
            raise ValueError(f"Correction attempt {attempt} is outside the budget 1..{max_attempts}")
        prompt = self.prompts.build_correction_prompt(
            question, failed_sql=failed_sql, problems=list(problems), attempt=attempt, max_attempts=max_attempts,
            config=config, schema_context=schema_context, history=history, is_follow_up=is_follow_up)
        response = self.llm.complete(prompt, SQLGenerationResult)
        result, new_problems = _postprocess(response.output)
        logger.info("Correction %d/%d: intent=%s sql=%s", attempt, max_attempts, result.intent,
                    "yes" if result.sql else "no")
        return GenerationOutcome(result=result, prompt=prompt, call=response.record, problems=new_problems)

    def explain(
        self,
        sql: str,
        *,
        level: ExplanationLevel = "beginner",
        schema_context: SchemaContext | None = None,
    ) -> ExplanationOutcome:
        prompt = self.prompts.build_explanation_prompt(sql, level=level, schema_context=schema_context)
        response = self.llm.complete(prompt, SQLExplanationResult)
        return ExplanationOutcome(result=response.output, prompt=prompt, call=response.record)
