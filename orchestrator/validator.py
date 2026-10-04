"""Static validation of a generated CloudFormation template."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def run_cfn_lint(template_path: Path) -> tuple[bool, str]:
    """Run cfn-lint against the template. Returns (passed, combined_output)."""
    cfn_lint_exe = Path(sys.prefix) / "Scripts" / "cfn-lint.exe"
    cmd = [str(cfn_lint_exe) if cfn_lint_exe.exists() else "cfn-lint", str(template_path)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    output = (result.stdout or "") + (result.stderr or "")
    return result.returncode == 0, output
