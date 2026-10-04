"""Migration plan: the structured artifact the LLM produces after reasoning over
the cloud-neutral representation (CNR). It captures *what* AWS resources should
exist and how their properties map from the source -- not template syntax or
formatting. Rendering that plan into an actual template is a separate, mechanical
step (see cfn_generator.py). Keeping the two apart is what makes it possible to
add a second target generator (Terraform, GCP Deployment Manager, ...) later
without touching the reasoning step, and to validate/repair the plan itself
independently of template syntax.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .prompts.agent3_v1 import PLAN_JSON_SCHEMA_HINT, PROMPT_VERSION, build_migration_plan_prompt  # noqa: F401 (re-exported)


class MigrationPlanError(RuntimeError):
    pass


@dataclass
class PlannedResource:
    logical_id: str
    aws_type: str
    properties: dict
    depends_on: list[str] = field(default_factory=list)
    source_azure_type: str = ""


@dataclass
class MigrationPlan:
    description: str
    parameters: dict
    conditions: dict
    resources: list[PlannedResource]
    outputs: dict


def parse_migration_plan(raw_text: str) -> MigrationPlan:
    cleaned = _strip_markdown_fences(raw_text)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise MigrationPlanError(
            f"Migration plan is not valid JSON: {exc}\nRaw output:\n{raw_text}"
        ) from exc

    try:
        resources = [
            PlannedResource(
                logical_id=r["logical_id"],
                aws_type=r["aws_type"],
                properties=r.get("properties", {}) or {},
                depends_on=list(r.get("depends_on", []) or []),
                source_azure_type=r.get("source_azure_type", ""),
            )
            for r in data["resources"]
        ]
    except KeyError as exc:
        raise MigrationPlanError(f"Migration plan missing required field: {exc}") from exc

    conditions = data.get("conditions", {}) or {}
    referenced = set()
    for resource in resources:
        referenced |= _find_referenced_conditions(resource.properties)
    referenced |= _find_referenced_conditions(data.get("outputs", {}) or {})
    undeclared = sorted(referenced - conditions.keys())
    if undeclared:
        raise MigrationPlanError(
            "Migration plan references Fn::If condition(s) with no matching "
            f"entry in \"conditions\": {', '.join(undeclared)}"
        )

    parameters = data.get("parameters", {}) or {}
    _repair_optional_empty_sentinel_params(parameters, conditions)

    return MigrationPlan(
        description=data.get("description", ""),
        parameters=parameters,
        conditions=conditions,
        resources=resources,
        outputs=data.get("outputs", {}) or {},
    )


def _repair_optional_empty_sentinel_params(parameters: dict, conditions: dict) -> None:
    """Any parameter whose only purpose is an 'empty means not provided' condition
    (Fn::Equals against the literal "") must actually be deployable as empty --
    the LLM sometimes declares the condition but forgets Default/leaves a
    MinLength/AllowedPattern that rejects "", which the generator can't catch
    via cfn-lint and only surfaces as a deploy-time "no value available" failure.
    Mutates `parameters` in place; never invents a Default for unrelated params.
    """
    for condition in conditions.values():
        param_name = _find_empty_sentinel_param(condition)
        if param_name is None or param_name not in parameters:
            continue
        definition = parameters[param_name]
        definition.setdefault("Default", "")
        if definition.get("Default") == "":
            definition.pop("MinLength", None)
            definition.pop("AllowedPattern", None)


def _find_empty_sentinel_param(value) -> str | None:
    """If `value` is (or contains) an Fn::Equals comparing some {"Ref": X} to the
    literal "", return X -- the 'optional, empty means not provided' idiom."""
    if isinstance(value, dict):
        equals_args = value.get("Fn::Equals")
        if isinstance(equals_args, list) and len(equals_args) == 2:
            a, b = equals_args
            if isinstance(a, dict) and a.get("Ref") and b == "":
                return a["Ref"]
            if isinstance(b, dict) and b.get("Ref") and a == "":
                return b["Ref"]
        for v in value.values():
            found = _find_empty_sentinel_param(v)
            if found is not None:
                return found
    elif isinstance(value, list):
        for v in value:
            found = _find_empty_sentinel_param(v)
            if found is not None:
                return found
    return None


def _find_referenced_conditions(value) -> set[str]:
    """Collect every condition name used via Fn::If anywhere inside value."""
    found: set[str] = set()
    if isinstance(value, dict):
        if_args = value.get("Fn::If")
        if isinstance(if_args, list) and if_args and isinstance(if_args[0], str):
            found.add(if_args[0])
        for v in value.values():
            found |= _find_referenced_conditions(v)
    elif isinstance(value, list):
        for v in value:
            found |= _find_referenced_conditions(v)
    return found


def _strip_markdown_fences(text: str) -> str:
    cleaned = text.strip().replace("\ufeff", "")
    fenced = re.search(r"```(?:json)?\s*(.*?)```", cleaned, re.IGNORECASE | re.DOTALL)
    if fenced:
        cleaned = fenced.group(1).strip()
    return cleaned
