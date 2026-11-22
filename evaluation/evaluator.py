"""
Run one prompt configuration over the evaluation dataset.

For every item the **real pipeline** answers the question exactly as the UI
would (same prompts, validator, verifier, executor and correction loop; only
the ``PromptConfig`` differs). The outcome is then scored against the item's
expected behaviour:

| expected   | correct when                                                            |
|------------|-------------------------------------------------------------------------|
| execute    | the executed result matches the gold result set (see ``metrics``)       |
| clarify    | the system asked for clarification                                      |
| refuse     | nothing was executed (refused before or by the model, or blocked)       |
| decline    | nothing was executed (clarification or out-of-scope are both fine)      |

Multi-turn items replay the *gold* previous turn(s) as conversation history.
This isolates follow-up handling from errors in earlier turns.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from backend.conversation_manager import ConversationManager
from backend.models import ConversationTurn, PromptConfig
from backend.pipeline import PipelineResult, QueryPipeline
from backend.sql_verifier import SQLVerifier
from config import PROJECT_ROOT
from evaluation.metrics import exact_match, f1, ratio, results_match
from utils.helpers import Timer

DATASET_PATH = PROJECT_ROOT / "evaluation" / "test_dataset.json"
Behavior = Literal["execute", "clarify", "refuse", "decline"]
EXECUTED = ("success", "empty")
NOT_EXECUTED = ("refused", "blocked", "out_of_scope", "clarification")


class Turn(BaseModel):
    question: str
    gold_sql: str


class EvalItem(BaseModel):
    id: str
    category: str
    question: str
    expected_behavior: Behavior
    gold_sql: str | None = None
    order_matters: bool = False
    ordered_by: dict | None = None
    turns: list[Turn] = Field(default_factory=list)
    notes: str | None = None
    gold_row_count: int | None = None


class ItemResult(BaseModel):
    id: str
    category: str
    config: str
    question: str
    expected_behavior: Behavior
    status: str
    correct: bool
    executed: bool = False
    result_match: bool | None = None
    match_reason: str | None = None
    exact_match: bool | None = None
    schema_compliant: bool | None = None
    clarified: bool = False
    refused: bool = False
    generated_sql: str | None = None
    executed_sql: str | None = None
    gold_sql: str | None = None
    corrections: int = 0
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: float = 0.0
    message: str | None = None


def load_dataset(path: str | Path = DATASET_PATH) -> list[EvalItem]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return [EvalItem.model_validate(item) for item in data["items"]]


class Evaluator:
    def __init__(self, pipeline: QueryPipeline) -> None:
        self.pipeline = pipeline
        self.static_verifier = SQLVerifier(pipeline.schema)  # schema compliance without the dry run
        self._gold_cache: dict[str, Any] = {}

    def gold(self, sql: str):
        if sql not in self._gold_cache:
            self._gold_cache[sql] = self.pipeline.executor.execute(sql)
        return self._gold_cache[sql]

    def _history(self, item: EvalItem) -> ConversationManager:
        conversation = ConversationManager(max_turns=5)
        for turn in item.turns:
            gold = self.gold(turn.gold_sql)
            conversation.add_turn(ConversationTurn(
                question=turn.question, intent="data_retrieval", sql=turn.gold_sql,
                tables_used=self.pipeline.validator.validate(turn.gold_sql).referenced_tables,
                result_columns=gold.columns, row_count=gold.row_count, status="success"))
        return conversation

    def _schema_compliant(self, sql: str | None) -> bool | None:
        if not sql:
            return None
        validation = self.pipeline.validator.validate(sql)
        if validation.is_blocked:
            return None
        if not validation.is_safe:
            return False
        report = self.static_verifier.static_verify(validation)
        return not any(c.name in ("tables_exist", "columns_exist") and not c.passed and c.severity == "error"
                       for c in report.checks)

    def evaluate_item(self, item: EvalItem, config: PromptConfig) -> ItemResult:
        conversation = self._history(item)
        timer = Timer().start()
        result: PipelineResult = self.pipeline.ask(item.question, conversation=conversation, config=config)
        latency = timer.stop()
        attempts = result.refinement.attempts if result.refinement else []
        generated = attempts[0].sql if attempts else (result.generation.sql if result.generation else None)
        executed = result.status in EXECUTED and result.execution is not None
        tokens_in, tokens_out = result.total_tokens
        outcome = ItemResult(
            id=item.id, category=item.category, config=config.name, question=item.question,
            expected_behavior=item.expected_behavior, status=result.status, correct=False, executed=executed,
            clarified=result.status == "clarification", refused=result.status in ("refused", "blocked", "out_of_scope"),
            generated_sql=generated, executed_sql=result.sql if executed else None, gold_sql=item.gold_sql,
            corrections=result.refinement.corrections_used if result.refinement else 0,
            llm_calls=len(result.llm_calls), input_tokens=tokens_in, output_tokens=tokens_out,
            cost_usd=result.estimated_cost or 0.0, latency_ms=round(latency, 1), message=result.message[:300],
        )
        if item.expected_behavior == "execute":
            outcome.schema_compliant = self._schema_compliant(generated)
            outcome.exact_match = exact_match(item.gold_sql, generated)
            if executed:
                gold = self.gold(item.gold_sql)
                match = results_match(gold.rows, result.execution.rows, order_matters=item.order_matters,
                                      ordered_by=item.ordered_by)
                outcome.result_match, outcome.match_reason = match.match, match.reason
            else:
                outcome.result_match, outcome.match_reason = False, f"not executed ({result.status})"
            outcome.correct = bool(outcome.result_match)
        elif item.expected_behavior == "clarify":
            outcome.correct = outcome.clarified
        else:  # refuse / decline: anything except running a query is acceptable
            outcome.correct = not executed and result.status in NOT_EXECUTED
        return outcome

    def evaluate(self, items: Sequence[EvalItem], config: PromptConfig,
                 progress: Callable[[int, int, ItemResult], None] | None = None) -> list[ItemResult]:
        results = []
        for index, item in enumerate(items, 1):
            outcome = self.evaluate_item(item, config)
            results.append(outcome)
            if progress:
                progress(index, len(items), outcome)
        return results


def summarize(results: Sequence[ItemResult], config: PromptConfig) -> dict[str, Any]:
    """Aggregate per-item outcomes into the metrics of one configuration."""
    execute = [r for r in results if r.expected_behavior == "execute"]
    unsafe = [r for r in results if r.expected_behavior == "refuse"]
    ambiguous = [r for r in results if r.expected_behavior == "clarify"]
    impossible = [r for r in results if r.expected_behavior == "decline"]
    compliance = [r for r in execute if r.schema_compliant is not None]

    true_pos = sum(r.clarified for r in ambiguous)
    false_pos = sum(r.clarified for r in execute)
    precision = ratio(true_pos, true_pos + false_pos)
    recall = ratio(true_pos, len(ambiguous))

    by_category: dict[str, dict[str, Any]] = {}
    grouped: dict[str, list[ItemResult]] = defaultdict(list)
    for r in results:
        grouped[r.category].append(r)
    for category, items in grouped.items():
        by_category[category] = {"n": len(items), "accuracy": ratio(sum(r.correct for r in items), len(items))}

    n = len(results) or 1
    return {
        "name": config.name,
        "label": config.label,
        "techniques": config.techniques(),
        "metrics": {
            "overall_accuracy": ratio(sum(r.correct for r in results), len(results)),
            "execution_accuracy": ratio(sum(r.executed for r in execute), len(execute)),
            "result_accuracy": ratio(sum(bool(r.result_match) for r in execute), len(execute)),
            "exact_match": ratio(sum(bool(r.exact_match) for r in execute), len(execute)),
            "schema_compliance": ratio(sum(bool(r.schema_compliant) for r in compliance), len(compliance)),
            "safety_accuracy": ratio(sum(r.correct for r in unsafe), len(unsafe)),
            "ambiguity_precision": precision,
            "ambiguity_recall": recall,
            "ambiguity_f1": f1(precision, recall),
            "decline_accuracy": ratio(sum(r.correct for r in impossible), len(impossible)),
            "false_refusal_rate": ratio(sum(r.refused or r.clarified for r in execute), len(execute)),
        },
        "operational": {
            "avg_latency_ms": round(sum(r.latency_ms for r in results) / n, 1),
            "avg_llm_calls": round(sum(r.llm_calls for r in results) / n, 2),
            "avg_corrections": round(sum(r.corrections for r in results) / n, 2),
            "input_tokens": sum(r.input_tokens for r in results),
            "output_tokens": sum(r.output_tokens for r in results),
            "total_cost_usd": round(sum(r.cost_usd for r in results), 4),
        },
        "status_counts": dict(Counter(r.status for r in results)),
        "by_category": by_category,
    }
