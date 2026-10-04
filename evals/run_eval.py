#!/usr/bin/env python
"""CLI for the Phase 3 eval harness: Agent 3 (prompt/RAG/model) or the real
Agent 1->5 pipeline trajectory, scored with orchestrator/evaluation.py's
evaluators. Online runs create a LangSmith experiment (`langsmith.evaluate`);
--local-only runs entirely offline against evals/datasets/reference/*.json and
never contacts LangSmith, AWS stack APIs, or a human.

Examples:
    python evals/run_eval.py --target agent3 --dataset bicep-to-cfn-migration-v1 --repetitions 3
    python evals/run_eval.py --target agent3 --dataset bicep-to-cfn-migration-v1 --model-id amazon.nova-lite-v1:0 --no-rag
    python evals/run_eval.py --target pipeline --local-only --limit 2
"""
from __future__ import annotations

import argparse
import dataclasses
import inspect
import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from orchestrator import cli_ui  # noqa: E402
from orchestrator.config import Config  # noqa: E402
from orchestrator.evaluation import (  # noqa: E402
    build_agent3_evaluators,
    build_pipeline_evaluators,
    build_target_agent3,
    build_target_pipeline,
)
from orchestrator.knowledge_base import KnowledgeBase  # noqa: E402
from orchestrator.prompts.agent3_v1 import PROMPT_VERSION  # noqa: E402

DEFAULT_KB_INDEX = REPO_ROOT / "knowledge_base" / "index.json"
REFERENCE_DIR = REPO_ROOT / "evals" / "datasets" / "reference"
OUTPUT_ROOT = REPO_ROOT / "output" / "evals"


def _git_sha() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=5, cwd=str(REPO_ROOT)
        )
        return result.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def _load_local_examples(limit: int | None) -> list[dict]:
    examples = [
        {"name": (doc := json.loads(p.read_text(encoding="utf-8")))["name"], "inputs": doc["inputs"], "outputs": doc["expected"]}
        for p in sorted(REFERENCE_DIR.glob("*.json"))
    ]
    return examples[:limit] if limit else examples


def _serialize_eval_result(result: object) -> dict:
    if hasattr(result, "model_dump"):
        return result.model_dump()
    if hasattr(result, "dict"):
        return result.dict()
    if isinstance(result, dict):
        return result
    return {"key": getattr(result, "key", "unknown"), "score": getattr(result, "score", None), "comment": getattr(result, "comment", None)}


def _call_evaluator(evaluator, outputs: dict, reference_outputs: dict) -> list[dict]:
    """Mirrors LangSmith's own flexible evaluator signature binding (by
    parameter name) so the same evaluator functions work in --local-only mode."""
    sig = inspect.signature(evaluator)
    kwargs = {}
    if "outputs" in sig.parameters:
        kwargs["outputs"] = outputs
    if "reference_outputs" in sig.parameters:
        kwargs["reference_outputs"] = reference_outputs
    result = evaluator(**kwargs)
    return [_serialize_eval_result(r) for r in result["results"]] if "results" in result else [_serialize_eval_result(result)]


def _run_local(target, evaluators, examples: list[dict], repetitions: int) -> list[dict]:
    rows = []
    for example in examples:
        for rep in range(repetitions):
            try:
                outputs = target(example["inputs"])
            except Exception as exc:  # noqa: BLE001 -- one bad example shouldn't kill the whole run
                outputs = {"error": str(exc)}
            eval_results = []
            for evaluator in evaluators:
                try:
                    eval_results.extend(_call_evaluator(evaluator, outputs, example["outputs"]))
                except Exception as exc:  # noqa: BLE001
                    eval_results.append({"key": getattr(evaluator, "__name__", "evaluator"), "score": None, "comment": f"evaluator crashed: {exc}"})
            rows.append({"example": example["name"], "repetition": rep, "outputs": outputs, "evaluation_results": eval_results})
    return rows


def _write_summary(output_dir: Path, experiment_name: str, rows: list[dict], metadata: dict) -> tuple[int, int, dict[str, list[float]]]:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "results.jsonl").write_text(
        "\n".join(json.dumps(row, default=str) for row in rows) + "\n", encoding="utf-8"
    )

    per_key: dict[str, list[float]] = {}
    failing_rows = []
    for row in rows:
        row_failed = False
        for result in row["evaluation_results"]:
            score = result.get("score")
            if score is None:
                continue
            per_key.setdefault(result["key"], []).append(float(score))
            if float(score) < 1.0:
                row_failed = True
        if row_failed:
            failing_rows.append(row)

    lines = [f"# Eval summary -- {experiment_name}", "", "## Metadata", ""]
    lines += [f"- {k}: {v}" for k, v in metadata.items()]
    pass_rate = (len(rows) - len(failing_rows)) / len(rows) if rows else 0.0
    lines += ["", f"## Pass rate: {len(rows) - len(failing_rows)}/{len(rows)} rows ({pass_rate:.1%})", ""]
    lines += ["## Scores (mean over all rows/repetitions)", "", "| Evaluator | Mean score | N |", "|---|---|---|"]
    for key, scores in sorted(per_key.items()):
        lines.append(f"| {key} | {sum(scores) / len(scores):.3f} | {len(scores)} |")
    lines += ["", "Token usage is already captured per-row in the LangSmith trace (see generator.py's @traceable Bedrock span); not duplicated here."]
    if failing_rows:
        lines += ["", "## Per-example failures (first 10)", ""]
        for row in failing_rows[:10]:
            failing = [r for r in row["evaluation_results"] if r.get("score") is not None and float(r["score"]) < 1.0]
            detail = "; ".join(f"{r['key']}={r['score']} ({r.get('comment', '')})" for r in failing)
            lines.append(f"- **{row['example']}** (rep {row['repetition']}): {detail}")
    (output_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(rows) - len(failing_rows), len(rows), per_key


def _print_summary_table(passed: int, total: int, per_key: dict[str, list[float]], summary_path: Path) -> None:
    pass_rate = passed / total if total else 0.0
    style = "bold green" if passed == total else "bold yellow"
    cli_ui.console.print(f"\n[{style}]Pass rate: {passed}/{total} rows ({pass_rate:.1%})[/{style}]")
    table = cli_ui.Table(show_header=True, header_style="bold cyan", border_style="dim")
    table.add_column("Evaluator")
    table.add_column("Mean score", justify="right")
    table.add_column("N", justify="right")
    for key, scores in sorted(per_key.items()):
        table.add_row(key, f"{sum(scores) / len(scores):.3f}", str(len(scores)))
    cli_ui.console.print(table)
    cli_ui.success(f"Wrote {summary_path.relative_to(REPO_ROOT)}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dataset", default=None, help="LangSmith dataset name (ignored with --local-only)")
    parser.add_argument("--target", choices=["agent3", "pipeline"], required=True)
    parser.add_argument("--model-id", default=None, help="Override BEDROCK_MODEL_ID for this run")
    parser.add_argument(
        "--prompt-version", default=None,
        help="orchestrator/prompts/agent3_v<N> to use -- only affects --target agent3 "
        "(--target pipeline always runs the real agent3_v1 production node); defaults to v1",
    )
    parser.add_argument("--rag", dest="rag", action="store_true", default=None)
    parser.add_argument("--no-rag", dest="rag", action="store_false")
    parser.add_argument("--top-k", type=int, default=None)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument(
        "--local-only", action="store_true",
        help="Never contacts LangSmith; runs entirely offline against evals/datasets/reference/*.json",
    )
    args = parser.parse_args()

    cli_ui.banner(f"Eval: {args.target}", f"dataset={args.dataset or 'local'} repetitions={args.repetitions}")

    config = Config.from_env()
    overrides = {}
    if args.model_id:
        overrides["bedrock_model_id"] = args.model_id
    if args.rag is not None:
        overrides["rag_enabled"] = args.rag
    if args.top_k is not None:
        overrides["rag_top_k"] = args.top_k
    if overrides:
        config = dataclasses.replace(config, **overrides)

    knowledge_base = KnowledgeBase(DEFAULT_KB_INDEX)
    prompt_version = args.prompt_version or PROMPT_VERSION
    experiment_prefix = f"{args.target}-{prompt_version}"
    metadata = {
        "bedrock_model_id": config.bedrock_model_id,
        "prompt_version": prompt_version,
        "rag_enabled": config.rag_enabled,
        "rag_top_k": config.rag_top_k,
        "max_fix_attempts": config.max_fix_attempts,
        "git_sha": _git_sha(),
        "dataset": args.dataset or "local",
    }

    if args.target == "agent3":
        target = build_target_agent3(knowledge_base, config, prompt_version=prompt_version)
        evaluators = build_agent3_evaluators(config)
    else:
        target = build_target_pipeline(knowledge_base, config, OUTPUT_ROOT / "_pipeline_runs")
        evaluators = build_pipeline_evaluators(config)

    if args.local_only:
        examples = _load_local_examples(args.limit)
        experiment_name = f"{experiment_prefix}-local-{int(time.time())}"
        rows = _run_local(target, evaluators, examples, args.repetitions)
        output_dir = OUTPUT_ROOT / experiment_name
        passed, total, per_key = _write_summary(output_dir, experiment_name, rows, metadata)
        _print_summary_table(passed, total, per_key, output_dir / "summary.md")
        return 0

    if not args.dataset:
        parser.error("--dataset is required unless --local-only is set")
    if not (os.environ.get("LANGSMITH_API_KEY") or "").strip():
        parser.error("LANGSMITH_API_KEY is not set -- pass --local-only to run offline instead")

    from langsmith import Client, evaluate

    client = Client()
    if not client.has_dataset(dataset_name=args.dataset):
        parser.error(
            f"LangSmith dataset '{args.dataset}' doesn't exist yet. Create/push it first:\n"
            f"  python scripts/create_langsmith_dataset.py --dataset-name {args.dataset}"
        )
    # `evaluate(data=dataset_name)` ignores --limit -- fetch examples ourselves when it's set.
    data = list(client.list_examples(dataset_name=args.dataset, limit=args.limit)) if args.limit else args.dataset

    results = evaluate(
        target,
        data=data,
        evaluators=evaluators,
        experiment_prefix=experiment_prefix,
        metadata=metadata,
        max_concurrency=2,  # Bedrock throttling
        num_repetitions=args.repetitions,
    )

    rows = []
    for row in results:
        eval_results = row["evaluation_results"].get("results", []) if isinstance(row["evaluation_results"], dict) else []
        rows.append({
            "example": str(row["example"].id),
            "repetition": 0,
            "outputs": row["run"].outputs or {},
            "evaluation_results": [_serialize_eval_result(r) for r in eval_results],
        })
    output_dir = OUTPUT_ROOT / results.experiment_name
    passed, total, per_key = _write_summary(output_dir, results.experiment_name, rows, metadata)
    _print_summary_table(passed, total, per_key, output_dir / "summary.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
