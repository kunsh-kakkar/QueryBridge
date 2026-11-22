"""
Conversation memory for multi-turn querying.

The manager keeps a rolling window of ``ConversationTurn`` records. Each one
holds the question, intent, SQL, tables, result *shape* (columns and row
count) and status. Result rows are never stored or sent to the LLM; this
keeps prompts small and stops database content from being replayed as
instructions.

It also tracks a pending clarification. When the model asks "Do you mean
sales, ratings or projects?", the original question is remembered, and the
user's answer (a clicked option or typed text) is merged into a combined
question for the next generation.

The state serialises to plain dicts (``to_dict`` / ``from_dict``) so it can
live in Streamlit's session state.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from backend.example_selector import looks_like_follow_up
from backend.models import ConversationTurn


class PendingClarification(BaseModel):
    original_question: str
    clarification_question: str
    options: list[str] = Field(default_factory=list)


class ConversationManager:
    def __init__(self, max_turns: int = 5, max_stored_turns: int = 50) -> None:
        self.max_turns = max_turns
        self.max_stored_turns = max_stored_turns
        self.turns: list[ConversationTurn] = []
        self.pending_clarification: PendingClarification | None = None

    # ------------------------------------------------------------- reading
    def history(self) -> list[ConversationTurn]:
        """The turns given to the prompt (most recent ``max_turns``)."""
        return self.turns[-self.max_turns:] if self.max_turns > 0 else []

    def last_successful_turn(self) -> ConversationTurn | None:
        return next((t for t in reversed(self.turns) if t.status in ("success", "empty") and t.sql), None)

    def previous_tables(self) -> list[str]:
        """Tables of the last successful query: kept in context for follow-ups."""
        turn = self.last_successful_turn()
        return list(turn.tables_used) if turn else []

    def is_follow_up(self, question: str) -> bool:
        """True when there is a previous result and the wording refers back to it."""
        return self.last_successful_turn() is not None and looks_like_follow_up(question)

    # ------------------------------------------------------------- writing
    def add_turn(self, turn: ConversationTurn) -> None:
        self.turns.append(turn)
        if len(self.turns) > self.max_stored_turns:
            self.turns = self.turns[-self.max_stored_turns:]
        if turn.status != "clarification":
            self.pending_clarification = None

    def ask_for_clarification(self, original_question: str, clarification_question: str,
                              options: list[str]) -> None:
        self.pending_clarification = PendingClarification(
            original_question=original_question,
            clarification_question=clarification_question,
            options=list(options),
        )
        self.add_turn(ConversationTurn(question=original_question, intent="clarification_needed",
                                       status="clarification", clarification_question=clarification_question))

    def resolve_clarification(self, answer: str) -> str:
        """Merge the user's answer with the original question and clear the pending state."""
        pending = self.pending_clarification
        if pending is None:
            return answer
        self.pending_clarification = None
        return (f"{pending.original_question.strip()} "
                f"(clarification - {pending.clarification_question.strip()} -> {answer.strip()})")

    def clear(self) -> None:
        self.turns.clear()
        self.pending_clarification = None

    # ------------------------------------------------------------- persistence
    def to_dict(self) -> dict[str, Any]:
        return {
            "max_turns": self.max_turns,
            "turns": [t.model_dump() for t in self.turns],
            "pending_clarification": self.pending_clarification.model_dump() if self.pending_clarification else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ConversationManager":
        manager = cls(max_turns=int(data.get("max_turns", 5)))
        manager.turns = [ConversationTurn.model_validate(t) for t in data.get("turns", [])]
        pending = data.get("pending_clarification")
        manager.pending_clarification = PendingClarification.model_validate(pending) if pending else None
        return manager
