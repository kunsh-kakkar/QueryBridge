"""Tests for backend/database.py: read-only enforcement, URLs, errors."""

from __future__ import annotations

import shutil
import sqlite3

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, OperationalError

from backend.database import (
    DatabaseManager,
    build_database_url,
    make_sqlite_authorizer,
    open_sqlite_readonly,
    sanitize_db_error,
)
from backend.exceptions import DatabaseConnectionError
from database.init_db import logical_fingerprint
from utils.helpers import file_sha256
from utils.logger import clear_registered_secrets, redact

WRITE_ATTEMPTS = [
    "INSERT INTO departments VALUES (99, 'X', 'Y', 1)",
    "UPDATE employees SET salary = 1",
    "DELETE FROM sales",
    "REPLACE INTO departments VALUES (1, 'X', 'Y', 1)",
    "DROP TABLE sales",
    "ALTER TABLE employees ADD COLUMN hacked TEXT",
    "CREATE TABLE evil (x)",
    "CREATE TEMP TABLE evil (x)",
    "CREATE INDEX evil_idx ON employees (salary)",
    "CREATE VIEW evil_view AS SELECT 1",
    "CREATE TRIGGER evil AFTER INSERT ON sales BEGIN DELETE FROM sales; END",
    "ATTACH DATABASE 'other.db' AS other",
    "PRAGMA writable_schema = 1",
    "PRAGMA query_only = 0",
    "PRAGMA table_info(employees)",
    "VACUUM",
    "REINDEX",
    "ANALYZE",
    "SELECT * FROM sqlite_master",
    "SELECT load_extension('evil')",
]


@pytest.mark.parametrize("sql", WRITE_ATTEMPTS)
def test_strict_connection_blocks_writes_and_escapes(demo_db, sql):
    with demo_db.connect() as conn:
        with pytest.raises((OperationalError, DBAPIError)):
            conn.execute(text(sql))


def test_database_unchanged_after_attack_attempts(demo_db, demo_db_path):
    before_file, before_logical = file_sha256(demo_db_path), logical_fingerprint(demo_db_path)
    with demo_db.connect() as conn:
        for sql in WRITE_ATTEMPTS:
            try:
                conn.execute(text(sql))
            except DBAPIError:
                pass
    assert file_sha256(demo_db_path) == before_file
    assert logical_fingerprint(demo_db_path) == before_logical


def test_read_queries_work(demo_db):
    with demo_db.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM employees")).scalar_one() == 60
        recursive = conn.execute(text(
            "WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM n WHERE x < 4) SELECT SUM(x) FROM n"
        )).scalar_one()
        assert recursive == 10
        ranked = conn.execute(text(
            "SELECT employee_name, RANK() OVER (ORDER BY salary DESC) AS r FROM employees ORDER BY r LIMIT 1"
        )).one()
        assert ranked.r == 1
        joined = conn.execute(text(
            "SELECT d.department_name, COUNT(*) FROM employees e JOIN departments d "
            "ON d.department_id = e.department_id GROUP BY d.department_name"
        )).all()
        assert len(joined) == 8


def test_inspection_connection_allows_introspection_but_not_writes(demo_db):
    with demo_db.inspection_connection() as conn:
        assert conn.execute(text("PRAGMA table_info(employees)")).all()
        assert conn.execute(text("SELECT COUNT(*) FROM sqlite_master")).scalar_one() > 0
        with pytest.raises(DBAPIError):
            conn.execute(text("DELETE FROM sales"))
        with pytest.raises(DBAPIError):
            conn.execute(text("PRAGMA writable_schema = 1"))


def test_each_read_only_layer_blocks_writes_on_its_own(demo_db_path, tmp_path):
    """Defence in depth: any single layer is sufficient to refuse a write."""
    # Layer 1 alone: file opened with mode=ro.
    only_ro = sqlite3.connect(f"{demo_db_path.as_uri()}?mode=ro", uri=True)
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        only_ro.execute("DELETE FROM sales")
    only_ro.close()

    # Layers 2 and 3 are tested on a writable COPY so the shared DB is never at risk.
    copy = tmp_path / "copy.db"
    shutil.copyfile(demo_db_path, copy)

    only_query_only = sqlite3.connect(copy)  # read-write handle
    only_query_only.execute("PRAGMA query_only = ON")
    with pytest.raises(sqlite3.OperationalError):
        only_query_only.execute("DELETE FROM sales")
    only_query_only.close()

    only_authorizer = sqlite3.connect(copy)  # read-write handle, no query_only
    only_authorizer.set_authorizer(make_sqlite_authorizer(strict=True))
    with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
        only_authorizer.execute("DELETE FROM sales")
    only_authorizer.close()

    assert logical_fingerprint(copy) == logical_fingerprint(demo_db_path)


def test_open_sqlite_readonly_combines_all_layers(demo_db_path):
    conn = open_sqlite_readonly(demo_db_path, strict=True)
    try:
        assert conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 40
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute("UPDATE products SET price = 0")
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute("PRAGMA query_only = OFF")  # cannot switch the protection off
    finally:
        conn.close()


def test_authorizer_policy_unit():
    strict = make_sqlite_authorizer(True)
    lenient = make_sqlite_authorizer(False)
    assert strict(sqlite3.SQLITE_SELECT, None, None, None, None) == sqlite3.SQLITE_OK
    assert strict(sqlite3.SQLITE_READ, "employees", "salary", "main", None) == sqlite3.SQLITE_OK
    assert strict(sqlite3.SQLITE_READ, "sqlite_master", "sql", "main", None) == sqlite3.SQLITE_DENY
    assert strict(sqlite3.SQLITE_READ, "t", "c", "temp", None) == sqlite3.SQLITE_DENY
    assert lenient(sqlite3.SQLITE_READ, "sqlite_master", "sql", "main", None) == sqlite3.SQLITE_OK
    assert strict(sqlite3.SQLITE_FUNCTION, None, "upper", None, None) == sqlite3.SQLITE_OK
    assert strict(sqlite3.SQLITE_FUNCTION, None, "load_extension", None, None) == sqlite3.SQLITE_DENY
    assert strict(sqlite3.SQLITE_PRAGMA, "table_info", "employees", None, None) == sqlite3.SQLITE_DENY
    assert lenient(sqlite3.SQLITE_PRAGMA, "table_info", "employees", None, None) == sqlite3.SQLITE_OK
    assert strict(sqlite3.SQLITE_PRAGMA, "read_uncommitted", None, None, None) == sqlite3.SQLITE_OK
    assert strict(sqlite3.SQLITE_PRAGMA, "read_uncommitted", "1", None, None) == sqlite3.SQLITE_DENY
    for action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE,
                   sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_DROP_TABLE):
        assert strict(action, "x", None, "main", None) == sqlite3.SQLITE_DENY
        assert lenient(action, "x", None, "main", None) == sqlite3.SQLITE_DENY


def test_connection_status(demo_db):
    status = demo_db.test_connection()
    assert status.ok and status.dialect == "sqlite" and status.read_only_enforced
    assert status.latency_ms is not None and status.server_version


@pytest.mark.parametrize("bad", ["missing.db", ":memory:"])
def test_invalid_sqlite_targets(tmp_path, bad):
    target = tmp_path / bad if bad != ":memory:" else bad
    with pytest.raises(DatabaseConnectionError):
        DatabaseManager(f"sqlite:///{target}")


def test_non_sqlite_file_rejected(tmp_path):
    fake = tmp_path / "fake.db"
    fake.write_bytes(b"definitely not sqlite")
    with pytest.raises(DatabaseConnectionError, match="not a SQLite"):
        DatabaseManager.for_sqlite_file(fake)


def test_unsupported_backend_rejected():
    with pytest.raises(DatabaseConnectionError):
        DatabaseManager("oracle://u:p@host/db")


def test_server_url_password_is_masked_and_redacted():
    clear_registered_secrets()
    manager = DatabaseManager("postgresql://reader:S3cr3t!Pass@db.example.com/sales", query_timeout_seconds=7)
    assert "S3cr3t!Pass" not in manager.masked_url and "S3cr3t!Pass" not in repr(manager)
    assert "S3cr3t!Pass" not in redact("auth failed with S3cr3t!Pass")
    assert manager.dialect == "postgresql"
    assert manager.masked_url.startswith("postgresql+psycopg://")  # installed driver chosen
    args = manager.server_connect_args()
    assert "default_transaction_read_only=on" in args["options"]
    assert "statement_timeout=7000" in args["options"]
    clear_registered_secrets()


def test_mysql_connect_args():
    manager = DatabaseManager("mysql://reader:pw@localhost/shop", query_timeout_seconds=3)
    args = manager.server_connect_args()
    assert "transaction_read_only = ON" in args["init_command"]
    assert "max_execution_time = 3000" in args["init_command"]
    assert manager.masked_url.startswith("mysql+pymysql://")


def test_unreachable_server_reports_sanitised_error():
    manager = DatabaseManager("postgresql://reader:TopSecret99@127.0.0.1:1/none", query_timeout_seconds=1)
    status = manager.test_connection()
    assert status.ok is False
    assert "TopSecret99" not in status.message


def test_build_database_url():
    url = build_database_url("postgresql", database="sales", host="db", username="u", password="p@ss:w/rd")
    assert url.password == "p@ss:w/rd"  # special characters preserved, escaped in rendering
    assert url.port == 5432 and url.drivername == "postgresql+psycopg"
    assert "p@ss" not in url.render_as_string(hide_password=True)
    assert build_database_url("mysql", database="shop", host="h").port == 3306
    assert build_database_url("sqlite", database="/tmp/x.db").database == "/tmp/x.db"
    with pytest.raises(ValueError):
        build_database_url("oracle", database="x", host="h")
    with pytest.raises(ValueError):
        build_database_url("postgresql", database="x")
    with pytest.raises(ValueError):
        build_database_url("sqlite", database=" ")


def test_sanitize_db_error_strips_sql_and_secrets(demo_db):
    with demo_db.connect() as conn:
        with pytest.raises(DBAPIError) as excinfo:
            conn.execute(text("SELECT no_such_column FROM employees WHERE 'password=hunter2' = 'x'"))
    message = sanitize_db_error(excinfo.value)
    assert "no such column" in message
    assert "[SQL" not in message and "Background" not in message
    assert "hunter2" not in message


def test_sanitize_plain_exception():
    assert sanitize_db_error(RuntimeError("token=abcdef1234567 oops")) == "token=***REDACTED*** oops"
    assert sanitize_db_error(RuntimeError("")) == "RuntimeError"
