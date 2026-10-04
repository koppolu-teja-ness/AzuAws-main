"""Extracts the set of Azure resource types referenced by a compiled ARM template."""
from __future__ import annotations

# Azure auto-creates these as default child resources of storage accounts/web apps
# (they show up whenever Agent 0 exports a *live* resource group, never in hand-authored
# Bicep) and have no independent AWS resource to create -- but only dropped by Agent 1
# when is_default_shape() confirms they still hold Azure's trivial default properties,
# so a real override never gets silently discarded.
IMPLICIT_NOOP_TYPES: set[str] = {
    "Microsoft.Web/sites/hostNameBindings",
    "Microsoft.Web/sites/basicPublishingCredentialsPolicies",
    # Runtime/host function keys are data-plane secrets, not deploy-time infra.
    # They are intentionally never migrated into CloudFormation resources.
    "Microsoft.Web/sites/functions/keys",
    "Microsoft.Web/sites/host/functionKeys",
    "Microsoft.Storage/storageAccounts/blobServices",
    "Microsoft.Storage/storageAccounts/fileServices",
    "Microsoft.Storage/storageAccounts/queueServices",
    "Microsoft.Storage/storageAccounts/tableServices",
    # Every NSG always carries 6 platform-injected default rules (AllowVnetInBound,
    # AllowAzureLoadBalancerInBound, DenyAllInBound, AllowVnetOutBound,
    # AllowInternetOutBound, DenyAllOutBound) that show up as their own child
    # resources on a live export -- they have no AWS equivalent to create and
    # are never user-authored, so they're dropped the same way as the other
    # entries here (shape-checked via priority, see is_default_shape).
    "Microsoft.Network/networkSecurityGroups/securityRules",
}

# Child resources with no AWS resource of their own but that can carry real settings
# -- Agent 2 folds their properties into the parent resource instead of dropping them.
FOLDABLE_CHILD_TYPES: set[str] = {
    "Microsoft.Web/sites/config",
    # Child function metadata (bindings/disabled flags/etc.) is folded into the
    # parent Function App CNR so Agent 3 can reason about trigger shape without
    # trying to emit a standalone AWS resource for each child.
    "Microsoft.Web/sites/functions",
    "Microsoft.Storage/storageAccounts/tableServices/tables",
    # blobServices has no AWS resource of its own, but deleteRetentionPolicy/cors
    # settings on it apply to every sibling blob container -> S3 bucket (see
    # knowledge_base/storage-to-cloudformation.md), so fold rather than drop.
    "Microsoft.Storage/storageAccounts/blobServices",
}


def extract_resource_types(arm_template: dict) -> list[str]:
    """Return the distinct resource types used, including nested child resources."""
    types: set[str] = set()

    def _walk(resources: list[dict]) -> None:
        for resource in resources:
            resource_type = resource.get("type")
            if resource_type:
                types.add(resource_type)
            _walk(resource.get("resources", []))

    _walk(arm_template.get("resources", []))
    return sorted(types)


def extract_type_properties(arm_template: dict) -> dict[str, list[dict]]:
    """Map each resource type to the `properties` dict of every instance found
    (including nested children) -- used to shape-check IMPLICIT_NOOP_TYPES."""
    by_type: dict[str, list[dict]] = {}

    def _walk(resources: list[dict]) -> None:
        for resource in resources:
            resource_type = resource.get("type")
            if resource_type:
                by_type.setdefault(resource_type, []).append(resource.get("properties", {}) or {})
            _walk(resource.get("resources", []))

    _walk(arm_template.get("resources", []))
    return by_type


def is_default_shape(resource_type: str, properties: dict) -> bool:
    """True if `properties` matches Azure's trivial auto-generated default for an
    IMPLICIT_NOOP_TYPES entry -- a conservative, deterministic allowlist per type,
    never a blanket "empty means default" assumption."""
    if resource_type == "Microsoft.Web/sites/basicPublishingCredentialsPolicies":
        return set(properties) <= {"allow"}
    if resource_type == "Microsoft.Web/sites/hostNameBindings":
        return not properties.get("sslState") and not properties.get("thumbprint")
    if resource_type in {
        "Microsoft.Web/sites/functions/keys",
        "Microsoft.Web/sites/host/functionKeys",
    }:
        # These represent runtime secret material, never infrastructure intent.
        # Treat all observed shapes as noop to avoid routing secret payloads into
        # migration planning context.
        return True
    if resource_type in {
        "Microsoft.Storage/storageAccounts/blobServices",
        "Microsoft.Storage/storageAccounts/fileServices",
        "Microsoft.Storage/storageAccounts/queueServices",
        "Microsoft.Storage/storageAccounts/tableServices",
    }:
        cors_rules = (properties.get("cors") or {}).get("corsRules") or []
        retention = (properties.get("deleteRetentionPolicy") or {}).get("enabled", False)
        return not cors_rules and not retention
    if resource_type == "Microsoft.Network/networkSecurityGroups/securityRules":
        # Azure reserves priority 65000-65500 exclusively for its own 6 default rules;
        # user-defined rules must use 100-4096, so this range is an unambiguous,
        # properties-only signal -- no need to also check the (platform-fixed) rule name.
        priority = properties.get("priority")
        return isinstance(priority, int) and priority >= 65000
    return not properties
