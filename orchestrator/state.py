"""Shared state passed between the 7 migration agents in orchestrator/graph.py.

One TypedDict flows through every LangGraph node; each agent reads what it
needs and returns a partial dict of updates (LangGraph merges these into the
running state). `agent_log` is additive (each agent appends one entry) so
Agent 7 can build a report from the full history without re-deriving it.
"""
from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict

from .secrets_handling import SecretValue


class AgentLogEntry(TypedDict):
    agent: str
    status: str  # "ok" | "warning" | "stopped" | "failed"
    message: str


class MigrationState(TypedDict, total=False):
    # Inputs
    bicep_path: str
    output_dir: str
    run_id: str
    dry_run: bool
    started_at: float  # time.time() when the run began -- used for time-to-migrate

    # Human-in-the-loop decisions, one entry per interactive gate that actually
    # prompted (by default this is stack_check_gate only). Used by
    # evaluation.py's human-intervention-rate metric.
    human_decisions: Annotated[list[dict], operator.add]

    # Agent 0: export an Azure resource group as the source .bicep (optional;
    # if resource_group is unset, bicep_path above is used as-is)
    resource_group: str
    subscription_id: str
    source_secret_values: dict[str, SecretValue]  # real Key Vault secret values, keyed by secret name
    source_vault_names: list[str]  # Key Vault name(s) found in the source resource group

    # Agent 1: validate
    arm_template: dict[str, Any]
    resource_types: list[str]
    supported_types: list[str]
    unsupported_types: list[str]
    noop_types: list[str]  # implicit Azure child resources, confirmed default shape, dropped
    foldable_types: list[str]  # no own AWS resource; properties folded into parent by Agent 2
    agent1_decision: str  # "auto_continue" | "human_approved" | "stopped"
    stopped: bool
    stop_reason: str

    # Agent 2: cloud-neutral representation
    cnr: Any  # orchestrator.cloud_neutral.CloudNeutralRepresentation
    per_resource_templates: dict[str, dict]  # logical_id -> custom template

    # Agent 3: mapping / migration plan (LLM reasoning)
    mapping_docs: dict[str, str]
    migration_plan_raw: str
    migration_plan: Any  # orchestrator.migration_plan.MigrationPlan
    mapping_table: list[dict]  # [{logical_id, source_azure_type, aws_type}]

    # Plan approval gate: checkpoint after clean lint and before deployment gates
    plan_confirmed: bool

    # Agent 4: render
    cfn_yaml: str
    output_path: str

    # Agent 5: validate rendered template
    lint_passed: bool
    lint_output: str
    lint_errors: int
    lint_warnings: int
    validation_history: Annotated[list[dict], operator.add]  # one entry per cfn-lint attempt
    fix_attempts: int
    max_fix_attempts: int

    # Guardrail scan gate: checkov + custom secret/IAM/network checks on the
    # rendered template; findings are logged and deployment auto-continues.
    guardrail_findings: list[dict]  # dataclasses.asdict(Finding) -- see orchestrator/guardrails.py
    guardrail_report_path: str

    # Stack conflict gate + Agent 6/Verify: deploy + post-deploy smoke checks
    param_overrides: dict[str, str]  # CFN parameter values from --params-file / CLI
    param_values: dict[str, str | SecretValue]  # fully resolved/validated values used for deploy
    stack_action: str  # normally "create" from stack_check_gate; may be "update" only via Agent 6 fallback
    deploy_result: dict
    verify_result: dict  # {secrets_checked, vpc_reachability, lambda_invocations}

    # Agent 7: report
    report_path: str
    agent_log: Annotated[list[AgentLogEntry], operator.add]
