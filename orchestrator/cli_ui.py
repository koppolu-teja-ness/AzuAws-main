"""Shared Rich console helpers for consistent, colored CLI output across
migrate.py, migrate_agents.py, the human-in-the-loop gates in agents.py, and
evals/run_eval.py. Purely cosmetic -- no behavior here affects pipeline logic.
"""
from __future__ import annotations

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

console = Console()

_AGENT_LABELS = {
    "agent0_export_resource_group": "Agent 0 - Export Resource Group",
    "agent1_validate": "Agent 1 - Validate Source",
    "agent2_build_cnr": "Agent 2 - Build CNR",
    "agent3_map_resources": "Agent 3 - Map Resources",
    "plan_approval_gate": "Plan Approval Gate",
    "agent4_render": "Agent 4 - Render CFN",
    "agent5_validate_cfn": "Agent 5 - Validate CFN",
    "bump_fix_attempts": "Retry Controller",
    "lint_give_up": "Retry Exhausted",
    "guardrail_scan_gate": "Guardrail Scan Gate",
    "stack_check_gate": "Stack Check Gate",
    "agent6_deploy": "Agent 6 - Deploy",
    "agent7_report": "Agent 7 - Report",
}

_STATUS_BADGES = {
    "ok": "[bold green]OK[/bold green]",
    "warning": "[bold yellow]WARN[/bold yellow]",
    "stopped": "[bold red]STOPPED[/bold red]",
    "failed": "[bold red]FAILED[/bold red]",
}


def banner(title: str, subtitle: str | None = None) -> None:
    """A boxed title, printed once at the start of a CLI run."""
    body = f"[bold]{title}[/bold]" + (f"\n[dim]{subtitle}[/dim]" if subtitle else "")
    console.print(Panel.fit(body, border_style="cyan"))


def step(agent: str, message: str) -> None:
    """One agent's progress/status line, e.g. '[Agent 1] validating...'."""
    console.print(f"\n[bold cyan][{agent}][/bold cyan] {message}")


def success(message: str) -> None:
    console.print(f"[bold green]\u2713[/bold green] {message}")


def warning(message: str) -> None:
    console.print(f"[bold yellow]\u26a0[/bold yellow] {message}")


def error(message: str) -> None:
    console.print(f"[bold red]\u2717[/bold red] {message}")


def rule(title: str = "") -> None:
    console.rule(title, style="cyan")


def details_panel(title: str, details: dict[str, str]) -> None:
    lines = [f"[bold]{key}:[/bold] {value}" for key, value in details.items()]
    console.print(Panel("\n".join(lines), title=title, border_style="dim"))


def gate_context_table(details: dict[str, str], title: str = "Gate Context") -> Table:
    table = Table(title=title, show_header=True, header_style="bold cyan", border_style="dim")
    table.add_column("Field", style="white")
    table.add_column("Value", style="green")
    for key, value in details.items():
        table.add_row(str(key), str(value))
    return table


def mapping_table(rows: list[dict]) -> Table:
    table = Table(show_header=True, header_style="bold cyan", border_style="dim")
    table.add_column("Logical ID", style="white")
    table.add_column("Azure type", style="magenta")
    table.add_column("AWS type", style="green")
    for m in rows:
        table.add_row(m["logical_id"], m["source_azure_type"], m["aws_type"])
    return table


def _choice_table(options: list[tuple[str, str, str]]) -> Table:
    table = Table(show_header=True, header_style="bold cyan", border_style="dim")
    table.add_column("Key", justify="center", style="bold white")
    table.add_column("Action", style="white")
    table.add_column("Outcome", style="dim")
    for key, action, outcome in options:
        table.add_row(key, action, outcome)
    return table


def select_option(prompt: str, options: dict[str, str], default: str | None = None) -> str:
    choice_rows = [(key, label, "") for key, label in options.items()]
    console.print(_choice_table(choice_rows))
    suffix = f" [default: {default}]" if default else ""
    answer = console.input(f"[bold yellow]{prompt}{suffix}[/bold yellow] ").strip().lower()
    if not answer and default is not None:
        return default.lower()
    return answer


def confirm(
    prompt: str,
    *,
    yes_label: str = "Approve and continue",
    no_label: str = "Reject and stop",
    default: str = "n",
) -> bool:
    console.print(Panel(prompt, border_style="yellow"))
    console.print(
        _choice_table(
            [
                ("y", yes_label, "Continue"),
                ("n", no_label, "Stop this run"),
            ]
        )
    )
    answer = console.input(f"[bold yellow]Choose [y/n] (default: {default})[/bold yellow] ").strip().lower()
    if not answer:
        answer = default.lower()
    return answer == "y"


def ask(prompt: str) -> str:
    return console.input(f"[bold yellow]{prompt}[/bold yellow] ").strip()


def _truncate(value: str, max_len: int) -> str:
    if len(value) <= max_len:
        return value
    return value[: max_len - 3] + "..."


def human_agent_name(agent: str) -> str:
    return _AGENT_LABELS.get(agent, agent)


def status_badge(status: str) -> str:
    return _STATUS_BADGES.get(status.lower(), status.upper())


def agent_log_table(agent_log: list[dict], message_max_len: int = 140) -> Table:
    table = Table(show_header=True, header_style="bold cyan", border_style="dim")
    table.add_column("Agent", style="white")
    table.add_column("Status", justify="center")
    table.add_column("Result", style="white")
    for index, entry in enumerate(agent_log):
        message = _truncate(str(entry.get("message", "")).replace("\n", " "), message_max_len)
        row_style = "" if index % 2 == 0 else "dim"
        table.add_row(
            human_agent_name(str(entry.get("agent", ""))),
            status_badge(str(entry.get("status", ""))),
            message,
            style=row_style,
        )
    return table


def agent_phase(agent: str, activity: str) -> None:
    """Show the current activity for one agent."""
    console.print(f"\n[bold cyan][{agent}][/bold cyan] {activity}")


def agent_result(agent: str, status: str, message: str) -> None:
    """Show one concise result line for an agent step."""
    prefix = f"[{agent}] "
    if status == "ok":
        success(prefix + message)
    elif status == "warning":
        warning(prefix + message)
    else:
        error(prefix + message)


def plan_parameters_table(parameters: dict) -> Table:
    table = Table(show_header=True, header_style="bold cyan", border_style="dim")
    table.add_column("Parameter")
    table.add_column("Type", style="magenta")
    table.add_column("Required", justify="center")
    table.add_column("Secret", justify="center")
    table.add_column("Default", style="green")
    for name, definition in parameters.items():
        default = definition.get("Default")
        required = "No" if default is not None else "Yes"
        table.add_row(
            name,
            str(definition.get("Type", "String")),
            required,
            "Yes" if definition.get("NoEcho") else "No",
            "(none)" if default is None else str(default),
        )
    return table


def plan_outputs_table(outputs: dict) -> Table:
    table = Table(show_header=True, header_style="bold cyan", border_style="dim")
    table.add_column("Output")
    table.add_column("Value preview", style="green")
    for name, value in outputs.items():
        preview = str(value).replace("\n", " ")
        if len(preview) > 90:
            preview = preview[:87] + "..."
        table.add_row(name, preview)
    return table


def plan_conditions_table(conditions: dict) -> Table:
    table = Table(show_header=True, header_style="bold cyan", border_style="dim")
    table.add_column("Condition")
    table.add_column("Expression preview", style="green")
    for name, expr in conditions.items():
        preview = str(expr).replace("\n", " ")
        if len(preview) > 100:
            preview = preview[:97] + "..."
        table.add_row(name, preview)
    return table


_SEVERITY_STYLES = {"CRITICAL": "bold red", "HIGH": "red", "MEDIUM": "yellow", "LOW": "dim"}


def guardrail_findings_table(findings: list[dict]) -> Table:
    table = Table(show_header=True, header_style="bold cyan", border_style="dim")
    table.add_column("Severity")
    table.add_column("Source", style="magenta")
    table.add_column("Check")
    table.add_column("Resource", style="white")
    table.add_column("Message", style="white")
    for f in findings:
        severity = str(f.get("severity", ""))
        style = _SEVERITY_STYLES.get(severity, "")
        table.add_row(
            f"[{style}]{severity}[/{style}]" if style else severity,
            str(f.get("source", "")),
            str(f.get("check_id", "")),
            str(f.get("resource", "")),
            _truncate(str(f.get("message", "")).replace("\n", " "), 90),
        )
    return table
