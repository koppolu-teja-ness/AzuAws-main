"""Guardrail security scanning for the rendered CloudFormation template.

Two independent engines feed into one list of `Finding`, so agents.py can
gate deployment on severity without caring which engine produced a result:

- checkov (static policy scanning, ~1000 built-in CloudFormation checks).
  Invoked via `python -m checkov.main` rather than the installed console
  script -- checkov's Windows .cmd entry point fails to resolve its own
  package in a venv (`ModuleNotFoundError: No module named 'checkov'`);
  `-m` always finds it via sys.path regardless of platform.
- a handful of custom, fast checks targeting this project's specific
  migration risks: hardcoded secret-looking literals, wildcard IAM
  Action/Resource/Principal, and security-group ingress open to the
  world on sensitive ports. These catch patterns checkov's open-source
  rule set doesn't (e.g. a secret-sounding parameter missing NoEcho).

Severity drives the deployment gate in agents.py (HIGH/CRITICAL block;
MEDIUM/LOW are advisory-only). checkov's open-source checks never carry a
real `severity` (that field is populated by the paid Bridgecrew platform and
is always null without an API key), so `_infer_checkov_severity` assigns one
from the check's own description via keyword heuristics.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

SEVERITY_ORDER = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}
BLOCKING_SEVERITIES = {"HIGH", "CRITICAL"}


@dataclass
class Finding:
    source: str  # "checkov" | "custom"
    check_id: str
    severity: str  # LOW | MEDIUM | HIGH | CRITICAL
    resource: str  # logical id / parameter name, or "(template)"
    message: str
    guideline: str | None = None


@dataclass
class GuardrailScanResult:
    findings: list[Finding] = field(default_factory=list)
    checkov_ran: bool = True
    checkov_error: str | None = None

    @property
    def blocking_findings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity in BLOCKING_SEVERITIES]

    @property
    def passed(self) -> bool:
        return not self.blocking_findings


# ---------------------------------------------------------------------------
# checkov
# ---------------------------------------------------------------------------

# Open-source checkov checks never carry a real severity -- that field only
# comes from the paid Bridgecrew platform. Approximate one from keywords in
# the check's own description so the HIGH/CRITICAL deployment gate still
# means something without an API key.
_HIGH_SEVERITY_KEYWORDS = (
    "public", "0.0.0.0", "::/0", "wildcard", "unencrypted", "unrestricted",
    "world", "anyone", "plaintext", "plain text", "mfa", "root account",
    "not enabled for the stack", "password",
)


def _infer_checkov_severity(check_name: str) -> str:
    lowered = check_name.lower()
    if any(keyword in lowered for keyword in _HIGH_SEVERITY_KEYWORDS):
        return "HIGH"
    return "MEDIUM"


def run_checkov(template_path: Path) -> tuple[bool, list[Finding], str | None]:
    """Run checkov against the rendered template. Returns (ran_ok, findings, error)."""
    cmd = [
        sys.executable, "-m", "checkov.main",
        "-f", str(template_path),
        "--framework", "cloudformation",
        "--output", "json",
        "--compact",
        "--quiet",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
    except OSError as exc:
        return False, [], f"Could not run checkov: {exc}"

    output = (result.stdout or "").strip()
    if not output:
        error = (result.stderr or "checkov produced no output").strip()
        return False, [], error[:2000]

    try:
        payload = json.loads(output)
    except json.JSONDecodeError as exc:
        return False, [], f"Could not parse checkov output: {exc}"

    # Single framework -> one report dict; multiple frameworks -> a list of them.
    reports = payload if isinstance(payload, list) else [payload]
    findings: list[Finding] = []
    for report in reports:
        if not isinstance(report, dict):
            continue
        failed_checks = (report.get("results") or {}).get("failed_checks") or []
        for check in failed_checks:
            check_name = check.get("check_name", "") or ""
            findings.append(
                Finding(
                    source="checkov",
                    check_id=check.get("check_id", "UNKNOWN"),
                    severity=check.get("severity") or _infer_checkov_severity(check_name),
                    resource=check.get("resource") or "(template)",
                    message=check_name,
                    guideline=check.get("guideline"),
                )
            )
    return True, findings, None


# ---------------------------------------------------------------------------
# Custom checks: hardcoded secrets
# ---------------------------------------------------------------------------

_SECRET_KEY_PATTERN = re.compile(
    r"(password|secret|token|apikey|api_key|accesskey|access_key|"
    r"privatekey|private_key|connectionstring|connection_string)",
    re.IGNORECASE,
)
_PLACEHOLDER_PATTERN = re.compile(
    r"^(changeme|change_me|change-me|placeholder|todo|xxx+|<.*>|\{\{.*\}\})$",
    re.IGNORECASE,
)
_INTRINSIC_KEYS = {"Ref", "Condition"}


def _is_intrinsic(value: Any) -> bool:
    """True for a CFN intrinsic-function dict (dynamic value, not a literal)."""
    return isinstance(value, dict) and len(value) == 1 and (
        next(iter(value)) in _INTRINSIC_KEYS or next(iter(value)).startswith("Fn::")
    )


def _iter_leaf_values(node: Any, key_hint: str = "") -> list[tuple[str, Any]]:
    """Yield (nearest-key, value) for every literal leaf under `node`, skipping
    intrinsic-function dicts entirely (their contents are dynamic, not a
    hardcoded value)."""
    if _is_intrinsic(node):
        return []
    if isinstance(node, dict):
        leaves = []
        for key, value in node.items():
            leaves.extend(_iter_leaf_values(value, key))
        return leaves
    if isinstance(node, list):
        leaves = []
        for item in node:
            leaves.extend(_iter_leaf_values(item, key_hint))
        return leaves
    return [(key_hint, node)]


def scan_hardcoded_secrets(template: dict) -> list[Finding]:
    findings: list[Finding] = []
    resources = template.get("Resources") or {}
    for logical_id, body in resources.items():
        if not isinstance(body, dict):
            continue
        for key, value in _iter_leaf_values(body.get("Properties") or {}):
            if (
                isinstance(value, str) and value.strip()
                and _SECRET_KEY_PATTERN.search(key)
                and not _PLACEHOLDER_PATTERN.match(value.strip())
            ):
                findings.append(Finding(
                    source="custom",
                    check_id="CUSTOM_HARDCODED_SECRET",
                    severity="HIGH",
                    resource=logical_id,
                    message=(
                        f"Property '{key}' looks like a secret but has a literal hardcoded "
                        "value instead of a Ref/dynamic reference."
                    ),
                ))

    parameters = template.get("Parameters") or {}
    for name, definition in parameters.items():
        if not isinstance(definition, dict) or not _SECRET_KEY_PATTERN.search(name):
            continue
        default = definition.get("Default")
        if default not in (None, "") and not definition.get("NoEcho"):
            findings.append(Finding(
                source="custom",
                check_id="CUSTOM_SECRET_PARAM_NO_NOECHO",
                severity="HIGH",
                resource=name,
                message=(
                    f"Parameter '{name}' looks like a secret, has a hardcoded Default, "
                    "and is missing NoEcho: true."
                ),
            ))
    return findings


# ---------------------------------------------------------------------------
# Custom checks: overly permissive IAM
# ---------------------------------------------------------------------------

_IAM_TYPES = {"AWS::IAM::Role", "AWS::IAM::Policy", "AWS::IAM::ManagedPolicy", "AWS::IAM::User", "AWS::IAM::Group"}


def _as_list(value: Any) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _has_wildcard(value: Any) -> bool:
    return any(v == "*" for v in _as_list(value) if isinstance(v, str))


def _principal_is_wildcard(principal: Any) -> bool:
    if principal == "*":
        return True
    if isinstance(principal, dict):
        return any(_has_wildcard(v) for v in principal.values())
    return False


def _scan_policy_document(doc: Any, logical_id: str, doc_label: str) -> list[Finding]:
    if not isinstance(doc, dict):
        return []
    findings = []
    for statement in _as_list(doc.get("Statement")):
        if not isinstance(statement, dict) or statement.get("Effect") != "Allow":
            continue
        action_wild = _has_wildcard(statement.get("Action"))
        resource_wild = _has_wildcard(statement.get("Resource"))
        principal_wild = _principal_is_wildcard(statement.get("Principal"))
        if action_wild and resource_wild:
            findings.append(Finding(
                source="custom", check_id="CUSTOM_IAM_FULL_ADMIN", severity="CRITICAL",
                resource=logical_id,
                message=f"{doc_label} grants Action:'*' on Resource:'*' (unrestricted admin access).",
            ))
        elif action_wild:
            findings.append(Finding(
                source="custom", check_id="CUSTOM_IAM_WILDCARD_ACTION", severity="HIGH",
                resource=logical_id,
                message=f"{doc_label} allows Action:'*' (wildcard permissions).",
            ))
        elif resource_wild:
            findings.append(Finding(
                source="custom", check_id="CUSTOM_IAM_WILDCARD_RESOURCE", severity="HIGH",
                resource=logical_id,
                message=f"{doc_label} allows access to Resource:'*' (wildcard resource scope).",
            ))
        if principal_wild:
            findings.append(Finding(
                source="custom", check_id="CUSTOM_IAM_WILDCARD_PRINCIPAL", severity="CRITICAL",
                resource=logical_id,
                message=f"{doc_label} trusts Principal:'*' (assumable/usable by anyone).",
            ))
    return findings


def scan_iam_wildcards(template: dict) -> list[Finding]:
    findings: list[Finding] = []
    resources = template.get("Resources") or {}
    for logical_id, body in resources.items():
        if not isinstance(body, dict) or body.get("Type") not in _IAM_TYPES:
            continue
        properties = body.get("Properties") or {}
        findings.extend(_scan_policy_document(
            properties.get("AssumeRolePolicyDocument"), logical_id, "AssumeRolePolicyDocument"
        ))
        findings.extend(_scan_policy_document(
            properties.get("PolicyDocument"), logical_id, "PolicyDocument"
        ))
        for policy in _as_list(properties.get("Policies")):
            if isinstance(policy, dict):
                findings.extend(_scan_policy_document(
                    policy.get("PolicyDocument"), logical_id,
                    f"Inline policy '{policy.get('PolicyName', '?')}'",
                ))
    return findings


# ---------------------------------------------------------------------------
# Custom checks: open network ingress
# ---------------------------------------------------------------------------

_SENSITIVE_PORTS = {22: "SSH", 3389: "RDP", 3306: "MySQL", 5432: "PostgreSQL", 1433: "MSSQL", 6379: "Redis", 27017: "MongoDB"}
_OPEN_CIDRS = {"0.0.0.0/0", "::/0"}


def _ingress_is_open(rule: dict) -> bool:
    if not isinstance(rule, dict):
        return False
    return rule.get("CidrIp") in _OPEN_CIDRS or rule.get("CidrIpv6") in _OPEN_CIDRS


def _describe_port_range(rule: dict) -> tuple[str, str]:
    """Returns (severity, port_description) for an open ingress rule."""
    protocol = str(rule.get("IpProtocol", "")).lower()
    from_port, to_port = rule.get("FromPort"), rule.get("ToPort")
    if protocol in ("-1", "all"):
        return "CRITICAL", "all ports/protocols"
    if from_port is None or to_port is None:
        return "HIGH", f"protocol {protocol or 'unknown'}"
    try:
        from_port, to_port = int(from_port), int(to_port)
    except (TypeError, ValueError):
        return "HIGH", f"ports {from_port}-{to_port}"
    hit_sensitive = [name for port, name in _SENSITIVE_PORTS.items() if from_port <= port <= to_port]
    if hit_sensitive:
        return "CRITICAL", f"port(s) {from_port}-{to_port} ({', '.join(hit_sensitive)})"
    if to_port - from_port >= 100:
        return "HIGH", f"a wide port range {from_port}-{to_port}"
    return "MEDIUM", f"port(s) {from_port}-{to_port}"


def scan_open_network(template: dict) -> list[Finding]:
    findings: list[Finding] = []
    resources = template.get("Resources") or {}
    for logical_id, body in resources.items():
        if not isinstance(body, dict):
            continue
        properties = body.get("Properties") or {}
        rule_sets: list[tuple[str, list]] = []
        if body.get("Type") == "AWS::EC2::SecurityGroup":
            rule_sets.append(("SecurityGroupIngress", _as_list(properties.get("SecurityGroupIngress"))))
        elif body.get("Type") == "AWS::EC2::SecurityGroupIngress":
            rule_sets.append(("(standalone ingress rule)", [properties]))
        for rule_label, rules in rule_sets:
            for rule in rules:
                if not _ingress_is_open(rule):
                    continue
                severity, port_desc = _describe_port_range(rule)
                findings.append(Finding(
                    source="custom", check_id="CUSTOM_OPEN_INGRESS", severity=severity,
                    resource=logical_id,
                    message=f"{rule_label} is open to the internet (0.0.0.0/0) on {port_desc}.",
                ))
    return findings


# ---------------------------------------------------------------------------
# Aggregate entry point
# ---------------------------------------------------------------------------

def scan_template(template_path: Path, template_yaml: str | None = None) -> GuardrailScanResult:
    """Run checkov plus all custom checks against the rendered template.

    `template_yaml` lets the caller pass the already-in-memory rendered text
    (state["cfn_yaml"]) instead of re-reading the file, for the custom checks
    only -- checkov still needs the real file path.
    """
    checkov_ran, checkov_findings, checkov_error = run_checkov(template_path)

    text = template_yaml if template_yaml is not None else template_path.read_text(encoding="utf-8")
    try:
        template = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        return GuardrailScanResult(
            findings=checkov_findings,
            checkov_ran=checkov_ran,
            checkov_error=checkov_error or f"Could not parse template for custom checks: {exc}",
        )

    custom_findings = [
        *scan_hardcoded_secrets(template),
        *scan_iam_wildcards(template),
        *scan_open_network(template),
    ]
    all_findings = checkov_findings + custom_findings
    all_findings.sort(key=lambda f: SEVERITY_ORDER.get(f.severity, 0), reverse=True)
    return GuardrailScanResult(findings=all_findings, checkov_ran=checkov_ran, checkov_error=checkov_error)
