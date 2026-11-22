"""
LLM client interface: the shared ``StructuredLLM`` protocol, per-stage
settings, usage accounting, and the client factory.

The provider client is ``GeminiClient`` in ``backend/gemini_client.py``
(Google Gemini, which has a free tier). ``create_llm_client(settings)`` builds
it from the settings; the pipeline, the verifier, the web app and the
evaluation only depend on the ``StructuredLLM`` interface defined here, so
tests can swap in a fake.

* **Per-stage settings.** Generation, correction, verification and
  explanation each have their own model, effort and token budget
  (``StageSettings``).
* **Usage accounting.** ``UsageTracker`` totals tokens, cache hits and an
  estimated cost per stage.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar, Generic, Protocol, TypeVar

from pydantic import BaseModel

from backend.exceptions import LLMConfigurationError
from backend.models import BuiltPrompt, LLMCallRecord
from config import Settings

T = TypeVar("T", bound=BaseModel)


# =============================================================================
# Interfaces
# =============================================================================
@dataclass(frozen=True)
class StageSettings:
    model: str
    effort: str | None
    max_tokens: int


@dataclass
class LLMResponse(Generic[T]):
    output: T
    record: LLMCallRecord


class StructuredLLM(Protocol):
    """What the generator/verifier need; implemented by ``GeminiClient`` and test fakes."""

    def complete(self, prompt: BuiltPrompt, output_model: type[T]) -> LLMResponse[T]: ...


@dataclass
class UsageTracker:
    """Thread-safe running totals of all LLM calls in a session."""

    records: list[LLMCallRecord] = field(default_factory=list)
    failed_calls: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, record: LLMCallRecord) -> None:
        with self._lock:
            self.records.append(record)

    def add_failure(self) -> None:
        with self._lock:
            self.failed_calls += 1

    def reset(self) -> None:
        with self._lock:
            self.records.clear()
            self.failed_calls = 0

    def summary(self) -> dict[str, Any]:
        with self._lock:
            records = list(self.records)
            failed = self.failed_calls
        costs = [r.estimated_cost_usd for r in records if r.estimated_cost_usd is not None]
        total_input = sum(r.total_input_tokens for r in records)
        cache_read = sum(r.cache_read_input_tokens for r in records)
        by_stage: dict[str, int] = {}
        for r in records:
            by_stage[r.stage] = by_stage.get(r.stage, 0) + 1
        return {
            "calls": len(records),
            "failed_calls": failed,
            "calls_by_stage": by_stage,
            "input_tokens": sum(r.input_tokens for r in records),
            "output_tokens": sum(r.output_tokens for r in records),
            "cache_creation_input_tokens": sum(r.cache_creation_input_tokens for r in records),
            "cache_read_input_tokens": cache_read,
            "cache_hit_ratio": round(cache_read / total_input, 3) if total_input else 0.0,
            "estimated_cost_usd": round(sum(costs), 4) if costs else None,
            "average_latency_ms": round(sum(r.latency_ms for r in records) / len(records), 1) if records else None,
            "fallbacks_used": sum(r.fallback_used for r in records),
        }


REQUIRED_STAGES = frozenset({"generation", "correction", "verification", "explanation"})


class ProviderLLM:
    """Base of the real provider client (Gemini); test fakes implement ``StructuredLLM`` directly."""

    provider: ClassVar[str]
    stage_settings: dict[str, StageSettings]
    tracker: UsageTracker

    @staticmethod
    def _checked_stages(stage_settings: Mapping[str, StageSettings]) -> dict[str, StageSettings]:
        missing = REQUIRED_STAGES - set(stage_settings)
        if missing:
            raise LLMConfigurationError(f"Missing stage settings for: {', '.join(sorted(missing))}")
        return dict(stage_settings)

    def complete(self, prompt: BuiltPrompt, output_model: type[T]) -> LLMResponse[T]:
        raise NotImplementedError

    def preview_request(self, prompt: BuiltPrompt,
                        output_model: type[BaseModel]) -> tuple[dict[str, Any], dict[str, Any]]:
        """The request with the prompt text shortened to its structure, and the enforced output format."""
        raise NotImplementedError


def create_llm_client(settings: Settings, *, tracker: UsageTracker | None = None) -> ProviderLLM:
    """The Gemini client for these settings."""
    from backend.gemini_client import GeminiClient  # imported here: gemini_client imports this module

    return GeminiClient.from_settings(settings, tracker=tracker)
