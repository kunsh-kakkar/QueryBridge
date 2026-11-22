"""Tests for backend/complexity_analyzer.py."""

from __future__ import annotations

import pytest

from backend.complexity_analyzer import analyze_complexity


@pytest.mark.parametrize(
    "sql, level",
    [
        ("SELECT employee_name FROM employees WHERE salary > 50000", "Simple"),
        ("SELECT employee_name FROM employees ORDER BY salary DESC LIMIT 5", "Simple"),
        ("SELECT d.department_name, AVG(e.salary) FROM employees e JOIN departments d "
         "ON d.department_id = e.department_id GROUP BY d.department_name HAVING COUNT(*) > 5", "Moderate"),
        ("SELECT employee_name FROM employees WHERE salary > (SELECT AVG(salary) FROM employees)", "Moderate"),
        ("WITH u AS (SELECT p.category, p.product_name, SUM(s.quantity) AS units FROM products p "
         "JOIN sales s ON s.product_id = p.product_id GROUP BY p.category, p.product_name), "
         "r AS (SELECT category, product_name, RANK() OVER (PARTITION BY category ORDER BY units DESC) AS rk FROM u) "
         "SELECT * FROM r WHERE rk = 1", "Complex"),
    ],
)
def test_levels(sql, level):
    assert analyze_complexity(sql).level == level


def test_features_are_counted():
    report = analyze_complexity(
        "SELECT d.department_name, COUNT(*) AS n, CASE WHEN AVG(e.salary) > 90000 THEN 'high' ELSE 'low' END AS band "
        "FROM employees e JOIN departments d ON d.department_id = e.department_id "
        "WHERE e.salary > 1 AND e.joining_date > '2020-01-01' GROUP BY d.department_name")
    f = report.features
    assert f["joins"] == 1 and f["aggregates"] == 2 and f["group_by"] == 1 and f["case_expressions"] == 1
    assert f["extra_predicates"] == 1 and report.tables == 2
    assert "1 join" in report.reasons and "1 CASE expression" in report.reasons


def test_subqueries_vs_ctes_and_unions():
    assert analyze_complexity("SELECT 1 UNION SELECT 2").features["subqueries"] == 0
    assert analyze_complexity("WITH t AS (SELECT 1 AS x) SELECT x FROM t").features == {
        **analyze_complexity("WITH t AS (SELECT 1 AS x) SELECT x FROM t").features, "subqueries": 0, "ctes": 1}
    nested = analyze_complexity("SELECT c.customer_name FROM customers c WHERE NOT EXISTS "
                                "(SELECT 1 FROM sales s WHERE s.customer_id = c.customer_id)")
    assert nested.features["subqueries"] == 1


def test_self_join_counted_only_within_one_select():
    assert analyze_complexity("SELECT e.employee_name FROM employees e JOIN employees m "
                              "ON m.employee_id = e.manager_id").features["self_joins"] == 1
    assert analyze_complexity("SELECT employee_name FROM employees WHERE salary > "
                              "(SELECT AVG(salary) FROM employees)").features["self_joins"] == 0


def test_unparseable_sql():
    report = analyze_complexity("SELECT (")
    assert report.level == "Simple" and report.reasons == ["could not be parsed"]
