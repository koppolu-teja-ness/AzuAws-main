"""Phase 3: offline-friendly evaluation harness for Agent 3 and the Agent 1->5
pipeline trajectory. Two eval "targets" (see evals/run_eval.py for the CLI):

- target_agent3: RAG + BedrockGenerator -> MigrationPlan, in isolation. Honors
  RAG_ENABLED/RAG_TOP_K/BEDROCK_MODEL_ID/prompt version via the `Config` passed
  into the factory.
- target_pipeline: the REAL Agent 1-5 LangGraph nodes (including the Agent 5 ->
  Agent 3 retry loop), via a dedicated eval-only graph (build_eval_graph) that
  simply never defines stack_check_gate/agent6_deploy/agent7_report nodes --
  so it is structurally, not just behaviorally, incapable of deploying
  anything. plan_approval_gate is replaced with an auto-approving stub (no
  human input()).

Both targets take the dataset example's `inputs` dict and return a plain
`outputs` dict for the evaluators below to score. Evaluator functions use
LangSmith's flexible signature binding (`outputs`, `reference_outputs`
parameter names) -- see langsmith.evaluation.evaluator._normalize_evaluator_func.
"""
from __future__ import annotations

import dataclasses
import datetime
import json
import re
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from langgraph.graph import END, StateGraph

from .agents import (
    _validate_param_value,
    agent2_build_cnr,
    agent5_validate_cfn,
    make_agent1_validate,
    make_agent3_map_resources,
    make_agent4_render,
)
from .config import Config
from .generator import BedrockGenerator, Generator
from .knowledge_base import KnowledgeBase
from .migration_plan import MigrationPlanError, parse_migration_plan
from .observability import trace_span
from .prompts import get_prompt_module
from .rag import build_kb_query, retrieve_mapping_docs_with_diagnostics
from .state import MigrationState

JUDGE_MODEL_ID_DEFAULT = "amazon.nova-lite-v1:0"  # deliberately different from the generator's default model

# ---------------------------------------------------------------------------
# target_agent3
# ---------------------------------------------------------------------------
def _reconstruct_cnr(cnr_dict: dict) -> SimpleNamespace:
    """Rebuilds just enough of a CloudNeutralRepresentation (attribute access,
    `vars()`-able) from the dataset's plain-dict `inputs.cnr` for
    build_kb_query()/build_migration_plan_prompt(), without a dependency on
    orchestrator.cloud_neutral's dataclasses."""
    parameters = [SimpleNamespace(**p) for p in cnr_dict.get("parameters", [])]
    resources = [SimpleNamespace(**r) for r in cnr_dict.get("resources", [])]
    return SimpleNamespace(parameters=parameters, resources=resources, outputs=cnr_dict.get("outputs", {}))


def build_target_agent3(knowledge_base: KnowledgeBase, config: Config, prompt_version: str = "v1") -> Callable[[dict], dict]:
    prompt_module = get_prompt_module(prompt_version)
    generator = BedrockGenerator(config, system_prompt=prompt_module.SYSTEM_PROMPT)

    def target_agent3(inputs: dict) -> dict:
        cnr = _reconstruct_cnr(inputs.get("cnr", {}))
        resource_types = inputs.get("resource_types", [])
        query = build_kb_query(cnr, resource_types)
        mapping_docs, rag_diagnostics = retrieve_mapping_docs_with_diagnostics(knowledge_base, resource_types, query, config)
        prompt = prompt_module.build_migration_plan_prompt(cnr, mapping_docs)

        start = time.monotonic()
        try:
            raw_text = generator.generate(prompt)
        except Exception as exc:  # noqa: BLE001 -- surfaced as a failing eval row, not a crash
            return {
                "migration_plan": None, "migration_plan_raw": None,
                "error": str(exc), "latency_ms": (time.monotonic() - start) * 1000,
                "rag_diagnostics": rag_diagnostics,
            }
        latency_ms = (time.monotonic() - start) * 1000

        try:
            plan = parse_migration_plan(raw_text)
        except MigrationPlanError as exc:
            return {
                "migration_plan": None, "migration_plan_raw": raw_text,
                "error": str(exc), "latency_ms": latency_ms,
                "rag_diagnostics": rag_diagnostics,
            }
        return {
            "migration_plan": dataclasses.asdict(plan),
            "migration_plan_raw": raw_text,
            "error": None,
            "latency_ms": latency_ms,
            "rag_diagnostics": rag_diagnostics,
        }


    return target_agent3


# ---------------------------------------------------------------------------
# target_pipeline -- a trimmed eval-only graph, Agent 1 through Agent 5 only.
# ---------------------------------------------------------------------------
_ALLOWED_PIPELINE_NODES = {
    "agent1_validate", "agent2_build_cnr", "agent3_map_resources",
    "plan_approval_gate", "agent4_render", "agent5_validate_cfn", "bump_fix_attempts",
}


def _auto_approve_plan_gate(state: MigrationState) -> dict:
    """Gate stub injected by the eval harness -- never asks a human, never used
    for real deploys (this graph has no deploy node to reach)."""
    return {
        "plan_confirmed": True,
        "agent_log": [{
            "agent": "plan_approval_gate", "status": "ok",
            "message": "Auto-approved by eval harness stub.",
        }],
    }


def _bump_fix_attempts(state: MigrationState) -> dict:
    return {"fix_attempts": state.get("fix_attempts", 0) + 1}


def _route_after_validate(state: MigrationState) -> str:
    return "stop" if state.get("stopped") else "continue"


def _route_after_plan_gate(state: MigrationState) -> str:
    return "render" if state.get("plan_confirmed") else "stop"


def _route_after_lint(state: MigrationState) -> str:
    if state.get("lint_passed"):
        return "done"
    if state.get("fix_attempts", 0) < state.get("max_fix_attempts", 2):
        return "retry"
    return "done"  # retries exhausted -- stop here, same as the real graph's lint_give_up (no deploy either way)


def build_eval_graph(knowledge_base: KnowledgeBase, generator: Generator, config: Config, output_dir: Path):
    """Agent 1 -> Agent 5 (incl. the retry loop) only. stack_check_gate,
    agent6_deploy and agent7_report are never added as nodes, so this graph
    cannot reach them -- not a behavioral promise, a structural one."""
    graph = StateGraph(MigrationState)
    graph.add_node("agent1_validate", make_agent1_validate(knowledge_base))
    graph.add_node("agent2_build_cnr", agent2_build_cnr)
    graph.add_node("agent3_map_resources", make_agent3_map_resources(knowledge_base, generator, config))
    graph.add_node("plan_approval_gate", _auto_approve_plan_gate)
    graph.add_node("agent4_render", make_agent4_render(output_dir))
    graph.add_node("agent5_validate_cfn", agent5_validate_cfn)
    graph.add_node("bump_fix_attempts", _bump_fix_attempts)

    graph.set_entry_point("agent1_validate")
    graph.add_conditional_edges("agent1_validate", _route_after_validate, {"stop": END, "continue": "agent2_build_cnr"})
    graph.add_edge("agent2_build_cnr", "agent3_map_resources")
    graph.add_edge("agent3_map_resources", "plan_approval_gate")
    graph.add_conditional_edges("plan_approval_gate", _route_after_plan_gate, {"render": "agent4_render", "stop": END})
    graph.add_edge("agent4_render", "agent5_validate_cfn")
    graph.add_conditional_edges("agent5_validate_cfn", _route_after_lint, {"retry": "bump_fix_attempts", "done": END})
    graph.add_edge("bump_fix_attempts", "agent3_map_resources")
    return graph.compile()


def build_target_pipeline(knowledge_base: KnowledgeBase, config: Config, output_root: Path) -> Callable[[dict], dict]:
    def target_pipeline(inputs: dict) -> dict:
        run_id = uuid.uuid4().hex[:8]
        run_dir = output_root / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        arm_path = run_dir / "arm.json"
        arm_path.write_text(json.dumps(inputs.get("arm_template", {})), encoding="utf-8")

        generator = BedrockGenerator(config)
        graph = build_eval_graph(knowledge_base, generator, config, run_dir)
        state = {
            "bicep_path": str(arm_path),
            "output_dir": str(run_dir),
            "run_id": run_id,
            "dry_run": False,
            "fix_attempts": 0,
            "max_fix_attempts": config.max_fix_attempts,
            "param_overrides": {},
            "agent_log": [],
        }
        final_state = graph.invoke(state, config={"recursion_limit": 50})
        plan = final_state.get("migration_plan")
        return {
            "stopped": bool(final_state.get("stopped")),
            "stop_reason": final_state.get("stop_reason"),
            "lint_passed": final_state.get("lint_passed"),
            "fix_attempts": final_state.get("fix_attempts", 0),
            "validation_history": final_state.get("validation_history", []),
            "mapping_table": final_state.get("mapping_table", []),
            "migration_plan": dataclasses.asdict(plan) if plan is not None else None,
            "node_path": [entry["agent"] for entry in final_state.get("agent_log", [])],
        }

    return target_pipeline


# ---------------------------------------------------------------------------
# Deterministic evaluators -- all accept `(outputs, reference_outputs)` (or a
# subset), per LangSmith's evaluator signature binding.
# ---------------------------------------------------------------------------
def plan_schema_valid(outputs: dict) -> dict:
    plan = outputs.get("migration_plan")
    if not plan:
        return {"key": "plan_schema_valid", "score": 0.0, "comment": outputs.get("error") or "no migration plan produced"}
    resources = plan.get("resources")
    if not isinstance(resources, list):
        return {"key": "plan_schema_valid", "score": 0.0, "comment": "'resources' is not a list"}
    bad = [i for i, r in enumerate(resources) if not isinstance(r, dict) or not r.get("logical_id") or not r.get("aws_type")]
    if bad:
        return {"key": "plan_schema_valid", "score": 0.0, "comment": f"resource(s) at index {bad} missing logical_id/aws_type"}
    return {"key": "plan_schema_valid", "score": 1.0, "comment": "ok"}


def resource_type_accuracy(outputs: dict, reference_outputs: dict) -> dict:
    expected = set((reference_outputs or {}).get("expected_aws_resource_types", []))
    if not expected:
        return {"key": "resource_type_accuracy", "score": 1.0, "comment": "no expected types declared"}
    plan = outputs.get("migration_plan") or {}
    actual = {r.get("aws_type") for r in plan.get("resources", []) if isinstance(r, dict)}
    score = len(expected & actual) / len(expected)
    missing = sorted(expected - actual)
    return {
        "key": "resource_type_accuracy", "score": score,
        "comment": "ok" if not missing else f"missing expected type(s): {missing}",
    }


def parameter_hygiene(outputs: dict, reference_outputs: dict) -> dict:
    plan = outputs.get("migration_plan")
    if not plan:
        return {"key": "parameter_hygiene", "score": 0.0, "comment": "no migration plan produced"}
    parameters = plan.get("parameters") or {}
    violations: list[str] = []
    for name in (reference_outputs or {}).get("must_be_noecho", []):
        definition = parameters.get(name)
        if definition is None:
            violations.append(f"expected NoEcho parameter '{name}' is missing from the plan")
        elif not definition.get("NoEcho"):
            violations.append(f"parameter '{name}' must be NoEcho but isn't")
    for name, definition in parameters.items():
        default = definition.get("Default")
        if definition.get("NoEcho") and default not in (None, ""):
            violations.append(f"NoEcho parameter '{name}' has a non-empty literal Default (secret leak risk)")
        if default not in (None, ""):
            error = _validate_param_value(str(default), definition)
            if error is not None:
                violations.append(f"parameter '{name}' Default violates its own constraints: {error}")
    return {"key": "parameter_hygiene", "score": 0.0 if violations else 1.0, "comment": "; ".join(violations) or "ok"}


def forbidden_patterns_absent(outputs: dict, reference_outputs: dict) -> dict:
    patterns = (reference_outputs or {}).get("forbidden_patterns") or []
    haystack = json.dumps(outputs, default=str)
    hits = [p for p in patterns if re.search(p, haystack)]
    return {
        "key": "forbidden_patterns_absent", "score": 0.0 if hits else 1.0,
        "comment": f"matched forbidden pattern(s): {hits}" if hits else "ok",
    }


def cfn_lint_clean(outputs: dict) -> dict:
    if "lint_passed" not in outputs:
        return {"key": "cfn_lint_clean", "score": None, "comment": "not applicable to this target"}
    return {"key": "cfn_lint_clean", "score": 1.0 if outputs["lint_passed"] else 0.0, "comment": outputs.get("stop_reason") or "ok"}


def lint_attempts(outputs: dict) -> dict:
    history = outputs.get("validation_history") or []
    if not history:
        return {"results": [
            {"key": "first_try_lint_pass", "score": None, "comment": "not applicable to this target"},
            {"key": "fix_attempts_used", "score": None, "comment": "not applicable to this target"},
        ]}
    return {"results": [
        {"key": "first_try_lint_pass", "score": 1.0 if history[0]["passed"] else 0.0},
        {"key": "fix_attempts_used", "score": float(outputs.get("fix_attempts", len(history) - 1))},
    ]}


def build_trajectory_valid(max_fix_attempts: int) -> Callable[[dict], dict]:
    def trajectory_valid(outputs: dict) -> dict:
        node_path = outputs.get("node_path")
        if node_path is None:
            return {"key": "trajectory_valid", "score": None, "comment": "not applicable to this target"}
        violations = []
        if "agent6_deploy" in node_path or "stack_check_gate" in node_path:
            violations.append("node path reached a deploy-adjacent node -- must never happen in eval")
        illegal = [n for n in node_path if n not in _ALLOWED_PIPELINE_NODES]
        if illegal:
            violations.append(f"illegal node(s) in path: {illegal}")
        if outputs.get("fix_attempts", 0) > max_fix_attempts:
            violations.append(f"fix_attempts {outputs.get('fix_attempts')} exceeds max_fix_attempts {max_fix_attempts}")
        return {"key": "trajectory_valid", "score": 0.0 if violations else 1.0, "comment": "; ".join(violations) or "ok"}

    return trajectory_valid


def latency_ms(outputs: dict) -> dict:
    value = outputs.get("latency_ms")
    if value is None:
        return {"key": "latency_ms", "score": None, "comment": "not recorded"}
    return {"key": "latency_ms", "score": float(value)}


def retrieval_quality(outputs: dict) -> dict:
    """Scores RAG retrieval itself (orchestrator/rag.py), independent of what
    the LLM does with the retrieved text: did each resource type actually get
    targeted chunks (vs. silently falling back to the full doc), and did the
    `where={"source_doc": ...}` filter actually keep retrieval scoped to that
    type's own doc (a cross-doc hit would mean the Chroma metadata tagging is
    broken). score=None when the target didn't record diagnostics (e.g. RAG
    disabled or --target pipeline, which doesn't expose this)."""
    diagnostics = outputs.get("rag_diagnostics")
    if not diagnostics:
        return {"key": "retrieval_quality", "score": None, "comment": "no RAG diagnostics recorded"}
    violations = []
    fallback_count = 0
    for rtype, info in diagnostics.items():
        if info.get("error"):
            violations.append(f"{rtype}: retrieval error: {info['error']}")
            continue
        if info.get("used_fallback"):
            fallback_count += 1
            continue
        if info.get("off_source_chunks"):
            violations.append(f"{rtype}: retrieved chunk(s) from the wrong doc: {info['off_source_chunks']}")
        if not info.get("retrieved_chunk_count"):
            violations.append(f"{rtype}: zero chunks retrieved")
    total = len(diagnostics) or 1
    fallback_rate = fallback_count / total
    hybrid_count = sum(1 for info in diagnostics.values() if info.get("used_hybrid"))
    score = 0.0 if violations else round(1.0 - fallback_rate, 3)
    comment = (
        "; ".join(violations) if violations
        else f"ok ({fallback_count}/{total} type(s) fell back to the full doc, {hybrid_count}/{total} used hybrid search)"
    )
    return {"key": "retrieval_quality", "score": score, "comment": comment}


# ---------------------------------------------------------------------------
# LLM-as-judge: mapping_fidelity. Uses a different (cheaper/faster) Bedrock
# model than the generator by default, temperature 0. Best-effort: returns
# score=None (not 0) when Bedrock is unreachable, so offline/CI runs aren't
# penalized for an environment gap -- this is a judge-availability issue, not
# a finding about the plan.
# ---------------------------------------------------------------------------
def _invoke_judge(config: Config, judge_model_id: str, prompt: str) -> str | None:
    try:
        import boto3

        client = boto3.client("bedrock-runtime", region_name=config.aws_region)
        with trace_span("mapping_fidelity_judge", run_type="llm", metadata={"ls_provider": "amazon_bedrock", "ls_model_name": judge_model_id}):
            body = json.dumps({
                "schemaVersion": "messages-v1",
                "messages": [{"role": "user", "content": [{"text": prompt}]}],
                "inferenceConfig": {"temperature": 0},
            })
            response = client.invoke_model(
                modelId=judge_model_id, body=body, contentType="application/json", accept="application/json",
            )
            payload = json.loads(response["body"].read())
        blocks = payload.get("output", {}).get("message", {}).get("content", [])
        for block in blocks:
            if isinstance(block, dict) and block.get("text"):
                return block["text"]
        return None
    except Exception:
        return None


def build_mapping_fidelity(config: Config, judge_model_id: str = JUDGE_MODEL_ID_DEFAULT) -> Callable[[dict, dict], dict]:
    def mapping_fidelity(outputs: dict, reference_outputs: dict) -> dict:
        plan = outputs.get("migration_plan")
        if not plan:
            return {"key": "mapping_fidelity", "score": 0.0, "comment": "no migration plan produced"}
        prompt = (
            "You are grading how faithfully a migration plan follows its mapping reference "
            "and source resources. Score strictly 0, 0.5, or 1.\n"
            "0 = plan ignores or contradicts the mapping reference, or invents AWS types.\n"
            "0.5 = plan is directionally correct but has gaps (missing properties, parameter "
            "hygiene issues, inconsistent naming).\n"
            "1 = plan faithfully follows the mapping reference and reflects the source resources.\n"
            f"Expected AWS resource types: {(reference_outputs or {}).get('expected_aws_resource_types')}\n"
            f"Migration plan produced:\n{json.dumps(plan, default=str)[:4000]}\n"
            'Respond with ONLY a JSON object: {"score": 0|0.5|1, "rationale": "..."}'
        )
        raw = _invoke_judge(config, judge_model_id, prompt)
        if raw is None:
            return {"key": "mapping_fidelity", "score": None, "comment": "judge model unavailable (offline/no AWS access)"}
        cleaned = BedrockGenerator._normalize_text_output(raw)
        try:
            parsed = json.loads(cleaned)
            return {"key": "mapping_fidelity", "score": float(parsed["score"]), "comment": parsed.get("rationale", "")}
        except Exception:
            return {"key": "mapping_fidelity", "score": None, "comment": f"unparseable judge response: {raw[:200]}"}

    return mapping_fidelity


# ---------------------------------------------------------------------------
# Convenience evaluator lists for the two targets (see evals/run_eval.py).
# ---------------------------------------------------------------------------
def build_agent3_evaluators(config: Config, judge_model_id: str = JUDGE_MODEL_ID_DEFAULT) -> list:
    return [
        plan_schema_valid,
        resource_type_accuracy,
        parameter_hygiene,
        forbidden_patterns_absent,
        latency_ms,
        retrieval_quality,
        build_mapping_fidelity(config, judge_model_id),
    ]


def build_pipeline_evaluators(config: Config) -> list:
    return [
        plan_schema_valid,
        resource_type_accuracy,
        parameter_hygiene,
        forbidden_patterns_absent,
        cfn_lint_clean,
        lint_attempts,
        build_trajectory_valid(config.max_fix_attempts),
    ]


# ---------------------------------------------------------------------------
# Run-history logging & calibration report -- distinct from the LangSmith eval
# harness above: this logs REAL migrate_agents.py runs (orchestrator.graph's
# full 0->7 agent graph, via agent7_report) to a flat JSONL file, for the
# capstone's evaluation section. The eval harness's build_eval_graph() never
# adds an agent7_report node, so eval runs never pollute this history --
# only real migrations are logged.
#
# Brier score needs a predicted probability to compare against the actual
# outcome. predict_success_probability() below uses a lightweight historical
# success-rate prior: the mean actual outcome of past runs that touched any of
# the same resource type(s), defaulting to 0.5 (an uninformative prior) with
# no matching history. It's computed from history written BEFORE the current
# run's own record, so a run never "predicts" using its own outcome.
# ---------------------------------------------------------------------------
DEFAULT_HISTORY_PATH = Path("output/runs/history.jsonl")


def _run_actual_outcome(state: MigrationState) -> float:
    """1.0 if the run reached a successful end state, else 0.0. A deploy
    result (if attempted) is the strongest signal; runs that stop earlier
    (dry-run, rejected plan, lint exhausted) fall back to lint_passed."""
    deploy_result = state.get("deploy_result")
    if deploy_result:
        return 1.0 if deploy_result.get("status") not in (None, "FAILED") else 0.0
    if state.get("stopped"):
        return 0.0
    return 1.0 if state.get("lint_passed") else 0.0


def _read_history(history_path: Path) -> list[dict]:
    if not history_path.exists():
        return []
    records = []
    for line in history_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def predict_success_probability(resource_types: list[str], history_path: Path = DEFAULT_HISTORY_PATH) -> float:
    types = set(resource_types or [])
    outcomes = [
        r["actual_outcome"]
        for r in _read_history(history_path)
        if r.get("actual_outcome") is not None and types & set(r.get("resource_types") or [])
    ]
    return sum(outcomes) / len(outcomes) if outcomes else 0.5


def log_run_outcome(state: MigrationState, history_path: Path = DEFAULT_HISTORY_PATH) -> dict:
    """Appends one JSONL record to history_path for a completed real migration
    run. Call from agent7_report -- the graph's universal terminal node -- so
    every real run (success, human rejection, or lint-exhausted) is captured
    exactly once."""
    resource_types = state.get("resource_types") or []
    predicted = predict_success_probability(resource_types, history_path)
    actual = _run_actual_outcome(state)
    started_at = state.get("started_at")
    deploy_result = state.get("deploy_result")
    entry = {
        "run_id": state.get("run_id"),
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "resource_types": resource_types,
        "completed": not bool(state.get("stopped")),
        "stop_reason": state.get("stop_reason"),
        "lint_passed": state.get("lint_passed"),
        "fix_attempts": state.get("fix_attempts", 0),
        "deploy_attempted": bool(deploy_result),
        "deploy_succeeded": (None if not deploy_result else deploy_result.get("status") not in (None, "FAILED")),
        "human_decisions": state.get("human_decisions") or [],
        "human_intervention": bool(state.get("human_decisions")),
        "predicted_success_probability": predicted,
        "actual_outcome": actual,
        "time_to_migrate_seconds": (time.time() - started_at) if started_at else None,
    }
    history_path.parent.mkdir(parents=True, exist_ok=True)
    with history_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
    return entry


def _brier_score(records: list[dict]) -> float | None:
    scored = [
        r for r in records
        if r.get("predicted_success_probability") is not None and r.get("actual_outcome") is not None
    ]
    if not scored:
        return None
    return sum((r["predicted_success_probability"] - r["actual_outcome"]) ** 2 for r in scored) / len(scored)


def build_calibration_report(history_path: Path = DEFAULT_HISTORY_PATH) -> dict:
    """Aggregate metrics across every logged run: pass rate, human-intervention
    rate, deploy success rate, mean/median time-to-migrate, and the Brier
    score of predict_success_probability()'s forecast vs. actual outcome
    (0 = perfectly calibrated, 0.25 = no better than a coin flip at p=0.5,
    1 = perfectly wrong)."""
    records = _read_history(history_path)
    if not records:
        return {"run_count": 0}

    run_count = len(records)
    durations = [r["time_to_migrate_seconds"] for r in records if r.get("time_to_migrate_seconds") is not None]
    deploy_attempts = [r for r in records if r.get("deploy_attempted")]
    return {
        "run_count": run_count,
        "pass_rate": sum(1 for r in records if r.get("completed")) / run_count,
        "human_intervention_rate": sum(1 for r in records if r.get("human_intervention")) / run_count,
        "deploy_success_rate": (
            sum(1 for r in deploy_attempts if r.get("deploy_succeeded")) / len(deploy_attempts)
            if deploy_attempts else None
        ),
        "mean_time_to_migrate_seconds": (sum(durations) / len(durations)) if durations else None,
        "median_time_to_migrate_seconds": sorted(durations)[len(durations) // 2] if durations else None,
        "brier_score": _brier_score(records),
    }


def render_calibration_report_markdown(history_path: Path = DEFAULT_HISTORY_PATH) -> str:
    metrics = build_calibration_report(history_path)
    if metrics["run_count"] == 0:
        return f"# Migration Run Calibration Report\n\nNo logged runs found in {history_path}.\n"

    def fmt_pct(x):
        return f"{x * 100:.1f}%" if x is not None else "n/a"

    def fmt_secs(x):
        return f"{x:.1f}s" if x is not None else "n/a"

    def fmt_score(x):
        return f"{x:.4f}" if x is not None else "n/a"

    return "\n".join([
        "# Migration Run Calibration Report",
        "",
        f"- **Runs logged:** {metrics['run_count']}",
        f"- **Pass rate:** {fmt_pct(metrics['pass_rate'])}",
        f"- **Human-intervention rate:** {fmt_pct(metrics['human_intervention_rate'])}",
        f"- **Deploy success rate (of attempted deploys):** {fmt_pct(metrics['deploy_success_rate'])}",
        f"- **Mean time-to-migrate:** {fmt_secs(metrics['mean_time_to_migrate_seconds'])}",
        f"- **Median time-to-migrate:** {fmt_secs(metrics['median_time_to_migrate_seconds'])}",
        f"- **Brier score (historical-rate forecast vs. actual outcome):** {fmt_score(metrics['brier_score'])}",
        "",
        "Brier score ranges 0 (perfectly calibrated) to 1 (perfectly wrong); 0.25 is the baseline",
        "for an uninformative always-p=0.5 forecast. `predict_success_probability()` currently",
        "uses only historical outcomes for matching resource types.",
        "",
    ])


def write_calibration_report(history_path: Path = DEFAULT_HISTORY_PATH, report_path: Path | None = None) -> Path:
    report_path = report_path or history_path.parent / "calibration_report.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(render_calibration_report_markdown(history_path), encoding="utf-8")
    return report_path
