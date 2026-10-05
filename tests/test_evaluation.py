"""Tests for orchestrator/evaluation.py -- evaluators scored against fixed fake
plans (no AWS/LangSmith needed), a recorded-response Agent 3 fixture that
exercises the real Agent5->Agent3 retry loop fully offline, and a structural
guarantee that the eval graph can never reach agent6_deploy.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from orchestrator.config import Config
from orchestrator.evaluation import (
    build_calibration_report,
    build_eval_graph,
    build_pipeline_evaluators,
    build_trajectory_valid,
    cfn_lint_clean,
    forbidden_patterns_absent,
    lint_attempts,
    log_run_outcome,
    parameter_hygiene,
    plan_schema_valid,
    predict_success_probability,
    render_calibration_report_markdown,
    resource_type_accuracy,
    retrieval_quality,
)
from orchestrator.generator import Generator
from orchestrator.knowledge_base import KnowledgeBase

REPO_ROOT = Path(__file__).resolve().parent.parent
REFERENCE_DIR = REPO_ROOT / "evals" / "datasets" / "reference"
KB_INDEX = REPO_ROOT / "knowledge_base" / "index.json"


def _test_config(**overrides) -> Config:
    base = Config.from_env()
    import dataclasses

    return dataclasses.replace(base, **overrides) if overrides else base


# ---------------------------------------------------------------------------
# Fixed fake plans: good / subtly wrong / adversarial
# ---------------------------------------------------------------------------
GOOD_PLAN = {
    "description": "Key Vault secrets -> Secrets Manager",
    "parameters": {
        "SecretNamePrefix": {"Type": "String", "Default": "myapp"},
        "DbUsername": {"Type": "String", "NoEcho": True},
        "DbPassword": {"Type": "String", "NoEcho": True},
    },
    "conditions": {},
    "resources": [
        {"logical_id": "DbUsernameSecret", "aws_type": "AWS::SecretsManager::Secret", "properties": {}, "depends_on": [], "source_azure_type": "Microsoft.KeyVault/vaults/secrets"},
        {"logical_id": "DbPasswordSecret", "aws_type": "AWS::SecretsManager::Secret", "properties": {}, "depends_on": [], "source_azure_type": "Microsoft.KeyVault/vaults/secrets"},
    ],
    "outputs": {},
}

SUBTLY_WRONG_PLAN = {
    "description": "Missing NoEcho + missing a resource type",
    "parameters": {
        "SecretNamePrefix": {"Type": "String", "Default": "myapp"},
        "DbUsername": {"Type": "String", "NoEcho": True},
        "DbPassword": {"Type": "String"},  # BUG: must be NoEcho
    },
    "conditions": {},
    "resources": [
        {"logical_id": "DbUsernameSecret", "aws_type": "AWS::SecretsManager::Secret", "properties": {}, "depends_on": [], "source_azure_type": "Microsoft.KeyVault/vaults/secrets"},
    ],
    "outputs": {},
}

ADVERSARIAL_PLAN = {
    "description": "Leaks a literal secret + invalid parameter default",
    "parameters": {
        "DbPassword": {"Type": "String", "NoEcho": True, "Default": "hunter2-literal-leak"},  # BUG: non-empty default on NoEcho
        "SecretNamePrefix": {"Type": "String", "Default": "", "MinLength": 1},  # BUG: default violates own MinLength
    },
    "conditions": {},
    "resources": [
        {"logical_id": "DbPasswordSecret", "aws_type": "AWS::SecretsManager::Secret", "properties": {"SecretString": "hunter2-literal-leak"}, "depends_on": [], "source_azure_type": "Microsoft.KeyVault/vaults/secrets"},
    ],
    "outputs": {},
}

KEYVAULT_EXPECTED = json.loads((REFERENCE_DIR / "keyvault.json").read_text(encoding="utf-8"))["expected"]


def test_plan_schema_valid_good_and_missing():
    assert plan_schema_valid({"migration_plan": GOOD_PLAN})["score"] == 1.0
    assert plan_schema_valid({"migration_plan": None, "error": "boom"})["score"] == 0.0
    assert plan_schema_valid({"migration_plan": {"resources": [{"logical_id": "X"}]}})["score"] == 0.0  # missing aws_type


def test_resource_type_accuracy_good_subtle_adversarial():
    good = resource_type_accuracy({"migration_plan": GOOD_PLAN}, KEYVAULT_EXPECTED)
    assert good["score"] == 1.0

    subtle = resource_type_accuracy({"migration_plan": SUBTLY_WRONG_PLAN}, KEYVAULT_EXPECTED)
    assert subtle["score"] == 1.0  # the one resource present is still the right type -- hygiene catches the real bug

    adversarial = resource_type_accuracy({"migration_plan": ADVERSARIAL_PLAN}, KEYVAULT_EXPECTED)
    assert adversarial["score"] == 1.0  # type itself is right; parameter_hygiene is what must catch this plan


def test_parameter_hygiene_good_subtle_adversarial():
    assert parameter_hygiene({"migration_plan": GOOD_PLAN}, KEYVAULT_EXPECTED)["score"] == 1.0

    subtle = parameter_hygiene({"migration_plan": SUBTLY_WRONG_PLAN}, KEYVAULT_EXPECTED)
    assert subtle["score"] == 0.0
    assert "DbPassword" in subtle["comment"]

    adversarial = parameter_hygiene({"migration_plan": ADVERSARIAL_PLAN}, KEYVAULT_EXPECTED)
    assert adversarial["score"] == 0.0
    assert "non-empty literal Default" in adversarial["comment"]


def test_forbidden_patterns_absent_catches_leaked_secret():
    reference = {"forbidden_patterns": ["hunter2-literal-leak"]}
    assert forbidden_patterns_absent({"migration_plan": GOOD_PLAN}, reference)["score"] == 1.0
    assert forbidden_patterns_absent({"migration_plan": ADVERSARIAL_PLAN}, reference)["score"] == 0.0


def test_forbidden_patterns_absent_ignores_required_source_azure_type_field():
    # GOOD_PLAN's resources legitimately carry source_azure_type="Microsoft.KeyVault/..."
    # (a required traceability field) -- that must not trip a forbidden pattern targeting
    # the same Azure namespace prefix.
    reference = {"forbidden_patterns": ["Microsoft.KeyVault"]}
    assert forbidden_patterns_absent({"migration_plan": GOOD_PLAN}, reference)["score"] == 1.0


def test_cfn_lint_clean_and_lint_attempts():
    assert cfn_lint_clean({"foo": "bar"})["score"] is None  # not applicable (agent3-only outputs)
    assert cfn_lint_clean({"lint_passed": True})["score"] == 1.0
    assert cfn_lint_clean({"lint_passed": False})["score"] == 0.0

    none_result = lint_attempts({})
    assert all(r["score"] is None for r in none_result["results"])

    history = [{"attempt": 1, "passed": False}, {"attempt": 2, "passed": True}]
    result = lint_attempts({"validation_history": history, "fix_attempts": 1})
    by_key = {r["key"]: r["score"] for r in result["results"]}
    assert by_key["first_try_lint_pass"] == 0.0
    assert by_key["fix_attempts_used"] == 1.0


def test_retrieval_quality_good_fallback_contaminated_and_error():
    # no diagnostics recorded at all (RAG disabled, or --target pipeline)
    assert retrieval_quality({})["score"] is None

    # every type got targeted chunks, scoped to its own doc -- full marks
    good = {
        "Microsoft.KeyVault/vaults/secrets": {
            "source_doc": "bicep-to-cloudformation.md", "used_fallback": False,
            "retrieved_chunk_count": 3, "off_source_chunks": [],
        },
    }
    assert retrieval_quality({"rag_diagnostics": good})["score"] == 1.0

    # fell back to the full doc for one of two types -- partial credit, not a hard failure
    partial = {
        **good,
        "Microsoft.Network/virtualNetworks": {
            "source_doc": "vpc-to-cloudformation.md", "used_fallback": True, "retrieved_chunk_count": 0,
        },
    }
    partial_score = retrieval_quality({"rag_diagnostics": partial})["score"]
    assert 0.0 < partial_score < 1.0

    # a chunk came back tagged with the WRONG source doc -- the `where` filter failed; hard fail
    contaminated = {
        "Microsoft.KeyVault/vaults/secrets": {
            "source_doc": "bicep-to-cloudformation.md", "used_fallback": False,
            "retrieved_chunk_count": 3, "off_source_chunks": ["vpc-to-cloudformation.md"],
        },
    }
    assert retrieval_quality({"rag_diagnostics": contaminated})["score"] == 0.0

    # retrieval itself raised -- hard fail, not silently ignored
    errored = {"Microsoft.KeyVault/vaults/secrets": {"used_fallback": True, "error": "boom"}}
    assert retrieval_quality({"rag_diagnostics": errored})["score"] == 0.0


def test_trajectory_valid_legal_and_illegal_paths():
    evaluator = build_trajectory_valid(max_fix_attempts=2)
    legal = evaluator({"node_path": ["agent1_validate", "agent2_build_cnr", "agent3_map_resources", "agent4_render", "agent5_validate_cfn", "plan_approval_gate"], "fix_attempts": 0})
    assert legal["score"] == 1.0

    deploy_leak = evaluator({"node_path": ["agent1_validate", "agent6_deploy"], "fix_attempts": 0})
    assert deploy_leak["score"] == 0.0
    assert "deploy" in deploy_leak["comment"]

    too_many_retries = evaluator({"node_path": ["agent1_validate"], "fix_attempts": 5})
    assert too_many_retries["score"] == 0.0


def test_build_pipeline_evaluators_returns_callables():
    evaluators = build_pipeline_evaluators(_test_config())
    assert len(evaluators) >= 5
    assert all(callable(e) for e in evaluators)


# ---------------------------------------------------------------------------
# Structural guarantee: the eval graph has no deploy-adjacent nodes at all.
# ---------------------------------------------------------------------------
def test_eval_graph_cannot_reach_agent6_deploy(tmp_path):
    knowledge_base = KnowledgeBase(KB_INDEX)

    class _UnusedGenerator(Generator):
        def generate(self, prompt: str) -> str:
            raise AssertionError("generator should not be called by this test")

    graph = build_eval_graph(knowledge_base, _UnusedGenerator(), _test_config(), tmp_path)
    node_names = set(graph.nodes.keys())

    assert "agent6_deploy" not in node_names
    assert "stack_check_gate" not in node_names
    assert "agent7_report" not in node_names
    assert node_names == {
        "__start__", "agent1_validate", "agent2_build_cnr", "agent3_map_resources",
        "plan_approval_gate", "agent4_render", "agent5_validate_cfn", "bump_fix_attempts",
    }


# ---------------------------------------------------------------------------
# Recorded-response Agent 3 fixture -- exercises the real Agent5->Agent3 retry
# loop fully offline (no Bedrock call at all).
# ---------------------------------------------------------------------------
class RecordedGenerator(Generator):
    """Returns canned migration-plan JSON text in sequence, so the retry loop
    can be driven deterministically without a real LLM call."""

    def __init__(self, responses: list[str]):
        self._responses = list(responses)
        self.calls = 0

    def generate(self, prompt: str) -> str:
        response = self._responses[min(self.calls, len(self._responses) - 1)]
        self.calls += 1
        return response


_BAD_SECRET_PLAN = json.dumps({
    "description": "bad -- unrecognized property trips cfn-lint",
    "parameters": {},
    "conditions": {},
    "resources": [{
        "logical_id": "DbPasswordSecret", "aws_type": "AWS::SecretsManager::Secret",
        "source_azure_type": "Microsoft.KeyVault/vaults/secrets",
        "properties": {"ThisPropertyDoesNotExist": "x"},
        "depends_on": [],
    }],
    "outputs": {},
})

_GOOD_SECRET_PLAN = json.dumps({
    "description": "fixed",
    "parameters": {},
    "conditions": {},
    "resources": [{
        "logical_id": "DbPasswordSecret", "aws_type": "AWS::SecretsManager::Secret",
        "source_azure_type": "Microsoft.KeyVault/vaults/secrets",
        "properties": {"Name": "db-password", "SecretString": "placeholder"},
        "depends_on": [],
    }],
    "outputs": {},
})


def test_retry_loop_runs_offline_with_recorded_generator(tmp_path):
    arm_template = json.loads((REFERENCE_DIR / "keyvault.json").read_text(encoding="utf-8"))["inputs"]["arm_template"]
    arm_path = tmp_path / "arm.json"
    arm_path.write_text(json.dumps(arm_template), encoding="utf-8")

    knowledge_base = KnowledgeBase(KB_INDEX)
    generator = RecordedGenerator([_BAD_SECRET_PLAN, _GOOD_SECRET_PLAN])
    config = _test_config(max_fix_attempts=2, rag_enabled=False)  # no RAG -- keep this test fully offline, no Bedrock embedding call
    graph = build_eval_graph(knowledge_base, generator, config, tmp_path)

    state = {
        "bicep_path": str(arm_path), "output_dir": str(tmp_path), "run_id": "test-retry",
        "dry_run": False, "fix_attempts": 0, "max_fix_attempts": config.max_fix_attempts,
        "param_overrides": {}, "agent_log": [],
    }
    final_state = graph.invoke(state, config={"recursion_limit": 50})

    assert generator.calls == 2  # one failing attempt, one corrected retry
    assert final_state.get("lint_passed") is True
    assert final_state.get("fix_attempts") == 1
    node_path = [entry["agent"] for entry in final_state.get("agent_log", [])]
    assert "agent6_deploy" not in node_path


# ---------------------------------------------------------------------------
# Run-history logging & calibration report -- fully offline, against a temp
# history.jsonl (never touches the real output/runs/history.jsonl).
# ---------------------------------------------------------------------------
def test_predict_success_probability_no_history_defaults_to_half(tmp_path):
    history_path = tmp_path / "history.jsonl"
    assert predict_success_probability(["Microsoft.KeyVault/vaults/secrets"], history_path) == 0.5


def test_log_run_outcome_appends_and_predicts_from_prior_runs(tmp_path):
    history_path = tmp_path / "history.jsonl"

    # First run: Key Vault, succeeded. No prior history -> predicted 0.5.
    state1 = {
        "run_id": "run1", "resource_types": ["Microsoft.KeyVault/vaults/secrets"],
        "stopped": False, "lint_passed": True, "fix_attempts": 0,
        "human_decisions": [{"gate": "plan_approval_gate", "decision": "approved"}],
        "started_at": time.time() - 12.5,
    }
    entry1 = log_run_outcome(state1, history_path=history_path)
    assert entry1["predicted_success_probability"] == 0.5
    assert entry1["actual_outcome"] == 1.0
    assert entry1["completed"] is True
    assert entry1["human_intervention"] is True
    assert entry1["time_to_migrate_seconds"] >= 12.0

    # Second run: same resource type, this time stopped (failed). Prediction
    # should now reflect the one prior success (1.0), not the default.
    state2 = {
        "run_id": "run2", "resource_types": ["Microsoft.KeyVault/vaults/secrets"],
        "stopped": True, "stop_reason": "lint exhausted", "lint_passed": False, "fix_attempts": 2,
        "human_decisions": [],
    }
    entry2 = log_run_outcome(state2, history_path=history_path)
    assert entry2["predicted_success_probability"] == 1.0
    assert entry2["actual_outcome"] == 0.0
    assert entry2["completed"] is False
    assert entry2["human_intervention"] is False

    lines = history_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2


def test_log_run_outcome_deploy_result_takes_precedence_over_lint(tmp_path):
    state = {
        "run_id": "run3", "resource_types": [], "stopped": False, "lint_passed": True,
        "deploy_result": {"status": "FAILED"},
    }
    entry = log_run_outcome(state, history_path=tmp_path / "history.jsonl")
    assert entry["actual_outcome"] == 0.0
    assert entry["deploy_attempted"] is True
    assert entry["deploy_succeeded"] is False


def test_build_calibration_report_empty_and_populated(tmp_path):
    empty_path = tmp_path / "missing_history.jsonl"
    assert build_calibration_report(empty_path) == {"run_count": 0}

    history_path = tmp_path / "history.jsonl"
    log_run_outcome({
        "run_id": "a", "resource_types": ["t1"], "stopped": False, "lint_passed": True,
        "human_decisions": [], "started_at": time.time() - 10,
    }, history_path=history_path)
    log_run_outcome({
        "run_id": "b", "resource_types": ["t1"], "stopped": True, "lint_passed": False,
        "human_decisions": [{"gate": "plan_approval_gate", "decision": "rejected"}],
        "started_at": time.time() - 20,
    }, history_path=history_path)

    report = build_calibration_report(history_path)
    assert report["run_count"] == 2
    assert report["pass_rate"] == 0.5
    assert report["human_intervention_rate"] == 0.5
    assert report["brier_score"] is not None
    assert report["mean_time_to_migrate_seconds"] > 0

    markdown = render_calibration_report_markdown(history_path)
    assert "Brier score" in markdown
    assert "Pass rate" in markdown


def test_render_calibration_report_markdown_no_history(tmp_path):
    markdown = render_calibration_report_markdown(tmp_path / "nope.jsonl")
    assert "No logged runs found" in markdown
