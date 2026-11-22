"""Tests for backend/example_selector.py: pattern detection and context-aware selection."""

from __future__ import annotations

import pytest

from backend.example_selector import ExampleSelector, detect_question_tags, looks_like_follow_up


@pytest.fixture(scope="module")
def selector() -> ExampleSelector:
    return ExampleSelector()


@pytest.mark.parametrize(
    "question, expected",
    [
        ("Show me the top 5 employees with the highest salaries.", {"order_limit", "superlative"}),
        ("Which departments have more than 5 employees?", {"having"}),
        ("employees earning above the company average salary", {"comparison_to_aggregate", "nested"}),
        ("customers who have never bought anything", {"anti_join", "nested"}),
        ("best-selling product in each category", {"top_per_group", "window", "cte"}),
        ("employees who earn more than their manager", {"self_join", "comparison"}),
        ("monthly revenue in 2025", {"date", "date_grouping"}),
        ("sales in the last 90 days", {"date", "relative_date"}),
        ("percentage of Enterprise customers", {"ratio", "conditional_aggregation"}),
        ("count employees by salary range", {"bucketing", "case", "aggregate"}),
        ("Delete all sales from 2023", {"unsafe"}),
        ("give everyone a 10% raise", {"unsafe"}),
        ("highest-performing employees", {"ambiguous"}),
        ("products with 'Pro' in the name", {"text_search"}),
    ],
)
def test_tag_detection(question, expected):
    assert expected <= detect_question_tags(question)


@pytest.mark.parametrize(
    "question, absent",
    [
        ("sales in the last 90 days", {"order_limit"}),          # a date range, not top-N
        ("What is the best-selling product?", {"ambiguous"}),     # 'best-selling' is a clear metric
        ("show the percentage increase in sales", {"unsafe"}),    # 'increase' is not a write here
        ("list all products", {"unsafe", "ambiguous", "anti_join"}),
    ],
)
def test_tag_detection_avoids_false_positives(question, absent):
    assert not (absent & detect_question_tags(question))


def test_join_tags_come_from_table_hint():
    assert "join" not in detect_question_tags("list employees", likely_table_count=1)
    assert "join" in detect_question_tags("list employees", likely_table_count=2)
    assert "multi_join" in detect_question_tags("list employees", likely_table_count=3)


def test_follow_up_wording():
    assert looks_like_follow_up("Now sort them by department")
    assert looks_like_follow_up("only those in 2025")
    assert not looks_like_follow_up("Show me the top 5 employees")
    assert "follow_up" not in detect_question_tags("only show staff in Sales")  # needs conversation state
    assert "follow_up" in detect_question_tags("only show staff in Sales", is_follow_up=True)


@pytest.mark.parametrize(
    "question, best_id, kwargs",
    [
        ("Show me the top 5 employees with the highest salaries.", "E02", {}),
        ("Which departments have more than 5 employees?", "E04", {"likely_table_count": 2}),
        ("List employees earning above the company average salary", "E09", {}),
        ("Which customers have never bought anything?", "E10", {"likely_table_count": 2}),
        ("What is the best-selling product in each category?", "E11", {"likely_table_count": 2}),
        ("Which employees earn more than their manager?", "E12", {}),
        ("Show monthly revenue for 2025", "E08", {}),
        ("How many sales were made in the last 90 days?", "E07", {}),
        ("What percentage of customers are Enterprise?", "E13", {}),
        ("Compare total sales in 2024 and 2025 by category", "E14", {"likely_table_count": 2}),
        ("Count employees by salary range: under 50k, 50k-100k, over 100k", "E15", {}),
        ("Find products with 'Pro' in the name", "E20", {}),
        ("Delete all sales from 2023", "E18", {}),
        ("Show me the highest-performing employees", "E17", {"likely_table_count": 3}),
        ("What is each employee's phone number?", "E19", {}),
        ("Now sort them by department", "E16", {"is_follow_up": True}),
    ],
)
def test_most_relevant_example_is_last(selector, question, best_id, kwargs):
    picks = selector.select(question, 4, **kwargs)
    assert len(picks) == 4
    assert picks[-1].example.id == best_id, [p.example.id for p in picks]
    assert picks[-1].matched_tags and picks[-1].score > 0


def test_behaviour_examples_need_their_trigger(selector):
    ids = {p.example.id for p in selector.select("Total sales amount by product category", 4, likely_table_count=2)}
    assert not ids & {"E16", "E17", "E18", "E19"}


def test_at_most_one_behaviour_example(selector):
    picks = selector.select("Delete the best customers and their phone numbers", 6)
    assert sum(p.example.is_behavior_example for p in picks) == 1


def test_no_duplicates_and_k_respected(selector):
    for k in (1, 3, 6):
        picks = selector.select("Total revenue per category in 2025 above the average", k)
        ids = [p.example.id for p in picks]
        assert len(ids) == k == len(set(ids))


def test_k_zero_returns_nothing(selector):
    assert selector.select("anything", 0) == []


def test_unmatched_question_filled_with_core_patterns(selector):
    picks = selector.select("hello", 3)
    assert [p.example.id for p in picks] == ["E03", "E02", "E09"]
    assert all(p.reason.startswith("core pattern") for p in picks)


def test_static_strategy_is_fixed(selector):
    first = [p.example.id for p in selector.select("top 5 employees", 4, strategy="static")]
    second = [p.example.id for p in selector.select("customers who never ordered", 4, strategy="static")]
    assert first == second == ["E03", "E02", "E09", "E08"]


def test_selection_is_deterministic(selector):
    question = "Which employees earn more than their manager in each department?"
    runs = {tuple(p.example.id for p in selector.select(question, 4)) for _ in range(5)}
    assert len(runs) == 1


def test_diversity_avoids_near_duplicates(selector):
    picks = selector.select("monthly orders and order count by month and per year", 3)
    date_grouping = [p for p in picks if "date_grouping" in p.example.tags]
    assert len(date_grouping) <= 2
