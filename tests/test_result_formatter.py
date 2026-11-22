"""Tests for backend/result_formatter.py."""

from __future__ import annotations

from decimal import Decimal

import pandas as pd

from backend.result_formatter import EMPTY_RESULT_MESSAGE, format_result, to_csv_bytes
from backend.sql_executor import ExecutionResult


def result(columns, rows, **kwargs) -> ExecutionResult:
    return ExecutionResult(sql="SELECT ...", columns=columns, rows=rows, truncated=kwargs.get("truncated", False),
                           execution_time_ms=3.21, max_rows=1000)


def test_types_are_converted_and_classified():
    formatted = format_result(result(
        ["employee_id", "employee_name", "department", "salary", "joining_date", "year", "active"],
        [(i, f"Person {i}", ["Sales", "Engineering"][i % 2], Decimal("50000.50") + i, f"2024-0{1 + i % 9}-15",
          2020 + i % 3, bool(i % 2)) for i in range(40)]))
    kinds = {p.name: p.kind for p in formatted.profiles}
    assert kinds == {"employee_id": "identifier", "employee_name": "text", "department": "categorical",
                     "salary": "numeric", "joining_date": "datetime", "year": "categorical", "active": "boolean"}
    frame = formatted.dataframe
    assert frame["salary"].dtype == "float64"
    assert pd.api.types.is_datetime64_any_dtype(frame["joining_date"])
    assert formatted.profile("salary").minimum == "50,000.50"


def test_year_month_strings_become_dates():
    formatted = format_result(result(["month", "revenue"], [("2025-01", 10.0), ("2025-02", 12.5)]))
    assert formatted.profile("month").kind == "datetime"


def test_mixed_text_is_not_forced_into_dates():
    formatted = format_result(result(["code"], [("2025-01-01",), ("not a date",)]))
    assert formatted.profile("code").kind == "categorical"


def test_summary_and_flags():
    formatted = format_result(result(["n"], [(5,)]))
    assert formatted.is_scalar and not formatted.is_empty
    assert formatted.summary == "1 row · 1 column · 3.2 ms"
    truncated = format_result(result(["x"], [(1,), (2,)], truncated=True))
    assert "(truncated)" in truncated.summary


def test_empty_result():
    formatted = format_result(result(["a", "b"], []))
    assert formatted.is_empty and formatted.row_count == 0
    assert all(p.kind == "empty" for p in formatted.profiles)
    assert "returned no rows" in EMPTY_RESULT_MESSAGE


def test_csv_export_round_trip():
    formatted = format_result(result(["city", "total"], [("Zürich", 1.5), ("Paris", 2.0)]))
    data = to_csv_bytes(formatted.dataframe)
    assert data.startswith(b"\xef\xbb\xbf")  # BOM for spreadsheet tools
    assert "Zürich" in data.decode("utf-8-sig") and data.decode("utf-8-sig").splitlines()[0] == "city,total"
