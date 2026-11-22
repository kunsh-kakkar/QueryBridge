"""Live smoke tests against the real Gemini API.

Skipped unless GEMINI_API_KEY is configured (in the environment or .env).
Run explicitly with:

    pytest -m live -v

Each run makes a few small API calls: free on the Gemini free tier (it uses a
few of the day's free requests). These tests check the integration
(authentication, structured output, executable SQL). They do not measure
accuracy; that is the evaluation's job (Phase 9).
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import text

from backend.llm_client import create_llm_client
from backend.prompt_manager import PromptManager, preset_from_settings
from backend.schema_retriever import SchemaRetriever
from backend.sql_generator import SQLGenerator
from config import Settings

pytestmark = pytest.mark.live


def _settings() -> Settings | None:
    try:
        return Settings()
    except Exception:
        return None


def _has_credentials() -> bool:
    settings = _settings()
    return settings is not None and settings.has_llm_credentials


requires_credentials = pytest.mark.skipif(not _has_credentials(), reason="no GEMINI_API_KEY configured")


@pytest.fixture(scope="module")
def live_generator(demo_schema):
    settings = Settings()
    client = create_llm_client(settings)
    manager = PromptManager(demo_schema, reference_date=date(2026, 11, 15))
    return SQLGenerator(client, manager), client, preset_from_settings(settings, "C")


@requires_credentials
def test_live_generation_returns_executable_sql(live_generator, demo_db, demo_schema):
    generator, client, config = live_generator
    question = "Show me the top 5 employees with the highest salaries."
    context = SchemaRetriever(demo_schema).retrieve(question)
    outcome = generator.generate(question, config=config, schema_context=context)

    assert outcome.is_usable, outcome.result.model_dump()
    assert outcome.result.intent == "data_retrieval"
    assert "employees" in [t.lower() for t in outcome.result.tables_used]
    with demo_db.connect() as conn:
        rows = conn.execute(text(outcome.result.sql)).all()
    salaries = [row._mapping.get("salary", row[-1]) for row in rows]
    assert len(rows) == 5
    assert salaries == sorted(salaries, reverse=True)
    assert outcome.call.output_tokens > 0 and outcome.call.request_id
