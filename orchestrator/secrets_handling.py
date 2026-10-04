"""Secret-safety utilities shared across the migration pipeline.

This module provides three protections:
1) SecretValue wrapper for in-memory state values that should never stringify
   to plaintext.
2) A shared registry of known secret literals so accidental logging can be
   redacted by exact-value replacement.
3) A root-logger filter that redacts sensitive strings before records are
   emitted by handlers.
"""
from __future__ import annotations

import logging
import re
from typing import Any

_REDACTED = "***"
_SECRET_KEY_RE = re.compile(r"password|secret|token|key|connectionstring", re.IGNORECASE)

_registered_secret_values: set[str] = set()
_redaction_filter: logging.Filter | None = None


class SecretValue:
    """Wrapper whose string representation is always masked.

    Use .reveal() only at the API boundary that truly needs the raw value.
    """

    __slots__ = ("_value",)

    def __init__(self, value: str):
        self._value = str(value)
        register_secret_values(self._value)

    def reveal(self) -> str:
        return self._value

    def __str__(self) -> str:
        return _REDACTED

    def __repr__(self) -> str:
        return _REDACTED


def is_secret_value(value: Any) -> bool:
    return isinstance(value, SecretValue)


def reveal_secret_value(value: str | SecretValue) -> str:
    if isinstance(value, SecretValue):
        return value.reveal()
    return str(value)


def wrap_secret_mapping(values: dict[str, str]) -> dict[str, SecretValue]:
    return {name: SecretValue(value) for name, value in values.items()}


def register_secret_values(*values: Any) -> None:
    """Register secret literals for redaction in both logging and tracing."""
    normalized: list[str] = []
    for value in values:
        if value is None:
            continue
        raw = value.reveal() if isinstance(value, SecretValue) else str(value)
        if not raw:
            continue
        _registered_secret_values.add(raw)
        normalized.append(raw)

    if not normalized:
        return

    # Keep observability's value-redaction registry in sync when available.
    try:
        from .observability import register_secret_values as register_observability_secrets

        register_observability_secrets(*normalized)
    except Exception:
        # Redaction in this module should never fail closed.
        pass


def redact_string(value: str) -> str:
    redacted = value
    # Replace longer values first so overlapping secrets do not partially reveal.
    for secret in sorted(_registered_secret_values, key=len, reverse=True):
        if secret and secret in redacted:
            redacted = redacted.replace(secret, _REDACTED)
    return redacted


def redact_object(value: Any) -> Any:
    if isinstance(value, SecretValue):
        return _REDACTED
    if isinstance(value, str):
        return redact_string(value)
    if isinstance(value, dict):
        cleaned: dict[Any, Any] = {}
        for k, v in value.items():
            if _SECRET_KEY_RE.search(str(k)):
                cleaned[k] = _REDACTED
            else:
                cleaned[k] = redact_object(v)
        return cleaned
    if isinstance(value, list):
        return [redact_object(v) for v in value]
    if isinstance(value, tuple):
        return tuple(redact_object(v) for v in value)
    return value


class RootRedactionFilter(logging.Filter):
    """Best-effort log record redaction filter.

    This mutates record.msg/record.args in place so handlers emit sanitized
    text even if call sites accidentally pass raw secret literals.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = redact_object(record.msg)
            if isinstance(record.args, tuple):
                record.args = tuple(redact_object(arg) for arg in record.args)
            elif isinstance(record.args, dict):
                record.args = {k: redact_object(v) for k, v in record.args.items()}
            elif record.args:
                record.args = redact_object(record.args)
        except Exception:
            # Never block logging.
            pass
        return True


def install_root_redaction_filter() -> None:
    """Install a singleton redaction filter on the root logger and handlers."""
    global _redaction_filter
    if _redaction_filter is not None:
        return

    redaction_filter = RootRedactionFilter()
    root_logger = logging.getLogger()
    root_logger.addFilter(redaction_filter)
    for handler in root_logger.handlers:
        handler.addFilter(redaction_filter)
    _redaction_filter = redaction_filter
