"""Deterministic CloudFormation generator: renders a MigrationPlan into CFN YAML.

This step has no LLM involved by design -- once the plan (which AWS resources,
which properties) is decided, turning it into template syntax is a mechanical
transformation. This is also the seam where other target generators (Terraform,
GCP Deployment Manager, ...) would plug in, since they'd all consume the same
MigrationPlan instead of re-prompting an LLM for template syntax.
"""
from __future__ import annotations

import yaml

from .migration_plan import MigrationPlan


def generate_cloudformation(plan: MigrationPlan) -> str:
    template: dict = {"AWSTemplateFormatVersion": "2010-09-09"}
    if plan.description:
        template["Description"] = plan.description
    if plan.parameters:
        template["Parameters"] = plan.parameters
    if plan.conditions:
        template["Conditions"] = _simplify_intrinsics(plan.conditions)

    resources: dict = {}
    for resource in plan.resources:
        entry: dict = {
            "Type": resource.aws_type,
            "Properties": _simplify_intrinsics(resource.properties),
        }
        if resource.depends_on:
            entry["DependsOn"] = resource.depends_on
        resources[resource.logical_id] = entry
    template["Resources"] = resources

    if plan.outputs:
        template["Outputs"] = _simplify_intrinsics(plan.outputs)

    return yaml.dump(template, sort_keys=False, default_flow_style=False)


def _simplify_intrinsics(value):
    """Collapse LLM artifacts like {"Fn::Sub": "plain text"} (no ${...} vars) to
    the plain string, since cfn-lint (W1020) flags Fn::Sub with nothing to
    substitute. This is a mechanical cleanup, not a mapping decision, so it
    belongs in the deterministic generator rather than a prompt/retry fix.
    """
    if isinstance(value, dict):
        if (
            set(value.keys()) == {"Fn::Sub"}
            and isinstance(value["Fn::Sub"], str)
            and "${" not in value["Fn::Sub"]
        ):
            return value["Fn::Sub"]
        return {k: _simplify_intrinsics(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_simplify_intrinsics(v) for v in value]
    return value
