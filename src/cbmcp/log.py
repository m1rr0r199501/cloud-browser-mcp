"""Logging with credential redaction.

Two rules this module exists to enforce:

1. stdout belongs to the MCP protocol. All logs go to stderr.
2. CDP URLs, API keys, proxy passwords, cookies and auth headers must never
   reach a log line. The redactor is applied to the formatted message, so it
   also catches secrets that arrive inside exception strings from httpx or
   Playwright -- which is where they usually leak from.
"""

from __future__ import annotations

import logging
import re
import sys
from typing import Any

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bbu_[A-Za-z0-9_\-]{8,}"), "bu_***"),
    (re.compile(r"(wss?://[^\s\"']*)"), "<cdp-url-redacted>"),
    (re.compile(r"(?i)(x-browser-use-api-key\s*[:=]\s*)\S+"), r"\1***"),
    (re.compile(r"(?i)(authorization\s*[:=]\s*)\S+"), r"\1***"),
    (re.compile(r"(?i)(\"?(?:api[_-]?key|token|password|secret|cookie)\"?\s*[:=]\s*\"?)([^\s\",}]+)"), r"\1***"),
    (re.compile(r"(?i)(//[^/@\s:]+:)[^@\s/]+(@)"), r"\1***\2"),
]


def redact(text: str) -> str:
    out = text
    for pattern, repl in _PATTERNS:
        out = pattern.sub(repl, out)
    return out


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


_configured = False


def setup_logging(level: str = "INFO") -> None:
    global _configured
    if _configured:
        logging.getLogger("cbmcp").setLevel(level.upper())
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        RedactingFormatter("%(asctime)s %(levelname)-5s %(name)s | %(message)s", "%H:%M:%S")
    )
    root = logging.getLogger("cbmcp")
    root.setLevel(level.upper())
    root.addHandler(handler)
    root.propagate = False
    # Playwright and httpx are chatty and their messages can contain CDP URLs.
    for noisy in ("httpx", "httpcore", "websockets", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _configured = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"cbmcp.{name}")


def safe_extra(**kwargs: Any) -> str:
    parts = [f"{k}={v}" for k, v in kwargs.items() if v is not None]
    return redact(" ".join(parts))
