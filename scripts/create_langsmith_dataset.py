#!/usr/bin/env python
"""(Re)generates evals/datasets/reference/*.json from the seed Bicep/ARM
sources, and optionally pushes them to a versioned LangSmith dataset.

Inputs (resource_types/cnr/etc.) are produced deterministically by the same
code Agents 1-2 use (bicep_compiler + cloud_neutral + resource_extractor) --
no LLM call, no AWS call, no human input() prompt. Regenerating only ever
touches the "inputs" block of each reference file; the hand-reviewed
"expected" block is preserved across re-runs (a fresh file gets a TODO stub
instead). Secrets are stripped before anything is written to disk or
uploaded: securestring/@secure() parameter defaults are replaced with a
placeholder, then the whole structure is passed through the same redact()
used for tracing (orchestrator/observability.py).

Usage:
    python scripts/create_langsmith_dataset.py                 # regenerate + push (if LANGSMITH_API_KEY set)
    python scripts/create_langsmith_dataset.py --local-only     # regenerate only, never touches the network
    python scripts/create_langsmith_dataset.py --dataset-name my-dataset-v2

See evals/datasets/README.md for the dataset layout and how to add a new example.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from orchestrator.config import Config  # noqa: E402 -- side effect: loads .env for LANGSMITH_API_KEY etc.
from orchestrator.bicep_compiler import BicepCompilerError, compile_bicep_to_arm  # noqa: E402
from orchestrator.cloud_neutral import build_cnr  # noqa: E402
from orchestrator.knowledge_base import KnowledgeBase  # noqa: E402
from orchestrator.observability import redact  # noqa: E402
from orchestrator.resource_extractor import (  # noqa: E402
    FOLDABLE_CHILD_TYPES,
    IMPLICIT_NOOP_TYPES,
    extract_resource_types,
    extract_type_properties,
    is_default_shape,
)

DATASET_VERSION = "v1"
DATASET_BASE_NAME = "bicep-to-cfn-migration"
REFERENCE_DIR = REPO_ROOT / "evals" / "datasets" / "reference"
FIXTURES_DIR = REPO_ROOT / "evals" / "fixtures" / "adversarial"
KB_INDEX = REPO_ROOT / "knowledge_base" / "index.json"
_STRIPPED = "**STRIPPED-FOR-DATASET**"

# name -> source .bicep/.json path. Add a new KB resource type's example here
# (see evals/datasets/README.md).
SEED_SOURCES = {
    "keyvault": REPO_ROOT / "resources" / "keyvault" / "main.bicep",
    "vpc": REPO_ROOT / "resources" / "vpc" / "main.bicep",
    "functions": REPO_ROOT / "resources" / "functions" / "main.bicep",
    "messaging": REPO_ROOT / "resources" / "messaging" / "main.bicep",
    "e2e_full_scope": REPO_ROOT / "resources" / "e2e_full_scope" / "main.bicep",
    "adversarial_missing_property": FIXTURES_DIR / "missing_property.json",
    "adversarial_secret_param": FIXTURES_DIR / "secret_param.json",
}

_STUB_EXPECTED = {
    "expected_aws_resource_types": [],
    "expected_parameter_names": [],
    "must_be_noecho": [],
    "forbidden_patterns": [],
    "expected_min_resource_count": 0,
    "notes": "TODO: human review needed -- auto-generated stub, fill in expected outputs.",
}


def _classify_resources(arm_template: dict, knowledge_base: KnowledgeBase) -> dict:
    """Mirrors Agent 1's deterministic noop/foldable/unsupported split -- no
    human input() involved (that only happens inside the real agent1_validate
    graph node, which this script never calls)."""
    all_types = extract_resource_types(arm_template)
    type_properties = extract_type_properties(arm_template)
    noop_types = [
        t for t in all_types if t in IMPLICIT_NOOP_TYPES and all(is_default_shape(t, p) for p in type_properties.get(t, []))
    ]
    foldable_types = [t for t in all_types if t in FOLDABLE_CHILD_TYPES]
    resource_types = [t for t in all_types if t not in noop_types and t not in foldable_types]
    supported = set(knowledge_base.supported_types())
    return {
        "all_resource_types": all_types,
        "resource_types": resource_types,
        "unsupported_types": [t for t in resource_types if t not in supported],
        "noop_types": noop_types,
        "foldable_types": foldable_types,
    }


def _strip_dataset_secrets(cnr) -> dict:
    """Replace any secure-parameter literal default with a placeholder, then
    apply the same key/value redaction used for tracing as defense in depth."""
    cnr_dict = dataclasses.asdict(cnr)
    for param in cnr_dict.get("parameters", []):
        if param.get("secure") and param.get("default") is not None:
            param["default"] = _STRIPPED
    return redact(cnr_dict)


def _strip_arm_template_secrets(arm_template: dict) -> dict:
    """Same secure-parameter stripping as _strip_dataset_secrets, applied to the
    raw ARM template kept for target_pipeline (orchestrator/evaluation.py).
    Only `resources` goes through the generic key-based redact() -- ARM's
    `parameters` dict is keyed by the parameter's own name (e.g. "dbPassword"),
    which would otherwise collapse the whole {type, defaultValue} definition
    instead of just the secret value, breaking downstream dict-shaped reads."""
    stripped = json.loads(json.dumps(arm_template))
    for definition in (stripped.get("parameters") or {}).values():
        if "secure" in str(definition.get("type", "")).lower() and definition.get("defaultValue") is not None:
            definition["defaultValue"] = _STRIPPED
    if "resources" in stripped:
        stripped["resources"] = redact(stripped["resources"])
    return stripped


def build_inputs(source_path: Path, knowledge_base: KnowledgeBase) -> dict:
    if source_path.suffix == ".json":
        arm_template = json.loads(source_path.read_text(encoding="utf-8"))
    else:
        arm_template = compile_bicep_to_arm(source_path)
    inputs = _classify_resources(arm_template, knowledge_base)
    inputs["source"] = str(source_path.relative_to(REPO_ROOT)).replace("\\", "/")
    inputs["arm_template"] = _strip_arm_template_secrets(arm_template)
    inputs["cnr"] = _strip_dataset_secrets(build_cnr(arm_template))
    return inputs


def write_reference_file(name: str, inputs: dict, kind: str) -> Path:
    path = REFERENCE_DIR / f"{name}.json"
    expected = _STUB_EXPECTED
    if path.exists():
        try:
            expected = json.loads(path.read_text(encoding="utf-8")).get("expected", _STUB_EXPECTED)
        except json.JSONDecodeError:
            pass  # corrupt file -- fall back to a fresh stub rather than crash
    doc = {
        "name": name,
        "example_key": f"{name}-{DATASET_VERSION}",
        "kind": kind,  # "seed" | "adversarial"
        "inputs": inputs,
        "expected": expected,
    }
    REFERENCE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    return path


def regenerate_all() -> list[Path]:
    knowledge_base = KnowledgeBase(KB_INDEX)
    written = []
    for name, source_path in SEED_SOURCES.items():
        kind = "adversarial" if name.startswith("adversarial_") else "seed"
        try:
            inputs = build_inputs(source_path, knowledge_base)
        except BicepCompilerError as exc:
            print(f"warning: skipping '{name}' ({source_path}): {exc}", file=sys.stderr)
            continue
        written.append(write_reference_file(name, inputs, kind))
    return written


def push_to_langsmith(dataset_name: str, reference_files: list[Path]) -> None:
    from langsmith import Client

    client = Client()
    if not client.has_dataset(dataset_name=dataset_name):
        client.create_dataset(
            dataset_name=dataset_name,
            description="Bicep -> CloudFormation migration eval dataset (Agent 3 + pipeline trajectory).",
        )

    for path in reference_files:
        doc = json.loads(path.read_text(encoding="utf-8"))
        example_key = doc["example_key"]
        payload = {
            "inputs": doc["inputs"],
            "outputs": doc["expected"],
            "metadata": {"example_key": example_key, "kind": doc["kind"], "name": doc["name"]},
        }
        existing = list(client.list_examples(dataset_name=dataset_name, metadata={"example_key": example_key}))
        if existing:
            client.update_examples(dataset_name=dataset_name, updates=[{"id": existing[0].id, **payload}])
            print(f"  updated example '{example_key}'")
        else:
            client.create_examples(dataset_name=dataset_name, examples=[payload])
            print(f"  created example '{example_key}'")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dataset-name", default=f"{DATASET_BASE_NAME}-{DATASET_VERSION}")
    parser.add_argument(
        "--local-only", action="store_true",
        help="Only (re)write evals/datasets/reference/*.json; never contacts LangSmith.",
    )
    args = parser.parse_args()

    written = regenerate_all()
    print(f"Wrote {len(written)} reference file(s) to {REFERENCE_DIR.relative_to(REPO_ROOT)}:")
    for p in written:
        print(f"  - {p.relative_to(REPO_ROOT)}")

    if args.local_only:
        return 0
    if not (os.environ.get("LANGSMITH_API_KEY") or "").strip():
        print("LANGSMITH_API_KEY not set -- skipping LangSmith push (reference files were still written).", file=sys.stderr)
        return 0

    print(f"Pushing to LangSmith dataset '{args.dataset_name}'...")
    push_to_langsmith(args.dataset_name, written)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
