"""Tests for prompts/few_shot_examples.py: correctness, coverage and no leakage."""

from __future__ import annotations

import sqlite3

import pytest
import sqlglot
from sqlglot import exp

from prompts.few_shot_examples import (
    BOOKSTORE_DDL,
    BOOKSTORE_TABLES,
    CORE_FILL_ORDER,
    EXAMPLES_BY_ID,
    FEW_SHOT_EXAMPLES,
)

SQL_EXAMPLES = [e for e in FEW_SHOT_EXAMPLES if e.sql is not None]
REQUIRED_PATTERNS = {  # assignment: SELECT/WHERE, ORDER BY/LIMIT, GROUP BY/HAVING, joins, aggregates, dates, nested
    "filter", "order_limit", "group_by", "having", "join", "aggregate", "date", "nested",
}


@pytest.fixture(scope="module")
def bookstore() -> sqlite3.Connection:
    """The example schema as a real database with a few rows."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(BOOKSTORE_DDL)
    conn.executescript("""
        INSERT INTO authors VALUES (1, 'Frank Herbert', 'USA', 1920), (2, 'Mary Beard', 'UK', 1955);
        INSERT INTO books VALUES
          (1, 'Dune', 1, 'Science Fiction', 9.99, '1965-08-01', 412),
          (2, 'SPQR', 2, 'History', 18.50, '2015-10-20', 608),
          (3, 'The War of Dragons', 1, 'Fantasy', 12.00, '2024-04-12', 350),
          (4, 'Starlight', 1, 'Fantasy', 29.00, '2023-02-01', 280);
        INSERT INTO members VALUES (1, 'Ana', 'Lisbon', '2023-01-10', 'Gold'), (2, 'Ben', 'Oslo', '2024-05-02', 'Basic');
        INSERT INTO orders VALUES (1, 1, '2025-06-20', 'Shipped'), (2, 1, '2024-03-02', 'Cancelled');
        INSERT INTO order_items VALUES (1, 1, 2, 9.99), (1, 3, 1, 12.00), (2, 2, 1, 18.50);
        INSERT INTO staff VALUES (1, 'Chief', NULL, 40.00, '2015-01-01'), (2, 'Dana', 1, 45.00, '2020-01-01');
    """)
    yield conn
    conn.close()


def test_ids_are_unique_and_lookup_works():
    ids = [e.id for e in FEW_SHOT_EXAMPLES]
    assert len(ids) == len(set(ids)) == len(EXAMPLES_BY_ID)
    assert all(i in EXAMPLES_BY_ID for i in CORE_FILL_ORDER)


@pytest.mark.parametrize("example", SQL_EXAMPLES, ids=lambda e: e.id)
def test_every_example_sql_executes(bookstore, example):
    bookstore.execute(example.sql).fetchall()
    if example.previous_sql:
        bookstore.execute(example.previous_sql).fetchall()


def test_examples_return_sensible_results(bookstore):
    assert bookstore.execute(EXAMPLES_BY_ID["E05"].sql).fetchall() == [("Ana", "Lisbon")]
    assert bookstore.execute(EXAMPLES_BY_ID["E10"].sql).fetchall() == [("Ben", "2024-05-02")]
    assert bookstore.execute(EXAMPLES_BY_ID["E12"].sql).fetchall()[0][0] == "Dana"
    assert bookstore.execute(EXAMPLES_BY_ID["E13"].sql).fetchone()[0] == 50.0
    assert bookstore.execute(EXAMPLES_BY_ID["E22"].sql).fetchall() == [("Chief", "2015-01-01")]


@pytest.mark.parametrize("example", SQL_EXAMPLES, ids=lambda e: e.id)
def test_examples_are_read_only_selects(example):
    tree = sqlglot.parse_one(example.sql, read="sqlite")
    assert isinstance(tree, (exp.Select, exp.Union))
    assert not list(tree.find_all(exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Create))


def test_no_leakage_into_demo_schema(demo_schema):
    """Examples only reference bookstore tables, never the demo database."""
    demo_tables = {t.lower() for t in demo_schema.table_names}
    assert not (BOOKSTORE_TABLES & demo_tables)
    for example in SQL_EXAMPLES:
        tree = sqlglot.parse_one(example.sql, read="sqlite")
        ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
        tables = {t.name.lower() for t in tree.find_all(exp.Table)} - ctes
        assert tables <= BOOKSTORE_TABLES, (example.id, tables)
        assert not (tables & demo_tables), example.id


def test_required_patterns_are_covered():
    covered = set().union(*(e.tags for e in SQL_EXAMPLES))
    assert REQUIRED_PATTERNS <= covered
    assert {"cte", "window", "self_join", "anti_join", "ratio", "case"} <= covered


def test_behaviour_examples():
    behaviour = {e.id: e for e in FEW_SHOT_EXAMPLES if e.is_behavior_example}
    assert {e.intent for e in behaviour.values()} == {"clarification_needed", "unsafe_request", "out_of_scope"}
    assert all(e.trigger_tags for e in behaviour.values())
    clarify = next(e for e in behaviour.values() if e.intent == "clarification_needed")
    assert clarify.clarification_question and 2 <= len(clarify.clarification_options) <= 4


def test_follow_up_example_shows_previous_turn():
    follow = next(e for e in FEW_SHOT_EXAMPLES if "follow_up" in e.tags)
    assert follow.intent == "follow_up_modification"
    assert follow.previous_question and follow.previous_sql
    assert follow.previous_sql.split("WHERE")[1].split("ORDER")[0].strip() in follow.sql
