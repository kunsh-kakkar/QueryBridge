"""End-to-end tests for backend/pipeline.py (real DB, validator, verifier, executor; FakeLLM)."""

from __future__ import annotations

from datetime import date

import pytest

from backend.conversation_manager import ConversationManager
from backend.exceptions import LLMAuthenticationError
from backend.history_store import HistoryStore
from backend.pipeline import QueryPipeline, composite_confidence, confidence_label
from backend.prompt_manager import PRESETS
from database.init_db import logical_fingerprint
from tests.fakes import FakeLLM, clarification, generation, refusal, sql_explanation, verdict

OVER_50K = "SELECT e.employee_name, e.salary FROM employees AS e WHERE e.salary > 50000"
SORTED = ("SELECT e.employee_name, e.salary, d.department_name FROM employees AS e JOIN departments AS d "
          "ON d.department_id = e.department_id WHERE e.salary > 50000 ORDER BY d.department_name")


@pytest.fixture
def make(demo_db, demo_schema, tmp_path):
    def _make(*script, config="C", history=True):
        llm = FakeLLM(list(script))
        pipeline = QueryPipeline(db=demo_db, schema=demo_schema, llm=llm, config=PRESETS[config],
                                 reference_date=date(2026, 11, 15),
                                 history=HistoryStore(tmp_path / "history.db") if history else None)
        return llm, pipeline
    return _make


def test_success_end_to_end(make):
    llm, pipeline = make(generation(OVER_50K, tables=["employees"], confidence=0.92), verdict(True))
    result = pipeline.ask("Show employees earning more than 50,000")
    assert result.status == "success" and result.ok
    assert result.formatted.row_count == 52 and result.execution.columns == ["employee_name", "salary"]
    assert result.sql.endswith("LIMIT 1001")
    assert result.tables_used == ["employees"]
    assert "employees.salary" in result.columns_used
    assert result.complexity.level == "Simple"
    assert result.confidence_label == "High" and 0.8 <= result.confidence <= 0.99
    assert [s.name for s in result.trace.steps][:3] == ["Input check", "Schema selection", "SQL generation"]
    assert len(result.llm_calls) == 2 and result.history_id is not None
    assert result.charts and result.explanation.summary


def test_two_turn_follow_up_uses_memory(make):
    llm, pipeline = make(generation(OVER_50K), verdict(True),
                         generation(SORTED, intent="follow_up_modification"), verdict(True))
    conversation = ConversationManager()
    pipeline.ask("Show employees earning more than 50,000", conversation=conversation)
    second = pipeline.ask("Now sort them by department", conversation=conversation)
    assert second.status == "success" and second.is_follow_up
    follow_prompt = llm.prompts[2]
    assert OVER_50K in follow_prompt.section("history").text
    assert "appears to refer to the previous turn" in follow_prompt.section("context").text
    assert "employees" in second.schema_context.ranked_tables or "departments" in second.schema_context.ranked_tables
    assert [t.status for t in conversation.turns] == ["success", "success"]
    assert conversation.turns[0].row_count == 52 and conversation.turns[0].result_columns == ["employee_name", "salary"]


def test_clarification_round_trip(make):
    llm, pipeline = make(clarification("How should performance be measured?",
                                       ["Total sales amount", "Average review rating"]),
                         generation("SELECT e.employee_name, SUM(s.sale_amount) AS total_sales FROM employees e "
                                    "JOIN sales s ON s.employee_id = e.employee_id GROUP BY e.employee_name "
                                    "ORDER BY total_sales DESC LIMIT 5"), verdict(True))
    conversation = ConversationManager()
    asked = pipeline.ask("Show me the highest-performing employees", conversation=conversation)
    assert asked.status == "clarification" and asked.clarification_options[0] == "Total sales amount"
    assert conversation.pending_clarification is not None
    answered = pipeline.ask("Show me the highest-performing employees", conversation=conversation,
                            clarification_answer="Total sales amount")
    assert answered.status == "success" and answered.formatted.row_count == 5
    assert "Total sales amount" in llm.prompts[1].section("question").text
    assert conversation.pending_clarification is None


def test_explicit_write_is_refused_without_llm_call(make, demo_db_path):
    before = logical_fingerprint(demo_db_path)
    llm, pipeline = make()
    result = pipeline.ask("Delete all sales from 2023")
    assert result.status == "refused" and llm.calls == []
    assert "read-only" in result.message
    assert logical_fingerprint(demo_db_path) == before


def test_llm_refusal_and_out_of_scope(make):
    llm, pipeline = make(refusal("unsafe_request"), refusal("out_of_scope"))
    assert pipeline.ask("Give everyone a 10% raise").status == "refused"
    assert pipeline.ask("What is each employee's phone number?").status == "out_of_scope"


def test_unsafe_generated_sql_is_blocked(make, demo_db_path):
    before = logical_fingerprint(demo_db_path)
    llm, pipeline = make(generation("SELECT 1; DROP TABLE employees"))
    result = pipeline.ask("List the employees")
    assert result.status == "blocked" and "blocked" in result.message
    assert llm.stages == ["generation"] and result.execution is None
    assert logical_fingerprint(demo_db_path) == before


def test_static_error_is_corrected(make):
    llm, pipeline = make(generation("SELECT category, COUNT(*) AS n FROM prodcts GROUP BY category"),
                         generation("SELECT category, COUNT(*) AS product_count FROM products GROUP BY category"),
                         verdict(True))
    result = pipeline.ask("How many products per category?")
    assert result.status == "success" and result.refinement.corrections_used == 1
    assert any(s.name.startswith("Corrected query") for s in result.trace.steps)
    assert result.confidence < 0.9


def test_failure_after_budget(make):
    llm, pipeline = make(*[generation("SELECT nope FROM employees")] * 3)
    result = pipeline.ask("List employees")
    assert result.status == "failed" and "after 2 correction attempts" in result.message
    assert llm.stages.count("correction") == 2


def test_empty_result_status(make):
    llm, pipeline = make(generation("SELECT employee_name FROM employees WHERE salary > 9999999"), verdict(True))
    result = pipeline.ask("Employees earning more than 10 million")
    assert result.status == "empty" and result.ok and "no rows" in result.message


def test_input_errors(make):
    _, pipeline = make()
    assert pipeline.ask("   ").status == "error"
    assert "too long" in pipeline.ask("x" * 5000).message


def test_llm_errors_are_reported_safely(make):
    llm, pipeline = make(LLMAuthenticationError("The Gemini API key is invalid.", detail="401"))
    result = pipeline.ask("List employees")
    assert result.status == "error" and result.message == "The Gemini API key is invalid."


def test_config_b_pipeline_makes_one_call(make):
    llm, pipeline = make(generation(OVER_50K), config="B")
    assert pipeline.ask("Show employees earning more than 50,000").status == "success"
    assert llm.stages == ["generation"]


def test_history_records_every_outcome(make, tmp_path):
    llm, pipeline = make(generation(OVER_50K), verdict(True))
    pipeline.ask("Show employees earning more than 50,000")
    pipeline.ask("Delete all sales")
    entries = pipeline.history.list()
    assert [e.status for e in entries] == ["refused", "success"]
    assert entries[1].row_count == 52 and entries[1].llm_calls == 2 and entries[1].config_name == "C"
    assert all("password" not in (e.database_label or "") for e in entries)


def test_run_sql_rerun_without_llm(make):
    llm, pipeline = make()
    rerun = pipeline.run_sql("SELECT COUNT(*) AS n FROM employees")
    assert rerun.status == "success" and rerun.formatted.dataframe.iloc[0, 0] == 60 and llm.calls == []
    assert pipeline.run_sql("DROP TABLE employees").status == "blocked"
    assert pipeline.run_sql("SELECT nme FROM employees").status == "failed"


def test_explain_sql(make):
    llm, pipeline = make(sql_explanation("Counts employees."))
    explanation, validation, error = pipeline.explain_sql("SELECT COUNT(*) FROM employees", level="technical")
    assert explanation.summary == "Counts employees." and validation.is_safe and error is None
    assert llm.stages == ["explanation"]


def test_composite_confidence_and_labels():
    assert composite_confidence(0.9, verification=None, corrections_used=0, warning_count=0, complexity=None) == 0.9
    assert composite_confidence(0.9, verification=None, corrections_used=2, warning_count=3, complexity=None) == 0.61
    assert composite_confidence(1.5, verification=None, corrections_used=0, warning_count=0, complexity=None) == 0.99
    assert confidence_label(0.85, 0.6) == "High" and confidence_label(0.7, 0.6) == "Medium"
    assert confidence_label(0.4, 0.6) == "Low"


def test_from_settings_builds_a_pipeline(make_settings, demo_db_path, tmp_path):
    settings = make_settings(database_url=f"sqlite:///{demo_db_path}", gemini_api_key="AIza-test",
                             data_dir=tmp_path)
    pipeline = QueryPipeline.from_settings(settings, llm=FakeLLM())
    assert pipeline.schema.dialect == "sqlite" and len(pipeline.schema.tables) == 8
    assert pipeline.config.name == "C" and pipeline.validator.max_rows == settings.max_result_rows


def test_progress_callback_reports_stages(make):
    llm, pipeline = make(generation(OVER_50K), verdict(True))
    steps: list[str] = []
    pipeline.ask("Show employees earning more than 50,000", on_step=steps.append)
    assert steps == ["Checking your question…", "Selecting the relevant schema…", "Generating SQL with AI…",
                     "Validating, verifying and running the query…", "Formatting results…"]
