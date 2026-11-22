"""
Deterministic, offline stand-ins for the LLM, used by unit and pipeline tests.

``FakeLLM`` implements the same ``StructuredLLM`` interface as ``GeminiClient``:
it receives the real ``BuiltPrompt``s and returns scripted outputs. Scripted
items may be a Pydantic object, an exception to raise, or a callable
``(prompt, output_model) -> object``. Every prompt is recorded, so tests can
assert exactly what the pipeline sent.

These fakes are for tests only. The application never falls back to a fake
model: without an API key it reports that clearly.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from backend.llm_client import LLMResponse
from backend.models import (
    BuiltPrompt,
    LLMCallRecord,
    QueryExplanation,
    SQLExplanationResult,
    SQLGenerationResult,
    VerificationIssue,
    VerificationVerdict,
)

Scripted = BaseModel | Exception | Callable[[BuiltPrompt, type], Any]


class FakeLLM:
    def __init__(self, script: list[Scripted] | None = None,
                 responder: Callable[[BuiltPrompt, type], Any] | None = None) -> None:
        self.script: list[Scripted] = list(script or [])
        self.responder = responder
        self.calls: list[tuple[BuiltPrompt, type]] = []

    def queue(self, *items: Scripted) -> "FakeLLM":
        self.script.extend(items)
        return self

    @property
    def prompts(self) -> list[BuiltPrompt]:
        return [prompt for prompt, _ in self.calls]

    @property
    def stages(self) -> list[str]:
        return [prompt.stage for prompt in self.prompts]

    def complete(self, prompt: BuiltPrompt, output_model: type) -> LLMResponse:
        self.calls.append((prompt, output_model))
        if self.script:
            item = self.script.pop(0)
        elif self.responder is not None:
            item = self.responder
        else:
            raise AssertionError(f"FakeLLM has no scripted response for stage '{prompt.stage}'")
        if isinstance(item, Exception):
            raise item
        output = item(prompt, output_model) if callable(item) and not isinstance(item, BaseModel) else item
        assert isinstance(output, output_model), f"expected {output_model.__name__}, got {type(output).__name__}"
        record = LLMCallRecord(stage=prompt.stage, model="fake-model", served_model="fake-model",
                               input_tokens=prompt.total_token_estimate, output_tokens=50,
                               latency_ms=1.0, stop_reason="end_turn")
        return LLMResponse(output=output, record=record)


# ----------------------------------------------------------------- builders
def explanation(summary: str = "Returns the requested rows.", **overrides: Any) -> QueryExplanation:
    data = {"summary": summary, "tables_accessed": [], "conditions": [], "calculations": [],
            "join_rationale": None, "aggregation_rationale": None, "ordering_and_limits": None}
    data.update(overrides)
    return QueryExplanation(**data)


def generation(sql: str | None = "SELECT 1", *, intent: str = "data_retrieval", confidence: float = 0.9,
               tables: list[str] | None = None, columns: list[str] | None = None,
               assumptions: list[str] | None = None, summary: str = "Returns the requested rows.",
               needs_clarification: bool = False, clarification_question: str | None = None,
               clarification_options: list[str] | None = None) -> SQLGenerationResult:
    return SQLGenerationResult(
        intent=intent, needs_clarification=needs_clarification, clarification_question=clarification_question,
        clarification_options=clarification_options or [], sql=sql, tables_used=tables or [],
        columns_used=columns or [], explanation=explanation(summary), assumptions=assumptions or [],
        confidence=confidence,
    )


def clarification(question: str = "Which metric do you mean?", options: list[str] | None = None) -> SQLGenerationResult:
    return generation(None, intent="clarification_needed", needs_clarification=True,
                      clarification_question=question,
                      clarification_options=options or ["Total sales", "Average rating", "Projects led"],
                      confidence=0.3)


def refusal(intent: str = "unsafe_request") -> SQLGenerationResult:
    return generation(None, intent=intent, summary="QueryBridge is read-only.", confidence=0.95)


def verdict(correct: bool = True, *, issues: list[VerificationIssue] | None = None,
            corrected_sql: str | None = None) -> VerificationVerdict:
    return VerificationVerdict(is_correct=correct, matches_intent=correct, issues=issues or [],
                               corrected_sql=corrected_sql, summary="ok" if correct else "problems found",
                               confidence=0.9)


def sql_explanation(summary: str = "Counts rows.") -> SQLExplanationResult:
    return SQLExplanationResult(summary=summary, steps=[], tables_used=[], columns_used=[],
                                potential_issues=[], modifies_data=False)
