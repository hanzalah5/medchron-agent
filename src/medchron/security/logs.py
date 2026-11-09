"""PHI-safe structured logging.

The primary control is *not logging content*: the code logs IDs, counts,
hashes and timings, never record text, prompts or model output. This filter
is defense in depth. It masks patterns that look like identifiers (dates of
birth, MRN-like codes, phone numbers, emails, SSN-like numbers) in case
content slips into a message or an exception.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[SSN]"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "[EMAIL]"),
    (re.compile(r"\(?\b\d{3}\)?[-. ]\d{3}[-. ]\d{4}\b"), "[PHONE]"),
    (re.compile(r"\bMRN[:#\s]*[A-Z]?\d{5,}\b", re.I), "[MRN]"),
    (re.compile(r"\bDOB[:\s]*[\w/,. -]{6,20}\d{2,4}", re.I), "[DOB]"),
    (re.compile(r"\b[A-Z]\d{7}\b"), "[ID]"),
]


def scrub(text: str) -> str:
    for pattern, repl in PATTERNS:
        text = pattern.sub(repl, text)
    return text


class ScrubFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = scrub(record.getMessage())
        record.args = None
        if record.exc_text:
            record.exc_text = scrub(record.exc_text)
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "at": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key in ("llm", "request_id", "matter_id", "job_id", "trace_id"):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        if record.exc_info:
            payload["exc"] = scrub(self.formatException(record.exc_info))
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    handler.addFilter(ScrubFilter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
