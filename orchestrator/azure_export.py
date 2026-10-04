"""Exports an existing Azure resource group as ARM JSON.

Uses `az group export` to pull the resource group's live ARM template. This
lets the migration pipeline start directly from a real Azure account instead
of requiring a hand-authored .bicep file. No `az bicep decompile` step -- the
rest of the pipeline (bicep_compiler.compile_bicep_to_arm) only ever needs
ARM JSON too, and decompiling is a lossy, best-effort conversion that has
introduced real bugs (see _dedupe_inline_subnets below).
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .bicep_compiler import ensure_az_cli_available


class AzureExportError(RuntimeError):
    pass


# Agent 0's interactive "which service?" menu -- key is the input() choice,
# value is (display label, ARM resource types that make up that service for
# export filtering). Kept in sync with knowledge_base/index.json's coverage.
SERVICE_OPTIONS: dict[str, tuple[str, list[str]]] = {
    "1": ("Key Vault", ["Microsoft.KeyVault/vaults", "Microsoft.KeyVault/vaults/secrets"]),
    "2": ("Functions", ["Microsoft.Web/sites", "Microsoft.Web/serverfarms", "Microsoft.Storage/storageAccounts"]),
    "3": ("VNet", ["Microsoft.Network/virtualNetworks", "Microsoft.Network/virtualNetworks/subnets"]),
}


def _dedupe_inline_subnets(arm_template: dict) -> dict:
    """`az group export` emits each subnet both inline (VNet's properties.subnets)
    and as its own standalone Microsoft.Network/virtualNetworks/subnets resource
    with a dependsOn back to the VNet -- `az bicep decompile` then turns the
    inline entry's `id` into a reference to that standalone resource, creating a
    VNet<->subnet cycle (BCP080). Drop the inline copy whenever a standalone
    resource already covers it; the standalone one alone decompiles cleanly.
    """
    standalone_subnets = {
        resource["name"].split("/")[-1]
        for resource in arm_template.get("resources", [])
        if resource.get("type") == "Microsoft.Network/virtualNetworks/subnets"
    }
    if not standalone_subnets:
        return arm_template
    for resource in arm_template.get("resources", []):
        if resource.get("type") == "Microsoft.Network/virtualNetworks":
            resource.get("properties", {}).pop("subnets", None)
    return arm_template


def list_resource_ids_by_type(
    resource_group: str, azure_types: list[str], subscription_id: str | None = None
) -> list[str]:
    """Return the Azure resource IDs in `resource_group` whose type is one of `azure_types`."""
    ensure_az_cli_available()
    resource_ids: list[str] = []
    for azure_type in azure_types:
        list_cmd = [
            "az", "resource", "list", "--resource-group", resource_group,
            "--resource-type", azure_type, "--query", "[].id", "-o", "tsv",
        ]
        if subscription_id:
            list_cmd += ["--subscription", subscription_id]
        result = subprocess.run(list_cmd, capture_output=True, text=True, shell=True)
        if result.returncode != 0:
            raise AzureExportError(
                f"'az resource list' failed for type '{azure_type}' in resource group '{resource_group}':\n"
                f"{result.stderr}"
            )
        resource_ids += [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return resource_ids


def export_resource_group_to_bicep(
    resource_group: str,
    input_dir: Path,
    subscription_id: str | None = None,
    resource_ids: list[str] | None = None,
) -> Path:
    """Export `resource_group` from Azure as ARM JSON under `input_dir`. Returns
    the path to the resulting .json file. When `resource_ids` is given, only
    those resources are exported (used to scope the export to one service's
    resources instead of the whole group).

    `az group export` already returns ARM JSON -- the rest of the pipeline
    (bicep_compiler.compile_bicep_to_arm) only ever needs ARM JSON too, so we
    skip `az bicep decompile`/`az bicep build` entirely for this path rather
    than round-tripping JSON -> Bicep -> JSON. That round trip was also the
    source of real bugs (the decompiler introduced a circular VNet<->subnet
    reference that didn't exist in the original export -- see
    _dedupe_inline_subnets), so skipping it removes a whole class of
    decompiler-fidelity issues, not just the extra subprocess call.
    """
    ensure_az_cli_available()
    input_dir.mkdir(parents=True, exist_ok=True)

    export_cmd = ["az", "group", "export", "--name", resource_group]
    if subscription_id:
        export_cmd += ["--subscription", subscription_id]
    if resource_ids:
        export_cmd += ["--resource-ids", *resource_ids]
    result = subprocess.run(export_cmd, capture_output=True, text=True, shell=True)
    if result.returncode != 0:
        raise AzureExportError(
            f"'az group export' failed for resource group '{resource_group}':\n{result.stderr}"
        )
    try:
        arm_template = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise AzureExportError(
            f"Could not parse ARM JSON exported from resource group '{resource_group}': {exc}"
        ) from exc
    arm_template = _dedupe_inline_subnets(arm_template)

    json_path = input_dir / f"{resource_group}.json"
    json_path.write_text(json.dumps(arm_template, indent=2), encoding="utf-8")
    return json_path


def fetch_resource_group_secret_values(
    resource_group: str, subscription_id: str | None = None
) -> tuple[dict[str, str], list[str], list[str]]:
    """Best-effort fetch of every Key Vault secret's real value in `resource_group`,
    keyed by secret name -- ARM/Bicep exports never carry secret values (Azure's
    control-plane export API omits them by design), so this reads them directly
    from each vault's data plane instead. Requires the az CLI identity to have
    secret-read access (e.g. "Key Vault Secrets User"); any failure is skipped
    rather than raised (a missing value just falls back further downstream),
    but is returned as a warning so the caller can surface it instead of the
    fetch silently doing nothing. Returns (secret_values, vault_names, warnings)
    -- vault_names lets the caller fall back to the source vault's own name for
    naming/prefix-style parameters that have no Default.
    """
    values: dict[str, str] = {}
    warnings: list[str] = []
    list_cmd = ["az", "keyvault", "list", "--resource-group", resource_group, "--query", "[].name", "-o", "tsv"]
    if subscription_id:
        list_cmd += ["--subscription", subscription_id]
    result = subprocess.run(list_cmd, capture_output=True, text=True, shell=True)
    if result.returncode != 0:
        warnings.append(f"'az keyvault list' failed for resource group '{resource_group}': {result.stderr.strip()}")
        return values, [], warnings

    vault_names = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    for vault_name in vault_names:
        list_secrets_cmd = [
            "az", "keyvault", "secret", "list", "--vault-name", vault_name, "--query", "[].name", "-o", "tsv",
        ]
        secrets_result = subprocess.run(list_secrets_cmd, capture_output=True, text=True, shell=True)
        if secrets_result.returncode != 0:
            warnings.append(f"Could not list secrets in vault '{vault_name}': {secrets_result.stderr.strip()}")
            continue
        for secret_name in (line.strip() for line in secrets_result.stdout.splitlines() if line.strip()):
            show_cmd = [
                "az", "keyvault", "secret", "show", "--vault-name", vault_name, "--name", secret_name,
                "--query", "value", "-o", "tsv",
            ]
            show_result = subprocess.run(show_cmd, capture_output=True, text=True, shell=True)
            if show_result.returncode == 0 and show_result.stdout.strip():
                values[secret_name] = show_result.stdout.strip()
            else:
                warnings.append(f"Could not read secret '{secret_name}' in vault '{vault_name}': {show_result.stderr.strip()}")
    return values, vault_names, warnings
