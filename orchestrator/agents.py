"""The 7 migration agents, each a thin, testable function over MigrationState.

Deterministic stages (1, 2, 4, 5) wrap existing orchestrator modules unchanged;
only Agent 3 (mapping) calls the LLM. This keeps the same "LLM only reasons,
never emits template syntax" split as pipeline.py, just organized as discrete
agents instead of one linear function.
"""
from __future__ import annotations

import dataclasses
import datetime
import json
import os
import re
from pathlib import Path
from typing import Any

from . import cli_ui
from .azure_export import (
    AzureExportError,
    SERVICE_OPTIONS,
    export_resource_group_to_bicep,
    fetch_resource_group_secret_values,
    list_resource_ids_by_type,
)
from .bicep_compiler import BicepCompilerError, compile_bicep_to_arm
from .cfn_generator import generate_cloudformation
from .cloud_neutral import build_cnr, write_cnr
from .config import Config
from .generator import Generator, GeneratorNotConfiguredError
from . import guardrails
from .knowledge_base import KnowledgeBase
from .migration_plan import (
    MigrationPlanError,
    PLAN_JSON_SCHEMA_HINT,
    build_migration_plan_prompt,
    parse_migration_plan,
)
from .observability import annotate_current_run, attach_feedback, get_trace_url, trace_span, traceable
from .rag import build_kb_query, retrieve_mapping_docs
from .resource_extractor import (
    FOLDABLE_CHILD_TYPES,
    IMPLICIT_NOOP_TYPES,
    extract_resource_types,
    extract_type_properties,
    is_default_shape,
)
from .secrets_handling import SecretValue, register_secret_values, reveal_secret_value, wrap_secret_mapping
from .state import MigrationState
from .validator import run_cfn_lint


def _log(agent: str, status: str, message: str) -> list[dict]:
    return [{"agent": agent, "status": status, "message": message}]


def _with_gate_meta(state: MigrationState, details: dict[str, str]) -> dict[str, str]:
    """Attach consistent run metadata to human-review gate context tables."""
    enriched = {
        "Run ID": str(state.get("run_id") or "(unknown)"),
        "Timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
    }
    enriched.update(details)
    return enriched


def _run_dir(state: MigrationState) -> Path:
    """Per-run artifact folder (CNR, migration plan, lint report, final report.md)."""
    run_dir = Path(state["output_dir"]) / "runs" / state["run_id"]
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


# ---------------------------------------------------------------------------
# Agent 0: export a live Azure resource group as the source ARM JSON.
# Only runs when a resource_group was passed instead of an existing .bicep file.
# Asks which service's resources to export (Key Vault / Functions / VNet) and
# scopes the export to just that service's resource types. No decompile step --
# `az group export` already returns ARM JSON, which Agent 1 reads directly.
# ---------------------------------------------------------------------------
def make_agent0_export_resource_group(input_dir: Path):
    def agent0_export_resource_group(state: MigrationState) -> dict:
        resource_group = state.get("resource_group")
        if not resource_group:
            cli_ui.agent_result("Agent 0", "ok", "Using provided source file; live export skipped.")
            return {
                "agent_log": _log(
                    "agent0_export_resource_group", "ok", "No resource group given; using the provided .bicep file."
                ),
            }

        cli_ui.step("Agent 0", f"Which Azure service should be exported from resource group '{resource_group}'?")
        choice = cli_ui.select_option(
            "Select service",
            {key: label for key, (label, _) in SERVICE_OPTIONS.items()},
        )
        option = SERVICE_OPTIONS.get(choice)
        if option is None:
            reason = f"Invalid service selection '{choice}'; expected one of {', '.join(SERVICE_OPTIONS)}."
            cli_ui.agent_result("Agent 0", "failed", reason)
            return {
                "stopped": True,
                "stop_reason": reason,
                "agent_log": _log("agent0_export_resource_group", "stopped", reason),
            }
        service_label, azure_types = option

        cli_ui.step("Agent 0", f"Exporting {service_label} resources from resource group '{resource_group}'...")
        try:
            resource_ids = list_resource_ids_by_type(resource_group, azure_types, state.get("subscription_id"))
            if not resource_ids:
                reason = f"No {service_label} resources found in resource group '{resource_group}'."
                cli_ui.agent_result("Agent 0", "failed", reason)
                return {
                    "stopped": True,
                    "stop_reason": reason,
                    "agent_log": _log("agent0_export_resource_group", "stopped", reason),
                }
            bicep_path = export_resource_group_to_bicep(
                resource_group, input_dir, state.get("subscription_id"), resource_ids=resource_ids
            )
        except AzureExportError as exc:
            cli_ui.agent_result("Agent 0", "failed", f"Resource group export failed: {exc}")
            return {
                "stopped": True,
                "stop_reason": f"Resource group export failed: {exc}",
                "agent_log": _log("agent0_export_resource_group", "stopped", str(exc)),
            }

        log_entries = _log(
            "agent0_export_resource_group", "ok",
            f"Exported {service_label} resources from '{resource_group}' to {bicep_path}.",
        )

        # ARM/Bicep exports never include Key Vault secret values (Azure omits them from
        # the control-plane export API); read the real values from the data plane here so
        # agent6_deploy can carry them over instead of fabricating new ones. Only relevant
        # when Key Vault was the selected service.
        source_secret_values: dict[str, SecretValue] = {}
        source_vault_names: list[str] = []
        if service_label == "Key Vault":
            source_secret_values_raw, source_vault_names, secret_fetch_warnings = fetch_resource_group_secret_values(
                resource_group, state.get("subscription_id")
            )
            source_secret_values = wrap_secret_mapping(source_secret_values_raw)
            log_entries[-1]["message"] += f" Fetched {len(source_secret_values)} real secret value(s)."
            for secret_warning in secret_fetch_warnings:
                cli_ui.warning(f"[Agent 0] {secret_warning}")
                log_entries += _log("agent0_export_resource_group", "warning", secret_warning)
        cli_ui.agent_result("Agent 0", "ok", f"Export completed: {bicep_path}")
        return {
            "bicep_path": str(bicep_path),
            "source_secret_values": source_secret_values,
            "source_vault_names": source_vault_names,
            "agent_log": log_entries,
        }

    return agent0_export_resource_group


# ---------------------------------------------------------------------------
# Agent 1: validate the Bicep file (compile + resource-support check).
# ---------------------------------------------------------------------------
def make_agent1_validate(knowledge_base: KnowledgeBase):
    def agent1_validate(state: MigrationState) -> dict:
        cli_ui.agent_phase("Agent 1", "Compiling source and validating knowledge-base coverage...")
        bicep_path = Path(state["bicep_path"])
        try:
            # Agent 0's resource-group export already produces ARM JSON directly (no
            # decompile/build round trip -- see azure_export.export_resource_group_to_bicep);
            # only an actual hand-authored .bicep source needs `az bicep build` here.
            if bicep_path.suffix == ".json":
                arm_template = json.loads(bicep_path.read_text(encoding="utf-8"))
            else:
                arm_template = compile_bicep_to_arm(bicep_path)
        except (BicepCompilerError, OSError, json.JSONDecodeError) as exc:
            cli_ui.agent_result("Agent 1", "failed", f"Validation failed: {exc}")
            return {
                "stopped": True,
                "stop_reason": f"Bicep compilation failed: {exc}",
                "agent_log": _log("agent1_validate", "stopped", str(exc)),
            }

        all_resource_types = extract_resource_types(arm_template)
        type_properties = extract_type_properties(arm_template)

        # Confirmed-default implicit child resources (e.g. storage account's default
        # blobServices, a web app's auto hostNameBinding) are dropped entirely; anything
        # that doesn't match the known default shape falls through to the human gate below
        # instead of being silently discarded.
        noop_types = [
            t
            for t in all_resource_types
            if t in IMPLICIT_NOOP_TYPES and all(is_default_shape(t, p) for p in type_properties.get(t, []))
        ]
        # Foldable children have no AWS resource of their own; Agent 2 merges their
        # properties into the parent resource instead, so they're excluded here too.
        foldable_types = [t for t in all_resource_types if t in FOLDABLE_CHILD_TYPES]

        resource_types = [t for t in all_resource_types if t not in noop_types and t not in foldable_types]
        supported_types = knowledge_base.supported_types()
        unsupported = [t for t in resource_types if t not in supported_types]

        base_update = {
            "arm_template": arm_template,
            "resource_types": resource_types,
            "supported_types": supported_types,
            "unsupported_types": unsupported,
            "noop_types": noop_types,
            "foldable_types": foldable_types,
        }

        if not all_resource_types:
            cli_ui.agent_result("Agent 1", "failed", "No resources found in source template.")
            return {
                **base_update,
                "stopped": True,
                "stop_reason": "No resources found in the Bicep template.",
                "agent1_decision": "stopped",
                "agent_log": _log("agent1_validate", "stopped", "No resources found."),
            }

        if unsupported:
            cli_ui.warning(
                f"[Agent 1] Unsupported resource type(s): {', '.join(unsupported)} "
                "(no knowledge-base mapping doc yet)."
            )
            cli_ui.console.print(
                cli_ui.gate_context_table(
                    _with_gate_meta(
                        state,
                        {
                            "Detected resource types": str(len(resource_types)),
                            "Unsupported": str(len(unsupported)),
                            "Supported if continued": str(len(resource_types) - len(unsupported)),
                            "Implicit defaults skipped": str(len(noop_types)),
                            "Foldable child types": str(len(foldable_types)),
                        },
                    ),
                    title="Unsupported Type Review",
                )
            )
            if not cli_ui.confirm(
                "Continue migrating only the supported resources?"
            ):
                cli_ui.agent_result("Agent 1", "failed", "Run stopped due to unsupported resource types.")
                return {
                    **base_update,
                    "stopped": True,
                    "stop_reason": f"Human stopped the run due to unsupported types: {', '.join(unsupported)}",
                    "agent1_decision": "stopped",
                    "human_decisions": [{"gate": "agent1_validate", "decision": "rejected"}],
                    "agent_log": _log(
                        "agent1_validate", "stopped", "Human declined to continue with unsupported types."
                    ),
                }
            cli_ui.agent_result(
                "Agent 1",
                "warning",
                "Continuing with supported resource types only; unsupported types were skipped.",
            )
            return {
                **base_update,
                "resource_types": [t for t in resource_types if t not in unsupported],
                "stopped": False,
                "agent1_decision": "human_approved",
                "human_decisions": [{"gate": "agent1_validate", "decision": "approved"}],
                "agent_log": _log(
                    "agent1_validate", "warning",
                    "Human approved continuing with the supported resources only; unsupported types were skipped.",
                ),
            }

        cli_ui.agent_result(
            "Agent 1",
            "ok",
            f"Validation complete: {len(resource_types)} supported type(s), {len(unsupported)} unsupported.",
        )
        return {
            **base_update,
            "stopped": False,
            "agent1_decision": "auto_continue",
            "agent_log": _log(
                "agent1_validate",
                "ok",
                f"All {len(resource_types)} resource type(s) supported "
                f"({len(noop_types)} implicit default(s) dropped, {len(foldable_types)} folded into parent).",
            ),
        }

    return agent1_validate


# ---------------------------------------------------------------------------
# Agent 2: build the cloud-neutral representation (custom template per resource)

# ---------------------------------------------------------------------------
def agent2_build_cnr(state: MigrationState) -> dict:
    cli_ui.agent_phase("Agent 2", "Building cloud-neutral representation (CNR)...")
    cnr = build_cnr(state["arm_template"])
    _fold_child_properties(cnr, state.get("foldable_types", []))
    write_cnr(cnr, output_dir=str(_run_dir(state)))
    per_resource_templates = {
        r.logical_id: dataclasses.asdict(r)
        for r in cnr.resources
        if r.azure_type in state["resource_types"]
    }
    cli_ui.agent_result("Agent 2", "ok", f"Created {len(per_resource_templates)} cloud-neutral resource template(s).")
    return {
        "cnr": cnr,
        "per_resource_templates": per_resource_templates,
        "agent_log": _log(
            "agent2_build_cnr", "ok", f"Built cloud-neutral templates for {len(per_resource_templates)} resource(s)."
        ),
    }


def _fold_child_properties(cnr, foldable_types: list[str]) -> None:
    """Merge each FOLDABLE_CHILD_TYPES resource's properties into its parent's
    properties (keyed by child type + name) so dropping its own template entry
    later doesn't lose the data -- mutates `cnr.resources` in place."""
    if not foldable_types:
        return
    by_logical_id = {r.logical_id: r for r in cnr.resources}
    for child in cnr.resources:
        if child.azure_type not in foldable_types or not child.parent_logical_id:
            continue
        parent = by_logical_id.get(child.parent_logical_id)
        if parent is None:
            continue
        folded = parent.properties.setdefault("_foldedChildren", {})
        folded.setdefault(child.azure_type, {})[str(child.name_expression)] = child.properties


# ---------------------------------------------------------------------------
# Agent 3: LLM-reasoned mapping of each resource to its AWS equivalent.
# Mapping docs are fetched via RAG retrieval (top-K relevant sections per doc,
# see rag.py) when enabled, falling back to full doc text otherwise. This only
# affects prompt content -- the resource-type support gate stays in Agent 1.
# ---------------------------------------------------------------------------
@traceable(
    run_type="retriever",
    name="rag_retrieval",
    process_inputs=lambda inputs: {k: v for k, v in inputs.items() if k != "knowledge_base"},
)
def _traced_retrieve_mapping_docs(knowledge_base: KnowledgeBase, resource_types: list[str], query: str, config: Config):
    docs = retrieve_mapping_docs(knowledge_base, resource_types, query, config)
    annotate_current_run(metadata={"source_docs": sorted(docs.keys()), "rag_enabled": config.rag_enabled})
    return docs


def make_agent3_map_resources(knowledge_base: KnowledgeBase, generator: Generator, config: Config):
    def agent3_map_resources(state: MigrationState) -> dict:
        lint_output = state.get("lint_output")
        fix_attempts = state.get("fix_attempts", 0)
        cli_ui.agent_phase(
            "Agent 3",
            "Repairing migration plan from lint feedback..." if lint_output and fix_attempts > 0
            else "Mapping Azure resources to AWS using migration knowledge...",
        )
        annotate_current_run(tags=[f"fix_attempt:{fix_attempts}"], metadata={"fix_attempt": fix_attempts})

        if lint_output and fix_attempts > 0:
            # Self-correction retry: feed the previous plan + lint failure back in.
            prompt = (
                f"The CloudFormation template rendered from your migration plan failed "
                f"cfn-lint with the following output:\n\n{lint_output}\n\n"
                f"Here is the migration plan JSON you produced:\n```json\n{state['migration_plan_raw']}\n```\n\n"
                f"Return a corrected migration plan fixing these issues.\n\n{PLAN_JSON_SCHEMA_HINT}"
            )
        else:
            query = build_kb_query(state["cnr"], state["resource_types"])
            mapping_docs = _traced_retrieve_mapping_docs(knowledge_base, state["resource_types"], query, config)
            prompt = build_migration_plan_prompt(state["cnr"], mapping_docs)

        run_dir = _run_dir(state)
        (run_dir / "migration_prompt.txt").write_text(prompt, encoding="utf-8")

        raw_plan_text = generator.generate(prompt)
        try:
            plan = parse_migration_plan(raw_plan_text)
        except MigrationPlanError as exc:
            cli_ui.agent_result("Agent 3", "failed", f"Migration plan invalid: {exc}")
            return {
                "migration_plan_raw": raw_plan_text,
                "stopped": True,
                "stop_reason": f"Migration plan invalid: {exc}",
                "agent_log": _log("agent3_map_resources", "failed", str(exc)),
            }

        (run_dir / "migration_plan.json").write_text(
            json.dumps(dataclasses.asdict(plan), indent=2, default=str), encoding="utf-8"
        )

        mapping_table = [
            {
                "logical_id": r.logical_id,
                "source_azure_type": r.source_azure_type,
                "aws_type": r.aws_type,
            }
            for r in plan.resources
        ]
        cli_ui.agent_result("Agent 3", "ok", f"Mapped {len(mapping_table)} resource(s); plan saved for review.")
        return {
            "migration_plan_raw": raw_plan_text,
            "migration_plan": plan,
            "mapping_table": mapping_table,
            # Clear any stale stop flag left by an earlier failed self-correction attempt
            # (agent3_map_resources -> plan_approval_gate is unconditional, so a prior
            # MigrationPlanError here doesn't actually end the run but would otherwise
            # leak into the final report as a false "STOPPED" status).
            "stopped": False,
            "stop_reason": "",
            "agent_log": _log(
                "agent3_map_resources", "ok", f"Mapped {len(mapping_table)} resource(s) to AWS equivalents."
            ),
        }

    return agent3_map_resources


# ---------------------------------------------------------------------------
# Plan approval gate: mandatory human review of the migration plan before any
# CloudFormation is generated.
# ---------------------------------------------------------------------------
def plan_approval_gate(state: MigrationState) -> dict:
    cli_ui.agent_phase("Plan Approval", "Review migration plan details before CloudFormation generation.")
    plan = state["migration_plan"]
    fix_attempts = state.get("fix_attempts", 0)

    if fix_attempts > 0:
        max_attempts = state.get("max_fix_attempts", 2)
        cli_ui.warning(
            f"Self-correction retry {fix_attempts}/{max_attempts} (previous CloudFormation failed cfn-lint):"
        )
        if state.get("lint_output"):
            cli_ui.console.print(f"[dim]{state['lint_output'].strip()}[/dim]")
    cli_ui.console.print(
        cli_ui.gate_context_table(
            _with_gate_meta(
                state,
                {
                    "Fix attempt": str(fix_attempts),
                    "Max fix retries": str(state.get("max_fix_attempts", 2)),
                    "Previous lint errors": str(state.get("lint_errors", 0)),
                    "Previous lint warnings": str(state.get("lint_warnings", 0)),
                },
            ),
            title="Plan Gate Context",
        )
    )
    cli_ui.rule("Migration Plan")
    summary = (
        f"[bold]Description:[/bold] {plan.description or '(no description)'}\n"
        f"[bold]Resources:[/bold] {len(plan.resources)}  "
        f"[bold]Parameters:[/bold] {len(plan.parameters)}  "
        f"[bold]Conditions:[/bold] {len(plan.conditions)}  "
        f"[bold]Outputs:[/bold] {len(plan.outputs)}"
    )
    cli_ui.console.print(cli_ui.Panel(summary, border_style="cyan"))

    resource_rows = [
        {
            "logical_id": r.logical_id,
            "source_azure_type": r.source_azure_type,
            "aws_type": r.aws_type,
        }
        for r in plan.resources
    ]
    cli_ui.console.print(cli_ui.mapping_table(resource_rows))

    if plan.parameters:
        cli_ui.console.print("\n[bold cyan]Parameters[/bold cyan]")
        cli_ui.console.print(cli_ui.plan_parameters_table(plan.parameters))
    if plan.conditions:
        cli_ui.console.print("\n[bold cyan]Conditions[/bold cyan]")
        cli_ui.console.print(cli_ui.plan_conditions_table(plan.conditions))
    if plan.outputs:
        cli_ui.console.print("\n[bold cyan]Outputs[/bold cyan]")
        cli_ui.console.print(cli_ui.plan_outputs_table(plan.outputs))

    approved = cli_ui.confirm(
        "Approve this migration plan and proceed to CloudFormation generation?"
    )

    annotate_current_run(metadata={"gate": "plan_approval_gate", "decision": "approved" if approved else "rejected"})
    attach_feedback("plan_approved", score=1 if approved else 0)
    update = {
        "plan_confirmed": approved,
        "human_decisions": [{"gate": "plan_approval_gate", "decision": "approved" if approved else "rejected"}],
        "agent_log": _log(
            "plan_approval_gate", "ok" if approved else "stopped",
            "Human approved the migration plan." if approved else "Human rejected the migration plan.",
        ),
    }
    if not approved:
        cli_ui.agent_result("Plan Approval", "failed", "Migration plan rejected by reviewer.")
        update["stopped"] = True
        update["stop_reason"] = "Human rejected the migration plan at the plan approval gate."
    else:
        cli_ui.agent_result("Plan Approval", "ok", "Migration plan approved.")
    return update


# ---------------------------------------------------------------------------
# Agent 4: deterministic render of the migration plan into CloudFormation YAML.
# ---------------------------------------------------------------------------
def make_agent4_render(output_dir: Path):
    def agent4_render(state: MigrationState) -> dict:
        cli_ui.agent_phase("Agent 4", "Rendering CloudFormation template from approved migration plan...")
        yaml_text = generate_cloudformation(state["migration_plan"])
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"{Path(state['bicep_path']).stem}.generated.yaml"
        output_path.write_text(yaml_text, encoding="utf-8")
        cli_ui.agent_result("Agent 4", "ok", f"CloudFormation template written to {output_path}.")
        return {
            "cfn_yaml": yaml_text,
            "output_path": str(output_path),
            "agent_log": _log("agent4_render", "ok", f"Rendered CloudFormation template to {output_path}."),
        }

    return agent4_render


# ---------------------------------------------------------------------------
# Agent 5: validate the rendered template (cfn-lint).
# ---------------------------------------------------------------------------
def _count_lint_findings(lint_output: str) -> tuple[int, int]:
    """Count cfn-lint error (E####) vs. warning (W####) codes at line starts."""
    errors = len(re.findall(r"(?m)^E\d{4}", lint_output))
    warnings = len(re.findall(r"(?m)^W\d{4}", lint_output))
    return errors, warnings


def _render_validation_report(state: MigrationState, history: list[dict]) -> str:
    """Attempt-by-attempt trace of what cfn-lint checked and where it failed/passed."""
    max_attempts = state.get("max_fix_attempts", 2) + 1
    lines = [
        f"CFN VALIDATION REPORT -- run {state.get('run_id')}",
        f"Template: {state.get('output_path')}",
        "Tool: cfn-lint, run against the rendered template before any real AWS call.",
        "",
    ]
    for entry in history:
        result = "PASSED" if entry["passed"] else "FAILED"
        lines.append(
            f"Attempt {entry['attempt']}/{max_attempts} -- {result} "
            f"({entry['errors']} error(s), {entry['warnings']} warning(s))"
        )
        for out_line in entry["output"].splitlines():
            lines.append(f"  {out_line}")
        if not entry["passed"] and entry["attempt"] < max_attempts:
            lines.append("  -> Fed back to Agent 3 (LLM) for a corrected migration plan.")
        lines.append("")

    final = history[-1]
    if final["passed"]:
        retries = final["attempt"] - 1
        lines.append(
            "Final result: PASSED"
            + (f" after {retries} self-correction retry(ies)." if retries else " (first attempt).")
        )
    else:
        lines.append(f"Final result: FAILED after {final['attempt']} attempt(s) -- retries exhausted.")
    return "\n".join(lines) + "\n"


def agent5_validate_cfn(state: MigrationState) -> dict:
    attempt = state.get("fix_attempts", 0) + 1
    cli_ui.agent_phase("Agent 5", f"Running cfn-lint validation (attempt {attempt})...")
    with trace_span("cfn_lint", run_type="tool", inputs={"attempt": attempt}, tags=[f"fix_attempt:{state.get('fix_attempts', 0)}"]) as span:
        lint_passed, lint_output = run_cfn_lint(Path(state["output_path"]))
        errors, warnings = _count_lint_findings(lint_output)
        span.end(outputs={"passed": lint_passed, "error_count": errors, "warning_count": warnings})
    attempt_entry = {
        "attempt": attempt,
        "passed": lint_passed,
        "errors": errors,
        "warnings": warnings,
        "output": lint_output.strip() or "(no findings)",
    }
    history = state.get("validation_history", []) + [attempt_entry]
    (_run_dir(state) / "cfn_validation_report.txt").write_text(
        _render_validation_report(state, history), encoding="utf-8"
    )
    status = "ok" if lint_passed else "warning"
    cli_ui.agent_result(
        "Agent 5",
        "ok" if lint_passed else "warning",
        f"cfn-lint {'passed' if lint_passed else 'found issues'} ({errors} error(s), {warnings} warning(s)).",
    )
    return {
        "lint_passed": lint_passed,
        "lint_output": lint_output,
        "lint_errors": errors,
        "lint_warnings": warnings,
        "validation_history": [attempt_entry],
        "agent_log": _log("agent5_validate_cfn", status, "cfn-lint passed." if lint_passed else lint_output),
    }


# ---------------------------------------------------------------------------
# Guardrail scan gate: static security scanning of the rendered template --
# checkov (built-in CloudFormation policies) plus custom secret/IAM/network
# checks (orchestrator/guardrails.py) -- run only after cfn-lint passes.
# HIGH/CRITICAL findings require explicit human sign-off before Agent 6 can
# deploy, mirroring plan_approval_gate/stack_check_gate's human-in-the-loop
# pattern; MEDIUM/LOW findings are advisory and never block.
# ---------------------------------------------------------------------------
def _render_guardrail_report(state: MigrationState, result) -> str:
    lines = [
        f"GUARDRAIL SECURITY SCAN -- run {state.get('run_id')}",
        f"Template: {state.get('output_path')}",
        "Engines: checkov (CloudFormation policies) + custom secret/IAM/network checks.",
        "",
    ]
    if not result.checkov_ran:
        lines.append(f"checkov did not run: {result.checkov_error}")
        lines.append("")
    if not result.findings:
        lines.append("No findings.")
        return "\n".join(lines) + "\n"
    for f in result.findings:
        lines.append(f"[{f.severity}] ({f.source}:{f.check_id}) {f.resource} -- {f.message}")
        if f.guideline:
            lines.append(f"  guideline: {f.guideline}")
    lines.append("")
    blocking = [f for f in result.findings if f.severity in guardrails.BLOCKING_SEVERITIES]
    lines.append(
        f"Total: {len(result.findings)} finding(s), {len(blocking)} HIGH/CRITICAL (blocking)."
    )
    return "\n".join(lines) + "\n"


def make_guardrail_scan_gate(config: Config):
    def guardrail_scan_gate(state: MigrationState) -> dict:
        if not config.guardrail_scan_enabled:
            cli_ui.agent_result("Guardrail Scan", "ok", "Skipped (GUARDRAIL_SCAN_ENABLED=false).")
            return {
                "guardrail_findings": [],
                "agent_log": _log("guardrail_scan_gate", "ok", "Guardrail scan disabled via config."),
            }

        cli_ui.agent_phase("Guardrail Scan", "Running checkov + custom secret/IAM/network checks...")
        run_dir = _run_dir(state)
        with trace_span("guardrail_scan", run_type="tool") as span:
            result = guardrails.scan_template(Path(state["output_path"]), template_yaml=state.get("cfn_yaml"))
            span.end(outputs={
                "finding_count": len(result.findings),
                "blocking_count": len(result.blocking_findings),
                "checkov_ran": result.checkov_ran,
            })

        if not result.checkov_ran:
            cli_ui.warning(f"[Guardrail Scan] checkov could not run: {result.checkov_error}")

        report_path = run_dir / "guardrail_scan_report.txt"
        report_path.write_text(_render_guardrail_report(state, result), encoding="utf-8")
        findings_dicts = [dataclasses.asdict(f) for f in result.findings]
        base_update = {"guardrail_findings": findings_dicts, "guardrail_report_path": str(report_path)}

        blocking = result.blocking_findings
        if not result.findings:
            cli_ui.agent_result("Guardrail Scan", "ok", "No security findings.")
            attach_feedback("guardrail_gate_decision", value="clean")
            return {
                **base_update,
                "agent_log": _log("guardrail_scan_gate", "ok", "No guardrail findings."),
            }

        cli_ui.console.print(cli_ui.guardrail_findings_table(findings_dicts))
        cli_ui.console.print(
            cli_ui.gate_context_table(
                _with_gate_meta(
                    state,
                    {
                        "Total findings": str(len(result.findings)),
                        "Blocking (HIGH/CRITICAL)": str(len(blocking)),
                        "Non-blocking": str(len(result.findings) - len(blocking)),
                        "checkov executed": "yes" if result.checkov_ran else "no",
                    },
                ),
                title="Guardrail Gate Context",
            )
        )
        if not blocking:
            cli_ui.agent_result(
                "Guardrail Scan", "warning",
                f"{len(result.findings)} low/medium finding(s), none blocking; continuing.",
            )
            attach_feedback("guardrail_gate_decision", value="non_blocking")
            return {
                **base_update,
                "agent_log": _log(
                    "guardrail_scan_gate", "warning", f"{len(result.findings)} non-blocking finding(s)."
                ),
            }

        cli_ui.warning(f"{len(blocking)} HIGH/CRITICAL guardrail finding(s) -- review before deploying.")
        if not cli_ui.confirm(
            "Proceed to deployment despite these findings?"
        ):
            attach_feedback("guardrail_gate_decision", value="cancelled")
            cli_ui.agent_result("Guardrail Scan", "failed", "Deployment stopped at guardrail gate.")
            return {
                **base_update,
                "stopped": True,
                "stop_reason": f"Human stopped deployment due to {len(blocking)} HIGH/CRITICAL guardrail finding(s).",
                "human_decisions": [{"gate": "guardrail_scan_gate", "decision": "rejected"}],
                "agent_log": _log(
                    "guardrail_scan_gate", "stopped",
                    f"Human declined to proceed past {len(blocking)} blocking finding(s).",
                ),
            }
        attach_feedback("guardrail_gate_decision", value="approved_with_findings")
        cli_ui.agent_result(
            "Guardrail Scan", "warning", f"Human approved proceeding despite {len(blocking)} blocking finding(s)."
        )
        return {
            **base_update,
            "human_decisions": [{"gate": "guardrail_scan_gate", "decision": "approved_with_findings"}],
            "agent_log": _log(
                "guardrail_scan_gate", "warning",
                f"Human approved proceeding despite {len(blocking)} HIGH/CRITICAL finding(s).",
            ),
        }

    return guardrail_scan_gate


# ---------------------------------------------------------------------------
# Agent 6: deploy the validated template to AWS and verify it (real deploy).
# Fully automatic -- no human confirmation and no interactive value prompts.
# Parameter values are resolved from --params-file, CFN_PARAM_<NAME> env vars,
# source Key Vault values (NoEcho only), template defaults, and selected
# safe infrastructure-derived fallbacks (e.g. first available AZ in-region
# for AWS::EC2::AvailabilityZone::Name); anything still unresolved or invalid
# fails the run fast instead of blocking on input().
# ---------------------------------------------------------------------------
def make_agent6_deploy(config: Config):
    def agent6_deploy(state: MigrationState) -> dict:
        import boto3

        cli_ui.agent_phase("Agent 6", "Resolving deploy parameters and applying CloudFormation stack...")
        plan = state["migration_plan"]
        stack_name = _stack_name_for(state)
        cfn = boto3.client("cloudformation", region_name=config.aws_region)
        overrides = state.get("param_overrides") or {}
        source_secret_values = state.get("source_secret_values") or {}
        source_name_candidates = list(state.get("source_vault_names") or [])
        if state.get("resource_group"):
            source_name_candidates.append(state["resource_group"])

        param_values: dict[str, str | SecretValue] = {}
        parameters = []
        carried_over: list[str] = []
        named_from_source: list[str] = []
        derived_aws_values: list[str] = []
        resolutions: list[dict] = []  # {name, source} only -- never values
        ec2 = None
        with trace_span(
            "resolve_cfn_parameters", run_type="tool", inputs={"parameter_names": list((plan.parameters or {}).keys())}
        ) as param_span:
            for name, definition in (plan.parameters or {}).items():
                value, source = _resolve_param_value(
                    name, definition, overrides, source_secret_values, source_name_candidates
                )
                if value is None and _is_az_name_param(definition):
                    if ec2 is None:
                        ec2 = boto3.client("ec2", region_name=config.aws_region)
                    value, source = _resolve_first_available_az(ec2, config.aws_region)
                if value is None:
                    msg = (
                        f"No value available for required parameter '{name}'. Supply one via "
                        f"--params-file, the CFN_PARAM_{name.upper()} environment variable, a matching "
                        "source Key Vault secret, or a template Default."
                    )
                    cli_ui.agent_result("Agent 6", "failed", msg)
                    return {
                        "deploy_result": {"stack_name": stack_name, "status": "FAILED", "error": msg},
                        "agent_log": _log("agent6_deploy", "failed", msg),
                        "stopped": True,
                        "stop_reason": msg,
                    }
                error = _validate_param_value(value, definition)
                if error is not None:
                    msg = (
                        f"Parameter '{name}' value from {source} is invalid: {error}. Fix it via "
                        f"--params-file or the CFN_PARAM_{name.upper()} environment variable."
                    )
                    cli_ui.agent_result("Agent 6", "failed", msg)
                    return {
                        "deploy_result": {"stack_name": stack_name, "status": "FAILED", "error": msg},
                        "agent_log": _log("agent6_deploy", "failed", msg),
                        "stopped": True,
                        "stop_reason": msg,
                    }
                if definition.get("NoEcho"):
                    register_secret_values(value)
                    state_value: str | SecretValue = SecretValue(value)
                    api_value = state_value.reveal()
                else:
                    state_value = value
                    api_value = value
                if source == "source-keyvault":
                    carried_over.append(name)
                elif source == "source-name":
                    named_from_source.append(name)
                elif source == "region-availability-zone":
                    derived_aws_values.append(name)
                resolutions.append({"name": name, "source": source})
                param_values[name] = state_value
                parameters.append({"ParameterKey": name, "ParameterValue": api_value})
            param_span.end(outputs={"resolutions": resolutions})
            cli_ui.agent_result("Agent 6", "ok", f"Resolved {len(parameters)} deployment parameter(s).")

        template_body = state["cfn_yaml"]
        # stack_check_gate decides create vs. update ahead of time; fall back to a
        # fresh existence check if the gate was somehow skipped.
        action = state.get("stack_action") or ("update" if _stack_exists(cfn, stack_name) else "create")
        try:
            if action == "update":
                cfn.update_stack(
                    StackName=stack_name, TemplateBody=template_body, Parameters=parameters,
                    Capabilities=["CAPABILITY_IAM", "CAPABILITY_NAMED_IAM"],
                )
                waiter = cfn.get_waiter("stack_update_complete")
            else:
                cfn.create_stack(
                    StackName=stack_name, TemplateBody=template_body, Parameters=parameters,
                    Capabilities=["CAPABILITY_IAM", "CAPABILITY_NAMED_IAM"],
                )
                waiter = cfn.get_waiter("stack_create_complete")
            waiter.wait(StackName=stack_name)
        except Exception as exc:  # noqa: BLE001 - surfaced to report, not raised
            cli_ui.agent_result("Agent 6", "failed", f"Deployment failed: {exc}")
            return {
                "deploy_result": {"stack_name": stack_name, "status": "FAILED", "error": str(exc)},
                "agent_log": _log("agent6_deploy", "failed", f"Deploy failed: {exc}"),
            }

        description = cfn.describe_stacks(StackName=stack_name)["Stacks"][0]
        deploy_result = {
            "stack_name": stack_name,
            "stack_id": description["StackId"],
            "status": description["StackStatus"],
        }

        message = f"Stack '{stack_name}' deployed with status {deploy_result['status']}."
        if carried_over:
            message += f" Carried over real values from the source Key Vault for: {', '.join(carried_over)}."
        if named_from_source:
            message += f" Used the source resource group/Key Vault name for: {', '.join(named_from_source)}."
        if derived_aws_values:
            message += (
                f" Auto-selected first available AZ in {config.aws_region} for: "
                f"{', '.join(derived_aws_values)}."
            )
        cli_ui.agent_result("Agent 6", "ok", f"Deployment status: {deploy_result['status']}.")
        return {
            "deploy_result": deploy_result,
            "param_values": param_values,
            "agent_log": _log("agent6_deploy", "ok", message),
        }

    return agent6_deploy


def make_agent6_verify(config: Config):
    """Run post-deploy smoke checks by resource type after Agent 6 succeeds.

    This node is intentionally additive: deployment has already completed, and
    these checks help catch obvious runtime/configuration issues early.
    """

    def agent6_verify(state: MigrationState) -> dict:
        deploy_result = state.get("deploy_result") or {}
        if not deploy_result or deploy_result.get("status") in (None, "FAILED"):
            return {
                "agent_log": _log(
                    "agent6_verify",
                    "warning",
                    "Post-deploy verification skipped because deployment did not complete successfully.",
                )
            }

        cli_ui.agent_phase("Verify", "Running resource-type post-deploy smoke checks...")
        verify_result = _verify_post_deploy(
            config=config,
            plan=state["migration_plan"],
            param_values=state.get("param_values", {}),
            stack_name=deploy_result.get("stack_name", ""),
        )

        checks_run = sum(len(v) for v in verify_result.values() if isinstance(v, list))
        failures = 0
        for secret_check in verify_result.get("secrets_checked", []):
            if secret_check.get("exists") is False:
                failures += 1
        for vpc_check in verify_result.get("vpc_reachability", []):
            if vpc_check.get("reachable") is False:
                failures += 1
        for invoke_check in verify_result.get("lambda_invocations", []):
            if invoke_check.get("success") is False:
                failures += 1

        status = "ok" if failures == 0 else "warning"
        cli_ui.agent_result(
            "Verify",
            status,
            f"Completed {checks_run} smoke check(s); {failures} issue(s) detected.",
        )
        return {
            "verify_result": verify_result,
            "agent_log": _log(
                "agent6_verify",
                status,
                f"Ran {checks_run} post-deploy smoke check(s); {failures} issue(s).",
            ),
        }

    return agent6_verify


def _stack_name_for(state: MigrationState) -> str:
    return f"migrated-{Path(state['bicep_path']).stem}"


def _resolve_param_value(
    name: str,
    definition: dict,
    overrides: dict[str, str],
    source_secret_values: dict[str, SecretValue],
    source_name_candidates: list[str],
) -> tuple[str | None, str]:
    """Resolve a CFN parameter value with no human input, in priority order:
    --params-file override, CFN_PARAM_<NAME> env var, a matching real value
    fetched from the source Key Vault (NoEcho params only), template Default
    (never a *non-empty* default for NoEcho/secret params -- but an explicit
    "" default, the 'optional, empty means not provided' idiom, is always
    honored regardless of NoEcho), then -- for naming/prefix-style params
    only (e.g. 'SecretNamePrefix') with no Default -- the source resource
    group/Key Vault name. Returns (value, source); value is None when nothing
    could be resolved, which fails the run fast.
    """
    if name in overrides:
        return overrides[name], "params-file"
    env_value = os.environ.get(f"CFN_PARAM_{name.upper()}")
    if env_value is not None:
        return env_value, "environment"
    no_echo = bool(definition.get("NoEcho"))
    if no_echo:
        matched = _match_source_secret(name, source_secret_values)
        if matched is not None:
            return matched, "source-keyvault"
    default = definition.get("Default")
    if default is not None and (not no_echo or default == ""):
        return str(default), "template default"
    if not no_echo and source_name_candidates and _looks_like_name_prefix_param(name):
        return source_name_candidates[0], "source-name"
    return None, "missing"


_NAME_PREFIX_TOKENS = ("prefix", "namespace")


def _looks_like_name_prefix_param(name: str) -> bool:
    """Heuristic: does this parameter name look like a naming/prefix param
    (e.g. 'SecretNamePrefix'), as opposed to an arbitrary required value we
    can't safely guess (e.g. a KMS key ID)?
    """
    normalized = _normalize_key(name)
    return any(token in normalized for token in _NAME_PREFIX_TOKENS)


def _is_az_name_param(definition: dict) -> bool:
    return str(definition.get("Type", "")).strip() == "AWS::EC2::AvailabilityZone::Name"


def _resolve_first_available_az(ec2_client, region: str) -> tuple[str | None, str]:
    """Pick a stable Availability Zone fallback for AZ-name CFN parameters.

    Uses the first lexicographically sorted available AZ in the configured region.
    """
    try:
        response = ec2_client.describe_availability_zones(
            Filters=[
                {"Name": "region-name", "Values": [region]},
                {"Name": "state", "Values": ["available"]},
            ],
            AllAvailabilityZones=False,
        )
    except Exception:
        return None, "missing"

    zones = sorted(
        zone.get("ZoneName", "")
        for zone in response.get("AvailabilityZones", [])
        if zone.get("ZoneName")
    )
    if not zones:
        return None, "missing"
    return zones[0], "region-availability-zone"


def _normalize_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _match_source_secret(name: str, source_secret_values: dict[str, SecretValue]) -> str | None:
    """Match a CFN parameter name to a fetched Key Vault secret by normalized
    name (case/hyphen/underscore-insensitive), e.g. 'DbPassword' <-> 'db-password'.
    """
    target = _normalize_key(name)
    for secret_name, value in source_secret_values.items():
        if _normalize_key(secret_name) == target:
            return reveal_secret_value(value)
    return None


def _validate_param_value(value: str, definition: dict) -> str | None:
    """Check a candidate parameter value against its CFN constraints client-side,
    so a bad value fails fast with a re-prompt instead of a wasted Create/UpdateStack
    call that can leave the stack in ROLLBACK_COMPLETE.
    """
    min_length = definition.get("MinLength")
    if min_length is not None and len(value) < int(min_length):
        return f"must be at least {min_length} character(s) long"
    max_length = definition.get("MaxLength")
    if max_length is not None and len(value) > int(max_length):
        return f"must be at most {max_length} character(s) long"
    pattern = definition.get("AllowedPattern")
    if pattern and re.fullmatch(pattern, value) is None:
        return f"must match pattern {pattern}"
    allowed_values = definition.get("AllowedValues")
    if allowed_values and value not in allowed_values:
        return f"must be one of {allowed_values}"
    return None


def _stack_exists(cfn, stack_name: str) -> bool:
    try:
        cfn.describe_stacks(StackName=stack_name)
        return True
    except Exception:
        return False


def _stack_status(cfn, stack_name: str) -> str | None:
    try:
        return cfn.describe_stacks(StackName=stack_name)["Stacks"][0]["StackStatus"]
    except Exception:
        return None


def _delete_stack_and_wait(cfn, stack_name: str) -> None:
    cfn.delete_stack(StackName=stack_name)
    cfn.get_waiter("stack_delete_complete").wait(StackName=stack_name)


# Stack statuses where CloudFormation refuses UpdateStack outright (e.g. the
# ROLLBACK_COMPLETE case seen after a failed create) -- must be deleted first.
_STACK_BLOCKS_UPDATE = {"ROLLBACK_COMPLETE", "CREATE_FAILED", "DELETE_FAILED"}


# ---------------------------------------------------------------------------
# Stack conflict gate: verify whether the target CFN stack already exists
# before agent6 mutates anything, and let the human choose update vs. delete.
# ---------------------------------------------------------------------------
def make_stack_check_gate(config: Config):
    def stack_check_gate(state: MigrationState) -> dict:
        import boto3

        cli_ui.agent_phase("Stack Check", "Checking whether the target CloudFormation stack already exists...")
        stack_name = _stack_name_for(state)
        cfn = boto3.client("cloudformation", region_name=config.aws_region)
        status = _stack_status(cfn, stack_name)

        if status is None:
            cli_ui.agent_result("Stack Check", "ok", f"No existing stack found for '{stack_name}'.")
            return {
                "stack_action": "create",
                "agent_log": _log("stack_check_gate", "ok", f"No existing stack '{stack_name}' -- will create."),
            }

        if status.endswith("_IN_PROGRESS"):
            cli_ui.agent_result("Stack Check", "failed", f"Stack '{stack_name}' is busy ({status}).")
            return {
                "stopped": True,
                "stop_reason": f"Stack '{stack_name}' has an operation in progress ({status}); try again later.",
                "agent_log": _log("stack_check_gate", "stopped", f"Stack busy ({status})."),
            }

        if status in _STACK_BLOCKS_UPDATE:
            cli_ui.warning(f"Stack '{stack_name}' exists in state {status}, which cannot be updated.")
            cli_ui.console.print(
                cli_ui.gate_context_table(
                    _with_gate_meta(
                        state,
                        {
                            "Stack": stack_name,
                            "Status": status,
                            "Action required": "Delete and recreate",
                            "Region": config.aws_region,
                        },
                    ),
                    title="Stack Conflict Context",
                )
            )
            if not cli_ui.confirm(
                "Delete this stack and recreate it?"
            ):
                attach_feedback("stack_gate_decision", value="cancelled")
                cli_ui.agent_result("Stack Check", "failed", f"Cancelled; stack '{stack_name}' remains in {status}.")
                return {
                    "stopped": True,
                    "stop_reason": f"Human declined to delete stack '{stack_name}' stuck in {status}.",
                    "human_decisions": [{"gate": "stack_check_gate", "decision": "rejected"}],
                    "agent_log": _log("stack_check_gate", "stopped", f"Declined deletion of stack in {status}."),
                }
            _delete_stack_and_wait(cfn, stack_name)
            attach_feedback("stack_gate_decision", value="delete_recreate")
            cli_ui.agent_result("Stack Check", "warning", f"Deleted '{stack_name}' in {status}; recreating.")
            return {
                "stack_action": "create",
                "human_decisions": [{"gate": "stack_check_gate", "decision": "delete_recreate"}],
                "agent_log": _log(
                    "stack_check_gate", "ok", f"Deleted stack '{stack_name}' (was {status}); will recreate."
                ),
            }

        cli_ui.step("Stack check", f"Stack '{stack_name}' already exists (status: {status}).")
        cli_ui.console.print(
            cli_ui.gate_context_table(
                _with_gate_meta(
                    state,
                    {
                        "Stack": stack_name,
                        "Status": status,
                        "Available actions": "update / delete+recreate / cancel",
                        "Default": "cancel",
                    },
                ),
                title="Stack Action Context",
            )
        )
        answer = cli_ui.select_option(
            "Choose stack action",
            {
                "u": "Update existing stack",
                "d": "Delete and recreate stack",
                "c": "Cancel deployment",
            },
            default="c",
        )
        if answer == "u":
            attach_feedback("stack_gate_decision", value="update")
            cli_ui.agent_result("Stack Check", "ok", f"Proceeding with stack update for '{stack_name}'.")
            return {
                "stack_action": "update",
                "human_decisions": [{"gate": "stack_check_gate", "decision": "update"}],
                "agent_log": _log("stack_check_gate", "ok", f"Human chose to update existing stack '{stack_name}'."),
            }
        if answer == "d":
            _delete_stack_and_wait(cfn, stack_name)
            attach_feedback("stack_gate_decision", value="delete_recreate")
            cli_ui.agent_result("Stack Check", "warning", f"Deleted '{stack_name}'; proceeding with recreate.")
            return {
                "stack_action": "create",
                "human_decisions": [{"gate": "stack_check_gate", "decision": "delete_recreate"}],
                "agent_log": _log(
                    "stack_check_gate", "ok", f"Human chose to delete and recreate stack '{stack_name}'."
                ),
            }
        attach_feedback("stack_gate_decision", value="cancelled")
        cli_ui.agent_result("Stack Check", "failed", "Deployment cancelled at stack conflict gate.")
        return {
            "stopped": True,
            "stop_reason": f"Human cancelled deployment; stack '{stack_name}' already exists.",
            "human_decisions": [{"gate": "stack_check_gate", "decision": "cancelled"}],
            "agent_log": _log("stack_check_gate", "stopped", "Human cancelled at stack conflict gate."),
        }

    return stack_check_gate


def _verify_post_deploy(
    config: Config,
    plan,
    param_values: dict[str, str | SecretValue],
    stack_name: str,
) -> dict:
    """Run additive post-deploy smoke tests, scoped by deployed resource types."""
    import boto3

    cfn = boto3.client("cloudformation", region_name=config.aws_region)
    stack_resources_by_type = _stack_resources_by_type(cfn, stack_name)

    verify_result = {
        "secrets_checked": _verify_secrets(config, plan, param_values),
        "vpc_reachability": _verify_vpc_reachability(config, stack_resources_by_type),
        "lambda_invocations": _verify_lambda_invocations(config, stack_resources_by_type),
    }
    return verify_result


def _stack_resources_by_type(cfn_client, stack_name: str) -> dict[str, list[dict[str, str]]]:
    resources = cfn_client.describe_stack_resources(StackName=stack_name).get("StackResources", [])
    grouped: dict[str, list[dict[str, str]]] = {}
    for resource in resources:
        resource_type = str(resource.get("ResourceType") or "")
        grouped.setdefault(resource_type, []).append(
            {
                "logical_id": str(resource.get("LogicalResourceId") or ""),
                "physical_id": str(resource.get("PhysicalResourceId") or ""),
            }
        )
    return grouped


def _verify_secrets(config: Config, plan, param_values: dict[str, str | SecretValue]) -> list[dict]:
    """Smoke-test: confirm each planned SecretsManager secret exists (never reads value)."""
    import boto3

    secrets_client = boto3.client("secretsmanager", region_name=config.aws_region)
    checked = []
    for resource in plan.resources:
        if resource.aws_type != "AWS::SecretsManager::Secret":
            continue
        name = _resolve_name(resource.properties.get("Name"), param_values)
        try:
            if name is None or "***" in name:
                checked.append({"logical_id": resource.logical_id, "exists": "unknown (secret-derived or unresolvable name)"})
                continue
            secrets_client.describe_secret(SecretId=name)
            checked.append({"logical_id": resource.logical_id, "name": name, "exists": True})
        except Exception as exc:  # noqa: BLE001
            checked.append({"logical_id": resource.logical_id, "name": name, "exists": False, "error": str(exc)})
    return checked


def _verify_vpc_reachability(config: Config, stack_resources_by_type: dict[str, list[dict[str, str]]]) -> list[dict]:
    """Basic VPC egress smoke test: each deployed VPC should have an active
    default route to an internet/NAT/transit target in at least one route table.
    """
    import boto3

    vpcs = stack_resources_by_type.get("AWS::EC2::VPC", [])
    if not vpcs:
        return []

    ec2 = boto3.client("ec2", region_name=config.aws_region)
    results: list[dict] = []
    for vpc in vpcs:
        vpc_id = vpc.get("physical_id")
        logical_id = vpc.get("logical_id")
        if not vpc_id:
            results.append({"logical_id": logical_id, "vpc_id": vpc_id, "reachable": False, "error": "missing VPC id"})
            continue
        try:
            response = ec2.describe_route_tables(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])
        except Exception as exc:  # noqa: BLE001
            results.append({"logical_id": logical_id, "vpc_id": vpc_id, "reachable": False, "error": str(exc)})
            continue

        route_target = None
        for table in response.get("RouteTables", []):
            for route in table.get("Routes", []):
                if route.get("DestinationCidrBlock") != "0.0.0.0/0":
                    continue
                if route.get("State") not in (None, "active"):
                    continue
                target = (
                    route.get("GatewayId")
                    or route.get("NatGatewayId")
                    or route.get("TransitGatewayId")
                    or route.get("EgressOnlyInternetGatewayId")
                )
                if target:
                    route_target = target
                    break
            if route_target:
                break

        results.append(
            {
                "logical_id": logical_id,
                "vpc_id": vpc_id,
                "reachable": route_target is not None,
                "default_route_target": route_target,
            }
        )
    return results


def _verify_lambda_invocations(config: Config, stack_resources_by_type: dict[str, list[dict[str, str]]]) -> list[dict]:
    """Invoke each deployed Lambda function with an empty JSON payload."""
    import boto3

    functions = stack_resources_by_type.get("AWS::Lambda::Function", [])
    if not functions:
        return []

    lambda_client = boto3.client("lambda", region_name=config.aws_region)
    results: list[dict] = []
    for function in functions:
        function_name = function.get("physical_id")
        logical_id = function.get("logical_id")
        if not function_name:
            results.append(
                {
                    "logical_id": logical_id,
                    "function_name": function_name,
                    "success": False,
                    "error": "missing function name",
                }
            )
            continue
        try:
            response = lambda_client.invoke(
                FunctionName=function_name,
                InvocationType="RequestResponse",
                Payload=b"{}",
            )
            status_code = int(response.get("StatusCode", 0))
            function_error = response.get("FunctionError")
            success = 200 <= status_code < 300 and not function_error
            results.append(
                {
                    "logical_id": logical_id,
                    "function_name": function_name,
                    "status_code": status_code,
                    "function_error": function_error,
                    "success": success,
                }
            )
        except Exception as exc:  # noqa: BLE001
            results.append(
                {
                    "logical_id": logical_id,
                    "function_name": function_name,
                    "success": False,
                    "error": str(exc),
                }
            )
    return results


def _resolve_name(name: Any, param_values: dict[str, str | SecretValue]) -> str | None:
    """Resolve a CFN `Name` property (plain string, Fn::Sub, or Ref) to its deployed value."""
    if isinstance(name, str):
        return name
    if not isinstance(name, dict):
        return None
    if "Fn::Sub" in name and isinstance(name["Fn::Sub"], str):
        return re.sub(
            r"\$\{(\w+)\}",
            lambda m: str(param_values.get(m.group(1), m.group(0))),
            name["Fn::Sub"],
        )
    if "Ref" in name:
        value = param_values.get(name["Ref"])
        return None if value is None else str(value)
    return None


# ---------------------------------------------------------------------------
# Agent 7: write the migration report from the full graph state.
# ---------------------------------------------------------------------------
def make_agent7_report(reports_dir: Path):
    def agent7_report(state: MigrationState) -> dict:
        cli_ui.agent_phase("Agent 7", "Writing migration report and final run summary...")
        run_id = state["run_id"]
        run_dir = reports_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)

        validation_history = state.get("validation_history", [])
        completed = not state.get("stopped")
        attach_feedback("run_completed", score=1 if completed else 0, on_trace_root=True)
        if validation_history:
            attach_feedback("lint_passed_first_try", score=1 if validation_history[0]["passed"] else 0, on_trace_root=True)
        attach_feedback("fix_attempts_used", value=state.get("fix_attempts", 0), on_trace_root=True)
        if state.get("deploy_result"):
            deploy_ok = state["deploy_result"].get("status") not in (None, "FAILED")
            attach_feedback("deploy_succeeded", score=1 if deploy_ok else 0, on_trace_root=True)

        report_path = run_dir / "report.md"
        report_path.write_text(_render_report(state, get_trace_url()), encoding="utf-8")
        cli_ui.agent_result("Agent 7", "ok", f"Report written to {report_path}.")

        # Local import: evaluation.py imports node factories from this module at
        # module scope, so importing it back at module scope here would be circular.
        from .evaluation import log_run_outcome, write_calibration_report

        history_path = reports_dir / "history.jsonl"
        log_run_outcome(state, history_path=history_path)
        write_calibration_report(history_path=history_path)

        return {
            "report_path": str(report_path),
            "agent_log": _log("agent7_report", "ok", f"Report written to {report_path}."),
        }

    return agent7_report


_AGENT_LABELS = {
    "agent0_export_resource_group": "Agent 0 — Export Resource Group",
    "agent1_validate": "Agent 1 — Validate",
    "agent2_build_cnr": "Agent 2 — Build Cloud-Neutral Representation",
    "agent3_map_resources": "Agent 3 — Map Resources",
    "plan_approval_gate": "Plan Approval Gate",
    "agent4_render": "Agent 4 — Render CloudFormation",
    "agent5_validate_cfn": "Agent 5 — Validate CloudFormation",
    "guardrail_scan_gate": "Guardrail Security Scan",
    "stack_check_gate": "Stack Check Gate",
    "agent6_deploy": "Agent 6 — Deploy",
    "agent6_verify": "Verify — Post-Deployment Smoke Tests",
    "agent7_report": "Agent 7 — Report",
}
_STATUS_LABELS = {"ok": "✓ OK", "warning": "⚠ Warning", "stopped": "⏹ Stopped", "failed": "✗ Failed"}
_DEPLOY_FIELD_LABELS = {"stack_name": "Stack name", "stack_id": "Stack ID", "status": "Status", "error": "Error"}


def _render_report(state: MigrationState, trace_url: str | None = None) -> str:
    stopped = bool(state.get("stopped"))
    status_line = f"✗ Stopped — {state['stop_reason']}" if stopped else "✓ Completed"
    lines = [
        f"# Migration Report — Run {state['run_id']}",
        "",
        "## Summary",
        "",
        f"- **Status:** {status_line}",
        f"- **Generated:** {datetime.datetime.now().isoformat(timespec='seconds')}",
        f"- **Source:** {state.get('bicep_path') or state.get('resource_group') or '(unknown)'}",
    ]
    if trace_url:
        lines.append(f"- **LangSmith trace:** {trace_url}")
    lines += [
        "",
        "## Agent Log",
        "",
        "| Agent | Status | Message |",
        "|---|---|---|",
    ]
    for entry in state.get("agent_log", []):
        message = entry["message"].replace("\n", " ").replace("|", "\\|")[:300]
        agent_label = _AGENT_LABELS.get(entry["agent"], entry["agent"])
        status_label = _STATUS_LABELS.get(entry["status"], entry["status"])
        lines.append(f"| {agent_label} | {status_label} | {message} |")

    lines += ["", "## Resource Types", ""]
    lines.append(f"- **Supported and processed:** {', '.join(state.get('resource_types', [])) or '(none)'}")
    if state.get("unsupported_types"):
        lines.append(f"- **Unsupported (skipped):** {', '.join(state['unsupported_types'])}")
    if state.get("noop_types"):
        lines.append(f"- **Implicit defaults (dropped, no AWS equivalent):** {', '.join(state['noop_types'])}")
    if state.get("foldable_types"):
        lines.append(f"- **Folded into parent resource properties:** {', '.join(state['foldable_types'])}")

    if state.get("mapping_table"):
        lines += ["", "## Resource Mapping", "", "| Logical ID | Azure Type | AWS Type |", "|---|---|---|"]
        for m in state["mapping_table"]:
            lines.append(f"| {m['logical_id']} | {m['source_azure_type']} | {m['aws_type']} |")

    if "plan_confirmed" in state:
        lines += ["", "## Plan Approval", "", f"- **Approved:** {'Yes' if state['plan_confirmed'] else 'No'}"]

    if state.get("output_path"):
        lines += ["", "## Generated Template", "", f"- **Path:** {state['output_path']}"]

    if state.get("validation_history"):
        max_attempts = state.get("max_fix_attempts", 2) + 1
        lines += [
            "",
            "## Validation",
            "",
            "`cfn-lint` run against the rendered template before any real AWS call:",
            "",
        ]
        for entry in state["validation_history"]:
            result = "Passed" if entry["passed"] else "Failed"
            lines.append(
                f"- Attempt {entry['attempt']}/{max_attempts}: **{result}** "
                f"({entry['errors']} error(s), {entry['warnings']} warning(s))"
            )
            if not entry["passed"]:
                first_line = entry["output"].splitlines()[0] if entry["output"] else ""
                lines.append(f"  - {first_line}")
                lines.append("  - Fed back to Agent 3 for a corrected migration plan.")
        lines.append("")
        lines.append("Full per-attempt output: `cfn_validation_report.txt` in this run's folder.")

    if "guardrail_findings" in state:
        findings = state["guardrail_findings"]
        lines += ["", "## Guardrail Security Scan", "", "`checkov` + custom secret/IAM/network checks:", ""]
        if not findings:
            lines.append("- No findings.")
        else:
            blocking = [f for f in findings if f["severity"] in guardrails.BLOCKING_SEVERITIES]
            lines.append(f"- **{len(findings)}** finding(s), **{len(blocking)}** HIGH/CRITICAL (blocking).")
            lines += ["", "| Severity | Source | Check | Resource | Message |", "|---|---|---|---|---|"]
            for f in findings:
                message = f["message"].replace("\n", " ").replace("|", "\\|")[:200]
                lines.append(
                    f"| {f['severity']} | {f['source']} | {f['check_id']} | {f['resource']} | {message} |"
                )
        lines.append("")
        lines.append("Full report: `guardrail_scan_report.txt` in this run's folder.")

    if state.get("deploy_result"):
        lines += ["", "## Deployment", ""]
        for k, v in state["deploy_result"].items():
            label = _DEPLOY_FIELD_LABELS.get(k, k.replace("_", " ").capitalize())
            lines.append(f"- **{label}:** {v}")

    if state.get("verify_result"):
        lines += ["", "## Post-Deployment Verification", ""]
        secrets_checked = state["verify_result"].get("secrets_checked", [])
        if secrets_checked:
            lines += ["", "### Secrets Manager Existence", ""]
            for check in secrets_checked:
                lines.append(f"- {check}")

        vpc_reachability = state["verify_result"].get("vpc_reachability", [])
        if vpc_reachability:
            lines += ["", "### VPC Reachability (Default Route)", ""]
            for check in vpc_reachability:
                lines.append(f"- {check}")

        lambda_invocations = state["verify_result"].get("lambda_invocations", [])
        if lambda_invocations:
            lines += ["", "### Lambda Invocation", ""]
            for check in lambda_invocations:
                lines.append(f"- {check}")

    return "\n".join(lines) + "\n"
