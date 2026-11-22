"""Tests for backend/sql_executor.py: validation-first execution, timeout, row cap."""

from __future__ import annotations

import time

import pytest

from backend.database import DatabaseManager
from backend.sql_executor import QueryExecutionError, QueryTimeoutError, SQLExecutor
from backend.sql_validator import SQLValidator
from database.init_db import logical_fingerprint

RUNAWAY = "WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM n) SELECT COUNT(*) FROM n"


@pytest.fixture
def executor(demo_db) -> SQLExecutor:
    return SQLExecutor(demo_db, SQLValidator("sqlite", max_rows=10), timeout_seconds=0.5)


def test_executes_and_measures(executor):
    result = executor.execute("SELECT employee_name, salary FROM employees ORDER BY salary DESC LIMIT 5")
    assert result.columns == ["employee_name", "salary"] and result.row_count == 5
    assert not result.truncated and result.execution_time_ms >= 0
    assert result.rows[0][1] >= result.rows[-1][1]


def test_truncation_is_detected_exactly(demo_db):
    executor = SQLExecutor(demo_db, SQLValidator("sqlite", max_rows=8))
    exact = executor.execute("SELECT department_name FROM departments")  # exactly 8 rows
    assert exact.row_count == 8 and not exact.truncated
    more = executor.execute("SELECT employee_name FROM employees")
    assert more.row_count == 8 and more.truncated and more.warnings


def test_timeout_stops_runaway_queries(executor):
    start = time.monotonic()
    with pytest.raises(QueryTimeoutError) as excinfo:
        executor.execute(RUNAWAY)
    assert time.monotonic() - start < 3
    assert "0.5-second limit" in excinfo.value.user_message


def test_connection_is_reusable_after_timeout(executor):
    with pytest.raises(QueryTimeoutError):
        executor.execute(RUNAWAY)
    assert executor.execute("SELECT COUNT(*) FROM products").rows == [(40,)]


@pytest.mark.parametrize("sql", ["DELETE FROM sales", "SELECT 1; DROP TABLE employees", "PRAGMA writable_schema=1"])
def test_executor_validates_before_running(executor, demo_db_path, sql):
    before = logical_fingerprint(demo_db_path)
    with pytest.raises(QueryExecutionError, match="not allowed"):
        executor.execute(sql)
    assert logical_fingerprint(demo_db_path) == before


def test_database_errors_are_sanitised(executor):
    with pytest.raises(QueryExecutionError) as excinfo:
        executor.execute("SELECT nme FROM employees")
    assert excinfo.value.detail == "no such column: nme"


def test_colon_literals_are_not_bind_parameters(executor):
    result = executor.execute("SELECT '10:30' AS t, strftime('%H:%M', '2024-01-01 09:15') AS hm")
    assert result.rows == [("10:30", "09:15")]


def test_duplicate_column_names_are_made_unique(executor):
    frame = executor.execute("SELECT e.department_id, d.department_id FROM employees e "
                             "JOIN departments d ON d.department_id = e.department_id").to_dataframe()
    assert list(frame.columns) == ["department_id", "department_id_2"]


def test_hook_reports_errors_as_feedback(executor):
    hook = executor.as_hook()
    ok = hook("SELECT COUNT(*) FROM employees")
    assert ok.ok and ok.result.rows == [(60,)]
    failed = hook("SELECT nme FROM employees")
    assert not failed.ok and failed.error == "no such column: nme"


def test_missing_database_file_is_reported(tmp_path, demo_db_path):
    import shutil

    copy = tmp_path / "gone.db"
    shutil.copyfile(demo_db_path, copy)
    executor = SQLExecutor(DatabaseManager.for_sqlite_file(copy), SQLValidator("sqlite"))
    copy.unlink()
    with pytest.raises(QueryExecutionError):
        executor.execute("SELECT 1")
