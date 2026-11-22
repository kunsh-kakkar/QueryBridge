"""
QueryBridge exception hierarchy.

Every error carries a ``user_message`` that is safe to show in the UI: it never
contains credentials, raw connection strings or internal stack details. The
optional ``detail`` field holds sanitised technical context for logs and the
pipeline trace.
"""

from __future__ import annotations


class QueryBridgeError(Exception):
    """Base class for all expected, user-reportable failures."""

    def __init__(self, user_message: str, *, detail: str | None = None) -> None:
        super().__init__(user_message)
        self.user_message = user_message
        self.detail = detail


class ConfigurationError(QueryBridgeError):
    """Invalid or missing configuration."""


class DatabaseConnectionError(QueryBridgeError):
    """The database could not be opened, or the connection was refused."""


class SchemaInspectionError(QueryBridgeError):
    """The database schema could not be read."""


class InputValidationError(QueryBridgeError):
    """The user's question was rejected before reaching the model (empty, too long...)."""


# --- LLM errors ---------------------------------------------------------------
class LLMError(QueryBridgeError):
    """Base class for failures talking to the AI provider's API (Google Gemini)."""

    retryable: bool = False


class LLMAuthenticationError(LLMError):
    """Missing, invalid or insufficiently privileged API credentials."""


class LLMConfigurationError(LLMError):
    """The request configuration is invalid (e.g. unknown model id)."""


class LLMRequestError(LLMError):
    """The API rejected the request (HTTP 400)."""


class LLMRateLimitError(LLMError):
    """Rate limit reached (HTTP 429), after the SDK's automatic retries."""

    retryable = True

    def __init__(self, user_message: str, *, detail: str | None = None, retry_after: float | None = None) -> None:
        super().__init__(user_message, detail=detail)
        self.retry_after = retry_after


class LLMConnectionError(LLMError):
    """Network failure reaching the API."""

    retryable = True


class LLMTimeoutError(LLMConnectionError):
    """The API did not answer within the configured timeout."""


class LLMServiceError(LLMError):
    """The API had an internal error or was overloaded (HTTP 5xx / 529)."""

    retryable = True


class LLMRefusalError(LLMError):
    """The model declined the request (stop_reason = refusal)."""

    def __init__(self, user_message: str, *, detail: str | None = None, category: str | None = None) -> None:
        super().__init__(user_message, detail=detail)
        self.category = category


class LLMResponseError(LLMError):
    """The model's response was truncated or did not match the output contract."""
