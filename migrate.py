#!/usr/bin/env python
"""CLI entrypoint for the Azure Bicep -> AWS CloudFormation migration pipeline.

Examples:
    # Check parsing + knowledge-base coverage without calling any LLM/API
    python migrate.py main.bicep --dry-run

    # Full run (requires AWS Bedrock credentials)
    python migrate.py main.bicep --output-dir output
"""
from __future__ import annotations

import argparse
from pathlib import Path

from orchestrator import cli_ui
from orchestrator.bicep_compiler import BicepCompilerError
from orchestrator.config import Config
from orchestrator.generator import BedrockGenerator, GeneratorNotConfiguredError
from orchestrator.knowledge_base import KnowledgeBase
from orchestrator.migration_plan import MigrationPlanError
from orchestrator.pipeline import UnmappedResourceError, run_pipeline
from orchestrator.secrets_handling import install_root_redaction_filter

REPO_ROOT = Path(__file__).parent
DEFAULT_KB_INDEX = REPO_ROOT / "knowledge_base" / "index.json"


def main() -> int:
    install_root_redaction_filter()

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bicep_file", type=Path, help="Path to the .bicep source file")
    parser.add_argument(
        "--output-dir", type=Path, default=REPO_ROOT / "output",
        help="Directory to write the generated CFN YAML into",
    )
    parser.add_argument(
        "--kb-index", type=Path, default=DEFAULT_KB_INDEX,
        help="Path to the knowledge base index.json",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Only compile + check knowledge-base coverage, skip generation/LLM calls",
    )
    args = parser.parse_args()

    cli_ui.banner("Bicep -> CloudFormation migration", str(args.bicep_file))
    cli_ui.details_panel(
        "Run Context",
        {
            "Mode": "Dry run" if args.dry_run else "Full run",
            "Input": str(args.bicep_file),
            "Output dir": str(args.output_dir),
        },
    )

    config = Config.from_env()
    knowledge_base = KnowledgeBase(args.kb_index)

    generator = None
    if not args.dry_run:
        try:
            generator = BedrockGenerator(config)
        except GeneratorNotConfiguredError as exc:
            cli_ui.error(str(exc))
            cli_ui.console.print("[dim]hint: pass --dry-run to test parsing without a generator.[/dim]")
            return 1

    try:
        result = run_pipeline(
            bicep_path=args.bicep_file,
            output_dir=args.output_dir,
            knowledge_base=knowledge_base,
            generator=generator,
            config=config,
            dry_run=args.dry_run,
        )
    except (BicepCompilerError, UnmappedResourceError, GeneratorNotConfiguredError, MigrationPlanError) as exc:
        cli_ui.error(str(exc))
        return 1

    cli_ui.rule("Run Summary")
    summary_table = cli_ui.Table(show_header=True, header_style="bold cyan", border_style="dim")
    summary_table.add_column("Metric")
    summary_table.add_column("Value", style="white")
    summary_table.add_row("Resource types", ", ".join(result.resource_types) or "(none)")
    if args.dry_run:
        summary_table.add_row("Validation", "Knowledge-base coverage check only")
        cli_ui.console.print(summary_table)
        cli_ui.success("Dry run OK: all resource types have knowledge-base mappings.")
        return 0

    summary_table.add_row("Generated template", str(result.output_path))
    summary_table.add_row("cfn-lint", "Passed" if result.lint_passed else "Failed")
    cli_ui.console.print(summary_table)

    cli_ui.success(f"Generated template: {result.output_path}")
    if result.lint_passed:
        cli_ui.success("cfn-lint passed")
    else:
        cli_ui.error("cfn-lint failed")
        cli_ui.console.print(f"[dim]{result.lint_output}[/dim]")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
