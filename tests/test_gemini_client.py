"""Tests for backend/gemini_client.py.

The real google-genai SDK runs against an in-process mock HTTP transport, so
request construction, response handling and error mapping are tested end to
end without network access, an API key or free-tier quota.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Literal

import httpx2
import pytest
from pydantic import BaseModel, Field

from backend.exceptions import (
    LLMAuthenticationError,
    LLMConfigurationError,
    LLMConnectionError,
    LLMRateLimitError,
    LLMRefusalError,
    LLMRequestError,
    LLMResponseError,
    LLMServiceError,
    LLMTimeoutError,
)
from backend.gemini_client import GeminiClient, gemini_schema, thinking_level
from backend.llm_client import StageSettings, UsageTracker, create_llm_client
from backend.models import SQLExplanationResult, SQLGenerationResult, VerificationVerdict
from backend.pipeline import QueryPipeline
from backend.prompt_manager import PRESETS, PromptManager
from tests.fakes import generation, verdict

KEY = "AIza" "SyTEST-0123456789abcdefghijklmnopqrs"
STAGES = {
    "generation": StageSettings("gemini-3.8-flash", "medium", 4000),
    "correction": StageSettings("gemini-3.8-flash", "medium", 4000),
    "verification": StageSettings("gemini-3.5-flash-lite", "low", 2000),
    "explanation": StageSettings("gemini-3.8-flash", "low", 2000),
}
USAGE = {"promptTokenCount": 3120, "cachedContentTokenCount": 3000, "candidatesTokenCount": 40,
         "thoughtsTokenCount": 25, "totalTokenCount": 3185}


def response_json(text: str, *, finish: str = "STOP", model: str = "gemini-3.8-flash", usage: dict | None = None,
                  thought: bool = True) -> dict:
    parts = ([{"text": "Considering the schema.", "thought": True}] if thought else []) + [{"text": text}]
    return {
        "candidates": [{"content": {"role": "model", "parts": parts}, "finishReason": finish, "index": 0}],
        "usageMetadata": usage or USAGE,
        "modelVersion": model,
        "responseId": "resp_abc",
    }


class Recorder:
    """Mock transport handler that records requests and replays responses (the last one repeats)."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, Exception):
            raise item
        return item

    @property
    def body(self) -> dict:
        return json.loads(self.requests[-1].content)


def make_client(*responses, max_retries: int = 0, tracker: UsageTracker | None = None, api_key: str | None = KEY,
                stages: dict[str, StageSettings] = STAGES):
    recorder = Recorder(*responses)
    client = GeminiClient(stage_settings=stages, api_key=api_key, max_retries=max_retries, retry_initial_delay=0.0,
                          tracker=tracker, http_client=httpx2.Client(transport=httpx2.MockTransport(recorder)))
    return client, recorder


def ok(model_obj: BaseModel, **kwargs) -> httpx2.Response:
    return httpx2.Response(200, json=response_json(model_obj.model_dump_json(), **kwargs))


def error(code: int, message: str, status: str, details: list[dict] | None = None) -> httpx2.Response:
    return httpx2.Response(code, json={"error": {"code": code, "message": message, "status": status,
                                                 "details": details or []}})


def level(body: dict) -> str | None:
    config = body["generationConfig"].get("thinkingConfig", {})
    return config.get("thinkingLevel", config.get("thinking_level"))


@pytest.fixture(scope="module")
def prompt(demo_schema):
    return PromptManager(demo_schema, reference_date=date(2026, 11, 15)).build_generation_prompt(
        "Top 5 employees by salary", config=PRESETS["C"])


@pytest.fixture(scope="module")
def verification_prompt(demo_schema):
    return PromptManager(demo_schema).build_verification_prompt("q", sql="SELECT 1")


# ------------------------------------------------------------------ requests
def test_request_body_matches_design(prompt):
    client, rec = make_client(ok(generation("SELECT employee_name FROM employees")))
    client.complete(prompt, SQLGenerationResult)
    request = rec.requests[-1]
    assert request.url.host == "generativelanguage.googleapis.com"
    assert request.url.path.endswith("/models/gemini-3.8-flash:generateContent")
    assert request.headers["x-goog-api-key"] == KEY
    body = rec.body
    assert [p["text"] for p in body["systemInstruction"]["parts"]] == [prompt.system_text]
    assert body["contents"] == [{"role": "user", "parts": [{"text": s.text} for s in prompt.user_sections]}]
    config = body["generationConfig"]
    assert config["maxOutputTokens"] == 4000 and config["responseMimeType"] == "application/json"
    assert config["responseJsonSchema"] == gemini_schema(SQLGenerationResult)
    assert level(body) == "MEDIUM" and "includeThoughts" not in config["thinkingConfig"]
    assert not {"temperature", "topP", "topK"} & set(config)  # no sampling parameters


def test_verification_uses_its_own_model_and_level(verification_prompt):
    client, rec = make_client(ok(verdict(), model="gemini-3.5-flash-lite"))
    client.complete(verification_prompt, VerificationVerdict)
    assert rec.requests[-1].url.path.endswith("/models/gemini-3.5-flash-lite:generateContent")
    assert level(rec.body) == "LOW"


def test_thinking_config_omitted_for_models_without_levels(prompt):
    stages = {**STAGES, "generation": StageSettings("gemini-2.5-flash-lite", None, 1000)}
    client, rec = make_client(ok(generation()), stages=stages)
    client.complete(prompt, SQLGenerationResult)
    assert "thinkingConfig" not in rec.body["generationConfig"]


@pytest.mark.parametrize(
    "model, effort, expected",
    [
        ("gemini-3.8-flash", "low", "LOW"), ("gemini-3.8-flash", "medium", "MEDIUM"),
        ("gemini-3.8-flash", "high", "HIGH"), ("gemini-3.8-flash", "xhigh", "HIGH"), ("gemini-3.8-flash", "max", "HIGH"),
        ("gemini-4-hydrogen", "max", "XHIGH"), ("gemini-4-neon", "xhigh", "XHIGH"), ("gemini-4-carbon", "xhigh", "HIGH"),
        ("gemini-3.5-flash-lite", "low", "LOW"), ("gemini-3-flash-preview", "high", "HIGH"),
        ("gemini-2.5-flash-lite", "low", None), ("gemini-flash-latest", "medium", None), ("gemini-3.8-flash", None, None),
    ],
)
def test_thinking_level_mapping(model, effort, expected):
    assert thinking_level(model, effort) == expected


def test_preview_request_shows_structure_not_text(prompt):
    client, rec = make_client(ok(generation()))
    preview, output_format = client.preview_request(prompt, SQLGenerationResult)
    assert preview["model"] == "gemini-3.8-flash"
    assert preview["config"]["system_instruction"] == {"chars": len(prompt.system_text)}
    assert preview["config"]["thinking_config"] == {"thinking_level": "MEDIUM"}
    assert preview["contents"] == [{"role": "user", "parts": len(prompt.user_sections)}]
    assert output_format == {"response_mime_type": "application/json",
                             "response_json_schema": gemini_schema(SQLGenerationResult)}
    assert prompt.system_text[:60] not in json.dumps(preview)
    assert rec.requests == []  # a preview never calls the API


def test_from_settings_and_factory(make_settings):
    settings = make_settings(gemini_api_key=KEY, verifier_model="gemini-3.5-flash-lite", verification_effort="high",
                             max_output_tokens=8000)
    client = create_llm_client(settings)
    assert isinstance(client, GeminiClient) and client.provider == "gemini"
    assert client.stage_settings["generation"] == StageSettings("gemini-3.8-flash", "medium", 8000)
    assert client.stage_settings["verification"] == StageSettings("gemini-3.5-flash-lite", "high", 8000)
    older = GeminiClient.from_settings(make_settings(model="gemini-2.5-flash-lite"))
    assert older.stage_settings["generation"].effort is None
    assert isinstance(create_llm_client(make_settings()), GeminiClient)  # no key: still builds (lazily)


def test_missing_stage_settings_rejected():
    with pytest.raises(LLMConfigurationError):
        GeminiClient(stage_settings={"generation": STAGES["generation"]}, api_key=KEY)


# ------------------------------------------------------------------ responses
def test_successful_call_is_parsed_and_recorded(prompt):
    tracker = UsageTracker()
    client, _ = make_client(ok(generation("SELECT 1;", confidence=1.4)), tracker=tracker)
    response = client.complete(prompt, SQLGenerationResult)
    assert response.output.sql == "SELECT 1" and response.output.confidence == 1.0  # validators ran, thought ignored
    record = response.record
    assert (record.input_tokens, record.cache_read_input_tokens, record.output_tokens) == (120, 3000, 65)
    assert record.served_model == "gemini-3.8-flash" and record.request_id == "resp_abc"
    assert record.stop_reason == "stop" and record.effort == "medium"
    assert record.estimated_cost_usd is None and record.fallback_used is False
    summary = tracker.summary()
    assert summary["calls"] == 1 and summary["cache_hit_ratio"] == round(3000 / 3120, 3)


def test_truncated_response_is_reported(prompt):
    client, _ = make_client(ok(generation(), finish="MAX_TOKENS"))
    with pytest.raises(LLMResponseError, match="cut off"):
        client.complete(prompt, SQLGenerationResult)


@pytest.mark.parametrize("finish", ["SAFETY", "PROHIBITED_CONTENT", "RECITATION"])
def test_safety_stop_is_a_refusal(prompt, finish):
    client, _ = make_client(ok(generation(), finish=finish))
    with pytest.raises(LLMRefusalError) as excinfo:
        client.complete(prompt, SQLGenerationResult)
    assert excinfo.value.category == finish.lower()


def test_blocked_prompt_is_a_refusal(prompt):
    tracker = UsageTracker()
    body = {"promptFeedback": {"blockReason": "PROHIBITED_CONTENT"}, "usageMetadata": {"promptTokenCount": 3000}}
    client, _ = make_client(httpx2.Response(200, json=body), tracker=tracker)
    with pytest.raises(LLMRefusalError) as excinfo:
        client.complete(prompt, SQLGenerationResult)
    assert excinfo.value.category == "prohibited_content"
    assert tracker.records[0].stop_reason == "prompt_blocked" and tracker.records[0].input_tokens == 3000


@pytest.mark.parametrize("text, message", [("not json", "unexpected format"), ("", "empty response")])
def test_unusable_output_is_reported(prompt, text, message):
    client, _ = make_client(httpx2.Response(200, json=response_json(text)))
    with pytest.raises(LLMResponseError, match=message):
        client.complete(prompt, SQLGenerationResult)


# ------------------------------------------------------------------ errors
RETRY_INFO = {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "34s"}
DAILY_QUOTA = {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
               "violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier", "quotaValue": "250"}]}
KEY_INVALID = {"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "API_KEY_INVALID"}


@pytest.mark.parametrize(
    "response, error_type, text",
    [
        (error(400, "API key not valid. Please pass a valid API key.", "INVALID_ARGUMENT", [KEY_INVALID]),
         LLMAuthenticationError, "GEMINI_API_KEY"),
        (error(403, "Permission denied.", "PERMISSION_DENIED"), LLMAuthenticationError, "not allowed"),
        (error(404, "models/gemini-9 is not found.", "NOT_FOUND"), LLMConfigurationError, "QB_MODEL"),
        (error(429, "Resource exhausted.", "RESOURCE_EXHAUSTED", [RETRY_INFO]), LLMRateLimitError, "about 34 seconds"),
        (error(429, "You exceeded your current quota.", "RESOURCE_EXHAUSTED", [DAILY_QUOTA, RETRY_INFO]),
         LLMRateLimitError, "midnight Pacific"),
        (error(400, "User location is not supported for the API use without a billing account linked.",
               "FAILED_PRECONDITION"), LLMConfigurationError, "location"),
        (error(400, "Invalid JSON payload received.", "INVALID_ARGUMENT"), LLMRequestError, "rejected"),
        (error(503, "The model is overloaded.", "UNAVAILABLE"), LLMServiceError, "temporarily unavailable"),
        (error(500, "Internal error.", "INTERNAL"), LLMServiceError, "temporarily unavailable"),
    ],
)
def test_api_errors_are_mapped(prompt, response, error_type, text):
    tracker = UsageTracker()
    client, rec = make_client(response, tracker=tracker)
    with pytest.raises(error_type, match=text):
        client.complete(prompt, SQLGenerationResult)
    assert tracker.failed_calls == 1 and tracker.records == [] and len(rec.requests) == 1


def test_rate_limit_carries_the_suggested_wait(prompt):
    client, _ = make_client(error(429, "Resource exhausted.", "RESOURCE_EXHAUSTED", [RETRY_INFO]))
    with pytest.raises(LLMRateLimitError) as excinfo:
        client.complete(prompt, SQLGenerationResult)
    assert excinfo.value.retry_after == 34.0 and excinfo.value.retryable


@pytest.mark.parametrize(
    "exc, error_type",
    [(httpx2.ConnectError("connection refused"), LLMConnectionError), (httpx2.ReadTimeout("timed out"), LLMTimeoutError)],
)
def test_transport_failures_are_mapped(prompt, exc, error_type):
    client, _ = make_client(exc)
    with pytest.raises(error_type):
        client.complete(prompt, SQLGenerationResult)


def test_sdk_retries_an_overload_then_succeeds(prompt):
    client, rec = make_client(error(503, "The model is overloaded.", "UNAVAILABLE"), ok(generation()), max_retries=1)
    assert client.complete(prompt, SQLGenerationResult).output.sql == "SELECT 1"
    assert len(rec.requests) == 2


def test_missing_key_is_reported_when_asking_not_at_startup(prompt, isolated_env):
    client, rec = make_client(ok(generation()), api_key=None)  # building the client needs no key
    with pytest.raises(LLMAuthenticationError, match="No Gemini API key"):
        client.complete(prompt, SQLGenerationResult)
    assert rec.requests == []


def test_api_key_never_appears_in_errors(prompt):
    client, _ = make_client(error(400, f"Bad request with key {KEY} in it.", "INVALID_ARGUMENT"))
    with pytest.raises(LLMRequestError) as excinfo:
        client.complete(prompt, SQLGenerationResult)
    assert KEY not in f"{excinfo.value.user_message} {excinfo.value.detail}"


# ------------------------------------------------------------------ output schema
_ALLOWED = {"type", "description", "enum", "required", "properties", "items", "anyOf", "prefixItems",
            "additionalProperties", "minimum", "maximum", "minItems", "maxItems", "format"}


def _keywords(node, found: set[str]) -> set[str]:
    if isinstance(node, dict):
        found.update(node)
        for key, value in node.items():
            if key == "properties":
                for sub in value.values():
                    _keywords(sub, found)
            elif key in ("items", "additionalProperties"):
                _keywords(value, found)
            elif key in ("anyOf", "prefixItems"):
                for sub in value:
                    _keywords(sub, found)
    return found


@pytest.mark.parametrize("output_model", [SQLGenerationResult, VerificationVerdict, SQLExplanationResult])
def test_app_output_schemas_use_only_supported_keywords(output_model):
    schema = gemini_schema(output_model)
    assert _keywords(schema, set()) <= _ALLOWED
    original = output_model.model_json_schema()
    assert list(schema["properties"]) == list(original["properties"])  # field order kept: the model answers in order
    assert schema["required"] == original["required"]


class _Inner(BaseModel):
    label: str = Field(description="inner label")


class _Example(BaseModel):
    kind: Literal["a", "b"]
    fixed: Literal["x"]
    note: str | None = Field(description="optional note")
    choice: Literal["p", "q"] | None
    inner: _Inner = Field(description="the inner object")
    maybe_inner: _Inner | None
    tags: list[str] = Field(max_length=3)
    when: date
    score: float = Field(ge=0, le=1)
    code: str = Field(pattern=r"^\w+$", max_length=10)


class _Node(BaseModel):
    children: list[_Node] = Field(default_factory=list)


def test_schema_simplification():
    schema = gemini_schema(_Example)
    props = schema["properties"]
    assert "title" not in schema and "$defs" not in schema
    assert props["kind"] == {"type": "string", "enum": ["a", "b"]}
    assert props["fixed"] == {"type": "string", "enum": ["x"]}
    assert props["note"] == {"type": ["string", "null"], "description": "optional note"}
    assert props["choice"]["anyOf"] == [{"type": "string", "enum": ["p", "q"]}, {"type": "null"}]
    assert props["inner"] == {"type": "object", "description": "the inner object", "required": ["label"],
                              "properties": {"label": {"type": "string", "description": "inner label"}}}
    assert props["maybe_inner"]["anyOf"][0]["properties"]["label"]["type"] == "string"
    assert props["tags"] == {"type": "array", "items": {"type": "string"}, "maxItems": 3}
    assert props["when"] == {"type": "string", "format": "date"}
    assert props["score"] == {"type": "number", "minimum": 0, "maximum": 1}
    assert props["code"] == {"type": "string"}  # pattern / maxLength are checked by Pydantic instead


def test_recursive_output_models_are_rejected():
    with pytest.raises(LLMConfigurationError, match="recursive"):
        gemini_schema(_Node)


# ------------------------------------------------------------------ whole pipeline
def test_pipeline_answers_end_to_end_with_gemini(demo_db, demo_schema):
    sql = "SELECT employee_name, salary FROM employees ORDER BY salary DESC LIMIT 5"
    client, rec = make_client(ok(generation(sql, tables=["employees"])), ok(verdict(True), model="gemini-3.5-flash-lite"))
    pipeline = QueryPipeline(db=demo_db, schema=demo_schema, llm=client, config=PRESETS["C"],
                             reference_date=date(2026, 11, 15), history=None)
    result = pipeline.ask("Show me the top 5 employees with the highest salaries.")
    assert result.status == "success" and result.formatted.row_count == 5
    assert [c.stage for c in result.llm_calls] == ["generation", "verification"]
    assert [r.url.path.rsplit("/", 1)[-1] for r in rec.requests] == [
        "gemini-3.8-flash:generateContent", "gemini-3.5-flash-lite:generateContent"]
