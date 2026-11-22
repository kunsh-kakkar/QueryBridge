"""Integrity of evaluation/test_dataset.json (gold SQL, coverage, no leakage)."""

from __future__ import annotations

from collections import Counter
from difflib import SequenceMatcher

import pytest

from backend.sql_executor import SQLExecutor
from backend.sql_validator import SQLValidator
from evaluation.evaluator import load_dataset
from prompts.few_shot_examples import FEW_SHOT_EXAMPLES

ITEMS = load_dataset()
REQUIRED_CATEGORIES = {"simple", "filtering", "sorting", "aggregation", "joins", "nested", "date", "complex",
                       "multi_turn", "ambiguous", "unsafe", "impossible"}


@pytest.fixture(scope="module")
def executor(demo_db) -> SQLExecutor:
    return SQLExecutor(demo_db, SQLValidator("sqlite", max_rows=1000))


def test_size_and_coverage():
    assert len(ITEMS) >= 30
    counts = Counter(i.category for i in ITEMS)
    assert REQUIRED_CATEGORIES <= set(counts)
    assert all(counts[c] >= 2 for c in REQUIRED_CATEGORIES)


def test_ids_and_questions_are_unique():
    assert len({i.id for i in ITEMS}) == len(ITEMS)
    assert len({i.question.lower() for i in ITEMS}) == len(ITEMS)


def test_behaviour_matches_gold_presence():
    for item in ITEMS:
        if item.expected_behavior == "execute":
            assert item.gold_sql, item.id
        else:
            assert item.gold_sql is None, item.id


@pytest.mark.parametrize("item", [i for i in ITEMS if i.gold_sql], ids=lambda i: i.id)
def test_gold_sql_is_safe_executes_and_is_not_empty(executor, item):
    result = executor.execute(item.gold_sql)
    assert result.row_count > 0 and result.rows != [(None,)]
    assert not result.truncated
    assert result.row_count == item.gold_row_count
    for turn in item.turns:
        assert executor.execute(turn.gold_sql).row_count > 0
    if item.ordered_by:
        assert int(item.ordered_by["column"]) < len(result.columns)


def test_order_sensitive_items_have_unique_ordering(executor):
    for item in (i for i in ITEMS if i.order_matters):
        rows = executor.execute(item.gold_sql).rows
        assert len(set(rows)) == len(rows), item.id


def test_no_overlap_with_few_shot_examples():
    """The few-shot pool must not contain (near-)copies of evaluation questions.

    Pattern-level similarity (~0.6, e.g. "never placed an order" vs "never made a purchase")
    is intended: examples teach SQL patterns. Near-copies (>= 0.8) would leak answers; this
    test caught two such cases while the dataset was being built.
    """
    examples = [e.question.lower() for e in FEW_SHOT_EXAMPLES]
    for item in ITEMS:
        for example in examples:
            similarity = SequenceMatcher(None, item.question.lower(), example).ratio()
            assert similarity < 0.8, (item.id, item.question, example)
