"""Tests for backend/history_store.py."""

from __future__ import annotations

import pytest

from backend.history_store import HistoryEntry, HistoryStore


@pytest.fixture
def store(tmp_path) -> HistoryStore:
    return HistoryStore(tmp_path / "meta" / "history.db", max_entries=5)


def entry(question: str, status: str = "success", **kwargs) -> HistoryEntry:
    return HistoryEntry(database_label="demo.db", question=question, status=status,
                        sql=kwargs.pop("sql", "SELECT 1"), **kwargs)


def test_add_get_list(store):
    first = store.add(entry("first", execution_time_ms=2.0, estimated_cost=0.01))
    second = store.add(entry("second", status="failed", sql=None))
    assert store.get(first).question == "first" and store.get(first).created_at
    assert [e.question for e in store.list()] == ["second", "first"]  # newest first
    assert [e.id for e in store.list(status="failed")] == [second]
    assert store.get(9999) is None


def test_search(store):
    store.add(entry("salaries by department", sql="SELECT AVG(salary) FROM employees"))
    store.add(entry("products", sql="SELECT * FROM products"))
    assert [e.question for e in store.list(search="salary")] == ["salaries by department"]
    assert [e.question for e in store.list(search="products")] == ["products"]


def test_retention_cap(store):
    for i in range(8):
        store.add(entry(f"q{i}"))
    assert [e.question for e in store.list(limit=100)] == ["q7", "q6", "q5", "q4", "q3"]


def test_delete_and_clear(store):
    keep = store.add(entry("keep"))
    drop = store.add(entry("drop"))
    assert store.delete(drop) and not store.delete(drop)
    assert [e.id for e in store.list()] == [keep]
    assert store.clear() == 1 and store.list() == []


def test_stats(store):
    store.add(entry("a", execution_time_ms=2.0, estimated_cost=0.01))
    store.add(entry("b", status="failed", execution_time_ms=4.0, estimated_cost=0.02))
    stats = store.stats()
    assert stats["total"] == 2 and stats["by_status"] == {"success": 1, "failed": 1}
    assert stats["average_execution_ms"] == 3.0 and stats["estimated_cost_usd"] == 0.03


def test_persistence_across_instances(tmp_path):
    path = tmp_path / "h.db"
    HistoryStore(path).add(entry("persisted"))
    assert HistoryStore(path).list()[0].question == "persisted"
