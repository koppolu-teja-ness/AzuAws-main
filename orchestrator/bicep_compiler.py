"""Compiles a .bicep file to ARM JSON using the Azure CLI (`az bicep build`).

We parse the compiled ARM JSON rather than the raw .bicep syntax because it
gives a stable, well-defined structure (resources[].type) regardless of Bicep
language features (loops, modules, conditionals) used in the source.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path


class BicepCompilerError(RuntimeError):
    pass


def ensure_az_cli_available() -> None:
    if shutil.which("az") is None:
        raise BicepCompilerError(
            "Azure CLI ('az') was not found on PATH. Install it or run "
            "'az bicep install' so main.bicep can be compiled to ARM JSON."
        )


def compile_bicep_to_arm(bicep_path: Path) -> dict:
    """Compile a .bicep file and return the parsed ARM JSON template."""
    ensure_az_cli_available()
    if not bicep_path.exists():
        raise BicepCompilerError(f"Bicep file not found: {bicep_path}")

    result = subprocess.run(
        ["az", "bicep", "build", "--file", str(bicep_path), "--stdout"],
        capture_output=True,
        text=True,
        shell=True,
    )
    if result.returncode != 0:
        raise BicepCompilerError(
            f"'az bicep build' failed for {bicep_path}:\n{result.stderr}"
        )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise BicepCompilerError(
            f"Could not parse ARM JSON produced from {bicep_path}: {exc}"
        ) from exc
