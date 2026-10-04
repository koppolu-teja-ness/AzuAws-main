"""Tests for post-deploy verification helpers in orchestrator/agents.py.

These are pure/unit tests with mocked AWS clients; no live AWS calls.
"""
from __future__ import annotations

import dataclasses

import boto3
import pytest

from orchestrator import cli_ui
from orchestrator.agents import (
    _stack_resources_by_type,
    _verify_lambda_invocations,
    _verify_vpc_reachability,
    make_agent6_verify,
)
from orchestrator.config import Config


def _test_config(**overrides) -> Config:
    base = Config.from_env()
    return dataclasses.replace(base, **overrides) if overrides else base


class _FakeCfn:
    def describe_stack_resources(self, StackName: str):  # noqa: N803 - boto3-style arg name
        assert StackName == "test-stack"
        return {
            "StackResources": [
                {
                    "ResourceType": "AWS::EC2::VPC",
                    "LogicalResourceId": "VpcA",
                    "PhysicalResourceId": "vpc-123",
                },
                {
                    "ResourceType": "AWS::Lambda::Function",
                    "LogicalResourceId": "FnA",
                    "PhysicalResourceId": "fn-a",
                },
            ]
        }


def test_stack_resources_by_type_groups_logical_and_physical_ids():
    grouped = _stack_resources_by_type(_FakeCfn(), "test-stack")
    assert grouped["AWS::EC2::VPC"][0]["logical_id"] == "VpcA"
    assert grouped["AWS::EC2::VPC"][0]["physical_id"] == "vpc-123"
    assert grouped["AWS::Lambda::Function"][0]["logical_id"] == "FnA"


def test_verify_vpc_reachability_passes_with_active_default_route(monkeypatch):
    class _FakeEc2:
        def describe_route_tables(self, Filters):  # noqa: N803 - boto3-style arg name
            assert Filters[0]["Name"] == "vpc-id"
            return {
                "RouteTables": [
                    {
                        "Routes": [
                            {
                                "DestinationCidrBlock": "0.0.0.0/0",
                                "State": "active",
                                "GatewayId": "igw-1",
                            }
                        ]
                    }
                ]
            }

    monkeypatch.setattr(boto3, "client", lambda *a, **k: _FakeEc2())

    checks = _verify_vpc_reachability(
        _test_config(aws_region="us-east-1"),
        {"AWS::EC2::VPC": [{"logical_id": "VpcA", "physical_id": "vpc-123"}]},
    )
    assert len(checks) == 1
    assert checks[0]["reachable"] is True
    assert checks[0]["default_route_target"] == "igw-1"


def test_verify_vpc_reachability_fails_without_default_route(monkeypatch):
    class _FakeEc2:
        def describe_route_tables(self, Filters):  # noqa: N803 - boto3-style arg name
            return {"RouteTables": [{"Routes": [{"DestinationCidrBlock": "10.0.0.0/16"}]}]}

    monkeypatch.setattr(boto3, "client", lambda *a, **k: _FakeEc2())

    checks = _verify_vpc_reachability(
        _test_config(aws_region="us-east-1"),
        {"AWS::EC2::VPC": [{"logical_id": "VpcA", "physical_id": "vpc-123"}]},
    )
    assert len(checks) == 1
    assert checks[0]["reachable"] is False


def test_verify_lambda_invocations_handles_success_and_failure(monkeypatch):
    class _FakeLambda:
        def invoke(self, FunctionName, InvocationType, Payload):  # noqa: N803 - boto3-style arg names
            assert InvocationType == "RequestResponse"
            assert Payload == b"{}"
            if FunctionName == "fn-a":
                return {"StatusCode": 200}
            raise RuntimeError("invoke failed")

    monkeypatch.setattr(boto3, "client", lambda *a, **k: _FakeLambda())

    checks = _verify_lambda_invocations(
        _test_config(aws_region="us-east-1"),
        {
            "AWS::Lambda::Function": [
                {"logical_id": "FnA", "physical_id": "fn-a"},
                {"logical_id": "FnB", "physical_id": "fn-b"},
            ]
        },
    )
    assert len(checks) == 2
    by_id = {c["logical_id"]: c for c in checks}
    assert by_id["FnA"]["success"] is True
    assert by_id["FnB"]["success"] is False
    assert "invoke failed" in by_id["FnB"]["error"]


def test_agent6_verify_node_reports_warning_summary_on_failed_checks(monkeypatch):
    gate = make_agent6_verify(_test_config(aws_region="us-east-1"))
    captured: dict[str, str] = {}

    monkeypatch.setattr(cli_ui, "agent_phase", lambda *a, **k: None)

    def _capture_result(agent: str, status: str, message: str):
        captured["agent"] = agent
        captured["status"] = status
        captured["message"] = message

    monkeypatch.setattr(cli_ui, "agent_result", _capture_result)

    monkeypatch.setattr(
        "orchestrator.agents._verify_post_deploy",
        lambda **kwargs: {
            "secrets_checked": [{"logical_id": "S1", "exists": True}],
            "vpc_reachability": [{"logical_id": "V1", "reachable": False, "error": "no route"}],
            "lambda_invocations": [
                {"logical_id": "F1", "success": True, "status_code": 200},
                {"logical_id": "F2", "success": False, "error": "invoke failed"},
            ],
        },
    )

    state = {
        "deploy_result": {"stack_name": "test-stack", "status": "CREATE_COMPLETE"},
        "migration_plan": object(),
        "param_values": {"X": "Y"},
    }
    result = gate(state)

    assert captured["agent"] == "Verify"
    assert captured["status"] == "warning"
    assert "4 smoke check(s); 2 issue(s)" in captured["message"]
    assert result["agent_log"][0]["status"] == "warning"
    assert "4 post-deploy smoke check(s); 2 issue(s)" in result["agent_log"][0]["message"]
    assert "verify_result" in result


def test_agent6_verify_node_skips_when_deploy_not_successful(monkeypatch):
    gate = make_agent6_verify(_test_config())
    monkeypatch.setattr(cli_ui, "agent_phase", lambda *a, **k: pytest.fail("should not phase when skipped"))
    monkeypatch.setattr(cli_ui, "agent_result", lambda *a, **k: pytest.fail("should not report when skipped"))

    result = gate({"deploy_result": {"stack_name": "test-stack", "status": "FAILED"}})
    assert "verify_result" not in result
    assert result["agent_log"][0]["status"] == "warning"
    assert "skipped" in result["agent_log"][0]["message"].lower()
