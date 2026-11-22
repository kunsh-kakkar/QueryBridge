"""
Input processing: the first gate in the pipeline.

``InputProcessor.process`` cleans the user's question and flags risks before
anything reaches the model:

1. **Normalisation**: Unicode NFKC folds compatibility look-alikes
   (full-width "ＤＥＬＥＴＥ" -> "DELETE"); control and invisible characters
   (zero-width, bidi overrides) are removed; whitespace is collapsed.
2. **Validation**: empty questions and questions over the length limit are
   rejected with a clear message (``InputValidationError``).
3. **Explicit write intent**: an imperative write command ("Delete all
   sales", "update employees set ...") or an embedded write statement
   ("...; DROP TABLE sales") is classified as ``explicit``, and the pipeline
   refuses it without calling the LLM. Only unambiguous verbs count:
   "remove the cancelled ones" in a follow-up is a filter, so softer wording
   is left to the model, whose rules cover it, and to the SQL validator.
4. **Prompt-injection signals** ("ignore previous instructions", fake tags)
   are recorded for the trace and UI. They do not block the question: the
   prompt treats the question as data, and the SQL validator is the hard
   barrier.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Literal

from pydantic import BaseModel, Field

from backend.exceptions import InputValidationError
from utils.helpers import collapse_whitespace, strip_control_chars

WriteIntent = Literal["none", "explicit"]

_F = re.IGNORECASE
# Imperative write command at the start of the request.
_IMPERATIVE_WRITE = re.compile(
    r"^\s*(?:please\s+|kindly\s+)?(?:(?:can|could|would|will)\s+you\s+(?:please\s+)?|i\s+(?:want|need)\s+to\s+|"
    r"go\s+ahead\s+and\s+|let'?s\s+)?(?:delete|drop|truncate|insert|update(?!\s+me\b)|alter|grant|revoke|wipe|erase|purge)\b",
    _F,
)
# A write/DDL statement embedded anywhere in the text.
_WRITE_STATEMENT = re.compile(
    r"\b(insert\s+into|delete\s+from|drop\s+(?:table|database|view|index|schema)|truncate\s+table\s+\w+|"
    r"alter\s+table|update\s+\w+\s+set|create\s+(?:table|view|index|database|trigger)|"
    r"grant\s+(?:all|select|insert|update|delete)|attach\s+database|pragma\s+\w+\s*=|replace\s+into|merge\s+into)\b",
    _F,
)
_INJECTION_SIGNALS: dict[str, re.Pattern[str]] = {
    "instruction_override": re.compile(
        r"\b(ignore|disregard|forget|override)\b.{0,40}\b(instructions?|rules?|prompts?|guidelines?|constraints?)\b", _F),
    "role_hijack": re.compile(r"\b(you are now|act as|pretend to be|new instructions|developer mode|jailbreak)\b", _F),
    "prompt_extraction": re.compile(r"\b(system prompt|your instructions|reveal .{0,20}prompt|hidden prompt)\b", _F),
    "fake_markup": re.compile(r"</?\s*(question|rules|system|database_schema|conversation_history|context|data)\s*>", _F),
    "sql_comment_or_stacking": re.compile(r";\s*\w+|--\s|/\*", _F),
}


class ProcessedInput(BaseModel):
    original: str
    text: str
    write_intent: WriteIntent = "none"
    write_reason: str | None = None
    injection_signals: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @property
    def should_refuse(self) -> bool:
        return self.write_intent == "explicit"


class InputProcessor:
    def __init__(self, max_chars: int = 1000) -> None:
        self.max_chars = max_chars

    def process(self, question: object) -> ProcessedInput:
        if not isinstance(question, str):
            raise InputValidationError("Please type your question as text.")
        normalised = unicodedata.normalize("NFKC", question)
        cleaned = collapse_whitespace(strip_control_chars(normalised))
        if not cleaned:
            raise InputValidationError("Please enter a question about your data.")
        if len(cleaned) > self.max_chars:
            raise InputValidationError(
                f"Your question is too long ({len(cleaned):,} characters; the limit is {self.max_chars:,}). "
                "Please shorten it."
            )
        if not re.search(r"[A-Za-z0-9À-￿]", cleaned):
            raise InputValidationError("Your question does not contain any words.")

        warnings: list[str] = []
        if cleaned != collapse_whitespace(question):
            warnings.append("Hidden or non-standard characters were removed from the question.")

        write_intent: WriteIntent = "none"
        write_reason = None
        statement = _WRITE_STATEMENT.search(cleaned)
        if statement:
            write_intent, write_reason = "explicit", f"contains a data-changing statement ('{statement.group(0)}')"
        elif _IMPERATIVE_WRITE.search(cleaned):
            write_intent, write_reason = "explicit", "asks to modify or delete data"

        signals = [name for name, pattern in _INJECTION_SIGNALS.items() if pattern.search(cleaned)]
        if signals:
            warnings.append("The question contains instruction-like text; it is treated strictly as data.")

        return ProcessedInput(
            original=question,
            text=cleaned,
            write_intent=write_intent,
            write_reason=write_reason,
            injection_signals=signals,
            warnings=warnings,
        )
