"""Tests for backend/sql_generator.py (offline, with FakeLLM)."""

from __future__ import annotations

from datetime import date

import pytest

from backend.exceptions import LLMRateLimitError
from backend.models import ConversationTurn, SQLExplanationResult, SQLGenerationResult
from backend.prompt_manager import PRESETS, PromptManager
from backend.schema_retriever import SchemaRetriever
from backend.sql_generator import SQLGenerator
from tests.fakes import FakeLLM, clarification, generation, refusal, sql_explanation

QUESTION = "Show me the top 5 employees with the highest salaries."


@pytest.fixture
def pm(demo_schema) -> PromptManager:
    return PromptManager(demo_schema, reference_date=date(2026, 11, 15))


def test_generate_sends_generation_prompt_and_returns_outcome(pm, demo_schema):
    sql = "SELECT e.employee_name, e.salary FROM employees AS e ORDER BY e.salary DESC LIMIT 5"
    llm = FakeLLM([generation(sql, tables=["employees", "Employees"], columns=["employees.salary"] * 2)])
    context = SchemaRetriever(demo_schema).retrieve(QUESTION)
    outcome = SQLGenerator(llm, pm).generate(QUESTION, config=PRESETS["C"], schema_context=context)
    prompt, output_model = llm.calls[0]
    assert prompt.stage == "generation" and output_model is SQLGenerationResult
    assert prompt.has_section("examples") and QUESTION in prompt.section("question").text
    assert outcome.result.sql == sql and outcome.is_usable
    assert outcome.result.tables_used == ["employees"]          # de-duplicated, case-insensitive
    assert outcome.result.columns_used == ["employees.salary"]
    assert outcome.prompt is prompt and outcome.call.stage == "generation"


@pytest.mark.parametrize("config_name, has_examples", [("A", False), ("B", False), ("D", True), ("C", True)])
def test_configuration_controls_prompt(pm, config_name, has_examples):
    llm = FakeLLM([generation()])
    SQLGenerator(llm, pm).generate(QUESTION, config=PRESETS[config_name])
    assert llm.prompts[0].has_section("examples") is has_examples
    assert llm.prompts[0].config_name == config_name


def test_answerable_intent_without_sql_is_a_problem(pm):
    outcome = SQLGenerator(FakeLLM([generation(None)]), pm).generate(QUESTION, config=PRESETS["C"])
    assert outcome.problems and not outcome.is_usable


@pytest.mark.parametrize("result", [clarification(), refusal(), refusal("out_of_scope")])
def test_non_sql_outcomes_are_not_executed_but_not_errors(pm, result):
    outcome = SQLGenerator(FakeLLM([result]), pm).generate("Show the best employees", config=PRESETS["C"])
    assert outcome.problems == [] and not outcome.is_usable


def test_follow_up_passes_history(pm):
    history = [ConversationTurn(question="Show employees earning more than 50,000",
                                sql="SELECT employee_name, salary FROM employees WHERE salary > 50000",
                                tables_used=["employees"], result_columns=["employee_name", "salary"], row_count=52)]
    llm = FakeLLM([generation(intent="follow_up_modification")])
    SQLGenerator(llm, pm).generate("Now sort them by department", config=PRESETS["C"], history=history)
    prompt = llm.prompts[0]
    assert "WHERE salary > 50000" in prompt.section("history").text
    assert prompt.selected_examples[-1].example.id == "E16"


def test_correction_uses_correction_prompt(pm):
    llm = FakeLLM([generation("SELECT employee_name FROM employees")])
    outcome = SQLGenerator(llm, pm).correct(
        QUESTION, failed_sql="SELECT nme FROM employees", problems=["no such column: nme"],
        attempt=1, max_attempts=2, config=PRESETS["C"])
    prompt = llm.prompts[0]
    assert prompt.stage == "correction"
    assert "no such column: nme" in prompt.section("failed_attempt").text
    assert outcome.is_usable


@pytest.mark.parametrize("attempt, max_attempts", [(0, 2), (3, 2), (1, 0)])
def test_correction_attempt_must_be_within_budget(pm, attempt, max_attempts):
    llm = FakeLLM([generation()])
    with pytest.raises(ValueError):
        SQLGenerator(llm, pm).correct(QUESTION, failed_sql="x", problems=[], attempt=attempt,
                                      max_attempts=max_attempts, config=PRESETS["C"])
    assert llm.calls == []  # no API call was made


def test_explain(pm):
    llm = FakeLLM([sql_explanation("Counts all sales.")])
    outcome = SQLGenerator(llm, pm).explain("SELECT COUNT(*) FROM sales", level="technical")
    prompt, output_model = llm.calls[0]
    assert prompt.stage == "explanation" and output_model is SQLExplanationResult
    assert "for a technical reader" in prompt.section("sql").text
    assert outcome.result.summary == "Counts all sales."


def test_llm_errors_propagate(pm):
    llm = FakeLLM([LLMRateLimitError("slow down", retry_after=3)])
    with pytest.raises(LLMRateLimitError):
        SQLGenerator(llm, pm).generate(QUESTION, config=PRESETS["C"])
