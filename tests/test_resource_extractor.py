"""Tests for the resource_extractor.py / cloud_neutral.py hardening added for NSG
default-rule detection and dependsOn-based parent inference (needed so
FOLDABLE_CHILD_TYPES/IMPLICIT_NOOP_TYPES actually find their parent for
hand-authored `parent:` Bicep, not just a live `az group export`'s nested ARM)."""
from __future__ import annotations

from orchestrator.cloud_neutral import build_cnr
from orchestrator.resource_extractor import (
    FOLDABLE_CHILD_TYPES,
    IMPLICIT_NOOP_TYPES,
    is_default_shape,
)


# ---------------------------------------------------------------------------
# NSG default-rule shape detection
# ---------------------------------------------------------------------------
def test_nsg_default_rule_is_default_shape():
    assert "Microsoft.Network/networkSecurityGroups/securityRules" in IMPLICIT_NOOP_TYPES
    default_rule = {
        "priority": 65000,
        "direction": "Inbound",
        "access": "Allow",
        "protocol": "*",
        "sourceAddressPrefix": "VirtualNetwork",
        "destinationAddressPrefix": "VirtualNetwork",
    }
    assert is_default_shape("Microsoft.Network/networkSecurityGroups/securityRules", default_rule)


def test_nsg_custom_rule_is_not_default_shape():
    custom_rule = {
        "priority": 100,
        "direction": "Inbound",
        "access": "Allow",
        "protocol": "Tcp",
        "sourceAddressPrefix": "*",
        "destinationAddressPrefix": "*",
        "destinationPortRange": "443",
    }
    assert not is_default_shape(
        "Microsoft.Network/networkSecurityGroups/securityRules", custom_rule
    )


def test_nsg_rule_missing_priority_is_not_default_shape():
    assert not is_default_shape(
        "Microsoft.Network/networkSecurityGroups/securityRules", {"direction": "Inbound"}
    )


# ---------------------------------------------------------------------------
# Function App child-resource edge cases
# ---------------------------------------------------------------------------
def test_function_keys_are_treated_as_implicit_noop():
    assert "Microsoft.Web/sites/functions/keys" in IMPLICIT_NOOP_TYPES
    assert is_default_shape(
        "Microsoft.Web/sites/functions/keys",
        {"value": "not-migrated-runtime-secret"},
    )


def test_sites_functions_is_foldable_type():
    assert "Microsoft.Web/sites/functions" in FOLDABLE_CHILD_TYPES


# ---------------------------------------------------------------------------
# dependsOn-based parent inference (flat `parent:` Bicep compile shape)
# ---------------------------------------------------------------------------
def _arm_template_with_blob_container() -> dict:
    return {
        "resources": [
            {
                "type": "Microsoft.Storage/storageAccounts",
                "apiVersion": "2023-05-01",
                "name": "[parameters('storageAccountName')]",
                "properties": {"minimumTlsVersion": "TLS1_2"},
            },
            {
                "type": "Microsoft.Storage/storageAccounts/blobServices",
                "apiVersion": "2023-05-01",
                "name": "[format('{0}/{1}', parameters('storageAccountName'), 'default')]",
                "dependsOn": [
                    "[resourceId('Microsoft.Storage/storageAccounts', parameters('storageAccountName'))]"
                ],
                "properties": {"deleteRetentionPolicy": {"enabled": True, "days": 7}},
            },
        ]
    }


def _arm_template_with_function_child() -> dict:
    return {
        "resources": [
            {
                "type": "Microsoft.Web/sites",
                "apiVersion": "2022-09-01",
                "name": "[parameters('functionAppName')]",
                "properties": {},
            },
            {
                "type": "Microsoft.Web/sites/functions",
                "apiVersion": "2022-09-01",
                "name": "[format('{0}/{1}', parameters('functionAppName'), 'QueueProcessor')]",
                "dependsOn": [
                    "[resourceId('Microsoft.Web/sites', parameters('functionAppName'))]"
                ],
                "properties": {
                    "config": {
                        "bindings": [
                            {
                                "type": "queueTrigger",
                                "queueName": "jobs",
                            }
                        ]
                    }
                },
            },
        ]
    }


def _arm_template_with_storage_queue_child() -> dict:
    return {
        "resources": [
            {
                "type": "Microsoft.Storage/storageAccounts",
                "apiVersion": "2023-05-01",
                "name": "[parameters('storageAccountName')]",
                "properties": {},
            },
            {
                "type": "Microsoft.Storage/storageAccounts/queueServices",
                "apiVersion": "2023-05-01",
                "name": "[format('{0}/{1}', parameters('storageAccountName'), 'default')]",
                "dependsOn": [
                    "[resourceId('Microsoft.Storage/storageAccounts', parameters('storageAccountName'))]"
                ],
                "properties": {},
            },
            {
                "type": "Microsoft.Storage/storageAccounts/queueServices/queues",
                "apiVersion": "2023-05-01",
                "name": "[format('{0}/{1}/{2}', parameters('storageAccountName'), 'default', parameters('queueName'))]",
                "dependsOn": [
                    "[resourceId('Microsoft.Storage/storageAccounts/queueServices', parameters('storageAccountName'), 'default')]"
                ],
                "properties": {},
            },
        ]
    }


def test_blob_services_parent_inferred_from_depends_on():
    cnr = build_cnr(_arm_template_with_blob_container())
    by_type = {r.azure_type: r for r in cnr.resources}
    storage_account = by_type["Microsoft.Storage/storageAccounts"]
    blob_services = by_type["Microsoft.Storage/storageAccounts/blobServices"]
    assert blob_services.parent_logical_id == storage_account.logical_id


def test_no_parent_inferred_without_matching_depends_on():
    template = _arm_template_with_blob_container()
    template["resources"][1]["dependsOn"] = []
    cnr = build_cnr(template)
    by_type = {r.azure_type: r for r in cnr.resources}
    assert by_type["Microsoft.Storage/storageAccounts/blobServices"].parent_logical_id is None


def test_function_child_parent_inferred_from_depends_on():
    cnr = build_cnr(_arm_template_with_function_child())
    by_type = {r.azure_type: r for r in cnr.resources}
    site = by_type["Microsoft.Web/sites"]
    fn_child = by_type["Microsoft.Web/sites/functions"]
    assert fn_child.parent_logical_id == site.logical_id


def test_multisegment_child_parent_inferred_from_depends_on_args():
    cnr = build_cnr(_arm_template_with_storage_queue_child())
    by_type = {r.azure_type: r for r in cnr.resources}
    queue_services = by_type["Microsoft.Storage/storageAccounts/queueServices"]
    queue_child = by_type["Microsoft.Storage/storageAccounts/queueServices/queues"]
    assert queue_child.parent_logical_id == queue_services.logical_id


def test_nested_arm_resources_still_use_recursive_parent_linkage():
    """Genuinely nested ARM (as `az group export` emits) must keep working exactly
    as before -- the dependsOn-based inference only applies to flat top-level entries."""
    template = {
        "resources": [
            {
                "type": "Microsoft.Web/sites",
                "name": "func-app",
                "properties": {},
                "resources": [
                    {
                        "type": "Microsoft.Web/sites/config",
                        "name": "func-app/web",
                        "properties": {"alwaysOn": True},
                    }
                ],
            }
        ]
    }
    cnr = build_cnr(template)
    by_type = {r.azure_type: r for r in cnr.resources}
    site = by_type["Microsoft.Web/sites"]
    config = by_type["Microsoft.Web/sites/config"]
    assert "Microsoft.Web/sites/config" in FOLDABLE_CHILD_TYPES
    assert config.parent_logical_id == site.logical_id
