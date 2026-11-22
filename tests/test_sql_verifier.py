"""Tests for backend/sql_verifier.py (static checks + semantic verification)."""

from __future__ import annotations

from datetime import date

import pytest

from backend.exceptions import LLMServiceError
from backend.models import VerificationIssue
from backend.prompt_manager import PRESETS, PromptManager
from backend.sql_validator import SQLValidator
from backend.sql_verifier import SQLVerifier
from tests.fakes import FakeLLM, verdict

TOP5 = "Show me the top 5 employees with the highest salaries."


@pytest.fixture(scope="module")
def validator() -> SQLValidator:
    return SQLValidator("sqlite", max_rows=1000)


@pytest.fixture(scope="module")
def verifier(demo_schema, demo_db) -> SQLVerifier:
    return SQLVerifier(demo_schema, db=demo_db)


def check(verifier, validator, sql, question=None):
    return verifier.static_verify(validator.validate(sql), question=question)


def failed(report, name):
    return [c for c in report.checks if c.name == name and not c.passed]


def test_valid_query_passes_all_checks(verifier, validator):
    report = check(verifier, validator,
                   "SELECT e.employee_name, d.department_name FROM employees AS e "
                   "JOIN departments AS d ON d.department_id = e.department_id", "employees and departments")
    assert report.static_passed and report.passed
    assert report.referenced_tables == ["employees", "departments"]
    assert set(report.referenced_columns) >= {"employees.employee_name", "departments.department_name"}
    assert report.explain_plan  # EXPLAIN QUERY PLAN ran
    assert report.findings == ["all automated checks passed (tables, columns, joins, dry run)"]


def test_unknown_table_with_suggestion(verifier, validator):
    report = check(verifier, validator, "SELECT * FROM employes")
    assert not report.static_passed
    message = failed(report, "tables_exist")[0].message
    assert "'employes'" in message and "did you mean 'employees'" in message


def test_unknown_column_with_suggestion(verifier, validator):
    report = check(verifier, validator, "SELECT employee_nme FROM employees")
    message = failed(report, "columns_exist")[0].message
    assert "Unknown column 'employee_nme'" in message and "employees.employee_name" in message


def test_ambiguous_column(verifier, validator):
    report = check(verifier, validator, "SELECT department_id FROM employees e JOIN departments d "
                                        "ON d.department_id = e.department_id")
    assert "ambiguous" in failed(report, "columns_exist")[0].message


def test_dry_run_catches_runtime_compile_errors(verifier, validator):
    report = check(verifier, validator, "SELECT employee_name FROM employees WHERE no_such_fn(salary) > 1")
    errors = failed(report, "dry_run")
    assert errors and "no such function" in errors[0].message
    assert not report.static_passed


def test_undocumented_join_is_a_warning_not_an_error(verifier, validator):
    report = check(verifier, validator, "SELECT e.employee_name FROM employees e JOIN projects p "
                                        "ON p.project_id = e.department_id")
    assert report.static_passed
    assert "not a documented relationship" in failed(report, "joins")[0].message
    assert any("not a documented relationship" in f for f in report.findings)


def test_self_join_on_manager_is_documented(verifier, validator):
    report = check(verifier, validator, "SELECT e.employee_name FROM employees e JOIN employees m "
                                        "ON m.employee_id = e.manager_id WHERE e.salary > m.salary")
    assert not failed(report, "joins") and report.static_passed


@pytest.mark.parametrize("sql", ["SELECT e.employee_name, d.department_name FROM employees e, departments d",
                                 "SELECT e.employee_name FROM employees e CROSS JOIN departments d"])
def test_cartesian_product_warning(verifier, validator, sql):
    assert "cartesian product" in failed(check(verifier, validator, sql), "joins")[0].message


def test_comma_join_linked_in_where_is_fine(verifier, validator):
    report = check(verifier, validator, "SELECT e.employee_name FROM employees e, departments d "
                                        "WHERE d.department_id = e.department_id")
    assert not any("cartesian" in c.message for c in failed(report, "joins"))


def test_top_n_mismatch_and_missing_order(verifier, validator):
    mismatch = check(verifier, validator, "SELECT employee_name FROM employees ORDER BY salary DESC LIMIT 10", TOP5)
    assert "uses LIMIT 10" in failed(mismatch, "answer_shape")[0].message
    missing = check(verifier, validator, "SELECT employee_name, salary FROM employees", TOP5)
    messages = " ".join(c.message for c in failed(missing, "answer_shape"))
    assert "no LIMIT 5" in messages and "no ORDER BY" in messages
    good = check(verifier, validator, "SELECT employee_name FROM employees ORDER BY salary DESC LIMIT 5", TOP5)
    assert not failed(good, "answer_shape")
    days = check(verifier, validator, "SELECT COUNT(*) FROM sales WHERE sale_date > '2026-08-01'",
                 "How many sales in the last 90 days?")
    assert not failed(days, "answer_shape")  # 'last 90 days' is not a top-N request


def test_select_star_warning(verifier, validator):
    assert failed(check(verifier, validator, "SELECT * FROM products"), "select_star")


def test_unsafe_validation_short_circuits(verifier, validator):
    report = verifier.static_verify(validator.validate("DROP TABLE employees"))
    assert not report.static_passed and report.checks[0].name == "validated"


def test_no_database_skips_dry_run(demo_schema, validator):
    report = SQLVerifier(demo_schema).static_verify(validator.validate("SELECT employee_name FROM employees"))
    assert report.static_passed and report.explain_plan == []


# ------------------------------------------------------------------ semantic stage
@pytest.fixture
def pm(demo_schema) -> PromptManager:
    return PromptManager(demo_schema, reference_date=date(2026, 11, 15))


def test_semantic_verification_passes_findings_to_reviewer(demo_schema, demo_db, validator, pm):
    llm = FakeLLM([verdict(True)])
    verifier = SQLVerifier(demo_schema, db=demo_db, llm=llm, prompt_manager=pm)
    sql = "SELECT employee_name FROM employees ORDER BY salary DESC LIMIT 10"
    report = verifier.static_verify(validator.validate(sql), question=TOP5)
    verifier.semantic_verify(report, question=TOP5, sql=sql, config=PRESETS["C"])
    prompt = llm.prompts[0]
    assert prompt.stage == "verification"
    assert "uses LIMIT 10" in prompt.section("candidate").text
    assert report.semantic.is_correct and report.passed and report.semantic_call is not None


def test_blocking_verdict_produces_problems(demo_schema, demo_db, validator, pm):
    issue = VerificationIssue(category="ordering", severity="high", description="sorts ascending instead of descending")
    llm = FakeLLM([verdict(False, issues=[issue])])
    verifier = SQLVerifier(demo_schema, db=demo_db, llm=llm, prompt_manager=pm)
    sql = "SELECT employee_name FROM employees ORDER BY salary LIMIT 5"
    report = verifier.semantic_verify(verifier.static_verify(validator.validate(sql), question=TOP5),
                                      question=TOP5, sql=sql, config=PRESETS["C"])
    assert not report.passed and report.static_passed
    assert report.problems == ["[high] ordering: sorts ascending instead of descending"]


def test_semantic_skipped_when_static_fails(demo_schema, demo_db, validator, pm):
    llm = FakeLLM([verdict(True)])
    verifier = SQLVerifier(demo_schema, db=demo_db, llm=llm, prompt_manager=pm)
    report = verifier.static_verify(validator.validate("SELECT nme FROM employees"))
    verifier.semantic_verify(report, question="names", sql="SELECT nme FROM employees", config=PRESETS["C"])
    assert llm.calls == [] and "static checks failed" in report.semantic_skipped_reason


def test_reviewer_outage_degrades_gracefully(demo_schema, demo_db, validator, pm):
    llm = FakeLLM([LLMServiceError("overloaded")])
    verifier = SQLVerifier(demo_schema, db=demo_db, llm=llm, prompt_manager=pm)
    sql = "SELECT employee_name FROM employees"
    report = verifier.semantic_verify(verifier.static_verify(validator.validate(sql)), question="names",
                                      sql=sql, config=PRESETS["C"])
    assert report.passed and report.semantic is None
    assert "reviewer unavailable" in report.semantic_skipped_reason and report.warnings
