"""Tests for backend/sql_validator.py, including a red-team suite of attack SQL."""

from __future__ import annotations

import pytest
import sqlglot

from backend.sql_validator import SQLValidator

from evaluation.red_team import ATTACKS  # noqa: E402  (shared with the evaluation report)

LEGITIMATE = [
    "SELECT employee_name, salary FROM employees WHERE salary > 50000",
    "SELECT e.employee_name, d.department_name FROM employees AS e JOIN departments AS d "
    "ON d.department_id = e.department_id ORDER BY d.department_name",
    "SELECT category, COUNT(*) AS n FROM products GROUP BY category HAVING COUNT(*) > 3",
    "WITH t AS (SELECT department_id, AVG(salary) AS a FROM employees GROUP BY department_id) SELECT * FROM t",
    "WITH RECURSIVE chain(id, lvl) AS (SELECT employee_id, 0 FROM employees WHERE manager_id IS NULL "
    "UNION ALL SELECT e.employee_id, c.lvl + 1 FROM employees e JOIN chain c ON e.manager_id = c.id) SELECT * FROM chain",
    "SELECT employee_name, RANK() OVER (PARTITION BY department_id ORDER BY salary DESC) AS r FROM employees",
    "SELECT city FROM customers UNION SELECT location FROM departments",
    "SELECT CASE WHEN salary > 100000 THEN 'high' ELSE 'normal' END AS band, COUNT(*) FROM employees GROUP BY band",
    "SELECT product_name FROM products WHERE product_name LIKE '%DROP TABLE%'",   # keywords inside a string
    "SELECT 'a;b' AS text_with_semicolon",
    "SELECT REPLACE(product_name, 'Pro', 'Professional') AS renamed FROM products",  # REPLACE() function is fine
    "SELECT strftime('%Y', sale_date) AS y, SUM(sale_amount) FROM sales GROUP BY y",
    "(SELECT employee_name FROM employees)",
    "SELECT employee_name FROM employees;",                                      # trailing semicolon is fine
    "SELECT 1 FROM employees WHERE salary > 1 -- trailing comment",
    "SELECT e.employee_name FROM main.employees AS e",
    "SELECT COUNT(DISTINCT customer_id) FROM sales WHERE sale_date BETWEEN '2025-01-01' AND '2025-12-31'",
]


@pytest.fixture
def validator() -> SQLValidator:
    return SQLValidator("sqlite", max_rows=100)


@pytest.mark.parametrize("dialect, sql", ATTACKS, ids=lambda v: v if isinstance(v, str) and len(v) < 50 else None)
def test_attacks_are_blocked_and_never_regenerated(dialect, sql):
    report = SQLValidator(dialect).validate(sql)
    assert report.is_safe is False and report.safe_sql is None, sql
    assert report.is_blocked, (sql, report.violations)
    assert not report.is_correctable


def test_red_team_suite_size():
    assert len(ATTACKS) >= 60


@pytest.mark.parametrize("sql", LEGITIMATE)
def test_legitimate_queries_are_not_over_blocked(validator, sql):
    report = validator.validate(sql)
    assert report.is_safe, (sql, report.violations)
    assert sqlglot.parse_one(report.safe_sql, read="sqlite") is not None


@pytest.mark.parametrize("sql", ["", "   ", None])
def test_empty_sql_is_correctable(validator, sql):
    report = validator.validate(sql)
    assert not report.is_safe and report.is_correctable


@pytest.mark.parametrize("sql", ["SELECT name FROM employees WHERE salary >", "SELECT (1 FROM employees",
                                 "SELECT name FROM employees LIMIT (SELECT 5)"])
def test_syntax_errors_are_correctable_not_blocked(validator, sql):
    report = validator.validate(sql)
    assert not report.is_safe and report.is_correctable and not report.is_blocked
    assert report.violations[0].kind == "syntax"


def test_row_cap_added_when_missing(validator):
    report = validator.validate("SELECT employee_name FROM employees")
    assert report.limit_added and report.applied_limit == 101  # max_rows + 1 lets the executor detect truncation
    assert report.original_limit is None
    assert report.safe_sql.endswith("LIMIT 101")


def test_small_limit_kept_and_large_limit_reduced(validator):
    kept = validator.validate("SELECT employee_name FROM employees ORDER BY salary DESC LIMIT 5")
    assert (kept.original_limit, kept.applied_limit, kept.limit_added) == (5, 5, False)
    reduced = validator.validate("SELECT employee_name FROM employees LIMIT 5000")
    assert reduced.original_limit == 5000 and reduced.applied_limit == 101
    assert any("reduced" in w for w in reduced.warnings)


def test_revalidating_capped_sql_adds_no_warning(validator):
    """The executor re-validates SQL the validator already capped; that must not report a reduction."""
    first = validator.validate("SELECT employee_name FROM employees")
    again = validator.validate(first.safe_sql)
    assert again.is_safe and again.applied_limit == 101 and not again.limit_added
    assert again.warnings == [] and again.safe_sql == first.safe_sql
    reduced = validator.validate("SELECT employee_name FROM employees LIMIT 102")
    assert reduced.applied_limit == 101 and any("reduced" in w for w in reduced.warnings)


def test_limit_on_union_and_offset(validator):
    union = validator.validate("SELECT city FROM customers UNION SELECT location FROM departments")
    assert union.safe_sql.endswith("LIMIT 101")
    offset = validator.validate("SELECT employee_name FROM employees LIMIT 10 OFFSET 20")
    assert "OFFSET 20" in offset.safe_sql and offset.applied_limit == 10


def test_fetch_first_cap_for_postgres():
    report = SQLValidator("postgresql", max_rows=100).validate(
        "SELECT employee_name FROM employees FETCH FIRST 500 ROWS ONLY")
    assert report.is_safe and report.applied_limit == 101


def test_comments_are_removed_from_executed_sql(validator):
    report = validator.validate("SELECT employee_name /* sneaky */ FROM employees -- ; DROP TABLE x")
    assert report.is_safe
    assert "sneaky" not in report.safe_sql and "DROP" not in report.safe_sql


def test_referenced_tables_exclude_ctes(validator):
    report = validator.validate("WITH t AS (SELECT * FROM employees) SELECT * FROM t JOIN departments d ON 1 = 1")
    assert sorted(report.referenced_tables) == ["departments", "employees"]
    assert report.cte_names == ["t"]


def test_dialect_translation_is_reported(validator):
    report = validator.validate("SELECT DATEDIFF(joining_date, '2020-01-01') FROM employees")
    assert report.is_safe and "JULIANDAY" in report.safe_sql.upper()
    assert any("DATEDIFF()" in w for w in report.warnings)


def test_pretty_sql_is_available(validator):
    report = validator.validate("SELECT a.x, b.y FROM a JOIN b ON a.id = b.id WHERE a.x > 1")
    assert "\n" in report.pretty_sql


def test_too_long_sql_is_blocked():
    report = SQLValidator("sqlite", max_sql_chars=50).validate("SELECT " + ", ".join(["salary"] * 20) + " FROM employees")
    assert report.is_blocked
