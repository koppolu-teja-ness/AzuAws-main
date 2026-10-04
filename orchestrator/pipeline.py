"""End-to-end pipeline: Bicep -> Parser/Discovery -> Cloud-Neutral Representation
-> LLM reasoning (Migration Plan) -> CloudFormation Generator, with static
validation and a self-correction loop that feeds cfn-lint errors back into the
migration plan.

Deliberately NOT a single "Bicep -> CloudFormation" LLM call: the LLM only ever
reasons over the cloud-neutral representation and returns a structured migration
plan; rendering that plan into CFN template syntax is a separate, deterministic
step. That split is what lets us add new source parsers or new target generators
independently later.
"""
from __future__ import annotations

from pathlib import Path

from .bicep_compiler import BicepCompilerError, compile_bicep_to_arm
from .cfn_generator import generate_cloudformation
from .cloud_neutral import CloudNeutralRepresentation, build_cnr
from .config import Config
from .generator import Generator, GeneratorNotConfiguredError
from .knowledge_base import KnowledgeBase
from .migration_plan import (
    MigrationPlan,
    MigrationPlanError,
    PLAN_JSON_SCHEMA_HINT,
    build_migration_plan_prompt,
    parse_migration_plan,
)
from .rag import build_kb_query, retrieve_mapping_docs
from .resource_extractor import extract_resource_types
from .validator import run_cfn_lint


class UnmappedResourceError(RuntimeError):
    """Raised when the Bicep template uses a resource type with no knowledge-base doc."""


class PipelineResult:
    def __init__(
        self,
        resource_types: list[str],
        output_path: Path | None,
        lint_passed: bool | None,
        lint_output: str | None,
        migration_plan: MigrationPlan | None = None,
    ):
        self.resource_types = resource_types
        self.output_path = output_path
        self.lint_passed = lint_passed
        self.lint_output = lint_output
        self.migration_plan = migration_plan


def run_pipeline(
    bicep_path: Path,
    output_dir: Path,
    knowledge_base: KnowledgeBase,
    generator: Generator | None,
    config: Config,
    dry_run: bool = False,
) -> PipelineResult:
    # 1. Parser/Discovery: compile Bicep -> ARM JSON (stable structure to parse)
    arm_template = compile_bicep_to_arm(bicep_path)

    # 2. Identify resource types in use
    resource_types = extract_resource_types(arm_template)

    # 3. Knowledge lookup: stop and flag anything with no mapping doc yet
    missing = knowledge_base.missing_types(resource_types)
    if missing:
        raise UnmappedResourceError(
            "The following resource types have no mapping doc in the knowledge "
            f"base yet, add one before continuing: {', '.join(missing)}"
        )

    if dry_run:
        return PipelineResult(resource_types, None, None, None)

    if generator is None:
        raise GeneratorNotConfiguredError(
            "No generator configured. Set up Bedrock credentials or pass --dry-run."
        )

    # 4. Cloud-Neutral Representation: normalize out of Bicep/ARM-specific shape
    cnr: CloudNeutralRepresentation = build_cnr(arm_template)

    # 5-7. LLM reasoning -> Migration Plan -> deterministic CFN render -> cfn-lint,
    # with a unified retry loop: both invalid plans (e.g. undeclared conditions)
    # and lint failures are fed back to the LLM as a corrected-plan request.
    query = build_kb_query(cnr, resource_types)
    mapping_docs = retrieve_mapping_docs(knowledge_base, resource_types, query, config)
    prompt = build_migration_plan_prompt(cnr, mapping_docs)

    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{bicep_path.stem}.generated.yaml"

    attempts = 0
    while True:
        raw_plan_text = generator.generate(prompt)
        try:
            plan = parse_migration_plan(raw_plan_text)
        except MigrationPlanError as exc:
            if attempts >= config.max_fix_attempts:
                raise
            attempts += 1
            prompt = (
                f"Your migration plan was invalid: {exc}\n\n"
                f"Here is the plan JSON you produced:\n```json\n{raw_plan_text}\n```\n\n"
                f"Return a corrected migration plan fixing this.\n\n{PLAN_JSON_SCHEMA_HINT}"
            )
            continue

        yaml_text = generate_cloudformation(plan)
        output_path.write_text(yaml_text, encoding="utf-8")
        lint_passed, lint_output = run_cfn_lint(output_path)

        if lint_passed or attempts >= config.max_fix_attempts:
            break
        attempts += 1
        prompt = (
            f"The CloudFormation template rendered from your migration plan failed "
            f"cfn-lint with the following output:\n\n{lint_output}\n\n"
            f"Here is the migration plan JSON you produced:\n```json\n{raw_plan_text}\n```\n\n"
            f"Here is the rendered template for reference:\n```yaml\n{yaml_text}\n```\n\n"
            f"Return a corrected migration plan fixing these issues.\n\n{PLAN_JSON_SCHEMA_HINT}"
        )

    return PipelineResult(resource_types, output_path, lint_passed, lint_output, plan)
