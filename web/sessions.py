"""
Per-browser sessions.

The browser generates a random session id (kept in ``localStorage``) and
sends it with every request. A session holds the conversation memory used
for follow-up questions, the chosen technique configuration, and the most
recent results, so charts and details can be fetched later. Sessions live
in memory, are capped in number (least recently used are evicted), and
never contain credentials.
"""

from __future__ import annotations

import re
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field

from backend.conversation_manager import ConversationManager
from backend.pipeline import PipelineResult

SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9-]{16,64}$")


@dataclass
class Session:
    id: str
    conversation: ConversationManager
    config_name: str = "C"
    results: OrderedDict[str, PipelineResult] = field(default_factory=OrderedDict)
    lock: threading.Lock = field(default_factory=threading.Lock)
    last_seen: float = field(default_factory=time.monotonic)


class SessionStore:
    def __init__(self, *, max_sessions: int = 200, max_results: int = 30, conversation_turns: int = 5) -> None:
        self.max_sessions = max_sessions
        self.max_results = max_results
        self.conversation_turns = conversation_turns
        self._sessions: OrderedDict[str, Session] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, session_id: str | None) -> Session:
        """Return (creating if needed) the session for a valid id; raises ValueError otherwise."""
        if not session_id or not SESSION_ID_PATTERN.match(session_id):
            raise ValueError("invalid session id")
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                session = Session(id=session_id, conversation=ConversationManager(max_turns=self.conversation_turns))
                self._sessions[session_id] = session
                while len(self._sessions) > self.max_sessions:
                    self._sessions.popitem(last=False)
            self._sessions.move_to_end(session_id)
            session.last_seen = time.monotonic()
            return session

    def add_result(self, session: Session, result: PipelineResult) -> str:
        result_id = uuid.uuid4().hex[:16]
        with self._lock:
            session.results[result_id] = result
            while len(session.results) > self.max_results:
                session.results.popitem(last=False)
        return result_id

    def result(self, session: Session, result_id: str) -> PipelineResult | None:
        return session.results.get(result_id)

    def reset_all_conversations(self) -> None:
        """Called when the database changes: earlier SQL no longer applies."""
        with self._lock:
            for session in self._sessions.values():
                session.conversation.clear()
                session.results.clear()

    def __len__(self) -> int:
        return len(self._sessions)
