"""Small, dependency-free helpers used across QueryBridge."""

from __future__ import annotations

import hashlib
import html
import math
import re
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from types import TracebackType

# Invisible characters that can hide or reorder text ("Trojan Source" style):
# zero-width characters, bidi embeddings/overrides/isolates, and the BOM.
_INVISIBLE_CHARS = frozenset(
    [chr(c) for c in range(0x200B, 0x2010)]
    + [chr(c) for c in range(0x202A, 0x202F)]
    + [chr(c) for c in range(0x2060, 0x2065)]
    + [chr(c) for c in range(0x2066, 0x206A)]
    + ["﻿"]
)
_ALLOWED_CONTROL = frozenset("\t\n\r")


def strip_control_chars(text: str) -> str:
    """Remove control characters and invisible Unicode formatting characters.

    Tabs and newlines are kept. Visible characters, including non-Latin
    scripts, are never touched.
    """
    return "".join(
        ch
        for ch in text
        if ch in _ALLOWED_CONTROL
        or (unicodedata.category(ch) != "Cc" and ch not in _INVISIBLE_CHARS)
    )


def collapse_whitespace(text: str) -> str:
    """Collapse every run of whitespace (newlines included) into one space."""
    return " ".join(text.split())


def truncate_text(text: str, max_chars: int, suffix: str = "…") -> str:
    """Shorten ``text`` to at most ``max_chars`` characters, ending in ``suffix``."""
    if max_chars < 0:
        raise ValueError("max_chars must be >= 0")
    if len(text) <= max_chars:
        return text
    if max_chars <= len(suffix):
        return text[:max_chars]
    return text[: max_chars - len(suffix)].rstrip() + suffix


def estimate_tokens(text: str) -> int:
    """Rough token estimate (about 4 characters per token for English text).

    Always label the result as an estimate. Exact counts come from the API's
    ``usage`` field.
    """
    return math.ceil(len(text) / 4) if text else 0


def escape_html(value: object) -> str:
    """HTML-escape any value before it goes into custom HTML (prevents XSS)."""
    return html.escape(str(value), quote=True)


_MARKDOWN_SPECIAL = re.compile(r"([\\`*_{}\[\]()#+\-.!|<>~$])")


def escape_markdown(value: object) -> str:
    """Escape Markdown so untrusted text (model or database output) renders literally.

    This stops model or database content from injecting links, images (which
    could exfiltrate data through the URL), headings or tables into the UI.
    """
    return _MARKDOWN_SPECIAL.sub(r"\\\1", str(value))


def format_duration_ms(ms: float) -> str:
    """Human-readable duration: '0.42 ms', '12.3 ms', '1.24 s', '2m 5.0s'."""
    if ms < 0:
        raise ValueError("duration must be >= 0")
    if ms < 1:
        return f"{ms:.2f} ms"
    if ms < 1000:
        return f"{ms:.1f} ms"
    seconds = ms / 1000
    if seconds < 60:
        return f"{seconds:.2f} s"
    minutes, rest = divmod(seconds, 60)
    return f"{int(minutes)}m {rest:.1f}s"


def format_number(value: int | float | None, decimals: int = 2) -> str:
    """Thousands-separated number; '—' for None."""
    if value is None:
        return "—"
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int) or (isinstance(value, float) and value.is_integer() and abs(value) < 1e15):
        return f"{int(value):,}"
    return f"{value:,.{decimals}f}"


def file_sha256(path: str | Path, chunk_size: int = 1 << 20) -> str:
    """SHA-256 of a file, used to prove the demo DB was not modified."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def utc_now_iso() -> str:
    """Current UTC time as ISO-8601 with second precision."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Timer:
    """High-resolution timer, usable as a context manager.

    >>> with Timer() as t:
    ...     pass
    >>> t.elapsed_ms >= 0
    True
    """

    def __init__(self) -> None:
        self._start: float | None = None
        self._end: float | None = None

    def start(self) -> "Timer":
        self._start = time.perf_counter()
        self._end = None
        return self

    def stop(self) -> float:
        if self._start is None:
            raise RuntimeError("Timer was never started")
        self._end = time.perf_counter()
        return self.elapsed_ms

    @property
    def elapsed_ms(self) -> float:
        """Elapsed milliseconds (still counting while the timer is running)."""
        if self._start is None:
            return 0.0
        end = self._end if self._end is not None else time.perf_counter()
        return (end - self._start) * 1000

    def __enter__(self) -> "Timer":
        return self.start()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.stop()
