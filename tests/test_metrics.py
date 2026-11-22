"""Tests for evaluation/metrics.py."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from evaluation.metrics import exact_match, f1, normalize_sql, normalize_value, ratio, results_match

GOLD = [("Alice", 100.0), ("Bob", 90.0)]


def test_identical_results_match():
    assert results_match(GOLD, GOLD).match


def test_column_order_and_extra_columns_are_allowed():
    assert results_match(GOLD, [(100.0, "Alice", 1), (90.0, "Bob", 2)]).match


def test_row_order_ignored_unless_required():
    swapped = [("Bob", 90.0), ("Alice", 100.0)]
    assert results_match(GOLD, swapped).match
    assert not results_match(GOLD, swapped, order_matters=True).match


def test_missing_column_or_rows_fail():
    assert "too few columns" in results_match(GOLD, [("Alice",), ("Bob",)]).reason
    assert "row count" in results_match(GOLD, GOLD[:1]).reason
    assert not results_match(GOLD, [("Alice", 100.0), ("Carol", 90.0)]).match


def test_values_must_line_up_in_rows():
    # same column multisets, but the pairs are crossed
    assert not results_match(GOLD, [("Alice", 90.0), ("Bob", 100.0)]).match


def test_numeric_normalisation():
    assert results_match([(91958.3333333,)], [(91958.33,)]).match            # ROUND(AVG(x), 2)
    assert results_match([(0.125,)], [(0.13,)]).match                        # half-up rounding
    assert results_match([(60,)], [(60.0,)]).match                           # int vs float
    assert results_match([(Decimal("12.50"),)], [(12.5,)]).match
    assert not results_match([(91958.33,)], [(91958.0,)]).match              # rounded to 0 dp is wrong


def test_value_normalisation():
    assert normalize_value(True) == 1
    assert normalize_value(date(2026, 1, 2)) == "2026-01-02"
    assert normalize_value("  x ") == "x"
    assert normalize_value(None) is None


def test_ordered_by_checks_sort_with_ties():
    gold = [("a", "Eng"), ("b", "Eng"), ("c", "Ops")]
    sorted_pred = [("b", "Eng", 1), ("a", "Eng", 2), ("c", "Ops", 3)]       # ties in any order are fine
    assert results_match(gold, sorted_pred, ordered_by={"column": 1, "direction": "asc"}).match
    unsorted = [("c", "Ops"), ("a", "Eng"), ("b", "Eng")]
    result = results_match(gold, unsorted, ordered_by={"column": 1, "direction": "asc"})
    assert not result.match and "not sorted" in result.reason


def test_duplicate_columns_resolved_by_assignment():
    gold = [(1, 1), (2, 2)]
    assert results_match(gold, [(1, 1, 9), (2, 2, 9)]).match


def test_empty_results():
    assert results_match([], []).match
    assert not results_match([], [(1,)]).match


def test_exact_match_normalisation():
    assert exact_match("SELECT name FROM employees", "select   NAME  from Employees")
    assert not exact_match("SELECT name FROM employees", "SELECT e.name FROM employees AS e")
    assert not exact_match("SELECT 1", None)
    assert normalize_sql("SELECT (") is None


@pytest.mark.parametrize("numerator, denominator, expected", [(1, 4, 0.25), (0, 3, 0.0), (1, 0, None)])
def test_ratio(numerator, denominator, expected):
    assert ratio(numerator, denominator) == expected


def test_f1():
    assert f1(1.0, 1.0) == 1.0
    assert f1(0.5, 1.0) == pytest.approx(0.6667, abs=1e-4)
    assert f1(0.0, 0.0) == 0.0
    assert f1(None, 0.5) is None
