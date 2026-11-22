"""
Red-team suite: attack SQL that the security validator must block.

Shared by the unit tests (``tests/test_sql_validator.py``) and the evaluation
report (``evaluation/baseline_comparison.py``), so the published number always
matches what the tests enforce. Grouped by dialect.
"""

from __future__ import annotations

SQLITE_ATTACKS = [
    "DROP TABLE employees",
    "DELETE FROM sales",
    "UPDATE employees SET salary = 0",
    "INSERT INTO departments VALUES (9, 'x', 'y', 1)",
    "REPLACE INTO departments VALUES (1, 'x', 'y', 1)",
    "ALTER TABLE employees ADD COLUMN hacked TEXT",
    "CREATE TABLE evil (a)",
    "CREATE TEMP TABLE evil AS SELECT * FROM employees",
    "SELECT 1; DROP TABLE employees",
    "SELECT * FROM employees; DELETE FROM sales;",
    "SELECT 1;\n-- innocent comment\nDROP TABLE employees",
    "WITH d AS (DELETE FROM sales RETURNING *) SELECT * FROM d",
    "ATTACH DATABASE '/tmp/evil.db' AS evil",
    "DETACH DATABASE evil",
    "PRAGMA writable_schema = 1",
    "PRAGMA table_info(employees)",
    "SELECT * FROM pragma_table_info('employees')",
    "SELECT * FROM sqlite_master",
    "SELECT sql FROM sqlite_schema",
    "SELECT name FROM main.sqlite_master",
    "SELECT load_extension('evil.so')",
    "SELECT readfile('/etc/passwd')",
    "SELECT writefile('/tmp/x', 'y')",
    "SELECT randomblob(1000000000)",
    "SELECT zeroblob(1000000000)",
    "VACUUM",
    "VACUUM INTO '/tmp/copy.db'",
    "REINDEX",
    "ANALYZE",
    "EXPLAIN SELECT 1",
    "BEGIN TRANSACTION",
    "COMMIT",
    "SELECT * INTO backup FROM employees",
    "SELECT * FROM employees FOR UPDATE",
    "/* harmless */ DELETE FROM sales",
    "   delete from sales",
    "SELECT * FROM temp.secret",
    "SELECT 1 UNION ALL SELECT name FROM sqlite_master",
    "SELECT * FROM employees WHERE employee_id IN (SELECT 1); UPDATE employees SET salary = 1",
    "SELECT 1\x00",
    "SELECT e.salary FROM other_db.main.employees AS e",
    "VALUES (1, 2)",
    "SELECT 1 UNION DELETE FROM sales",            # unparseable but clearly a write: blocked, not "fixable"
]
POSTGRES_ATTACKS = [
    "SELECT pg_sleep(10)",
    "SET statement_timeout = 0",
    "COPY employees TO '/tmp/out.csv'",
    "GRANT ALL ON employees TO public",
    "SELECT * FROM pg_catalog.pg_user",
    "SELECT * FROM information_schema.tables",
    "SELECT pg_read_file('/etc/passwd')",
    "WITH x AS (UPDATE employees SET salary = 0 RETURNING *) SELECT * FROM x",
    "SELECT * FROM pg_shadow",
    "TRUNCATE employees",
    "SELECT set_config('default_transaction_read_only', 'off', false)",
    "SELECT pg_terminate_backend(1)",
]
MYSQL_ATTACKS = [
    "SELECT SLEEP(10)",
    "SELECT BENCHMARK(100000000, MD5('x'))",
    "LOAD DATA INFILE '/etc/passwd' INTO TABLE t",
    "SELECT LOAD_FILE('/etc/passwd')",
    "SELECT * FROM mysql.user",
    "SELECT * INTO OUTFILE '/tmp/x' FROM employees",
    "HANDLER employees OPEN",
    "CALL dangerous_procedure()",
]
ATTACKS = ([("sqlite", a) for a in SQLITE_ATTACKS] + [("postgresql", a) for a in POSTGRES_ATTACKS]
           + [("mysql", a) for a in MYSQL_ATTACKS])


def run_red_team() -> dict[str, int]:
    """Validate every attack; return {"attacks": n, "blocked": k}."""
    from backend.sql_validator import SQLValidator

    blocked = sum(1 for dialect, sql in ATTACKS if SQLValidator(dialect).validate(sql).is_blocked)
    return {"attacks": len(ATTACKS), "blocked": blocked}
