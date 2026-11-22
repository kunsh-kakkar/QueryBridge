"""
Logging for QueryBridge with built-in secret redaction.

Every handler created here does two things:

* applies ``RedactingFilter``, which redacts the rendered message, and
* uses ``RedactingFormatter``, which redacts the final output line, including
  exception tracebacks.

So API keys, bearer tokens, passwords in ``key=value`` pairs, credentials
embedded in database URLs, and any value passed to ``register_secret()`` (for
example a password typed into the connection form) never reach the console or
a log file.

The ``querybridge`` logger does not propagate to the root logger, so
third-party handlers (such as Streamlit's) never see unredacted records.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import IO

ROOT_LOGGER_NAME = "querybridge"
REDACTED = "***REDACTED***"
LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# Order matters: URL credentials are handled before generic key=value pairs.
_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Google API keys (Gemini): AIza followed by 35 characters
    (re.compile(r"AIza[0-9A-Za-z_\-]{20,}"), f"AIza{REDACTED}"),
    # Credentials embedded in URLs: scheme://user:password@host
    (
        re.compile(r"(?P<prefix>\b[a-zA-Z][a-zA-Z0-9+.\-]*://[^\s:/@]*:)[^\s@/]+(?=@)"),
        r"\g<prefix>" + REDACTED,
    ),
    # Bearer tokens (Authorization headers)
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/\-]{8,}=*"), f"Bearer {REDACTED}"),
    # key=value / key: value pairs whose key looks secret
    (
        re.compile(
            r"(?i)(?P<key>\b(?:x[-_]api[-_]key|api[-_]?key|(?:auth[-_]?|access[-_]?|refresh[-_]?)?token"
            r"|secret(?:[-_]?key)?|client[-_]?secret|password|passwd|pwd)\b)"
            r"(?P<sep>[\"']?\s*[=:]\s*[\"']?)[^\s\"',;&]+"
        ),
        r"\g<key>\g<sep>" + REDACTED,
    ),
)

_MIN_SECRET_LENGTH = 4
_registered_secrets: set[str] = set()
_lock = threading.Lock()
_configured = False


def register_secret(value: str | None) -> None:
    """Redact this exact value from all future log output.

    Values shorter than 4 characters are ignored: redacting them would mangle
    ordinary text.
    """
    if value and len(value) >= _MIN_SECRET_LENGTH:
        with _lock:
            _registered_secrets.add(value)


def clear_registered_secrets() -> None:
    """Forget every registered secret (used by tests and on disconnect)."""
    with _lock:
        _registered_secrets.clear()


def redact(text: str) -> str:
    """Return ``text`` with every known secret pattern replaced."""
    if not text:
        return text
    with _lock:
        secrets = sorted(_registered_secrets, key=len, reverse=True)
    for secret in secrets:
        text = text.replace(secret, REDACTED)
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class RedactingFilter(logging.Filter):
    """Render the message with its arguments, redact it, and freeze the result."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # malformed %-format: keep the raw template
            message = str(record.msg)
        record.msg = redact(message)
        record.args = None
        return True


class RedactingFormatter(logging.Formatter):
    """Redact the fully formatted output, tracebacks included."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def _coerce_level(level: str | int) -> int:
    if isinstance(level, int):
        return level
    mapping = logging.getLevelNamesMapping()
    try:
        return mapping[level.strip().upper()]
    except KeyError:
        raise ValueError(f"Unknown log level: {level!r}") from None


def _make_handler(handler: logging.Handler) -> logging.Handler:
    handler.setFormatter(RedactingFormatter(LOG_FORMAT, DATE_FORMAT))
    handler.addFilter(RedactingFilter())
    return handler


def setup_logging(
    level: str | int = "INFO",
    log_file: str | Path | None = None,
    *,
    stream: IO[str] | None = None,
    force: bool = False,
) -> logging.Logger:
    """Configure the ``querybridge`` logger. Safe to call more than once.

    Args:
        level: Log level name or number.
        log_file: Optional path for a rotating log file (1 MB x 3 backups).
        stream: Console stream (defaults to stderr; tests pass a StringIO).
        force: Replace existing handlers even if logging is already configured.
    """
    global _configured
    logger = logging.getLogger(ROOT_LOGGER_NAME)
    with _lock:
        if _configured and not force:
            return logger
        for old in list(logger.handlers):
            logger.removeHandler(old)
            old.close()
        logger.addHandler(_make_handler(logging.StreamHandler(stream or sys.stderr)))
        if log_file:
            path = Path(log_file)
            path.parent.mkdir(parents=True, exist_ok=True)
            logger.addHandler(
                _make_handler(
                    RotatingFileHandler(path, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
                )
            )
        logger.setLevel(_coerce_level(level))
        logger.propagate = False
        _configured = True
    return logger


def get_logger(name: str | None = None) -> logging.Logger:
    """Return a logger under the ``querybridge`` namespace.

    ``get_logger(__name__)`` in ``backend/sql_validator.py`` returns
    ``querybridge.backend.sql_validator``. If logging is not configured yet,
    it is set up from ``QB_LOG_LEVEL`` (default INFO).
    """
    if not _configured:
        env_level = os.environ.get("QB_LOG_LEVEL", "INFO")
        try:
            setup_logging(env_level)
        except ValueError:
            setup_logging("INFO")
    if not name or name == ROOT_LOGGER_NAME:
        return logging.getLogger(ROOT_LOGGER_NAME)
    if name.startswith(ROOT_LOGGER_NAME + "."):
        return logging.getLogger(name)
    return logging.getLogger(f"{ROOT_LOGGER_NAME}.{name}")
