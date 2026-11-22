"""
Result formatting: turn raw rows into an analysis-ready, display-ready table.

* Converts driver types (``Decimal`` to float; ISO date strings to datetimes
  where a whole column parses), so sorting, charts and CSV behave correctly.
* Profiles each column (numeric / categorical / datetime / text / boolean /
  identifier) for the chart recommender and the UI.
* Produces the user-facing summary ("5 rows · 2 columns · 3.2 ms"), a
  helpful message for empty results, and CSV export bytes.

Result values are data from the database: they are displayed, never
interpreted, and never sent back to the LLM.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal

import pandas as pd
from pydantic import BaseModel

from backend.sql_executor import ExecutionResult
from utils.helpers import format_duration_ms, format_number

ColumnKind = Literal["numeric", "categorical", "datetime", "text", "boolean", "identifier", "empty"]

_ISO_DATE = re.compile(r"^\d{4}-\d{2}(-\d{2})?([ T]\d{2}:\d{2}(:\d{2}(\.\d+)?)?)?$")
_ID_NAME = re.compile(r"(^id$|_id$|^id_)", re.IGNORECASE)
_YEAR_NAME = re.compile(r"(^|_)(year|yr)($|_)", re.IGNORECASE)
CATEGORICAL_MAX_DISTINCT = 30


class ColumnProfile(BaseModel):
    name: str
    kind: ColumnKind
    distinct: int
    nulls: int
    minimum: str | None = None
    maximum: str | None = None


@dataclass
class FormattedResult:
    dataframe: pd.DataFrame
    profiles: list[ColumnProfile]
    row_count: int
    truncated: bool
    execution_time_ms: float
    warnings: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return self.row_count == 0

    @property
    def is_scalar(self) -> bool:
        return self.row_count == 1 and len(self.dataframe.columns) == 1

    @property
    def summary(self) -> str:
        rows = f"{format_number(self.row_count)} row{'s' if self.row_count != 1 else ''}"
        if self.truncated:
            rows += " (truncated)"
        columns = len(self.dataframe.columns)
        return f"{rows} · {columns} column{'s' if columns != 1 else ''} · {format_duration_ms(self.execution_time_ms)}"

    def columns_of(self, *kinds: ColumnKind) -> list[str]:
        return [p.name for p in self.profiles if p.kind in kinds]

    def profile(self, name: str) -> ColumnProfile | None:
        return next((p for p in self.profiles if p.name == name), None)


EMPTY_RESULT_MESSAGE = (
    "The query ran successfully but returned no rows. The filters may be too strict, or the values "
    "may be spelled differently in the database. Try broadening the question."
)


def _convert_column(series: pd.Series) -> pd.Series:
    non_null = series.dropna()
    if non_null.empty:
        return series
    if non_null.map(lambda v: isinstance(v, Decimal)).all():
        return series.map(lambda v: float(v) if isinstance(v, Decimal) else v).astype("float64")
    if non_null.map(lambda v: isinstance(v, str)).all() and non_null.map(lambda v: bool(_ISO_DATE.match(v))).all():
        parsed = pd.to_datetime(series, errors="coerce", format="mixed")
        if parsed.notna().sum() == len(non_null):
            return parsed
    return series


def _classify(name: str, series: pd.Series) -> ColumnKind:
    non_null = series.dropna()
    if non_null.empty:
        return "empty"
    if pd.api.types.is_bool_dtype(series):
        return "boolean"
    if pd.api.types.is_datetime64_any_dtype(series):
        return "datetime"
    if pd.api.types.is_numeric_dtype(series):
        if _ID_NAME.search(name):
            return "identifier"
        if _YEAR_NAME.search(name) and non_null.between(1900, 2100).all():
            return "categorical"
        return "numeric"
    distinct = non_null.nunique()
    if distinct <= CATEGORICAL_MAX_DISTINCT or distinct <= max(2, len(non_null) // 2):
        return "categorical"
    return "text"


def _bound(series: pd.Series, kind: ColumnKind) -> tuple[str | None, str | None]:
    non_null = series.dropna()
    if non_null.empty or kind not in ("numeric", "datetime"):
        return None, None
    low, high = non_null.min(), non_null.max()
    if kind == "datetime":
        return str(low.date() if hasattr(low, "date") else low), str(high.date() if hasattr(high, "date") else high)
    return format_number(float(low)), format_number(float(high))


def format_result(result: ExecutionResult) -> FormattedResult:
    frame = result.to_dataframe()
    for column in frame.columns:
        frame[column] = _convert_column(frame[column])
    profiles = []
    for column in frame.columns:
        kind = _classify(str(column), frame[column])
        low, high = _bound(frame[column], kind)
        profiles.append(ColumnProfile(name=str(column), kind=kind, distinct=int(frame[column].nunique()),
                                      nulls=int(frame[column].isna().sum()), minimum=low, maximum=high))
    return FormattedResult(dataframe=frame, profiles=profiles, row_count=result.row_count,
                           truncated=result.truncated, execution_time_ms=result.execution_time_ms,
                           warnings=list(result.warnings))


def to_csv_bytes(frame: pd.DataFrame) -> bytes:
    """UTF-8 CSV with BOM, so spreadsheet tools open accented text correctly."""
    return frame.to_csv(index=False).encode("utf-8-sig")
