"""Unit tests for Agent 6 deployment-parameter fallbacks."""
from __future__ import annotations

import zipfile
from io import BytesIO

from orchestrator.agents import (
    _build_artifact_bucket_name,
    _build_lambda_bootstrap_zip_bytes,
    _is_lambda_deployment_bucket_param,
    _resolve_lambda_deployment_package_key,
)
from orchestrator.migration_plan import MigrationPlan, PlannedResource


def _plan_with_lambda_bucket_ref() -> MigrationPlan:
    return MigrationPlan(
        description="",
        parameters={
            "DeploymentBucketName": {"Type": "String"},
            "DeploymentPackageKey": {"Type": "String", "Default": "function.zip"},
        },
        conditions={},
        resources=[
            PlannedResource(
                logical_id="Function",
                aws_type="AWS::Lambda::Function",
                properties={
                    "Code": {
                        "S3Bucket": {"Ref": "DeploymentBucketName"},
                        "S3Key": {"Ref": "DeploymentPackageKey"},
                    }
                },
                depends_on=[],
                source_azure_type="Microsoft.Web/sites",
            )
        ],
        outputs={},
    )


def test_is_lambda_deployment_bucket_param_detects_lambda_s3_ref():
    plan = _plan_with_lambda_bucket_ref()
    assert _is_lambda_deployment_bucket_param("DeploymentBucketName", {"Type": "String"}, plan)


def test_is_lambda_deployment_bucket_param_rejects_unrelated_name():
    plan = _plan_with_lambda_bucket_ref()
    assert not _is_lambda_deployment_bucket_param("ArtifactsBucket", {"Type": "String"}, plan)


def test_resolve_lambda_deployment_package_key_uses_override_first():
    plan = _plan_with_lambda_bucket_ref()
    key = _resolve_lambda_deployment_package_key(
        plan,
        overrides={"DeploymentPackageKey": "custom/build.zip"},
        source_secret_values={},
        source_name_candidates=[],
    )
    assert key == "custom/build.zip"


def test_build_lambda_bootstrap_zip_bytes_contains_index_js():
    payload = _build_lambda_bootstrap_zip_bytes()
    with zipfile.ZipFile(BytesIO(payload), mode="r") as archive:
        names = set(archive.namelist())
        assert "index.js" in names


def test_build_artifact_bucket_name_is_valid_length():
    name = _build_artifact_bucket_name(
        stack_name="Migrated-RG-CLOUD-MIGRATION-DEMO-VERY-LONG-NAME-WITH-EXTRA-SEGMENTS",
        account_id="123456789012",
        region="us-east-1",
    )
    assert 3 <= len(name) <= 63
    assert name == name.lower()
