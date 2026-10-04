"""Tests for SecretValue masking and log redaction filter behavior."""
from __future__ import annotations

import logging

from orchestrator.secrets_handling import (
    RootRedactionFilter,
    SecretValue,
    redact_object,
    register_secret_values,
    reveal_secret_value,
    wrap_secret_mapping,
)


def test_secret_value_masks_string_representation():
    secret = SecretValue("super-secret")
    assert str(secret) == "***"
    assert repr(secret) == "***"
    assert secret.reveal() == "super-secret"


def test_wrap_secret_mapping_and_reveal_roundtrip():
    wrapped = wrap_secret_mapping({"db-password": "p@ssw0rd"})
    assert str(wrapped["db-password"]) == "***"
    assert reveal_secret_value(wrapped["db-password"]) == "p@ssw0rd"


def test_redact_object_masks_registered_values_and_secret_keys():
    register_secret_values("my-token-value")
    payload = {
        "message": "using my-token-value here",
        "DbPassword": "should-hide",
        "nested": {"safe": "ok"},
    }
    redacted = redact_object(payload)
    assert "my-token-value" not in redacted["message"]
    assert redacted["DbPassword"] == "***"
    assert redacted["nested"]["safe"] == "ok"


def test_root_redaction_filter_scrubs_log_record_message():
    register_secret_values("TOPSECRET")
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="token=TOPSECRET",
        args=(),
        exc_info=None,
    )

    redaction_filter = RootRedactionFilter()
    assert redaction_filter.filter(record) is True
    assert "TOPSECRET" not in str(record.msg)
