"""Tests for orchestrator/observability.py -- tracing must be a true no-op
when disabled, and redact() must never let a secret reach an outbound payload.
"""
from __future__ import annotations

import types

import pytest

from orchestrator import observability as obs


@pytest.fixture(autouse=True)
def _isolated_observability_state(monkeypatch):
    """Each test gets a clean slate: no ambient LANGSMITH_* env vars, no
    registered secrets, no cached client."""
    for var in ("LANGSMITH_TRACING", "LANGSMITH_API_KEY", "LANGSMITH_PROJECT", "LANGSMITH_ENDPOINT"):
        monkeypatch.delenv(var, raising=False)
    obs._registered_secret_values.clear()
    obs._client = None
    yield
    obs._registered_secret_values.clear()
    obs._client = None


# ---------------------------------------------------------------------------
# Tracing-off is a true no-op
# ---------------------------------------------------------------------------
def test_tracing_disabled_by_default():
    assert obs.is_tracing_enabled() is False


def test_tracing_requires_both_flag_and_api_key(monkeypatch):
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    # No API key set -- must still be disabled.
    assert obs.is_tracing_enabled() is False

    monkeypatch.setenv("LANGSMITH_API_KEY", "fake-key")
    assert obs.is_tracing_enabled() is True

    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    assert obs.is_tracing_enabled() is False


def test_traceable_is_noop_and_never_touches_client(monkeypatch):
    monkeypatch.setattr(obs, "get_client", lambda: (_ for _ in ()).throw(AssertionError("get_client must not be called")))

    @obs.traceable(run_type="llm")
    def add(a, b):
        return a + b

    assert add(1, 2) == 3
    # Must also accept (and drop) langsmith_extra like the real decorator would.
    assert add(1, 2, langsmith_extra={"metadata": {"x": 1}}) == 3


def test_trace_span_is_noop_when_disabled():
    with obs.trace_span("thing", run_type="tool", inputs={"a": 1}) as span:
        span.end(outputs={"ok": True})  # must not raise
        assert span.id is None


def test_build_run_config_empty_when_disabled():
    fake_config = types.SimpleNamespace(aws_region="us-east-1", bedrock_model_id="m", rag_enabled=True, rag_top_k=4, max_fix_attempts=2)
    assert obs.build_run_config(run_id="abc123", source_file="x.bicep", input_mode="bicep_file", config=fake_config) == {}


def test_attach_feedback_and_trace_url_are_noop_when_disabled():
    obs.attach_feedback("plan_approved", score=1)  # must not raise
    assert obs.get_trace_url() is None


def test_tracing_scope_is_noop_when_disabled():
    with obs.tracing_scope(project_name="p", tags=["t"]):
        pass  # must not raise, must not import langchain_core.tracers.context


# ---------------------------------------------------------------------------
# redact()
# ---------------------------------------------------------------------------
def test_redact_masks_secret_shaped_keys():
    payload = {"DbPassword": "hunter2", "nested": {"ApiToken": "tok-123", "safe": "ok"}}
    redacted = obs.redact(payload)
    assert redacted["DbPassword"] == obs._REDACTED
    assert redacted["nested"]["ApiToken"] == obs._REDACTED
    assert redacted["nested"]["safe"] == "ok"


def test_redact_masks_registered_exact_values_anywhere_in_text():
    obs.register_secret_values("sUp3rSecretValue")
    payload = {"description": "the value is sUp3rSecretValue embedded in prose"}
    redacted = obs.redact(payload)
    assert "sUp3rSecretValue" not in redacted["description"]
    assert obs._REDACTED in redacted["description"]


def test_redact_masks_azure_guid_and_aws_account_id():
    payload = {
        "arm_id": "/subscriptions/11111111-2222-3333-4444-555555555555/resourceGroups/rg",
        "stack_id": "arn:aws:cloudformation:us-east-1:123456789012:stack/foo/bar",
    }
    redacted = obs.redact(payload)
    assert "11111111-2222-3333-4444-555555555555" not in redacted["arm_id"]
    assert "123456789012" not in redacted["stack_id"]


def test_redact_handles_dataclasses_and_lists():
    import dataclasses

    @dataclasses.dataclass
    class Param:
        name: str
        literal_value: str

    payload = {"params": [Param(name="DbPassword", literal_value="literal-secret")]}
    redacted = obs.redact(payload)
    assert redacted["params"][0]["literal_value"] == "literal-secret"  # key name here is safe
    assert redacted["params"][0]["name"] == "DbPassword"


# ---------------------------------------------------------------------------
# A mocked client must never receive a planted secret, even via the hide hooks
# used to configure the real Client.
# ---------------------------------------------------------------------------
def test_mocked_client_never_receives_planted_secret(monkeypatch):
    planted_secret = "PLANTED-FAKE-SECRET-VALUE"
    obs.register_secret_values(planted_secret)

    captured_payloads = []

    class FakeClient:
        def __init__(self, **kwargs):
            self.hide_inputs = kwargs["hide_inputs"]
            self.hide_outputs = kwargs["hide_outputs"]

        def create_run(self, inputs, outputs):
            captured_payloads.append(self.hide_inputs(inputs))
            captured_payloads.append(self.hide_outputs(outputs))

    fake_langsmith_module = types.SimpleNamespace(Client=FakeClient)
    monkeypatch.setitem(__import__("sys").modules, "langsmith", fake_langsmith_module)
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "fake-key")

    client = obs.get_client()
    client.create_run(
        inputs={"prompt": f"please use {planted_secret} to authenticate"},
        outputs={"DbPassword": planted_secret},
    )

    for payload in captured_payloads:
        serialized = str(payload)
        assert planted_secret not in serialized


def test_build_run_config_keys_when_enabled(monkeypatch):
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "fake-key")
    fake_config = types.SimpleNamespace(aws_region="us-east-1", bedrock_model_id="m", rag_enabled=True, rag_top_k=4, max_fix_attempts=2)

    run_config = obs.build_run_config(run_id="abc123", source_file="x.bicep", input_mode="bicep_file", config=fake_config)

    assert run_config["run_name"] == "migration-abc123"
    assert "bicep-to-cfn-migration" in run_config["tags"]
    expected_metadata_keys = {
        "migration_run_id", "source_file", "input_mode", "aws_region", "bedrock_model_id",
        "rag_enabled", "rag_top_k", "max_fix_attempts", "git_sha", "prompt_version",
    }
    assert expected_metadata_keys == set(run_config["metadata"].keys())
