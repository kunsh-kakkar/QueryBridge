"""Tests for backend/schema_retriever.py."""

from __future__ import annotations

import pytest

from backend.schema_retriever import (
    SchemaRetriever,
    expand_question_tokens,
    singularize,
    tokenize_identifier,
    tokenize_text,
)


@pytest.fixture(scope="module")
def retriever(demo_schema):
    """Force retrieval mode on the 8-table demo DB (full mode is the default)."""
    return SchemaRetriever(demo_schema, full_context_max_tables=4, max_tables=4)


# ------------------------------------------------------------------ text helpers
@pytest.mark.parametrize(
    "word, expected",
    [("salaries", "salary"), ("employees", "employee"), ("sales", "sale"), ("status", "status"),
     ("addresses", "address"), ("class", "class"), ("analysis", "analysis"), ("box", "box")],
)
def test_singularize(word, expected):
    assert singularize(word) == expected


def test_tokenizers():
    assert tokenize_identifier("employee_projects") == ["employee", "project"]
    assert tokenize_identifier("saleAmount") == ["sale", "amount"]
    assert tokenize_text("Show me the top 5 employees with the highest salaries") == ["employee", "salary"]


def test_synonym_expansion_weights():
    tokens = expand_question_tokens("total revenue from clients")
    assert tokens["revenue"] == 1.0
    assert tokens["sale"] == pytest.approx(0.8) and tokens["customer"] == pytest.approx(0.8)


# --------------------------------------------------------------------- selection
def test_full_mode_for_small_schema(demo_schema):
    ctx = SchemaRetriever(demo_schema).retrieve("top 5 employees by salary")  # default threshold 12
    assert ctx.selection_mode == "full"
    assert ctx.table_names == demo_schema.table_names  # stable order -> cacheable prompt prefix
    assert ctx.ranked_tables[0] == "employees"
    assert len(ctx.relationships) == len(demo_schema.relationships)


def test_forced_full_mode(retriever):
    assert retriever.retrieve("anything", mode="full").selection_mode == "full"


@pytest.mark.parametrize(
    "question, must_include, must_exclude",
    [
        ("Show me the top 5 employees with the highest salaries.", {"employees"}, {"sales", "products"}),
        ("Total sales amount by product category", {"sales", "products"}, {"performance_reviews"}),
        ("Which projects are On Hold?", {"projects"}, {"sales", "customers"}),
        ("Which employees work on more than two projects?",
         {"employees", "projects", "employee_projects"}, {"sales"}),
        ("What is the average rating per department?", {"performance_reviews", "departments"}, {"products"}),
        ("List all clients in the Enterprise segment", {"customers"}, {"projects"}),
    ],
)
def test_retrieval_selects_relevant_tables(retriever, question, must_include, must_exclude):
    ctx = retriever.retrieve(question)
    assert ctx.selection_mode == "retrieved"
    names = set(ctx.table_names)
    assert must_include <= names, (question, names)
    assert not (must_exclude & names), (question, names)
    assert set(ctx.omitted_tables) == set(retriever.schema.table_names) - names


def test_bridge_table_added_for_indirect_join(retriever):
    ctx = retriever.retrieve("Which customers from London bought Electronics products?")
    assert {"customers", "products", "sales"} <= set(ctx.table_names)
    rating = retriever.retrieve("average review rating per department")
    assert "employees" in rating.table_names  # performance_reviews -> employees -> departments
    assert any("bridge" in r for r in rating.reasons["employees"])


def test_stored_values_ground_the_question(retriever):
    ctx = retriever.retrieve("Which projects are On Hold?")
    assert any("status='on hold'" in r for r in ctx.reasons["projects"])
    electronics = retriever.retrieve("show Electronics items")
    assert any("category='electronics'" in r for r in electronics.reasons["products"])


def test_ambiguous_performance_question_keeps_all_candidate_metrics(retriever):
    names = set(retriever.retrieve("Show me the highest-performing employees").table_names)
    assert {"employees", "sales", "performance_reviews"} <= names
    assert "projects" in names or "employee_projects" in names


def test_follow_up_keeps_previous_tables(retriever):
    ctx = retriever.retrieve("Now sort them by department", previous_tables=["employees"])
    assert {"employees", "departments"} <= set(ctx.table_names)
    assert any("previous" in r for r in ctx.reasons["employees"])
    # even when the follow-up text mentions nothing about the earlier table
    ctx = retriever.retrieve("only the ones in 2025", previous_tables=iter(["sales"]))
    assert "sales" in ctx.table_names


def test_unknown_previous_tables_are_ignored(retriever):
    ctx = retriever.retrieve("top employees by salary", previous_tables=["ghost_table"])
    assert "ghost_table" not in ctx.table_names


def test_fallback_when_nothing_matches(retriever):
    ctx = retriever.retrieve("hello there")
    assert ctx.selection_mode == "fallback"
    assert 0 < len(ctx.tables) <= retriever.max_tables + retriever.bridge_allowance
    assert ctx.ranked_tables == []


def test_relationships_are_limited_to_selected_tables(retriever):
    ctx = retriever.retrieve("Total sales amount by product category")
    selected = {n.lower() for n in ctx.table_names}
    assert ctx.relationships
    assert all(r.from_table in selected and r.to_table in selected for r in ctx.relationships)


def test_no_self_scoring_from_name_or_key_columns(retriever):
    scores = retriever.score_tables("list all employees")
    # FK columns like sales.employee_id must not count as evidence for sales
    assert scores["sales"][0] < retriever.min_score
    assert scores["employee_projects"][0] < retriever.min_score
    assert scores["employees"][0] >= retriever.min_score


def test_scores_and_reasons_are_explained(retriever):
    ctx = retriever.retrieve("Show me the top 5 employees with the highest salaries.")
    assert ctx.scores["employees"] > ctx.scores["products"]
    assert any("salary" in r for r in ctx.reasons["employees"])
    assert ctx.table_names == sorted(ctx.table_names, key=retriever.schema.table_names.index)


def test_cap_on_selected_tables(demo_schema):
    tight = SchemaRetriever(demo_schema, full_context_max_tables=1, max_tables=2, bridge_allowance=1)
    ctx = tight.retrieve("customers products employees departments projects reviews sales")
    assert len(ctx.tables) <= 3
