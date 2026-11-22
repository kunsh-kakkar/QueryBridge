"""Tests for the LLM output contract in backend/models.py."""

from __future__ import annotations

import pytest

from backend.gemini_client import gemini_schema
from backend.models import (
    BuiltPrompt,
    PromptConfig,
    PromptSection,
    SQLExplanationResult,
    SQLGenerationResult,
    VerificationIssue,
    VerificationVerdict,
)

OUTPUT_MODELS = [SQLGenerationResult, VerificationVerdict, SQLExplanationResult]


def _explanation() -> dict:
    return {"summary": "s", "tables_accessed": ["employees"], "conditions": [], "calculations": [],
            "join_rationale": None, "aggregation_rationale": None, "ordering_and_limits": None}


def _generation(**overrides) -> dict:
    data = {"intent": "data_retrieval", "needs_clarification": False, "clarification_question": None,
            "clarification_options": [], "sql": "SELECT 1", "tables_used": [], "columns_used": [],
            "explanation": _explanation(), "assumptions": [], "confidence": 0.9}
    data.update(overrides)
    return data


def _all_objects(schema: dict):
    """Yield every object schema (root + $defs + nested)."""
    stack = [schema]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if node.get("type") == "object":
                yield node
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)


@pytest.mark.parametrize("model", OUTPUT_MODELS, ids=lambda m: m.__name__)
def test_every_output_field_is_required(model):
    """Structured output must fill every field: each object, before and after the Gemini conversion."""
    for schema in (model.model_json_schema(), gemini_schema(model)):
        objects = list(_all_objects(schema))
        assert objects
        for obj in objects:
            assert set(obj.get("required", [])) == set(obj["properties"]), obj.get("title")


@pytest.mark.parametrize("raw, expected", [(1.7, 1.0), (-0.2, 0.0), ("0.4", 0.4), ("high", 0.5), (float("nan"), 0.5)])
def test_confidence_is_clamped(raw, expected):
    assert SQLGenerationResult.model_validate(_generation(confidence=raw)).confidence == expected


@pytest.mark.parametrize("raw, expected", [("SELECT 1;", "SELECT 1"), ("  SELECT 1 ; ;\n", "SELECT 1"), ("  ", None), (None, None)])
def test_sql_is_cleaned(raw, expected):
    assert SQLGenerationResult.model_validate(_generation(sql=raw)).sql == expected


def test_clarification_consistency():
    result = SQLGenerationResult.model_validate(_generation(
        needs_clarification=True, clarification_question="", clarification_options=[" a ", "", "b", "c", "d", "e"]))
    assert result.intent == "clarification_needed"
    assert result.clarification_question
    assert result.clarification_options == ["a", "b", "c", "d"]
    assert result.wants_execution is False


def test_intent_clarification_sets_flag():
    result = SQLGenerationResult.model_validate(_generation(intent="clarification_needed", sql=None))
    assert result.needs_clarification is True


def test_no_clarification_clears_question():
    result = SQLGenerationResult.model_validate(_generation(clarification_question="leftover?", clarification_options=["x"]))
    assert result.clarification_question is None and result.clarification_options == []
    assert result.wants_execution


@pytest.mark.parametrize("intent", ["unsafe_request", "out_of_scope"])
def test_non_sql_intents_do_not_execute(intent):
    assert not SQLGenerationResult.model_validate(_generation(intent=intent)).wants_execution


def test_verification_blocking_logic():
    def verdict(**kw):
        base = {"is_correct": True, "matches_intent": True, "issues": [], "corrected_sql": None,
                "summary": "ok", "confidence": 0.9}
        base.update(kw)
        return VerificationVerdict.model_validate(base)

    assert not verdict().has_blocking_issue
    assert verdict(is_correct=False).has_blocking_issue
    assert verdict(matches_intent=False).has_blocking_issue
    low = VerificationIssue(category="other", severity="low", description="alias style")
    high = VerificationIssue(category="join", severity="high", description="missing join condition")
    assert not verdict(issues=[low.model_dump()]).has_blocking_issue
    assert verdict(issues=[high.model_dump()]).has_blocking_issue
    assert verdict(corrected_sql="SELECT 2;").corrected_sql == "SELECT 2"


def test_prompt_config_techniques_and_bounds():
    assert "Structured output" in PromptConfig().techniques()
    assert PromptConfig(use_baseline_prompt=True).techniques()[0] == "Basic instruction"
    with pytest.raises(ValueError):
        PromptConfig(max_correction_attempts=4)


def test_built_prompt_blocks():
    prompt = BuiltPrompt(stage="generation", config_name="C", sections=[
        PromptSection(key="role", title="r", technique="t", location="system", text="ROLE"),
        PromptSection(key="schema", title="s", technique="t", location="system", text="SCHEMA", cache_breakpoint=True),
        PromptSection(key="question", title="q", technique="t", location="user", text="Q"),
    ])
    assert [s.text for s in prompt.system_sections] == ["ROLE", "SCHEMA"]
    assert [s.text for s in prompt.user_sections] == ["Q"]
    assert prompt.system_text == "ROLE\n\nSCHEMA" and prompt.user_text == "Q"
    assert "[cache breakpoint]" in prompt.render()
    assert prompt.total_token_estimate > 0
