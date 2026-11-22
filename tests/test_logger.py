"""Tests for utils/logger.py: secret redaction and logger configuration."""

from __future__ import annotations

import io
import logging

import pytest

from utils import logger as qb_logger
from utils.logger import REDACTED, get_logger, redact, register_secret, setup_logging


@pytest.fixture(autouse=True)
def _clean_secrets():
    qb_logger.clear_registered_secrets()
    yield
    qb_logger.clear_registered_secrets()


@pytest.fixture
def captured() -> io.StringIO:
    stream = io.StringIO()
    setup_logging("DEBUG", stream=stream, force=True)
    yield stream
    setup_logging("INFO", force=True)


@pytest.mark.parametrize(
    "text, secret",
    [
        ("gemini key AIza" "SyA1b2C3d4E5f6G7h8I9j0K-LmNoPqRsTuV", "AIza" "SyA1b2C3d4E5f6G7h8I9j0K-LmNoPqRsTuV"),
        ("x-goog-api-key: AIza" "SyQwErTyUiOp1234567890asdfghjklzx", "AIza" "SyQwErTyUiOp1234567890asdfghjklzx"),
        ("connect postgresql+psycopg://reader:Pa55word@db:5432/x", "Pa55word"),
        ("mysql+pymysql://root:hunter2@localhost/db failed", "hunter2"),
        ("Authorization: Bearer abcdefghijklmnop123", "abcdefghijklmnop123"),
        ("password=supersecret host=db", "supersecret"),
        ('{"api_key": "k-123456789"}', "k-123456789"),
        ("PWD: letmein99", "letmein99"),
        ("token=abc123456789", "abc123456789"),
        ("refresh_token: r-98765432", "r-98765432"),
        ("secret='s3cr3tvalue'", "s3cr3tvalue"),
    ],
)
def test_redact_patterns(text, secret):
    out = redact(text)
    assert secret not in out
    assert REDACTED in out


@pytest.mark.parametrize(
    "text",
    [
        "SELECT employee_name, salary FROM employees WHERE salary > 50000",
        "sqlite:///data/querybridge_demo.db",
        "http://localhost:8501/dashboard",
        "the password_hint column is missing",
        "usage max_tokens=16000 input_tokens=812 output_tokens: 412",
        "",
    ],
)
def test_redact_leaves_normal_text_alone(text):
    assert redact(text) == text


def test_registered_secret_is_redacted():
    register_secret("Corp#DbPass!2026")
    assert "Corp#DbPass!2026" not in redact("login failed for Corp#DbPass!2026 at host")


def test_short_secrets_are_not_registered():
    register_secret("ab")
    assert redact("about absolute") == "about absolute"


def test_log_output_is_redacted(captured):
    log = get_logger("tests.redaction")
    log.info("calling API with key=%s", "AIza" "SySHOULDNOTAPPEAR999abcdefghijk")
    log.warning("db url %s", "postgresql://u:VerySecret1@h/db")
    output = captured.getvalue()
    assert "SHOULDNOTAPPEAR999" not in output
    assert "VerySecret1" not in output
    assert output.count(REDACTED) >= 2


def test_traceback_is_redacted(captured):
    register_secret("TracebackSecret42")
    log = get_logger("tests.traceback")
    try:
        raise RuntimeError("connection refused for password TracebackSecret42")
    except RuntimeError:
        log.exception("query failed")
    output = captured.getvalue()
    assert "Traceback" in output
    assert "TracebackSecret42" not in output


def test_logger_namespacing_and_no_propagation():
    assert get_logger("backend.sql_validator").name == "querybridge.backend.sql_validator"
    assert get_logger("querybridge.x").name == "querybridge.x"
    assert get_logger().name == "querybridge"
    assert logging.getLogger("querybridge").propagate is False


def test_log_file_handler(tmp_path):
    log_file = tmp_path / "logs" / "qb.log"
    setup_logging("INFO", log_file=log_file, stream=io.StringIO(), force=True)
    try:
        get_logger("tests.file").info("token=abc123456789 stored")
        for handler in logging.getLogger("querybridge").handlers:
            handler.flush()
        content = log_file.read_text(encoding="utf-8")
        assert "stored" in content and "abc123456789" not in content
    finally:
        setup_logging("INFO", force=True)


def test_invalid_level_rejected():
    with pytest.raises(ValueError):
        setup_logging("LOUD", stream=io.StringIO(), force=True)
    setup_logging("INFO", force=True)
