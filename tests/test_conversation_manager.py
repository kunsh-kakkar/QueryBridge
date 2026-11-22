"""Tests for backend/conversation_manager.py."""

from __future__ import annotations

from backend.conversation_manager import ConversationManager
from backend.models import ConversationTurn


def success(question: str, tables: list[str], sql: str = "SELECT 1") -> ConversationTurn:
    return ConversationTurn(question=question, intent="data_retrieval", sql=sql, tables_used=tables,
                            result_columns=["a"], row_count=3, status="success")


def test_history_window_and_storage_cap():
    manager = ConversationManager(max_turns=2, max_stored_turns=3)
    for i in range(5):
        manager.add_turn(success(f"q{i}", ["employees"]))
    assert [t.question for t in manager.turns] == ["q2", "q3", "q4"]
    assert [t.question for t in manager.history()] == ["q3", "q4"]
    assert ConversationManager(max_turns=0).history() == []


def test_follow_up_requires_a_previous_result():
    manager = ConversationManager()
    assert not manager.is_follow_up("Now sort them by department")
    manager.add_turn(success("Show employees earning more than 50,000", ["employees"]))
    assert manager.is_follow_up("Now sort them by department")
    assert not manager.is_follow_up("How many products are out of stock?")
    assert manager.previous_tables() == ["employees"]


def test_failed_turns_are_not_used_as_the_base_for_follow_ups():
    manager = ConversationManager()
    manager.add_turn(success("q1", ["sales"]))
    manager.add_turn(ConversationTurn(question="q2", sql="SELECT bad", status="error"))
    assert manager.last_successful_turn().question == "q1"
    assert manager.previous_tables() == ["sales"]


def test_clarification_round_trip():
    manager = ConversationManager()
    manager.ask_for_clarification("Show me the highest-performing employees",
                                  "How should performance be measured?", ["Total sales", "Average rating"])
    assert manager.pending_clarification is not None
    assert manager.turns[-1].status == "clarification"
    combined = manager.resolve_clarification("Total sales")
    assert combined == ("Show me the highest-performing employees "
                        "(clarification - How should performance be measured? -> Total sales)")
    assert manager.pending_clarification is None


def test_resolve_without_pending_returns_answer():
    assert ConversationManager().resolve_clarification("just a question") == "just a question"


def test_new_question_clears_pending_clarification():
    manager = ConversationManager()
    manager.ask_for_clarification("best customers?", "By what?", ["Revenue"])
    manager.add_turn(success("unrelated question", ["products"]))
    assert manager.pending_clarification is None


def test_serialisation_round_trip():
    manager = ConversationManager(max_turns=3)
    manager.add_turn(success("q1", ["employees"]))
    manager.ask_for_clarification("q2", "Which metric?", ["A", "B"])
    restored = ConversationManager.from_dict(manager.to_dict())
    assert restored.max_turns == 3
    assert [t.model_dump() for t in restored.turns] == [t.model_dump() for t in manager.turns]
    assert restored.pending_clarification == manager.pending_clarification


def test_clear():
    manager = ConversationManager()
    manager.add_turn(success("q1", ["employees"]))
    manager.ask_for_clarification("q2", "?", [])
    manager.clear()
    assert manager.turns == [] and manager.pending_clarification is None
