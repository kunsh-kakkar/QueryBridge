"""Tests for backend/sql_corrector.py: the bounded validate/verify/execute/correct loop."""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from backend.database import sanitize_db_error
from backend.exceptions import LLMRateLimitError, LLMServiceError
from backend.models import VerificationIssue
from backend.prompt_manager import PRESETS, PromptManager
from backend.sql_corrector import ExecutionAttempt, SQLCorrector
from backend.sql_generator import SQLGenerator
from backend.sql_validator import SQLValidator
from backend.sql_verifier import SQLVerifier
from database.init_db import logical_fingerprint
from tests.fakes import FakeLLM, clarification, generation, refusal, verdict

QUESTION = "Show me the top 5 employees with the highest salaries."
GOOD_SQL = "SELECT e.employee_name, e.salary FROM employees AS e ORDER BY e.salary DESC LIMIT 5"
BAD_COLUMN_SQL = "SELECT e.employee_nme FROM employees AS e ORDER BY e.salary DESC LIMIT 5"
HIGH_ISSUE = VerificationIssue(category="logic", severity="high", description="wrong ordering")


@pytest.fixture
def make(demo_schema, demo_db):
    def _make(*script, max_semantic_calls: int = 2):
        llm = FakeLLM(list(script))
        pm = PromptManager(demo_schema, reference_date=date(2026, 11, 15))
        generator = SQLGenerator(llm, pm)
        verifier = SQLVerifier(demo_schema, db=demo_db, llm=llm, prompt_manager=pm)
        corrector = SQLCorrector(generator, SQLValidator("sqlite", max_rows=1000), verifier,
                                 max_semantic_calls=max_semantic_calls)
        return llm, generator, corrector
    return _make


@pytest.fixture
def execute(demo_db):
    def _execute(sql: str) -> ExecutionAttempt:
        try:
            with demo_db.connect() as conn:
                return ExecutionAttempt(ok=True, result=conn.execute(text(sql)).all())
        except DBAPIError as exc:
            return ExecutionAttempt(ok=False, error=sanitize_db_error(exc))
    return _execute


def run(llm, generator, corrector, config, execute=None, question=QUESTION):
    initial = generator.generate(question, config=config)
    return corrector.refine(question, initial, config=config, execute=execute)


def test_happy_path_full_pipeline(make, execute):
    llm, gen, cor = make(generation(GOOD_SQL), verdict(True))
    result = run(llm, gen, cor, PRESETS["C"], execute)
    assert result.status == "success" and result.corrections_used == 0
    assert llm.stages == ["generation", "verification"]
    assert len(result.execution.result) == 5
    assert result.final_sql.endswith("LIMIT 5")
    assert [a.outcome for a in result.attempts] == ["passed"]
    assert len(result.llm_calls) == 2 and len(result.prompts) == 2


def test_config_b_runs_without_verification_or_correction(make, execute):
    llm, gen, cor = make(generation(GOOD_SQL))
    result = run(llm, gen, cor, PRESETS["B"], execute)
    assert result.status == "success" and llm.stages == ["generation"]
    assert result.verification is None


def test_config_b_does_not_correct_failures(make, execute):
    llm, gen, cor = make(generation(BAD_COLUMN_SQL))
    result = run(llm, gen, cor, PRESETS["B"], execute)
    assert result.status == "failed" and llm.stages == ["generation"]
    assert "no such column" in result.problems[0]


def test_static_failure_is_corrected(make, execute):
    llm, gen, cor = make(generation(BAD_COLUMN_SQL), generation(GOOD_SQL), verdict(True))
    result = run(llm, gen, cor, PRESETS["C"], execute)
    assert result.status == "success" and result.corrections_used == 1
    assert llm.stages == ["generation", "correction", "verification"]
    correction_prompt = llm.prompts[1]
    assert "Unknown column 'employee_nme'" in correction_prompt.section("failed_attempt").text
    assert [a.outcome for a in result.attempts] == ["static_failed", "passed"]


def test_unsafe_sql_is_blocked_without_regeneration(make, execute, demo_db_path):
    before = logical_fingerprint(demo_db_path)
    llm, gen, cor = make(generation("DROP TABLE employees"))
    result = run(llm, gen, cor, PRESETS["C"], execute)
    assert result.status == "blocked" and result.corrections_used == 0
    assert llm.stages == ["generation"]  # never sent back to the model
    assert result.execution is None and result.final_sql is None
    assert logical_fingerprint(demo_db_path) == before


def test_blocked_correction_stops_the_loop(make, execute):
    llm, gen, cor = make(generation(BAD_COLUMN_SQL), generation("SELECT 1; DELETE FROM sales"))
    result = run(llm, gen, cor, PRESETS["C"], execute)
    assert result.status == "blocked" and llm.stages == ["generation", "correction"]


@pytest.mark.parametrize("budget", [1, 2, 3])
def test_budget_is_never_exceeded(make, execute, budget):
    script = [generation(BAD_COLUMN_SQL)] * (budget + 5)
    llm, gen, cor = make(*script)
    config = PRESETS["C"].model_copy(update={"max_correction_attempts": budget})
    result = run(llm, gen, cor, config, execute)
    assert result.status == "failed"
    assert result.corrections_used == budget
    assert llm.stages.count("correction") == budget
    assert len(result.attempts) == budget + 1


def test_execution_error_is_corrected(make):
    calls = []

    def flaky_execute(sql: str) -> ExecutionAttempt:
        calls.append(sql)
        if len(calls) == 1:
            return ExecutionAttempt(ok=False, error="interrupted: query timed out")
        return ExecutionAttempt(ok=True, result=[("x",)])

    llm, gen, cor = make(generation(GOOD_SQL), verdict(True), generation(GOOD_SQL), verdict(True))
    result = run(llm, gen, cor, PRESETS["C"], flaky_execute)
    assert result.status == "success" and result.corrections_used == 1
    assert "query timed out" in llm.prompts[2].section("failed_attempt").text
    assert [a.outcome for a in result.attempts] == ["execution_failed", "passed"]


def test_reviewer_fix_is_used_without_extra_generation(make, execute):
    fixed = "SELECT e.employee_name, e.salary FROM employees AS e ORDER BY e.salary DESC LIMIT 5"
    wrong = "SELECT e.employee_name, e.salary FROM employees AS e ORDER BY e.salary ASC LIMIT 5"
    llm, gen, cor = make(generation(wrong), verdict(False, issues=[HIGH_ISSUE], corrected_sql=fixed))
    result = run(llm, gen, cor, PRESETS["C"], execute)
    assert result.status == "success" and result.corrections_used == 1
    assert llm.stages == ["generation", "verification"]  # reviewer SQL is not re-reviewed by itself
    assert result.generation.sql == fixed
    assert any("Adjusted by the AI reviewer" in a for a in result.generation.assumptions)
    assert [a.source for a in result.attempts] == ["generation", "reviewer"]


def test_reviewer_issue_without_fix_triggers_correction(make, execute):
    llm, gen, cor = make(generation(GOOD_SQL), verdict(False, issues=[HIGH_ISSUE]),
                         generation(GOOD_SQL), verdict(True))
    result = run(llm, gen, cor, PRESETS["C"], execute)
    assert result.status == "success"
    assert llm.stages == ["generation", "verification", "correction", "verification"]
    assert "[high] logic: wrong ordering" in llm.prompts[2].section("failed_attempt").text


def test_worst_case_is_five_llm_calls(make, execute):
    always_wrong = verdict(False, issues=[HIGH_ISSUE])
    llm, gen, cor = make(generation(GOOD_SQL), always_wrong, generation(GOOD_SQL), always_wrong,
                         generation(GOOD_SQL), always_wrong, always_wrong)
    result = run(llm, gen, cor, PRESETS["C"], execute)
    assert len(llm.calls) == 5
    assert llm.stages == ["generation", "verification", "correction", "verification", "correction"]
    assert result.status == "success" and result.semantic_calls == 2


def test_budget_spent_with_only_reviewer_concerns_returns_with_warning(make, execute):
    config = PRESETS["C"].model_copy(update={"max_correction_attempts": 0})
    llm, gen, cor = make(generation(GOOD_SQL), verdict(False, issues=[HIGH_ISSUE]))
    result = run(llm, gen, cor, config, execute)
    assert result.status == "success" and result.execution.ok
    assert any("reviewer still had concerns" in w for w in result.warnings)


def test_correction_can_conclude_out_of_scope(make, execute):
    llm, gen, cor = make(generation(BAD_COLUMN_SQL), refusal("out_of_scope"))
    result = run(llm, gen, cor, PRESETS["C"], execute)
    assert result.status == "no_sql" and result.generation.intent == "out_of_scope"


@pytest.mark.parametrize("initial", [clarification(), refusal()])
def test_non_sql_initial_result(make, execute, initial):
    llm, gen, cor = make(initial)
    result = run(llm, gen, cor, PRESETS["C"], execute)
    assert result.status == "no_sql" and llm.stages == ["generation"]


def test_llm_error_during_correction_fails_cleanly(make, execute):
    llm, gen, cor = make(generation(BAD_COLUMN_SQL), LLMRateLimitError("slow down", retry_after=2))
    result = run(llm, gen, cor, PRESETS["C"], execute)
    assert result.status == "failed"
    assert any("Correction unavailable: slow down" in p for p in result.problems)


def test_reviewer_outage_still_succeeds(make, execute):
    llm, gen, cor = make(generation(GOOD_SQL), LLMServiceError("overloaded"))
    result = run(llm, gen, cor, PRESETS["C"], execute)
    assert result.status == "success"
    assert any("reviewer could not be reached" in w for w in result.warnings)


def test_without_execute_hook_validated_sql_is_returned(make):
    llm, gen, cor = make(generation(GOOD_SQL), verdict(True))
    result = run(llm, gen, cor, PRESETS["C"], execute=None)
    assert result.status == "success" and result.execution is None and result.final_sql
