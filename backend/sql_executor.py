"""
Safe query execution.

``SQLExecutor.execute`` is the only code path that runs SQL against the
target database, and it validates the SQL itself before running it. Even a
caller that skipped the pipeline cannot execute anything the validator would
reject.

Execution limits:

* **Timeout.** SQLite: a progress handler aborts the statement once the
  deadline passes (the engine raises "interrupted"). PostgreSQL/MySQL:
  server-side ``statement_timeout`` / ``max_execution_time`` set when the
  connection is created.
* **Row cap.** The validator puts LIMIT ``max_rows + 1`` on the query; the
  executor fetches at most that many rows, so it can tell a complete result
  of exactly ``max_rows`` rows from a truncated one.
* **Raw driver execution** (``exec_driver_sql``). The SQL is passed to the
  driver unchanged, so text such as '10:30' is never mistaken for a
  SQLAlchemy bind parameter.

Errors become ``QueryTimeoutError`` / ``QueryExecutionError`` with sanitised
messages; these feed the correction loop and the UI.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any

import pandas as pd
from sqlalchemy.exc import DBAPIError, SQLAlchemyError

from backend.database import DatabaseManager, sanitize_db_error
from backend.exceptions import DatabaseConnectionError, QueryBridgeError
from backend.sql_corrector import ExecuteHook, ExecutionAttempt
from backend.sql_validator import SQLValidator, ValidationReport
from utils.helpers import Timer
from utils.logger import get_logger

logger = get_logger(__name__)

PROGRESS_HANDLER_STEPS = 1000  # SQLite VM instructions between deadline checks


class QueryExecutionError(QueryBridgeError):
    """The database could not run the query."""


class QueryTimeoutError(QueryExecutionError):
    """The query ran longer than the configured limit."""


@dataclass
class ExecutionResult:
    sql: str
    columns: list[str]
    rows: list[tuple[Any, ...]]
    truncated: bool
    execution_time_ms: float
    max_rows: int
    warnings: list[str] = field(default_factory=list)

    @property
    def row_count(self) -> int:
        return len(self.rows)

    def to_dataframe(self) -> pd.DataFrame:
        return pd.DataFrame.from_records(self.rows, columns=_unique_columns(self.columns))


def _unique_columns(columns: list[str]) -> list[str]:
    """Make column labels unique ('department_id', 'department_id_2') for DataFrames and the UI."""
    seen: dict[str, int] = {}
    unique: list[str] = []
    for name in columns:
        label = name or "column"
        count = seen.get(label, 0) + 1
        seen[label] = count
        unique.append(label if count == 1 else f"{label}_{count}")
    return unique


class SQLExecutor:
    def __init__(self, db: DatabaseManager, validator: SQLValidator, *, timeout_seconds: float = 10.0) -> None:
        self.db = db
        self.validator = validator
        self.timeout_seconds = timeout_seconds

    @property
    def max_rows(self) -> int:
        return self.validator.max_rows

    def execute(self, sql: str) -> ExecutionResult:
        """Validate, then run ``sql`` with timeout and row cap. Raises on any failure."""
        report: ValidationReport = self.validator.validate(sql)
        if not report.is_safe or not report.safe_sql:
            raise QueryExecutionError("This query is not allowed to run.", detail="; ".join(report.messages))
        return self._run(report.safe_sql, warnings=list(report.warnings))

    def as_hook(self) -> ExecuteHook:
        """Adapter for ``SQLCorrector``: database errors become feedback instead of exceptions."""

        def hook(sql: str) -> ExecutionAttempt:
            try:
                return ExecutionAttempt(ok=True, result=self.execute(sql))
            except QueryExecutionError as exc:
                return ExecutionAttempt(ok=False, error=exc.detail or exc.user_message)

        return hook

    # ------------------------------------------------------------------ internals
    def _run(self, sql: str, *, warnings: list[str]) -> ExecutionResult:
        timer = Timer().start()
        try:
            with self.db.connect() as conn:
                driver = conn.connection.driver_connection
                if isinstance(driver, sqlite3.Connection):
                    deadline = time.monotonic() + self.timeout_seconds
                    driver.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0,
                                                PROGRESS_HANDLER_STEPS)
                try:
                    result = conn.exec_driver_sql(sql)
                    columns = list(result.keys())
                    rows = [tuple(row) for row in result.fetchmany(self.max_rows + 1)]
                    result.close()
                finally:
                    if isinstance(driver, sqlite3.Connection):
                        driver.set_progress_handler(None, PROGRESS_HANDLER_STEPS)
        except DatabaseConnectionError as exc:
            raise QueryExecutionError(exc.user_message, detail=exc.detail) from None
        except (DBAPIError, SQLAlchemyError) as exc:
            message = sanitize_db_error(exc)
            if self._is_timeout(exc, message):
                raise QueryTimeoutError(
                    f"The query took longer than the {self.timeout_seconds:g}-second limit and was stopped.",
                    detail=f"Query exceeded the {self.timeout_seconds:g} s time limit; write a simpler or more "
                           f"selective query.",
                ) from None
            raise QueryExecutionError("The database could not run this query.", detail=message) from None

        elapsed = timer.stop()
        truncated = len(rows) > self.max_rows
        if truncated:
            rows = rows[: self.max_rows]
            warnings.append(f"Showing the first {self.max_rows:,} rows; the full result is larger.")
        logger.info("Executed query: %d rows%s in %.1f ms", len(rows), " (truncated)" if truncated else "", elapsed)
        return ExecutionResult(sql=sql, columns=columns, rows=rows, truncated=truncated,
                               execution_time_ms=round(elapsed, 2), max_rows=self.max_rows, warnings=warnings)

    @staticmethod
    def _is_timeout(exc: Exception, message: str) -> bool:
        lowered = message.lower()
        return ("interrupted" in lowered or "statement timeout" in lowered or "canceling statement" in lowered
                or "max_execution_time" in lowered or "maximum statement execution time" in lowered)
