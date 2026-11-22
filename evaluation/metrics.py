"""
Evaluation metrics.

**Result-set accuracy** (main metric, "execution accuracy" in the Spider/BIRD
sense): the predicted result answers the question when, after value
normalisation,

* the row counts are equal;
* every gold column is matched by a predicted column with the same multiset
  of values (column order and names are ignored; extra predicted columns,
  such as an added name or total, are allowed);
* the rows match as a multiset, or as a sequence when ``order_matters``;
* when ``ordered_by`` is given, the matched column is sorted as required
  (ties allowed).

Values are normalised: numbers are rounded half-up to 2 decimals (so that
ROUND(AVG(x), 2) equals AVG(x), and float summation noise is ignored),
datetimes become ISO strings, and text is stripped.

**Exact match** compares SQLGlot-normalised SQL. It is strict (aliases and
equivalent formulations count as different) and is reported for reference
only.

All other metrics (safety, ambiguity precision/recall/F1, schema
compliance, false refusals) are computed in ``summarize`` from per-item
outcomes.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from itertools import islice
from typing import Any

import sqlglot
from pydantic import BaseModel

from backend.sql_validator import SQLGLOT_DIALECTS

MAX_ASSIGNMENTS = 5000


class MatchResult(BaseModel):
    match: bool
    reason: str


def normalize_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float, Decimal)):
        try:
            return float(Decimal(repr(float(value))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        except (InvalidOperation, ValueError):
            return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if hasattr(value, "isoformat"):  # pandas Timestamp
        return value.isoformat()
    return str(value).strip()


def _normalize_rows(rows: Sequence[Sequence[Any]]) -> list[tuple]:
    return [tuple(normalize_value(v) for v in row) for row in rows]


def _sorted_ok(values: list[Any], direction: str) -> bool:
    present = [v for v in values if v is not None]
    pairs = zip(present, present[1:])
    try:
        return all(a <= b for a, b in pairs) if direction == "asc" else all(a >= b for a, b in pairs)
    except TypeError:
        return False


def _assignments(candidates: list[list[int]]):
    """Injective gold->pred column assignments (depth-first)."""
    def walk(index: int, used: set[int], current: list[int]):
        if index == len(candidates):
            yield list(current)
            return
        for option in candidates[index]:
            if option not in used:
                used.add(option)
                current.append(option)
                yield from walk(index + 1, used, current)
                current.pop()
                used.discard(option)
    yield from walk(0, set(), [])


def results_match(gold_rows: Sequence[Sequence[Any]], pred_rows: Sequence[Sequence[Any]], *,
                  order_matters: bool = False, ordered_by: dict | None = None) -> MatchResult:
    gold = _normalize_rows(gold_rows)
    pred = _normalize_rows(pred_rows)
    if len(gold) != len(pred):
        return MatchResult(match=False, reason=f"row count differs: expected {len(gold)}, got {len(pred)}")
    if not gold:
        return MatchResult(match=True, reason="both results are empty")
    gold_width, pred_width = len(gold[0]), len(pred[0])
    if pred_width < gold_width:
        return MatchResult(match=False, reason=f"too few columns: expected at least {gold_width}, got {pred_width}")

    gold_columns = [Counter(row[j] for row in gold) for j in range(gold_width)]
    pred_columns = [Counter(row[k] for row in pred) for k in range(pred_width)]
    candidates = [[k for k in range(pred_width) if pred_columns[k] == gold_columns[j]] for j in range(gold_width)]
    missing = [j for j, options in enumerate(candidates) if not options]
    if missing:
        return MatchResult(match=False, reason=f"no predicted column has the values of expected column(s) {missing}")

    gold_rows_counter = Counter(gold)
    for assignment in islice(_assignments(candidates), MAX_ASSIGNMENTS):
        projected = [tuple(row[k] for k in assignment) for row in pred]
        if order_matters:
            if projected != gold:
                continue
        elif Counter(projected) != gold_rows_counter:
            continue
        if ordered_by is not None:
            column = assignment[int(ordered_by["column"])]
            if not _sorted_ok([row[column] for row in pred], ordered_by.get("direction", "asc")):
                return MatchResult(match=False, reason="rows match but are not sorted as requested")
        return MatchResult(match=True, reason="result matches")
    return MatchResult(match=False, reason="row order differs" if order_matters else "row values differ")


def normalize_sql(sql: str | None, dialect: str = "sqlite") -> str | None:
    """Canonical SQL text for exact-match comparison (None when unparseable)."""
    if not sql:
        return None
    try:
        tree = sqlglot.parse_one(sql, read=SQLGLOT_DIALECTS.get(dialect, dialect))
    except sqlglot.errors.ParseError:
        return None
    return " ".join(tree.sql(dialect=SQLGLOT_DIALECTS.get(dialect, dialect), normalize=True, comments=False)
                    .lower().split())


def exact_match(gold_sql: str | None, pred_sql: str | None, dialect: str = "sqlite") -> bool:
    gold, pred = normalize_sql(gold_sql, dialect), normalize_sql(pred_sql, dialect)
    return gold is not None and gold == pred


def ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def f1(precision: float | None, recall: float | None) -> float | None:
    if not precision or not recall:
        return 0.0 if precision is not None and recall is not None else None
    return round(2 * precision * recall / (precision + recall), 4)
