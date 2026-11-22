"""Tests for utils/helpers.py."""

from __future__ import annotations

import hashlib
import time

import pytest

from utils.helpers import (
    Timer,
    collapse_whitespace,
    escape_markdown,
    escape_html,
    estimate_tokens,
    file_sha256,
    format_duration_ms,
    format_number,
    strip_control_chars,
    truncate_text,
    utc_now_iso,
)


def test_strip_control_chars_removes_invisible_and_control():
    dirty = "Show\u200b emp\u202eloyees\x00\x07 now\ufeff"
    assert strip_control_chars(dirty) == "Show employees now"


def test_strip_control_chars_keeps_whitespace_and_unicode():
    text = "Salário médio\tpor\ndepartamento — 東京"
    assert strip_control_chars(text) == text


def test_collapse_whitespace():
    assert collapse_whitespace("  top   5 \n\n employees\t") == "top 5 employees"


@pytest.mark.parametrize(
    "text, limit, expected",
    [("short", 10, "short"), ("abcdefghij", 6, "abcde…"), ("abc", 1, "a"), ("abc", 3, "abc")],
)
def test_truncate_text(text, limit, expected):
    assert truncate_text(text, limit) == expected


def test_truncate_text_rejects_negative():
    with pytest.raises(ValueError):
        truncate_text("x", -1)


def test_estimate_tokens():
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcde") == 2


def test_escape_html():
    assert escape_html('<script>alert("x")</script>') == "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;"


@pytest.mark.parametrize(
    "ms, expected",
    [(0.4219, "0.42 ms"), (12.34, "12.3 ms"), (1240, "1.24 s"), (125_000, "2m 5.0s")],
)
def test_format_duration_ms(ms, expected):
    assert format_duration_ms(ms) == expected


@pytest.mark.parametrize(
    "value, expected",
    [(1234, "1,234"), (1234.5678, "1,234.57"), (2.0, "2"), (None, "—"), (True, "True")],
)
def test_format_number(value, expected):
    assert format_number(value) == expected


def test_file_sha256(tmp_path):
    path = tmp_path / "f.bin"
    path.write_bytes(b"querybridge" * 1000)
    assert file_sha256(path) == hashlib.sha256(b"querybridge" * 1000).hexdigest()


def test_utc_now_iso_format():
    stamp = utc_now_iso()
    assert stamp.endswith("+00:00") and "T" in stamp


def test_timer_context_manager():
    with Timer() as timer:
        time.sleep(0.01)
    assert timer.elapsed_ms >= 9
    frozen = timer.elapsed_ms
    time.sleep(0.005)
    assert timer.elapsed_ms == frozen  # stopped timers do not keep counting


def test_timer_errors():
    assert Timer().elapsed_ms == 0.0
    with pytest.raises(RuntimeError):
        Timer().stop()


def test_escape_markdown_neutralises_links_and_images():
    text = "![x](https://evil.example/?q=secret) [click](http://x) # Title *bold*"
    escaped = escape_markdown(text)
    assert "![" not in escaped and "](" not in escaped and "\\#" in escaped
    assert escape_markdown("plain words 123") == "plain words 123"
