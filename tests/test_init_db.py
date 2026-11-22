"""Tests for database/init_db.py and database/sample_data.py.

Covers deterministic builds, the build lifecycle (atomic, idempotent, forced
rebuild), and the business rules the sample data must satisfy.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from database import init_db
from database.init_db import (
    build_database,
    ensure_database,
    is_sqlite_file,
    logical_fingerprint,
    table_row_counts,
    verify_database,
)
from database.sample_data import DEFAULT_SEED, TABLE_COLUMNS, generate_sample_data

EXPECTED_TABLES = set(TABLE_COLUMNS)


@pytest.fixture(scope="module")
def ro(demo_db_path: Path):
    conn = sqlite3.connect(f"{demo_db_path.as_uri()}?mode=ro", uri=True)
    yield lambda sql: conn.execute(sql).fetchall()
    conn.close()


# --------------------------------------------------------------------------- build
def test_generation_is_deterministic():
    assert generate_sample_data(7).tables == generate_sample_data(7).tables
    assert generate_sample_data(7).tables != generate_sample_data(8).tables


def test_build_is_reproducible(tmp_path, demo_db_path):
    report = build_database(tmp_path / "again.db", seed=DEFAULT_SEED)
    assert report.fingerprint == logical_fingerprint(demo_db_path)
    assert set(report.row_counts) == EXPECTED_TABLES
    assert report.total_rows == sum(table_row_counts(report.path).values())


def test_different_seed_gives_different_data(tmp_path, demo_db_path):
    other = build_database(tmp_path / "other.db", seed=DEFAULT_SEED + 1)
    assert other.fingerprint != logical_fingerprint(demo_db_path)


def test_build_refuses_to_overwrite_and_cleans_up(tmp_path):
    path = tmp_path / "db.sqlite"
    first = build_database(path)
    with pytest.raises(FileExistsError):
        build_database(path)
    second = build_database(path, overwrite=True)
    assert first.fingerprint == second.fingerprint
    assert not (tmp_path / "db.sqlite.building").exists()


def test_ensure_database_builds_only_once(tmp_path):
    path = tmp_path / "ensure.db"
    ensure_database(path)
    mtime = path.stat().st_mtime_ns
    ensure_database(path)
    assert path.stat().st_mtime_ns == mtime


def test_ensure_database_rejects_non_sqlite(tmp_path):
    bogus = tmp_path / "bogus.db"
    bogus.write_text("not a database")
    assert not is_sqlite_file(bogus)
    with pytest.raises(init_db.DatabaseBuildError):
        ensure_database(bogus)


def test_verify_database(demo_db_path, tmp_path):
    ok, message = verify_database(demo_db_path)
    assert ok, message
    assert verify_database(tmp_path / "missing.db")[0] is False


def test_cli_build_verify_and_idempotent(tmp_path, capsys):
    path = tmp_path / "cli.db"
    assert init_db.main(["--path", str(path)]) == 0
    assert "Built" in capsys.readouterr().out
    assert init_db.main(["--path", str(path)]) == 0
    assert "already exists" in capsys.readouterr().out
    assert init_db.main(["--path", str(path), "--verify"]) == 0
    assert "fingerprint" in capsys.readouterr().out
    assert init_db.main(["--path", str(tmp_path / "nope.db"), "--verify"]) == 1


# ----------------------------------------------------------------- data rules
def test_row_counts(ro):
    counts = {t: ro(f"SELECT COUNT(*) FROM {t}")[0][0] for t in EXPECTED_TABLES}
    assert counts["departments"] == 8
    assert counts["employees"] == 60
    assert counts["products"] == 40
    assert counts["customers"] == 150
    assert counts["sales"] == 2500
    assert counts["projects"] == 20
    assert 80 <= counts["employee_projects"] <= 160
    assert 150 <= counts["performance_reviews"] <= 260


def test_no_foreign_key_violations(ro):
    assert ro("PRAGMA foreign_key_check") == []


def test_sales_respect_registration_and_joining_dates(ro):
    bad = ro("""
        SELECT COUNT(*) FROM sales s
        JOIN customers c ON c.customer_id = s.customer_id
        JOIN employees e ON e.employee_id = s.employee_id
        WHERE s.sale_date < c.registration_date OR s.sale_date < e.joining_date
    """)
    assert bad[0][0] == 0


def test_only_sales_department_sells(ro):
    departments = ro("""
        SELECT DISTINCT d.department_name FROM sales s
        JOIN employees e ON e.employee_id = s.employee_id
        JOIN departments d ON d.department_id = e.department_id
    """)
    assert departments == [("Sales",)]


def test_sales_dates_within_range(ro):
    low, high = ro("SELECT MIN(sale_date), MAX(sale_date) FROM sales")[0]
    assert "2023-01-01" <= low and high <= "2026-10-31"
    years = {row[0] for row in ro("SELECT DISTINCT substr(sale_date, 1, 4) FROM sales")}
    assert years == {"2023", "2024", "2025", "2026"}


def test_manager_hierarchy(ro):
    assert ro("SELECT COUNT(*) FROM employees WHERE manager_id IS NULL")[0][0] == 1
    ceo = ro("SELECT employee_id, job_title FROM employees WHERE manager_id IS NULL")[0]
    assert ceo[1] == "Chief Executive Officer"
    heads = ro(f"SELECT COUNT(*) FROM employees WHERE manager_id = {ceo[0]}")[0][0]
    assert heads == 8
    # every non-head reports to someone in their own department
    cross = ro("""
        SELECT COUNT(*) FROM employees e JOIN employees m ON m.employee_id = e.manager_id
        WHERE m.manager_id IS NOT NULL AND m.department_id <> e.department_id
    """)
    assert cross[0][0] == 0


def test_interesting_cases_exist_for_demo_questions(ro):
    assert ro("SELECT COUNT(*) FROM employees e JOIN employees m ON m.employee_id = e.manager_id "
              "WHERE e.salary > m.salary")[0][0] >= 1
    assert ro("SELECT COUNT(*) FROM employees WHERE joining_date >= '2026-01-01'")[0][0] >= 1
    assert ro("SELECT COUNT(*) FROM products WHERE stock_quantity = 0")[0][0] >= 1
    assert ro("SELECT COUNT(*) FROM projects WHERE end_date IS NULL")[0][0] >= 1
    assert ro("SELECT COUNT(*) FROM performance_reviews WHERE comments IS NULL")[0][0] >= 1


def test_each_project_has_exactly_one_lead(ro):
    leads = ro("SELECT project_id, SUM(role = 'Lead') FROM employee_projects GROUP BY project_id")
    assert len(leads) == 20
    assert all(count == 1 for _, count in leads)


def test_reviews_are_written_by_the_manager(ro):
    mismatched = ro("""
        SELECT COUNT(*) FROM performance_reviews r JOIN employees e ON e.employee_id = r.employee_id
        WHERE r.reviewer_id <> e.manager_id
    """)
    assert mismatched[0][0] == 0
    assert ro("SELECT MIN(rating), MAX(rating) FROM performance_reviews")[0] == (1, 5)


def test_sale_amount_is_consistent_with_price(ro):
    # amount = price * qty * (1 - discount) with discount in [0, 20%]
    bad = ro("""
        SELECT COUNT(*) FROM sales s JOIN products p ON p.product_id = s.product_id
        WHERE s.sale_amount > p.price * s.quantity + 0.01
           OR s.sale_amount < p.price * s.quantity * 0.8 - 0.01
    """)
    assert bad[0][0] == 0


def test_categorical_values(ro):
    assert {r[0] for r in ro("SELECT DISTINCT segment FROM customers")} == {"Enterprise", "SMB", "Consumer"}
    assert {r[0] for r in ro("SELECT DISTINCT status FROM projects")} == {"Planned", "Active", "Completed", "On Hold"}
    assert len(ro("SELECT DISTINCT category FROM products")) == 5
