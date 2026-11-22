"""
Google Gemini client: the ``StructuredLLM`` implementation (Gemini has a free tier).

It implements the interface in ``backend/llm_client.py``, so the pipeline, the
verifier, the web app and the evaluation never talk to the SDK directly.

Responsibilities
----------------
* **Structured output.** The Pydantic output model's JSON schema is reduced to
  the subset Gemini's constrained decoding supports (``$ref`` inlined, titles
  and other unsupported keywords removed) and sent as ``response_json_schema``
  with ``response_mime_type="application/json"``. The finish reason is checked
  *before* validation, so a safety block or a truncated answer is reported as
  such and not as a JSON error. Pydantic then validates the full model.
* **Per-stage settings.** Generation, correction, verification and
  explanation each have their own model, thinking level and token budget.
  QueryBridge's effort levels map to Gemini thinking levels on Gemini 3 and
  later; other models keep their default. Thought text is never requested.
* **Caching.** Gemini caches repeated prompt prefixes implicitly. The stable
  system instruction (role, rules, dialect, schema) comes first, and the
  cached token count is recorded per call.
* **Typed errors.** SDK and transport failures map to the same ``LLMError``
  subclasses (``backend/exceptions.py``), with UI-safe messages: missing or
  invalid key, permission, unknown model, rate limit or used-up free daily
  quota (with the suggested wait), timeout, network, overload / 5xx, safety
  blocks, and invalid or truncated output. The SDK retries 408/429/5xx and
  transient network errors itself.
* **No key, no crash.** The SDK refuses to build a client without a key, so
  the client is created on first use. The app starts, and asking a question
  explains how to add a key.
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Mapping
from functools import lru_cache
from typing import Any

import httpx2
from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from pydantic import BaseModel, ValidationError

from backend.exceptions import (
    LLMAuthenticationError,
    LLMConfigurationError,
    LLMConnectionError,
    LLMError,
    LLMRateLimitError,
    LLMRefusalError,
    LLMRequestError,
    LLMResponseError,
    LLMServiceError,
    LLMTimeoutError,
)
from backend.llm_client import LLMResponse, ProviderLLM, StageSettings, T, UsageTracker
from backend.models import BuiltPrompt, LLMCallRecord, PromptStage
from config import Settings
from utils.helpers import Timer, truncate_text
from utils.logger import get_logger, redact, register_secret

logger = get_logger(__name__)

KEY_HELP = "Create a free key in Google AI Studio (aistudio.google.com) and set GEMINI_API_KEY in your .env file."

# --------------------------------------------------------------------------- thinking
# Gemini 3 and later take a thinking level. Every one of them accepts low, medium
# and high; xhigh exists only on the models listed in _XHIGH_MODELS, so QueryBridge's
# xhigh and max become high elsewhere. Older models (gemini-2.5-*) and aliases keep
# their default thinking.
_THINKING_LEVELS = {"low": "LOW", "medium": "MEDIUM", "high": "HIGH", "xhigh": "XHIGH", "max": "XHIGH"}
_LEVEL_MODELS = re.compile(r"^gemini-(?:[3-9]|[1-9]\d)(?:[.-]|$)")
_XHIGH_MODELS = re.compile(r"^gemini-4-(?:neon|hydrogen)(?:-|$)")


def supports_thinking_level(model: str) -> bool:
    return bool(_LEVEL_MODELS.match(model))


def thinking_level(model: str, effort: str | None) -> str | None:
    """Gemini ``thinking_level`` for a QueryBridge effort level (None = model default)."""
    if not effort or not supports_thinking_level(model):
        return None
    level = _THINKING_LEVELS[effort]
    if level == "XHIGH" and not _XHIGH_MODELS.match(model):
        level = "HIGH"
    return level


# --------------------------------------------------------------------------- output schema
_KEPT_KEYWORDS = frozenset({
    "type", "description", "enum", "required", "minimum", "maximum", "minItems", "maxItems",
})
_FORMATS = frozenset({"date-time", "date", "time"})
_PRIMITIVES = frozenset({"string", "number", "integer", "boolean"})
_NULL = {"type": "null"}


@lru_cache(maxsize=32)
def gemini_schema(output_model: type[BaseModel]) -> dict[str, Any]:
    """The JSON schema Gemini enforces for ``output_model`` (cached).

    ``$ref`` is inlined, ``title``/``default``/``examples`` and keywords Gemini
    does not enforce are dropped, and ``anyOf [primitive, null]`` becomes a
    nullable type array. Pydantic still validates the full model afterwards.
    """
    schema = output_model.model_json_schema()
    definitions = schema.pop("$defs", {})
    return _simplify(schema, definitions, ())


def _simplify(node: Any, definitions: Mapping[str, Any], seen: tuple[str, ...]) -> Any:
    if isinstance(node, list):
        return [_simplify(item, definitions, seen) for item in node]
    if not isinstance(node, dict):
        return node
    if "$ref" in node:
        name = str(node["$ref"]).rsplit("/", 1)[-1]
        if name in seen:
            raise LLMConfigurationError(f"The output model '{name}' is recursive, which Gemini schemas cannot express.")
        siblings = {k: v for k, v in node.items() if k != "$ref"}
        return _simplify({**definitions[name], **siblings}, definitions, (*seen, name))
    if isinstance(node.get("allOf"), list) and len(node["allOf"]) == 1:  # older Pydantic wraps a described $ref
        siblings = {k: v for k, v in node.items() if k != "allOf"}
        return _simplify({**node["allOf"][0], **siblings}, definitions, seen)
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key == "properties":
            out[key] = {name: _simplify(sub, definitions, seen) for name, sub in value.items()}
        elif key in ("items", "additionalProperties"):
            out[key] = _simplify(value, definitions, seen)
        elif key in ("anyOf", "oneOf", "prefixItems"):
            out["prefixItems" if key == "prefixItems" else "anyOf"] = [_simplify(sub, definitions, seen) for sub in value]
        elif key == "const":
            out["enum"] = [value]
        elif key == "format":
            if value in _FORMATS:
                out[key] = value
        elif key in _KEPT_KEYWORDS:
            out[key] = value
    return _collapse_nullable(out)


def _collapse_nullable(node: dict[str, Any]) -> dict[str, Any]:
    """``anyOf: [{"type": "string"}, {"type": "null"}]`` -> ``type: ["string", "null"]``."""
    options = node.get("anyOf")
    if not isinstance(options, list) or len(options) != 2 or _NULL not in options:
        return node
    other = options[0] if options[1] == _NULL else options[1]
    kind = other.get("type")
    if not isinstance(kind, str) or kind not in _PRIMITIVES or not set(other) <= {"type", "description", "format"}:
        return node
    rest = {k: v for k, v in node.items() if k != "anyOf"}
    return {**other, **rest, "type": [other["type"], "null"]}


# --------------------------------------------------------------------------- responses
_REFUSAL_REASONS = frozenset({
    "SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII", "LANGUAGE", "MODEL_ARMOR", "JAILBREAK",
})


def _name(value: Any) -> str:
    """An SDK enum (or an unknown raw string) as its upper-case name."""
    return str(getattr(value, "value", value) or "").upper()


def _block_reason(response: Any) -> str | None:
    feedback = getattr(response, "prompt_feedback", None)
    reason = _name(getattr(feedback, "block_reason", None))
    return reason if reason and reason != "BLOCKED_REASON_UNSPECIFIED" else None


def _finish_reason(response: Any) -> str | None:
    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        return None
    return _name(candidates[0].finish_reason) or None


def _error_details(exc: genai_errors.APIError) -> list[dict[str, Any]]:
    details = exc.details if isinstance(exc.details, dict) else {}
    error = details.get("error", details)
    items = error.get("details", []) if isinstance(error, dict) else []
    return [item for item in items if isinstance(item, dict)]


def _retry_delay(details: list[dict[str, Any]]) -> float | None:
    for item in details:
        if str(item.get("@type", "")).endswith("RetryInfo"):
            match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)s\s*", str(item.get("retryDelay", "")))
            if match:
                return float(match.group(1))
    return None


def _daily_quota(details: list[dict[str, Any]]) -> bool:
    return any("perday" in str(violation.get("quotaId", "")).lower()
               for item in details for violation in item.get("violations", []) or []
               if isinstance(violation, dict))


# =============================================================================
# Client
# =============================================================================
class GeminiClient(ProviderLLM):
    """Calls Gemini with a ``BuiltPrompt`` and returns a validated Pydantic object."""

    provider = "gemini"

    def __init__(
        self,
        *,
        stage_settings: Mapping[str, StageSettings],
        api_key: str | None = None,
        timeout_seconds: float = 120.0,
        max_retries: int = 2,
        retry_initial_delay: float = 1.0,
        http_client: httpx2.Client | None = None,
        tracker: UsageTracker | None = None,
    ) -> None:
        self.stage_settings = self._checked_stages(stage_settings)
        self.tracker = tracker or UsageTracker()
        self._api_key = api_key
        register_secret(api_key)
        self._http_options = types.HttpOptions(
            timeout=int(timeout_seconds * 1000),  # milliseconds
            retry_options=types.HttpRetryOptions(attempts=max_retries + 1, initial_delay=retry_initial_delay,
                                                 jitter=min(1.0, retry_initial_delay)),
            httpx_client=http_client,
        )
        self._client: genai.Client | None = None
        self._client_lock = threading.Lock()

    @classmethod
    def from_settings(cls, settings: Settings, *, http_client: httpx2.Client | None = None,
                      tracker: UsageTracker | None = None) -> GeminiClient:
        def stage(model: str, effort: str, max_tokens: int) -> StageSettings:
            return StageSettings(model=model, effort=effort if supports_thinking_level(model) else None,
                                 max_tokens=max_tokens)

        tokens = settings.max_output_tokens
        model = settings.effective_model
        return cls(
            stage_settings={
                "generation": stage(model, settings.generation_effort, tokens),
                "correction": stage(model, settings.correction_effort, tokens),
                "verification": stage(settings.effective_verifier_model, settings.verification_effort, tokens),
                "explanation": stage(model, settings.explanation_effort, tokens),
            },
            api_key=settings.gemini_api_key.get_secret_value() if settings.gemini_api_key else None,
            timeout_seconds=settings.llm_timeout_seconds,
            max_retries=settings.llm_max_retries,
            http_client=http_client,
            tracker=tracker,
        )

    def _sdk(self) -> genai.Client:
        with self._client_lock:
            if self._client is None:
                self._client = genai.Client(api_key=self._api_key, http_options=self._http_options)
            return self._client

    # ---------------------------------------------------------------- request
    def request_kwargs(self, prompt: BuiltPrompt, output_model: type[BaseModel]) -> dict[str, Any]:
        """The exact ``generate_content`` arguments for this prompt (inspectable, tested)."""
        stage = self.stage_settings[prompt.stage]
        config: dict[str, Any] = {}
        if prompt.system_text.strip():
            config["system_instruction"] = prompt.system_text
        config.update({
            "max_output_tokens": stage.max_tokens,
            "response_mime_type": "application/json",
            "response_json_schema": gemini_schema(output_model),
        })
        level = thinking_level(stage.model, stage.effort)
        if level:
            config["thinking_config"] = {"thinking_level": level}
        parts = [{"text": s.text} for s in prompt.user_sections if s.text.strip()]
        return {"model": stage.model, "contents": [{"role": "user", "parts": parts}], "config": config}

    def preview_request(self, prompt: BuiltPrompt,
                        output_model: type[BaseModel]) -> tuple[dict[str, Any], dict[str, Any]]:
        request = self.request_kwargs(prompt, output_model)
        config = dict(request["config"])
        output_format = {key: config.pop(key) for key in ("response_mime_type", "response_json_schema")}
        system = config.pop("system_instruction", "")
        preview = {
            "model": request["model"],
            "config": {**config, "system_instruction": {"chars": len(system)}},
            "contents": [{"role": c["role"], "parts": len(c["parts"])} for c in request["contents"]],
        }
        return preview, output_format

    # ---------------------------------------------------------------- main API
    def complete(self, prompt: BuiltPrompt, output_model: type[T]) -> LLMResponse[T]:
        stage: PromptStage = prompt.stage
        settings = self.stage_settings[stage]
        kwargs = self.request_kwargs(prompt, output_model)
        timer = Timer().start()
        try:
            response = self._sdk().models.generate_content(**kwargs)
        except Exception as exc:  # mapped to typed, UI-safe errors
            self.tracker.add_failure()
            raise self._map_exception(exc, settings.model) from None

        record = self._record(stage, settings, response, timer.stop())
        self.tracker.add(record)
        logger.info(
            "LLM %s: model=%s in=%d (cache read %d) out=%d stop=%s %.0f ms",
            stage, record.served_model or record.model, record.input_tokens, record.cache_read_input_tokens,
            record.output_tokens, record.stop_reason, record.latency_ms,
        )
        return LLMResponse(output=self._parse(response, output_model, record), record=record)

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def _record(stage: PromptStage, settings: StageSettings, response: Any, latency_ms: float) -> LLMCallRecord:
        usage = getattr(response, "usage_metadata", None)

        def count(name: str) -> int:
            return int(getattr(usage, name, 0) or 0)

        prompt_tokens = count("prompt_token_count")
        cached = min(count("cached_content_token_count"), prompt_tokens)  # part of the prompt count
        block = _block_reason(response)
        finish = _finish_reason(response)
        return LLMCallRecord(
            stage=stage,
            model=settings.model,
            served_model=getattr(response, "model_version", None) or None,
            effort=settings.effort,
            input_tokens=prompt_tokens - cached,
            output_tokens=count("candidates_token_count") + count("thoughts_token_count"),
            cache_read_input_tokens=cached,
            latency_ms=round(latency_ms, 1),
            stop_reason="prompt_blocked" if block else (finish.lower() if finish else None),
            request_id=getattr(response, "response_id", None),
        )

    @staticmethod
    def _parse(response: Any, output_model: type[T], record: LLMCallRecord) -> T:
        block = _block_reason(response)
        finish = _finish_reason(response)
        if block or finish in _REFUSAL_REASONS:
            category = (block or finish or "").lower()
            raise LLMRefusalError(
                "The AI model declined to answer this request. Please rephrase it as a read-only data question.",
                detail=f"{'prompt blocked' if block else 'finish reason'}: {category}",
                category=category,
            )
        if finish == "MAX_TOKENS":
            raise LLMResponseError(
                "The AI response was cut off before it was complete. Try a simpler question, or raise "
                "QB_MAX_OUTPUT_TOKENS.",
                detail=f"finish_reason=MAX_TOKENS after {record.output_tokens} output tokens",
            )
        candidates = getattr(response, "candidates", None) or []
        content = candidates[0].content if candidates else None
        parts = getattr(content, "parts", None) or []
        text = "".join(part.text for part in parts if part.text and not part.thought)
        if not text.strip():
            raise LLMResponseError("The AI model returned an empty response.", detail=f"finish_reason={finish}")
        try:
            return output_model.model_validate_json(text)
        except (ValidationError, json.JSONDecodeError) as exc:
            raise LLMResponseError(
                "The AI model returned a response in an unexpected format.",
                detail=truncate_text(redact(str(exc)), 300),
            ) from None

    @staticmethod
    def _map_exception(exc: Exception, model: str) -> LLMError:
        """Translate SDK/transport exceptions into typed errors with safe messages."""
        if isinstance(exc, genai_errors.APIError):
            code = int(exc.code or 0)
            message = truncate_text(redact(str(exc.message or exc)), 300)
            details = _error_details(exc)
            reasons = {str(item.get("reason", "")) for item in details}
            if code == 429 or exc.status == "RESOURCE_EXHAUSTED":
                retry_after = _retry_delay(details)
                if _daily_quota(details):
                    return LLMRateLimitError(
                        f"Today's free Gemini requests for {model} are used up. They reset at midnight Pacific "
                        "time; until then you can set QB_MODEL in .env to another free model.",
                        detail=message, retry_after=retry_after)
                wait = f" Try again in about {retry_after:.0f} seconds." if retry_after else " Please wait a moment and retry."
                return LLMRateLimitError("The Gemini API rate limit was reached." + wait,
                                         detail=message, retry_after=retry_after)
            if code == 401 or "API_KEY_INVALID" in reasons or "api key not valid" in message.lower():
                return LLMAuthenticationError("The Gemini API key is invalid or has been revoked. Check GEMINI_API_KEY.",
                                              detail=message)
            if code == 403:
                return LLMAuthenticationError(f"The Gemini API key is not allowed to use model '{model}'.",
                                              detail=message)
            if code == 404:
                return LLMConfigurationError(f"Model '{model}' was not found. Check QB_MODEL / QB_VERIFIER_MODEL.",
                                             detail=message)
            if "location is not supported" in message.lower():
                return LLMConfigurationError(
                    "Google does not offer the Gemini API in your location without a billing account.",
                    detail=message)
            if code >= 500:
                return LLMServiceError("The AI service is temporarily unavailable or overloaded. Please try again.",
                                       detail=f"HTTP {code}: {message}")
            if code == 400:
                return LLMRequestError("The AI service rejected the request.", detail=message)
            return LLMRequestError(f"The AI service returned an error (HTTP {code}).", detail=message)
        detail = truncate_text(redact(str(exc)), 300)
        if isinstance(exc, httpx2.TimeoutException):
            return LLMTimeoutError("The AI service did not respond in time. Please try again.", detail=detail)
        if isinstance(exc, httpx2.TransportError):
            return LLMConnectionError("Could not reach the Gemini API. Check your internet connection.", detail=detail)
        if isinstance(exc, ValueError) and "api key" in str(exc).lower():
            return LLMAuthenticationError("No Gemini API key is configured. " + KEY_HELP,
                                          detail="SDK could not find an API key")
        if isinstance(exc, genai_errors.UnknownApiResponseError):
            return LLMError("Unexpected response from the AI service.", detail=detail)
        raise exc  # programming errors must not be hidden
