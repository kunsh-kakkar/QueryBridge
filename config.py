"""
QueryBridge configuration.

All runtime settings come from environment variables (prefix ``QB_``, plus the
standard ``GEMINI_API_KEY``) or from a ``.env`` file in the project root.
Pydantic validates every value, so a misconfiguration fails at startup
instead of surfacing halfway through a query.

Security guarantees enforced here:

* The API key and the database URL are ``SecretStr`` values: ``repr()``,
  logs and validation errors never show them (``hide_input_in_errors``).
* The SQL correction loop is capped at ``HARD_MAX_CORRECTION_ATTEMPTS``; a
  larger configured value is rejected, never silently accepted.
* Only SQLite, PostgreSQL and MySQL URLs are accepted.
"""

from __future__ import annotations

import os
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError

PROJECT_ROOT: Path = Path(__file__).resolve().parent
ENV_FILE: Path = PROJECT_ROOT / ".env"
DEFAULT_DATA_DIR: Path = PROJECT_ROOT / "data"

# The AI provider is Google Gemini (backend/gemini_client.py behind the
# interface in backend/llm_client.py). It has a free tier.
LLM_PROVIDER = "gemini"
DEFAULT_MODEL = "gemini-3.5-flash-lite"
PROVIDER_NAME = "Google Gemini"
API_KEY_VARIABLE = "GEMINI_API_KEY"
HARD_MAX_CORRECTION_ATTEMPTS = 3
SUPPORTED_DIALECTS: tuple[str, ...] = ("sqlite", "postgresql", "mysql")

EffortLevel = Literal["low", "medium", "high", "xhigh", "max"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

# Optional fields where an empty string in .env means "not set".
_OPTIONAL_FIELDS = (
    "gemini_api_key",
    "model",
    "verifier_model",
    "database_url",
    "reference_date",
    "log_file",
    "allowed_hosts",
)


def parse_database_url(raw: str) -> URL:
    """Parse and validate a SQLAlchemy database URL.

    Raises ``ValueError`` with a message that never contains the URL itself,
    because a URL can carry a password.
    """
    try:
        url = make_url(raw.strip())
    except (ArgumentError, ValueError):
        raise ValueError(
            "Database URL is malformed (expected e.g. sqlite:///path/to/file.db)."
        ) from None
    backend = url.get_backend_name()
    if backend not in SUPPORTED_DIALECTS:
        raise ValueError(
            f"Unsupported database backend '{backend}'. "
            f"Supported: {', '.join(SUPPORTED_DIALECTS)}."
        )
    return url


class Settings(BaseSettings):
    """Validated application settings (immutable after load)."""

    model_config = SettingsConfigDict(
        env_prefix="QB_",
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        frozen=True,
        hide_input_in_errors=True,
        validate_by_name=True,
        validate_by_alias=True,
    )

    # --- AI provider ----------------------------------------------------------
    gemini_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("GEMINI_API_KEY", "GOOGLE_API_KEY", "QB_GEMINI_API_KEY"),
        description="Google Gemini API key (read from GEMINI_API_KEY, or GOOGLE_API_KEY).",
    )
    model: str | None = Field(default=None, description="Model for generation/correction (None = default model).")
    verifier_model: str | None = Field(default=None, description="Model for semantic verification.")
    generation_effort: EffortLevel = "medium"
    verification_effort: EffortLevel = "low"
    correction_effort: EffortLevel = "medium"
    explanation_effort: EffortLevel = "low"
    max_output_tokens: int = Field(default=16000, ge=1024, le=64000)
    llm_timeout_seconds: float = Field(default=120.0, gt=0, le=600)
    llm_max_retries: int = Field(default=2, ge=0, le=5)

    # --- Database -----------------------------------------------------------
    database_url: SecretStr | None = Field(
        default=None, description="SQLAlchemy URL; None = bundled SQLite demo DB."
    )
    data_dir: Path = DEFAULT_DATA_DIR
    demo_db_filename: str = "querybridge_demo.db"
    meta_db_filename: str = "querybridge_meta.db"

    # --- Safety & execution limits -------------------------------------------
    query_timeout_seconds: float = Field(default=10.0, ge=0.5, le=120)
    max_result_rows: int = Field(default=1000, ge=1, le=50000)
    max_correction_attempts: int = Field(default=2, ge=0, le=HARD_MAX_CORRECTION_ATTEMPTS)
    max_question_chars: int = Field(default=1000, ge=50, le=5000)

    # --- Prompt construction -------------------------------------------------
    conversation_turns: int = Field(default=5, ge=0, le=20)
    few_shot_k: int = Field(default=4, ge=0, le=10)
    # Small schemas are sent whole: a stable, cacheable prompt prefix and no
    # risk of leaving out a needed table. Retrieval starts above this size.
    schema_full_context_max_tables: int = Field(default=12, ge=1, le=500)
    schema_max_tables: int = Field(default=6, ge=1, le=500)
    include_sample_values: bool = True
    sample_values_per_column: int = Field(default=10, ge=0, le=25)
    enable_semantic_verification: bool = True
    confidence_warning_threshold: float = Field(default=0.6, ge=0.0, le=1.0)
    reference_date: date | None = None

    # --- Web hosting -----------------------------------------------------------
    hosted: bool = Field(
        default=False,
        description="Running as a public website: changing the database from the browser is turned off.",
    )
    allowed_hosts: str | None = Field(
        default=None,
        description="Extra host names the web server answers to, comma-separated (e.g. the public host name). "
                    "On Render, RENDER_EXTERNAL_HOSTNAME is added automatically.",
    )

    # --- Logging --------------------------------------------------------------
    log_level: LogLevel = "INFO"
    log_file: Path | None = None

    # ------------------------------------------------------------------------
    # Validators
    # ------------------------------------------------------------------------
    @field_validator(*_OPTIONAL_FIELDS, mode="before")
    @classmethod
    def _empty_string_is_none(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("model", "verifier_model")
    @classmethod
    def _validate_model_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value or any(ch.isspace() for ch in value):
            raise ValueError("Model id must be a non-empty string without spaces.")
        return value

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_log_level(cls, value: Any) -> Any:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("database_url")
    @classmethod
    def _validate_database_url(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None:
            parse_database_url(value.get_secret_value())
        return value

    @field_validator("demo_db_filename", "meta_db_filename")
    @classmethod
    def _plain_filename(cls, value: str) -> str:
        value = value.strip()
        if not value or Path(value).name != value or value in {".", ".."}:
            raise ValueError("Must be a plain file name without directories.")
        return value

    @field_validator("data_dir", "log_file")
    @classmethod
    def _resolve_relative_path(cls, value: Path | None) -> Path | None:
        if value is None:
            return None
        value = value.expanduser()
        return value if value.is_absolute() else (PROJECT_ROOT / value).resolve()

    # ------------------------------------------------------------------------
    # Derived values
    # ------------------------------------------------------------------------
    @property
    def demo_db_path(self) -> Path:
        return self.data_dir / self.demo_db_filename

    @property
    def meta_db_path(self) -> Path:
        return self.data_dir / self.meta_db_filename

    @property
    def is_demo_database(self) -> bool:
        return self.database_url is None

    def database_url_value(self) -> str:
        """Return the real connection URL, which may contain a password.

        Only the engine factory should call this. Never log or display the result.
        """
        if self.database_url is None:
            return f"sqlite:///{self.demo_db_path.as_posix()}"
        return self.database_url.get_secret_value().strip()

    @property
    def masked_database_url(self) -> str:
        """Connection URL with the password replaced by ``***`` (safe to show)."""
        return parse_database_url(self.database_url_value()).render_as_string(hide_password=True)

    @property
    def database_dialect(self) -> str:
        return parse_database_url(self.database_url_value()).get_backend_name()

    @property
    def effective_model(self) -> str:
        return self.model or DEFAULT_MODEL

    @property
    def effective_verifier_model(self) -> str:
        return self.verifier_model or self.effective_model

    @property
    def effective_reference_date(self) -> date:
        return self.reference_date or date.today()

    @property
    def has_llm_credentials(self) -> bool:
        """True when a Gemini API key is configured."""
        return self.gemini_api_key is not None

    @property
    def extra_allowed_hosts(self) -> list[str]:
        """Host names to accept besides the local ones: ``allowed_hosts`` plus Render's public host name."""
        hosts = [h.strip() for h in (self.allowed_hosts or "").split(",") if h.strip()]
        render_host = os.environ.get("RENDER_EXTERNAL_HOSTNAME", "").strip()
        if render_host and render_host not in hosts:
            hosts.append(render_host)
        return hosts

    def ensure_directories(self) -> None:
        """Create the data directory, and the log directory if a log file is set."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if self.log_file is not None:
            self.log_file.parent.mkdir(parents=True, exist_ok=True)

    def public_summary(self) -> dict[str, Any]:
        """Settings that are safe to display in the UI or logs (no secrets)."""
        return {
            "provider": LLM_PROVIDER,
            "model": self.effective_model,
            "verifier_model": self.effective_verifier_model,
            "effort": {
                "generation": self.generation_effort,
                "verification": self.verification_effort,
                "correction": self.correction_effort,
                "explanation": self.explanation_effort,
            },
            "llm_credentials_configured": self.has_llm_credentials,
            "database": self.masked_database_url,
            "database_dialect": self.database_dialect,
            "demo_database": self.is_demo_database,
            "query_timeout_seconds": self.query_timeout_seconds,
            "max_result_rows": self.max_result_rows,
            "max_correction_attempts": self.max_correction_attempts,
            "few_shot_k": self.few_shot_k,
            "conversation_turns": self.conversation_turns,
            "semantic_verification": self.enable_semantic_verification,
            "hosted": self.hosted,
            "reference_date": self.effective_reference_date.isoformat(),
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings, loaded once and cached."""
    return Settings()


def reload_settings() -> Settings:
    """Re-read the environment and ``.env`` (used by tests and the settings UI)."""
    get_settings.cache_clear()
    return get_settings()
