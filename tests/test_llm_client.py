"""Tests for backend/llm_client.py: usage accounting and the client factory."""

from __future__ import annotations

from backend.gemini_client import GeminiClient
from backend.llm_client import ProviderLLM, UsageTracker, create_llm_client
from backend.models import LLMCallRecord


def record(stage: str, **counts) -> LLMCallRecord:
    return LLMCallRecord(stage=stage, model="gemini-3.5-flash-lite", **counts)


def test_tracker_totals_calls_tokens_and_cache_hits():
    tracker = UsageTracker()
    tracker.add(record("generation", input_tokens=100, output_tokens=50, cache_read_input_tokens=300,
                       latency_ms=200.0))
    tracker.add(record("verification", input_tokens=60, output_tokens=10, latency_ms=100.0))
    tracker.add_failure()
    summary = tracker.summary()
    assert summary["calls"] == 2 and summary["failed_calls"] == 1
    assert summary["calls_by_stage"] == {"generation": 1, "verification": 1}
    assert (summary["input_tokens"], summary["output_tokens"], summary["cache_read_input_tokens"]) == (160, 60, 300)
    assert summary["cache_hit_ratio"] == round(300 / 460, 3)
    assert summary["estimated_cost_usd"] is None  # free tier: no list price
    assert summary["average_latency_ms"] == 150.0


def test_tracker_summary_and_reset():
    tracker = UsageTracker()
    assert tracker.summary()["calls"] == 0 and tracker.summary()["average_latency_ms"] is None
    tracker.add_failure()
    tracker.reset()
    assert tracker.summary()["failed_calls"] == 0


def test_factory_builds_the_gemini_client(make_settings):
    tracker = UsageTracker()
    client = create_llm_client(make_settings(), tracker=tracker)
    assert isinstance(client, GeminiClient) and isinstance(client, ProviderLLM)
    assert client.tracker is tracker
