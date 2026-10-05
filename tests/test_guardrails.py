"""Tests for orchestrator/guardrails.py -- custom secret/IAM/network checks
(pure functions over a parsed template dict, fully offline) plus checkov
integration: one live run against a real adversarial template (checkov is a
required dependency, so this is allowed to actually invoke it) and a mocked
subprocess test for the error-handling path (missing/broken installation).
"""
from __future__ import annotations

import dataclasses
import json
import subprocess
from pathlib import Path

import pytest
import yaml

from orchestrator import cli_ui
from orchestrator.agents import make_guardrail_scan_gate
from orchestrator.config import Config
from orchestrator.guardrails import (
    BLOCKING_SEVERITIES,
    Finding,
    run_checkov,
    scan_hardcoded_secrets,
    scan_iam_wildcards,
    scan_open_network,
    scan_template,
)

ADVERSARIAL_TEMPLATE = yaml.safe_load("""
AWSTemplateFormatVersion: '2010-09-09'
Parameters:
  DbPassword:
    Type: String
    Default: SuperSecret123
Resources:
  MyRole:
    Type: AWS::IAM::Role
    Properties:
      AssumeRolePolicyDocument:
        Statement:
        - Effect: Allow
          Principal: "*"
          Action: "*"
      Policies:
      - PolicyName: full
        PolicyDocument:
          Statement:
          - Effect: Allow
            Action: "*"
            Resource: "*"
  MySG:
    Type: AWS::EC2::SecurityGroup
    Properties:
      GroupDescription: bad
      SecurityGroupIngress:
      - IpProtocol: tcp
        FromPort: 22
        ToPort: 22
        CidrIp: 0.0.0.0/0
  MyDb:
    Type: AWS::RDS::DBInstance
    Properties:
      MasterUserPassword: hardcoded-literal-pw
""")

CLEAN_TEMPLATE = yaml.safe_load("""
AWSTemplateFormatVersion: '2010-09-09'
Parameters:
  DbPassword:
    Type: String
    NoEcho: true
Resources:
  MyRole:
    Type: AWS::IAM::Role
    Properties:
      AssumeRolePolicyDocument:
        Statement:
        - Effect: Allow
          Principal:
            Service: ec2.amazonaws.com
          Action: sts:AssumeRole
  MyDb:
    Type: AWS::RDS::DBInstance
    Properties:
      MasterUserPassword:
        Ref: DbPassword
""")


def test_scan_hardcoded_secrets_flags_literal_property_and_param():
    findings = scan_hardcoded_secrets(ADVERSARIAL_TEMPLATE)
    check_ids = {f.check_id for f in findings}
    assert "CUSTOM_HARDCODED_SECRET" in check_ids
    assert "CUSTOM_SECRET_PARAM_NO_NOECHO" in check_ids
    assert all(f.severity == "HIGH" for f in findings)


def test_scan_hardcoded_secrets_clean_template_has_no_findings():
    assert scan_hardcoded_secrets(CLEAN_TEMPLATE) == []


def test_scan_iam_wildcards_flags_full_admin_and_wildcard_principal():
    findings = scan_iam_wildcards(ADVERSARIAL_TEMPLATE)
    check_ids = {f.check_id for f in findings}
    assert "CUSTOM_IAM_FULL_ADMIN" in check_ids
    assert "CUSTOM_IAM_WILDCARD_PRINCIPAL" in check_ids
    assert any(f.severity == "CRITICAL" for f in findings)


def test_scan_iam_wildcards_clean_template_has_no_findings():
    assert scan_iam_wildcards(CLEAN_TEMPLATE) == []


def test_scan_open_network_flags_ssh_open_to_world():
    findings = scan_open_network(ADVERSARIAL_TEMPLATE)
    assert len(findings) == 1
    assert findings[0].check_id == "CUSTOM_OPEN_INGRESS"
    assert findings[0].severity == "CRITICAL"
    assert "SSH" in findings[0].message


def test_scan_open_network_clean_template_has_no_findings():
    assert scan_open_network(CLEAN_TEMPLATE) == []


def test_run_checkov_handles_missing_executable(monkeypatch):
    def _raise(*args, **kwargs):
        raise OSError("no such file")

    monkeypatch.setattr(subprocess, "run", _raise)
    ran, findings, error = run_checkov(Path("irrelevant.yaml"))
    assert ran is False
    assert findings == []
    assert "no such file" in error


def test_run_checkov_handles_unparseable_output(monkeypatch):
    class _FakeResult:
        stdout = "not json"
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakeResult())
    ran, findings, error = run_checkov(Path("irrelevant.yaml"))
    assert ran is False
    assert findings == []
    assert error is not None


def test_run_checkov_parses_failed_checks_from_mocked_output(monkeypatch):
    payload = {
        "check_type": "cloudformation",
        "results": {
            "failed_checks": [
                {
                    "check_id": "CKV_AWS_24",
                    "check_name": "Ensure no security groups allow ingress from 0.0.0.0:0 to port 22",
                    "resource": "AWS::EC2::SecurityGroup.MySG",
                    "severity": None,
                    "guideline": "https://example.com/guideline",
                }
            ]
        },
    }

    class _FakeResult:
        stdout = json.dumps(payload)
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakeResult())
    ran, findings, error = run_checkov(Path("irrelevant.yaml"))
    assert ran is True
    assert error is None
    assert len(findings) == 1
    assert findings[0].source == "checkov"
    # "0.0.0.0" keyword heuristic promotes this to HIGH since checkov's open-source
    # checks never carry a real severity.
    assert findings[0].severity == "HIGH"


def test_scan_template_aggregates_checkov_and_custom_findings(monkeypatch, tmp_path):
    """End-to-end with checkov mocked out (fast/deterministic) -- custom findings
    from the adversarial template must still show up, sorted with CRITICAL first."""
    monkeypatch.setattr(
        "orchestrator.guardrails.run_checkov",
        lambda path: (True, [Finding("checkov", "CKV_FAKE", "MEDIUM", "(template)", "fake finding")], None),
    )
    template_path = tmp_path / "template.yaml"
    template_yaml = yaml.dump(ADVERSARIAL_TEMPLATE)
    template_path.write_text(template_yaml, encoding="utf-8")

    result = scan_template(template_path, template_yaml=template_yaml)
    assert result.checkov_ran is True
    assert len(result.findings) == 1 + len(scan_hardcoded_secrets(ADVERSARIAL_TEMPLATE)) + len(
        scan_iam_wildcards(ADVERSARIAL_TEMPLATE)
    ) + len(scan_open_network(ADVERSARIAL_TEMPLATE))
    assert result.findings[0].severity == "CRITICAL"
    assert not result.passed
    assert result.blocking_findings


def test_scan_template_clean_template_passes(monkeypatch, tmp_path):
    monkeypatch.setattr("orchestrator.guardrails.run_checkov", lambda path: (True, [], None))
    template_path = tmp_path / "template.yaml"
    template_yaml = yaml.dump(CLEAN_TEMPLATE)
    template_path.write_text(template_yaml, encoding="utf-8")

    result = scan_template(template_path, template_yaml=template_yaml)
    assert result.findings == []
    assert result.passed


@pytest.mark.parametrize("severity", sorted(BLOCKING_SEVERITIES))
def test_blocking_severities_are_high_or_critical(severity):
    assert severity in ("HIGH", "CRITICAL")


def test_run_checkov_live_against_real_adversarial_template(tmp_path):
    """One real (non-mocked) invocation of the installed checkov binary, so a
    future broken invocation method (e.g. the Windows console-script wrapper
    regression this module works around) gets caught by CI, not just in prod."""
    template_path = tmp_path / "adversarial.yaml"
    template_path.write_text(yaml.dump(ADVERSARIAL_TEMPLATE), encoding="utf-8")
    ran, findings, error = run_checkov(template_path)
    assert ran is True, error


# ---------------------------------------------------------------------------
# guardrail_scan_gate node (orchestrator/agents.py) -- direct invocation,
# monkeypatching cli_ui.confirm instead of driving the whole LangGraph/Bedrock
# pipeline, same style as test_evaluation.py's node-level tests.
# ---------------------------------------------------------------------------

def _gate_state(tmp_path: Path, template: dict) -> dict:
    template_path = tmp_path / "template.yaml"
    yaml_text = yaml.dump(template)
    template_path.write_text(yaml_text, encoding="utf-8")
    return {
        "run_id": "test-guardrail", "output_dir": str(tmp_path),
        "output_path": str(template_path), "cfn_yaml": yaml_text, "agent_log": [],
    }


def _config(**overrides) -> Config:
    base = Config.from_env()
    return dataclasses.replace(base, **overrides) if overrides else base


def test_guardrail_gate_auto_continues_on_clean_template(tmp_path, monkeypatch):
    monkeypatch.setattr(cli_ui, "confirm", lambda *_: pytest.fail("should not prompt without blocking findings"))
    gate = make_guardrail_scan_gate(_config())
    result = gate(_gate_state(tmp_path, CLEAN_TEMPLATE))
    # checkov may still report real non-blocking (MEDIUM) findings against the RDS
    # instance (e.g. encryption-at-rest, multi-AZ) -- only HIGH/CRITICAL should ever
    # stop or prompt; this template has none of our own custom-check findings.
    assert all(f["severity"] not in ("HIGH", "CRITICAL") for f in result["guardrail_findings"])
    assert result.get("stopped") is not True
    assert result["agent_log"][0]["status"] in ("ok", "warning")


def test_guardrail_gate_auto_continues_on_blocking_findings_without_prompt(tmp_path, monkeypatch):
    monkeypatch.setattr(cli_ui, "confirm", lambda *_: pytest.fail("guardrail gate should not prompt"))
    gate = make_guardrail_scan_gate(_config())
    result = gate(_gate_state(tmp_path, ADVERSARIAL_TEMPLATE))
    assert result.get("stopped") is not True
    assert len(result["guardrail_findings"]) > 0
    assert Path(result["guardrail_report_path"]).exists()


def test_guardrail_gate_logs_warning_on_blocking_findings(tmp_path, monkeypatch):
    monkeypatch.setattr(cli_ui, "confirm", lambda *_: pytest.fail("guardrail gate should not prompt"))
    gate = make_guardrail_scan_gate(_config())
    result = gate(_gate_state(tmp_path, ADVERSARIAL_TEMPLATE))
    assert result.get("stopped") is not True
    assert len(result["guardrail_findings"]) > 0
    assert result["agent_log"][0]["status"] == "warning"


def test_guardrail_gate_disabled_via_config_skips_scan_entirely(tmp_path, monkeypatch):
    monkeypatch.setattr(cli_ui, "confirm", lambda *_: pytest.fail("should not prompt when disabled"))
    gate = make_guardrail_scan_gate(_config(guardrail_scan_enabled=False))
    result = gate(_gate_state(tmp_path, ADVERSARIAL_TEMPLATE))
    assert result["guardrail_findings"] == []
    assert result.get("stopped") is not True

